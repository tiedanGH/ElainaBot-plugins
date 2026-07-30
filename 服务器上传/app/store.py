"""审核记录与数据文件管理 (全部落在插件 data/ 目录)。

    data/
    ├── config.yaml              插件配置
    ├── records.jsonl            审核记录 (一行一条, 便于追加与轮转)
    ├── reviews/<记录号>.md      模型的完整回复 (群里不输出, 只在此留档)
    ├── archives/<记录号>_x.zip  原压缩包留档 (keep_archive 开启时)
    ├── staging/<记录号>/        解压暂存 (部署或失败后清理)
    ├── backups/<名称>.<时间戳>/ 覆盖部署时备份的旧目录
    └── diagnostics/<记录号>.json  未能定位到文件时的原始消息载荷
"""

from __future__ import annotations

import json
import os
import shutil
import time

from .config import DATA_DIR

RECORDS_FILE = os.path.join(DATA_DIR, 'records.jsonl')
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


def get_record(rid: str) -> dict | None:
    return next((r for r in list_records(_MAX_RECORDS) if r.get('id') == rid), None)


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


def list_files() -> list:
    """列出 data/ 下全部文件 (相对路径 + 大小 + 修改时间 + 是否可文本预览)。"""
    init()
    root = os.path.realpath(DATA_DIR)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(dirnames)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace('\\', '/')
            out.append({
                'path': rel,
                'size': st.st_size,
                'mtime': int(st.st_mtime),
                'viewable': fn.lower().endswith(_TEXT_VIEW_EXTS) and st.st_size <= _MAX_VIEW_BYTES,
            })
    out.sort(key=lambda x: x['mtime'], reverse=True)
    return out


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


def delete_file(rel: str) -> str:
    """删除 data/ 内的单个留档文件; 返回错误信息 (空串 = 成功)。配置文件禁止删除。"""
    full = resolve(rel)
    if not full or not os.path.isfile(full):
        return '文件不存在'
    if os.path.basename(full) in ('config.yaml',):
        return '配置文件不允许删除'
    try:
        os.remove(full)
    except OSError as e:
        return str(e)
    return ''
