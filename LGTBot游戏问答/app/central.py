"""中央 AI LLM 模块适配层。

本插件不保存任何接口地址或密钥 —— 全部由 modules/ai_llm 管理，这里只按
``provider_id`` / ``model_preference`` 做选择（modules/ai_llm/docs/README.md）。

刻意用 ``complete()`` 而不是 ``run_agent()``：run_agent 会打开中央运行时能力
（其他插件共享的工具、MCP、Skills）。本插件面向普通群友开放，工具面必须收敛到
自己这五个只读工具，不能让模型顺手拿到别的插件注册的写操作或联网能力。

同理传 ``prepare_context=False``：历史由 store.py 自己裁剪，不需要中央再压一遍。
"""
from __future__ import annotations

CONSUMER = 'lgtbot_qa'


def _raw_service():
    """拿中央模块实例。模块可能热重载，所以每次现取，不缓存。"""
    try:
        from core.application import get_app
    except ImportError:
        return None
    app = get_app()
    manager = getattr(app, 'module_manager', None) if app else None
    if manager is None:
        return None
    service = manager.get('ai_llm')
    if service is not None:
        return service
    for item in manager.list_modules():
        if str(item.get('display_name') or '').strip() == 'AI LLM 服务':
            return manager.get(str(item.get('name') or ''))
    return None


def get_service():
    return _raw_service()


def available() -> bool:
    service = get_service()
    if service is None:
        return False
    if hasattr(service, 'available'):
        return bool(service.available())
    config = service.config()
    if not config.get('enabled'):
        return False
    return any(
        item.get('enabled') and item.get('base_url') and (item.get('model') or item.get('models'))
        for item in config.get('providers', [])
    )


def status() -> dict:
    service = get_service()
    if service is None:
        return {
            'installed': False, 'enabled': False,
            'message': '未检测到 AI LLM 模块，请前往插件市场下载并启用',
        }
    config = service.config()
    if not config.get('enabled'):
        return {'installed': True, 'enabled': False, 'message': '中央 AI LLM 未启用'}
    if not available():
        return {'installed': True, 'enabled': True, 'message': '中央 AI LLM 没有可用接口或模型'}
    return {'installed': True, 'enabled': True, 'message': '中央 AI LLM 已就绪'}


def public_config() -> dict:
    service = get_service()
    return service.config(public=True) if service else {}


def resolve_selection(provider_id: str = '', model: str = '') -> tuple[str, str]:
    """把面板上的选择校验成中央模块当前真实可用的 (provider_id, model)。

    选中的接口/模型被管理员在中央面板停用后，这里会退回空串 = 自动选择，
    而不是拿着失效的组合去调用然后报错。
    """
    config = public_config()
    providers = [item for item in config.get('providers', []) if item.get('enabled')]

    def usable(provider: dict) -> set[str]:
        disabled = {str(item) for item in provider.get('disabled_models', [])}
        values = [
            *(provider.get('model_priority') or []),
            *(provider.get('models') or []),
            provider.get('model'),
        ]
        return {
            str(item).strip() for item in values
            if str(item or '').strip() and str(item).strip() not in disabled
        }

    if provider_id:
        provider = next((item for item in providers if item.get('id') == provider_id), None)
        if provider is None:
            return '', ''
        return str(provider['id']), model if model in usable(provider) else ''
    if model:
        return ('', model) if any(model in usable(item) for item in providers) else ('', '')
    return '', ''


# 中央模块在模型不输出原生 tool_calls 时会退回 XML 文本协议，但它的解析器只认
# <工具名><参数名>值</参数名></工具名> 和无参数的 <工具名/>。模型很容易写成属性式
# <工具名 参数="值"/> —— 那样一个都匹配不上，raw XML 会被当成答案直接发给用户，
# 且中央层还记为 success（modules/ai_llm/app/service.py:114 的 self_pattern）。
#
# 这条规则写死在代码里、而不是塞进面板可编辑的 system_prompt：后者一旦被用户改写、
# 或被旧配置里存着的老提示词覆盖就会丢，而这是正确性相关的硬约束。
TOOL_FORMAT_RULE = (
    '【工具调用格式】优先使用接口原生 tool_calls。只有在不支持原生调用时才输出 XML，'
    '且必须用嵌套标签写参数：<工具名><参数名>参数值</参数名></工具名>。'
    '禁止把参数写成 XML 属性 —— <工具名 参数="值"/> 无法被解析，会导致工具完全没有执行。'
    '只有完全无参数的工具才写 <工具名/>。输出 XML 时不要同时输出解释文字。'
)

# 检测到属性式泄漏后重试那一轮追加的纠正指令
CORRECTION_PROMPT = (
    '你上一次回复把工具调用写成了 XML 属性式，工具因此没有真正执行，请重新作答。'
    '改用接口原生 tool_calls，或用 <工具名><参数名>参数值</参数名></工具名> 的嵌套写法，'
    '绝不要把参数写在标签属性里。'
)


def build_system_prompt(current: dict, scope_hint: str) -> str:
    parts = [
        str(current.get('system_prompt') or '').strip(),
        scope_hint,
        TOOL_FORMAT_RULE,
        str(current.get('extra_prompt') or '').strip(),
        f'回答控制在 {int(current.get("answer_max_chars") or 1500)} 字以内。',
    ]
    return '\n\n'.join(item for item in parts if item)


async def ask(
    current: dict, messages: list[dict], system_prompt: str,
    tool_handler, session_id: str,
) -> dict:
    """跑一轮带工具的问答。异常原样抛出，由调用方决定怎么回话。"""
    from . import tools

    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    provider_id, model = resolve_selection(
        str(current.get('provider_id') or ''), str(current.get('model_preference') or ''),
    )
    return await service.complete(
        messages=messages,
        system_prompt=system_prompt,
        provider_id=provider_id,
        model=model,
        temperature=float(current.get('temperature') or 0.2),
        max_tokens=int(current.get('max_tokens') or 4096),
        tools=tools.TOOLS_SCHEMA,
        tool_handler=tool_handler,
        max_tool_rounds=int(current.get('max_tool_rounds') or 10),
        session_id=session_id,
        consumer_plugin=CONSUMER,
        prepare_context=False,
    )
