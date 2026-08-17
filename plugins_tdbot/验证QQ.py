"""验证 QQ 号插件

用法: /验证 <QQ号>
  · 自动取当前 event 的 appid + 用户 openid, 与用户输入的 QQ 号一起提交给外部 API
  · API 接口: https://xiaodi.ykxbl.top/Api/qqidpd.php
  · 仅输出: 结果 / 相似度 / 判断时间; 状态非 True 时报错

注意:
  本插件会把 (appid, openid, qq) 三元组发送到第三方 API, 涉及隐私转发。
"""

import asyncio
import aiohttp

from core.plugin.decorators import handler


_API_URL = 'https://xiaodi.ykxbl.top/Api/qqidpd.php'
_TIMEOUT_S = 10.0


def _is_full_volume_group(event):
    """判断是否位于全量群: 用主框架 data.db 中的 full_access_groups 记录 (与 /全量列表 同源)"""
    if not event.is_group:
        return False
    gid = event.group_id or ''
    if not gid:
        return False
    try:
        from core.bot.manager import _bot_manager_ref
        if not _bot_manager_ref:
            return False
        rows = _bot_manager_ref.get_full_access_groups() or []
        for r in rows:
            rid = r.get('group_id') if isinstance(r, dict) else r
            if rid == gid:
                return True
    except Exception:
        pass
    return False


async def _verify(appid, openid, qq):
    """调 API. 返回 (data_dict | None, error_str | None)"""
    params = {'appid': appid, 'openid': openid, 'qq': qq}
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_API_URL, params=params) as resp:
                if resp.status != 200:
                    return None, f"API HTTP {resp.status}"
                # 服务端 content_type 不一定标准, 强制按 JSON 解析
                data = await resp.json(content_type=None)
                return data, None
    except asyncio.TimeoutError:
        return None, f"API 请求超时 ({_TIMEOUT_S:.0f}s)"
    except aiohttp.ClientError as e:
        return None, f"网络错误: {type(e).__name__}"
    except Exception as e:
        return None, f"API 调用异常: {type(e).__name__}"


@handler(r'^/?验证\s+(\d{5,15})\s*$', name='验证QQ号',
         desc='验证当前 openid 是否对应指定 QQ 号', block=True)
async def cmd_verify(event, match):
    # 群场景: 仅全量群可触发 (私信不限)
    # if event.is_group and not _is_full_volume_group(event):
    #     btn = [[{'text': '全量消息授权', 'data': '全量申请', 'style': 4}]]
    #     await event.reply("ℹ 此功能仅全量群可用", btn)
    #     return

    qq = match.group(1)
    appid = event.appid or ''
    openid = event.user_id or ''

    if not appid or not openid:
        await event.reply("**[错误]** 无法获取 appid 或 openid")
        return

    data, err = await _verify(appid, openid, qq)
    if err:
        await event.reply(f"**[请求失败]**\n{err}")
        return

    if not isinstance(data, dict):
        await event.reply("**[错误]** API 返回格式异常")
        return

    status = data.get('状态')
    if status is not True:
        # 状态非 True: 显示 API 的错误描述 (如有)
        reason = data.get('结果') or data.get('msg') or data.get('message') or '未知错误'
        await event.reply(f"**[验证失败]** 状态异常\n{reason}")
        return

    result = data.get('结果', '?')
    similarity = data.get('相似度', '?')
    judge_ms = data.get('判断时间(ms)', '?')

    md = (
        "**🔍 QQ 验证结果**\n"
        f"结果: **{result}**\n"
        f"相似度: **{similarity}**\n"
        f"判断时间: **{judge_ms} ms**"
    )
    await event.reply(md)
