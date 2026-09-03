"""指令解析 + 上传流程编排: 取引用文件 → 下载 → 解压/校验 → 审核 → 部署 → 编译 → 记录 → @通知。

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
  · 新游戏编译成功后自动请求 LGTBot 计划重启 (自动模式, 维护原因「新游戏《X》」),
    请求成功即不再 @ 开发者 —— 对局清空后由 LGTBot 自行重启, 本插件不跟踪;
    只有请求失败才 @ 开发者手动安排。老游戏更新走热更新, 不请求重启也不 @ 开发者。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time

from core.base.logger import PLUGIN, get_logger, report_error

from . import archive, config, deploy, quoted, report, review, store
from . import compile as compilemod

log = get_logger(PLUGIN, 'LGTBot自动部署')

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
        '-> 重新编译 (仅上次编译失败时可用, 不需重传文件)',
        '/compile <文件夹名>',
        '',
        '🔸上传需引用一条群文件消息, 审核通过后自动落地',
        '🔸压缩包: 解压至 lgtbot 下的同名文件夹, 重名文件夹直接替换',
        '🔸单文件: 写入 lgtbot 下已存在的指定文件夹, 重名文件直接替换',
        '🔸压缩包支持 zip / tar.gz / tar.bz2 / tar.xz',
    ]
    if required:
        lines.append('🔸压缩包必须包含: ' + '、'.join(required))
    lines.append('🔸rule.md 需有原作标注 (改编需注明「改编自 X」)')
    lines.append('🔸新游戏上传成功后目录自动绑定上传者, 此后仅绑定用户可更新该目录')
    lines.append('🔸审核通过后自动请求编译并回报结果')
    lines.append('🔸内容完全相同的文件会被直接拒收; 只是编译失败请用 /compile 重编')
    return '\n'.join(lines)


# ==================== 消息发送 ====================

async def _send(event, text: str, active: bool = False):
    """优先被动回复 (不占主动额度); 失败退回主动群消息。

    ``active=True`` 跳过被动回复直接发主动消息: 被动消息 ID 只在收到消息后的
    5 分钟内有效, 长耗时收尾 (如编译等到超时才回) 到这一步时它多半已经失效,
    再走被动只会静默丢消息。
    """
    if not active:
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
        'restart': {},
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
        'sha256': '',
        'stage': 'received',
        'verdict': '',
        'categories': [],
        'manual': False,
        'error': '',
        'http_status': None,
        'attempts': 0,
        'dest': '',
        'review_file': '',
        'archive_file': '',
        'report_file': '',
        'report_url': '',
        'model': '',
        'criteria': [],
        'findings': [],
        'game_name': '',
        'game_desc': '',
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
        report_error(PLUGIN, 'LGTBot自动部署', e,
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

    # 2.5) 内容查重: 字节完全一致的包直接拒收, 不重复占审核额度与编译时间。
    # 无论上次过没过审都拒 —— 内容一个字节没改, 重传不会得出不同结论。
    record['sha256'] = hashlib.sha256(data).hexdigest()
    dup = store.find_by_sha(record['sha256'], rid)
    if dup:
        record.update(stage='duplicate',
                      error=f'与记录 {dup.get("id")} 完全一致 (SHA256 相同), '
                            f'旧结论「{_record_outcome(dup)}」')
        # 不 @ 部署人员: 重传同一个包纯属上传者操作失误, 没有任何要人工介入的地方
        return await _finish_fail(event, cfg, record, '重复上传, 已拒收',
                                  suffix=_dup_tip(dup), notify=False)

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
        local_name, local_desc = review.parse_game_props(pkg)
        record.update(stage='review', verdict='force' if skip_reason == 'force' else 'skip',
                      manual=False, game_name=review._clean_display(local_name),
                      game_desc=local_desc[:500])
        record['review_file'] = store.save_review_text(rid, note, header=_review_header(record))
        return await _finish_deploy(event, cfg, record, data, staging, info, target, folder,
                                    skip_reason=skip_reason)

    # 6.5) 送审文本超出总量上限 → 直接拒收, 不送审。
    # 宁可拒收也不截断: 截掉的部分从未被审查, 违规内容完全可能藏在后半段。
    if pkg.get('oversize'):
        ov = pkg['oversize']
        record.update(stage='review', error=_oversize_error(ov))
        return await _finish_fail(event, cfg, record, '送审文本超出上限, 未送审',
                                  suffix=_OVERSIZE_TIP, extra=_oversize_lines(ov))
    meta = {'filename': fname, 'size': record['size'], 'total_size': info['total_size'],
            'mode': record['mode'], 'target': target['key'], 'folder': folder}
    result = await _review_with_retry(event, cfg, pkg, meta, record)
    record.update(stage='review', verdict=result['verdict'], categories=result['categories'],
                  manual=result['manual'], model=result['model'], error=result['error'],
                  http_status=result.get('http_status'), attempts=result.get('attempts', 1),
                  criteria=result.get('criteria') or [], findings=result.get('findings') or [],
                  game_name=result.get('game_name') or '', game_desc=result.get('game_desc') or '')
    record['review_file'] = store.save_review_text(
        rid, result['raw'] or f'(无回复内容) 错误: {result["error"]}',
        header=_review_header(record, result))

    if result['verdict'] != 'pass':
        return await _finish_reject(event, cfg, record, result)

    # 7) 通过 → 部署
    return await _finish_deploy(event, cfg, record, data, staging, info, target, folder)


# ==================== 审核重试 ====================

# 审核服务异常 (HTTP 失败 / 超时 / 回复无法解析) 时的重试策略: 共 3 次尝试
# (首次 + 2 次重试), 每次之间等 60 秒。前两次失败**不 @ 开发者** —— 多数是上游
# 抖动或限流, 等一会儿重试就好, 不必每次都惊动人; 三次都失败才算重试次数耗尽,按需人工复核收尾并 @ 开发者。
_REVIEW_ATTEMPTS = 3
_REVIEW_RETRY_WAIT = 60


async def _review_with_retry(event, cfg: dict, pkg: dict, meta: dict, record: dict) -> dict:
    """带重试地跑内容审核, 返回最后一次的结果。

    只有「服务异常」(``manual=True``) 才重试 —— 模型正常给出 reject 是有效判定,
    重试没有意义。配置类错误 (未配置密钥, ``retryable=False``) 同样不重试。
    结果里额外带 ``attempts`` (实际尝试次数), 供结论消息区分是首次失败还是耗尽。
    """
    result: dict = {}
    for attempt in range(1, _REVIEW_ATTEMPTS + 1):
        result = await review.review(pkg, meta, cfg)
        result['attempts'] = attempt
        if not result['manual'] or not result.get('retryable', True):
            return result
        log.warning(f'审核服务异常 (第 {attempt}/{_REVIEW_ATTEMPTS} 次, '
                    f'{review.status_text(result)}): {result["error"]}')
        if attempt >= _REVIEW_ATTEMPTS:
            return result
        # 走主动消息: 第一次重试提示就可能在 180s 审核超时之后, 第二次更是累计 7 分钟往上, 被动消息 ID 的 5 分钟有效期基本已过。
        await _send(event, _retry_text(record, result, attempt), active=True)
        await asyncio.sleep(_REVIEW_RETRY_WAIT)
    return result


def _retry_text(record: dict, result: dict, attempt: int) -> str:
    """重试等待提示: 只告知上传者稍等, 不 @ 开发者。"""
    return '\n'.join([
        f'⚠️ 审核服务异常 ({review.status_text(result)}), '
        f'第 {attempt}/{_REVIEW_ATTEMPTS} 次尝试失败',
        f'📦 文件: {record["filename"]} ({_size_mb(record["size"])}) → {_where(record)}',
        f''
        f'⏳ {_REVIEW_RETRY_WAIT} 秒后自动重试, 请勿重复上传…',
    ])


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
        f'- 游戏名称: {record.get("game_name") or "（未取到）"}',
        f'- 游戏描述: {record.get("game_desc") or "（未取到）"}',
    ]
    if result:
        lines += [
            f'- 审查标准: {review.criteria_labels(result.get("criteria") or [])}',
            f'- 模型: {result["model"]}',
            f'- 结论: {result["verdict"]}',
            f'- 分类: {", ".join(result["categories"]) or "无"}',
            f'- 耗时: {result["elapsed"]}s',
            f'- 尝试次数: {result.get("attempts", 1)} / {_REVIEW_ATTEMPTS}',
            f'- HTTP: {review.status_text(result) if result["error"] else "HTTP 200"}',
            f'- 错误: {result["error"] or "无"}',
        ]
        floc = _finding_lines(record)
        if floc:
            lines.append('- 违规位置:')
            lines += [f'    {x}' for x in floc]
    return '\n'.join(lines)


def _size_mb(n: int) -> str:
    return f'{(n or 0) / 1048576:.2f} MB'


def _game_label(record: dict, game: str) -> str:
    """群消息里的游戏标识: 有中文名则「中文名(目录名)」, 否则只用目录名。

    中文名来自审核结果 (或本地解析 mygame.cc), 已在 review._clean_display 里去掉
    换行与 ``<> 「」`` —— 不会被用来伪造 @ 或伪造多行消息。
    """
    name = (record.get('game_name') or '').strip()
    return f'{name}({game})' if name else game


def _where(record: dict) -> str:
    """结论消息里的目标描述: 目标 key (+ 文件夹名)。"""
    return f'{record["target"]}/{record["folder"]}' if record['folder'] else record['target']


def _fail_text(record: dict, cfg: dict, title: str, suffix: str = '',
               extra: list | None = None, notify: bool = True) -> str:
    """失败结论: 只给原因与记录号, 不含任何模型输出。``extra`` 是补充明细行。

    ``notify=False`` 不 @ 部署人员 —— 用于上传者自己就能处理完、开发者不需要
    知道的情形 (如重复上传拒收: 内容一个字节没改, 没有任何要人工介入的地方)。
    """
    lines = [
        f'⚠️ {title}, 需人工处理' if not suffix else f'⚠️ {title}',
        f'📦 文件: {record["filename"]} → {_where(record)}',
        f'❗ 原因: {record["error"] or "未知错误"}',
    ]
    lines += extra or []
    lines.append(f'🆔 记录: {record["id"]}')
    if suffix:
        lines.append(f'> 💡 {suffix}')
    at = _mentions(cfg, record) if notify else ''
    if at:
        lines.append(at + (' 请知悉' if suffix else ' 请人工处理'))
    return '\n'.join(lines)


async def _finish_fail(event, cfg: dict, record: dict, title: str, suffix: str = '',
                       extra: list | None = None, notify: bool = True):
    _persist(record)
    await _send(event, _fail_text(record, cfg, title, suffix, extra, notify))


# ==================== 送审文本超限 ====================

_OVERSIZE_TIP = '请精简过大的文本文件后重新上传 (词库 / 题库等纯数据文件建议改用二进制), 或联系管理员人工审查。'


def _oversize_error(ov: dict) -> str:
    return (f'包内文本总量超过审核上限 {ov["budget"]} 字 '
            f'(共 {ov["text_files"]} 个文本文件, {_size_mb(ov["text_bytes"])}), '
            f'读到「{ov["hit"]}」时触顶。本次未送审')


def _oversize_lines(ov: dict) -> list:
    """群消息里的超限明细: 最大的几个文本文件 (让上传者知道该精简谁)。"""
    if not ov.get('largest'):
        return []
    return ['📄 最大的文本文件:'] + [
        f'{i}. {f["path"]} ({_size_mb(f["size"])})'
        for i, f in enumerate(ov['largest'], 1)]


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
    # 审核服务异常不出报告: 群消息里的状态码与重试次数已经是全部信息, 页面上没有
    # 第二样东西可写。报告要在 _persist 之前生成 —— report_file 得跟着记录一起落盘,
    # 否则删记录时清不掉那个页面。
    link = '' if result['manual'] else report.generate(cfg, record, 'review', result)
    _persist(record)
    if result['manual']:
        # 审核服务异常 (HTTP 失败/超时/解析失败): 群里只给状态码与重试情况,
        # 具体报错在记录与留档里, 不刷进群聊。
        attempts = result.get('attempts', 1)
        lines = [
            f'⚠️ 审核服务异常 ({review.status_text(result)}), 需人工处理',
            f'📦 文件: {record["filename"]} ({_size_mb(record["size"])}) → {_where(record)}',
        ]
        if attempts > 1:
            lines.insert(1, f'🔁 已尝试 {attempts} 次 (含 {attempts - 1} 次重试), 重试次数耗尽')
        lines.append(f'🆔 记录: {record["id"]}')
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
        lines.append('')
        lines.append(f'🆔 记录: {record["id"]}')
        # 有报告页就给链接 (上传者自己能看到每条 finding 的完整说明), 否则退回
        # 「去后台看」—— 上传者进不了后台, 那条只对被 @ 的开发者有意义
        lines.append(link or '📄 详细说明已留档, 请在后台「LGTBot 自动部署」页查看')
    at = _mentions(cfg, record)
    if at:
        lines.append(at + ' 请人工复核')
    # 审核结论一律走主动消息: 服务异常路径最长要跑 3 次尝试 + 2 次 60s 等待
    # (累计十分钟往上), 正常审核也可能耗时 180s, 被动消息 ID 多半已失效。
    await _send(event, '\n'.join(lines), active=True)


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

# record['compile'] / record['restart'] 只存解析后的字段;
# raw / 完整 log_tail 落留档 (append_review_text)
_COMPILE_RECORD_KEYS = ('status', 'ok', 'new', 'error', 'http_status', 'elapsed_sec',
                        'active_matches', 'returncode', 'terminate')
_RESTART_RECORD_KEYS = ('status', 'ok', 'error', 'http_status', 'enabled', 'auto',
                        'reason', 'active_matches', 'message')


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
    if record.get('game_name'):
        lines.append(f'🎮 游戏名称: {record["game_name"]}')
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

    # 空行隔开部署详情与编译提示, 让「已请求编译」更醒目
    lines.append('')
    if record.get('is_new'):
        lines.append('🔧 已请求编译 (新游戏完整编译, 耗时更长), 请耐心等待结果…')
    else:
        lines.append('🔧 已请求编译, 编译可能较慢, 请耐心等待结果…')
    await _send(event, '\n'.join(lines))

    await _compile_and_report(event, cfg, record, game)


async def _compile_and_report(event, cfg: dict, record: dict, game: str) -> dict:
    """请求编译 → 新游戏成功则请求计划重启 → 写留档与记录 → 回报结果。

    上传部署与 ``/compile`` 重新编译共用同一段 —— 两条路径的编译语义本就一致,
    分开写迟早走岔 (比如只在一边请求计划重启)。
    """
    # ---- 请求编译: 新游戏带 new=true 走完整编译, 老游戏更新走增量 ----
    result = await compilemod.request_compile(game, cfg, is_new=bool(record.get('is_new')))
    record['compile'] = {k: result.get(k) for k in _COMPILE_RECORD_KEYS}
    notes = ['## 编译结果\n' + compilemod.describe(result)]

    # ---- 新游戏编译成功 → 请求计划重启 (自动模式), 不再 @ 开发者安排重启 ----
    # 老游戏更新走热更新, 不需要重启, 也就不请求。
    restart = None
    if result.get('ok') and record.get('is_new'):
        restart = await compilemod.request_planned_restart(
            cfg, f'新游戏《{_restart_game_name(record, game)}》')
        record['restart'] = {k: restart.get(k) for k in _RESTART_RECORD_KEYS}
        notes.append('## 计划重启请求\n' + compilemod.describe_restart(restart))
    store.append_review_text(record['id'], '\n\n'.join(notes))
    # 编译没成 → 出报告页: 完整报错与编译日志尾部群消息里塞不下 (日志还掺着源码,
    # 本来就只进留档), 而要看它的恰恰是进不了后台的上传者。disabled 不是失败, 跳过。
    link = ('' if result.get('ok') or result.get('status') == 'disabled'
            else report.generate(cfg, record, 'compile', result, game))
    _persist(record)
    # 编译结果一律走主动消息: 走到这一步的累计耗时 (下载 + 解压 + 审核, 审核还可能
    # 重试 3 次、间隔 60s, 再加最长 compile_timeout 的编译) 很容易超过被动消息 ID
    # 的 5 分钟有效期, 用被动只会把结论静默丢掉 —— 不差这一条主动额度。
    await _send(event, _compile_text(cfg, record, result, game, restart, link), active=True)
    return result


def _recompile_tip(game: str) -> str:
    """编译没成时的补救提示。

    内容相同的包会被查重拒收 (见 _pipeline), 所以必须主动告诉用户有 /compile 这条路,
    否则他只会去重传、然后撞上「重复上传, 已拒收」。
    """
    return f'> 💡 遇到临时问题可直接 /compile {game} 重新编译, 无需重传文件'


def _compile_reason(result: dict) -> str:
    """编译失败原因 (群消息用)。

    只取编译 API 的 ``error`` 字段 —— 它是 API 自己的固定文案 (见 build_api.py 的
    ``_err`` 调用), **不含编译器输出**; 编译器输出在单独的 ``log_tail`` 里, 那份掺着
    上传者的源码, 只进留档不进群。这里仍走一遍展示净化: 去控制字符与换行 (防伪造
    多行消息)、去 ``<>`` (防伪造 @ 全体), 再遮蔽 IP:端口 (与报告页共用 review.mask_addr)。
    """
    return review._clean_display(review.mask_addr(result.get('error')), 200)


# ==================== /compile 重新编译 ====================

async def handle_recompile(event, argline: str) -> bool:
    """``/compile <文件夹名>`` —— 只在上次编译失败时重新编译, 不重传文件。

    存在的理由: 内容完全相同的压缩包会被查重直接拒收 (见 _pipeline 的 sha256
    去重), 所以「审核通过了但编译失败」这种情况没法靠重传补救。上次编译**已经
    成功**就不给用 —— 否则它会变成一个无限重编按钮, 白占编译机。

    权限沿用目录绑定那套: 绑定用户或上次的上传者本人才能重编。
    """
    cfg = config.all_config(refresh=True)
    if not cfg.get('enabled') or not config.is_group_allowed(event.group_id):
        log.info(f'群 {event.group_id} 未启用或不在允许列表, 忽略 /compile')
        return False

    args = (argline or '').split()
    if len(args) != 1:
        await _send(event, '❌ 用法: /compile <文件夹名>\n仅在上次编译失败时可用')
        return False
    game = args[0]
    name_err = deploy.bad_name(game)
    if name_err:
        await _send(event, f'❌ {name_err}')
        return False

    last = store.last_game_record(game)
    if last is None:
        await _send(event, f'❌ 目录「{game}」没有上传记录, 请先用 /upload 上传')
        return False

    uid = event.user_id or ''
    owner = store.perm_get(game)
    allowed = {last.get('user_id') or ''} | ({owner.get('user_id')} if owner else set())
    if uid not in allowed:
        shown = (owner or {}).get('username') or last.get('username') or '上次的上传者'
        await _send(event, f'❌ 权限不足: 目录「{game}」由 {shown} 负责, 仅其本人可重新编译')
        return False

    comp = last.get('compile') or {}
    if comp.get('ok'):
        await _send(event, f'❌ 目录「{game}」上次编译已成功 (记录 {last.get("id")}), 无需重试\n'
                           '如需更新内容请修改后 /upload 重新上传')
        return False
    if not cfg.get('compile_enabled', True):
        await _send(event, '❌ 自动编译未在面板启用')
        return False

    global _busy
    if _busy:
        await _send(event, '⏳ 已有任务正在处理, 请稍后再试')
        return False
    _busy = True
    try:
        _spawn(_run_recompile(event, cfg, game, last))
    except Exception:
        _busy = False
        raise
    return True


async def _run_recompile(event, cfg: dict, game: str, last: dict):
    """按上次那条记录的参数重新编译, 另写一条 stage='recompile' 记录。

    另起一条而不是改旧记录: records.jsonl 只追加; 而且新记录会被
    store.last_game_record 优先取到 —— 编译一旦成功, 这条指令自然就用不了了。
    """
    global _busy
    rid = store.new_record_id()
    record = {
        'id': rid,
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'stage': 'recompile',
        'source_record': last.get('id') or '',
        'group_id': event.group_id or '',
        'user_id': event.user_id or '',
        'username': event.username or '',
        'filename': last.get('filename') or '',
        'sha256': last.get('sha256') or '',
        'target': last.get('target') or 'lgtbot',
        'target_path': last.get('target_path') or '',
        'folder': last.get('folder') or '',
        'deploy_name': last.get('deploy_name') or '',
        'mode': last.get('mode') or 'archive',
        'size': last.get('size') or 0,
        'is_new': bool(last.get('is_new')),
        'forced': bool(last.get('forced')),
        'verdict': last.get('verdict') or '',
        'game_name': last.get('game_name') or '',
        'game_desc': last.get('game_desc') or '',
        'compile': {},
        'restart': {},
        'error': '',
        '_started': time.time(),
    }
    # 返回值必须登记进 record —— 删除记录时按 review_file 清留档, 不登记就成孤儿文件
    record['review_file'] = store.save_review_text(
        rid,
        f'重新编译目录「{game}」, 未重新上传文件。\n'
        f'依据记录: {last.get("id")} ({_record_outcome(last)})',
        header=(f'# 重新编译 {rid}\n- 时间: {record["time"]}\n'
                f'- 发起人: {record["username"] or record["user_id"]}\n'
                f'- 目录: {game}\n- 依据记录: {last.get("id")}'))
    try:
        await _send(event, f'🔧 已请求重新编译「{_game_label(record, game)}」'
                           + ('（新游戏完整编译, 耗时更长）' if record['is_new'] else '')
                           + ', 请耐心等待结果…')
        await _compile_and_report(event, cfg, record, game)
    except asyncio.CancelledError:
        record.update(error='任务被取消 (插件卸载或重载)')
        _persist(record)
        raise
    except Exception as e:  # noqa: BLE001
        report_error(PLUGIN, 'LGTBot自动部署', e, context={'record': rid, 'game': game})
        record.update(error=f'{type(e).__name__}: {e}')
        _persist(record)
        await _send(event, _fail_text(record, cfg, '重新编译过程出现异常'), active=True)
    finally:
        _busy = False


# ==================== 重复上传 / 重新编译 ====================

_STAGE_TEXT = {
    'received': '未处理完', 'download': '下载失败', 'extract': '解压或校验失败',
    'integrity': '压缩包不完整', 'review': '审核阶段结束', 'deploy': '部署失败',
    'deployed': '已部署', 'duplicate': '重复上传被拒', 'recompile': '重新编译',
    'error': '处理异常', 'cancelled': '任务被取消',
}


def _record_outcome(rec: dict) -> str:
    """把一条历史记录压成一句人话结论 (重复上传提示 / 重新编译提示用)。"""
    stage = rec.get('stage') or ''
    if stage == 'deployed':
        comp = (rec.get('compile') or {}).get('status')
        label = compilemod.STATUS_LABELS.get(comp, comp) if comp else ''
        return '已部署' + (f', {label}' if label else '')
    if rec.get('manual'):
        return '审核服务异常, 需人工处理'
    if rec.get('verdict') == 'reject':
        return f'审核未通过 ({review.labels(rec.get("categories") or [])})'
    return _STAGE_TEXT.get(stage, stage or '结果未知')


def _dup_tip(dup: dict) -> str:
    """重复上传的处理建议: 上次编译没成的引导到 /compile, 否则让改内容再传。"""
    game = _record_game(dup)
    comp = (dup.get('compile') or {}).get('status')
    if dup.get('stage') == 'deployed' and comp and comp != 'success' and game:
        return f'内容没有改动无需重传; 上次编译失败 —— 直接用 /compile {game} 重试'
    return '内容没有任何改动就不必重传; 请修改后再上传, 或联系管理员人工处理'


def _record_game(rec: dict) -> str:
    """记录对应的游戏目录名 (单文件用 folder, 压缩包用落地目录名)。"""
    return str(rec.get('folder') or rec.get('deploy_name') or '')


def _restart_game_name(record: dict, game: str) -> str:
    """计划重启的维护原因里用的游戏名: 优先中文名, 取不到则用目录名 (英文名)。

    与 ``_game_label`` 不同 —— 维护提示是给玩家看的, 不带目录名括号。
    """
    return (record.get('game_name') or '').strip() or game


def _restart_lines(cfg: dict, record: dict, restart: dict | None) -> list:
    """计划重启请求的结果提示。

    只汇报请求结果, 不跟踪重启是否真的发生 (对局清空后由 LGTBot 自行触发)。
    请求成功**不 @ 开发者**; 只有请求失败才需要人工安排重启。
    """
    if not restart:
        return []
    if restart.get('ok'):
        left = restart.get('active_matches')
        tail = ('剩余对局清空后自动重启' if not left
                else f'剩余 {left} 局结束后自动重启')
        return [f'🔁 已请求计划重启 (自动模式): {tail}']
    lines = [f'⚠️ 计划重启请求失败 ({compilemod.RESTART_STATUS_LABELS.get(restart.get("status"), "异常")}'
             + (f", HTTP {restart['http_status']}" if restart.get('http_status') else '') + ')',
             f'❗ 原因: {restart.get("error") or "未知错误"}']
    at_dev = _mentions(cfg, record)
    if at_dev:
        lines.append(at_dev + ' 计划重启未生效, 请手动安排重启')
    return lines


def _compile_text(cfg: dict, record: dict, result: dict, game: str,
                  restart: dict | None = None, link: str = '') -> str:
    """编译结果消息。

    @ 规则 (force 一律不 @):
      · 编译成功 + 常规更新 → 只 @ 上传者 (热更新提示), 完全不 @ 开发者
      · 编译成功 + 新游戏   → @ 上传者 (需等重启); 计划重启请求成功则不 @ 开发者,
        只有请求失败才 @ 开发者手动安排重启 (见 _restart_lines)
      · 编译失败 / 超时 / 异常 → @ 开发者复查排查
    """
    at_dev = _mentions(cfg, record)
    uploader = f'<@{record["user_id"]}>' if record.get('user_id') else ''
    st = result.get('status')

    if st == 'success':
        elapsed = result.get('elapsed_sec')
        lines = [f'✅ 编译成功' + (f' (用时 {elapsed}s)' if elapsed is not None else '')]
        if record.get('is_new'):
            # 剩余对局数优先取重启请求的回包 (比编译回包更新)
            left = (restart or {}).get('active_matches')
            if left is None:
                left = result.get('active_matches')
            if left is not None:
                lines.append(f'🎲 当前剩余对局: {left} (重启需等待剩余对局结束)')
            lines.append(f'{uploader} 新游戏「{_game_label(record, game)}」'
                         '需等待 bot 重启后才能实装, 请耐心等待')
            lines += _restart_lines(cfg, record, restart)
        else:
            lines.append(f'{uploader} 游戏「{_game_label(record, game)}」已自动热更新, 重新开局即可生效;\n'
                         '游戏规则、描述、成就、倍率等属性的修改需等待 bot 重启后生效')
        return '\n'.join(lines)

    if st == 'timeout':
        wait = int(cfg.get('compile_timeout') or 180)
        term = result.get('terminate') or {}
        lines = [
            f'⚠️ 编译 {wait} 秒超时无响应, 已自动取消'
            + ('' if term.get('ok') else ' (取消请求未确认, 请到编译面板检查)'),
            f'🆔 记录: {record["id"]}',
        ]
        if link:
            lines.append(link)
        lines.append(_recompile_tip(game))
        if at_dev:
            lines.append(at_dev + ' 请复查编译问题')
        return '\n'.join(lines)

    if st == 'disabled':
        return '🔧 自动编译未启用, 需手动编译后生效'

    rc = result.get('returncode')
    lines = [f'❌ {compilemod.STATUS_LABELS.get(st, "编译失败")}'
             + (f' (退出码 {rc})' if rc is not None else '') + ', 需人工排查']
    reason = _compile_reason(result)
    if reason:
        lines.append(f'❗ 原因: {reason}')
    lines += [
        f'🆔 记录: {record["id"]}',
        # 有报告页就给链接: 上传者要的正是编译器日志, 而后台他进不去
        link or '📄 编译日志与 API 返回已留档, 请在后台「LGTBot 自动部署」页查看',
    ]
    # 目标名非法时重编也一样过不去 (compile API 的白名单不认中文目录名), 不给这条提示
    if st != 'invalid':
        lines.append(_recompile_tip(game))
    if at_dev:
        lines.append(at_dev + ' 请复查编译问题')
    return '\n'.join(lines)
