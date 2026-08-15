"""中央 AI LLM 模块适配层。

本插件不保存任何接口地址或密钥 —— 全部由 modules/ai_llm 管理，这里只按
``provider_id`` / ``model_preference`` 做选择（modules/ai_llm/docs/README.md）。

刻意用 ``complete()`` 而不是 ``run_agent()``：run_agent 会打开中央运行时能力
（其他插件共享的工具、MCP、Skills）。本插件面向普通群友开放，工具面必须收敛到
自己这五个只读工具，不能让模型顺手拿到别的插件注册的写操作或联网能力。

同理传 ``prepare_context=False``：历史由 store.py 自己裁剪，不需要中央再压一遍。
"""
from __future__ import annotations

import asyncio
import json

from core.base.logger import PLUGIN, get_logger

from . import safety
from .config import DEFAULT_SAFETY_REVIEW_PROMPT

CONSUMER = 'lgtbot_qa'
log = get_logger(PLUGIN, 'LGTBot游戏问答')


# 值得重试的**瞬时**故障：网关抖动、限流、超时、空响应。
# 这类错误换个时刻再打一次就可能成功，而一次问答往往已经跑了十几个工具调用，
# 因为一次 524 把全部检索结果作废太亏。
_TRANSIENT_MARKERS = (
    '524', '502', '503', '504', '429', '500',
    'bad gateway', 'gateway time-out', 'gateway timeout', 'service unavailable',
    'too many requests', 'rate limit', 'overload', 'temporarily',
    'timeout', 'timed out', '超时',
    'connection', 'reset by peer', 'broken pipe', 'eof occurred',
    '接口返回了空消息', '接口返回中没有',
)

# 明确**不该**重试的：重试多少次都是同样的结果，只会白烧时间和额度。
# 放在瞬时判断之前，避免 413 里的数字或 auth 报文里的词被误判成瞬时。
_PERMANENT_MARKERS = (
    '413', 'context_length_exceeded', '超最大字符限制', '超过最大长度',
    '401', '403', 'invalid api key', 'unauthorized', 'permission',
    '没有可用的 ai 接口', 'ai llm 服务未启用',
)


def is_transient(error) -> bool:
    """这个错误值不值得重试。"""
    text = str(error or '').casefold()
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)


async def _bounded(make_coro, current: dict, label: str):
    """调用模型：套超时 + 瞬时故障自动重试。

    ``make_coro`` 必须是**每次现造协程**的工厂 —— 协程不能 await 两次，
    传现成的协程对象重试时会直接 RuntimeError。

    超时的意义：卡住的请求会一直占着并发槽（默认只有 2 个），后面所有人都被
    挡在门外，单个请求慢会变成整个功能不可用。
    重试的意义：一次问答常常已经跑了十几个工具，不该被一次网关抖动全部作废。
    退避是线性的（delay × 第几次），够应付网关抖动，又不会把并发槽占太久。
    """
    timeout = float(current.get('request_timeout_seconds') or 90)
    tries = max(1, int(current.get('retry_attempts') or 3))
    delay = max(0.0, float(current.get('retry_delay_seconds') or 2))
    last_error: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            coro = make_coro()
            if timeout <= 0:
                return await coro
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError as error:
            last_error = RuntimeError(f'{label}超时（{timeout:.0f} 秒未返回）')
            last_error.__cause__ = error
        except Exception as error:  # noqa: BLE001 — 分类后决定重试还是上抛
            if not is_transient(error):
                raise
            last_error = error
        if attempt < tries:
            log.warning(
                f'{label}第 {attempt}/{tries} 次失败（{str(last_error)[:120]}），'
                f'{delay * attempt:.0f} 秒后重试'
            )
            if delay:
                await asyncio.sleep(delay * attempt)
    raise last_error if last_error else RuntimeError(f'{label}失败')


def _raw_service():
    """拿中央模块实例。模块可能热重载，所以每次现取，不缓存。"""
    try:
        from core.application import get_app
    except ImportError:
        return None
    app = get_app()
    manager = getattr(app, 'module_manager', None) if app else None
    if manager is None:
        return None
    service = manager.get('ai_llm')
    if service is not None:
        return service
    for item in manager.list_modules():
        if str(item.get('display_name') or '').strip() == 'AI LLM 服务':
            return manager.get(str(item.get('name') or ''))
    return None


def get_service():
    return _raw_service()


def available() -> bool:
    service = get_service()
    if service is None:
        return False
    if hasattr(service, 'available'):
        return bool(service.available())
    config = service.config()
    if not config.get('enabled'):
        return False
    return any(
        item.get('enabled') and item.get('base_url') and (item.get('model') or item.get('models'))
        for item in config.get('providers', [])
    )


def status() -> dict:
    service = get_service()
    if service is None:
        return {
            'installed': False, 'enabled': False,
            'message': '未检测到 AI LLM 模块，请前往插件市场下载并启用',
        }
    config = service.config()
    if not config.get('enabled'):
        return {'installed': True, 'enabled': False, 'message': '中央 AI LLM 未启用'}
    if not available():
        return {'installed': True, 'enabled': True, 'message': '中央 AI LLM 没有可用接口或模型'}
    return {'installed': True, 'enabled': True, 'message': '中央 AI LLM 已就绪'}


def public_config() -> dict:
    service = get_service()
    return service.config(public=True) if service else {}


def resolve_selection(provider_id: str = '', model: str = '') -> tuple[str, str]:
    """把面板上的选择校验成中央模块当前真实可用的 (provider_id, model)。

    选中的接口/模型被管理员在中央面板停用后，这里会退回空串 = 自动选择，
    而不是拿着失效的组合去调用然后报错。
    """
    config = public_config()
    providers = [item for item in config.get('providers', []) if item.get('enabled')]

    def usable(provider: dict) -> set[str]:
        disabled = {str(item) for item in provider.get('disabled_models', [])}
        values = [
            *(provider.get('model_priority') or []),
            *(provider.get('models') or []),
            provider.get('model'),
        ]
        return {
            str(item).strip() for item in values
            if str(item or '').strip() and str(item).strip() not in disabled
        }

    if provider_id:
        provider = next((item for item in providers if item.get('id') == provider_id), None)
        if provider is None:
            return '', ''
        return str(provider['id']), model if model in usable(provider) else ''
    if model:
        return ('', model) if any(model in usable(item) for item in providers) else ('', '')
    return '', ''


# 中央模块在模型不输出原生 tool_calls 时会退回 XML 文本协议，但它的解析器只认
# <工具名><参数名>值</参数名></工具名> 和无参数的 <工具名/>。模型很容易写成属性式
# <工具名 参数="值"/> —— 那样一个都匹配不上，raw XML 会被当成答案直接发给用户，
# 且中央层还记为 success（modules/ai_llm/app/service.py:114 的 self_pattern）。
#
# 这条规则写死在代码里、而不是塞进面板可编辑的 system_prompt：后者一旦被用户改写、
# 或被旧配置里存着的老提示词覆盖就会丢，而这是正确性相关的硬约束。
TOOL_FORMAT_RULE = (
    '【工具调用格式】优先使用接口原生 tool_calls。只有在不支持原生调用时才输出 XML，'
    '且必须用嵌套标签写参数：<工具名><参数名>参数值</参数名></工具名>。'
    '禁止把参数写成 XML 属性 —— <工具名 参数="值"/> 无法被解析，会导致工具完全没有执行。'
    '只有完全无参数的工具才写 <工具名/>。输出 XML 时不要同时输出解释文字。'
    '拿到工具结果后必须直接写出最终答案；'
    '**绝不要把「[请求执行工具]」这类方括号占位文本当成回复发出去** —— '
    '那是协议内部标记，不是答案。\n'
    '工具由**你自己**调用，用户看不到工具也没法运行它们。'
    '绝不要回复「请先运行 xxx」「请你执行 xxx」之类的话 —— 需要什么信息就自己去调，'
    '实测出现过让用户去跑 list_games 的错误回复。'
)

# 实测模型会凭空编造出 `<游戏>/game_logic/combinations.py:212-230` 这种**根本不存在**的
# Python 路径和行号，然后据此长篇大论 —— 整个 LGTBot 是 C++ 项目，一个 .py 都没有。
# 光说「要先检索」拦不住，必须把真实的代码布局摆给它，让「编一个路径」这件事失去空间。
# 这里只写项目级事实，不提任何具体游戏、不举具体游戏的例子。
CODE_LAYOUT_RULE = (
    '【源码结构】LGTBot 引擎与全部游戏都是 C++ 实现，源码里没有任何 Python 文件，'
    '不存在 game_logic/ 之类的目录。每个游戏是 games/ 下的一个目录，常见文件：\n'
    '- rule.md：作者写给玩家的规则文档\n'
    '- mygame.cc：主逻辑，阶段流转、玩家动作、计分与结算都在这里\n'
    '- achievements.h：成就定义与达成条件\n'
    '- options.h / option.cmake：游戏选项、默认值与倍率\n'
    '- unittest.cc：单元测试，能反映真实的判定预期\n'
    '**很多游戏还有额外的 .h / .cc，而机制往往正实现在那些额外文件里**，'
    '不要以为只有上面几个。最省事的办法是直接用 read_game_source 一次读全，'
    '它会带上该游戏所有源码；装不下时也会明确告诉你哪些文件没读到。\n'
    '**禁止臆造任何目录名、文件名或行号。**只允许引用工具真实返回过的路径，'
    '没读到就说没查到，绝不用「大概是这样」的方式补全代码细节。'
)

# 项目本身的事实。逐条对应 plugins/LGTBot_ElainaBot/README.md 的「致谢与项目来源」，
# 不是我编的。
#
# 为什么要预置：「LGTBot 是什么」这类元问题没有对应的游戏可读，模型只会凭自己的
# 先验知识作答 —— 而它对这个小众项目的先验基本是错的（实测答出过整段臆造）。
# 把权威事实摆进提示词，零检索也能答对；要细节再让它读 README.md。
PROJECT_FACTS = (
    '【项目简介】以下是关于本项目的权威事实，可直接用于回答，不需要检索：\n'
    '- LGTBot 是一个基于 C++20 的多人文字推理游戏裁判机器人库，内置 50+ 种不同风格的游戏，'
    '由 @Slontia 开发（LGPLv2）。游戏逻辑、引擎核心、图片渲染都是上游实现的。\n'
    '- 「LGT」取自日本漫画家甲斐谷忍《Liar Game》里的虚构组织'
    '「Liar Game Tournament 事务所」。\n'
    '- LGTBot_ElainaBot 是把 LGTBot 接到 ElainaBot_v2（QQ 官方机器人框架）的**适配层**，'
    '作者是铁蛋。它不改引擎功能，只做框架适配和 QQ 协议限制的处理'
    '（媒体消息合并、@提及格式、按钮交互等）。\n'
    '- 你自己是这个机器人上的游戏答疑助手，负责检索源码回答玩家关于游戏的问题。\n'
    '问到项目、引擎、机器人本身时，依据以上事实回答即可；需要更多细节再读 README.md。'
)

# 硬性作答前提。与 CODE_LAYOUT_RULE 一样写死在代码里，不进面板可编辑的 system_prompt。
GROUNDING_RULE = (
    '【作答前提】回答任何涉及规则、数值、判定、结算、成就的问题之前，'
    '必须先用 read_game_rule / read_file / search_code 真正读到相关内容。'
    '没有读到就只能回答「没有查到」，不允许根据游戏名、常识或其他游戏的经验推测。\n'
    '【顺着标识符再查一跳】源码里机制常常分两层：用户说的名词（技能名、状态名、术语）'
    '往往只出现在**定义处**，而「怎么排序、何时触发、按什么条件判定」的真正实现，'
    '在它所调用或重写的函数名、枚举、常量里 —— 那些地方通常一个中文名词都不出现。\n'
    '所以读到定义后，如果问题问的是顺序 / 时机 / 计算方式，'
    '**要把定义里出现的函数名或标识符当作新关键词再 search_code 一次**，'
    '定位到真正的实现再作答。停在定义上，就会误判成「没有这个机制」。\n'
    '同理，用中文词搜不到不等于不存在，换成代码里的英文标识符再搜一次。\n'
    '给出处时写工具真实返回过的路径与行号；拿不准就不要写出处，也不要编一个。\n'
    '回答要短：先给结论，再给必要依据。不要写分点长文，不要罗列推演过程，'
    '不要给「举例说明」式的枚举 —— 那些内容最容易掺进没有依据的臆测。'
)

# 检测到属性式泄漏后重试那一轮追加的纠正指令
CORRECTION_PROMPT = (
    '你上一次回复把工具调用写成了 XML 属性式，工具因此没有真正执行，请重新作答。'
    '改用接口原生 tool_calls，或用 <工具名><参数名>参数值</参数名></工具名> 的嵌套写法，'
    '绝不要把参数写在标签属性里。'
)


UNGROUNDED_PROMPT = (
    '你上一次回答没有真正读取任何源码，或者引用了不存在的文件与行号。现在重新作答：\n'
    '- 问的是**某个具体游戏**的规则或机制 → 先调用 read_game_source 把该游戏源码读进来'
    '（问题里的关键术语填进 focus），必要时再用 search_code 顺着代码里的函数名追一跳，'
    '看到真实实现之后才下结论。\n'
    '- 问的是**浏览、推荐、有哪些**这类问题 → 用 list_games 的结果直接作答即可，'
    '它已经带了每个游戏的简介，不需要再读源码。\n'
    '只引用工具返回过的路径；确实查不到再回答「没有查到」，不要编造。'
)


def build_system_prompt(current: dict, scope_hint: str) -> str:
    parts = [
        str(current.get('system_prompt') or '').strip(),
        scope_hint,
        PROJECT_FACTS,
        CODE_LAYOUT_RULE,
        GROUNDING_RULE,
        TOOL_FORMAT_RULE,
        safety.system_safety_rules(),
        str(current.get('extra_prompt') or '').strip(),
        f'回答控制在 {int(current.get("answer_max_chars") or 1500)} 字以内。',
    ]
    return '\n\n'.join(item for item in parts if item)


async def _moderate(current: dict, text: str, source: str) -> dict:
    """用独立的一次模型调用做内容安全分类（与 AI 聊天陪伴同口径）。

    刻意与主问答分开调用：不带工具、不带上下文、temperature=0、max_tokens 极小，
    并用独立的 consumer_plugin 记账，便于在中央审计里区分问答与审核。

    只接受「安全」/「内容违规，已禁止发送」两种回答，其余一律当审核不可用，
    由调用方按 fail-open / fail-closed 策略处置 —— 模型胡乱回一句不能被当成放行。
    """
    if not current.get('moderation_enabled'):
        return {'available': False, 'flagged': False}
    service = get_service()
    if service is None:
        return {'available': False, 'flagged': False, 'error': '中央 AI LLM 不可用'}
    provider_id, model = resolve_selection(
        str(current.get('provider_id') or ''), str(current.get('model_preference') or ''),
    )
    review_prompt = str(
        current.get('safety_review_prompt') or DEFAULT_SAFETY_REVIEW_PROMPT
    ).strip() + (
        '\n\n运行时强制规则：source 可能是 user_input 或 assistant_output，两者都必须完整审核。'
        '任何现实或历史政治人物的姓名、别名、称号、谐音、影射及模型主动补全均判定为违规；'
        '不得因为内容是引用、历史介绍、玩笑、纠错或中立讨论而放行。'
    )
    try:
        result = await _bounded(lambda: service.complete(
            [{'role': 'user', 'content': json.dumps(
                {'source': source, 'content': str(text or '')}, ensure_ascii=False,
            )}],
            system_prompt=review_prompt,
            provider_id=provider_id,
            model=model,
            temperature=0,
            max_tokens=24,
            consumer_plugin=f'{CONSUMER}_review',
            enable_runtime_tools=False,
            prepare_context=False,
        ), current, '内容审核')
        raw = str(result.get('text') or '').strip()
        decision = ''.join(raw.split()).strip('`"\'。.!！').replace(',', '，')
        if decision not in {'安全', '内容违规，已禁止发送'}:
            raise ValueError(f'审核模型返回了无效结果: {raw[:60]}')
        return {'available': True, 'flagged': decision == '内容违规，已禁止发送'}
    except Exception as error:  # noqa: BLE001 — 由调用方按配置的失败策略处置
        return {
            'available': False, 'flagged': False,
            'error': safety.redact_ips(str(error))[:300],
        }


async def moderate_input(current: dict, text: str) -> dict:
    return await _moderate(current, text, 'user_input')


async def moderate_output(current: dict, text: str) -> dict:
    return await _moderate(current, text, 'assistant_output')


SYNTHESIS_PROMPT = (
    '下面是刚才检索到的真实结果。请**直接给出最终答案**，不要再请求调用任何工具，'
    '不要输出 XML、不要输出「[请求执行工具]」之类的占位文本。'
    '只依据这些结果作答；结果里没有的内容就说没有查到。'
)


async def synthesize(
    current: dict, messages: list[dict], system_prompt: str,
    transcript: str, session_id: str,
) -> dict:
    """不带工具的合成兜底：把已拿到的工具结果交给模型，让它只管写答案。

    为什么这样能救场：中央模块的 XML 文本协议提示词由 _text_tool_protocol(tools)
    生成，**tools 为空时它返回空串**（modules/ai_llm/app/service.py:1793）。
    也就是说这一次调用完全不存在 XML 协议，也就不会出现
    '[请求执行工具]' 这个内部占位符（同文件 :1888）—— 模型没有工具可调，
    只能老老实实写答案。

    实测故障链：模型在 XML 协议下连续两轮正常调用工具（日志里 ok=True），
    第三轮却照抄自己前两轮被写成占位符的 assistant 内容，于是占位符变成了答案。
    带工具重试仍会踩同一个坑，所以兜底这一轮必须把工具彻底拿掉。
    """
    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    provider_id, model = resolve_selection(
        str(current.get('provider_id') or ''), str(current.get('model_preference') or ''),
    )
    payload = [*messages, {'role': 'user', 'content': f'{SYNTHESIS_PROMPT}\n\n{transcript}'}]
    return await _bounded(lambda: service.complete(
        messages=payload,
        system_prompt=system_prompt,
        provider_id=provider_id,
        model=model,
        temperature=float(current.get('temperature') or 0.2),
        max_tokens=int(current.get('max_tokens') or 4096),
        session_id=session_id,
        consumer_plugin=f'{CONSUMER}_synth',
        enable_runtime_tools=False,
        prepare_context=False,
    ), current, '合成兜底')


async def ask(
    current: dict, messages: list[dict], system_prompt: str,
    tool_handler, session_id: str,
) -> dict:
    """跑一轮带工具的问答。异常原样抛出，由调用方决定怎么回话。"""
    from . import tools

    service = get_service()
    if service is None:
        raise RuntimeError(status()['message'])
    provider_id, model = resolve_selection(
        str(current.get('provider_id') or ''), str(current.get('model_preference') or ''),
    )
    return await _bounded(lambda: service.complete(
        messages=messages,
        system_prompt=system_prompt,
        provider_id=provider_id,
        model=model,
        temperature=float(current.get('temperature') or 0.2),
        max_tokens=int(current.get('max_tokens') or 4096),
        tools=tools.TOOLS_SCHEMA,
        tool_handler=tool_handler,
        max_tool_rounds=int(current.get('max_tool_rounds') or 10),
        session_id=session_id,
        consumer_plugin=CONSUMER,
        prepare_context=False,
    ), current, '模型调用')
