"""AI 画图 Web 面板 API：配置、统计、历史记录与图片读取。"""
from __future__ import annotations

import asyncio
import base64
import os
import time

from aiohttp import web

from core.plugin.web_pages import register_route

from . import central, config, hosting, limiter, media, store

PREFIX = '/api/ext/ai-draw'
_INLINE_PREVIEW_BYTES = 8 * 1024 * 1024
_registered = False


def register_routes() -> None:
    global _registered
    if _registered:
        return
    register_route('GET', f'{PREFIX}/config')(_get_config)
    register_route('PUT', f'{PREFIX}/config')(_save_config)
    register_route('GET', f'{PREFIX}/stats')(_stats)
    register_route('POST', f'{PREFIX}/models/refresh')(_refresh_models)
    register_route('GET', f'{PREFIX}/records')(_records)
    register_route('GET', f'{PREFIX}/image')(_image)
    register_route('DELETE', f'{PREFIX}/records')(_delete_records)
    register_route('POST', f'{PREFIX}/test')(_test_draw)
    _registered = True


async def _body(request: web.Request) -> dict:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001 - 非 JSON 请求按空载荷处理
        return {}


def _with_central(value: dict) -> dict:
    value['shared_ai_available'] = central.available()
    value['shared_ai_status'] = central.status()
    value['shared_ai'] = central.public_config()
    value['valid_routes'] = len(central.valid_routes(value))
    value['image_sizes'] = list(config.IMAGE_SIZES)
    value['hosting'] = hosting.state()
    return value


async def _get_config(_request: web.Request) -> web.Response:
    return web.json_response({'success': True, 'data': _with_central(config.load())})


async def _save_config(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        value = await asyncio.to_thread(config.save, body)
        requested = str(value.get('prompt_provider_id') or '')
        resolved, model = central.resolve_selection(
            requested, str(value.get('prompt_model') or ''),
        )
        provider_id = requested if not requested or resolved == requested else ''
        if value.get('prompt_provider_id') != provider_id or value.get('prompt_model') != model:
            value = await asyncio.to_thread(config.save, {
                'prompt_provider_id': provider_id, 'prompt_model': model,
            })
        return web.json_response({'success': True, 'data': _with_central(value)})
    except (TypeError, ValueError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=400)


async def _stats(_request: web.Request) -> web.Response:
    data = await asyncio.to_thread(store.stats, limiter.day_start())
    current = config.load()
    data.update({
        'routes': len(config.enabled_routes(current)),
        'valid_routes': len(central.valid_routes(current)),
        'image_size': current['image_size'],
        'enabled': current['enabled'],
        'shared_ai_status': central.status(),
        'hosting': hosting.state(),
    })
    return web.json_response({'success': True, 'data': data})


async def _refresh_models(request: web.Request) -> web.Response:
    body = await _body(request)
    try:
        result = await central.refresh_models(str(body.get('provider_id') or ''))
        return web.json_response({'success': True, 'data': result})
    except (RuntimeError, ValueError, OSError) as error:
        return web.json_response({'success': False, 'error': str(error)}, status=502)


async def _records(request: web.Request) -> web.Response:
    query = request.query
    try:
        limit = int(query.get('limit') or 30)
        offset = int(query.get('offset') or 0)
    except ValueError:
        limit, offset = 30, 0
    result = await asyncio.to_thread(
        store.query,
        status=str(query.get('status') or '').strip(),
        keyword=str(query.get('keyword') or '')[:100],
        user_id=str(query.get('user_id') or '')[:80],
        chat_id=str(query.get('chat_id') or '')[:120],
        with_image=str(query.get('with_image') or '') in {'1', 'true'},
        limit=limit,
        offset=offset,
    )
    for item in result['items']:
        item['has_image'] = bool(item.get('image_file'))
    return web.json_response({'success': True, 'data': result})


async def _image(request: web.Request) -> web.StreamResponse:
    try:
        record_id = int(request.query.get('id') or 0)
    except ValueError as error:
        raise web.HTTPBadRequest(text='无效的记录 ID') from error
    record = await asyncio.to_thread(store.get, record_id)
    if record is None:
        raise web.HTTPNotFound()
    path = store.image_path(str(record.get('image_file') or ''))
    if not path or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={
        'Cache-Control': 'private, max-age=600',
        'Content-Type': media.mime_for(str(record['image_file'])),
        'Content-Disposition': f'inline; filename="ai-draw-{record_id}"',
    })


async def _delete_records(request: web.Request) -> web.Response:
    body = await _body(request)
    if body.get('all'):
        deleted = await asyncio.to_thread(store.clear)
        return web.json_response({'success': True, 'data': {'deleted': deleted}})
    try:
        record_id = int(body.get('id') or 0)
    except (TypeError, ValueError):
        record_id = 0
    if record_id <= 0:
        return web.json_response({'success': False, 'error': '缺少记录 ID'}, status=400)
    removed = await asyncio.to_thread(store.delete, record_id)
    return web.json_response({'success': True, 'data': {'deleted': 1 if removed else 0}})


async def _test_draw(request: web.Request) -> web.Response:
    """在面板中试跑一次生图，结果同样写入历史记录。"""
    body = await _body(request)
    current = config.load()
    prompt = str(body.get('prompt') or '').strip()[:current['input_max_length']]
    if not prompt:
        return web.json_response({'success': False, 'error': '请填写画面描述'}, status=400)
    record = {
        'created_at': time.time(), 'source': 'panel', 'status': 'failed',
        'user_id': 'panel', 'username': 'Web 面板', 'chat_type': 'panel',
        'prompt': prompt,
    }
    started = time.perf_counter()
    try:
        optimized = await central.optimize_prompt(current, prompt)
        final_prompt = central.build_prompt(current, optimized)
        record['final_prompt'] = final_prompt
        async with limiter.semaphore(current['max_concurrency']):
            result = await central.generate(current, final_prompt)
    except Exception as error:  # noqa: BLE001 - 面板需要看到真实失败原因
        record.update({
            'error': str(error)[:1000],
            'duration_ms': round((time.perf_counter() - started) * 1000),
        })
        await asyncio.to_thread(store.add, record, current['history_limit'])
        return web.json_response({'success': False, 'error': str(error)[:500]}, status=502)
    record.update({
        'status': 'success',
        'duration_ms': round((time.perf_counter() - started) * 1000),
        'provider': result['provider'], 'provider_id': result['provider_id'],
        'model': result['model'], 'size': current['image_size'],
        'image_url': result['url'], 'delivered': 1, 'send_mode': 'panel',
    })
    # 试跑只在面板里看图，不上传图床：预览直接读本地留存，省掉一次图床额度。
    image = result['data'] or (await media.download(result['url']) or b'')
    record_id = await asyncio.to_thread(store.add, record, current['history_limit'])
    stored = False
    if image and current.get('history_save_images'):
        stored = bool(await asyncio.to_thread(
            store.save_image, record_id, image, media.sniff(image)[0],
            current['history_image_limit'],
        ))
    width, height = media.dimensions(image, (1024, 1024)) if image else (0, 0)
    # 没落盘（关了历史留图）时把图片内联回面板，预览不依赖图床也不依赖存储设置。
    preview = ''
    if image and not stored and len(image) <= _INLINE_PREVIEW_BYTES:
        preview = f'data:{media.sniff(image)[1]};base64,{base64.b64encode(image).decode()}'
    return web.json_response({'success': True, 'data': {
        'id': record_id, 'final_prompt': record['final_prompt'],
        'provider': record['provider'], 'model': record['model'],
        'duration_ms': record['duration_ms'], 'image_url': record['image_url'],
        'width': width, 'height': height, 'preview': preview,
        'has_image': stored,
    }})
