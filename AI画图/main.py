"""AI 画图：任何人可用的「画图 XXX」指令，走中央 AI LLM 生图并留存历史记录。"""
from __future__ import annotations

import asyncio
import os
import time

from core.base.config import cfg
from core.base.logger import PLUGIN, get_logger, report_error
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import central, config, hosting, limiter, media, store, webpanel

__plugin_meta__ = {
    'name': 'AI 画图',
    'author': '铁蛋',
    'description': '所有人可用的「画图 XXX」指令，调用中央 AI LLM 生图，并提供配置与历史记录面板',
    'version': '1.0.1',
    'license': 'MIT',
}

log = get_logger(PLUGIN, 'AI画图')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
PAGE_KEY = 'ai-draw'
MESSAGE_EVENTS = [
    'GROUP_AT_MESSAGE_CREATE',
    'GROUP_MESSAGE_CREATE',
    'C2C_MESSAGE_CREATE',
    'DIRECT_MESSAGE_CREATE',
    'AT_MESSAGE_CREATE',
    'MESSAGE_CREATE',
]
MAX_INPUT_LENGTH = 1000

_ICON = (
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="2"/>'
    '<circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>'
)


@on_load
async def initialize() -> None:
    await asyncio.to_thread(config.init, DATA_DIR)
    await asyncio.to_thread(store.connect, DATA_DIR)
    limiter.reset()
    webpanel.register_routes()
    register_page(
        key=PAGE_KEY,
        label='AI 画图',
        source='plugin',
        source_name='AI画图',
        icon=_ICON,
        html_file=os.path.join(BASE_DIR, 'panel.html'),
    )
    log.info('AI 画图插件已加载')


@on_unload
async def cleanup() -> None:
    unregister_page(PAGE_KEY)
    limiter.reset()
    await asyncio.to_thread(store.close)


def _is_owner(event) -> bool:
    """读取机器人配置判断主人身份；配置不可用时按普通用户处理。"""
    try:
        bot_config = cfg.get_bot_config(str(getattr(event, 'appid', '') or ''))
    except Exception:  # noqa: BLE001 - 配置异常不应中断画图请求
        return False
    owner_ids = {
        str(item) for item in (bot_config.get('owner_ids') or [])
    } if isinstance(bot_config, dict) else set()
    return bool(owner_ids) and str(event.user_id) in owner_ids


def _scene_enabled(event, current: dict) -> bool:
    if getattr(event, 'is_group', False):
        return bool(current['group_enabled'])
    if getattr(event, 'is_direct', False):
        return bool(current['direct_enabled'])
    if getattr(event, 'is_channel', False):
        return bool(current['channel_enabled'])
    return False


def _mention(event, current: dict) -> str:
    """群聊与频道里生成 @发起人；私聊没有提及语义，返回空串。"""
    if not current.get('mention_user'):
        return ''
    if getattr(event, 'is_group', False) or getattr(event, 'is_channel', False):
        return f'<@{event.user_id}>'
    return ''


async def _reply_text(event, text: str, current: dict | None = None) -> None:
    mention = _mention(event, current if current is not None else config.load())
    await event.reply(f'{mention} {text}'.strip() if mention else text)


def _find_blocked(text: str, words: list[str]) -> str:
    value = str(text or '').casefold()
    return next((word for word in words if word and word.casefold() in value), '')


def _fill(template: str, **values) -> str:
    """按占位符渲染面板文案；模板写错时原样返回，不让回复流程失败。"""
    try:
        return str(template).format(**values)
    except (KeyError, IndexError, ValueError):
        return str(template)


def _caption(current: dict, event, prompt: str, result: dict) -> str:
    """渲染图片说明正文，不含 @提及。"""
    template = str(current.get('caption_template') or '').strip()
    if not template:
        return ''
    return _fill(
        template,
        prompt=prompt,
        model=result.get('model', ''),
        provider=result.get('provider', ''),
        size=current.get('image_size', ''),
        user=str(getattr(event, 'username', '') or event.user_id),
    )[:300]


def _media_content(event, current: dict, caption: str) -> str:
    """富媒体消息的附带文字；QQ 不会在 msg_type=7 中解析 <@openid>。"""
    mention = _mention(event, current)
    return f'{mention} {caption}'.strip() if mention else caption


def _markdown_text(event, current: dict, caption: str, url: str, size: tuple[int, int]) -> str:
    """@发起人开头的 Markdown 图片消息。"""
    width, height = size
    head = ' '.join(item for item in (_mention(event, current), caption) if item)
    image = f"![{current['markdown_alt']} #{width}px #{height}px]({url})"
    return f'{head}\n{image}' if head else image


def _delivered(response) -> bool:
    """平台失败时也会返回响应体，只有带消息 ID 才算真正发出去。"""
    return isinstance(response, dict) and bool(response.get('id'))


def _base_record(event, prompt: str) -> dict:
    return {
        'created_at': time.time(),
        'appid': str(getattr(event, 'appid', '') or ''),
        'user_id': str(event.user_id),
        'username': str(getattr(event, 'username', '') or ''),
        'chat_type': str(getattr(event, 'chat_type', '') or ''),
        'chat_id': str(getattr(event, 'chat_id', '') or ''),
        'source': 'command',
        'status': 'failed',
        'prompt': prompt,
    }


async def _persist(record: dict, current: dict, image: bytes = b'') -> int:
    record_id = await asyncio.to_thread(store.add, record, current['history_limit'])
    if image and current.get('history_save_images'):
        extension = media.sniff(image)[0]
        await asyncio.to_thread(
            store.save_image, record_id, image, extension, current['history_image_limit'],
        )
    return record_id


async def _image_bytes(result: dict) -> bytes:
    """拿到图片字节：接口直接返回则用它，否则下载接口给的公网 URL。"""
    if result.get('data'):
        return result['data']
    return await media.download(result.get('url', '')) or b''


async def _deliver(event, current: dict, caption: str, result: dict, image: bytes) -> dict:
    """优先图床直链 + Markdown 图片消息，失败时退回富媒体图片消息。"""
    outcome = {'send_mode': '', 'hosted_url': '', 'delivered': False, 'error': ''}
    if current['markdown_send'] and image:
        extension = media.sniff(image)[0]
        url = await hosting.upload(
            image,
            f'ai-draw-{int(time.time() * 1000)}.{extension}',
            appid=str(getattr(event, 'appid', '') or ''),
        )
        outcome['hosted_url'] = url
        if url:
            text = _markdown_text(
                event, current, caption, url, media.dimensions(image, (1024, 1024)),
            )
            response = await event.reply(
                text,
                msg_type=2,
                skip_suffix=True,
                force_verify_image_resource=bool(current['force_verify_image']),
            )
            outcome['send_mode'] = 'markdown'
            outcome['delivered'] = _delivered(response)
            if outcome['delivered']:
                return outcome
            outcome['error'] = 'Markdown 图片消息发送失败，请确认图床域名已在 QQ 开放平台报备'
        else:
            outcome['error'] = hosting.state()['message']
        if not current['media_fallback']:
            return outcome

    payload = image or result.get('url', '')
    if not payload:
        outcome['error'] = outcome['error'] or '没有可发送的图片内容'
        return outcome
    response = await event.reply_image(payload, _media_content(event, current, caption))
    outcome['send_mode'] = 'media'
    outcome['delivered'] = _delivered(response)
    if not outcome['delivered']:
        outcome['error'] = outcome['error'] or '图片发送失败，请检查图片格式或平台限制'
    else:
        outcome['error'] = ''
    return outcome


@handler(
    r'^/?(?:画图|绘图|AI画图|ai画图)\s*$',
    name='AI 画图帮助',
    desc='查看 AI 画图用法',
    priority=40,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def draw_help(event, _match) -> None:
    if not config.initialized():
        return
    current = config.load()
    if not current['enabled']:
        return
    routes = central.valid_routes(current)
    await _reply_text(
        event,
        '【AI 画图】\n'
        '发送「画图 <画面描述>」即可生成图片\n'
        '例：画图 雪山下的湖泊，黄昏，写实摄影\n'
        f"当前尺寸：{current['image_size']}\n"
        f"可用线路：{len(routes)} 条\n"
        f"提示词优化：{'已开启' if current['prompt_optimize_enabled'] else '已关闭'}\n"
        f"发送方式：{'Markdown 图片' if current['markdown_send'] and hosting.available() else '富媒体图片'}\n"
        f"{central.status()['message']}",
        current,
    )


@handler(
    r'^/?(?:画图|绘图|AI画图|ai画图)\s*(\S[\s\S]*)$',
    name='AI 画图',
    desc='画图 <描述>：调用中央 AI LLM 生成图片',
    priority=30,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def draw_command(event, match) -> None:
    if not config.initialized():
        return
    current = config.load()
    if not current['enabled'] or not _scene_enabled(event, current):
        return
    if getattr(event, 'is_bot', False):
        return
    prompt = str(match.group(1) or '').strip()
    if not prompt:
        return
    if len(prompt) > MAX_INPUT_LENGTH:
        await _reply_text(event, f'画面描述太长了，请控制在 {MAX_INPUT_LENGTH} 字以内。', current)
        return

    record = _base_record(event, prompt)
    blocked = _find_blocked(prompt, current['blocked_words'])
    if blocked:
        record.update({'status': 'blocked', 'error': f'命中违规词：{blocked}'})
        await _persist(record, current)
        await _reply_text(event, current['blocked_response'], current)
        return

    reason = await asyncio.to_thread(
        limiter.check, current,
        appid=record['appid'], user_id=record['user_id'],
        chat_id=record['chat_id'], is_owner=_is_owner(event),
    )
    if reason:
        # 限流只是拦下重复请求，不构成一次画图，不写入运行日志。
        await _reply_text(event, _fill(current['limited_response'], detail=reason), current)
        return

    if not central.available():
        limiter.release(record['appid'], record['user_id'], record['chat_id'])
        record.update({'status': 'failed', 'error': central.status()['message']})
        await _persist(record, current)
        await _reply_text(event, central.status()['message'], current)
        return

    started = time.perf_counter()
    review = await central.moderate(current, prompt)
    if review.get('flagged') or (
        not review.get('available')
        and current['moderation_enabled']
        and current['moderation_fail_closed']
    ):
        record.update({
            'status': 'blocked',
            'error': review.get('error') or 'AI 内容审核判定为违规',
            'duration_ms': round((time.perf_counter() - started) * 1000),
        })
        await _persist(record, current)
        await _reply_text(event, current['blocked_response'], current)
        return

    if current['notice_enabled']:
        await _reply_text(event, current['notice_text'], current)
    try:
        optimized = await central.optimize_prompt(current, prompt)
        final_prompt = central.build_prompt(current, optimized)
        record['final_prompt'] = final_prompt
        async with limiter.semaphore(current['max_concurrency']):
            result = await central.generate(current, final_prompt)
    except Exception as error:  # noqa: BLE001 - 统一记录并回复友好文案
        record.update({
            'status': 'failed',
            'error': str(error)[:1000],
            'duration_ms': round((time.perf_counter() - started) * 1000),
        })
        await _persist(record, current)
        log.warning('AI 画图失败: %s', error)
        await _reply_text(event, current['failure_message'], current)
        return

    record.update({
        'status': 'success',
        'duration_ms': round((time.perf_counter() - started) * 1000),
        'provider': result['provider'],
        'provider_id': result['provider_id'],
        'model': result['model'],
        'size': current['image_size'],
        'image_url': result['url'],
    })
    image = await _image_bytes(result)
    try:
        outcome = await _deliver(
            event, current, _caption(current, event, prompt, result), result, image,
        )
        record.update({
            'delivered': outcome['delivered'],
            'send_mode': outcome['send_mode'],
            'hosted_url': outcome['hosted_url'],
            'error': outcome['error'],
        })
    except Exception as error:  # noqa: BLE001 - 发送失败仍要保留生成记录
        record['error'] = f'图片发送异常：{error}'[:1000]
        report_error(PLUGIN, 'AI画图', error)
    await _persist(record, current, image if current['history_save_images'] else b'')
    if not record.get('delivered'):
        log.warning('AI 画图发送失败: %s', record.get('error', ''))
        await _reply_text(event, current['failure_message'], current)
