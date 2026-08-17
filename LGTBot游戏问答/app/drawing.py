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

from core.base.logger import PLUGIN, get_logger

from . import central

log = get_logger(PLUGIN, 'LGTBot游戏问答')

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


def detach_url(result: dict) -> str:
    """把图片链接从**回灌给模型**的结果里摘出来。

    刻意不让模型看到链接：它拿到就会往答案里贴，而 QQ 那边裸链接会被风控、
    Markdown 图片语法也渲染不出来，最后用户只看到一串乱码般的 URL。
    图片由插件自己 reply_image 发送，模型只需要知道「画好了」。
    """
    if not isinstance(result, dict):
        return ''
    url = str(result.pop('url', '') or '')
    result.pop('record_id', None)
    if result.get('ok'):
        result['note'] = '图片已生成，机器人会自动发给用户。直接用一句话回应即可，不要写链接。'
    return url


async def run(arguments: dict, current: dict) -> dict:
    """执行一次画图。任何失败都收敛成 {'ok': False, 'error': ...} 交给模型。"""
    if not enabled(current):
        return {'ok': False, 'error': '画图能力未开启'}
    prompt = str((arguments or {}).get('prompt') or '').strip()
    if not prompt:
        return {'ok': False, 'error': '缺少画面描述 prompt'}
    prompt = prompt[:_PROMPT_MAX]

    service = central.get_service()
    if service is None or not hasattr(service, 'call_capability'):
        return {'ok': False, 'error': '中央 AI LLM 模块不可用'}
    item = _authorized(service)
    if item is None:
        return {'ok': False, 'error': state()['message']}

    # 出图比一次模型调用慢得多（实测几十秒），必须单独设超时：这一路挂住会一直
    # 占着本插件的并发槽，后面所有人的提问都被挡在门外。
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
    log.info(f'画图完成（{result.get("model") or "未知模型"}）')
    return {'ok': True, 'url': url, 'model': str(result.get('model') or '')}
