"""压缩包安全解压 + 待审核内容采集。

解压前逐条校验成员名, 拦截 zip-slip (``../`` / 绝对路径 / 盘符) 与
tar 里的软链接、设备文件, 并对文件数、解压后总体积设硬上限 —— 上传者是普通
群成员, 压缩包必须当成不可信输入处理。

解压落到 data/staging/<记录号>/, 审核通过后才由 deploy.py 移入正式目录。
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import tarfile
import zipfile

# 上送审核的文本类扩展名 (源码 / 说明 / 配置)
TEXT_EXTS = ('.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg',
             '.py', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.js', '.ts', '.html',
             '.css', '.sh', '.cmake', '.rst', '.csv', '.lua')
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
FONT_EXTS = ('.ttf', '.otf', '.ttc', '.woff', '.woff2')

_MAX_TEXT_PER_FILE = 20000       # 单个文本文件上送的字符上限
_MAX_IMAGE_BYTES = 3 * 1024 * 1024   # 单张上送图片的体积上限


class ArchiveError(Exception):
    """压缩包非法 / 超限 (错误信息可直接回给群里)。"""


# ==================== 成员名校验 ====================

def _safe_member_name(name: str) -> str:
    """校验并归一压缩包内的相对路径; 非法时抛 ArchiveError。"""
    raw = (name or '').replace('\\', '/').strip()
    if not raw or raw in ('.', './'):
        return ''
    if raw.startswith('/') or (len(raw) > 1 and raw[1] == ':'):
        raise ArchiveError(f'压缩包含绝对路径成员: {name}')
    parts = []
    for part in raw.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            raise ArchiveError(f'压缩包含路径穿越成员: {name}')
        parts.append(part)
    return '/'.join(parts)


# ==================== 解压 ====================

def _extract_zip(data: bytes, dest: str, limits: dict) -> int:
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > limits['max_files']:
            raise ArchiveError(f'压缩包成员数 {len(infos)} 超过上限 {limits["max_files"]}')
        for info in infos:
            name = _safe_member_name(info.filename)
            if not name:
                continue
            target = os.path.join(dest, *name.split('/'))
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            total += info.file_size
            if total > limits['max_uncompressed']:
                raise ArchiveError(f'解压后体积超过上限 {limits["max_uncompressed"] / 1048576:.0f} MB')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, 65536)
    return total


def _extract_tar(data: bytes, dest: str, limits: dict) -> int:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as tf:
        members = tf.getmembers()
        if len(members) > limits['max_files']:
            raise ArchiveError(f'压缩包成员数 {len(members)} 超过上限 {limits["max_files"]}')
        for m in members:
            name = _safe_member_name(m.name)
            if not name:
                continue
            if m.issym() or m.islnk():
                raise ArchiveError(f'压缩包含链接成员 (不允许): {m.name}')
            if not (m.isfile() or m.isdir()):
                raise ArchiveError(f'压缩包含特殊文件成员 (不允许): {m.name}')
            target = os.path.join(dest, *name.split('/'))
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            total += m.size
            if total > limits['max_uncompressed']:
                raise ArchiveError(f'解压后体积超过上限 {limits["max_uncompressed"] / 1048576:.0f} MB')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, 65536)
    return total


def extract(data: bytes, filename: str, dest: str, limits: dict) -> dict:
    """解压到 dest (必须为空目录)。返回 {'total_size', 'root'}; 失败抛 ArchiveError。

    ``root`` 是压缩包唯一顶层目录名 (若压缩包直接把文件平铺在根则为空串),
    用于决定部署后的目录名。
    """
    low = (filename or '').lower()
    os.makedirs(dest, exist_ok=True)
    if low.endswith('.zip') or data[:2] == b'PK':
        total = _extract_zip(data, dest, limits)
    elif low.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar')):
        total = _extract_tar(data, dest, limits)
    elif low.endswith(('.rar', '.7z')):
        raise ArchiveError('暂不支持 rar / 7z, 请改用 zip 或 tar.gz 重新打包')
    else:
        # 无扩展名兜底: 依次尝试 zip / tar
        try:
            total = _extract_zip(data, dest, limits)
        except zipfile.BadZipFile:
            try:
                total = _extract_tar(data, dest, limits)
            except tarfile.TarError as e:
                raise ArchiveError(f'无法识别的压缩格式: {e}') from e
    entries = [e for e in os.listdir(dest)]
    root = entries[0] if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])) else ''
    return {'total_size': total, 'root': root}


# ==================== 待审核内容采集 ====================

def _read_text(path: str) -> str:
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read(_MAX_TEXT_PER_FILE + 1)
    except OSError:
        return ''


def image_size(data: bytes) -> tuple[int, int]:
    """不依赖 PIL 读 PNG / JPEG / GIF 尺寸, 解析失败返回 (0, 0)。"""
    try:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return struct.unpack('>II', data[16:24])
        if data[:3] == b'GIF':
            return struct.unpack('<HH', data[6:10])
        if data[:2] == b'\xff\xd8':
            i = 2
            while i < len(data):
                while i < len(data) and data[i] != 0xFF:
                    i += 1
                while i < len(data) and data[i] == 0xFF:
                    i += 1
                marker = data[i]
                i += 1
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack('>HH', data[i + 3:i + 7])
                    return (w, h)
                i += struct.unpack('>H', data[i:i + 2])[0]
    except Exception:  # noqa: BLE001 — 尺寸只用于清单展示, 解析失败不影响审核
        pass
    return (0, 0)


def collect(root_dir: str, limits: dict, image_quota: int) -> dict:
    """遍历解压结果, 产出送审素材。

    返回:
      ``tree``    [{path, size, kind}]      全部文件清单 (kind: text/image/font/binary)
      ``texts``   [{path, content, truncated}]  文本内容 (受 text_budget 预算约束)
      ``images``  [{path, size, width, height, data}]  待上送的图片 (最多 image_quota 张)
      ``rule_files`` 命中 rule*.md / readme 的路径列表
    """
    tree, texts, images, rule_files = [], [], [], []
    budget = max(0, int(limits.get('text_budget', 0)))
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in ('.git', '__pycache__', 'node_modules'))
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_dir).replace('\\', '/')
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            low = fn.lower()
            if low.endswith(IMAGE_EXTS):
                kind = 'image'
            elif low.endswith(FONT_EXTS):
                kind = 'font'
            elif low.endswith(TEXT_EXTS):
                kind = 'text'
            else:
                kind = 'binary'
            tree.append({'path': rel, 'size': size, 'kind': kind})
            if low in ('rule.md', 'readme.md', 'rule.txt', 'readme') or low.startswith('rule.'):
                rule_files.append(rel)
            if kind == 'text' and budget > 0:
                content = _read_text(full)
                if not content:
                    continue
                truncated = len(content) > _MAX_TEXT_PER_FILE
                content = content[:_MAX_TEXT_PER_FILE]
                if len(content) > budget:
                    content, truncated = content[:budget], True
                budget -= len(content)
                texts.append({'path': rel, 'content': content, 'truncated': truncated})
            elif kind == 'image' and len(images) < image_quota and size <= _MAX_IMAGE_BYTES:
                try:
                    with open(full, 'rb') as f:
                        blob = f.read()
                except OSError:
                    continue
                w, h = image_size(blob)
                images.append({'path': rel, 'size': size, 'width': w, 'height': h, 'data': blob})
    # rule.md 必须优先送审: 把它排到文本列表最前面
    texts.sort(key=lambda t: (0 if t['path'].lower().endswith('rule.md') else 1, t['path']))
    return {'tree': tree, 'texts': texts, 'images': images, 'rule_files': rule_files}


def collect_single(data: bytes, filename: str, limits: dict, image_quota: int) -> dict:
    """单文件上传的送审素材, 结构与 ``collect`` 一致 (只有一个成员)。"""
    low = (filename or '').lower()
    if low.endswith(IMAGE_EXTS):
        kind = 'image'
    elif low.endswith(FONT_EXTS):
        kind = 'font'
    elif low.endswith(TEXT_EXTS):
        kind = 'text'
    else:
        kind = 'binary'
    tree = [{'path': filename, 'size': len(data), 'kind': kind}]
    texts, images = [], []
    if kind == 'text' and limits.get('text_budget', 0) > 0:
        budget = min(int(limits['text_budget']), _MAX_TEXT_PER_FILE)
        raw = data.decode('utf-8', errors='replace')
        texts.append({'path': filename, 'content': raw[:budget], 'truncated': len(raw) > budget})
    elif kind == 'image' and image_quota > 0 and len(data) <= _MAX_IMAGE_BYTES:
        w, h = image_size(data)
        images.append({'path': filename, 'size': len(data), 'width': w, 'height': h, 'data': data})
    rule_files = [filename] if low.endswith('rule.md') else []
    return {'tree': tree, 'texts': texts, 'images': images, 'rule_files': rule_files}
