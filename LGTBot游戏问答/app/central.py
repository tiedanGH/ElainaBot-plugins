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


def build_system_prompt(current: dict, scope_hint: str) -> str:
    parts = [
        str(current.get('system_prompt') or '').strip(),
        scope_hint,
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
