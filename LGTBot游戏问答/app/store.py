"""SQLite 存储：追问上下文、每日用量、累计统计。

用量必须落盘 —— 只放内存的话，插件一热重载每日上限就归零，等于没有上限。

所有函数都是同步阻塞的，调用方一律用 asyncio.to_thread 包起来（见 main.py）。
连接开在 check_same_thread=False + 单锁串行，避免线程池里多线程并发写。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
# 记住数据目录，close() 之后仍能自动重连（见 _db 的说明）
_data_dir = ''

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT    NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope, id);

CREATE TABLE IF NOT EXISTS usage (
    user_id  TEXT    NOT NULL,
    day      TEXT    NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    last_ts  REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS counters (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

-- 已经画出来的图。画一张要几十秒、要烧对方的额度，画完就得留住 ——
-- 请求被超时打断、用户重问一遍时直接复用，不再画第二张。
CREATE TABLE IF NOT EXISTS draw_cache (
    key        TEXT PRIMARY KEY,
    url        TEXT    NOT NULL,
    size       TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""


def _open() -> sqlite3.Connection:
    """真正建连。**调用方必须已持有 _lock** —— threading.Lock 不可重入，
    在这里再 with _lock 会直接死锁（_db 就是在持锁状态下调过来的）。
    """
    global _conn
    os.makedirs(_data_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(_data_dir, 'qa.db'), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(_SCHEMA)
    conn.commit()
    _conn = conn
    return conn


def connect(data_dir: str) -> None:
    global _data_dir
    with _lock:
        _data_dir = str(data_dir or _data_dir)
        if _conn is None:
            _open()


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _db() -> sqlite3.Connection:
    """取连接，必要时自动重连。调用方已持有 _lock。

    为什么要自动重连：热重载时 on_unload 会 close()，而那一刻可能还有请求在飞
    （一次问答要跑十几秒）。原先直接抛「存储尚未初始化」，在飞的请求全部报错；
    更糟的是**异常处理里也要写统计**，于是二次抛异常，_reply 根本没机会执行 ——
    用户什么都收不到，表现出来就是「卡死」。
    """
    if _conn is None:
        if not _data_dir:
            raise RuntimeError('存储尚未初始化')
        return _open()
    return _conn


# ==================== 上下文 ====================


def append(scope: str, role: str, content: str, keep: int) -> int:
    """写入一条消息并裁掉超出 keep 的旧记录，返回新消息 id。"""
    with _lock:
        db = _db()
        cursor = db.execute(
            'INSERT INTO messages (scope, role, content, created_at) VALUES (?, ?, ?, ?)',
            (scope, role, content, int(time.time())),
        )
        db.execute(
            'DELETE FROM messages WHERE scope = ? AND id NOT IN '
            '(SELECT id FROM messages WHERE scope = ? ORDER BY id DESC LIMIT ?)',
            (scope, scope, max(1, int(keep))),
        )
        db.commit()
        return int(cursor.lastrowid)


def remove(message_id: int) -> None:
    """回滚刚写入的用户消息（模型调用失败时用，避免留下没有答复的半截对话）。"""
    with _lock:
        db = _db()
        db.execute('DELETE FROM messages WHERE id = ?', (int(message_id),))
        db.commit()


def history(scope: str, limit: int, expire_seconds: int) -> list[dict]:
    """按时间正序返回最近 limit 条未过期消息。"""
    if limit <= 0:
        return []
    floor = int(time.time()) - max(60, int(expire_seconds))
    with _lock:
        rows = _db().execute(
            'SELECT role, content FROM messages WHERE scope = ? AND created_at >= ? '
            'ORDER BY id DESC LIMIT ?',
            (scope, floor, int(limit)),
        ).fetchall()
    return [{'role': row['role'], 'content': row['content']} for row in reversed(rows)]


def clear(scope: str) -> int:
    with _lock:
        db = _db()
        cursor = db.execute('DELETE FROM messages WHERE scope = ?', (scope,))
        db.commit()
        return cursor.rowcount


def clear_all() -> int:
    """清掉**所有人**的追问上下文。

    只动 messages —— 用量、统计、已画图片缓存都不碰：那些是运营数据，
    清上下文是「让大家重新开个头」，不该顺手把每日上限也重置了。
    """
    with _lock:
        db = _db()
        cursor = db.execute('DELETE FROM messages')
        db.commit()
        return cursor.rowcount


def prune_expired(expire_seconds: int, draw_ttl: int = 86400) -> int:
    floor = int(time.time()) - max(60, int(expire_seconds))
    with _lock:
        db = _db()
        cursor = db.execute('DELETE FROM messages WHERE created_at < ?', (floor,))
        db.execute('DELETE FROM usage WHERE day < ?', (_day(-30),))
        db.execute(
            'DELETE FROM draw_cache WHERE created_at < ?',
            (int(time.time()) - max(60, int(draw_ttl)),),
        )
        db.commit()
        return cursor.rowcount


# ==================== 画图结果缓存 ====================


def draw_cache_get(key: str, ttl: int) -> dict:
    """取一张还没过期的已画图片。ttl <= 0 表示不启用缓存。"""
    if ttl <= 0 or not key:
        return {}
    floor = int(time.time()) - int(ttl)
    with _lock:
        row = _db().execute(
            'SELECT url, size FROM draw_cache WHERE key = ? AND created_at >= ?',
            (str(key), floor),
        ).fetchone()
    return {'url': row['url'], 'size': row['size']} if row else {}


def draw_cache_put(key: str, url: str, size: str = '') -> None:
    if not key or not url:
        return
    with _lock:
        db = _db()
        db.execute(
            'INSERT INTO draw_cache (key, url, size, created_at) VALUES (?, ?, ?, ?) '
            'ON CONFLICT(key) DO UPDATE SET url = excluded.url, size = excluded.size, '
            'created_at = excluded.created_at',
            (str(key), str(url), str(size or ''), int(time.time())),
        )
        db.commit()


# ==================== 用量 ====================


def _day(offset: int = 0) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(time.time() + offset * 86400))


def usage_of(user_id: str) -> dict:
    with _lock:
        row = _db().execute(
            'SELECT count, last_ts FROM usage WHERE user_id = ? AND day = ?',
            (user_id, _day()),
        ).fetchone()
    return {
        'count': int(row['count']) if row else 0,
        'last_ts': float(row['last_ts']) if row else 0.0,
    }


def record_usage(user_id: str) -> int:
    """计数 +1 并刷新最后一次提问时间，返回今日累计次数。"""
    day, now = _day(), time.time()
    with _lock:
        db = _db()
        db.execute(
            'INSERT INTO usage (user_id, day, count, last_ts) VALUES (?, ?, 1, ?) '
            'ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1, last_ts = excluded.last_ts',
            (user_id, day, now),
        )
        row = db.execute(
            'SELECT count FROM usage WHERE user_id = ? AND day = ?', (user_id, day),
        ).fetchone()
        db.commit()
        return int(row['count']) if row else 1


def bump(key: str, amount: int = 1) -> None:
    with _lock:
        db = _db()
        db.execute(
            'INSERT INTO counters (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = value + excluded.value',
            (key, int(amount)),
        )
        db.commit()


def stats() -> dict:
    with _lock:
        db = _db()
        counters = {
            row['key']: int(row['value'])
            for row in db.execute('SELECT key, value FROM counters').fetchall()
        }
        today = db.execute(
            'SELECT COALESCE(SUM(count), 0) AS total, COUNT(*) AS users '
            'FROM usage WHERE day = ?', (_day(),),
        ).fetchone()
        contexts = db.execute(
            'SELECT COUNT(DISTINCT scope) AS scopes, COUNT(*) AS messages FROM messages',
        ).fetchone()
    return {
        'questions_total': counters.get('questions', 0),
        'answers_total': counters.get('answers', 0),
        'failures_total': counters.get('failures', 0),
        'tool_calls_total': counters.get('tool_calls', 0),
        'regenerations_total': counters.get('regenerations', 0),
        'synthesized_total': counters.get('synthesized', 0),
        'overflows_total': counters.get('overflows', 0),
        'recovered_calls_total': counters.get('recovered_calls', 0),
        'draws_total': counters.get('draws', 0),
        'draws_reused_total': counters.get('draws_reused', 0),
        'resumed_total': counters.get('resumed', 0),
        'busy_global_total': counters.get('busy_global', 0),
        'ungrounded_total': counters.get('ungrounded', 0),
        'blocked_input_total': counters.get('blocked_input', 0),
        'blocked_output_total': counters.get('blocked_output', 0),
        'questions_today': int(today['total']),
        'users_today': int(today['users']),
        'active_contexts': int(contexts['scopes']),
        'stored_messages': int(contexts['messages']),
    }
