"""画图能力适配层。

「AI 画图」插件把自己的出图能力注册成了中央 AI LLM 的共享工具
（`ai_draw:tool:draw`，见 AI画图/app/capability.py），任何插件都能通过
``service.call_capability()`` 调用它。本模块把那个能力包装成本插件模型可见的
第七个工具 ``draw_image``。

**能力键不自己拼**：按中央模块文档的要求，从 ``list_capabilities()`` 的返回值里
取 ``key``。自己拼字符串的话，对方改了 id 或 source 就会静默失效，而且绕过了
「这个能力当前是否授权给我」的判断。

分工：出图、内容审核、留档、每日额度全部发生在 AI 画图那边，本插件只负责
「要不要给模型这个工具」「一次问答最多画几张」，以及把图片发给用户。
"""
from __future__ import annotations

import asyncio
import hashlib

from core.base.logger import PLUGIN, get_logger

from . import central, store

log = get_logger(PLUGIN, 'LGTBot游戏问答')

# 正在跑的画图任务：缓存键 → Task。
#
# 这两层（内存任务 + 落盘缓存）是给「请求被打断」准备的。中央模块的单次调用超时
# 覆盖**整个** complete()，工具执行时间也算在内；画图动辄几十秒，一旦撞线，
# asyncio.wait_for 会取消整条调用链，中央那边记成 error='interrupted'
# （modules/ai_llm/app/service.py:1893），而我们这边会退避重试。
#
# 问题在于：图那时候往往**已经在画甚至画完了**。重试再画一张，等于同一个请求
# 烧两次额度，用户还是什么都没收到。所以画图跑在独立任务里、用 shield 挡住取消：
# 调用方被取消，任务照样跑完并把结果写进缓存，下次（重试或用户重问）直接复用。
_pending: dict = {}


def _normalize(prompt: str) -> str:
    return ' '.join(str(prompt or '').split())


def _cache_key(scope: str, prompt: str) -> str:
    """按「谁问的 + 画什么」做键。

    带上 scope 是刻意的：不同用户提同样的描述，各自出各自的图，不会拿到别人那张。
    只在同一个人重试 / 重问时才复用 —— 那正是被打断后要救的场景。
    """
    raw = f'{scope}\x00{_normalize(prompt)}'.encode()
    return hashlib.sha1(raw).hexdigest()


def _cache_ttl(current: dict) -> int:
    return int(current.get('draw_cache_seconds') or 0)

SOURCE = 'ai_draw'          # AI画图 注册时用的 source_plugin
CAPABILITY_ID = 'draw'      # 它的能力 id
TOOL_NAME = 'draw_image'    # 暴露给模型的工具名（与对方内部 id 区分开）
CALLER = 'LGTBot游戏问答'   # 传给对方留档用的调用方标注

# 画面描述的长度上限。AI 画图那边自己也有 input_max_length，这里先截一道，
# 避免模型把整段源码当描述丢过去。
_PROMPT_MAX = 600

TOOL_SCHEMA = {
    'type': 'function',
    'function': {
        'name': TOOL_NAME,
        'description': (
            '生成一张图片。**只有用户明确要求画图、生成图片、做张图时才调用**；'
            '规则、机制、计分、结算类问题一律用检索工具回答，不要配图。'
            '**要画的东西属于某个游戏时，必须先用 read_game_source / read_game_rule '
            '读到它在该游戏里的真实描述，再据此写画面描述** —— 凭游戏名想象会被拒绝。'
            '描述要写清主体、动作、场景、风格，越具体越好，不要传入任何链接。'
            '图片会由机器人自动发送给用户，你拿不到也不需要图片链接 —— '
            '回答里不要写链接、不要写 Markdown 图片语法。每次回答最多调用一次。'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {
                    'type': 'string',
                    'description': (
                        '画面描述，中文英文均可。用户只给了一句话时可以补充细节，'
                        '但不要改变原意，也不要加入用户没提过的元素。'
                    ),
                },
            },
            'required': ['prompt'],
            'additionalProperties': False,
        },
    },
}


def enabled(current: dict) -> bool:
    """管理员是否在本插件面板打开了画图。不查对方是否在线 —— 那是运行时的事。"""
    return bool(current.get('draw_enabled'))


def _all_draw_tools(service) -> dict | None:
    """不带消费者过滤地找这个能力，用于把「没装」和「没授权」区分开。"""
    try:
        items = service.plugin_capabilities(kind='tool', public=True)
    except Exception:  # noqa: BLE001 — 面板诊断用，取不到就按未安装处理
        return None
    return next((
        item for item in items
        if item.get('source_plugin') == SOURCE and item.get('id') == CAPABILITY_ID
    ), None)


def _authorized(service) -> dict | None:
    """取**本插件当前真的能调**的那条能力记录（含在线判断）。"""
    try:
        items = service.list_capabilities(central.CONSUMER, 'tool')
    except Exception:  # noqa: BLE001 — 发现失败等同于不可用
        return None
    return next((
        item for item in items
        if item.get('source_plugin') == SOURCE and item.get('id') == CAPABILITY_ID
    ), None)


def state() -> dict:
    """面板诊断：到底是没装、没启用、没共享，还是没在线。

    这几种情况在模型侧的表现完全一样（工具调用失败），但管理员要做的事完全不同，
    所以必须分开说清楚，而不是笼统回一句「画图不可用」。
    """
    service = central.get_service()
    if service is None:
        return {'installed': False, 'usable': False, 'message': '中央 AI LLM 模块不可用'}
    if not hasattr(service, 'list_capabilities'):
        return {
            'installed': False, 'usable': False,
            'message': '当前 AI LLM 模块版本不支持插件能力共享，请升级模块',
        }
    known = _all_draw_tools(service)
    if known is None:
        return {
            'installed': False, 'usable': False,
            'message': '未检测到「AI 画图」注册的画图能力，请确认已安装并启用 AI画图（≥1.5.0）',
        }
    name = str(known.get('name') or '画图')
    if not known.get('online'):
        return {
            'installed': True, 'usable': False,
            'message': f'{name}能力当前不在线：AI 画图插件未加载，或它自己的「对外开放」开关是关的',
        }
    if not known.get('enabled'):
        return {
            'installed': True, 'usable': False,
            'message': f'{name}能力在 AI LLM 面板里被停用了',
        }
    if _authorized(service) is None:
        return {
            'installed': True, 'usable': False,
            'message': (
                f'{name}能力未授权给本插件：请在 AI LLM 面板把它设为「共享给所有插件」，'
                f'或把 {central.CONSUMER} 加进它的 allowed_consumers'
            ),
        }
    return {'installed': True, 'usable': True, 'message': f'{name}能力已就绪，可供模型调用'}


def detach_image(result: dict) -> dict:
    """把图片链接与尺寸从**回灌给模型**的结果里摘出来。

    刻意不让模型看到链接：它拿到就会往答案里贴，而 QQ 那边裸链接会被风控、
    Markdown 图片语法在普通文本消息里也渲染不出来，最后用户只看到一串 URL。
    图片由插件自己发一条消息，模型只需要知道「画好了」。

    返回 {} 表示这次没出图。
    """
    if not isinstance(result, dict):
        return {}
    url = str(result.pop('url', '') or '')
    size = str(result.pop('size', '') or '')
    reused = bool(result.pop('reused', False))
    result.pop('record_id', None)
    if not (result.get('ok') and url):
        return {}
    result['note'] = (
        '这张图之前已经画好了，直接复用，没有重新生成。' if reused
        else '图片已生成，'
    ) + '机器人会自动发给用户。直接用一句话回应即可，不要写链接。'
    return {'url': url, 'size': size, 'reused': reused}


async def _generate(prompt: str, current: dict, key: str) -> dict:
    """真正调一次画图，成功就落盘缓存。

    **这个协程跑在独立任务里**（见 run 的 shield）：调用方被超时取消也不影响它跑完。
    图已经在画了，丢掉纯属浪费额度 —— 跑完把结果写进缓存，下次直接拿。
    """
    service = central.get_service()
    if service is None or not hasattr(service, 'call_capability'):
        return {'ok': False, 'error': '中央 AI LLM 模块不可用'}
    item = _authorized(service)
    if item is None:
        return {'ok': False, 'error': state()['message']}

    # 出图比一次模型调用慢得多（实测几十秒），必须单独设超时：任务挂住不放会一直
    # 留在 _pending 里，后面同样的描述会一直等它。
    timeout = float(current.get('draw_timeout_seconds') or 0)
    call = service.call_capability(
        consumer_plugin=central.CONSUMER,
        capability_key=str(item['key']),
        arguments={'prompt': prompt, 'caller': CALLER},
    )
    try:
        result = await (asyncio.wait_for(call, timeout) if timeout > 0 else call)
    except asyncio.TimeoutError:
        return {'ok': False, 'error': f'画图超时（{timeout:.0f} 秒未返回）'}
    except Exception as error:  # noqa: BLE001 — 失败原因原样给模型，让它换个说法或放弃
        log.warning(f'画图能力调用失败: {type(error).__name__}: {error}')
        return {'ok': False, 'error': f'画图失败: {error}'[:300]}

    if not isinstance(result, dict):
        return {'ok': False, 'error': '画图能力返回了无法识别的结果'}
    if not result.get('ok'):
        # 对方的失败原因（命中违规词、额度用完、线路全挂）原样透给模型，
        # 它才知道该不该换个描述再试一次。
        return {'ok': False, 'error': str(result.get('error') or '画图失败')[:300]}
    url = str(result.get('url') or '')
    if not url:
        return {'ok': False, 'error': '画图成功但没有拿到图片链接'}
    # size 是 AI 画图配置的出图尺寸（如 1024x1024）。留着给 Markdown 图片消息用 ——
    # QQ 的 `![alt #宽px #高px](url)` 要这两个数，有真实值就不用瞎猜。
    size = str(result.get('size') or '')
    if _cache_ttl(current) > 0:
        try:
            await asyncio.to_thread(store.draw_cache_put, key, url, size)
        except Exception as error:  # noqa: BLE001 — 缓存写不进去顶多下次重画
            log.warning(f'画图结果缓存失败: {error}')
    log.info(f'画图完成（{result.get("model") or "未知模型"}）')
    return {'ok': True, 'url': url, 'model': str(result.get('model') or ''), 'size': size}


async def _cached(key: str, current: dict) -> dict:
    ttl = _cache_ttl(current)
    if ttl <= 0:
        return {}
    try:
        return await asyncio.to_thread(store.draw_cache_get, key, ttl)
    except Exception as error:  # noqa: BLE001 — 缓存读不到就当没有，照常画
        log.debug(f'画图缓存读取失败: {error}')
        return {}


async def reusable(arguments: dict, current: dict, scope: str = '') -> bool:
    """这次画图能不能**不花额度**拿到图：缓存里有，或已经有一模一样的在跑。

    给 main.py 的张数上限用 —— 复用不该占额度，否则被打断后重试会卡在
    「本次已经画过 1 张」上，明明有现成的图却发不出去。
    """
    if not enabled(current):
        return False
    prompt = _normalize(str((arguments or {}).get('prompt') or ''))[:_PROMPT_MAX]
    if not prompt:
        return False
    key = _cache_key(scope, prompt)
    task = _pending.get(key)
    if task is not None and not task.done():
        return True
    return bool(await _cached(key, current))


async def run(arguments: dict, current: dict, scope: str = '') -> dict:
    """执行一次画图。任何失败都收敛成 {'ok': False, 'error': ...} 交给模型。

    三条路，按代价从低到高：
      1. 缓存命中 —— 同一个人刚画过同样的东西，直接复用
      2. 已经有一模一样的任务在跑 —— 等它，不再起一个
      3. 都没有 —— 起一个独立任务去画

    `asyncio.shield` 是关键：我们这次 await 被取消（外层超时）时，任务本身**不会**
    被取消，会继续画完并写缓存。否则每次超时都白扔一张已经在画的图。
    """
    if not enabled(current):
        return {'ok': False, 'error': '画图能力未开启'}
    prompt = _normalize(str((arguments or {}).get('prompt') or ''))
    if not prompt:
        return {'ok': False, 'error': '缺少画面描述 prompt'}
    prompt = prompt[:_PROMPT_MAX]
    key = _cache_key(scope, prompt)

    cached = await _cached(key, current)
    if cached:
        log.info('画图命中缓存，直接复用已有图片')
        return {'ok': True, 'reused': True, 'model': '', **cached}

    task = _pending.get(key)
    if task is None or task.done():
        task = asyncio.ensure_future(_generate(prompt, current, key))
        _pending[key] = task
        # 任务自己收尾：跑完就从 _pending 摘掉，别让字典无限长
        task.add_done_callback(
            lambda done, k=key: _pending.pop(k, None) if _pending.get(k) is done else None
        )
    else:
        log.info('同样的描述已经在画，等它出结果而不是再画一张')
    return await asyncio.shield(task)
