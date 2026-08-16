"""图片字节处理：格式识别与公网图片下载（拒绝内网与元数据地址）。"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
from urllib.parse import urlsplit

import aiohttp

MAX_IMAGE_BYTES = 20 * 1024 * 1024
_FALLBACK = ('png', 'image/png')


def sniff(data: bytes) -> tuple[str, str]:
    """按魔数判断图片扩展名与 MIME，未知格式回落到 PNG。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        return _FALLBACK
    header = bytes(data[:12])
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png', 'image/png'
    if header.startswith(b'\xff\xd8\xff'):
        return 'jpg', 'image/jpeg'
    if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
        return 'gif', 'image/gif'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    if header.startswith(b'BM'):
        return 'bmp', 'image/bmp'
    return _FALLBACK


def mime_for(file_name: str) -> str:
    suffix = str(file_name or '').rsplit('.', 1)[-1].casefold()
    return {
        'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'gif': 'image/gif', 'webp': 'image/webp', 'bmp': 'image/bmp',
    }.get(suffix, 'application/octet-stream')


def _header_dimensions(data: bytes) -> tuple[int, int]:
    """直接解析图片头部读取尺寸，不依赖 Pillow。"""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return struct.unpack('>II', data[16:24])
    if data[:3] == b'GIF':
        return struct.unpack('<HH', data[6:10])
    if data[:2] == b'\xff\xd8':  # JPEG：扫描到 SOF 段读取宽高
        index = 2
        while index < len(data) - 8:
            while index < len(data) and data[index] != 0xFF:
                index += 1
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack('>HH', data[index + 3:index + 7])
                return width, height
            index += struct.unpack('>H', data[index:index + 2])[0]
        return 0, 0
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8 ':
            width, height = struct.unpack('<HH', data[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if chunk == b'VP8L':
            b0, b1, b2, b3 = data[21:25]
            return (
                1 + (((b1 & 0x3F) << 8) | b0),
                1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6)),
            )
        if chunk == b'VP8X':
            return (
                1 + int.from_bytes(data[24:27], 'little'),
                1 + int.from_bytes(data[27:30], 'little'),
            )
    return 0, 0


def dimensions(data: bytes, fallback: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """返回图片像素尺寸；头部解析失败时退回 Pillow，仍失败则返回 fallback。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 16:
        return fallback
    value = bytes(data)
    try:
        width, height = _header_dimensions(value)
        if width > 0 and height > 0:
            return int(width), int(height)
    except (struct.error, IndexError, ValueError):
        pass
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(value)) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001 - 尺寸只用于 Markdown 展示，失败不阻断发送
        return fallback


async def _public_host(hostname: str) -> bool:
    if not hostname or hostname.casefold() == 'localhost':
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM,
        )
    except OSError:
        return False
    addresses = {item[4][0] for item in infos}
    if not addresses:
        return False
    return all(ipaddress.ip_address(value).is_global for value in addresses)


async def download(url: str, *, timeout: int = 30) -> bytes | None:
    """下载生图接口返回的公网图片，供历史记录留存缩略图。"""
    target = urlsplit(str(url or '').strip())
    if target.scheme not in {'http', 'https'} or not await _public_host(target.hostname or ''):
        return None
    client_timeout = aiohttp.ClientTimeout(total=max(5, min(120, timeout)))
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                content_type = response.headers.get('Content-Type', '').casefold()
                declared = int(response.headers.get('Content-Length') or 0)
                if not content_type.startswith('image/') or declared > MAX_IMAGE_BYTES:
                    return None
                chunks = []
                size = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        return None
                    chunks.append(chunk)
        data = b''.join(chunks)
        return data if data else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
