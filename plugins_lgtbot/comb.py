"""拼装毁灭者 (comb): 调用框架运行目录上一级的 comb 二进制程序

子命令 (同一可执行文件的不同 subcommand):
  comb XX   → ../comb seed XX    拼装毁灭者-seed
  card XX   → ../comb card XX    拼装毁灭者-card
  board XX  → ../comb board XX   拼装毁灭者-board

执行超时 20s, stdout 作为消息回复 (代码块包裹避免特殊字符被 markdown 渲染)。
"""

import os
import re
import asyncio

from core.plugin.decorators import handler


# 程序输出中含 `seed: 测试` 这种行, 含用户原始输入, 需脱敏避免在群里泄露种子。
# 加捕获组以便按 seed 实际长度生成等量 *。
_SEED_LINE = re.compile(r'(seed:\s*)([^\n\r]*)')

# 「查询/机挖查询/生草查询」分支输出排行榜, 每行形如:
#   高分: `1. <seed> - 295`
#   低分: `-1. <seed> - 100`     (前面带 - 号区分)
# 三种查询模式共用同一格式 (../comb_scores.txt / mine / grass), 都需脱敏。
# 保留 ' 排名. ' 前缀和 ' - 分数' 后缀, 只替换中间的 seed 部分。
# 由于 seed 可能含空格或 ... (shortenSeed 缩短), 用非贪婪 + 后缀锚定。
_RANK_LINE = re.compile(r'^(-?\d+\.\s+)(.+?)(\s+-\s+-?\d+)\s*$', re.MULTILINE)


def _mask(s):
    """生成与 s 等长的 * (至少 1 个, 防御 s 为空)"""
    return '*' * max(1, len(s))


def _hide_seed(text):
    """脱敏 comb 输出中两类 seed:
       (1) `seed: XX` 行 (seed 模式)
       (2) 排行榜 `N. <seed> - 分数` / `-N. <seed> - 分数` (查询模式)
       分数与排名保留, 仅 seed 字段按原长度替换为对应数量的 * + (已隐藏)
    """
    text = _SEED_LINE.sub(
        lambda m: f"{m.group(1)}{_mask(m.group(2))}", text)
    text = _RANK_LINE.sub(
        lambda m: f"{m.group(1)}{_mask(m.group(2))}{m.group(3)}", text)
    return text


_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))         # plugins/tiedan
_PROJECT_DIR = os.path.dirname(os.path.dirname(_PLUGIN_DIR))     # 框架根: ElainaBot_v2
_PARENT_DIR = os.path.dirname(_PROJECT_DIR)                       # 框架运行目录的上一级
_COMB_PATH = os.path.join(_PARENT_DIR, 'comb')                    # ../comb

_TIMEOUT_S = 3.0   # 程序超时事件
_MAX_OUT = 3500     # QQ markdown 单条上限 ~4k, 留 buffer


def _resolve_comb_exe():
    """返回 comb 可执行文件路径; Linux 直接 ../comb, Windows 回退 ../comb.exe; 都不存在返回 None"""
    if os.path.isfile(_COMB_PATH):
        return _COMB_PATH
    win_exe = _COMB_PATH + '.exe'
    if os.path.isfile(win_exe):
        return win_exe
    return None


def _is_full_volume_group(event):
    """判断是否位于全量群: 全量群的消息事件类型固定为 GROUP_MESSAGE_CREATE"""
    return event.event_type == 'GROUP_MESSAGE_CREATE'


async def _run_comb(subcommand, user_input):
    """异步执行 ../comb <subcommand> <user_input>, 返回 (stdout_text, error_text).

    安全说明: 用 create_subprocess_exec + 参数列表 (非 shell=True),
    用户输入作为单个参数透传, 不经过 shell 解析, 无命令注入风险。
    """
    exe = _resolve_comb_exe()
    if not exe:
        return '', f"程序不存在: {_COMB_PATH}"

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, subcommand, user_input,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # cwd 用项目根 (comb 目录的子目录), 让 comb.cpp 中写死的 ../comb_*.txt
            # 相对路径正好落在 comb 同目录 (用户数据文件存放处)
            cwd=_PROJECT_DIR,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_S
        )
        out = (stdout or b'').decode('utf-8', errors='replace').strip()
        err = (stderr or b'').decode('utf-8', errors='replace').strip()
        if proc.returncode != 0 and not out:
            return '', err or f"程序异常退出 (code={proc.returncode})"
        return out, ''
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        return '', f"程序执行超时 ({_TIMEOUT_S:.0f}s)"
    except FileNotFoundError:
        return '', f"程序不存在: {exe}"
    except Exception as e:
        return '', f"程序执行异常: {type(e).__name__}: {e}"


async def _handle(event, sub, user_input):
    # 群场景: 仅全量群可触发 (私信不限)
    if event.is_group and not _is_full_volume_group(event):
        btn = [[{'text': '全量消息授权', 'data': '全量申请', 'style': 4}]]
        await event.reply("ℹ 此功能仅全量群可用", btn)
        return

    out, err = await _run_comb(sub, user_input)
    if out:
        out = _hide_seed(out)
        if len(out) > _MAX_OUT:
            out = out[:_MAX_OUT] + '\n... (输出过长, 已截断)'
        # 代码块包裹: 避免 # / * / ` 等被当 markdown 渲染
        await event.reply(f"```comb\n{out}\n```\n> [仅记录模式] 出于安全考虑，详细内容请前往 KOOK 频道查看")
    else:
        await event.reply(err or "(空输出)")


# ==================== 三个子命令 ====================
# pattern 加 ^/?... 兼顾带/不带 / 前缀两种触发, 避免被 LGTBot `.*` 兜底接走


@handler(r'^/?comb\s+(.+)$', name='拼装毁灭者-seed',
         desc='[仅全量] 调用 ../comb seed <参数>', block=True)
async def cmd_comb(event, match):
    await _handle(event, 'seed', match.group(1).strip())


@handler(r'^/?card\s+(.+)$', name='拼装毁灭者-card',
         desc='[仅全量] 调用 ../comb card <参数>', block=True)
async def cmd_card(event, match):
    await _handle(event, 'card', match.group(1).strip())


@handler(r'^/?board\s+(.+)$', name='拼装毁灭者-board',
         desc='[仅全量] 调用 ../comb board <参数>', block=True)
async def cmd_board(event, match):
    await _handle(event, 'board', match.group(1).strip())
