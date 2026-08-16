"""中央 AI LLM 模块 (modules/ai_llm) 适配层。

审核不再自己持有接口地址与密钥 —— 统一走框架的 LLM 中央模块, 与 AI 开发插件、
AI 聊天陪伴共用同一套接口配置、模型优先级与故障切换 (对接方式见
``modules/ai_llm/docs/README.md``, 实现参照 ``plugins/AI聊天陪伴/app/central.py``)。
本插件配置里只保留 ``provider_id`` / ``model`` 两个**选择**, 绝不保存 API Key。

按中央模块的建议, 服务实例**每次现取不缓存**: ai_llm 热重载后旧实例会失效,
缓存住会一直拿到 None 或过期对象。
"""

from __future__ import annotations

import re

# 审核是一次性的结构化判定, 不需要中央的 Skills / Agent / MCP / 工具调用,
# 也不需要中央再裁剪上下文 (送审正文必须原样送到, 少一个字都可能漏审)。
CONSUMER = 'lgtbot_deploy_review'
_MODULE_NAME = 'ai_llm'
_MODULE_DISPLAY = 'AI LLM 服务'
_HTTP_RE = re.compile(r'HTTP (\d{3})')


def _raw_service():
    try:
        from core.application import get_app
    except ImportError:
        return None
    app = get_app()
    manager = getattr(app, 'module_manager', None) if app else None
    if manager is None:
        return None
    service = manager.get(_MODULE_NAME)
    if service is not None:
        return service
    for item in manager.list_modules():
        if str(item.get('display_name') or '').strip() == _MODULE_DISPLAY:
            return manager.get(str(item.get('name') or ''))
    return None


def get_service():
    return _raw_service()


def public_config() -> dict:
    """中央模块的公开配置 (密钥已脱敏), 供面板生成接口 / 模型选项。"""
    service = get_service()
    return service.config(public=True) if service is not None else {}


def provider_models(provider: dict) -> list:
    """某接口下可用的模型 (按配置的优先级顺序, 去掉停用的)。"""
    disabled = {str(item) for item in (provider or {}).get('disabled_models', [])}
    values = [
        *((provider or {}).get('model_priority') or []),
        *((provider or {}).get('models') or []),
        (provider or {}).get('model'),
    ]
    return list(dict.fromkeys(
        str(item).strip() for item in values
        if str(item or '').strip() and str(item).strip() not in disabled
    ))


def _enabled_providers() -> list:
    return [item for item in public_config().get('providers', []) if item.get('enabled')]


def available() -> bool:
    service = get_service()
    if service is None:
        return False
    if hasattr(service, 'available'):
        return bool(service.available())
    config = service.config()
    return bool(config.get('enabled')) and any(
        item.get('enabled') and (item.get('model') or item.get('models'))
        for item in config.get('providers', [])
    )


def status() -> dict:
    """面板与自检用的一句话状态。"""
    service = get_service()
    if service is None:
        return {'installed': False, 'enabled': False, 'ready': False,
                'message': '未安装 AI LLM 模块, 请在模块管理中安装并启用'}
    if not service.config().get('enabled'):
        return {'installed': True, 'enabled': False, 'ready': False,
                'message': '中央 AI LLM 模块未启用'}
    if not available():
        return {'installed': True, 'enabled': True, 'ready': False,
                'message': '中央 AI LLM 没有可用的接口或模型'}
    return {'installed': True, 'enabled': True, 'ready': True,
            'message': '中央 AI LLM 已就绪'}


def resolve_selection(provider_id: str = '', model: str = '') -> tuple:
    """把插件保存的选择校验成中央可用的 (provider_id, model)。

    选择已失效 (接口被删/停用、模型已下架) 时退回空串 = 交给中央自动选择,
    这样面板里的陈旧选择不会把审核整个卡死。
    """
    providers = _enabled_providers()
    provider_id = str(provider_id or '').strip()
    model = str(model or '').strip()
    if provider_id:
        provider = next((item for item in providers if item.get('id') == provider_id), None)
        if provider is None:
            return '', ''
        return str(provider['id']), (model if model in set(provider_models(provider)) else '')
    if model:
        provider = next((item for item in providers if model in set(provider_models(item))), None)
        return ('', model) if provider else ('', '')
    return '', ''


async def refresh_models(provider_id: str = '') -> dict:
    """让中央模块重新同步接口的模型目录 (面板「获取模型」按钮)。"""
    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    errors = {}
    refreshed = []
    for item in service.config().get('providers', []):
        if not item.get('enabled') or (provider_id and item.get('id') != provider_id):
            continue
        try:
            await service.fetch_models(item['id'])
            refreshed.append(item['id'])
        except Exception as error:  # noqa: BLE001 — 逐接口回报, 不因一个接口失败中断
            errors[item['id']] = str(error)[:300]
    return {'providers': public_config().get('providers', []),
            'refreshed': refreshed, 'errors': errors}


# ==================== 审核调用 ====================

def _fail(error: str, status_code, kind: str) -> dict:
    return {'error': error, 'status': status_code, 'kind': kind}


def _classify(error: Exception) -> dict:
    """把中央模块抛出的异常归一成 review 展示 / 重试要用的失败信息。

    中央模块在上游返回非 200 时抛 ``AIProviderError('HTTP 502: ...')``, 状态码就在
    消息里, 正则捞出来即可, 群消息与留档继续显示 HTTP 状态码 (1.4.2 起的行为)。
    捞不到就退回异常类型名。
    """
    text = str(error)
    match = _HTTP_RE.search(text)
    if match:
        return _fail(text[:300], int(match.group(1)), '')
    return _fail(f'{type(error).__name__}: {text}'[:300], None, type(error).__name__)


async def complete(messages: list, system_prompt: str, cfg: dict) -> tuple:
    """跑一次审核判定, 返回 ``(结果 dict 或 None, 失败信息)``。

    结果 dict 取中央 ``complete()`` 的 ``text`` / ``model``。审核是一次性结构化
    判定: 不开中央运行时工具 (``enable_runtime_tools=False``), 也不让中央裁剪
    上下文 (``prepare_context=False``) —— 送审正文必须一字不差地送到模型, 中央
    再压缩一次就等于悄悄漏审。
    """
    service = get_service()
    if service is None:
        info = status()
        return None, _fail(info['message'], None, info['message'])
    if not available():
        info = status()
        return None, _fail(info['message'], None, info['message'])

    provider_id, model = resolve_selection(cfg.get('provider_id'), cfg.get('model'))
    try:
        result = await service.complete(
            messages,
            system_prompt=system_prompt,
            provider_id=provider_id,
            model=model,
            temperature=cfg.get('temperature', 0.2),
            consumer_plugin=CONSUMER,
            enable_runtime_tools=False,
            prepare_context=False,
        )
    except Exception as error:  # noqa: BLE001 — 一律归一为「需人工」, 由上层决定重试
        return None, _classify(error)
    text = str((result or {}).get('text') or '')
    if not text.strip():
        return None, _fail('中央 AI LLM 返回了空回复', None, '空回复')
    return {'text': text,
            'model': str((result or {}).get('model') or ''),
            'provider_id': str((result or {}).get('provider_id') or ''),
            'provider_name': str((result or {}).get('provider_name') or ''),
            'run_id': str((result or {}).get('run_id') or '')}, _fail('', None, '')


def _provider_name(provider_id: str) -> str:
    if not provider_id:
        return ''
    item = next((p for p in _enabled_providers() if p.get('id') == provider_id), None)
    return str((item or {}).get('name') or provider_id)


async def probe(provider_id: str = '', model: str = '') -> dict:
    """面板「测试连接」: 按**指定的**接口与模型跑一句最小请求。

    ``provider_id`` / ``model`` 传面板当前选中的值 (可能还没保存), 两者都留空才
    退回插件已保存的配置。返回里带上**实际应答**的接口与模型 —— 选择留空或已失效
    时中央会自动挑选并故障切换, 不回报就会像「测的不是我选的那个」一样莫名其妙。
    """
    info = status()
    if not info['ready']:
        return {'ok': False, 'error': info['message'], 'http_status': None}

    want_provider = str(provider_id or '').strip()
    want_model = str(model or '').strip()
    if not want_provider and not want_model:
        from . import config as plugin_config
        saved = plugin_config.all_config()
        want_provider = str(saved.get('provider_id') or '')
        want_model = str(saved.get('model') or '')

    use_provider, use_model = resolve_selection(want_provider, want_model)
    # 选了却解析不出来 = 该接口/模型已被中央停用或删除, 必须明说,
    # 否则会静默退回自动选择, 让人以为测的就是自己选的那个
    stale = []
    if want_provider and not use_provider:
        stale.append(f'接口「{want_provider}」')
    if want_model and not use_model:
        stale.append(f'模型「{want_model}」')

    result, fail = await complete([{'role': 'user', 'content': '回复 ok'}], '',
                                  {'provider_id': use_provider, 'model': use_model,
                                   'temperature': 0})
    if result is None:
        return {'ok': False, 'error': fail['error'], 'http_status': fail['status'],
                'stale': stale}
    return {
        'ok': True,
        'model': result['model'],
        'provider_id': result['provider_id'],
        'provider_name': result['provider_name'] or _provider_name(result['provider_id']),
        'reply': result['text'][:100],
        'requested_model': want_model,
        # 有任一项没定死就说明中央参与了自动选择, 面板据此明确提示
        'auto_selected': not use_provider or not use_model,
        'stale': stale,
    }
