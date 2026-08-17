"""「LGTBot 问答」面板 API。

沿用框架默认的 Cookie 鉴权（register_route 的 auth 默认 True），不额外开放免验证
路由。返回的配置里没有任何密钥 —— 本插件本来就不存密钥。
"""
from __future__ import annotations

import asyncio

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from . import central, config, conflict, drawing, games, hosting, ratelimit, sandbox, store

PREFIX = '/api/ext/lgtbot-qa'
_registered = False
log = get_logger(PLUGIN, 'LGTBot游戏问答')


def _guarded(handler):
    """把未预期的异常收成 JSON 错误，而不是让 aiohttp 抛出裸 500。

    面板前端只认 {'success': False, 'error': ...}，遇到 500 只会弹一句
    「HTTP 500」，看不出到底哪儿坏了 —— 实测「重建索引」按钮就是这样，
    真实原因（传参写错）全被 500 吞掉了。
    """
    async def wrapper(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as error:  # noqa: BLE001 — 面板需要看到原因，不能静默
            log.exception(f'面板接口 {request.method} {request.path} 失败')
            return web.json_response(
                {'success': False, 'error': f'{type(error).__name__}: {error}'},
                status=500,
            )

    wrapper.__name__ = getattr(handler, '__name__', 'wrapper')
    return wrapper


def register_routes() -> None:
    global _registered
    if _registered:
        return
    register_route('GET', f'{PREFIX}/config')(_guarded(_get_config))
    register_route('PUT', f'{PREFIX}/config')(_guarded(_save_config))
    register_route('GET', f'{PREFIX}/stats')(_guarded(_stats))
    register_route('GET', f'{PREFIX}/games')(_guarded(_games))
    register_route('POST', f'{PREFIX}/games/refresh')(_guarded(_refresh_games))
    register_route('POST', f'{PREFIX}/probe')(_guarded(_probe))
    register_route('DELETE', f'{PREFIX}/context')(_guarded(_clear_context))
    _registered = True


async def _body(request: web.Request) -> dict:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _decorate(current: dict) -> dict:
    value = dict(current)
    value['source'] = sandbox.status(current)
    value['shared_ai'] = central.public_config()
    value['shared_ai_status'] = central.status()
    value['shared_ai_available'] = central.available()
    value['binding_warning'] = conflict.binding_warning(current)
    value['draw_capability'] = drawing.state()
    value['hosting'] = hosting.state()
    return value


async def _get_config(_request: web.Request) -> web.Response:
    return web.json_response({'success': True, 'data': _decorate(config.load())})


async def _save_config(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        value = await asyncio.to_thread(config.save, body)
        # 中央面板可能已经停用了这里选中的接口/模型，存完立刻校正回真实可用的组合
        provider_id, model = central.resolve_selection(
            str(value.get('provider_id') or ''), str(value.get('model_preference') or ''),
        )
        if value.get('provider_id') != provider_id or value.get('model_preference') != model:
            value = await asyncio.to_thread(
                config.save, {'provider_id': provider_id, 'model_preference': model},
            )
        games.invalidate()
        return web.json_response({'success': True, 'data': _decorate(value)})
    except (TypeError, ValueError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _stats(_request: web.Request) -> web.Response:
    data = await asyncio.to_thread(store.stats)
    current = config.load()
    catalog = await asyncio.to_thread(games.search, current, '')
    provider_id, model = central.resolve_selection(
        str(current.get('provider_id') or ''), str(current.get('model_preference') or ''),
    )
    shared = central.public_config()
    provider = next(
        (item for item in shared.get('providers', []) if item.get('id') == provider_id), None,
    )
    data.update({
        'active_now': ratelimit.active(),
        'games': len(catalog),
        'provider': provider['name'] if provider else '自动选择',
        'model': model or '自动选择',
        'roots': len(sandbox.roots(current)),
    })
    return web.json_response({'success': True, 'data': data})


async def _games(request: web.Request) -> web.Response:
    keyword = str(request.query.get('keyword') or '')
    items = await asyncio.to_thread(games.search, config.load(), keyword)
    return web.json_response({'success': True, 'data': items})


async def _refresh_games(_request: web.Request) -> web.Response:
    current = config.load()
    # index() 的 refresh 是**仅关键字参数**，按位置传会 TypeError → 面板 500。
    await asyncio.to_thread(games.index, current, refresh=True)
    items = await asyncio.to_thread(games.search, current, '')
    return web.json_response({'success': True, 'data': {'count': len(items)}})


async def _probe(request: web.Request) -> web.Response:
    """面板上直接试一条检索，确认沙箱边界和源码目录都对。"""
    body = await _body(request)
    from . import tools

    current = config.load()
    name = str(body.get('tool') or 'list_games')
    if name not in tools.TOOL_NAMES:
        return web.json_response({'success': False, 'error': f'未知工具: {name}'}, status=400)
    arguments = body.get('arguments')
    result = await tools.run(name, arguments if isinstance(arguments, dict) else {}, current)
    return web.json_response({'success': True, 'data': result})


async def _clear_context(request: web.Request) -> web.Response:
    body = await _body(request)
    scope = str(body.get('scope') or '').strip()
    if not scope:
        current = config.load()
        deleted = await asyncio.to_thread(
            store.prune_expired, int(current.get('context_expire_seconds') or 3600),
        )
    else:
        deleted = await asyncio.to_thread(store.clear, scope)
    return web.json_response({'success': True, 'data': {'deleted': deleted}})
