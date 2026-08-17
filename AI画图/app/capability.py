"""把画图能力注册到中央 AI LLM，供其他插件与模型调用。

审核、限额与留档仍然全部发生在本插件里，调用方只拿到一个图片链接。
返回值刻意只给链接和少量元信息：工具结果会回灌进调用方模型的上下文，
把 base64 塞回去会瞬间撑爆 token。
"""
from __future__ import annotations

import time

from core.base.logger import PLUGIN, get_logger

from . import central, config as draw_config, hosting, limiter, media, store

log = get_logger(PLUGIN, 'AI画图')
SOURCE = 'ai_draw'
TOOL_ID = 'draw'
CAPABILITY_KEY = f'{SOURCE}:tool:{TOOL_ID}'
_registered_service = None

_DEFINITION = {
    'id': TOOL_ID,
    'name': 'AI 画图',
    'description': (
        '根据文字描述生成一张图片，返回图片链接与实测像素宽高（width / height），'
        '并附一段可直接发送的 QQ Markdown 图片语法（markdown 字段）。'
        '描述要写清主体、动作、场景、风格；不要传入已有图片链接。'
        '图片由 AI 画图插件统一做内容审核与留档，失败时返回 ok=false 与原因。'
    ),
    'config': {
        'schema': {
            'type': 'object',
            'properties': {
                'prompt': {
                    'type': 'string',
                    'description': '画面描述，中文英文均可',
                },
                'caller': {
                    'type': 'string',
                    'description': '调用方名称，仅用于留档标注（自报，不做鉴权）',
                },
            },
            'required': ['prompt'],
            'additionalProperties': False,
        },
    },
}


def _fail(reason: str, record: dict | None = None, current: dict | None = None) -> dict:
    """统一失败返回；需要留档时顺带写一条记录。"""
    if record is not None and current is not None:
        record['error'] = reason[:1000]
        try:
            store.add(record, current['history_limit'])
        except Exception:  # noqa: BLE001 - 留档失败不该影响返回
            log.exception('画图能力留档失败')
    return {'ok': False, 'error': reason}


async def _run(arguments: dict) -> dict:
    current = draw_config.load()
    if not current['capability_enabled']:
        return {'ok': False, 'error': 'AI 画图未对外开放画图能力'}
    prompt = str((arguments or {}).get('prompt') or '').strip()
    caller = str((arguments or {}).get('caller') or '')[:60]
    if not prompt:
        return {'ok': False, 'error': '缺少画面描述 prompt'}
    if len(prompt) > current['input_max_length']:
        return {'ok': False, 'error': f"画面描述不能超过 {current['input_max_length']} 字"}

    record = {
        'created_at': time.time(), 'source': 'capability', 'status': 'failed',
        'user_id': 'capability', 'username': caller or '其他插件', 'chat_type': 'capability',
        'prompt': prompt, 'send_mode': 'capability',
    }
    blocked = next((
        word for word in current['blocked_words']
        if word and word.casefold() in prompt.casefold()
    ), '')
    if blocked:
        record['status'] = 'blocked'
        log.warning('画图能力调用命中违规词 caller=%s 违规词=%s', caller or '未知', blocked)
        return _fail(f'描述命中违规词：{blocked}', record, current)

    day = limiter.day_start()
    quota = int(current['capability_daily_limit'])
    if quota > 0 and store.count_since(day, 'capability') >= quota:
        return {'ok': False, 'error': f'画图能力今日调用已达上限（{quota} 次）'}
    total = int(current['global_daily_limit'])
    if total > 0 and store.count_since(day) >= total:
        return {'ok': False, 'error': '今日全局画图额度已用完'}

    started = time.perf_counter()
    review = await central.moderate(current, prompt)
    if review.get('flagged') or (
        not review.get('available')
        and current['moderation_enabled'] and current['moderation_fail_closed']
    ):
        record.update({
            'status': 'blocked',
            'duration_ms': round((time.perf_counter() - started) * 1000),
        })
        log.warning('画图能力调用未通过内容审核 caller=%s', caller or '未知')
        return _fail(review.get('error') or '描述未通过内容审核', record, current)

    log.info('画图能力被调用 caller=%s 描述=%s', caller or '未知', prompt[:60])
    try:
        optimized = await central.optimize_prompt(current, prompt)
        final_prompt = central.build_prompt(current, optimized)
        record['final_prompt'] = final_prompt
        async with limiter.semaphore(current['max_concurrency']):
            result = await central.generate(current, final_prompt)
    except Exception as error:  # noqa: BLE001 - 失败原因原样回给调用方
        record['duration_ms'] = round((time.perf_counter() - started) * 1000)
        log.warning('画图能力生图失败 caller=%s: %s', caller or '未知', error)
        return _fail(str(error)[:500], record, current)

    record.update({
        'status': 'success',
        'duration_ms': round((time.perf_counter() - started) * 1000),
        'provider': result['provider'], 'provider_id': result['provider_id'],
        'model': result['model'], 'size': current['image_size'],
        'image_url': result['url'],
    })
    image = result['data'] or (await media.download(result['url']) or b'')
    # 工具只回链接：优先图床直链，其次接口原始链接。绝不回 base64。
    url = ''
    if image:
        url = await hosting.upload(
            image, f'ai-draw-{int(time.time() * 1000)}.{media.sniff(image)[0]}',
        )
    record['hosted_url'] = url
    delivered_url = url or result['url']
    if not delivered_url:
        return _fail(
            '生成成功但拿不到图片链接：图床不可用且接口只返回了图片数据。'
            '请在 Image Hosting 模块中配置可用图床',
            record, current,
        )
    record['delivered'] = 1
    try:
        record_id = store.add(record, current['history_limit'])
        if image and current['history_save_images']:
            store.save_image(
                record_id, image, media.sniff(image)[0], current['history_image_limit'],
            )
    except Exception:  # noqa: BLE001 - 留档失败不影响把图给调用方
        log.exception('画图能力留档失败')
        record_id = 0
    # 实测像素尺寸：QQ 的 Markdown 图片必须写 #宽px #高px，调用方直接拿去用。
    # 只拿到链接、下不到字节时测不出来，这几个键就不返回，免得 0 被当成真实值。
    width, height = media.dimensions(image) if image else (0, 0)
    payload = {
        'ok': True,
        'url': delivered_url,
        'model': result['model'],
        'provider': result['provider'],
        'requested_size': current['image_size'],
        'record_id': record_id,
    }
    if width > 0 and height > 0:
        payload.update({
            'width': width,
            'height': height,
            # 直接可用的 QQ Markdown 图片语法
            'markdown': f"![{current['markdown_alt']} #{width}px #{height}px]({delivered_url})",
        })
    log.info('画图能力出图成功 caller=%s 线路=%s/%s 尺寸=%s',
             caller or '未知', result['provider'], result['model'],
             f'{width}x{height}' if width else '未知')
    return payload


async def handle(capability_id: str, arguments: dict) -> dict:
    """中央模块的能力入口；任何异常都收敛成 ok=false，不往模型里抛栈。"""
    if str(capability_id or '') != TOOL_ID:
        return {'ok': False, 'error': '未知能力'}
    try:
        return await _run(arguments if isinstance(arguments, dict) else {})
    except Exception as error:  # noqa: BLE001 - 工具结果要始终可序列化
        log.exception('画图能力执行异常')
        return {'ok': False, 'error': f'画图能力执行异常：{error}'[:300]}


def register() -> bool:
    """把画图工具注册到中央模块；共享范围由 AI LLM 面板控制。"""
    global _registered_service
    service = central.get_service()
    if service is None or not hasattr(service, 'register_plugin_capability'):
        return False
    try:
        service.register_plugin_capability(SOURCE, 'tool', dict(_DEFINITION), handle)
    except Exception:  # noqa: BLE001 - 注册失败不影响插件其余功能
        log.exception('注册画图能力失败')
        return False
    if service is not _registered_service:
        log.info('画图能力已注册到中央 AI LLM：%s', CAPABILITY_KEY)
    _registered_service = service
    return True


def unregister() -> None:
    global _registered_service
    service = _registered_service or central.get_service()
    if service is not None and hasattr(service, 'unregister_plugin_capabilities'):
        try:
            service.unregister_plugin_capabilities(SOURCE)
        except Exception:  # noqa: BLE001 - 卸载阶段不再抛错
            log.exception('注销画图能力失败')
    _registered_service = None


def sync(enabled: bool) -> bool:
    """按开关注册或下线；中央模块重载后由看护任务重新注册。"""
    if enabled:
        return register()
    if _registered_service is not None:
        unregister()
    return False


def state() -> dict:
    service = central.get_service()
    online = False
    shared = False
    if service is not None and hasattr(service, 'plugin_capabilities'):
        try:
            item = next((
                value for value in service.plugin_capabilities()
                if value.get('key') == CAPABILITY_KEY
            ), None)
        except Exception:  # noqa: BLE001 - 状态查询失败按未注册处理
            item = None
        if item is not None:
            online = bool(item.get('online'))
            shared = bool(item.get('shared')) and bool(item.get('enabled'))
    return {'key': CAPABILITY_KEY, 'online': online, 'shared': shared}
