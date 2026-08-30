"""审核记录与数据文件管理 (全部落在插件 data/ 目录)。

    data/
    ├── config.yaml              插件配置
    ├── records.jsonl            审核记录 (一行一条, 便于追加与轮转)
    ├── reviews/<记录号>.md      模型的完整回复 (群里不输出, 只在此留档)
    ├── archives/<记录号>_x.zip  原压缩包留档 (keep_archive 开启时)
    ├── staging/<记录号>/        解压暂存 (部署或失败后清理)
    ├── backups/<名称>.<时间戳>[/…]  替换时备份的旧目录/旧文件 (面板按整夹展示与删除)
    ├── backups/<文件夹>/<文件>.<时间戳>  单文件替换的备份
    └── diagnostics/<记录号>.json  未能定位到文件时的原始消息载荷
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time

from .config import DATA_DIR

RECORDS_FILE = os.path.join(DATA_DIR, 'records.jsonl')
PERMS_FILE = os.path.join(DATA_DIR, 'permissions.json')
REVIEWS_DIR = os.path.join(DATA_DIR, 'reviews')
ARCHIVES_DIR = os.path.join(DATA_DIR, 'archives')
STAGING_DIR = os.path.join(DATA_DIR, 'staging')
BACKUPS_DIR = os.path.join(DATA_DIR, 'backups')
DIAG_DIR = os.path.join(DATA_DIR, 'diagnostics')

_MAX_RECORDS = 1000              # 超过后按时间裁剪 (保留最近的)
_SAFE_NAME = '-_.'


def init():
    for d in (DATA_DIR, REVIEWS_DIR, ARCHIVES_DIR, STAGING_DIR, BACKUPS_DIR, DIAG_DIR):
        os.makedirs(d, exist_ok=True)


def new_record_id() -> str:
    """记录号: 20260730-143210-8f3a (可读 + 唯一, 同时用作各类文件名前缀)。"""
    return time.strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(2).hex()


def safe_filename(name: str) -> str:
    """清洗成安全文件名 (去目录分隔符与控制字符), 空则给兜底名。"""
    base = os.path.basename((name or '').replace('\\', '/')).strip()
    out = ''.join(c for c in base if c.isalnum() or c in _SAFE_NAME or ord(c) > 127)
    return out[:120] or 'upload.bin'


# ==================== 记录 ====================

def add_record(record: dict) -> dict:
    init()
    try:
        with open(RECORDS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
    except OSError:
        return record
    _trim_records()
    return record


def _trim_records():
    try:
        with open(RECORDS_FILE, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= _MAX_RECORDS:
        return
    tmp = RECORDS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.writelines(lines[-_MAX_RECORDS:])
    os.replace(tmp, RECORDS_FILE)


def list_records(limit: int = 200) -> list:
    """倒序返回最近的记录 (不含模型原文, 原文用 get_review_text 单独取)。"""
    if not os.path.isfile(RECORDS_FILE):
        return []
    try:
        with open(RECORDS_FILE, encoding='utf-8') as f:
            lines = f.readlines()[-max(1, limit):]
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out


def find_by_sha(sha: str, exclude_id: str = '') -> dict | None:
    """按内容 sha256 找最近一条同内容记录 (重复上传拒收用)。

    只认字节完全一致 —— 改了一个字符 sha 就变, 不会误伤真正的更新。
    """
    if not sha:
        return None
    return next((r for r in list_records(_MAX_RECORDS)
                 if r.get('sha256') == sha and r.get('id') != exclude_id), None)


def last_game_record(game: str) -> dict | None:
    """某游戏目录最近一条「已落地」记录 (部署成功, 或事后的重新编译)。

    ``/compile`` 据此判断上次编译成没成 —— 重新编译会另写一条 stage='recompile'
    的记录, 所以编译成功后再点就会被挡下, 不会变成无限重编按钮。
    """
    game = str(game or '').strip()
    if not game:
        return None
    for r in list_records(_MAX_RECORDS):
        if r.get('stage') in ('deployed', 'recompile')                 and (r.get('folder') or r.get('deploy_name') or '') == game:
            return r
    return None


def get_record(rid: str) -> dict | None:
    return next((r for r in list_records(_MAX_RECORDS) if r.get('id') == rid), None)


def delete_record(rid: str, with_files: bool = True) -> dict:
    """删除单条记录; ``with_files`` 时连同该记录的留档文件一并删。

    records.jsonl 是只追加的, 删一条要整份重写 —— 先写临时文件再原子替换, 中途
    出错不会把索引写坏。返回 ``{ok, error, files}``, files 是实际删掉的留档路径。

    注意: 删掉记录会让 ``find_by_sha`` 查不到这条内容, 也就是**同一个包可以重新
    上传**了 —— 这正是查重被误伤时的人工解法, 不是副作用。
    """
    rid = str(rid or '').strip()
    if not rid:
        return {'ok': False, 'error': '记录号为空', 'files': []}
    target = get_record(rid)
    if target is None:
        return {'ok': False, 'error': '记录不存在', 'files': []}

    kept = []
    try:
        with open(RECORDS_FILE, encoding='utf-8') as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    if json.loads(s).get('id') == rid:
                        continue
                except json.JSONDecodeError:
                    pass    # 坏行原样保留, 不在删单条时顺手丢数据
                kept.append(s)
        tmp = RECORDS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(kept) + ('\n' if kept else ''))
        os.replace(tmp, RECORDS_FILE)
    except OSError as e:
        return {'ok': False, 'error': f'重写记录索引失败: {e}', 'files': []}

    removed = []
    if with_files:
        # 按记录里登记的相对路径删, 而不是自己拼文件名 —— 拼错就会误删别人的留档
        for rel in (target.get('review_file'), target.get('archive_file')):
            full = resolve(str(rel or ''))
            if full and os.path.isfile(full):
                try:
                    os.remove(full)
                    removed.append(rel)
                except OSError:
                    pass
        diag = os.path.join(DIAG_DIR, f'{rid}.json')
        if os.path.isfile(diag):
            try:
                os.remove(diag)
                removed.append(os.path.relpath(diag, DATA_DIR).replace('\\', '/'))
            except OSError:
                pass
    return {'ok': True, 'error': '', 'files': removed}


def clear_records() -> int:
    """清空记录索引 (reviews/archives 里的留档文件保留, 可在文件页单独删)。"""
    n = len(list_records(_MAX_RECORDS))
    try:
        if os.path.isfile(RECORDS_FILE):
            os.remove(RECORDS_FILE)
    except OSError:
        return 0
    return n


# ==================== 留档文件 ====================

def save_review_text(rid: str, text: str, header: str = '') -> str:
    """模型完整回复落盘, 返回相对 data/ 的路径。"""
    init()
    path = os.path.join(REVIEWS_DIR, f'{rid}.md')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            if header:
                f.write(header.rstrip() + '\n\n---\n\n')
            f.write(text or '(无内容)')
    except OSError:
        return ''
    return os.path.relpath(path, DATA_DIR).replace('\\', '/')


def get_review_text(rid: str) -> str:
    path = os.path.join(REVIEWS_DIR, f'{rid}.md')
    if not os.path.isfile(path):
        return ''
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def append_review_text(rid: str, text: str):
    """向已有留档追加一段 (编译结果等后到的信息)。"""
    path = os.path.join(REVIEWS_DIR, f'{rid}.md')
    try:
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n\n' + (text or '').rstrip() + '\n')
    except OSError:
        pass


# ==================== 目录更新权限 ====================
# permissions.json: {folder: {user_id, username, time}} — 新游戏上传成功时自动
# 绑定上传者, 此后仅绑定用户可更新该目录 (force 除外); 面板可增删改。

def _load_perms() -> dict:
    if not os.path.isfile(PERMS_FILE):
        return {}
    try:
        with open(PERMS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_perms(data: dict):
    init()
    tmp = PERMS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PERMS_FILE)


def perm_get(folder: str) -> dict | None:
    """某目录的绑定信息 {user_id, username, time}; 未绑定返回 None。"""
    item = _load_perms().get(str(folder or ''))
    return dict(item) if isinstance(item, dict) else None


def perm_set(folder: str, user_id: str, username: str = '') -> dict:
    """绑定/改绑目录到用户 (面板「增/改」与新游戏自动绑定共用)。"""
    folder = str(folder or '').strip()
    data = _load_perms()
    data[folder] = {
        'user_id': str(user_id or '').strip(),
        'username': str(username or '').strip(),
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    _save_perms(data)
    return dict(data[folder])


def perm_delete(folder: str) -> bool:
    data = _load_perms()
    if str(folder or '') not in data:
        return False
    del data[str(folder)]
    _save_perms(data)
    return True


def perm_list() -> list:
    """全部绑定 [{folder, user_id, username, time}], 按目录名排序。"""
    return [{'folder': k, **v} for k, v in sorted(_load_perms().items())
            if isinstance(v, dict)]


def save_archive(rid: str, filename: str, data: bytes) -> str:
    init()
    path = os.path.join(ARCHIVES_DIR, f'{rid}_{safe_filename(filename)}')
    try:
        with open(path, 'wb') as f:
            f.write(data)
    except OSError:
        return ''
    return os.path.relpath(path, DATA_DIR).replace('\\', '/')


def save_diagnostic(rid: str, payload: dict) -> str:
    """未能从引用消息里定位文件时, 把原始载荷存下来供排查协议字段变化。"""
    init()
    path = os.path.join(DIAG_DIR, f'{rid}.json')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except OSError:
        return ''
    return os.path.relpath(path, DATA_DIR).replace('\\', '/')


def staging_path(rid: str) -> str:
    init()
    return os.path.join(STAGING_DIR, rid)


def cleanup_staging(rid: str):
    shutil.rmtree(os.path.join(STAGING_DIR, rid), ignore_errors=True)


# ==================== 面板文件浏览 ====================

_TEXT_VIEW_EXTS = ('.md', '.txt', '.json', '.jsonl', '.yaml', '.yml', '.log')
_MAX_VIEW_BYTES = 512 * 1024


def resolve(rel: str) -> str | None:
    """把面板传来的相对路径解析成 data/ 内的绝对路径, 越界返回 None。"""
    if not rel:
        return None
    root = os.path.realpath(DATA_DIR)
    full = os.path.realpath(os.path.join(root, rel.replace('\\', '/').lstrip('/')))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


# 模块目录统计的固定展示顺序 (其后追加未知目录, 最后是根目录散文件)
_KNOWN_DIRS = ('reviews', 'archives', 'backups', 'diagnostics', 'staging')
_ROOT_STAT = '(根目录)'


def _backup_unit(rel: str) -> str:
    """把 backups/ 内文件的相对路径归到「备份单元」。

    备份直接落在 backups/ 下 (不再分目标子目录): 目录备份是
    backups/<名称>.<时间戳>/..., 单文件备份是 backups/<文件夹>/<文件>.<时间戳>。
    单元取前两段 (backups/ 下的第一层条目), 面板按整夹展示与删除, 不展开内部
    文件; 旧版 backups/lgtbot/... 整组聚合为一个 "lgtbot" 单元, 可整夹清理。
    """
    parts = rel.split('/')
    return '/'.join(parts[:2])


def list_entries() -> dict:
    """数据文件列表 + 各模块目录大小统计。

    返回 ``{'entries': [...], 'stats': [...]}``:
      · entries — 普通文件为 {path, display, size, mtime, viewable, kind:'file'};
        backups/ 内的文件聚合成 {path, display, size, mtime, count, kind:'backup'},
        display 去掉 backups/ 前缀 (如 "lgtbot/gomoku.xxx"), 不展示内部文件。
      · stats — 每个顶层模块目录的 {name, size, count}, 已知目录即使为空也列出。
    """
    init()
    root = os.path.realpath(DATA_DIR)
    entries: list = []
    units: dict = {}
    stats: dict = {name: {'name': name, 'size': 0, 'count': 0} for name in _KNOWN_DIRS}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(dirnames)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace('\\', '/')
            top = rel.split('/', 1)[0] if '/' in rel else _ROOT_STAT
            bucket = stats.setdefault(top, {'name': top, 'size': 0, 'count': 0})
            bucket['size'] += st.st_size
            bucket['count'] += 1
            if rel.startswith('backups/'):
                unit = units.setdefault(_backup_unit(rel), {'size': 0, 'mtime': 0, 'count': 0})
                unit['size'] += st.st_size
                unit['count'] += 1
                unit['mtime'] = max(unit['mtime'], int(st.st_mtime))
                continue
            entries.append({
                'path': rel,
                'display': rel,
                'size': st.st_size,
                'mtime': int(st.st_mtime),
                'viewable': fn.lower().endswith(_TEXT_VIEW_EXTS) and st.st_size <= _MAX_VIEW_BYTES,
                'kind': 'file',
            })
    for path, u in units.items():
        entries.append({
            'path': path,
            'display': path[len('backups/'):] or path,
            'size': u['size'],
            'mtime': u['mtime'],
            'count': u['count'],
            'viewable': False,
            'kind': 'backup',
        })
    entries.sort(key=lambda x: x['mtime'], reverse=True)
    order = {name: i for i, name in enumerate(_KNOWN_DIRS)}
    stat_list = sorted(stats.values(),
                       key=lambda s: (order.get(s['name'], 90 if s['name'] != _ROOT_STAT else 99),
                                      s['name']))
    return {'entries': entries, 'stats': stat_list}


def list_files() -> list:
    """兼容旧调用: 仅返回文件/备份单元列表。"""
    return list_entries()['entries']


def read_file(rel: str) -> tuple[str, str]:
    """读取 data/ 内的文本文件, 返回 (内容, 错误)。"""
    full = resolve(rel)
    if not full or not os.path.isfile(full):
        return '', '文件不存在'
    try:
        if os.path.getsize(full) > _MAX_VIEW_BYTES:
            return '', f'文件超过 {_MAX_VIEW_BYTES // 1024} KB, 请直接在服务器上查看'
        with open(full, encoding='utf-8', errors='replace') as f:
            return f.read(), ''
    except OSError as e:
        return '', str(e)


def delete_entry(rel: str) -> str:
    """删除 data/ 内的留档文件或备份文件夹; 返回错误信息 (空串 = 成功)。

    目录删除只允许发生在 backups/ 内 (面板的备份单元整夹删除), 其余模块目录
    (reviews/ archives/ …) 只能按单个文件删; 配置文件禁止删除。
    """
    full = resolve(rel)
    if not full or not os.path.exists(full):
        return '文件不存在'
    if os.path.isdir(full):
        broot = os.path.realpath(BACKUPS_DIR)
        if full == broot or not full.startswith(broot + os.sep):
            return '只允许删除 backups 下的备份文件夹'
        shutil.rmtree(full, ignore_errors=True)
        if os.path.exists(full):
            return '删除失败, 文件可能被占用'
        # 顺手收掉空掉的目标分组目录 (backups/<目标>/), 保持列表干净
        parent = os.path.dirname(full)
        if parent != broot and not os.listdir(parent):
            with contextlib.suppress(OSError):
                os.rmdir(parent)
        return ''
    if os.path.basename(full) in ('config.yaml',):
        return '配置文件不允许删除'
    try:
        os.remove(full)
    except OSError as e:
        return str(e)
    return ''


def delete_file(rel: str) -> str:
    """兼容旧调用名。"""
    return delete_entry(rel)
