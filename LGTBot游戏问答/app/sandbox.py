"""只读代码沙箱：把 AI 的文件访问死锁在 LGTBot 源码目录内。

**所有**文件系统访问都必须经过本模块，工具层不得自己拼路径、不得自己 open()。

安全边界：
  · 只读            —— 本模块不提供任何写 / 删 / 重命名 / 执行入口
  · 根白名单        —— 目标必须落在某个启用的 root 内，用 realpath 比对，软链接
                       指向仓库外会被 realpath 解开后判定越界
  · 目录黑名单      —— .git / __pycache__ / data / build 等一律不可见
  · 后缀白名单      —— 只读文本源码；.so/.png/.db 这类二进制直接拒绝
  · 体积与行数上限  —— 单文件超限跳过，单次读取按行窗口截断

LGTBot 插件自己的 ``data/``（config.yaml、lgtbot.db、user_cache.db）不在任何 root
内，因此天然不可达 —— 这是刻意的，不要把 data 加进 roots。
"""
from __future__ import annotations

import fnmatch
import os

# 这些目录名在任何层级都不可见
_DENY_DIRS = {
    '.git', '.github', '__pycache__', 'node_modules', '.pytest_cache',
    '.idea', '.vscode', 'data', 'build', 'build_prebuilt', '.venv', 'venv',
}

# 只读文本源码。二进制（.so/.png/.db/.zip）不在列 = 直接拒绝
_TEXT_SUFFIXES = {
    '.cc', '.cpp', '.cxx', '.c', '.h', '.hpp', '.hxx', '.inl',
    '.md', '.txt', '.py', '.cmake', '.json', '.yml', '.yaml',
    '.js', '.ts', '.html', '.css', '.sh', '.toml', '.ini', '.cfg', '.in',
}
_TEXT_NAMES = {'CMakeLists.txt', 'Makefile', 'LICENSE', 'README'}


class SandboxError(ValueError):
    """路径越界、类型不允许或目标不存在。消息可直接回灌给模型。"""


# ==================== 根解析 ====================


def base_dir(current: dict) -> str:
    """LGTBot 插件目录。配置留空时按框架目录结构自动定位。"""
    configured = str(current.get('lgtbot_dir') or '').strip()
    if configured:
        return os.path.realpath(configured)
    # app/sandbox.py -> app -> LGTBot游戏问答 -> plugins
    plugins_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.realpath(os.path.join(plugins_dir, 'LGTBot_ElainaBot'))


def roots(current: dict) -> list[dict]:
    """启用且真实存在的根。不存在的根静默跳过（子模块没 checkout 时优雅降级）。"""
    base = base_dir(current)
    result = []
    for item in current.get('roots') or []:
        if not item.get('enabled'):
            continue
        relative = str(item.get('path') or '').strip().strip('/\\')
        if not relative or _has_denied_part(relative):
            continue
        target = os.path.realpath(os.path.join(base, relative))
        if not _inside(base, target) or not os.path.exists(target):
            continue
        result.append({
            'id': str(item.get('id') or relative),
            'label': str(item.get('label') or relative),
            'path': relative.replace(os.sep, '/'),
            'abs': target,
            'is_file': os.path.isfile(target),
        })
    return result


def status(current: dict) -> dict:
    """面板用：数据源是否就绪、哪些根缺失。"""
    base = base_dir(current)
    available = {item['id'] for item in roots(current)}
    missing = [
        {'id': str(item.get('id') or ''), 'path': str(item.get('path') or '')}
        for item in current.get('roots') or []
        if item.get('enabled') and str(item.get('id') or '') not in available
    ]
    return {
        'base_dir': base,
        'base_exists': os.path.isdir(base),
        'roots': [
            {'id': item['id'], 'label': item['label'], 'path': item['path']}
            for item in roots(current)
        ],
        'missing': missing,
    }


# ==================== 路径校验 ====================


def _inside(parent: str, target: str) -> bool:
    try:
        return os.path.commonpath((
            os.path.normcase(parent), os.path.normcase(target),
        )) == os.path.normcase(parent)
    except ValueError:      # 跨盘符（Windows）
        return False


def _has_denied_part(relative: str) -> bool:
    return any(
        part.casefold() in {name.casefold() for name in _DENY_DIRS}
        for part in relative.replace('\\', '/').split('/')
    )


def is_text_file(name: str) -> bool:
    base = os.path.basename(name)
    return base in _TEXT_NAMES or os.path.splitext(base)[1].lower() in _TEXT_SUFFIXES


def relative_of(current: dict, absolute: str) -> str:
    """绝对路径 → 给模型看的相对路径（始终用 /，避免 Windows 反斜杠混进回答）。"""
    return os.path.relpath(absolute, base_dir(current)).replace(os.sep, '/')


def resolve(current: dict, path: str, *, must_be_file: bool = False) -> str:
    """把模型给的相对路径解析成经过校验的绝对路径。

    校验顺序：非空 → 无黑名单目录 → realpath 落在某个启用根内 → 存在 →
    （可选）是文件且后缀在白名单。任何一步不过直接抛 SandboxError。
    """
    relative = str(path or '').strip().replace('\\', '/').lstrip('/')
    if not relative or relative in ('.', './'):
        raise SandboxError('缺少 path')
    if _has_denied_part(relative):
        raise SandboxError(f'该目录不可访问: {relative}')
    available = roots(current)
    if not available:
        raise SandboxError('LGTBot 源码目录不可用，请在面板检查「源码目录」配置')
    target = os.path.realpath(os.path.join(base_dir(current), relative))
    for root in available:
        if target == root['abs'] or (not root['is_file'] and _inside(root['abs'], target)):
            break
    else:
        raise SandboxError(f'路径超出可检索范围: {relative}')
    if not os.path.exists(target):
        raise SandboxError(f'路径不存在: {relative}')
    if must_be_file:
        if not os.path.isfile(target):
            raise SandboxError(f'不是文件: {relative}')
        if not is_text_file(target):
            raise SandboxError(f'该文件类型不可读（只支持文本源码）: {relative}')
    return target


def resolve_scope(current: dict, scope: str) -> list[dict]:
    """检索范围：空 = 全部启用根；root id 或相对目录路径 = 只搜那一块。"""
    text = str(scope or '').strip().replace('\\', '/').strip('/')
    available = roots(current)
    if not text:
        return available
    for root in available:
        if text.casefold() in (root['id'].casefold(), root['path'].casefold()):
            return [root]
    target = resolve(current, text)
    return [{
        'id': text, 'label': text, 'path': relative_of(current, target),
        'abs': target, 'is_file': os.path.isfile(target),
    }]


# ==================== 读取 ====================


def iter_files(current: dict, scope_roots: list[dict], pattern: str = '*'):
    """遍历范围内的文本文件（绝对路径）。跳过黑名单目录、非文本、超限文件。"""
    limit = int(current.get('file_max_bytes') or 400000)
    glob = str(pattern or '*').strip() or '*'
    for root in scope_roots:
        if root['is_file']:
            if _match(current, root['abs'], glob) and _readable(root['abs'], limit):
                yield root['abs']
            continue
        for folder, dirs, files in os.walk(root['abs']):
            dirs[:] = sorted(
                item for item in dirs
                if item.casefold() not in {name.casefold() for name in _DENY_DIRS}
            )
            for name in sorted(files):
                absolute = os.path.join(folder, name)
                if _match(current, absolute, glob) and _readable(absolute, limit):
                    yield absolute


def _match(current: dict, absolute: str, pattern: str) -> bool:
    if not is_text_file(absolute):
        return False
    if pattern == '*':
        return True
    name = os.path.basename(absolute)
    return fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(
        relative_of(current, absolute), pattern,
    )


def _readable(absolute: str, limit: int) -> bool:
    try:
        return os.path.getsize(absolute) <= limit
    except OSError:
        return False


def read_text(absolute: str, limit: int) -> str:
    """整文件读取（受 limit 字节约束）。errors='replace' 保证不会因编码炸掉。"""
    with open(absolute, encoding='utf-8', errors='replace') as file:
        return file.read(limit)


def read_lines(current: dict, absolute: str, start: int, count: int) -> dict:
    """按行窗口读取，返回带行号的文本 —— 模型据此给出「文件:行号」出处。"""
    maximum = int(current.get('read_max_lines') or 400)
    start = max(1, int(start or 1))
    count = max(1, min(int(count or maximum), maximum))
    lines: list[str] = []
    total = 0
    with open(absolute, encoding='utf-8', errors='replace') as file:
        for number, line in enumerate(file, 1):
            total = number
            if start <= number < start + count:
                lines.append(f'{number}|{line.rstrip()}')
    return {
        'path': relative_of(current, absolute),
        'start_line': start,
        'end_line': min(start + count - 1, total),
        'total_lines': total,
        'truncated': total > start + count - 1,
        'content': '\n'.join(lines),
    }


def list_entries(current: dict, absolute: str) -> list[dict]:
    """列目录。黑名单目录与二进制文件不出现在结果里。"""
    result = []
    for name in sorted(os.listdir(absolute)):
        full = os.path.join(absolute, name)
        if os.path.isdir(full):
            if name.casefold() in {item.casefold() for item in _DENY_DIRS}:
                continue
            result.append({'name': name, 'type': 'dir'})
        elif is_text_file(name):
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            result.append({'name': name, 'type': 'file', 'size': size})
    return result
