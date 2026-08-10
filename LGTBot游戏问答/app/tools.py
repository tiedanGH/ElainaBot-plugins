"""交给模型的五个只读工具。

全部经 sandbox 收口 —— 这里不出现任何 open() / os.walk() / 路径拼接。
工具集**只读**：没有写、删、移动、执行、发消息的入口，所以即使模型被上传的
游戏源码注入，能造成的最坏后果也只是读到别的游戏源码。

返回值都是 JSON 友好的 dict；失败统一返回 {'ok': False, 'error': ...}，让模型
看到失败原因后自己换个查法，而不是抛异常中断整轮。
"""
from __future__ import annotations

import asyncio
import os

from . import games, sandbox

TOOLS_SCHEMA = [
    {
        'type': 'function',
        'function': {
            'name': 'list_games',
            'description': (
                '列出 LGTBot 所有游戏，返回中文名、目录名和一句话简介。'
                '不确定用户说的是哪个游戏、或需要把中文名换成目录名时先调用它。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'keyword': {
                        'type': 'string',
                        'description': '可选，按中文名/目录名/简介过滤；留空返回全部游戏。',
                    },
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_game_rule',
            'description': (
                '读取某个游戏的 rule.md 完整规则文档（作者写给玩家的权威规则）。'
                '玩法、流程、术语类问题首选这个。计分与结算细节要另外读 mygame.cc。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'game': {
                        'type': 'string',
                        'description': '游戏中文名或目录名，如「换位象棋」或 move_chess。',
                    },
                },
                'required': ['game'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_code',
            'description': (
                '在可检索范围内做**子串**搜索（不是正则），返回匹配的文件、行号和该行内容。'
                '用来定位计分、结算、成就、选项的实现位置，再用 read_file 读上下文。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': '要搜的子串，如 score_ 或 OnStageEnd。'},
                    'scope': {
                        'type': 'string',
                        'description': (
                            '可选，限定范围。可填 list_games 返回的 path（如 lgtbot/games/move_chess）'
                            '或范围 id（games / bot_core / game_framework / utility / bridge）。'
                            '留空搜全部，慢且噪声大，建议先缩范围。'
                        ),
                    },
                    'file_pattern': {
                        'type': 'string',
                        'description': '可选，文件名通配符，如 *.cc、mygame.cc、*.h。默认全部文本文件。',
                    },
                    'case_sensitive': {'type': 'boolean', 'description': '可选，是否区分大小写，默认否。'},
                },
                'required': ['query'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': (
                '按行窗口读取一个文本文件，返回带行号的内容。'
                '用它读 mygame.cc / achievements.h / options.h 的具体实现并引用行号。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                        'description': '相对路径，如 lgtbot/games/move_chess/mygame.cc。',
                    },
                    'start_line': {'type': 'integer', 'description': '起始行号，从 1 开始，默认 1。'},
                    'line_count': {'type': 'integer', 'description': '读取行数，默认取配置上限。'},
                },
                'required': ['path'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_dir',
            'description': '列出目录下的文件和子目录，用来确认某个游戏目录里有哪些文件可读。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                        'description': '相对路径，如 lgtbot/games/move_chess。',
                    },
                },
                'required': ['path'],
                'additionalProperties': False,
            },
        },
    },
]

TOOL_NAMES = [item['function']['name'] for item in TOOLS_SCHEMA]


# ==================== 实现 ====================


def _list_games(current: dict, arguments: dict) -> dict:
    items = games.search(current, str(arguments.get('keyword') or ''))
    if not items:
        return {
            'ok': False,
            'error': '没有找到游戏。可能是 LGTBot 源码目录未配置或 lgtbot 子模块未拉取。',
        }
    return {
        'ok': True,
        'count': len(items),
        'games': [
            {'name': item['name'], 'dir': item['dir'], 'path': item['path'],
             'desc': item['desc'], 'has_rule': item['has_rule']}
            for item in items
        ],
    }


def _read_game_rule(current: dict, arguments: dict) -> dict:
    query = str(arguments.get('game') or '')
    item = games.resolve(current, query)
    if item is None:
        candidates = [entry['name'] for entry in games.search(current)][:40]
        return {
            'ok': False,
            'error': f'没有找到游戏「{query}」，也可能名字对应多个游戏。请先用 list_games 确认。',
            'candidates': candidates,
        }
    path = f'{item["path"]}/rule.md'
    try:
        absolute = sandbox.resolve(current, path, must_be_file=True)
    except sandbox.SandboxError as error:
        return {'ok': False, 'game': item['name'], 'error': str(error)}
    text = sandbox.read_text(absolute, int(current.get('file_max_bytes') or 400000))
    return {
        'ok': True,
        'game': item['name'],
        'dir': item['dir'],
        'path': sandbox.relative_of(current, absolute),
        'source_files': f'实现在 {item["path"]}/mygame.cc、achievements.h、options.h',
        'content': text,
    }


def _search_code(current: dict, arguments: dict) -> dict:
    query = str(arguments.get('query') or '')
    if not query.strip():
        return {'ok': False, 'error': '缺少 query'}
    try:
        scope_roots = sandbox.resolve_scope(current, str(arguments.get('scope') or ''))
    except sandbox.SandboxError as error:
        return {'ok': False, 'error': str(error)}
    if not scope_roots:
        return {'ok': False, 'error': 'LGTBot 源码目录不可用，请在面板检查「源码目录」配置'}
    limit = int(current.get('search_max_matches') or 60)
    case_sensitive = bool(arguments.get('case_sensitive'))
    needle = query if case_sensitive else query.casefold()
    pattern = str(arguments.get('file_pattern') or '*')
    matches: list[dict] = []
    scanned = 0
    for absolute in sandbox.iter_files(current, scope_roots, pattern):
        scanned += 1
        try:
            with open(absolute, encoding='utf-8', errors='replace') as file:
                for number, line in enumerate(file, 1):
                    candidate = line if case_sensitive else line.casefold()
                    if needle in candidate:
                        matches.append({
                            'path': sandbox.relative_of(current, absolute),
                            'line': number,
                            'text': line.strip()[:300],
                        })
                        if len(matches) >= limit:
                            return {
                                'ok': True, 'query': query, 'files_scanned': scanned,
                                'matches': matches, 'truncated': True,
                                'hint': '命中过多已截断，请缩小 scope 或换更具体的 query。',
                            }
        except OSError:
            continue
    return {
        'ok': True, 'query': query, 'files_scanned': scanned,
        'matches': matches, 'truncated': False,
    }


def _read_file(current: dict, arguments: dict) -> dict:
    try:
        absolute = sandbox.resolve(current, str(arguments.get('path') or ''), must_be_file=True)
    except sandbox.SandboxError as error:
        return {'ok': False, 'error': str(error)}
    try:
        result = sandbox.read_lines(
            current, absolute,
            int(arguments.get('start_line') or 1),
            int(arguments.get('line_count') or 0),
        )
    except (OSError, ValueError) as error:
        return {'ok': False, 'error': f'读取失败: {error}'}
    result['ok'] = True
    return result


def _list_dir(current: dict, arguments: dict) -> dict:
    path = str(arguments.get('path') or '')
    try:
        absolute = sandbox.resolve(current, path)
    except sandbox.SandboxError as error:
        return {'ok': False, 'error': str(error)}
    if os.path.isfile(absolute):
        return {'ok': True, 'path': sandbox.relative_of(current, absolute),
                'entries': [], 'note': '这是文件，不是目录，请直接用 read_file 读。'}
    try:
        entries = sandbox.list_entries(current, absolute)
    except OSError as error:
        return {'ok': False, 'error': f'列目录失败: {error}'}
    return {'ok': True, 'path': sandbox.relative_of(current, absolute), 'entries': entries}


_HANDLERS = {
    'list_games': _list_games,
    'read_game_rule': _read_game_rule,
    'search_code': _search_code,
    'read_file': _read_file,
    'list_dir': _list_dir,
}


async def run(name: str, arguments: dict, current: dict) -> dict:
    """工具总入口。磁盘 IO 放线程池，避免阻塞事件循环里的消息收发。"""
    handler = _HANDLERS.get(str(name or ''))
    if handler is None:
        return {'ok': False, 'error': f'未知工具: {name}'}
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        return await asyncio.to_thread(handler, current, arguments)
    except sandbox.SandboxError as error:
        return {'ok': False, 'error': str(error)}
    except (OSError, ValueError) as error:
        return {'ok': False, 'error': f'{type(error).__name__}: {error}'}
