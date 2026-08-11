"""按用户的冷却与每日上限。

框架 ``@handler(cooldown=...)`` 只是被 decorators.py 存进 handler 字典，core 里
没有任何地方读它（见 core/plugin/decorators.py:45），所以限流必须自己做。

四道闸，顺序固定：
  1. 单人并发闸 —— 同一用户上一问还在跑就直接拒，避免刷屏把模型调用堆起来
  2. 全局并发闸 —— 同时在跑的问答总数上限
  3. 冷却闸 —— 距上次提问不足 cooldown_seconds
  4. 日限闸 —— 今日已达 daily_limit

主人可按配置豁免 3、4，但**不豁免前两道**：并发闸防的是把服务打满，不是防滥用。

为什么必须有全局闸：一次问答要跑 1 次输入审核 + 1~2 次问答（每次最多 10 轮工具
调用）+ 1 次输出审核，实测单次 10~25 秒。只拦单人的话，几个玩家同时问就是几倍
的并发打到同一个接口上，模型侧排队、插件侧线程池也吃紧，表现出来就是「卡死」。
超了直接拒并告诉用户稍后再来，比让所有人一起慢要好。
"""
from __future__ import annotations

import asyncio
import time

from . import store

# user_id -> 开始时间（time.monotonic）
_running: dict[str, float] = {}


def _sweep(now: float, ttl: float) -> None:
    """清掉超时残留的占位。

    正常路径靠 finally 释放，但进程异常、任务被取消等情况下 release 可能丢失，
    槽位就会被永久占住 —— 全局闸下这会让**所有人**都问不了，比单人卡住严重得多。
    """
    for user_id, started in list(_running.items()):
        if now - started >= ttl:
            _running.pop(user_id, None)


def acquire(user_id: str, current: dict) -> str:
    """占位。返回空串表示成功，否则是拒绝原因：'self' / 'global'。"""
    now = time.monotonic()
    # 兜底 TTL 取总超时的两倍：正常请求一定在这之前结束，还没结束就是真出问题了
    _sweep(now, max(60.0, float(current.get('request_timeout_seconds') or 90) * 2))
    if user_id in _running:
        return 'self'
    limit = int(current.get('max_concurrent') or 0)
    if limit > 0 and len(_running) >= limit:
        return 'global'
    _running[user_id] = now
    return ''


def release(user_id: str) -> None:
    _running.pop(user_id, None)


def active() -> int:
    """当前在跑的问答数（面板统计用）。"""
    return len(_running)


def clear() -> None:
    _running.clear()


async def check(user_id: str, current: dict, is_owner: bool) -> tuple[bool, str]:
    """返回 (是否放行, 拒绝话术)。放行时话术为空。

    只做判断不计数 —— 计数在真正调用模型之后（见 main.py），否则被限流拒掉的
    请求也会吃掉今日额度。
    """
    if is_owner and current.get('owner_unlimited'):
        return True, ''
    cooldown = int(current.get('cooldown_seconds') or 0)
    daily = int(current.get('daily_limit') or 0)
    if cooldown <= 0 and daily <= 0:
        return True, ''
    usage = await asyncio.to_thread(store.usage_of, user_id)
    if daily > 0 and usage['count'] >= daily:
        return False, str(current.get('daily_limit_reply') or '').format(limit=daily)
    if cooldown > 0 and usage['last_ts'] > 0:
        remaining = cooldown - (time.time() - usage['last_ts'])
        if remaining > 0:
            template = str(current.get('cooldown_reply') or '')
            return False, template.format(seconds=max(1, int(remaining + 0.5)))
    return True, ''
