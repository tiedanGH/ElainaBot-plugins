"""游戏索引：目录名 ↔ 中文名。

玩家问的是「换位象棋怎么结算」，磁盘上是 ``games/move_chess/``，中间这层映射
必须由程序建立 —— 让模型自己猜目录名会直接猜错。

中文名从 mygame.cc 的 ``k_properties`` 里解析（``.name_`` / ``.description_``），
解析逻辑与 LGTBot_deploy 的 app/review.py 保持一致：支持多段字符串字面量拼接，
因为社区游戏常写成 "第一段" "第二段" 的形式。

索引按 mygame.cc 的 mtime+size 做缓存，玩家上传新游戏后自动失效重建。
"""
from __future__ import annotations

import os
import re
import threading

from . import sandbox

_PROP_TEMPLATE = r'\.\s*{field}\s*=\s*((?:"(?:[^"\\]|\\.)*"\s*)+)'
_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ESCAPES = (('\\n', '\n'), ('\\t', ' '), ('\\"', '"'), ('\\\\', '\\'))

_lock = threading.Lock()
_cache: dict = {}
_cache_key: tuple = ()


def _parse_prop(source: str, field: str) -> str:
    """取 k_properties 的某个字符串字段，支持相邻字面量拼接。"""
    match = re.search(_PROP_TEMPLATE.format(field=field), source or '')
    if not match:
        return ''
    text = ''.join(_STRING_LITERAL.findall(match.group(1)))
    for escape, char in _ESCAPES:
        text = text.replace(escape, char)
    return text.strip()


def _games_root(current: dict) -> dict | None:
    for root in sandbox.roots(current):
        if root['id'] == 'games' or root['path'].endswith('games'):
            if not root['is_file']:
                return root
    return None


def _signature(root: dict) -> tuple:
    """目录指纹：游戏名 + mygame.cc 的 (mtime, size)。任一变化即重建索引。"""
    items = []
    try:
        names = sorted(os.listdir(root['abs']))
    except OSError:
        return ()
    for name in names:
        source = os.path.join(root['abs'], name, 'mygame.cc')
        try:
            stat = os.stat(source)
        except OSError:
            continue
        items.append((name, int(stat.st_mtime), stat.st_size))
    return tuple(items)


def index(current: dict, *, refresh: bool = False) -> dict:
    """返回 {目录名: {dir, name, desc, has_rule}}。"""
    global _cache, _cache_key
    root = _games_root(current)
    if root is None:
        return {}
    key = (root['abs'], _signature(root))
    with _lock:
        if not refresh and key == _cache_key and _cache:
            return dict(_cache)
    limit = int(current.get('file_max_bytes') or 400000)
    result = {}
    for name, _mtime, _size in key[1]:
        folder = os.path.join(root['abs'], name)
        source = os.path.join(folder, 'mygame.cc')
        try:
            text = sandbox.read_text(source, limit)
        except OSError:
            text = ''
        result[name] = {
            'dir': name,
            'name': _parse_prop(text, 'name_') or name,
            'desc': _parse_prop(text, 'description_')[:300],
            'has_rule': os.path.isfile(os.path.join(folder, 'rule.md')),
            'path': f'{root["path"]}/{name}',
        }
    with _lock:
        _cache, _cache_key = result, key
    return dict(result)


def invalidate() -> None:
    global _cache, _cache_key
    with _lock:
        _cache, _cache_key = {}, ()


def _normalize(text: str) -> str:
    """比对用归一化：去空白与常见标点，全部小写。"""
    return re.sub(r'[\s_\-·、,，.。!！?？:：《》<>()（）]+', '', str(text or '')).casefold()


def resolve(current: dict, query: str) -> dict | None:
    """把用户/模型给的游戏名解析成索引项。

    依次尝试：目录名精确 → 中文名精确 → 归一化精确 → 归一化包含（唯一命中才算）。
    包含匹配要求唯一，避免「象棋」同时命中中国象棋 / 换位象棋时静默选错一个。
    """
    text = str(query or '').strip()
    if not text:
        return None
    catalog = index(current)
    if text in catalog:
        return catalog[text]
    for item in catalog.values():
        if item['name'] == text:
            return item
    target = _normalize(text)
    if not target:
        return None
    for item in catalog.values():
        if target in (_normalize(item['dir']), _normalize(item['name'])):
            return item
    partial = [
        item for item in catalog.values()
        if target in _normalize(item['name']) or target in _normalize(item['dir'])
    ]
    return partial[0] if len(partial) == 1 else None


def search(current: dict, keyword: str = '') -> list[dict]:
    """按关键词过滤游戏清单；关键词为空则返回全部（按中文名排序）。"""
    catalog = sorted(index(current).values(), key=lambda item: item['name'])
    target = _normalize(keyword)
    if not target:
        return catalog
    return [
        item for item in catalog
        if target in _normalize(item['name'])
        or target in _normalize(item['dir'])
        or target in _normalize(item['desc'])
    ]
