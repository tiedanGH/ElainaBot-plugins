"""中央 AI LLM 适配层：接口目录、提示词优化、内容审核与生图调用。"""
from __future__ import annotations

import base64
import binascii
import json
import re

import aiohttp

from core.base.logger import PLUGIN, get_logger

from . import config as draw_config

log = get_logger(PLUGIN, 'AI画图')
CONSUMER = 'ai_draw'
_MODERATION_SAFE = '安全'
_MODERATION_BLOCKED = '内容违规，已禁止发送'
# Gemini 这类多模态模型不在 /v1/images/generations 上，得走对话端口，
# 图片混在回复正文里返回。中央模块只把 message.content 当文本交回来，
# 所以这里只能从文本里把图片捞出来：data URI 或 Markdown/裸链接。
_DATA_URI = re.compile(r'data:image/([A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]{32,})')
_MARKDOWN_IMAGE = re.compile(r'!\[[^\]]*\]\(\s*(\S+?)\s*\)')
_BARE_URL = re.compile(r'https?://\S+\.(?:png|jpe?g|webp|gif)(?:\?\S*)?', re.IGNORECASE)
_BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+')
_CHAT_IMAGE_MAX_TOKENS = 65536
# 中转明确回「这个模型不在 images 端点上」时，不必等管理员去面板改开关，直接改走对话端点。
_IMAGES_ENDPOINT_REJECTED = re.compile(
    r'(?:not\s+supported|unsupported|does\s+not\s+support|不支持)[^\n]{0,120}?images?/(?:generations|edits)'
    r'|images?/(?:generations|edits)[^\n]{0,120}?(?:not\s+supported|unsupported|不支持)',
    re.IGNORECASE,
)


def _raw_service():
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
    """每次调用时重新获取，保证 AI LLM 重载后插件仍能拿到当前实例。"""
    return _raw_service()


def available() -> bool:
    service = get_service()
    if service is None:
        return False
    if hasattr(service, 'available'):
        return bool(service.available())
    config = service.config()
    return bool(config.get('enabled')) and any(
        item.get('enabled') and item.get('base_url') for item in config.get('providers', [])
    )


def status() -> dict:
    service = get_service()
    if service is None:
        return {
            'installed': False, 'enabled': False,
            'message': '请前往插件市场下载并启用 AI LLM 模块',
        }
    if not service.config().get('enabled'):
        return {'installed': True, 'enabled': False, 'message': '中央 AI LLM 未启用'}
    if not available():
        return {'installed': True, 'enabled': True, 'message': '中央 AI LLM 没有可用接口或模型'}
    return {'installed': True, 'enabled': True, 'message': '中央 AI LLM 已就绪'}


def public_config() -> dict:
    service = get_service()
    return service.config(public=True) if service is not None else {}


def _provider_models(provider: dict) -> list[str]:
    """返回接口当前可用的模型目录，按配置的优先级排序。"""
    disabled = {str(item) for item in provider.get('disabled_models', [])}
    values = [
        *(provider.get('model_priority') or []),
        *(provider.get('models') or []),
        provider.get('model'),
    ]
    return list(dict.fromkeys(
        str(item).strip() for item in values
        if str(item or '').strip() and str(item).strip() not in disabled
    ))


async def refresh_models(provider_id: str = '') -> dict:
    """通过中央服务刷新已启用接口的模型目录。"""
    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    providers = [
        item for item in service.config().get('providers', [])
        if item.get('enabled') and (not provider_id or item.get('id') == provider_id)
    ]
    if provider_id and not providers:
        raise ValueError('所选接口不存在或未启用')
    refreshed = {}
    errors = {}
    for provider in providers:
        target = str(provider.get('id') or '')
        try:
            refreshed[target] = await service.fetch_models(target)
        except Exception as error:  # noqa: BLE001 - 逐接口返回错误给面板
            errors[target] = str(error)[:300]
    return {
        'providers': service.config(public=True).get('providers', []),
        'refreshed': refreshed,
        'errors': errors,
    }


def resolve_selection(provider_id: str = '', model: str = '') -> tuple[str, str]:
    """把面板保存的接口与模型收敛为中央服务当前仍然有效的组合。"""
    providers = [item for item in public_config().get('providers', []) if item.get('enabled')]
    if provider_id:
        provider = next((item for item in providers if item.get('id') == provider_id), None)
        if provider is None:
            return '', ''
        return str(provider['id']), model if model in set(_provider_models(provider)) else ''
    if model:
        provider = next((item for item in providers if model in set(_provider_models(item))), None)
        return ('', model) if provider else ('', '')
    return '', ''


def valid_routes(config: dict) -> list[dict]:
    """过滤出接口仍然启用、模型仍在目录中的生图线路。"""
    providers = {
        str(item.get('id')): item
        for item in public_config().get('providers', []) if item.get('enabled')
    }
    result = []
    for route in draw_config.enabled_routes(config):
        provider = providers.get(str(route.get('provider_id')))
        if provider is None:
            continue
        if str(route.get('model')) in set(_provider_models(provider)):
            result.append(route)
    return result


def provider_name(provider_id: str) -> str:
    """接口展示名；查不到时回落到 ID，便于在故障日志里指认线路。"""
    target = str(provider_id or '')
    provider = next(
        (item for item in public_config().get('providers', []) if item.get('id') == target),
        None,
    )
    return str((provider or {}).get('name') or target)


async def optimize_prompt(config: dict, text: str) -> str:
    """用中央对话模型把用户描述改写为生图提示词；失败时回落到原文。"""
    value = str(text or '').strip()
    if not value or not config.get('prompt_optimize_enabled'):
        return value
    service = get_service()
    if service is None:
        return value
    provider_id, model = resolve_selection(
        str(config.get('prompt_provider_id') or ''), str(config.get('prompt_model') or ''),
    )
    system_prompt = str(
        config.get('prompt_system') or draw_config.DEFAULT_PROMPT_SYSTEM
    ).strip()
    system_prompt += (
        f"\n\n输出长度不要超过 {int(config.get('prompt_max_length', 1200))} 个字符。"
    )
    try:
        result = await service.complete(
            [{'role': 'user', 'content': json.dumps(
                {'user_request': value}, ensure_ascii=False,
            )}],
            system_prompt=system_prompt,
            provider_id=provider_id,
            model=model,
            temperature=config.get('prompt_temperature', 0.6),
            max_tokens=1024,
            consumer_plugin=CONSUMER,
            enable_runtime_tools=False,
            prepare_context=False,
        )
    except Exception:  # noqa: BLE001 - 优化只是增强，失败时直接使用原始描述
        return value
    rewritten = str(result.get('text') or '').strip().strip('`').strip()
    return rewritten or value


async def moderate(config: dict, text: str) -> dict:
    """用独立的一次模型请求判断画面描述是否违规。"""
    if not config.get('moderation_enabled'):
        return {'available': False, 'flagged': False}
    service = get_service()
    if service is None:
        return {'available': False, 'flagged': False, 'error': status()['message']}
    provider_id, model = resolve_selection(
        str(config.get('prompt_provider_id') or ''), str(config.get('prompt_model') or ''),
    )
    try:
        result = await service.complete(
            [{'role': 'user', 'content': json.dumps(
                {'source': 'image_prompt', 'content': str(text or '')}, ensure_ascii=False,
            )}],
            system_prompt=str(
                config.get('moderation_prompt') or draw_config.DEFAULT_MODERATION_PROMPT
            ).strip(),
            provider_id=provider_id,
            model=model,
            temperature=0,
            max_tokens=24,
            consumer_plugin=f'{CONSUMER}_review',
            enable_runtime_tools=False,
            prepare_context=False,
        )
        raw = str(result.get('text') or '').strip()
        decision = ''.join(raw.split()).strip('`"\'。.!！').replace(',', '，')
        if decision not in {_MODERATION_SAFE, _MODERATION_BLOCKED}:
            raise ValueError('审核模型返回了无效结果')
        return {'available': True, 'flagged': decision == _MODERATION_BLOCKED}
    except Exception as error:  # noqa: BLE001 - 调用方按配置决定失败策略
        return {'available': False, 'flagged': False, 'error': str(error)[:300]}


def build_prompt(config: dict, text: str) -> str:
    """拼接前后缀并截断，得到最终提交给生图接口的提示词。"""
    parts = [
        str(config.get('prompt_prefix') or '').strip(),
        str(text or '').strip(),
        str(config.get('prompt_suffix') or '').strip(),
    ]
    prompt = '\n'.join(item for item in parts if item)
    return prompt[:max(50, int(config.get('prompt_max_length', 1200)))]


def _image_from_text(text: str) -> tuple[bytes, str]:
    """从对话回复正文里捞出图片：data URI 直接解码，链接原样返回。"""
    value = str(text or '')
    match = _DATA_URI.search(value)
    if match:
        try:
            return base64.b64decode(''.join(match.group(2).split()), validate=True), ''
        except (ValueError, binascii.Error):
            pass
    for candidate in (
        *(item for item in _MARKDOWN_IMAGE.findall(value)),
        *(_BARE_URL.findall(value)),
    ):
        target = str(candidate).strip().rstrip(')>,。')
        if target.startswith('data:image/'):
            inner = _DATA_URI.search(target)
            if inner:
                try:
                    return base64.b64decode(''.join(inner.group(2).split()), validate=True), ''
                except (ValueError, binascii.Error):
                    continue
        elif target.startswith(('http://', 'https://')):
            return b'', target
    return b'', ''


def _scrub(text, secret: str = '') -> str:
    """任何要外泄的文本都先过一遍：抹掉密钥本身和 Bearer 头。"""
    value = str(text or '')
    if secret and len(secret) >= 8:
        value = value.replace(secret, '[密钥已隐藏]')
    return _BEARER.sub('Bearer [密钥已隐藏]', value)


def _direct_endpoint(config: dict, provider_id: str) -> dict:
    """取直连所需的接口信息。**调试开关关闭时直接抛错，不碰密钥。**"""
    if not config.get('debug_direct_request'):
        raise RuntimeError(
            '插件直连未开启：该中转把图片放在 message.images 里，中央模块只取 content。'
            '需要在面板「生图线路」页打开「插件直连调试」，插件才会读取接口密钥自行请求'
        )
    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    provider = next(
        (item for item in service.config().get('providers', [])
         if item.get('id') == provider_id and item.get('enabled')),
        None,
    )
    if provider is None:
        raise RuntimeError('接口不存在或未启用')
    api_type = str(provider.get('api_type') or 'openai_compatible')
    if api_type not in {'openai', 'openai_compatible'}:
        raise RuntimeError(f'插件直连只支持 OpenAI 兼容接口，当前接口类型是 {api_type}')
    base_url = str(provider.get('base_url') or '').strip().rstrip('/')
    api_key = str(provider.get('api_key') or '')
    if not base_url or not api_key:
        raise RuntimeError('该接口缺少 Base URL 或 API Key，无法直连')
    path = str(provider.get('chat_path') or '/chat/completions').strip()
    return {
        'endpoint': base_url + '/' + path.lstrip('/'),
        'api_key': api_key,
        'timeout': int(public_config().get('request_timeout') or 120),
    }


def _image_from_message(message: dict) -> tuple[bytes, str]:
    """按各家中转的返回形态依次找图片。"""
    candidates = []
    for item in message.get('images') or []:
        if isinstance(item, dict):
            candidates.append(str((item.get('image_url') or {}).get('url') or item.get('url') or ''))
        elif isinstance(item, str):
            candidates.append(item)
    single = message.get('image')
    if isinstance(single, dict):
        candidates.append(str((single.get('image_url') or {}).get('url') or single.get('url') or ''))
    content = message.get('content')
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get('type') in {'image_url', 'output_image', 'image'}:
                candidates.append(str(
                    (item.get('image_url') or {}).get('url')
                    or item.get('url') or item.get('data') or ''
                ))
    for value in candidates:
        target = value.strip()
        if target.startswith('data:image/'):
            match = _DATA_URI.search(target)
            if match:
                try:
                    return base64.b64decode(''.join(match.group(2).split()), validate=True), ''
                except (ValueError, binascii.Error):
                    continue
        elif target.startswith(('http://', 'https://')):
            return b'', target
    # 最后再看正文里有没有 data URI / Markdown 图片 / 裸链接
    return _image_from_text(content if isinstance(content, str) else '')


async def _direct_chat_image(config: dict, prompt: str, route: dict) -> dict:
    """绕过中央模块直接请求对话端点，能拿到 message.images 里的图片。

    只有面板「插件直连调试」打开时才走到这里。密钥只在本函数内构造请求头，
    不写日志、不进异常文本，所有外抛文本都过 `_scrub()`。
    """
    target = _direct_endpoint(config, route['provider_id'])
    secret = target['api_key']
    payload = {
        'model': route['model'],
        'messages': [{'role': 'user', 'content': prompt}],
        'modalities': ['image', 'text'],
    }
    timeout = aiohttp.ClientTimeout(total=min(300, max(30, target['timeout'])))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                target['endpoint'],
                headers={
                    'Authorization': f'Bearer {secret}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                json=payload,
                allow_redirects=False,
            ) as response:
                raw = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(
                        f'HTTP {response.status}: {_scrub(raw[:600], secret)}'
                    )
    except RuntimeError:
        raise
    except Exception as error:  # noqa: BLE001 - 网络异常也要脱敏后再上抛
        raise RuntimeError('直连请求失败：' + _scrub(error, secret)[:300]) from None
    try:
        message = json.loads(raw)['choices'][0]['message']
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError(
            '直连返回里没有 choices[0].message：' + _scrub(raw[:300], secret)
        ) from None
    message = message if isinstance(message, dict) else {}
    data, url = _image_from_message(message)
    if not data and not url:
        fields = '/'.join(sorted(message)) or '空'
        body = ' '.join(str(message.get('content') or '').split())[:200]
        raise RuntimeError(
            f'直连拿到回复但没有图片，message 字段有：{fields}；'
            f'正文：{_scrub(body, secret)}'
        )
    return {
        'data': data, 'url': url,
        'provider': provider_name(route['provider_id']),
        'provider_id': route['provider_id'],
        'model': route['model'],
    }


async def _chat_image(config: dict, prompt: str, route: dict) -> dict:
    """走对话端口出图（Gemini 等多模态模型），从回复正文里取图片。"""
    service = get_service()
    try:
        result = await service.complete(
            [{'role': 'user', 'content': prompt}],
            provider_id=route['provider_id'],
            model=route['model'],
            max_tokens=_CHAT_IMAGE_MAX_TOKENS,
            consumer_plugin=CONSUMER,
            enable_runtime_tools=False,
            prepare_context=False,
        )
    except Exception as error:
        if '空消息' in str(error):
            raise RuntimeError(
                '该接口把图片放在 message.images 字段里，中央 AI LLM 只取 content，'
                '插件拿不到图片。请在面板「生图线路」页打开「插件直连调试」，'
                '插件才会自行请求接口取到这张图'
            ) from error
        raise
    text = str(result.get('text') or '')
    data, url = _image_from_text(text)
    if not data and not url:
        raise RuntimeError(
            '对话端口没有返回图片，模型只回了文字：'
            + ' '.join(text.split())[:200]
        )
    return {
        'data': data, 'url': url,
        'provider': str(result.get('provider_name') or route['provider_id']),
        'provider_id': str(result.get('provider_id') or route['provider_id']),
        'model': str(result.get('model') or route['model']),
    }


async def _images_endpoint(config: dict, prompt: str, route: dict) -> dict:
    """走标准 /v1/images/generations 出图。"""
    service = get_service()
    if not hasattr(service, 'generate_image'):
        raise RuntimeError('当前 AI LLM 模块版本不支持生图接口')
    result = await service.generate_image(
        prompt,
        candidates=[route],
        size=str(config.get('image_size') or '1024x1024'),
    )
    encoded = str(result.get('b64_json') or '').strip()
    data = b''
    if encoded:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise RuntimeError('生图接口返回的图片数据无效') from error
    url = str(result.get('url') or '').strip()
    if not data and not url.startswith(('http://', 'https://')):
        raise RuntimeError('生图接口没有返回可用的图片')
    return {
        'data': data,
        'url': url if url.startswith(('http://', 'https://')) else '',
        'provider': str(result.get('provider') or ''),
        'provider_id': str(result.get('provider_id') or ''),
        'model': str(result.get('model') or ''),
    }


async def generate(config: dict, prompt: str, *, exclude=()) -> dict:
    """按线路顺序出图；返回 {data, url, provider, provider_id, model}。

    每条线路按自己的 `mode` 走生图端口或对话端口，失败则换下一条并汇总原因。
    `exclude` 传入已经试过的 (provider_id, model)，用于投递失败后换线路重试。
    """
    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    skipped = {tuple(item) for item in (exclude or ())}
    routes = [
        item for item in valid_routes(config)
        if (item['provider_id'], item['model']) not in skipped
    ]
    if not routes:
        raise RuntimeError(
            '其余生图线路都已试过，仍未能把图片送达' if skipped
            else '没有可用的生图线路，请先在面板中配置接口与模型'
        )
    errors = []
    for route in routes:
        label = f"{provider_name(route['provider_id'])}/{route['model']}"
        chat_mode = route.get('mode') == 'chat'
        try:
            if chat_mode:
                return await _chat_via(config, prompt, route)
            return await _images_endpoint(config, prompt, route)
        except Exception as error:  # noqa: BLE001 - 逐条线路汇总，最后统一上抛
            if chat_mode or not _IMAGES_ENDPOINT_REJECTED.search(str(error)):
                errors.append(f'{label}: {error}')
                continue
            # 中转明说这个模型不在生图端点上（Gemini 这类多模态模型），自动改走对话端点。
            log.info('%s 不在生图端点上，自动改走对话端点重试', label)
            try:
                return await _chat_via(config, prompt, route)
            except Exception as retry_error:  # noqa: BLE001 - 两次都失败就一起报出来
                errors.append(
                    f'{label}: 生图端点不支持该模型，自动改走对话端点后仍失败：{retry_error}'
                )
    raise RuntimeError('；'.join(errors)[:1200])


async def _chat_via(config: dict, prompt: str, route: dict) -> dict:
    """对话端点出图：开了直连调试就自己发请求，否则借中央模块的 complete()。"""
    if config.get('debug_direct_request'):
        return await _direct_chat_image(config, prompt, route)
    return await _chat_image(config, prompt, route)
