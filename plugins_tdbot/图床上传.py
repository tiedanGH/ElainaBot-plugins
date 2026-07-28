"""图床上传: 主人手动把图片上传到指定图床

指令 (仅主人):
  图床                单独输入 → 返回全部图床 + 可用图床列表
  图床 <图床名> <图片>  上传附带的图片到指定图床, 返回图片链接

依赖 image_hosting 模块 (modules/image_hosting)。
"""

import re
import inspect

import aiohttp

from core.plugin.decorators import handler
from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, "图床上传")

_DL_TIMEOUT_S = 15.0
_MAX_IMG = 30 * 1024 * 1024
_DL_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ElainaBot/1.0)'}


# ==================== 依赖获取 ====================

def _get_hosting():
    try:
        from core.bot.manager import _bot_manager_ref
        bm = _bot_manager_ref
        if bm is None or bm.module_manager is None:
            return None
        return bm.module_manager.get('image_hosting')
    except Exception:
        return None


def _get_bot(appid):
    try:
        from core.bot.manager import _bot_manager_ref
        return _bot_manager_ref.get_bot(appid) if _bot_manager_ref else None
    except Exception:
        return None


# ==================== 图片下载 ====================

async def _download(url):
    """下载图片 bytes; 失败返回 None"""
    try:
        timeout = aiohttp.ClientTimeout(total=_DL_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout, headers=_DL_HEADERS) as s:
            async with s.get(url) as resp:
                if resp.status != 200:
                    log.warning(f"图片下载非 200: {resp.status}")
                    return None
                clen = resp.headers.get('Content-Length')
                if clen and clen.isdigit() and int(clen) > _MAX_IMG:
                    log.warning(f"图片 Content-Length {clen} 超上限")
                    return None
                data = await resp.read()   # 完整读取到 EOF
                if not data or len(data) > _MAX_IMG:
                    return None
                return data
    except Exception as e:
        log.warning(f"图片下载异常 {type(e).__name__}: {e}")
        return None


# ==================== 图床列表 ====================

def _format_bed_list(hosting):
    status = hosting.status()
    if not status:
        return "未发现任何图床实现"
    lines = ["## 📋 图床列表"]
    available = []
    for name, ok in status.items():
        bed = hosting.get_bed(name)
        disp = getattr(bed, 'display_name', '') or name
        lines.append(f"{'✅' if ok else '❌'} `{name}` — {disp}")
        if ok:
            available.append(name)
    lines.append("")
    lines.append(f"**可用图床:** {'、'.join(f'`{n}`' for n in available) if available else '无'}")
    lines.append("> 用法: `图床 <图床名> <图片>`")
    return '\n'.join(lines)


# ==================== 上传分发 ====================

def _supported_kwargs(fn, **kwargs):
    """仅保留 fn 签名声明的关键字参数 (仿 image_hosting._call_with_supported_kwargs)"""
    try:
        params = inspect.signature(fn).parameters
        return {k: v for k, v in kwargs.items() if k in params and v is not None}
    except (TypeError, ValueError):
        return {}


async def _upload_to(hosting, name, img_bytes, event):
    """上传到指定图床, 返回 (url, error). url 非空表示成功。"""
    bed = hosting.get_bed(name)
    fn = getattr(bed, 'upload_url', None) or getattr(bed, 'upload', None)
    if fn is None:
        return None, "该图床未实现上传方法"

    bot = _get_bot(event.appid)
    tm = getattr(bot, 'token_manager', None) if bot else None
    sender = getattr(event, 'sender', None) or getattr(event, '_sender', None)
    kwargs = _supported_kwargs(
        fn,
        filename='image.png', file_name='image.png',
        token_manager=tm, sender=sender, user_id=event.user_id,
    )
    try:
        result = await fn(img_bytes, **kwargs)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    # 返回值统一: str(http) / dict(url|file_url) 视为成功; tuple(False,原因) 为失败
    if isinstance(result, str) and result.startswith('http'):
        return result, None
    if isinstance(result, dict):
        url = result.get('url') or result.get('file_url')
        if url:
            return url, None
    if isinstance(result, tuple) and len(result) >= 2:
        return None, str(result[1])
    return None, str(result)


# ==================== 主入口 ====================

@handler(r'^图床(?:\s+(.+?))?(?:<[^>]*>)?\s*$',
         name='图床上传',
         desc='主人手动上传图片到指定图床',
         owner_only=True)
async def cmd_bed(event, match):
    hosting = _get_hosting()
    if not hosting:
        await event.reply("图床模块 image_hosting 未启用")
        return

    # 剥离图片 <url> 标记后取图床名参数
    arg = re.sub(r'<[^>]*>', '', match.group(1) or '').strip()

    # 单独「图床」→ 返回列表
    if not arg:
        await event.reply(_format_bed_list(hosting))
        return

    name = arg.split()[0]
    status = hosting.status()
    if name not in status:
        avail = '、'.join(n for n, ok in status.items() if ok) or '无'
        await event.reply(f"❌ 未知图床「{name}」\n可用图床: {avail}\n（发送「图床」查看完整列表）")
        return
    if not status[name]:
        await event.reply(f"❌ 图床「{name}」当前不可用（未启用或配置不全）")
        return

    img_url = event.image_url
    if not img_url:
        await event.reply(f"❌ 请在「图床 {name}」指令后附带一张图片")
        return

    img_bytes = await _download(img_url)
    if not img_bytes:
        await event.reply("❌ 图片下载失败")
        return

    url, err = await _upload_to(hosting, name, img_bytes, event)
    if url:
        await event.reply(f"✅ 上传成功（{name}）\n`{url}`")
    else:
        await event.reply(f"❌ 上传失败（{name}）: {err}")
