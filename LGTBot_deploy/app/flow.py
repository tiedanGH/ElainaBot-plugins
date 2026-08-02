"""指令解析 + 上传流程编排: 取引用文件 → 下载 → 解压/校验 → 审核 → 部署 → 记录 → @通知。

指令形态 (唯一上传目录 lgtbot, 路径在面板配置):
    /upload                    引用压缩包消息 → 解压到 <上传目录>/<压缩包名>/
    /upload <文件夹名>          引用单文件消息 → 写入 <上传目录>/<文件夹名>/<文件名>
    /upload force [文件夹名]    仅主人: 跳过内容审核直传 (force 可简写 f)
    /upload help               查看指令帮助

同名目录 / 同名文件一律直接替换 (旧内容按配置备份到 data/backups)。

force 只跳过**内容审核**: 压缩包的成员名校验、体积/数量限额、必需文件清单、
落地路径越界校验一律照做 —— 那些是服务器完整性的底线, 与审不审内容无关。
主人权限由框架的 owner_only 判定 (见 main.py), flow 这边只按 force 标记走流程;
force 由主人本人发起, 完成后不再 @ 通知部署人员。

设计要点:
  · 指令**所有人可执行**, 但只在 config.allowed_groups 列出的群里生效;
    其它群一律静默 (只记日志), 避免把提示刷到无关群。
  · 收到文件后**先回一条确认消息**再进入审核 —— 让上传者立刻知道服务在线;
    这条消息里不写任何审核细节。
  · 审核 + 部署放到后台任务里跑: 框架对 handler 有 300 秒硬超时, 而一次
    带图审核可能更久, 放后台可避免被取消。发送结果时优先被动 reply
    (不占主动消息额度), 失败再退主动消息。
  · 任何一步失败都写记录 + 在群里给结论, 且**无论结果如何都 @ 部署人员**。
  · 同时只处理一个上传任务 (解压 / 审核都吃内存与带宽), 排队请求直接拒绝。
"""

from __future__ import annotations

import asyncio
import os
import time

from core.base.logger import PLUGIN, get_logger, report_error

from . import archive, config, deploy, quoted, review, store
from . import compile as compilemod

log = get_logger(PLUGIN, 'LGTBot_deploy')

_tasks: set = set()
# 「同时只处理一个上传」的占位标记。用同步标记而不是 asyncio.Lock:
# 流程跑在 create_task 里, 任务被创建到真正开始执行之间有一个事件循环间隙,
# 那期间 Lock 还没被持有, 第二条指令会被误判为空闲而一起受理。标记在
# handle() 里同步置位 (置位与判定之间没有 await), 因此不存在这个窗口。
_busy = False

HELP_KEYWORDS = ('help', '帮助', 'list', '列表')


def usage() -> str:
    """帮助文案 (必需文件清单实时取自配置)。"""
    required = config.all_config().get('required_files') or []
    lines = [
        '📋 lgtbot 文件上传指令帮助',
        '',
        '-> 上传群文件压缩包至 lgtbot',
        '/upload',
        '-> 上传单文件至 lgtbot 下的指定文件夹',
        '/upload <文件夹名>',
        '-> 强制上传, 跳过内容审核 (仅主人)',
        '/upload force [文件夹名]',
        '',
        '🔸上传需引用一条群文件消息, 审核通过后自动落地',
        '🔸压缩包: 解压至 lgtbot 下的同名文件夹, 重名文件夹直接替换',
        '🔸单文件: 写入 lgtbot 下已存在的指定文件夹, 重名文件直接替换',
        '🔸压缩包支持 zip / tar.gz / tar.bz2 / tar.xz',
    ]
    if required:
        lines.append('🔸压缩包必须包含: ' + '、'.join(required))
    lines.append('🔸rule.md 需注明游戏原型出处')
    lines.append('🔸新游戏上传成功后目录自动绑定上传者, 此后仅绑定用户可更新该目录')
    lines.append('🔸审核通过后自动请求编译并回报结果')
    return '\n'.join(lines)


# ==================== 消息发送 ====================

async def _send(event, text: str):
    """优先被动回复 (不占主动额度); 失败退回主动群消息。"""
    try:
        if await event.reply(text):
            return
    except Exception as e:  # noqa: BLE001
        log.warning(f'被动回复失败, 改用主动消息: {e}')
    try:
        await event.send_to_group(event.group_id, text)
    except Exception as e:  # noqa: BLE001
        log.warning(f'主动消息发送失败: {e}')


def _mentions(cfg: dict, record: dict | None = None) -> str:
    """结论消息里的 @ 部署人员串; force 指令由主人发起, 不再 @ 通知。"""
    if record and record.get('forced'):
        return ''
    users = cfg.get('notify_users') or []
    return ' '.join(f'<@{uid}>' for uid in users)


# ==================== 主入口 ====================

async def handle(event, argline: str, force: bool = False) -> bool:
    """指令入口: 解析 ``/upload`` 参数 + 校验后把耗时流程丢到后台任务。

    ``argline`` 是可选的文件夹名 (已剥掉 force 关键字)。``force=True`` 表示这次
    由主人发起、跳过内容审核 —— 该权限已由 main.py 的 owner_only 把关。
    返回 True 表示已受理 (后台在跑), False 表示未受理。

    普通调用在「插件关闭」「群不在白名单」时**静默返回**, 避免把提示刷到无关群;
    force 调用一定来自主人, 这两种情况改为明确回一句, 免得主人对着没反应的指令排查。
    """
    cfg = config.all_config(refresh=True)
    if not cfg.get('enabled'):
        if force:
            await _send(event, '❌ 插件已在后台面板关闭, 请先启用')
        return False
    if not config.is_group_allowed(event.group_id):
        log.info(f'群 {event.group_id} 不在允许列表, 忽略 /upload 指令')
        if force:
            await _send(event, '❌ 当前群不在允许列表, 请先在后台面板「生效范围」添加本群')
        return False

    args = (argline or '').split()
    if args and args[0].lower() in HELP_KEYWORDS:
        await _send(event, usage())
        return False
    if len(args) > 1:
        prefix = '/upload force' if force else '/upload'
        await _send(event, f'❌ 参数过多\n用法: {prefix} [文件夹名]\n发送 /upload help 查看帮助')
        return False

    target = config.upload_target()
    err = deploy.check_target(target)
    if err:
        await _send(event, f'❌ {err}')
        return False

    folder = args[0] if args else ''
    if folder:
        name_err = deploy.bad_name(folder)
        if name_err:
            await _send(event, f'❌ {name_err}')
            return False

    ref = quoted.extract_file_ref(event, allow_any=True)
    if not ref:
        rid = store.new_record_id()
        saved = store.save_diagnostic(rid, quoted.debug_payload(event))
        log.info(f'未能从引用消息定位文件, 原始载荷已存 {saved or "(保存失败)"}')
        await _send(event, '❌ 未检测到文件, 请引用一条群文件消息后再执行本指令\n发送 /upload help 查看帮助')
        return False

    fname = store.safe_filename(ref.get('filename'))
    is_archive = quoted.archive_ext(fname) in quoted.ARCHIVE_EXTS
    if not folder and not is_archive:
        await _send(event, f'❌ 引用的文件「{fname}」不是支持的压缩包\n'
                           '压缩包支持 zip / tar.gz / tar.bz2 / tar.xz；\n'
                           '如需上传单文件, 请指定目标文件夹: /upload <文件夹名>')
        return False
    if not folder:
        base_err = deploy.bad_name(deploy.strip_archive_ext(fname))
        if base_err:
            await _send(event, f'❌ 压缩包{base_err}')
            return False

    # 目录更新权限: 已存在的目录仅绑定用户可更新 (force 由主人发起, 不受限);
    # 未通过权限校验的请求直接拒绝, **不进入审核流程**。
    game = folder if folder else deploy.strip_archive_ext(fname)
    exists = os.path.isdir(os.path.join(target['path'], game))
    if exists and not force:
        owner = store.perm_get(game)
        if owner is None:
            await _send(event, f'❌ 权限不足: 目录「{game}」尚未绑定更新权限\n'
                               '已有游戏目录仅绑定用户可更新, 请联系管理员在后台「权限管理」页绑定')
            return False
        if owner.get('user_id') != (event.user_id or ''):
            shown = owner.get('username') or (owner.get('user_id') or '')[:8] + '…'
            await _send(event, f'❌ 权限不足: 目录「{game}」已绑定给 {shown}\n'
                               '仅绑定用户可更新该目录, 如需变更请联系管理员')
            return False
    is_new = (not folder) and not exists

    # 以下判定 → 置位 → 建任务之间不能有 await, 否则并发指令会一起受理
    global _busy
    if _busy:
        await _send(event, '⏳ 已有上传任务正在处理, 请稍后再试')
        return False
    _busy = True
    try:
        _spawn(_run(event, cfg, ref, target, folder, force, is_new))
    except Exception:
        _busy = False
        raise
    return True


def _spawn(coro):
    """后台跑完整流程: 绕开框架对 handler 的 300 秒硬超时。"""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def cancel_all():
    """插件卸载时取消未完成的后台任务并释放占位标记。"""
    global _busy
    for t in list(_tasks):
        t.cancel()
    _tasks.clear()
    _busy = False


# ==================== 流程 ====================

async def _run(event, cfg: dict, ref: dict, target: dict, folder: str,
               force: bool = False, is_new: bool = False):
    global _busy
    rid = store.new_record_id()
    record = {
        'forced': force,
        'is_new': is_new,
        'perm_bound': False,
        'compile': {},
        'id': rid,
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'group_id': event.group_id or '',
        'user_id': event.user_id or '',
        'username': event.username or '',
        'filename': store.safe_filename(ref.get('filename')),
        'target': target['key'],
        'target_path': target['path'],
        'folder': folder,
        'mode': 'file' if folder else 'archive',
        'source': ref.get('source', ''),
        'url': ref.get('url', ''),
        'size': 0,
        'stage': 'received',
        'verdict': '',
        'categories': [],
        'manual': False,
        'error': '',
        'dest': '',
        'review_file': '',
        'archive_file': '',
        'model': '',
        'criteria': [],
        'findings': [],
        'elapsed': 0.0,
        '_started': time.time(),
    }
    try:
        await _pipeline(event, cfg, ref, record, target, folder, force)
    except asyncio.CancelledError:
        record.update(stage='cancelled', error='任务被取消 (插件卸载或重载)')
        _persist(record)
        raise
    except Exception as e:  # noqa: BLE001 — 兜底: 未预期异常也要给结论 + 留记录
        report_error(PLUGIN, 'LGTBot_deploy', e,
                     context={'record': rid, 'group_id': event.group_id})
        record.update(stage='error', error=f'{type(e).__name__}: {e}')
        _persist(record)
        await _send(event, _fail_text(record, cfg, '处理过程出现异常'))
    finally:
        store.cleanup_staging(rid)
        _busy = False


def _persist(record: dict):
    """补上耗时后写入记录 (内部字段不落盘)。"""
    data = {k: v for k, v in record.items() if not k.startswith('_')}
    data['elapsed'] = round(time.time() - record.get('_started', time.time()), 1)
    store.add_record(data)


async def _pipeline(event, cfg: dict, ref: dict, record: dict, target: dict, folder: str,
                    force: bool = False):
    rid = record['id']
    fname = record['filename']
    single = bool(folder)

    # 1) 先回确认, 让上传者知道服务在线 (不含审核细节)
    where = f'{target["key"]}/{folder}' if single else target['key']
    await _send(event, f'📥 已收到文件「{fname}」→ {where}, 正在处理, 请稍候…')

    # 2) 下载
    ext = quoted.archive_ext(fname)
    if not single and ext in quoted.UNSUPPORTED_EXTS:
        record.update(stage='download', error=f'暂不支持 {ext}, 请改用 zip 或 tar.gz 重新打包')
        return await _finish_fail(event, cfg, record, '压缩格式不支持')
    max_bytes = int(cfg['max_archive_mb']) * 1024 * 1024
    data, err = await quoted.download(ref['url'], max_bytes, int(cfg['download_timeout']))
    if data is None:
        record.update(stage='download', error=err)
        return await _finish_fail(event, cfg, record, '文件下载失败')
    record['size'] = len(data)
    if cfg.get('keep_archive'):
        record['archive_file'] = store.save_archive(rid, fname, data)

    limits = {
        'max_files': int(cfg['max_files']),
        'max_uncompressed': int(cfg['max_uncompressed_mb']) * 1024 * 1024,
        'text_budget': int(cfg['text_budget']),
    }
    staging, info = '', {'total_size': len(data), 'root': ''}

    if single:
        # 单文件: 不解压, 直接把这一个文件送审
        pkg = archive.collect_single(data, fname, limits)
    else:
        # 3) 解压到暂存目录 (成员名与体积在 archive 内逐条校验)
        staging = store.staging_path(rid)
        store.cleanup_staging(rid)
        try:
            info = await asyncio.to_thread(archive.extract, data, fname, staging, limits)
        except archive.ArchiveError as e:
            record.update(stage='extract', error=str(e))
            return await _finish_fail(event, cfg, record, '压缩包校验失败')
        except Exception as e:  # noqa: BLE001
            record.update(stage='extract', error=f'{type(e).__name__}: {e}')
            return await _finish_fail(event, cfg, record, '解压失败')
        record['total_size'] = info['total_size']
        record['root'] = info['root']

        # 4) 采集送审内容 (仅文字)
        pkg = await asyncio.to_thread(archive.collect, staging, limits)
        if not pkg['tree']:
            record.update(stage='extract', error='压缩包内没有任何文件')
            return await _finish_fail(event, cfg, record, '压缩包为空')

    record['file_count'] = len(pkg['tree'])
    record['rule_files'] = pkg['rule_files']

    # 5) 压缩包完整性检查 (纯结构检查, 与 AI 审核无关, force 同样拒绝)
    if not single:
        missing = archive.missing_required(pkg['tree'], cfg.get('required_files') or [])
        if missing:
            record.update(stage='integrity', error='缺少必需文件: ' + '、'.join(missing))
            return await _finish_fail(event, cfg, record, '压缩包不完整',
                                      suffix='请补齐上述文件后重新打包上传')

    # 6) 审核 (force 与「面板关闭审核」都跳过, 但记录里区分开)
    skip_reason = 'force' if force else ('config' if not cfg.get('review_enabled') else '')
    if skip_reason:
        note = ('(主人强制上传, 本次未做内容审核)' if skip_reason == 'force'
                else '(审核已在面板关闭, 本次未做内容审核)')
        record.update(stage='review', verdict='force' if skip_reason == 'force' else 'skip',
                      manual=False)
        record['review_file'] = store.save_review_text(rid, note, header=_review_header(record))
        return await _finish_deploy(event, cfg, record, data, staging, info, target, folder,
                                    skip_reason=skip_reason)

    meta = {'filename': fname, 'size': record['size'], 'total_size': info['total_size'],
            'mode': record['mode'], 'target': target['key'], 'folder': folder}
    result = await review.review(pkg, meta, cfg)
    record.update(stage='review', verdict=result['verdict'], categories=result['categories'],
                  manual=result['manual'], model=result['model'], error=result['error'],
                  criteria=result.get('criteria') or [], findings=result.get('findings') or [])
    record['review_file'] = store.save_review_text(
        rid, result['raw'] or f'(无回复内容) 错误: {result["error"]}',
        header=_review_header(record, result))

    if result['verdict'] != 'pass':
        return await _finish_reject(event, cfg, record, result)

    # 7) 通过 → 部署
    return await _finish_deploy(event, cfg, record, data, staging, info, target, folder)


# ==================== 结论输出 ====================

def _review_header(record: dict, result: dict | None = None) -> str:
    """写在留档文件开头的元信息 (群里不输出这些内容)。"""
    lines = [
        f'# 审核留档 {record["id"]}',
        f'- 时间: {record["time"]}',
        f'- 群: {record["group_id"]}',
        f'- 提交者: {record["username"] or record["user_id"]}',
        f'- 文件: {record["filename"]} ({record["size"]} B)',
        f'- 模式: {"单文件增量" if record["mode"] == "file" else "压缩包"}'
        + ('  [主人强制上传, 跳过审核]' if record.get('forced') else ''),
        f'- 目标: {record["target"]} ({record["target_path"]})'
        + (f' / 文件夹 {record["folder"]}' if record['folder'] else ''),
        f'- 包内文件数: {record.get("file_count", 0)}',
        f'- rule 文件: {", ".join(record.get("rule_files") or []) or "无"}',
    ]
    if result:
        lines += [
            f'- 审查标准: {review.labels(result.get("criteria") or [])}',
            f'- 模型: {result["model"]}',
            f'- 结论: {result["verdict"]}',
            f'- 分类: {", ".join(result["categories"]) or "无"}',
            f'- 耗时: {result["elapsed"]}s',
            f'- 错误: {result["error"] or "无"}',
        ]
        floc = _finding_lines(record)
        if floc:
            lines.append('- 违规位置:')
            lines += [f'    {x}' for x in floc]
    return '\n'.join(lines)


def _size_mb(n: int) -> str:
    return f'{(n or 0) / 1048576:.2f} MB'


def _where(record: dict) -> str:
    """结论消息里的目标描述: 目标 key (+ 文件夹名)。"""
    return f'{record["target"]}/{record["folder"]}' if record['folder'] else record['target']


def _fail_text(record: dict, cfg: dict, title: str, suffix: str = '') -> str:
    """失败结论: 只给原因与记录号, 不含任何模型输出。"""
    lines = [
        f'⚠️ {title}, 需人工处理' if not suffix else f'⚠️ {title}',
        f'📦 文件: {record["filename"]} → {_where(record)}',
        f'❗ 原因: {record["error"] or "未知错误"}',
        f'🆔 记录: {record["id"]}',
    ]
    if suffix:
        lines.append(f'💡 {suffix}')
    at = _mentions(cfg, record)
    if at:
        lines.append(at + (' 请知悉' if suffix else ' 请人工处理'))
    return '\n'.join(lines)


async def _finish_fail(event, cfg: dict, record: dict, title: str, suffix: str = ''):
    _persist(record)
    await _send(event, _fail_text(record, cfg, title, suffix))


_MAX_SHOWN_FINDINGS = 8


def _finding_lines(record: dict) -> list:
    """违规位置列表 (只含分类 + 文件 + 行号, 绝不含违规内容原文)。"""
    findings = record.get('findings') or []
    lines = []
    for i, f in enumerate(findings[:_MAX_SHOWN_FINDINGS], 1):
        label = review.CATEGORY_LABELS.get(f.get('category'), '其他')
        if f.get('suspect'):
            label += '·疑似'
        loc = f.get('target') or '(未标注位置)'
        if f.get('line'):
            loc += f':{f["line"]}'
        lines.append(f'{i}. [{label}] {loc}')
    if len(findings) > _MAX_SHOWN_FINDINGS:
        lines.append(f'…… 其余 {len(findings) - _MAX_SHOWN_FINDINGS} 处见后台留档')
    return lines


async def _finish_reject(event, cfg: dict, record: dict, result: dict):
    _persist(record)
    if result['manual']:
        # 审核服务异常 (HTTP 失败/超时/解析失败): 群里只说明需人工处理,
        # 具体报错在记录与留档里, 不刷进群聊。
        lines = [
            '⚠️ 审核服务异常, 需人工处理',
            f'📦 文件: {record["filename"]} ({_size_mb(record["size"])}) → {_where(record)}',
            f'🆔 记录: {record["id"]}',
        ]
    else:
        suspect_only = bool(result['findings']) and all(f.get('suspect') for f in result['findings'])
        lines = [
            '❌ 审核未通过 (疑似违规, 需人工复核)' if suspect_only else '❌ 审核未通过',
            f'📦 文件: {record["filename"]} ({_size_mb(record["size"])}) → {_where(record)}',
            f'🚫 未通过分类: {review.labels(result["categories"])}',
        ]
        floc = _finding_lines(record)
        if floc:
            lines.append('📍 违规位置:')
            lines += floc
        lines.append(f'🆔 记录: {record["id"]}')
        lines.append('📄 详细说明已留档, 请在后台「LGTBot 自动部署」页查看')
    at = _mentions(cfg, record)
    if at:
        lines.append(at + ' 请人工复核')
    await _send(event, '\n'.join(lines))


_DEPLOY_HEAD = {
    '': '✅ 审核通过, 已上传到服务器',
    'config': '✅ 已上传到服务器\n⚠️ 审核功能已关闭',
    'force': '✅ 已强制上传到服务器\n⚠️ 文件未经内容审核',
}
_DEPLOY_FAIL_HEAD = {
    '': '⚠️ 审核通过, 但上传失败, 需人工处理',
    'config': '⚠️ 上传失败, 需人工处理',
    'force': '⚠️ 强制上传失败, 需人工处理',
}

# record['compile'] 只存解析后的字段; raw / 完整 log_tail 落留档 (append_review_text)
_COMPILE_RECORD_KEYS = ('status', 'ok', 'error', 'http_status', 'elapsed_sec',
                        'active_matches', 'returncode', 'terminate')


async def _finish_deploy(event, cfg: dict, record: dict, data: bytes, staging: str,
                         info: dict, target: dict, folder: str, skip_reason: str = ''):
    fname = record['filename']
    if folder:
        res = await asyncio.to_thread(deploy.deploy_single, data, fname, folder, target, cfg)
    else:
        base = deploy.strip_archive_ext(fname)
        res = await asyncio.to_thread(deploy.deploy_archive, staging, info['root'], base, target, cfg)

    if not res['ok']:
        record.update(stage='deploy', error=res['error'])
        _persist(record)
        lines = [
            _DEPLOY_FAIL_HEAD.get(skip_reason, _DEPLOY_FAIL_HEAD['']),
            f'📦 文件: {fname} → {_where(record)}',
            f'❗ 原因: {res["error"]}',
            f'🆔 记录: {record["id"]}',
        ]
        at = _mentions(cfg, record)
        if at:
            lines.append(at + ' 请人工处理')
        return await _send(event, '\n'.join(lines))

    record.update(stage='deployed', dest=res['dest'], deploy_name=res['name'],
                  backup=res['backup'])
    game = folder if folder else res['name']

    # 新游戏绑定目录权限: 必须审核通过 (force/关闭审核不绑定), 编译成败不影响
    if record.get('is_new') and record.get('verdict') == 'pass':
        try:
            store.perm_set(game, record['user_id'], record['username'])
            record['perm_bound'] = True
        except OSError as e:
            log.warning(f'目录权限绑定失败: {e}')

    # ---- 部署结果消息 (含「已请求编译」提示) ----
    head = _DEPLOY_HEAD.get(skip_reason, _DEPLOY_HEAD[''])
    lines = [head, f'📦 文件: {fname} ({_size_mb(record["size"])})']
    if folder:
        lines.append(f'📂 位置: {record["target"]} / {folder}/{fname}{res["note"]}')
    else:
        lines.append(f'📂 目录: {record["target"]} / {res["name"]}/  '
                     f'({record.get("file_count", 0)} 个文件){res["note"]}')
    if res['backup']:
        lines.append(f'♻️ 旧内容已备份: {os.path.basename(res["backup"])}')
    if record.get('perm_bound'):
        lines.append(f'🔗 目录权限已绑定: {record["username"] or record["user_id"]}')
    lines.append(f'🆔 记录: {record["id"]}')

    if not cfg.get('compile_enabled', True):
        # 未启用自动编译: 保持旧行为收尾 (通知部署人员), 记录标记 disabled
        record['compile'] = {'status': 'disabled', 'ok': False, 'error': '自动编译未启用'}
        store.append_review_text(record['id'], '## 编译结果\n- 状态: 未启用自动编译')
        _persist(record)
        lines.append('🔧 自动编译未启用, 需手动编译后生效')
        at = _mentions(cfg, record)
        if at:
            lines.append(at + ' 已完成上传')
        return await _send(event, '\n'.join(lines))

    lines.append('🔧 已请求编译, 编译可能较慢, 请耐心等待结果…')
    await _send(event, '\n'.join(lines))

    # ---- 请求编译 (客户端超时自动取消) 并落记录/留档 ----
    result = await compilemod.request_compile(game, cfg)
    record['compile'] = {k: result.get(k) for k in _COMPILE_RECORD_KEYS}
    store.append_review_text(record['id'], '## 编译结果\n' + compilemod.describe(result))
    _persist(record)
    await _send(event, _compile_text(cfg, record, result, game))


def _compile_text(cfg: dict, record: dict, result: dict, game: str) -> str:
    """编译结果消息。

    @ 规则 (force 一律不 @):
      · 编译成功 + 常规更新 → 只 @ 上传者 (热更新提示), 完全不 @ 开发者
      · 编译成功 + 新游戏   → @ 上传者 (需等重启) + @ 开发者 (安排重启)
      · 编译失败 / 超时 / 异常 → @ 开发者复查排查
    """
    at_dev = _mentions(cfg, record)
    uploader = f'<@{record["user_id"]}>' if record.get('user_id') else ''
    st = result.get('status')

    if st == 'success':
        elapsed = result.get('elapsed_sec')
        lines = [f'✅ 编译成功' + (f' (用时 {elapsed}s)' if elapsed is not None else '')]
        if record.get('is_new'):
            if result.get('active_matches') is not None:
                lines.append(f'🎲 当前剩余对局: {result["active_matches"]} '
                             '(重启需等待剩余对局结束)')
            lines.append(f'{uploader} 新游戏「{game}」需等待 bot 重启后才能实装, 请耐心等待')
            if at_dev:
                lines.append(at_dev + ' 新游戏已就绪, 请安排重启更新')
        else:
            lines.append(f'{uploader} 游戏「{game}」已自动热更新, 重新开局即可生效;'
                         '\n游戏规则与成就的修改需等待 bot 重启后生效')
        return '\n'.join(lines)

    if st == 'timeout':
        wait = int(cfg.get('compile_timeout') or 180)
        term = result.get('terminate') or {}
        lines = [
            f'⚠️ 编译 {wait} 秒超时无响应, 已自动取消'
            + ('' if term.get('ok') else ' (取消请求未确认, 请到编译面板检查)'),
            f'🆔 记录: {record["id"]}',
        ]
        if at_dev:
            lines.append(at_dev + ' 请复查编译问题')
        return '\n'.join(lines)

    if st == 'disabled':
        return '🔧 自动编译未启用, 需手动编译后生效'

    rc = result.get('returncode')
    lines = [
        '❌ 编译失败' + (f' (退出码 {rc})' if rc is not None else '') + ', 需人工排查',
        f'🆔 记录: {record["id"]}',
        '📄 编译日志与 API 返回已留档, 请在后台「LGTBot 自动部署」页查看',
    ]
    if at_dev:
        lines.append(at_dev + ' 请复查编译问题')
    return '\n'.join(lines)
