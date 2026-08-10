"""按用户的冷却与每日上限。

框架 ``@handler(cooldown=...)`` 只是被 decorators.py 存进 handler 字典，core 里
没有任何地方读它（见 core/plugin/decorators.py:45），所以限流必须自己做。

三道闸，顺序固定：
  1. 并发闸 —— 同一用户上一问还在跑就直接拒，避免刷屏把模型调用堆起来
  2. 冷却闸 —— 距上次提问不足 cooldown_seconds
  3. 日限闸 —— 今日已达 daily_limit

主人可按配置豁免 2、3，但**不豁免并发闸**：那道闸防的是把自己卡死，不是防滥用。
"""
from __future__ import annotations

import asyncio
import time

from . import store

_running: dict[str, float] = {}
_RUNNING_TTL = 300.0        # 兜底：进程异常导致 release 丢失时，5 分钟后自动放行


def acquire(user_id: str) -> bool:
    """占位成功返回 True；该用户已有请求在跑返回 False。"""
    now = time.monotonic()
    started = _running.get(user_id)
    if started is not None and now - started < _RUNNING_TTL:
        return False
    _running[user_id] = now
    return True


def release(user_id: str) -> None:
    _running.pop(user_id, None)


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
