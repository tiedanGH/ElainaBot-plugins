"""从「引用消息」中定位文件并下载。

QQ 群消息携带引用时, 被引用消息以 dict 形式塞在 ``d.msg_elements`` 里
(典型字段: author / content / message_type / msg_idx), 框架在
``parse_message_generic`` 中已把它拷到 ``event.msg_elements``。文件消息在
该结构里的确切字段名随协议版本变化, 因此这里**递归扫描**所有嵌套 dict/list,
凡是出现 url 形态的值就作为候选, 再按「文件名/内容类型是否像压缩包」排序,
而不是硬编码某个字段路径。

同时兼容用户把压缩包直接附在指令消息上的情况 (``event.attachments``)。
扫描不到文件时, 上层会把 msg_elements/attachments 原样存盘 (data/diagnostics),
方便在面板里查看真实载荷形状后再调整。
"""

from __future__ import annotations

import html
import os
import re

import aiohttp

_URL_KEYS = ('url', 'file_url', 'download_url', 'src', 'link', 'href', 'file_path')
_NAME_KEYS = ('file_name', 'filename', 'name', 'file_uuid', 'title')
_SIZE_KEYS = ('file_size', 'size', 'length')
_TYPE_KEYS = ('content_type', 'mime_type', 'file_type', 'type')

# 支持解压的扩展名 (rar/7z 无标准库支持, 识别出来给明确提示)
ARCHIVE_EXTS = ('.zip', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar')
UNSUPPORTED_EXTS = ('.rar', '.7z')

_IMAGE_CT = ('image/',)
_URL_RE = re.compile(r'^(?:https?://|//)?[\w.-]+\.[a-z]{2,}(?:[:/]|$)', re.I)
_DL_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ElainaBot/1.0)'}


def archive_ext(name: str) -> str:
    """返回 name 命中的压缩包扩展名 (含复合扩展, 如 .tar.gz), 未命中返回 ''。"""
    low = (name or '').lower()
    for ext in sorted(ARCHIVE_EXTS + UNSUPPORTED_EXTS, key=len, reverse=True):
        if low.endswith(ext):
            return ext
    return ''


def _normalize_url(value: str) -> str:
    """QQ 的附件 url 常常不带 scheme, 统一补成 https://。"""
    url = html.unescape(str(value or '')).strip()
    if not url or not _URL_RE.match(url):
        return ''
    if url.startswith('//'):
        return 'https:' + url
    if not url.lower().startswith(('http://', 'https://')):
        return 'https://' + url
    return url


def _candidate_from_dict(node: dict) -> dict | None:
    """把一个含 url 的 dict 归一成候选 {url, filename, size, content_type}。"""
    url = ''
    for k in _URL_KEYS:
        url = _normalize_url(node.get(k))
        if url:
            break
    if not url:
        return None
    name = ''
    for k in _NAME_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            name = v.strip()
            break
    size = 0
    for k in _SIZE_KEYS:
        v = node.get(k)
        if isinstance(v, (int, float)) and v > 0:
            size = int(v)
            break
        if isinstance(v, str) and v.isdigit():
            size = int(v)
            break
    ctype = ''
    for k in _TYPE_KEYS:
        v = node.get(k)
        if isinstance(v, str) and '/' in v:
            ctype = v.lower()
            break
    return {'url': url, 'filename': name, 'size': size, 'content_type': ctype}


def _walk(node, out: list, depth: int = 0):
    """递归收集所有含 url 的 dict (深度设上限, 防御异常深的载荷)。"""
    if depth > 8:
        return
    if isinstance(node, dict):
        cand = _candidate_from_dict(node)
        if cand:
            out.append(cand)
        for v in node.values():
            _walk(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, depth + 1)


def _score(cand: dict) -> int:
    """候选优先级: 压缩包 > 二进制附件 > 普通文件 > 图片。

    图片给 1 分 (而不是 0): 单文件上传允许上传图片素材, 但只要同一条消息里还有
    别的候选, 图片永远排在最后 —— 避免引用消息里的表情/配图被误当成待上传文件。
    """
    name = cand.get('filename') or ''
    url_name = os.path.basename((cand.get('url') or '').split('?')[0])
    ext = archive_ext(name) or archive_ext(url_name)
    if ext in ARCHIVE_EXTS:
        return 100
    if ext in UNSUPPORTED_EXTS:
        return 90
    ct = cand.get('content_type') or ''
    if 'zip' in ct or 'compress' in ct or 'octet-stream' in ct:
        return 80
    if ct.startswith(_IMAGE_CT) or url_name.lower().endswith(
            ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')):
        return 1
    return 10


def extract_file_ref(event, allow_any: bool = False) -> dict | None:
    """定位待上传文件: 优先引用消息 (msg_elements), 其次指令自带附件。

    ``allow_any=False`` 时只接受压缩包/二进制附件 (图片、纯链接不算);
    ``allow_any=True`` 时任何候选都可以 (单文件上传场景, 允许图片素材)。
    返回 ``{url, filename, size, content_type, source}``; 找不到返回 None。
    """
    threshold = 1 if allow_any else 10
    for source, payload in (('quote', getattr(event, 'msg_elements', None)),
                            ('attachment', getattr(event, 'attachments', None))):
        cands: list = []
        _walk(payload or [], cands)
        cands = [c for c in cands if _score(c) >= threshold]
        if not cands:
            continue
        best = max(cands, key=_score)
        if not best.get('filename'):
            best['filename'] = os.path.basename((best['url'].split('?')[0])) or 'upload.bin'
        best['source'] = source
        return best
    return None


def debug_payload(event) -> dict:
    """定位失败时留档的原始载荷 (供面板排查协议字段变化)。"""
    return {
        'event_type': getattr(event, 'event_type', ''),
        'content': getattr(event, 'content', ''),
        'msg_elements': getattr(event, 'msg_elements', None),
        'attachments': getattr(event, 'attachments', None),
        'message_scene': getattr(event, 'message_scene', None),
    }


async def download(url: str, max_bytes: int, timeout_s: int) -> tuple[bytes | None, str]:
    """流式下载并强制体积上限。返回 (数据, 错误信息), 成功时错误为空串。"""
    timeout = aiohttp.ClientTimeout(total=max(5, timeout_s))
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=_DL_HEADERS) as s, s.get(url) as resp:
            if resp.status != 200:
                return None, f'下载失败: HTTP {resp.status}'
            clen = resp.headers.get('Content-Length')
            if clen and clen.isdigit() and int(clen) > max_bytes:
                return None, f'文件过大: {int(clen) / 1048576:.1f} MB, 上限 {max_bytes / 1048576:.0f} MB'
            chunks, total = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                total += len(chunk)
                if total > max_bytes:
                    return None, f'文件过大: 超过上限 {max_bytes / 1048576:.0f} MB'
                chunks.append(chunk)
            data = b''.join(chunks)
            if not data:
                return None, '下载失败: 内容为空'
            return data, ''
    except Exception as e:  # noqa: BLE001 — 网络异常统一转为可回复的文案
        return None, f'下载异常: {type(e).__name__}: {e}'
