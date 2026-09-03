"""计时器: 倒计时 + 多时区世界时间 (主人可修改铁蛋时区, 配置持久化到 data/计时器_config.yaml)

输出策略:
  · 全部消息走纯文本 (msg_type=MSG_TYPE_TEXT), 不渲染 markdown
  · 全部回复带 message_reference_id, 引用触发本指令的用户原消息
  · 倒计时后台播报用 send_to_group 主动消息 (跨任务发送), 同样带引用
"""

import asyncio
from datetime import datetime

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

from core.plugin.decorators import handler
from core.message._http import MSG_TYPE_TEXT
from core.base.config import cfg
import core.plugin.context as _ctx_mod


# ==================== 配置 (插件加载时初始化) ====================

_DEFAULTS = {
    'tiedan_tz': 'Asia/Shanghai',
    'tiedan_tz_name': '北京',
}

# 模块 exec 时, _loader.py 已经把当前插件 ctx 注入到 ctx_mod.ctx。
# 捕获到本地变量, 避免在 on_load 执行(ctx 已被 reset 为 None)时无法访问。
_ctx = _ctx_mod.ctx
_config = _ctx.ensure_config(_DEFAULTS, '计时器_config.yaml')


def _save_config():
    _ctx.save_config(_config, '计时器_config.yaml')


def _is_full_volume_group(event):
    """判断是否位于全量群: 全量群的消息事件类型固定为 GROUP_MESSAGE_CREATE"""
    return event.event_type == 'GROUP_MESSAGE_CREATE'


def _is_owner(event):
    """与框架 core/plugin/_blacklist.py:_is_owner 同源：按 appid 取 owner_ids 比对。"""
    if not getattr(event, 'user_id', ''):
        return False
    bot_cfg = cfg.get_bot_config(str(getattr(event, 'appid', '') or ''))
    return bool(bot_cfg) and event.user_id in (bot_cfg.get('owner_ids') or [])


# ==================== 统一发送辅助 ====================
# 所有出口都走 (a) 纯文本 (b) 引用原指令消息 — 框架 _build_core_payload 会把
# message_reference_id 自动包成 {'message_id': ..., 'ignore_get_message_error': True}
#
# !! 关键: V2 群聊的 message_reference.message_id 必须用 REFIDX 格式
#         (从 message_scene.ext.msg_idx 解析, 已由 parsers.py 落到
#          event.message_reference_id). 直接传 event.message_id (ROBOT1.0_xxx 格式)
#         会被 QQ 服务端忽略, 客户端不会渲染引用预览框。

def _ref_id(event):
    """返回当前消息的 REFIDX (V2 群聊引用回复用的 ID), 不存在则返回空串"""
    return getattr(event, 'message_reference_id', '') or ''


def _send_kwargs(event):
    """构造 msg_type=TEXT + 引用 REFIDX 的 kwargs"""
    kwargs = {'msg_type': MSG_TYPE_TEXT}
    ref = _ref_id(event)
    if ref:
        kwargs['message_reference_id'] = ref
    return kwargs


async def _reply(event, content, buttons=None):
    """统一 reply: 文本 + 引用原指令"""
    kwargs = _send_kwargs(event)
    if buttons is not None:
        kwargs['buttons'] = buttons
    await event.reply(content, **kwargs)


# 固定时区 (参照 Kotlin 原版, 三个别名都映射到太平洋)
_FIXED = {
    'star': ('America/Los_Angeles', '太平洋标准时间', '星星'),
    '星星': ('America/Los_Angeles', '太平洋标准时间', '星星'),
    'bc':   ('America/Los_Angeles', '太平洋标准时间', 'BC'),
    'BC':   ('America/Los_Angeles', '太平洋标准时间', 'BC'),
    'cat':  ('America/Los_Angeles', '太平洋标准时间', '猫猫'),
    '猫猫': ('America/Los_Angeles', '太平洋标准时间', '猫猫'),
}


# ==================== 倒计时 ====================

_MAX_THREADS = 5
_active_threads = 0
_NOTIFY = {
    1800: '还剩30分钟',
    600:  '还剩10分钟',
    180:  '还剩3分钟',
    120:  '还剩2分钟',
    60:   '还剩1分钟',
    30:   '还剩30秒',
    10:   '还剩10秒',
}


async def _run_countdown(event, total):
    """后台倒计时: 每秒检查关键节点并播报 (主动消息推送, 不受 msg_id 5min/5次 限额约束)"""
    global _active_threads
    gid = event.group_id
    # 跨 task 提前算好 push kwargs (event 字段后续可能被框架重置)
    push_kw = _send_kwargs(event)
    try:
        remaining = total
        while remaining >= 0:
            if remaining == 0:
                try:
                    await event.send_to_group(gid, "时间到!", **push_kw)
                except Exception:
                    pass
                break
            tip = _NOTIFY.get(remaining)
            if tip and remaining != total:
                try:
                    await event.send_to_group(gid, f"倒计时{tip}", **push_kw)
                except Exception:
                    pass
            await asyncio.sleep(1)
            remaining -= 1
    finally:
        _active_threads -= 1


# ==================== 帮助文本 ====================

_HELP_EN = (
    " ·⏱️ 计时器指令帮助\n"
    "▶️ 启动一个计时器\n"
    "/t count <秒>\n"
    " ·👥 查看群友当前时间\n"
    "/t tiedan\n"
    "/t star\n"
    "/t BC\n"
    "/t 猫猫"
)
_HELP_CN = (
    " ·⏱️ 计时器指令帮助\n"
    "▶️ 启动一个计时器\n"
    "/时间 倒计时 <秒>\n"
    " ·👥 查看群友当前时间\n"
    "/时间 铁蛋\n"
    "/时间 星星\n"
    "/时间 BC\n"
    "/时间 猫猫"
)


# ==================== 时间格式化 ====================

def _format_at(tz_id):
    """返回 (HH:MM:SS   ±HHMM, error_msg). error_msg 非空表示时区无效。"""
    if ZoneInfo is None:
        return None, "Python 版本过低，缺少 zoneinfo 模块"
    try:
        dt = datetime.now(ZoneInfo(tz_id))
    except ZoneInfoNotFoundError:
        return None, f"未找到时区：{tz_id} (可能缺 tzdata 包)"
    except Exception as e:
        return None, f"时区解析失败：{e}"
    return dt.strftime('%H:%M:%S   %z'), ''


# ==================== 主入口 (普通指令) ====================

@handler(
    r'^[/#](?:t|时间)(?:\s+(.+))?$',
    name='计时器',
    desc='[部分全量] 倒计时 + 多时区世界时间 (子命令：help)',
    block=True,
)
async def time_main(event, match):
    # 群场景: 仅全量群可触发
    if event.is_group and not _is_full_volume_group(event):
        btn = [[{'text': '全量消息授权', 'data': '全量申请', 'style': 4}]]
        await _reply(event, "ℹ 此功能仅全量群可用", buttons=btn)
        return

    args_raw = (match.group(1) or '').strip()
    if not args_raw:
        await _reply(event, _HELP_EN)
        return

    parts = args_raw.split(maxsplit=2)
    sub = parts[0]

    # ----- 帮助 -----
    if sub == 'help':
        await _reply(event, _HELP_EN); return
    if sub == '帮助':
        await _reply(event, _HELP_CN); return

    # ----- 倒计时 (禁止私信) -----
    if sub in ('count', '倒计时'):
        if event.is_direct:
            await _reply(event, "ℹ 倒计时仅在全量群可用")
            return
        if len(parts) < 2:
            await _reply(event, "[参数不足]\n用法：/t count <秒>")
            return
        try:
            second = int(parts[1])
        except ValueError:
            await _reply(event, "数字转换错误，时间必须为整数")
            return
        if second < 1 or second > 3600:
            await _reply(event, "倒计时仅支持 1 ~ 3600 秒")
            return
        global _active_threads
        if _active_threads >= _MAX_THREADS:
            await _reply(event, f"计时器无法启动：已经有 {_active_threads} 个进程正在运行")
            return
        _active_threads += 1
        # "倒计时开始" 也走主动消息 + 引用, 与后台 _run_countdown 输出风格一致
        await event.send_to_group(event.group_id, "倒计时开始", **_send_kwargs(event))
        asyncio.create_task(_run_countdown(event, second))
        return

    # ----- 铁蛋时间 (可配置) -----
    if sub in ('tiedan', '铁蛋'):
        tz_id = _config.get('tiedan_tz') or _DEFAULTS['tiedan_tz']
        tz_name = _config.get('tiedan_tz_name') or ''
        formatted, err = _format_at(tz_id)
        if err:
            await _reply(event, f"❌ {err}\n请主人用 /t 时区 <IANA时区ID> 修复")
            return
        suffix = f"\n（{tz_name}时间）" if tz_name else ""
        await _reply(event, f"铁蛋现在的时间为：\n{formatted}{suffix}")
        return

    # ----- 固定时区 (star/BC/猫猫) -----
    fixed = _FIXED.get(sub)
    if fixed:
        tz_id, tz_name, display = fixed
        formatted, err = _format_at(tz_id)
        if err:
            await _reply(event, f"❌ {err}")
            return
        await _reply(event, f"{display}现在的时间为：\n{formatted}\n({tz_name})")
        return

    # ----- 时区 (漏到这里 = 参数缺失; 带参数的已被 set_tiedan_tz 消费) -----
    if sub in ('时区', 'timezone'):
        if _is_owner(event):
            await _reply(event,
                "[参数不足]\n"
                "用法：/t 时区 <IANA时区ID> [显示名]"
            )
        else:
            await _reply(event, "[权限不足]\n该指令仅主人可用")
        return

    # ----- 未知子命令 -----
    await _reply(event, "[参数不匹配] 未知子命令\n请使用 /t help 查看指令帮助")


# ==================== 主人专用：设置铁蛋时区 ====================

@handler(
    r'^[/#](?:t|时间)\s+(?:时区|timezone)\s+(\S+)(?:\s+(.+))?$',
    name='设置铁蛋时区',
    desc='[仅全量] 主人指令：修改铁蛋的时区',
    priority=10,
    owner_only=True,
    block=True,
)
async def set_tiedan_tz(event, match):
    if event.is_group and not _is_full_volume_group(event):
        btn = [[{'text': '全量消息授权', 'data': '全量申请 ', 'style': 4}]]
        await _reply(event, "此功能仅全量群可用", buttons=btn)
        return

    tz_id = match.group(1).strip()
    tz_name = (match.group(2) or '').strip()

    if ZoneInfo is None:
        await _reply(event, "❌ Python 版本过低，缺少 zoneinfo 模块")
        return
    try:
        ZoneInfo(tz_id)
    except ZoneInfoNotFoundError:
        await _reply(event,
            f"❌ 无效的时区 ID：{tz_id}\n"
            f"请使用标准 IANA 时区, 例：Asia/Shanghai"
        )
        return
    except Exception as e:
        await _reply(event, f"❌ 时区校验失败：{e}")
        return

    _config['tiedan_tz'] = tz_id
    _config['tiedan_tz_name'] = tz_name
    _save_config()
    if tz_name:
        await _reply(event, f"时区显示已修改：{tz_id}({tz_name}时间)")
    else:
        await _reply(event, f"时区显示已修改：{tz_id}")
