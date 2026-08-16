"""画图限流：用户与会话冷却、每日配额和并发上限。"""
from __future__ import annotations

import asyncio
import time

from . import store

_user_last: dict[str, float] = {}
_chat_last: dict[str, float] = {}
_semaphore: asyncio.Semaphore | None = None
_semaphore_size = 0


def reset() -> None:
    global _semaphore, _semaphore_size
    _user_last.clear()
    _chat_last.clear()
    _semaphore = None
    _semaphore_size = 0


def day_start(now: float | None = None) -> float:
    """返回本地时间当天零点的时间戳，用于每日配额与统计。"""
    value = time.localtime(now if now is not None else time.time())
    return time.mktime((
        value.tm_year, value.tm_mon, value.tm_mday, 0, 0, 0, 0, 0, -1,
    ))


def semaphore(max_concurrency: int) -> asyncio.Semaphore:
    """按配置返回并发闸门；上限变化时重建。"""
    global _semaphore, _semaphore_size
    size = max(1, int(max_concurrency or 1))
    if _semaphore is None or _semaphore_size != size:
        _semaphore = asyncio.Semaphore(size)
        _semaphore_size = size
    return _semaphore


def _remaining(seconds: float) -> str:
    value = max(1, int(seconds + 0.999))
    if value < 60:
        return f'{value} 秒'
    return f'{value // 60} 分 {value % 60} 秒' if value % 60 else f'{value // 60} 分钟'


def check(config: dict, *, appid: str, user_id: str, chat_id: str, is_owner: bool) -> str:
    """通过返回空串；被限流时返回可直接展示的原因，并在通过时记录冷却。"""
    if is_owner and config.get('owner_bypass_limits'):
        return ''
    now = time.monotonic()
    user_key = f'{appid}:{user_id}'
    chat_key = f'{appid}:{chat_id}'
    user_cooldown = int(config.get('user_cooldown_seconds', 0))
    if user_cooldown > 0:
        elapsed = now - _user_last.get(user_key, -user_cooldown)
        if elapsed < user_cooldown:
            return f'请等待 {_remaining(user_cooldown - elapsed)} 后再画'
    chat_cooldown = int(config.get('chat_cooldown_seconds', 0))
    if chat_cooldown > 0 and chat_id:
        elapsed = now - _chat_last.get(chat_key, -chat_cooldown)
        if elapsed < chat_cooldown:
            return f'当前会话请等待 {_remaining(chat_cooldown - elapsed)}'
    since = day_start()
    user_limit = int(config.get('user_daily_limit', 0))
    if user_limit > 0 and store.count_since(since, user_id) >= user_limit:
        return f'今天的画图次数已用完（每人 {user_limit} 张）'
    global_limit = int(config.get('global_daily_limit', 0))
    if global_limit > 0 and store.count_since(since) >= global_limit:
        return f'今天全局画图额度已用完（共 {global_limit} 张）'
    _user_last[user_key] = now
    if chat_id:
        _chat_last[chat_key] = now
    return ''


def release(appid: str, user_id: str, chat_id: str) -> None:
    """请求未真正发起时回退冷却记录，避免占用用户额度。"""
    _user_last.pop(f'{appid}:{user_id}', None)
    _chat_last.pop(f'{appid}:{chat_id}', None)
