"""SQLite 画图历史：记录每次请求的结果、耗时、线路与本地图片文件。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

STATUSES = ('success', 'failed', 'blocked')
_lock = threading.RLock()
_connection: sqlite3.Connection | None = None
_image_dir = ''


def connect(data_dir: str) -> None:
    global _connection, _image_dir
    os.makedirs(data_dir, exist_ok=True)
    _image_dir = os.path.join(data_dir, 'images')
    os.makedirs(_image_dir, exist_ok=True)
    with _lock:
        if _connection is not None:
            return
        _connection = sqlite3.connect(
            os.path.join(data_dir, 'history.db'), check_same_thread=False,
        )
        _connection.row_factory = sqlite3.Row
        _connection.execute('PRAGMA journal_mode=WAL')
        _connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                appid TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                chat_type TEXT NOT NULL DEFAULT '',
                chat_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'command',
                status TEXT NOT NULL DEFAULT 'failed',
                prompt TEXT NOT NULL DEFAULT '',
                final_prompt TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                size TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                hosted_url TEXT NOT NULL DEFAULT '',
                image_file TEXT NOT NULL DEFAULT '',
                send_mode TEXT NOT NULL DEFAULT '',
                delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ai_draw_created ON records(created_at);
            CREATE INDEX IF NOT EXISTS idx_ai_draw_user ON records(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_ai_draw_status ON records(status, created_at);
            """
        )
        _migrate()
        _connection.commit()


def _migrate() -> None:
    """为早期版本建立的表补齐后加入的列，并清掉已废弃的状态。"""
    existing = {
        row['name'] for row in _conn().execute('PRAGMA table_info(records)').fetchall()
    }
    for column in ('hosted_url', 'send_mode'):
        if column not in existing:
            _conn().execute(
                f"ALTER TABLE records ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    # 限流不再计入运行日志；旧版留下的记录在生图前就返回，不会关联本地图片。
    _conn().execute("DELETE FROM records WHERE status='limited'")


def close() -> None:
    global _connection
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


def _conn() -> sqlite3.Connection:
    if _connection is None:
        raise RuntimeError('AI 画图历史库尚未初始化')
    return _connection


def image_dir() -> str:
    return _image_dir


def image_path(file_name: str) -> str:
    """把历史记录中的文件名解析为图片目录内的绝对路径；越界时返回空串。"""
    if not _image_dir:
        return ''
    root = os.path.realpath(_image_dir)
    name = os.path.basename(str(file_name or ''))
    path = os.path.realpath(os.path.join(root, name))
    return path if name and path.startswith(root + os.sep) else ''


def add(record: dict, history_limit: int = 500) -> int:
    """写入一条画图记录并按上限裁剪旧记录。"""
    fields = (
        'appid', 'user_id', 'username', 'chat_type', 'chat_id', 'source', 'status',
        'prompt', 'final_prompt', 'provider_id', 'provider', 'model', 'size', 'error',
        'image_url', 'hosted_url', 'image_file', 'send_mode',
    )
    values = [str(record.get(key) or '') for key in fields]
    with _lock:
        cursor = _conn().execute(
            'INSERT INTO records(created_at, '
            + ', '.join(fields)
            + ', duration_ms, delivered) VALUES(?'
            + ',?' * (len(fields) + 2)
            + ')',
            (
                float(record.get('created_at') or time.time()),
                *values,
                max(0, int(record.get('duration_ms') or 0)),
                1 if record.get('delivered') else 0,
            ),
        )
        _conn().commit()
        record_id = int(cursor.lastrowid)
    _prune(history_limit)
    return record_id


def update(record_id: int, **changes) -> None:
    allowed = {
        'status', 'final_prompt', 'provider_id', 'provider', 'model', 'size',
        'duration_ms', 'error', 'image_url', 'hosted_url', 'image_file',
        'send_mode', 'delivered',
    }
    payload = {key: value for key, value in changes.items() if key in allowed}
    if not payload or not record_id:
        return
    assignments = ', '.join(f'{key}=?' for key in payload)
    parameters = [
        int(value) if key in {'duration_ms', 'delivered'} else str(value or '')
        for key, value in payload.items()
    ]
    with _lock:
        _conn().execute(
            f'UPDATE records SET {assignments} WHERE id=?', [*parameters, int(record_id)],
        )
        _conn().commit()


def save_image(record_id: int, data: bytes, extension: str, image_limit: int = 200) -> str:
    """把图片写入 data/images 并关联到记录；返回文件名，失败返回空串。"""
    if not data or not record_id or not _image_dir:
        return ''
    if int(image_limit or 0) <= 0:
        # 保留张数为 0：不落盘，同时清掉此前留下的图片。
        _prune_images(0)
        return ''
    suffix = str(extension or 'png').lstrip('.').casefold()[:5] or 'png'
    file_name = f'{int(record_id)}.{suffix}'
    path = image_path(file_name)
    if not path:
        return ''
    temporary = path + '.tmp'
    with open(temporary, 'wb') as file:
        file.write(data)
    os.replace(temporary, path)
    update(record_id, image_file=file_name)
    _prune_images(image_limit)
    return file_name


def _remove_files(names) -> None:
    for name in names:
        path = image_path(name)
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _prune(history_limit: int) -> None:
    limit = max(0, int(history_limit or 0))
    if limit <= 0:
        return
    with _lock:
        rows = _conn().execute(
            "SELECT image_file FROM records WHERE image_file<>'' AND id NOT IN "
            '(SELECT id FROM records ORDER BY id DESC LIMIT ?)',
            (limit,),
        ).fetchall()
        _conn().execute(
            'DELETE FROM records WHERE id NOT IN '
            '(SELECT id FROM records ORDER BY id DESC LIMIT ?)',
            (limit,),
        )
        _conn().commit()
    _remove_files(row['image_file'] for row in rows)


def _prune_images(image_limit: int) -> None:
    limit = max(0, int(image_limit or 0))
    with _lock:
        if limit <= 0:
            rows = _conn().execute(
                "SELECT id, image_file FROM records WHERE image_file<>''",
            ).fetchall()
        else:
            rows = _conn().execute(
                "SELECT id, image_file FROM records WHERE image_file<>'' AND id NOT IN "
                "(SELECT id FROM records WHERE image_file<>'' ORDER BY id DESC LIMIT ?)",
                (limit,),
            ).fetchall()
        if rows:
            _conn().executemany(
                "UPDATE records SET image_file='' WHERE id=?",
                [(row['id'],) for row in rows],
            )
            _conn().commit()
    _remove_files(row['image_file'] for row in rows)


def get(record_id: int) -> dict | None:
    with _lock:
        row = _conn().execute(
            'SELECT * FROM records WHERE id=?', (int(record_id),),
        ).fetchone()
    return dict(row) if row else None


def query(
    *, status: str = '', keyword: str = '', user_id: str = '', chat_id: str = '',
    with_image: bool = False, limit: int = 50, offset: int = 0,
) -> dict:
    """分页查询历史记录，供画廊与日志页共用。"""
    where = ['1=1']
    params: list = []
    # 传了状态就按它精确过滤：已废弃的状态自然查不到记录，而不是退化成「全部」。
    wanted = str(status or '').strip()[:20]
    if wanted:
        where.append('status=?')
        params.append(wanted)
    if user_id:
        where.append('user_id=?')
        params.append(str(user_id))
    if chat_id:
        where.append('chat_id=?')
        params.append(str(chat_id))
    if with_image:
        where.append("(image_file<>'' OR hosted_url<>'' OR image_url<>'')")
    value = str(keyword or '').strip()
    if value:
        where.append('(prompt LIKE ? OR final_prompt LIKE ? OR error LIKE ? OR username LIKE ?)')
        pattern = f'%{value}%'
        params.extend([pattern] * 4)
    clause = ' AND '.join(where)
    size = min(200, max(1, int(limit)))
    start = max(0, int(offset))
    with _lock:
        total = _conn().execute(
            f'SELECT COUNT(*) FROM records WHERE {clause}', params,
        ).fetchone()[0]
        rows = _conn().execute(
            f'SELECT * FROM records WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?',
            [*params, size, start],
        ).fetchall()
    return {'total': int(total), 'limit': size, 'offset': start, 'items': [dict(row) for row in rows]}


def count_since(since: float, user_id: str = '') -> int:
    where = 'created_at>=? AND status=?'
    params: list = [float(since), 'success']
    if user_id:
        where += ' AND user_id=?'
        params.append(str(user_id))
    with _lock:
        return int(_conn().execute(
            f'SELECT COUNT(*) FROM records WHERE {where}', params,
        ).fetchone()[0])


def stats(day_start: float) -> dict:
    with _lock:
        row = _conn().execute(
            'SELECT COUNT(*) AS total, '
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked, "
            'COUNT(DISTINCT user_id) AS users FROM records'
        ).fetchone()
        today = _conn().execute(
            'SELECT COUNT(*) FROM records WHERE created_at>=?', (float(day_start),),
        ).fetchone()[0]
        average = _conn().execute(
            "SELECT AVG(duration_ms) FROM records WHERE status='success' AND duration_ms>0"
        ).fetchone()[0]
        images = _conn().execute(
            "SELECT COUNT(*) FROM records WHERE image_file<>''"
        ).fetchone()[0]
    return {
        'total': int(row['total'] or 0),
        'success': int(row['success'] or 0),
        'failed': int(row['failed'] or 0),
        'blocked': int(row['blocked'] or 0),
        'users': int(row['users'] or 0),
        'today': int(today or 0),
        'stored_images': int(images or 0),
        'average_ms': round(float(average or 0)),
    }


def delete(record_id: int) -> bool:
    with _lock:
        row = _conn().execute(
            'SELECT image_file FROM records WHERE id=?', (int(record_id),),
        ).fetchone()
        if row is None:
            return False
        _conn().execute('DELETE FROM records WHERE id=?', (int(record_id),))
        _conn().commit()
    _remove_files([row['image_file']])
    return True


def clear() -> int:
    with _lock:
        rows = _conn().execute(
            "SELECT image_file FROM records WHERE image_file<>''",
        ).fetchall()
        cursor = _conn().execute('DELETE FROM records')
        _conn().commit()
        deleted = cursor.rowcount
    _remove_files(row['image_file'] for row in rows)
    return deleted
