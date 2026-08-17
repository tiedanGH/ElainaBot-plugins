"""交给模型的六个只读检索工具，外加一个可选的画图工具。

检索工具全部经 sandbox 收口 —— 这里不出现任何 open() / os.walk() / 路径拼接。
它们**只读**：没有写、删、移动、执行、发消息的入口，所以即使模型被上传的
游戏源码注入，能造成的最坏后果也只是读到别的游戏源码。

唯一的例外是 ``draw_image``（见 drawing.py）：它默认关闭，开了也只是把描述转给
「AI 画图」插件，审核与额度都在对方那边。它**不属于**只读集，因此在 main.py 的
接地闸里也不算「查过代码」。

返回值都是 JSON 友好的 dict；失败统一返回 {'ok': False, 'error': ...}，让模型
看到失败原因后自己换个查法，而不是抛异常中断整轮。
"""
from __future__ import annotations

import asyncio
import os

from . import drawing, games, sandbox

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
                '读取某个游戏的 rule.md 完整规则文档（作者写给玩家的权威规则），'
                '并返回该游戏目录下真实存在的文件清单 source_files。'
                '问某个游戏时**先调用它**：既拿到规则，也知道接下来能读哪些文件。'
                '玩法、流程、术语类问题看规则即可；计分、结算、成就、选项的细节'
                '必须再用 read_file 读 source_files 里的 .cc / .h 实现。'
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
            'name': 'read_game_source',
            'description': (
                '一次性读取某个游戏的**全部源码**（规则 + 所有 .cc/.h），带行号返回。'
                '问机制、流程、顺序、计分、判定时**首选这个** —— 一次就能看全，'
                '不用猜该读哪个文件、也不用反复调用 read_file。'
                '游戏太大装不下时会按 focus 关键词挑最相关的文件，'
                '并在 files_omitted 里列出没装下的文件，再用 search_code / read_file 补。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'game': {'type': 'string', 'description': '游戏中文名或目录名。'},
                    'focus': {
                        'type': 'string',
                        'description': (
                            '可选，本次要了解的关键词（用户问题里的术语即可）。'
                            '游戏超出体积上限时，据此优先挑含该词的文件。'
                        ),
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

READONLY_NAMES = [item['function']['name'] for item in TOOLS_SCHEMA]

# 供解析与校验用的**全集**，与「这一轮真的发给模型几个工具」无关：
# main.py 要靠它识别模型写成文本的工具调用，画图关着的时候也得认得出来
# （认出来才能在 run() 里明确回一句「未开启」，而不是当成普通正文发出去）。
TOOL_NAMES = [*READONLY_NAMES, drawing.TOOL_NAME]


def schema_for(current: dict) -> list:
    """本轮真正交给模型的工具清单。

    画图关着时**不能只在 run() 里挡**：schema 一直挂着的话，模型会反复尝试调用
    一个注定失败的工具，白烧轮数；schema 本身也要占输入预算（见 main._tool_budget）。
    """
    if drawing.enabled(current):
        return [*TOOLS_SCHEMA, drawing.TOOL_SCHEMA]
    return TOOLS_SCHEMA


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
        # 连同该游戏**真实存在**的文件清单一起返回。模型拿不到清单时会照着自己
        # 想象的工程结构编路径（实测编出过 game_logic/*.py，而本项目全是 C++），
        # 把真实清单摆在眼前，编造就失去了空间。
        'source_files': _game_files(current, item),
        'note': '只能引用上面 source_files 里真实存在的文件，不要臆造其他路径。',
        'content': text,
    }


def _game_files(current: dict, item: dict) -> list:
    """列出该游戏目录下真实存在的可读文件（相对路径）。"""
    try:
        absolute = sandbox.resolve(current, item['path'])
        entries = sandbox.list_entries(current, absolute)
    except (sandbox.SandboxError, OSError):
        return []
    return [
        f'{item["path"]}/{entry["name"]}'
        for entry in entries if entry['type'] == 'file'
    ]


def _read_game_source(current: dict, arguments: dict) -> dict:
    """一次性把整个游戏的源码交给模型。

    动机有两个：
    1) 游戏目录的文件数差异很大（实测 3~13 个），且额外的 .h 往往才是机制所在。
       让模型自己猜该读哪个，既多绕几轮也容易漏。
    2) 中央模块的 XML 回退协议本身不稳（见 central.TOOL_FORMAT_RULE），
       每多一轮工具调用就多一次失败机会。一次读全能把轮数压到最少。

    实测 68 个游戏中位体积 27KB、83% 在 60KB 以内，绝大多数能整包装下。
    装不下时按 focus 关键词的命中数挑文件，并如实告知哪些没装下。
    """
    query = str(arguments.get('game') or '')
    item = games.resolve(current, query)
    if item is None:
        return {
            'ok': False,
            'error': f'没有找到游戏「{query}」，请先用 list_games 确认。',
            'candidates': [entry['name'] for entry in games.search(current)][:40],
        }
    try:
        folder = sandbox.resolve(current, item['path'])
        entries = sandbox.list_entries(current, folder)
    except (sandbox.SandboxError, OSError) as error:
        return {'ok': False, 'game': item['name'], 'error': str(error)}

    # 取「单次整包上限」与「本轮总输入预算」的较小者：后者是接口的字符硬上限
    # 折算出来的，超了直接 HTTP 413，再大的单次上限也没意义。
    budget = min(
        int(current.get('game_source_max_chars') or 24000),
        int(current.get('input_budget_chars') or 30000),
    )
    focus = str(arguments.get('focus') or '').strip().casefold()
    files = []
    for entry in entries:
        if entry['type'] != 'file':
            continue
        relative = f'{item["path"]}/{entry["name"]}'
        try:
            absolute = sandbox.resolve(current, relative, must_be_file=True)
            text = sandbox.read_text(absolute, budget * 2)
        except (sandbox.SandboxError, OSError):
            continue
        files.append({
            'name': entry['name'], 'path': relative, 'text': text,
            'chars': len(text),
            'hits': text.casefold().count(focus) if focus else 0,
        })
    if not files:
        return {'ok': False, 'game': item['name'], 'error': '该游戏目录下没有可读的源码文件'}

    total = sum(item_['chars'] for item_ in files)
    # 有 focus 时留出三成预算给「片段」：命中最多的文件往往也最大（实测某游戏
    # 的核心 .h 有 111KB），整包装不下就被完全丢掉，而答案恰恰在里面。
    # 与其一字不给，不如把命中点周围的代码摘出来。
    whole_budget = int(budget * 0.7) if focus else budget
    order = sorted(files, key=lambda f: (-_priority(f['name'], f['hits']), f['chars']))
    included, omitted, used = [], [], 0
    for entry in order:
        if used + entry['chars'] <= whole_budget:
            included.append(entry)
            used += entry['chars']
        else:
            omitted.append(entry)
    included.sort(key=lambda f: -_priority(f['name'], f['hits']))

    blocks = []
    for entry in included:
        numbered = '\n'.join(
            f'{number}|{line}'
            for number, line in enumerate(entry['text'].split('\n'), 1)
        )
        blocks.append(f'===== {entry["path"]} =====\n{numbered}')

    excerpted = []
    if focus:
        for entry in sorted(omitted, key=lambda f: -f['hits']):
            if entry['hits'] <= 0 or used >= budget:
                continue
            snippet = _excerpt(entry['text'], focus, budget - used)
            if not snippet:
                continue
            blocks.append(
                f'===== {entry["path"]} （片段：含「{focus}」的部分，非全文）=====\n{snippet}'
            )
            used += len(snippet)
            excerpted.append(entry['path'])

    result = {
        'ok': True,
        'game': item['name'],
        'dir': item['dir'],
        'total_chars': total,
        'files_included': [entry['path'] for entry in included],
        'complete': not omitted,
        'content': '\n\n'.join(blocks),
    }
    if excerpted:
        result['files_excerpted'] = excerpted
    if omitted:
        result['files_omitted'] = [
            {'path': entry['path'], 'chars': entry['chars'],
             **({'focus_hits': entry['hits']} if focus else {})}
            for entry in sorted(omitted, key=lambda f: -f['chars'])
        ]
        result['hint'] = (
            f'该游戏共 {total} 字符，超过单次上限 {budget}，以上不是全部内容。'
            + (f'其中 {"、".join(excerpted)} 只给了含「{focus}」的片段。' if excerpted else '')
            + '结论涉及 files_omitted 里的文件时，必须再用 search_code 或 read_file 确认，'
              '不要因为没读到就当作不存在。'
        )
    return result


def _excerpt(text: str, focus: str, budget: int, context: int = 15) -> str:
    """摘出含 focus 的行及其上下文，重叠区间合并，带真实行号。

    行号必须是原文行号 —— 模型要靠它给出处，错了就等于捏造。
    """
    if budget <= 0:
        return ''
    lines = text.split('\n')
    wanted = set()
    for number, line in enumerate(lines, 1):
        if focus in line.casefold():
            wanted.update(range(max(1, number - context), min(len(lines), number + context) + 1))
    if not wanted:
        return ''
    out, previous = [], 0
    for number in sorted(wanted):
        if previous and number > previous + 1:
            out.append('   …')
        out.append(f'{number}|{lines[number - 1]}')
        previous = number
        if sum(len(item) + 1 for item in out) >= budget:
            out.append('   …（片段已达上限，其余请用 read_file 按行号读取）')
            break
    return '\n'.join(out)


# 挑文件的优先级：规则与小配置永远带上，其次是主逻辑，再按 focus 命中数排。
_NAME_PRIORITY = {
    'rule.md': 100, 'achievements.h': 90, 'options.h': 88, 'option.cmake': 86,
    'mygame.cc': 80,
}


def _priority(name: str, hits: int) -> int:
    base = _NAME_PRIORITY.get(name, 40 if name.endswith('.h') else 30)
    if name == 'unittest.cc':
        base = 20        # 测试有参考价值但最后再考虑
    return base + min(hits, 20) * 2


def _normalize_pattern(value) -> str:
    """把模型常写错的通配符补全。

    实测模型会传 ".cc" / "cc" / "mygame" 这类不含 * 的值，fnmatch 下一个文件都
    匹配不上，搜索静默返回空 —— 比报错更糟，模型会以为「代码里没有」。
    """
    text = str(value or '').strip()
    if not text or text == '*':
        return '*'
    if '*' in text or '?' in text or '[' in text:
        return text
    return f'*{text}' if text.startswith('.') else f'*{text}*'


def _search_code(current: dict, arguments: dict) -> dict:
    query = str(arguments.get('query') or '')
    if not query.strip():
        return {'ok': False, 'error': '缺少 query'}
    scope = str(arguments.get('scope') or '')
    try:
        scope_roots = sandbox.resolve_scope(current, scope)
    except sandbox.SandboxError as error:
        # 模型很自然地会把游戏中文名当 scope 传（scope="天赋云巢"）。与其报越界，
        # 不如按游戏名解析到它的目录 —— 这正是它想表达的范围。
        item = games.resolve(current, scope)
        if item is None:
            return {'ok': False, 'error': str(error)}
        try:
            scope_roots = sandbox.resolve_scope(current, item['path'])
        except sandbox.SandboxError:
            return {'ok': False, 'error': str(error)}
    if not scope_roots:
        return {'ok': False, 'error': 'LGTBot 源码目录不可用，请在面板检查「源码目录」配置'}
    limit = int(current.get('search_max_matches') or 60)
    case_sensitive = bool(arguments.get('case_sensitive'))
    needle = query if case_sensitive else query.casefold()
    pattern = _normalize_pattern(arguments.get('file_pattern'))
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
    result = {
        'ok': True, 'query': query, 'files_scanned': scanned,
        'matches': matches, 'truncated': False,
    }
    if not matches and scope:
        # 窄范围搜空了最危险：模型会直接下「这游戏没有这个东西」的结论。
        # 实测「天赋云巢有没有组合龙」就栽在这 —— 组合龙在 talent_comb_beta 里。
        # 所以搜空时顺手告诉模型这个词在哪些游戏出现过。
        elsewhere = _where_else(current, needle, case_sensitive, pattern)
        if elsewhere:
            result['found_in_other_games'] = elsewhere
            result['hint'] = (
                f'当前范围内没有「{query}」，但它出现在这些游戏里：'
                + '、'.join(f'{item["game"]}({item["dir"]})' for item in elsewhere)
                + '。请确认用户问的是哪一个，必要时改用对应的 scope 重搜。'
            )
        else:
            result['hint'] = f'整个可检索范围内都没有「{query}」，不要臆测，如实告知用户。'
    return result


def _where_else(current: dict, needle: str, case_sensitive: bool, pattern: str) -> list[dict]:
    """在全部游戏里找这个词还出现在哪，返回 (游戏名, 目录, 命中数)，最多 6 个。"""
    try:
        roots = sandbox.resolve_scope(current, 'games')
    except sandbox.SandboxError:
        return []
    if not roots or roots[0]['is_file']:
        return []
    prefix = roots[0]['path'] + '/'
    counts: dict[str, int] = {}
    for absolute in sandbox.iter_files(current, roots, pattern):
        relative = sandbox.relative_of(current, absolute)
        if not relative.startswith(prefix):
            continue
        folder = relative[len(prefix):].split('/', 1)[0]
        if counts.get(folder, 0) >= 50:
            continue
        try:
            with open(absolute, encoding='utf-8', errors='replace') as file:
                for line in file:
                    if needle in (line if case_sensitive else line.casefold()):
                        counts[folder] = counts.get(folder, 0) + 1
        except OSError:
            continue
    catalog = games.index(current)
    ranked = sorted(counts.items(), key=lambda item: -item[1])[:6]
    return [
        {'game': catalog.get(folder, {}).get('name', folder), 'dir': folder, 'hits': hits}
        for folder, hits in ranked
    ]


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
    'read_game_source': _read_game_source,
    'search_code': _search_code,
    'read_file': _read_file,
    'list_dir': _list_dir,
}


async def run(name: str, arguments: dict, current: dict, scope: str = '') -> dict:
    """工具总入口。磁盘 IO 放线程池，避免阻塞事件循环里的消息收发。

    ``scope`` 只有画图用得上：已画图片按「谁问的 + 画什么」缓存，键里要带上它。
    面板试跑不传，落到独立的空 scope，不会撞上群友的缓存。
    """
    if not isinstance(arguments, dict):
        arguments = {}
    # 画图是网络调用，本身就是协程，不能塞进 to_thread。
    # 它的开关判断在 drawing.run 里 —— 那是唯一的执行入口，模型把调用写成 XML
    # 文本被 main.py 代为执行时也要经过这里，只挡 schema 是拦不住的。
    if str(name or '') == drawing.TOOL_NAME:
        return await drawing.run(arguments, current, scope)
    handler = _HANDLERS.get(str(name or ''))
    if handler is None:
        return {'ok': False, 'error': f'未知工具: {name}'}
    try:
        return await asyncio.to_thread(handler, current, arguments)
    except sandbox.SandboxError as error:
        return {'ok': False, 'error': str(error)}
    except (OSError, ValueError) as error:
        return {'ok': False, 'error': f'{type(error).__name__}: {error}'}
