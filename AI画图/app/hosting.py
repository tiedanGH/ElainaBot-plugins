"""Image Hosting 模块适配层：把生成的图片转成可嵌入 Markdown 的公网直链。"""
from __future__ import annotations


def _app():
    try:
        from core.application import get_app
    except ImportError:
        return None
    return get_app()


def get_module():
    """返回 Image Hosting 模块实例；模块未启用或初始化失败时返回 None。"""
    app = _app()
    manager = getattr(app, 'module_manager', None) if app else None
    return manager.get('image_hosting') if manager is not None else None


def status() -> dict:
    """返回各图床可用状态，供面板展示。"""
    module = get_module()
    if module is None:
        return {}
    try:
        return dict(module.status() or {})
    except Exception:  # noqa: BLE001 - 状态查询失败按不可用处理
        return {}


def available() -> bool:
    return any(status().values())


def state() -> dict:
    module = get_module()
    if module is None:
        return {
            'installed': False, 'available': False, 'beds': {},
            'message': '未启用 Image Hosting 模块，将退回富媒体图片消息',
        }
    beds = status()
    usable = [name for name, ok in beds.items() if ok]
    return {
        'installed': True,
        'available': bool(usable),
        'beds': beds,
        'message': (
            f"可用图床：{'、'.join(usable)}" if usable
            else 'Image Hosting 已启用但没有可用图床'
        ),
    }


def _bot_context(appid: str) -> tuple:
    """取当前机器人的 sender 与 token_manager，部分图床需要它们。"""
    app = _app()
    getter = getattr(app, 'get_bot', None) if app else None
    if getter is None:
        return None, None
    try:
        bot = getter(str(appid or ''))
    except Exception:  # noqa: BLE001 - 取不到 bot 时交给模块自行选择
        return None, None
    if bot is None:
        return None, None
    return getattr(bot, 'sender', None), getattr(bot, 'token_manager', None)


def _extract_url(result) -> str:
    """把各图床的返回值归一成 URL：直链字符串或 dict 的 file_url / url 键。"""
    if isinstance(result, str) and result.startswith(('http://', 'https://')):
        return result
    if isinstance(result, dict):
        url = result.get('file_url') or result.get('url')
        if isinstance(url, str) and url.startswith(('http://', 'https://')):
            return url
    return ''


async def upload(data: bytes, file_name: str = 'ai-draw.png', *, appid: str = '') -> str:
    """按模块优先级上传图片并返回直链；模块缺失或全部失败时返回空串。"""
    module = get_module()
    if module is None or not data:
        return ''
    sender, token_manager = _bot_context(appid)
    try:
        result = await module.upload_any(
            data, file_name, token_manager=token_manager, sender=sender,
        )
    except Exception:  # noqa: BLE001 - 上传失败时上层退回富媒体消息
        return ''
    return _extract_url(result)
