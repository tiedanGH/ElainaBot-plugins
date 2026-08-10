"""LGTBot 游戏问答 —— @机器人即可提问，AI 现场检索 LGTBot 源码后作答。

与 AI 聊天陪伴、AI 开发助手完全独立：自己的配置、自己的上下文库、自己的限流，
共用的只有中央 AI LLM 模块（modules/ai_llm）这一个模型底座。

⚠️ 部署前必须做 bot 绑定
--------------------------------------------------------------------
默认 trigger_mode='at' 时，本插件注册的是 ``.*`` + ``block=True`` 的兜底
handler。而 LGTBot 把玩家的**所有游戏输入**靠 priority=-100 的
``LGTBot 消息派发`` 兜底送进 C++ 引擎（mod/dispatcher.py）。block 在匹配阶段
就 break（core/plugin/_dispatch.py:269），所以两者跑在同一个 bot 上时，本插件
会把游戏派发整条掐断，所有对局失联。

框架的 bot 白名单在 block 判定**之前**执行（_dispatch.py:231），因此只要在
「插件管理 → bot 绑定」里把本插件绑到问答 bot、LGTBot 绑到游戏 bot，两者
互不可见，block 也就伤不到 LGTBot。未绑定时 on_load 会打警告，面板顶部也会
显示红色横幅。
"""
from __future__ import annotations

import asyncio
import os
import re
import time

from core.base.config import cfg
from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import (
    central, config, conflict, games, ratelimit, sandbox, store, tools, webpanel,
)

__plugin_meta__ = {
    'name': 'LGTBot 游戏问答',
    'author': '铁蛋',
    'description': '@机器人提问 LGTBot 游戏规则与结算，AI 现场检索源码后作答',
    'version': '1.2.0',
    'license': 'MIT',
}

log = get_logger(PLUGIN, 'LGTBot游戏问答')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
PAGE_KEY = 'lgtbot-qa'

# 装饰器参数在导入期求值，此时 on_load 还没跑，只能裸读配置文件。
# 改了优先级要重载插件才生效（重载会重新导入本模块）。
PRIORITY = int(config.bootstrap(DATA_DIR, 'priority', 200))

MESSAGE_EVENTS = [
    'GROUP_AT_MESSAGE_CREATE',
    'GROUP_MESSAGE_CREATE',
    'C2C_MESSAGE_CREATE',
    'DIRECT_MESSAGE_CREATE',
]

_ICON = (
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/>'
    '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/></svg>'
)

_last_prune = 0.0


# ==================== 会话与身份 ====================


def _scope(event) -> str:
    """每个用户一条独立上下文，群聊与私聊共用（同一个人问同一件事，接得上）。"""
    appid = str(getattr(event, 'appid', '') or 'default')
    return f'lgtqa:{appid}:{event.user_id}'


def _is_owner(event) -> bool:
    """与框架 core/plugin/_blacklist.py:_is_owner 同源：按 appid 取 owner_ids 比对。"""
    if not getattr(event, 'user_id', ''):
        return False
    bot_cfg = cfg.get_bot_config(str(getattr(event, 'appid', '') or ''))
    return bool(bot_cfg) and event.user_id in (bot_cfg.get('owner_ids') or [])


async def _reply(event, text: str) -> None:
    """群聊里带上 @提问人，避免多人同时问时对不上号。"""
    content = str(text or '').strip()
    if not content:
        return
    if getattr(event, 'is_group', False):
        mention = f'<@{event.user_id}>'
        if not content.startswith(mention):
            content = f'{mention} {content}'
    await event.reply(content)


def _scene_allowed(event, current: dict) -> bool:
    if getattr(event, 'is_group', False):
        if not current.get('group_enabled'):
            return False
        allowed = current.get('allowed_groups') or []
        return not allowed or str(event.group_id) in allowed
    if getattr(event, 'is_direct', False):
        return bool(current.get('direct_enabled'))
    return False


# ==================== 问答主流程 ====================


_LEAKED_TOOL_XML = re.compile(
    r'<\s*(?:' + '|'.join(re.escape(name) for name in tools.TOOL_NAMES) + r')[\s/>]',
    re.IGNORECASE,
)


def _leaked_tool_xml(answer: str) -> bool:
    """答案里是否混着没被执行的工具调用 XML。

    中央模块的 XML 回退协议解析器只认嵌套标签写法，模型写成属性式
    <工具名 参数="值"/> 时一个都匹配不上：raw XML 原样回到 result['text']，
    而且中央层还记为 success。这种文本绝不能发给用户 —— 既难看，又意味着
    这一轮压根没检索过任何代码，答案毫无依据。
    """
    return bool(_LEAKED_TOOL_XML.search(str(answer or '')))


# 真正把内容读进上下文的工具。list_games / list_dir 只给出目录清单，
# 拿它们当「查过代码」的依据是不够的。
_CONTENT_TOOLS = {'read_game_rule', 'read_file', 'search_code'}

# 形如 mygame.cc:120 / lgtbot/games/x/achievements.h:10-24 的出处标注
_CITATION_RE = re.compile(
    r'([\w./\-]+\.(?:cc|cpp|cxx|c|h|hpp|hxx|inl|md|py|cmake|txt|json|ya?ml|sh|js|ts))'
    r'\s*[:：]\s*\d+',
)

# 「完全没调用工具」时允许的最大答案长度。留给「没有查到」「你问的是哪一个？」
# 这类短回复，它们本来就不需要检索。
_NO_TOOL_CHARS = 60

# 「只列了清单、没读任何文件」时允许的最大答案长度。列表类问题（有哪些游戏、
# 某目录下有什么）确实只靠 list_* 就能答，且可能列得比较长，所以放宽到这里；
# 超过这个长度还一个文件都没读，基本就是在展开自己想象的机制了。
_LIST_ONLY_CHARS = 300


def _collect_paths(name: str, result, seen: set) -> None:
    """把工具真实返回过的路径收进 seen，用于事后核对模型给的出处。"""
    if not isinstance(result, dict) or not result.get('ok'):
        return
    own = str(result.get('path') or '')
    if own:
        seen.add(own)
    for item in result.get('matches') or []:
        if isinstance(item, dict) and item.get('path'):
            seen.add(str(item['path']))
    for item in result.get('games') or []:
        if isinstance(item, dict) and item.get('path'):
            seen.add(str(item['path']))
    if name == 'list_dir' and own:
        for item in result.get('entries') or []:
            if isinstance(item, dict) and item.get('name'):
                seen.add(f'{own}/{item["name"]}')


def _fake_citations(answer: str, seen: set, current: dict) -> list:
    """挑出答案里指向**不存在**文件的出处标注。

    实测模型会编出 `<游戏>/game_logic/combinations.py:212-230` 这种路径 —— 整个
    项目是 C++，连 .py 都没有。凡是工具没返回过、且沙箱里也不存在的路径，
    就是凭空捏造，这种答案一个字都不能发出去。

    只按 basename 比对：模型常把完整路径缩写成 `mygame.cc:120`，那是合理写法，
    不该误判成捏造。
    """
    if not answer:
        return []
    known = {item.rsplit('/', 1)[-1] for item in seen}
    fake = []
    for cited in dict.fromkeys(_CITATION_RE.findall(answer)):
        if cited.rsplit('/', 1)[-1] in known:
            continue
        try:
            sandbox.resolve(current, cited, must_be_file=True)
        except sandbox.SandboxError:
            fake.append(cited)
    return fake


def _grounding_problem(answer: str, used: set, seen: set, current: dict) -> str:
    """答案是否站得住脚。返回空串表示通过，否则是记进日志的原因。

    三道闸，都不针对任何具体游戏：
      1. 出处指向不存在的文件 —— 铁证，直接毙掉
      2. 一个工具都没调用还长篇大论 —— 纯凭想象作答
      3. 只列了清单、没读任何文件，却写出长篇机制说明 —— 同样是在编

    闸 2、3 用长度分档，是为了不误伤合理的短回复和列表类回答：
    「没有查到」「你问的是哪个版本？」不需要检索，「已收录 67 个游戏，包括…」
    只靠 list_games 就能答且可能较长。
    """
    fake = _fake_citations(answer, seen, current)
    if fake:
        return f'引用了不存在的文件: {"、".join(fake[:5])}'
    length = len(answer)
    if not used and length > _NO_TOOL_CHARS:
        return f'一个工具都没调用就给出了 {length} 字的回答'
    if used and not (used & _CONTENT_TOOLS) and length > _LIST_ONLY_CHARS:
        return f'只列了清单没读任何文件，却给出了 {length} 字的回答'
    return ''


def _with_disclaimer(answer: str, current: dict) -> str:
    """给 AI 答案附上免责声明。

    只作用于**模型生成的答案**：帮助文案、限流话术、报错提示都不加 —— 那些是
    插件自己的确定性输出，挂上「仅供参考」反而误导。

    用空行分隔，markdown 下 `>` 才会渲染成引用块；纯文本模式下也只是多一个空行，
    不会和正文黏在一起。答案本身已按 answer_max_chars 截断，声明拼在截断之后，
    保证再长的回答也一定带着它。
    """
    text = str(answer or '').strip()
    note = str(current.get('disclaimer') or '').strip()
    if not text or not note:
        return text
    return f'{text}\n\n{note}'


async def _answer(event, question: str) -> None:
    current = config.load()
    if not central.available():
        await _reply(event, central.status()['message'])
        return
    if not sandbox.roots(current):
        await _reply(event, 'LGTBot 源码目录不可用，请管理员在「LGTBot 游戏问答」面板检查配置。')
        return

    user_id = str(event.user_id)
    owner = _is_owner(event)
    allowed, refusal = await ratelimit.check(user_id, current, owner)
    if not allowed:
        await _reply(event, refusal)
        return
    if not ratelimit.acquire(user_id):
        await _reply(event, str(current.get('busy_reply') or '正在处理上一个问题，请稍候。'))
        return

    scope = _scope(event)
    tool_calls = 0
    used_tools: set = set()
    seen_paths: set = set()

    async def tool_handler(name: str, arguments: dict):
        nonlocal tool_calls
        tool_calls += 1
        result = await tools.run(name, arguments, current)
        used_tools.add(str(name))
        _collect_paths(str(name), result, seen_paths)
        return result

    message_id = None
    try:
        await asyncio.to_thread(store.bump, 'questions')
        history = await asyncio.to_thread(
            store.history, scope,
            int(current.get('context_messages') or 8),
            int(current.get('context_expire_seconds') or 3600),
        )
        message_id = await asyncio.to_thread(
            store.append, scope, 'user', question,
            int(current.get('max_stored_messages') or 200),
        )
        payload = [*history, {'role': 'user', 'content': question}]
        system_prompt = central.build_system_prompt(current, _scope_hint(current))
        answer, reason = '', ''
        # 一次正常调用 + 最多一次纠正重试。两次都不合格就宁可报错，也不发
        # 未经检索的答案 —— 编造的规则比「查询失败」危害大得多。
        for attempt in (1, 2):
            correction = ''
            if attempt == 2:
                correction = (
                    central.CORRECTION_PROMPT if _leaked_tool_xml(answer)
                    else central.UNGROUNDED_PROMPT
                )
                log.warning(f'答案不合格({reason})，附纠正指令重试一次')
                await asyncio.to_thread(store.bump, 'regenerations')
                used_tools.clear()
                seen_paths.clear()
            result = await central.ask(
                current, payload,
                f'{system_prompt}\n\n{correction}' if correction else system_prompt,
                tool_handler, scope,
            )
            answer = str(result.get('text') or '').strip()
            if not answer:
                reason = '模型没有返回内容'
            elif _leaked_tool_xml(answer):
                reason = '工具调用写成了属性式 XML，实际没有执行'
            else:
                reason = _grounding_problem(answer, used_tools, seen_paths, current)
            if not reason:
                break
        else:
            await asyncio.to_thread(store.bump, 'ungrounded')
            raise RuntimeError(f'两次作答均不合格: {reason}')
        limit = int(current.get('answer_max_chars') or 1500)
        if len(answer) > limit:
            answer = answer[:limit] + '…'
        await asyncio.to_thread(
            store.append, scope, 'assistant', answer,
            int(current.get('max_stored_messages') or 200),
        )
        await asyncio.to_thread(store.record_usage, user_id)
        await asyncio.to_thread(store.bump, 'answers')
        if tool_calls:
            await asyncio.to_thread(store.bump, 'tool_calls', tool_calls)
        # 免责声明在入库之后才拼：上下文里存的是干净答案，不会随历史回灌给模型
        await _reply(event, _with_disclaimer(answer, current))
    except Exception as error:
        # 失败时撤回刚写入的提问，别在上下文里留下没有答复的半截对话
        if message_id is not None:
            await asyncio.to_thread(store.remove, message_id)
        await asyncio.to_thread(store.bump, 'failures')
        log.warning(f'问答失败: {type(error).__name__}: {error}')
        await _reply(event, '查询失败了，稍后再试一次；如果一直失败请联系管理员。')
    finally:
        ratelimit.release(user_id)


def _scope_hint(current: dict) -> str:
    """把当前真实可检索的范围写进 system prompt，省得模型去试不存在的路径。"""
    items = sandbox.roots(current)
    if not items:
        return ''
    lines = '\n'.join(f'- {item["path"]}（{item["label"]}）' for item in items)
    return (
        '【可检索范围】所有 path 都相对 LGTBot 插件目录，超出这些范围的路径一律会被拒绝：\n'
        f'{lines}'
    )


async def _maybe_prune(current: dict) -> None:
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < 600:
        return
    _last_prune = now
    await asyncio.to_thread(
        store.prune_expired, int(current.get('context_expire_seconds') or 3600),
    )


# ==================== 生命周期 ====================


@on_load
async def initialize() -> None:
    await asyncio.to_thread(config.init, DATA_DIR)
    await asyncio.to_thread(store.connect, DATA_DIR)
    games.invalidate()
    webpanel.register_routes()
    register_page(
        key=PAGE_KEY,
        label='LGTBot 游戏问答',
        source='plugin',
        source_name='LGTBot游戏问答',
        icon=_ICON,
        html_file=os.path.join(BASE_DIR, 'panel.html'),
    )
    current = config.load()
    ready = sandbox.roots(current)
    log.info(
        'LGTBot 游戏问答已加载（触发=%s，可检索范围 %s 个）',
        current.get('trigger_mode'), len(ready),
    )
    if not ready:
        log.warning('LGTBot 源码目录不可用: %s —— 请在面板配置', sandbox.base_dir(current))
    warning = conflict.binding_warning(current)
    if warning:
        log.warning(warning)


@on_unload
async def cleanup() -> None:
    unregister_page(PAGE_KEY)
    ratelimit.clear()
    games.invalidate()
    await asyncio.to_thread(store.close)


# ==================== 指令 ====================


@handler(
    r'^/(?:问答|lgtqa)\s*(?:help|帮助)?$',
    name='LGTBot 问答帮助',
    desc='查看 LGTBot 游戏问答用法',
    priority=PRIORITY + 10,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def help_command(event, _match) -> None:
    current = config.load()
    catalog = await asyncio.to_thread(games.search, current, '')
    mode = str(current.get('trigger_mode') or 'at')
    trigger = {
        'at': '直接 @我 提问即可',
        'prefix': f'发送「{current.get("prefix")} 你的问题」',
        'both': f'直接 @我 提问，或发送「{current.get("prefix")} 你的问题」',
    }.get(mode, '直接 @我 提问即可')
    await _reply(
        event,
        '【LGTBot 游戏问答】\n'
        f'{trigger}\n'
        '我会现场检索游戏源码后回答，规则、玩法、计分结算、成就都能问。\n'
        '/问答 清空 - 清空你的追问上下文\n'
        '/问答 游戏 - 查看收录的游戏数量\n'
        f'已收录 {len(catalog)} 个游戏\n'
        f'今日剩余次数上限：{current.get("daily_limit")} 次/人'
    )


@handler(
    r'^/(?:问答|lgtqa)\s+(?:clear|清空)$',
    name='清空 LGTBot 问答上下文',
    desc='清空当前用户的问答上下文',
    priority=PRIORITY + 10,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def clear_command(event, _match) -> None:
    deleted = await asyncio.to_thread(store.clear, _scope(event))
    await _reply(event, f'已清空你的问答上下文（{deleted} 条）。')


@handler(
    r'^/(?:问答|lgtqa)\s+(?:games|游戏)\s*(\S+)?$',
    name='LGTBot 游戏清单',
    desc='查看已收录的游戏',
    priority=PRIORITY + 10,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def games_command(event, match) -> None:
    current = config.load()
    keyword = str(match.group(1) or '')
    items = await asyncio.to_thread(games.search, current, keyword)
    if not items:
        await _reply(event, f'没有匹配「{keyword}」的游戏。' if keyword else '还没有收录任何游戏。')
        return
    if keyword:
        names = '、'.join(item['name'] for item in items[:30])
        await _reply(event, f'匹配到 {len(items)} 个游戏：\n{names}')
        return
    await _reply(event, f'已收录 {len(items)} 个游戏，直接 @我 问某个游戏的规则或结算即可。')


@handler(
    r'^(?:/问|/ask)\s+([\s\S]+)$',
    name='LGTBot 问答（前缀）',
    desc='带前缀提问 LGTBot 游戏规则与结算',
    priority=PRIORITY + 5,
    event_types=MESSAGE_EVENTS,
    ignore_at_check=True,
    block=True,
)
async def prefix_command(event, match) -> None:
    current = config.load()
    if not current.get('enabled') or str(current.get('trigger_mode')) == 'at':
        return
    if getattr(event, 'is_bot', False) or not _scene_allowed(event, current):
        return
    await _maybe_prune(current)
    await _answer(event, str(match.group(1) or '').strip())


@handler(
    r'(?s)^(.+)$',
    name='LGTBot 问答（@触发）',
    desc='@机器人直接提问 LGTBot 游戏规则与结算',
    priority=PRIORITY,
    event_types=MESSAGE_EVENTS,
    block=True,
)
async def at_message(event, match) -> None:
    """@ 兜底提问。

    刻意**不**设 ignore_at_check —— 框架据此把群内非 @ 消息挡在外面
    （_dispatch.py:238），本 handler 只吃 @机器人 的消息和私聊。
    """
    current = config.load()
    if not current.get('enabled') or str(current.get('trigger_mode')) == 'prefix':
        return
    if getattr(event, 'is_bot', False) or not _scene_allowed(event, current):
        return
    question = str(match.group(1) or '').strip()
    if not question or question.startswith('/'):
        return
    await _maybe_prune(current)
    await _answer(event, question)
