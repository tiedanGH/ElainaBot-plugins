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
import json
import os
import re
import time

from core.base.config import cfg
from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import (
    central, config, conflict, drawing, games, hosting, ratelimit, safety, sandbox, store,
    tools, webpanel,
)

__plugin_meta__ = {
    'name': 'LGTBot 游戏问答',
    'author': '铁蛋',
    'description': '@机器人提问 LGTBot 游戏规则与结算，AI 现场检索源码后作答',
    'version': '1.9.2',
    'license': 'MIT',
}

log = get_logger(PLUGIN, 'LGTBot游戏问答')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
PAGE_KEY = 'lgtbot-qa'

# 装饰器参数在导入期求值，此时 on_load 还没跑，只能裸读配置文件。
# 改了优先级要重载插件才生效（重载会重新导入本模块）。
PRIORITY = int(config.bootstrap(DATA_DIR, 'priority', 200))

# @ 兜底 handler 是否拦截后续插件。
# block 在**匹配阶段**就 break（core/plugin/_dispatch.py:269），函数体提前 return
# 也收不回来。全量群下本 handler 的 `.*` 会匹配每一条消息，于是把群里所有消息
# 都从低优先级插件那儿吞掉 —— 那些插件根本收不到。
# 装在全量群、且同 bot 还有别的插件要处理非 @ 消息时，把这项设为 false。
BLOCK_OTHERS = bool(config.bootstrap(DATA_DIR, 'block_others', True))

_last_swallow_warn = 0.0

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
    """群聊里带上 @提问人，避免多人同时问时对不上号。

    @ 与正文之间用**两个**换行，不是一个：markdown 里单个换行只是软换行，
    渲染出来就是个空格，@ 和正文还是挤在同一行；两个换行才是段落分隔，
    正文开头的列表、引用、标题才能被当成块级元素解析。
    免责声明与正文之间同样用 \\n\\n，口径一致。
    """
    content = str(text or '').strip()
    if not content:
        return
    if getattr(event, 'is_group', False):
        mention = f'<@{event.user_id}>'
        if not content.startswith(mention):
            content = f'{mention}\n\n{content}'
    await event.reply(content)


def _at_ok(event) -> bool:
    """这条消息是否满足「@了本机器人」。

    **只对 GROUP_MESSAGE_CREATE 卡 is_at_self**，不能对所有群消息都卡 ——
    上一版就栽在这里，把非全量群的正常提问全挡住了：

    · GROUP_AT_MESSAGE_CREATE：事件类型本身就代表用户 @ 了 bot。但 is_at_self
      是从 payload 的 mentions 数组推出来的，而 QQ 官方 bot 的 AT_CREATE payload
      **不一定带 mentions**（AT 信号在事件类型里，不在 payload 里重复），
      硬卡这道闸会把这类流量全误挡。框架自己也是这个约定：
      ``at_bot = event.is_at_self if 事件 == GROUP_MESSAGE_CREATE else True``
      （core/bot/event.py:178，注释写着「非全量消息收不到未艾特消息, 一律算艾特」）。
    · GROUP_MESSAGE_CREATE：全量群里的任意消息，只有 is_at_self 才是冲着机器人
      来的，其余是日常聊天，必须挡掉。
    · 私聊 / 频道私信：没有「@」概念，一律放行。

    与 LGTBot 自己的消息派发同一套判据（mod/dispatcher.py 里那道闸）。
    """
    if str(getattr(event, 'event_type', '')) != 'GROUP_MESSAGE_CREATE':
        return True
    return bool(getattr(event, 'is_at_self', False))


def _warn_swallow(event) -> None:
    """全量群里吃到非 @ 消息时告警（每 10 分钟最多一条）。

    走到这里说明该群开了全量（non_at_message），框架没有把非 @ 消息挡掉，
    本 handler 的 `.*` 匹配上了。函数体 return 能保证**不回复**，但
    block 是在匹配阶段就生效的，那条消息对更低优先级的插件已经不可见了。
    真发生了就得让管理员知道，否则只会看到「某插件在这个群失灵」。
    """
    global _last_swallow_warn
    if not BLOCK_OTHERS:
        return          # 没开拦截就不存在吞消息，不用吵
    now = time.monotonic()
    if now - _last_swallow_warn < 600:
        return
    _last_swallow_warn = now
    log.warning(
        f'群 {getattr(event, "group_id", "")} 已开启全量消息，本插件的 @ 兜底 handler '
        f'会匹配并**拦截**非 @ 消息（不会回复，但更低优先级的插件收不到了）。'
        f'同 bot 还有别的插件要处理非 @ 消息时，请在面板把「拦截后续插件」关掉。'
    )


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


# 通用包装标签。实测模型会输出
#   <tool_calls><tool_call><tool_name>x</tool_name><arg_key>k</arg_key>…
# 工具名只作为**文本内容**出现，靠「标签名 = 工具名」那组一个也匹配不到，
# 结果整块 XML 原样发给了用户。
_WRAPPER_TAGS = (
    r'/?tool_calls|/?tool_call|/?tool_name|/?arg_key|/?arg_value'
    r'|/?function_call|/?invoke|/?parameter'
)
_leaked_re = None

# 某些模型（实测 GLM 系）用 DSML 记法，标签名前面还夹一段管道包裹的命名空间：
#   <|DSML|tool_calls> / <|DSML|invoke name="x"> / </|DSML|parameter>
# 这样 `<` 后面直接跟的是 `|`，按标签名匹配一个也命中不到 —— 整块原样发给用户。
# 与其为每种记法各写一套解析，不如先把这层噪声抹掉，让它退化成已经认识的形态。
_NS_CLOSE = re.compile(r'<\s*/\s*\|[^|>]{0,24}\|\s*')
_NS_OPEN = re.compile(r'<\s*\|[^|>]{0,24}\|\s*')


def _normalize_tool_markup(text: str) -> str:
    """抹掉 `<|NS|tag>` 这类命名空间前缀，统一成 `<tag>`。

    只动尖括号紧跟的管道包裹段，不碰正文里的普通竖线（表格、或运算符等）。
    """
    body = str(text or '')
    if '<' not in body or '|' not in body:
        return body
    return _NS_OPEN.sub('<', _NS_CLOSE.sub('</', body))


def _leaked_pattern():
    """懒构建泄漏检测正则。

    **不能在模块级去读 tools.TOOL_NAMES**：框架重载时 _register_pkg 直接复用
    sys.modules 里的旧 app 包、子模块不重新导入（core/plugin/_loader.py:239），
    而 main.py 是重新执行的。子模块一旦处于半初始化状态，模块级的
    `tools.TOOL_NAMES` 就会直接让整个插件 enable 失败：
      module 'plugins.LGTBot游戏问答.app.tools' has no attribute 'TOOL_NAMES'
    推迟到真正用的时候取，那时 tools 一定已经加载完整。
    """
    global _leaked_re
    if _leaked_re is None:
        names = '|'.join(re.escape(name) for name in tools.TOOL_NAMES)
        _leaked_re = re.compile(
            rf'<\s*(?:{names}|{_WRAPPER_TAGS})[\s/>]', re.IGNORECASE,
        )
    return _leaked_re


# 解析通用包装方言用的三条正则。写死标签名、不做嵌套解析 —— 模型产出的这类文本
# 结构很固定，用 ElementTree 反而会因为缺少根节点或未转义字符直接抛错。
_TOOL_CALL_BLOCK = re.compile(r'<tool_call>(.*?)</tool_call>', re.DOTALL | re.IGNORECASE)
_TOOL_NAME_TAG = re.compile(r'<tool_name>\s*(.*?)\s*</tool_name>', re.DOTALL | re.IGNORECASE)
_ARG_PAIR = re.compile(
    r'<arg_key>\s*(.*?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>',
    re.DOTALL | re.IGNORECASE,
)
_TAG_STRIP = re.compile(r'<[^>]*>')

# invoke / parameter 记法（归一化之后）：
#   <invoke name="search_code"><parameter name="query"><![CDATA[crash]]></parameter>
# 实测这类输出经常是**畸形**的：闭合标签里带着下一个参数名
# （</parameter name="scope">），最后一个调用还可能被截断在 CDATA 中间。
# 所以不按标签配对解析，只扫「name="K" 后面跟的第一个值」这个稳定模式 ——
# 开标签闭标签写乱了也不影响。
_INVOKE_ANCHOR = re.compile(
    r'<\s*(?:invoke|function_call|tool_call)\b[^>]*?name\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_PARAM_PAIR = re.compile(
    r'name\s*=\s*["\']([^"\']+)["\']\s*>?\s*'
    r'(?:'
    # ① 完整 CDATA：优先，且内部允许出现 `<`（CDATA 的意义就在这）
    r'<!\[CDATA\[(.*?)\]\]>'
    # ② 被截断的 CDATA（没有 `]]>`）：取到下一个标签为止，
    #    否则会把尾随的 `</arg_value>` 之类当成值的一部分
    r'|<!\[CDATA\[([^<]*)'
    # ③ 没用 CDATA 的裸值
    r'|([^<]*)'
    r')',
    re.DOTALL | re.IGNORECASE,
)


def _parse_invoke_calls(text: str) -> list:
    """解析 invoke/parameter 记法，容忍畸形与截断。

    切分方式：以每个 invoke 锚点为界，锚点之后到下一个锚点之前的文本就是它的
    参数区。这样即便闭合标签全乱、末尾被截断，前面已经完整的调用依然能救出来 ——
    模型的检索意图是对的，没必要因为它把标签写坏就作废整轮。
    """
    anchors = list(_INVOKE_ANCHOR.finditer(text))
    calls = []
    for index, anchor in enumerate(anchors):
        name = anchor.group(1).strip()
        if name not in tools.TOOL_NAMES:
            continue
        end = anchors[index + 1].start() if index + 1 < len(anchors) else len(text)
        arguments = {}
        for key, whole, cut, plain in _PARAM_PAIR.findall(text[anchor.end():end]):
            key = key.strip()
            if key and key not in arguments:
                arguments[key] = _coerce(whole or cut or plain)
        calls.append((name, arguments))
        if len(calls) >= 8:
            break
    return calls


def _coerce(value: str):
    """XML 里所有参数都是字符串，把 true/false 还原成 bool。

    不还原的话 case_sensitive="false" 会变成真值（bool('false') is True），
    搜索行为跟模型的意图正好相反。
    """
    text = str(value or '').strip()
    low = text.casefold()
    if low in ('true', 'false'):
        return low == 'true'
    return text


def _parse_leaked_tool_calls(answer: str) -> list:
    """从答案文本里把「写成了 XML 却没被执行」的工具调用解析出来。

    支持实测见过的三种写法（都先过一遍 _normalize_tool_markup 抹掉命名空间前缀）：
      <tool_call>list_games</tool_call>                     ← 裸工具名
      <tool_call><tool_name>x</tool_name><arg_key>…         ← 键值标签
      <invoke name="x"><parameter name="k"><![CDATA[v]]>    ← invoke 记法（含 DSML）

    只接受 TOOL_NAMES 里的工具名，最多 8 个 —— 与中央模块的上限一致，
    也避免模型一口气刷出几十个调用。
    """
    text = _normalize_tool_markup(answer)
    calls = _parse_invoke_calls(text)
    if calls:
        return calls
    for block in _TOOL_CALL_BLOCK.findall(text):
        named = _TOOL_NAME_TAG.search(block)
        if named:
            name = named.group(1).strip()
            arguments = {
                key.strip(): _coerce(value)
                for key, value in _ARG_PAIR.findall(block)
            }
        else:
            name = _TAG_STRIP.sub('', block).strip()
            arguments = {}
        if name in tools.TOOL_NAMES:
            calls.append((name, arguments))
        if len(calls) >= 8:
            break
    return calls


# 中央模块在 XML 回退协议下，会用 '[请求执行工具]' 占住模型只发工具调用、没有正文
# 的那一轮（modules/ai_llm/app/service.py:1888）。模型在后续轮次看到自己「说过」
# 这句，就可能照抄成最终答案 —— 实测发生过，用户收到的整条回复就是这七个字。
# 凡是「整条答案只有一个方括号标记」的，一律是这类内部占位符，不能当答案。
_PLACEHOLDER_ANSWER = re.compile(r'^\s*[\[【][^\]】]{0,40}[\]】]\s*$')


def _placeholder_answer(answer: str) -> bool:
    return bool(_PLACEHOLDER_ANSWER.match(str(answer or '')))


def _leaked_tool_xml(answer: str) -> bool:
    """答案里是否混着没被执行的工具调用 XML。

    中央模块的 XML 回退协议解析器只认嵌套标签写法，模型写成属性式
    <工具名 参数="值"/> 时一个都匹配不上：raw XML 原样回到 result['text']，
    而且中央层还记为 success。这种文本绝不能发给用户 —— 既难看，又意味着
    这一轮压根没检索过任何代码，答案毫无依据。
    """
    # 先归一化：DSML 之类的 `<|NS|tag>` 记法不抹掉的话，`<` 后面跟的是 `|`，
    # 按标签名一个也匹配不到，整块会被当成正常答案发出去。
    return bool(_leaked_pattern().search(_normalize_tool_markup(answer)))


# 真正把内容读进上下文的工具。list_games / list_dir 只给出目录清单，
# 拿它们当「查过代码」的依据是不够的。
_CONTENT_TOOLS = {'read_game_rule', 'read_game_source', 'read_file', 'search_code'}

# 形如 mygame.cc:120 / lgtbot/games/x/achievements.h:10-24 的出处标注
_CITATION_RE = re.compile(
    r'([\w./\-]+\.(?:cc|cpp|cxx|c|h|hpp|hxx|inl|md|py|cmake|txt|json|ya?ml|sh|js|ts))'
    r'\s*[:：]\s*\d+',
)

# 「完全没调用工具」时允许的最大答案长度。留给「没有查到」「你问的是哪一个？」
# 这类短回复，它们本来就不需要检索。
_NO_TOOL_CHARS = 60

# 同上，但用于**没有点名任何具体游戏**的问题（「LGTBot 是什么」这类元问题）。
# 这类问题没有对应的游戏源码可读，权威事实已由 central.PROJECT_FACTS 预置进
# 提示词，零检索作答本就合理 —— 实测「LGTBot 是什么」正是卡在 60 字死线上连挂两次。
# 仍设上限而不是完全放开：借元问题的壳写长篇机制说明，照样要拦。
_NO_TOOL_META_CHARS = 500

# 「只列了清单、没读任何文件」时允许的最大答案长度。列表类问题（有哪些游戏、
# 某目录下有什么）确实只靠 list_* 就能答，且可能列得比较长，所以放宽到这里；
# 超过这个长度还一个文件都没读，基本就是在展开自己想象的机制了。
_LIST_ONLY_CHARS = 300


# 机制类问题的通用词汇（领域词，不是游戏名）。带上这些词就说明答案必须来自源码，
# 哪怕问题里那个游戏名根本不存在 —— 否则模型可以对着一个虚构游戏编 500 字。
_MECHANIC_WORDS = (
    '结算', '计分', '得分', '分数', '积分', '规则', '玩法', '怎么玩', '成就',
    '倍率', '判定', '触发', '顺序', '流程', '胜负', '输赢', '扣血', '血量',
    '天赋', '技能', '卡牌', '回合', '阶段', '选项', '配置',
)


def _mentions_game(question: str, current: dict) -> bool:
    """问题里有没有点名索引中**真实存在**的游戏。

    数据驱动：拿真实索引比对中文名与目录名，不写死任何游戏名。
    """
    text = str(question or '').casefold()
    if not text:
        return False
    for item in games.index(current).values():
        name = str(item.get('name') or '')
        folder = str(item.get('dir') or '')
        if len(name) >= 2 and name.casefold() in text:
            return True
        if len(folder) >= 3 and folder.casefold() in text:
            return True
    return False


def _needs_source(question: str, current: dict) -> bool:
    """这个问题是否必须以源码为依据（**零检索**闸用）。

    两种情况算「需要源码」：
      1. 点名了索引里真实存在的游戏
      2. 带机制类词汇（结算 / 计分 / 成就 / 触发 …）—— 即使点的游戏名不存在，
         也不能让模型凭空展开，否则等于对着虚构游戏编造

    都不沾边的才算元问题（「LGTBot 是什么」「谁开发的」），那类没有源码可读，
    权威事实由 central.PROJECT_FACTS 预置，零检索作答是合理的。
    """
    text = str(question or '').casefold()
    if not text:
        return True      # 判断不了就从严：宽松通道只发给能确认是元问题的提问
    if any(word in text for word in _MECHANIC_WORDS):
        return True
    return _mentions_game(question, current)


# 画图前必须先读源码时给模型的指令。措辞要能直接指导下一步动作 ——
# 只说「不行」，模型往往就放弃画图去写一段文字解释了。
_DRAW_SOURCE_FIRST = (
    '这个画图请求牵涉到具体游戏，不能凭游戏名想象。请先用 read_game_source'
    '（或 read_game_rule）读到该游戏里角色、场景、道具、棋盘的**真实描述**，'
    '再把读到的具体特征写进画面描述，重新调用画图。'
)


def _draw_needs_source(question: str, arguments: dict, current: dict, used: set) -> str:
    """画图之前是否必须先检索源码。返回空串表示可以直接画。

    判据只有一条：**这次请求点到了索引里真实存在的游戏**。点到了就必须先读内容，
    否则模型只能按游戏名去脑补角色长什么样、棋盘是什么样子 —— 画出来跟游戏毫无
    关系，而用户要的恰恰是「这个游戏里的东西」。

    与接地闸同源：数据驱动，不写死任何游戏名。问题里没点游戏的（画只猫、画个风景）
    照常直接画，不平白多绕一轮。

    连模型自己填的 prompt 一起看：追问式的「画一张这个游戏的插画」问题里没有游戏名，
    但模型展开后的描述里通常有。
    """
    if used & _CONTENT_TOOLS:
        return ''
    text = f'{question} {(arguments or {}).get("prompt") or ""}'
    return _DRAW_SOURCE_FIRST if _mentions_game(text, current) else ''


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
    # read_game_rule 的 source_files、read_game_source 的 files_included 与
    # files_excerpted，都是模型真实看到过内容的路径，引用它们不算捏造。
    # 漏掉 files_excerpted 会把「引用片段里的行号」误判成捏造，答案直接被毙。
    for key in ('source_files', 'files_included', 'files_excerpted'):
        for item in result.get(key) or []:
            if isinstance(item, str):
                seen.add(item)
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


def _grounding_problem(
    answer: str, used: set, seen: set, current: dict, question: str = '',
    drew: bool = False,
) -> str:
    """答案是否站得住脚。返回空串表示通过，否则是记进日志的原因。

    三道闸，都不针对任何具体游戏：
      1. 出处指向不存在的文件 —— 铁证，直接毙掉
      2. 一个工具都没调用还长篇大论 —— 纯凭想象作答
      3. 只列了清单、没读任何文件，却写出长篇机制说明 —— 同样是在编

    画图成功时闸 2、3 全部让路（``drew``）：那种回答的本体是图，配的文字本来就
    只有一句话，没有「必须来自源码」的成分。不放行的话，「画一张某某游戏的插画」
    会同时踩中「点名了具体游戏」和「没调用内容工具」，被判臆造毙掉，
    重试还会**再画一张**，白烧一次额度还多发一条图。
    闸 1 仍然生效 —— 编造出处任何时候都不能放过。

    闸 2、3 用长度分档，是为了不误伤合理的短回复和列表类回答：
    「没有查到」「你问的是哪个版本？」不需要检索，「已收录 67 个游戏，包括…」
    只靠 list_games 就能答且可能较长。

    闸 2 还要看问题**有没有点名具体游戏**：没点名的元问题（「LGTBot 是什么」）
    根本没有对应源码可读，权威事实由 central.PROJECT_FACTS 预置，零检索作答合理，
    所以放宽到 _NO_TOOL_META_CHARS。点名了游戏仍然从严 —— 那种问题必须来自源码。
    """
    fake = _fake_citations(answer, seen, current)
    if fake:
        return f'引用了不存在的文件: {"、".join(fake[:5])}'
    if drew:
        return ''
    length = len(answer)
    if not used:
        if _needs_source(question, current):
            if length > _NO_TOOL_CHARS:
                return f'问题需要源码依据却一个工具都没调用，就给出了 {length} 字的回答'
        elif length > _NO_TOOL_META_CHARS:
            return f'一个工具都没调用就给出了 {length} 字的回答'
    # 闸 3 只在**点名了具体游戏**时生效，判据比闸 2 更窄。
    # list_games 会连每个游戏的简介一起返回，所以「推荐几款休闲的多人游戏」这类
    # 浏览/推荐问题，仅凭清单作答完全站得住，而且天然会长（要覆盖几十个游戏）。
    # 生产实测踩过：一段 466 字的推荐被这道闸误杀，重试后模型反而叫用户自己去
    # 运行 list_games，那条错的短回答倒过了闸发出去了。
    # 点名了具体游戏还只看清单，才是真的在编 —— 那种情况必须拦。
    if (
        used and not (used & _CONTENT_TOOLS)
        and length > _LIST_ONLY_CHARS
        # 问题为空时判断不了，从严 —— 与闸 2 的 fail-safe 取向一致
        and (not str(question or '').strip() or _mentions_game(question, current))
    ):
        return f'点名了具体游戏却只列清单没读文件，就给出了 {length} 字的回答'
    return ''


_OVERFLOW_MARKERS = (
    'context_length_exceeded', '413', '超过最大长度', '超最大字符限制',
    'maximum context length', 'too many tokens', 'input too long', '10030301',
)


def _context_overflow(error: Exception) -> bool:
    """是不是撞上了接口的输入长度硬上限。

    这类失败重试没用 —— 同样的输入还是超。必须让管理员把预算调下来，
    所以要和普通网络故障区分开，日志给出可操作的指引。
    """
    text = str(error).casefold()
    return any(marker.casefold() in text for marker in _OVERFLOW_MARKERS)


# 截断后必须附的说明。措辞很重要：模型看到残缺内容时，默认会当成「源码里就这些」，
# 进而下错结论 —— 必须明说这是长度限制导致的，并给出补查的办法。
_TRUNCATED_NOTE = (
    '本次结果因输入上限被截断，不代表源码里只有这些。'
    '需要更多内容时缩小范围再查（如指定 focus 或用 read_file 按行号读）。'
)


def _tool_budget(current: dict, payload: list, system_prompt: str) -> int:
    """本轮工具结果还能占多少字符。

    ``input_budget_chars`` 是**整个请求**的输入上限（对标接口的硬限制）。
    工具结果的额度要把这一轮真实的固定开销全扣掉：

      系统提示词 + 工具 schema + 中央模块追加的 XML 协议提示词 + 历史 + 问题

    实测这些合计约 9000 字符（其中 tools schema 就有 2700）。写死一个估值不靠谱 ——
    历史长了照样 413，所以每轮实测扣减，历史变长会自动收紧。
    """
    limit = int(current.get('input_budget_chars') or 34000)
    fixed = len(system_prompt) + _result_size(payload) + _tools_schema_chars(current)
    return max(2000, limit - fixed - _PROTOCOL_RESERVE)


# 中央模块追加的 XML 兼容协议提示词，也算在输入里
_PROTOCOL_RESERVE = 1200
_schema_chars: dict = {}


def _tools_schema_chars(current: dict) -> int:
    """tools schema 序列化后的字符数（中央模块会把它原样发给接口，算输入）。

    按「画图开没开」分别缓存 —— 工具清单会随这个开关变长变短，一个数缓存下来
    就会算错预算。开关是布尔量，缓存最多两条，不必做失效。

    与 _leaked_pattern 同理：懒取，不在模块级碰 tools 的属性 —— 重载时子模块
    可能处于半初始化状态，模块级访问会让整个插件 enable 失败。
    """
    key = bool(current.get('draw_enabled'))
    if key not in _schema_chars:
        _schema_chars[key] = len(json.dumps(tools.schema_for(current), ensure_ascii=False))
    return _schema_chars[key]


def _result_size(result) -> int:
    """工具结果回灌给模型时占的字符数（中央模块用 json.dumps 序列化）。"""
    try:
        return len(json.dumps(result, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(result))


def _fit_result(result: dict, allowance: int) -> dict:
    """把工具结果压进剩余预算。

    接口对**输入总字符**有硬上限（实测 40000，超了直接 HTTP 413 整轮报废），
    而工具结果是一轮轮累加进 messages 的。所以不能只管单次调用的大小，
    必须按本轮累计预算裁剪。

    裁剪顺序：先砍最占地方的正文 content，再截列表类结果，且一定要告诉模型
    「这里被截断了」—— 否则它会把截断当成「代码里就这些」，得出错误结论。
    """
    # 预算见底：只回一句极短的说明。这条不能省 —— 模型必须知道「不是没有内容，
    # 而是这轮读不下了」，否则会当作源码里就没有。
    if allowance < 120:
        return {'ok': True, 'truncated': True, 'note': '本轮检索已达输入上限。'}
    trimmed = dict(result)
    body = str(trimmed.get('content') or '')
    if body:
        marker = '\n…（内容因输入上限被截断，未展示完）'
        # 直接拿「正文清空、但 note/truncated 都已就位」的副本量一次真实开销。
        # 手算容易漏掉后加字段的键名与 JSON 引号，一漏就刚好越界、白白退化成最小集。
        probe = dict(trimmed, content='', truncated=True, note=_TRUNCATED_NOTE)
        keep = max(0, allowance - _result_size(probe) - len(marker))
        if keep < len(body):
            trimmed['truncated'] = True
            trimmed['note'] = _TRUNCATED_NOTE
            trimmed['content'] = body[:keep] + marker
            # JSON 转义会让正文膨胀（换行 \n、引号 \" 各占两字符），按估算切完
            # 仍可能差几个字符越界。量一次真实序列化长度再收紧，避免为了一两个
            # 字符就退化成信息量小得多的最小集。
            for _ in range(3):
                excess = _result_size(trimmed) - allowance
                if excess <= 0:
                    break
                keep = max(0, keep - excess - 8)
                trimmed['content'] = body[:keep] + marker
    for key in ('matches', 'games', 'entries'):
        items = trimmed.get(key)
        while isinstance(items, list) and items and _result_size(trimmed) > allowance:
            items = items[: max(1, len(items) // 2)]
            trimmed[key] = items
            trimmed['truncated'] = True
    if trimmed.get('truncated'):
        trimmed['note'] = _TRUNCATED_NOTE
    if _result_size(trimmed) <= allowance:
        return trimmed
    # 光截正文还不够 —— files_omitted / hint 这些结构性字段本身就可能超预算。
    # 退回到只保留「模型还能用得上」的最小集，硬压进 allowance。
    minimal = {
        key: trimmed[key] for key in ('ok', 'game', 'dir', 'path', 'files_included')
        if key in trimmed
    }
    minimal['truncated'] = True
    minimal['note'] = '结果因输入上限被大幅截断，需要细节请缩小范围重查。'
    overflow = _result_size(minimal) - allowance
    if overflow > 0 and isinstance(minimal.get('files_included'), list):
        minimal.pop('files_included')
    body = str(trimmed.get('content') or '')
    room = allowance - _result_size(minimal)
    if body and room > 40:
        minimal['content'] = body[:room - 40] + '…（截断）'
    return minimal


async def _bump(key: str, amount: int = 1) -> None:
    """写统计，失败也不抛。

    统计只是观测数据，绝不能因为它把主流程带崩 —— 实测踩过：热重载关掉了
    存储连接，异常处理里的 store.bump 又抛一次，_reply 根本没执行，
    用户一个字都收不到。
    """
    try:
        await asyncio.to_thread(store.bump, key, amount)
    except Exception as error:  # noqa: BLE001 — 观测数据不值得中断主流程
        log.debug(f'统计写入失败({key}): {error}')


def _transcript_text(transcript: list, limit: int = 30000) -> str:
    """把这一轮真实拿到的工具结果拼成给模型看的文本。

    倒序截断：越靠后的检索越贴近模型当时的意图，超预算时优先保住它们。
    """
    blocks, used = [], 0
    for name, result in reversed(transcript):
        body = json.dumps(result, ensure_ascii=False, default=str)
        if used + len(body) > limit:
            body = body[: max(0, limit - used)] + '…（已截断）'
        if not body:
            break
        blocks.append(f'【工具 {name} 的结果】\n{body}')
        used += len(body)
        if used >= limit:
            break
    return '\n\n'.join(reversed(blocks))


async def _input_rejected(current: dict, text: str) -> bool:
    """用户提问的入口审核。审核不可用时按 moderation_fail_closed 决定放不放行。"""
    if not current.get('moderation_enabled'):
        return False
    result = await central.moderate_input(current, text)
    if result.get('flagged'):
        log.warning('用户输入被内容安全审核拦截')
        return True
    if not result.get('available') and current.get('moderation_fail_closed'):
        log.warning(f'输入审核不可用，按配置阻断: {result.get("error", "")}')
        return True
    return False


async def _output_rejected(current: dict, text: str) -> bool:
    """AI 回答的出口审核。

    与入口不同，这里**永远 fail-closed**：审核不可用就不发。发出去的每个字都算
    机器人自己说的，而回答素材来自玩家上传的游戏源码，宁可回一句「未通过检查」
    也不能在审核失灵时裸奔。这一点与 AI 聊天陪伴的处理一致。
    """
    if not current.get('moderation_enabled') or not str(text or '').strip():
        return False
    result = await central.moderate_output(current, text)
    if result.get('flagged'):
        log.warning('AI 输出被内容安全审核拦截')
        return True
    if not result.get('available'):
        log.warning(f'输出审核不可用，按严格策略阻断: {result.get("error", "")}')
        return True
    return False


def _with_reason(message: str, error: Exception, current: dict) -> str:
    """报错话术后面附上具体原因。

    失败原因是插件自己生成的确定性文本（接地闸的判定语、超长提示等），不经模型，
    所以**不需要过内容审核** —— 审核针对的是模型产出。

    但仍要过一遍 IP 脱敏并截断：异常有可能包着接口返回的原始报文，
    里面可能带内网地址或一长串 JSON，不适合原样丢给群友。
    """
    text = str(message or '').strip()
    if not current.get('error_detail_enabled', True):
        return text
    detail = safety.redact_ips(str(error or '')).strip().replace('\n', ' ')
    if not detail:
        return text
    limit = int(current.get('error_detail_chars') or 200)
    if len(detail) > limit:
        detail = detail[:limit] + '…'
    return f'{text}\n> 原因：{detail}'


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


# Markdown 图片消息里的 alt。QQ 的写法是 `![alt #宽px #高px](url)`，
# alt 里不能出现方括号，否则整条语法被破坏。
_IMAGE_ALT = '画图结果'
_SIZE_RE = re.compile(r'^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$', re.IGNORECASE)


def _pixel_size(size: str) -> tuple:
    """把 AI 画图给的 `1024x1024` 解析成 (宽, 高)。

    解析不出来就按 1024 见方走：这两个数只是给客户端排版的提示，
    宽高比不准最多显示时略有拉伸，不该为它拦下一张已经画好的图。
    """
    match = _SIZE_RE.match(str(size or ''))
    if not match:
        return 1024, 1024
    return int(match.group(1)), int(match.group(2))


def _markdown_image(event, item: dict) -> str:
    """@发起人 + 换行 + 图片的 Markdown 消息（与 AI 画图同一套写法）。

    富媒体图片消息（msg_type=7）**不解析** `<@openid>`，群里多人同时问就对不上号；
    Markdown 图片消息能带 @，所以图床可用时优先走这条。
    """
    width, height = _pixel_size(item.get('size', ''))
    body = f'![{_IMAGE_ALT} #{width}px #{height}px]({item["url"]})'
    if getattr(event, 'is_group', False) or getattr(event, 'is_channel', False):
        return f'<@{event.user_id}>\n{body}'
    return body


def _delivered(response) -> bool:
    """平台失败时框架也可能**不抛异常**，只返回 None 或不带消息 ID 的响应体
    （图片下不动、格式不认、域名没报备、被风控都是这样）。
    光看有没有异常会把失败记成成功，也就不会触发退回富媒体。
    """
    return isinstance(response, dict) and bool(response.get('id'))


async def _deliver_images(event, images: list) -> None:
    """把这一轮画出来的图发给用户。逐张出队，重复调用不会重发。

    两条投递路线，与 AI 画图一致：

      1. 图床可用 → Markdown 图片消息：`<@发起人>` 换行 `![alt #宽px #高px](链接)`
      2. 图床不可用、或 Markdown 没送达 → 富媒体图片消息（`reply_image`）

    为什么优先 Markdown：富媒体消息不解析 `<@openid>`，群里多人同时提问时
    收到一张没头没尾的图，根本对不上是谁问的。

    Markdown 失败的典型原因是**图床域名没在 QQ 开放平台报备**，或上游那次上传
    没成功、给回来的其实是接口原始链接。这两种都能靠退回富媒体救回来。

    图**单独发一条**，不塞进答案正文：富媒体不渲染引用块，Markdown 消息里
    塞长正文也会把免责声明的格式弄乱。分开发就各归各的。

    全程吞异常：图已经画出来了（对方额度也扣过了），发不出去顶多少一张图，
    绝不能让它把整轮问答带崩 —— 那样用户连文字回复都收不到。
    """
    for item in list(images):
        images.remove(item)
        url = str(item.get('url') or '')
        if not url:
            continue
        if hosting.available():
            try:
                # skip_suffix：不要在图片消息后面再拼插件签名之类的尾巴
                response = await event.reply(
                    _markdown_image(event, item), msg_type=2, skip_suffix=True,
                    force_verify_image_resource=True,
                )
            except Exception as error:  # noqa: BLE001 — 见 docstring
                response = None
                log.warning(f'Markdown 图片消息发送异常: {type(error).__name__}: {error}')
            if _delivered(response):
                await _bump('draws')
                continue
            log.warning(f'Markdown 图片消息未送达，改发富媒体（检查图床域名是否已报备）: {url[:120]}')
        try:
            response = await event.reply_image(url)
        except Exception as error:  # noqa: BLE001 — 见 docstring
            log.warning(f'画图结果发送失败: {type(error).__name__}: {error}')
            continue
        if not _delivered(response):
            log.warning(f'画图结果未能送达（平台未返回消息 ID）: {url[:120]}')
            continue
        # 统计放在这里而不是成功路径里：无论这一轮问答最终成功还是报错，
        # 只要图真的发出去了就该记一笔，两边各记一次反而对不上。
        await _bump('draws')


async def _resumable(
    make_call, current: dict, payload: list, system_prompt: str,
    transcript: list, used_tools: set, budget: int, scope: str,
):
    """带工具的模型调用，失败时**从已检索到的结果续答**，而不是整轮作废。

    一次问答常常已经跑了十几个工具（读规则、读源码、搜标识符…），中途一次
    524 / 空响应就把这些全丢掉太亏 —— 而且用户还白等了几十秒。

    两层保护：
      · central._bounded 已经对**瞬时**故障做了退避重试（524 / 429 / 超时 /
        空消息…），大多数抖动在那一层就消化掉了
      · 重试用尽仍失败时，只要**已经读到过源码**，就改用无工具合成：把
        transcript 交给模型直接写答案。这一轮不带工具、上下文也小得多，
        成功率明显更高，检索成果也保住了

    只有「一次源码都没读到」或「错误是超长这类重试无用的硬失败」才真的上抛。
    """
    try:
        return await make_call()
    except Exception as error:
        if _context_overflow(error) or not (used_tools & _CONTENT_TOOLS) or not transcript:
            raise
        log.warning(
            f'模型调用失败（{str(error)[:120]}），但已检索到 {len(transcript)} 份结果，'
            f'改用无工具合成续答'
        )
        await _bump('resumed')
        return await central.synthesize(
            current, payload, system_prompt,
            _transcript_text(transcript, budget), scope,
        )


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
    slot = ratelimit.acquire(user_id, current)
    if slot == 'self':
        await _reply(event, str(current.get('busy_reply') or '正在处理上一个问题，请稍候。'))
        return
    if slot == 'global':
        # 全局并发满了。宁可直接拒也不排队 —— 排队只会让所有人一起等，
        # 而 QQ 那边的被动消息额度还在倒计时。
        log.info(f'全局并发已满（{ratelimit.active()}），拒绝一次提问')
        await _bump('busy_global')
        await _reply(event, str(current.get('busy_global_reply') or '现在问的人有点多，稍等一会儿再来～'))
        return

    scope = _scope(event)
    # 入口审核放在限流占位之后、调用模型之前：违规提问不该消耗模型额度，
    # 也不该进上下文库。占位已经拿到，所以要走 finally 释放 —— 用 try 包住。
    tool_calls = 0
    used_tools: set = set()
    seen_paths: set = set()
    transcript: list = []
    drawn: list = []  # 画出来的图片链接，等文字答复发完再逐张发出去
    draws = 0
    budget = 0        # 工具结果额度，进 try 后按本轮实际开销算出
    spent = 0

    async def tool_handler(name: str, arguments: dict):
        nonlocal tool_calls, spent, draws
        tool_calls += 1
        reuse = False
        if str(name) == drawing.TOOL_NAME:
            # 牵涉具体游戏的画图，必须先把游戏内容读出来 —— 挡在这里而不是只写进
            # 提示词：提示词能被无视，这道闸不能。挡下来不算一次画图额度。
            hold = _draw_needs_source(question, arguments, current, used_tools)
            if hold:
                used_tools.add(str(name))
                log.info('画图请求牵涉具体游戏，要求先检索源码')
                return {'ok': False, 'error': hold}
            # 复用已画好的图不占额度。少了这一步，被超时打断后的重试会卡在
            # 「本次已经画过 1 张」上 —— 明明有现成的图，却一张都发不出去。
            reuse = await drawing.reusable(arguments, current, scope)
            # 单次问答的画图次数上限。模型可以在 max_tool_rounds 轮里反复调用，
            # 不在这儿卡死的话，一个提问就能把 AI 画图的日额度掏空、并连发十条图。
            cap = int(current.get('draw_max_per_question') or 1)
            if not reuse and draws >= cap:
                used_tools.add(str(name))
                return {'ok': False, 'error': f'本次回答已经画过 {cap} 张图了，不能再画。'}
            if not reuse:
                draws += 1
        result = await tools.run(name, arguments, current, scope)
        used_tools.add(str(name))
        if str(name) == drawing.TOOL_NAME:
            # 链接摘出来自己发，不回灌给模型（理由见 drawing.detach_image）
            image = drawing.detach_image(result)
            if image:
                if image.get('reused'):
                    await _bump('draws_reused')
                # 同一张图别发两遍：模型重复调用、或重试命中缓存都会走到这儿
                if not any(item['url'] == image['url'] for item in drawn):
                    drawn.append(image)
            elif not reuse:
                draws -= 1   # 没出图就不占额度，让模型换个描述还能再试
            # 结果只剩一句话，占不了多少，但仍要计进预算 —— 漏算就等于给 413 让路。
            # 不进 transcript：合成兜底要的是源码，一句「图已生成」没有合成价值。
            spent += _result_size(result)
            return result
        # 先按剩余预算裁剪再回灌：工具结果是一轮轮累加进 messages 的，
        # 不控总量迟早撞上接口的输入字符硬上限（HTTP 413，整轮报废）
        if isinstance(result, dict) and result.get('ok'):
            size = _result_size(result)
            if spent + size > budget:
                log.info(f'工具 {name} 结果 {size} 字符超出剩余预算，已裁剪')
                result = _fit_result(result, budget - spent)
                size = _result_size(result)
            spent += size
            _collect_paths(str(name), result, seen_paths)
            # 留一份检索结果：作答失败时靠它走「不带工具的合成兜底」
            transcript.append((str(name), result))
        else:
            _collect_paths(str(name), result, seen_paths)
        return result

    message_id = None
    try:
        await _bump('questions')
        if await _input_rejected(current, question):
            await _bump('blocked_input')
            await _reply(event, str(current.get('moderation_blocked_response') or ''))
            return
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
        budget = _tool_budget(current, payload, system_prompt)
        answer, reason = '', ''
        # 第一轮带工具正常检索；不合格时分两条路补救，两条都失败才报错 ——
        # 编造的规则比「查询失败」危害大得多。
        for attempt in (1, 2):
            synthesizing = False
            if attempt == 1:
                result = await _resumable(
                    lambda: central.ask(
                        current, payload, system_prompt, tool_handler, scope,
                    ),
                    current, payload, system_prompt, transcript, used_tools,
                    budget, scope,
                )
            elif used_tools & _CONTENT_TOOLS:
                synthesizing = True
                # 注意条件是「调用过**内容**工具」，不是「transcript 非空」——
                # list_games / list_dir 只给目录清单，拿它去合成只会得出「没有查到」。
                # 实测踩过：模型只调了 list_games 就作答，被闸拦下后我却拿这份清单
                # 去合成，于是回了「没有查到关于…的规则」，而代码其实在那儿。
                # 只有真读到过源码，合成才有意义；否则要让它带着工具重新去查。
                log.warning(f'答案不合格({reason})，改用无工具合成兜底')
                await _bump('synthesized')
                result = await central.synthesize(
                    current, payload, system_prompt,
                    _transcript_text(transcript, budget), scope,
                )
            else:
                # 一次都没检索成功，工具那步就崩了 —— 带纠正指令重来
                log.warning(f'答案不合格({reason})，附纠正指令重试一次')
                await _bump('regenerations')
                used_tools.clear()
                seen_paths.clear()
                corrected = (
                    f'{system_prompt}\n\n'
                    + (central.CORRECTION_PROMPT if _leaked_tool_xml(answer)
                       else central.UNGROUNDED_PROMPT)
                )
                result = await _resumable(
                    lambda: central.ask(
                        current, payload, corrected, tool_handler, scope,
                    ),
                    current, payload, system_prompt, transcript, used_tools,
                    budget, scope,
                )
            answer = str(result.get('text') or '').strip()
            if not answer:
                reason = '模型没有返回内容'
            elif _placeholder_answer(answer):
                reason = f'回答只是中央模块的内部占位符: {answer[:40]}'
            elif _leaked_tool_xml(answer):
                reason = '工具调用写成了 XML 文本，中央模块没能解析执行'
            else:
                reason = _grounding_problem(
                    answer, used_tools, seen_paths, current, question, bool(drawn),
                )
            if not reason:
                break
            # 能从泄漏文本里解析出工具调用的话，代为执行 —— 模型的意图是对的，
            # 只是写法不合中央模块解析器的口味，没必要把这一轮整个作废。
            # 执行完 used_tools 就有内容工具了，下一轮自然走无工具合成。
            leaked = _parse_leaked_tool_calls(answer)
            if leaked:
                log.warning(f'模型把 {len(leaked)} 个工具调用写成了 XML 文本，代为执行')
                await _bump('recovered_calls')
                for name, arguments in leaked:
                    await tool_handler(name, arguments)
        else:
            await _bump('ungrounded')
            raise RuntimeError(f'两次作答均不合格: {reason}')
        limit = int(current.get('answer_max_chars') or 1500)
        if len(answer) > limit:
            answer = answer[:limit] + '…'

        # ---- 出口把关：截断之后、入库与发送之前 ----
        # 顺序是有意的：先做确定性过滤（违规词 / IP 脱敏），再送模型复审 ——
        # 前者零成本且不会失灵，能先削掉一部分，也避免把 IP 原文送去外部审核。
        answer, hit_word = safety.safe_output(
            answer,
            current.get('blocked_words') or [],
            str(current.get('blocked_response') or ''),
        )
        blocked = bool(hit_word)
        if hit_word:
            log.warning('AI 输出命中违规词，已替换为安全回复')
        elif await _output_rejected(current, answer):
            answer = str(current.get('moderation_blocked_response') or '')
            blocked = True
        if blocked:
            await _bump('blocked_output')
            # 文字都没过审，图也别发了。图片本身在 AI 画图那边过过一遍审核，
            # 但这一轮整体已经被判定为不该发出去，这时候单发一张图既没有上下文、
            # 也与「拦下来」的判断自相矛盾 —— 从严。
            drawn.clear()
            # 连这一轮的提问一起撤回：违规答案不入库，光留个没有回复的提问会让
            # 下一轮的历史出现悬空的 user 消息，反而干扰模型。
            if message_id is not None:
                await asyncio.to_thread(store.remove, message_id)
                message_id = None
        else:
            # 只有真正通过审核的答案才进上下文库：违规文本一旦入库，
            # 下一轮会当历史回灌给模型，等于把污染留在会话里。
            await asyncio.to_thread(
                store.append, scope, 'assistant', answer,
                int(current.get('max_stored_messages') or 200),
            )
            answer = _with_disclaimer(answer, current)
        await asyncio.to_thread(store.record_usage, user_id)
        await _bump('answers')
        if tool_calls:
            await _bump('tool_calls', tool_calls)
        # 被拦下时发的是插件自己的固定话术，不挂免责声明（那不是 AI 的回答）
        await _reply(event, answer)
    except Exception as error:
        # 失败时撤回刚写入的提问，别在上下文里留下没有答复的半截对话
        if message_id is not None:
            await asyncio.to_thread(store.remove, message_id)
        await _bump('failures')
        if _context_overflow(error):
            # 接口有输入字符硬上限，撞上就整轮报废。这不是偶发故障，靠重试解决不了，
            # 必须在面板把「输入预算」调到模型上限之下，所以日志要说清楚怎么改。
            await _bump('overflows')
            log.error(
                f'输入超出接口字符上限（当前预算 {budget}，本轮工具结果已用 {spent}）。'
                f'请在面板把「输入预算」调小到模型上限以下: {str(error)[:200]}'
            )
            await _reply(event, _with_reason(
                '这个问题涉及的代码太多，超出了模型单次可读的长度，换个更具体的问法试试。',
                error, current,
            ))
        else:
            log.warning(f'问答失败: {type(error).__name__}: {error}')
            await _reply(event, _with_reason(
                '查询失败了，稍后再试一次；如果一直失败请联系管理员。', error, current,
            ))
    finally:
        # 先放并发槽再发图：发图要等 QQ 那边上传，几秒起步，占着槽只会让别人白等。
        ratelimit.release(user_id)
        # 放 finally 而不是成功路径里：图一旦画出来，AI 画图那边的额度就已经扣了。
        # 后面哪一步失败（作答不合格、审核报错、发文字失败）都不该让这张图作废 ——
        # 唯一不发的情况是内容审核明确拦下了这一轮，那时上面已经把列表清空。
        await _deliver_images(event, drawn)


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
    """定期清理过期上下文。纯清理动作，失败绝不打断这一轮问答。

    它在 _answer **之前**调用，早期版本直接抛异常，热重载关掉存储时会让整个
    handler 在真正开始干活前就死掉 —— 跟统计写入是同一类问题，一并兜住。
    """
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < 600:
        return
    _last_prune = now
    try:
        await asyncio.to_thread(
            store.prune_expired, int(current.get('context_expire_seconds') or 3600),
            int(current.get('draw_cache_seconds') or 86400),
        )
    except Exception as error:  # noqa: BLE001 — 清理失败不值得中断问答
        log.debug(f'清理过期上下文失败: {error}')


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
    if current.get('draw_enabled'):
        # 插件加载顺序不保证，AI 画图可能还没注册能力，所以这条只作提示，
        # 以面板上的实时状态为准。
        state = drawing.state()
        log.info('画图能力：%s%s', state['message'],
                 '' if state['usable'] else '（若 AI 画图尚未加载完，稍后以面板状态为准）')
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
    block=BLOCK_OTHERS,
)
async def at_message(event, match) -> None:
    """@ 兜底提问。

    不设 ignore_at_check **不等于**只吃 @ 消息 —— 框架那道闸是
    ``if is_non_at and not ignore_at_check and not non_at_ok: continue``
    （core/plugin/_dispatch.py:238）。**全量群**下 non_at_ok 为真，这条 continue
    不执行，本 handler 的 ``.*`` 就会吃下群里每一条消息。
    所以必须在函数体里自己再过一道 is_at_self —— LGTBot 自己的消息派发也是
    这么做的（mod/dispatcher.py 里同样强制复查一次）。
    """
    current = config.load()
    if not current.get('enabled') or str(current.get('trigger_mode')) == 'prefix':
        return
    if getattr(event, 'is_bot', False) or not _scene_allowed(event, current):
        return
    # 群里必须冲着机器人来。判据见 _at_ok —— 只对全量事件卡 is_at_self，
    # 对 GROUP_AT_MESSAGE_CREATE 硬卡会误挡（payload 不一定带 mentions）。
    if not _at_ok(event):
        _warn_swallow(event)
        return
    question = str(match.group(1) or '').strip()
    if not question or question.startswith('/'):
        return
    await _maybe_prune(current)
    await _answer(event, question)
