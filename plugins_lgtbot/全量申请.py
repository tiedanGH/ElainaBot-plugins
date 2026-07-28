"""全量申请: 生成群全量消息授权链接"""

__plugin_meta__ = {
    'name': '全量申请',
    'author': 'lengxi',
    'description': '生成群全量消息授权链接，支持记录申请与列表查看',
    'version': '1.1.0',
}


import asyncio
import json
import os
from datetime import datetime

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load


log = get_logger(PLUGIN, "全量申请")
_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE, 'data')
_RECORD_FILE = os.path.join(_DATA_DIR, '全量申请_apply_records.json')
_record_lock = asyncio.Lock()

_URL_TPL = (
    'https://club.vip.qq.com/transfer?open_kuikly_info='
    '%7B%22page_name%22%3A%20%22ai_group_service_agreement_pop_page%22'
    '%2C%22groupCode%22%3A{group_code}'
    '%2C%22botUin%22%3A{bot_uin}'
    '%2C%22botUid%22%3A%22{bot_uid}%22'
    '%2C%22screen%22%3A1%7D'
)


_CONFIG_FILE = os.path.join(_DATA_DIR, '全量申请_config.json')
_CONFIG_DEFAULTS = {
    'uin': '',
    'uid': '',
}


def _ensure_config():
    """确保 data/config.json 存在，不存在则自动创建默认配置"""
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.isfile(_CONFIG_FILE):
        with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(_CONFIG_DEFAULTS, f, ensure_ascii=False, indent=2)
        log.info('已自动生成配置文件: data/全量申请_config.json')


def _get_bot_uin_uid():
    """从插件目录下 data/全量申请_config.json 读取 uin / uid"""
    _ensure_config()
    try:
        with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception:
        config = {}
    uin = str(config.get('uin', '') or '')
    uid = str(config.get('uid', '') or '')
    return uin, uid


@on_load
def _init_config():
    _ensure_config()

_IMG = '![菜单头图 #300px #250px](https://qqbot.ugcimg.cn/102813815/9fd08ad10f048984fc0a9d36f71dd450e0780587/c7f24f5aeadfb1908561622d43de3169)'
_PROMPT_IMG = '![教程图 #1416px #1520px](https://xingyeai-images-cdn.oss-cn-shanghai.aliyuncs.com/talkie-user-img/424611064001759/424612459550316.jpeg?x-oss-process=image/format,jpg)'
_INPUT_TIP = f"1. 请群主点击我的头像\n2. 点击右上角齿轮设置\n3. 点击**可获取的群聊消息范围**设置为**获取群内全部消息**\n4. 勾选**主动在群聊内发言**即可\n\n{_PROMPT_IMG}\n\n备选：<qqbot-cmd-input text='全量申请 ' show='请点击这里并输入群号' />\n>💡 授权后无需再点击按钮刷新会话\n需要9.2.90以上版本QQ设置哦！"
_INVALID_GROUP_TIP = "群号过短，请重新输入：\n<qqbot-cmd-input text='全量申请 ' show='全量申请 群号' />"


def _append_json_record_sync(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, 'wb') as f:
            f.write(b'[\n' + payload + b'\n]')
        return

    with open(path, 'r+b') as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell() - 1
        while pos >= 0:
            f.seek(pos)
            ch = f.read(1)
            if ch not in b' \t\r\n':
                break
            pos -= 1
        if pos < 0 or ch != b']':
            raise ValueError('记录文件不是有效的 JSON 数组')

        prev_pos = pos - 1
        prev = b''
        while prev_pos >= 0:
            f.seek(prev_pos)
            prev = f.read(1)
            if prev not in b' \t\r\n':
                break
            prev_pos -= 1
        f.seek(pos)
        f.write((b'\n' if prev == b'[' else b',\n') + payload + b'\n]')
        f.truncate()


async def _record_apply(event, group_code, status):
    record = {
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'appid': str(getattr(event, 'appid', '') or ''),
        'group_id': str(getattr(event, 'group_id', '') or ''),
        'input_group_code': str(group_code),
        'user_id': str(getattr(event, 'user_id', '') or ''),
        'event_type': str(getattr(event, 'event_type', '') or ''),
        'status': status,
    }
    try:
        async with _record_lock:
            await asyncio.to_thread(_append_json_record_sync, _RECORD_FILE, record)
    except Exception as e:
        log.warning(f"记录全量申请失败: {e}")


@handler(r'^全量申请\s*(\d{6,10})$', name='全量申请', desc='生成群全量消息授权链接', priority=10)
async def apply_full_access(event, match):
    group_code = match.group(1)
    await _record_apply(event, group_code, 'submitted')
    if event.event_type == 'GROUP_MESSAGE_CREATE':
        return await event.reply(f"<@{event.user_id}>\n当前群已开启全量消息，无需再次申请")
    bot_uin, bot_uid = _get_bot_uin_uid()
    if not bot_uin or not bot_uid:
        return await event.reply(f"<@{event.user_id}>\n请先在插件配置 data/全量申请_config.json 中填写 uin 和 uid")
    url = _URL_TPL.format(group_code=group_code, bot_uin=bot_uin, bot_uid=bot_uid)
    msg = (
        "## 🔔 全量消息授权\n"
        "群主授权后，机器人可以推送主动消息，*无需再点击刷新按钮*\n"
        f"{_IMG}\n"
        "**请群主点击下方按钮授权**\n"
        "> **需要更新QQ到最新版(9.2.90及以上)**\n"
        "> **IOS可能暂不支持此方式授权，请直接在bot头像设置中开启**"
    )
    btn = [[{'text': '群主大大请点击这里同意申请', 'link': url, 'style': 1}]]
    await event.reply(msg, btn)


@handler(r'^全量申请$', name='全量申请提示', desc='提示输入全量申请群号')
async def prompt_full_access_group(event, match):
    if event.event_type == 'GROUP_MESSAGE_CREATE':
        return await event.reply(f"<@{event.user_id}>\n当前群已开启全量消息，无需再次申请")
    await event.reply(f"<@{event.user_id}>\n{_INPUT_TIP}")


@handler(r'^全量申请\s*(\d{1,5})$', name='全量申请群号校验', desc='提示重新输入疑似错误群号')
async def reject_short_group_code(event, match):
    if event.event_type == 'GROUP_MESSAGE_CREATE':
        return await event.reply(f"<@{event.user_id}>\n当前群已开启全量消息，无需再次申请")
    await _record_apply(event, match.group(1), 'invalid_short')
    await event.reply(f"<@{event.user_id}>\n{_INVALID_GROUP_TIP}")


@handler(r'^全量列表$', name='全量列表', desc='列出所有已开启全量消息的群', owner_only=True)
async def list_full_access(event, match):
    from core.bot.manager import _bot_manager_ref
    if not _bot_manager_ref:
        return await event.reply(f"<@{event.user_id}>\n服务未就绪")
    if not hasattr(_bot_manager_ref, 'get_full_access_groups'):
        return await event.reply(f"<@{event.user_id}>\n该功能不可用，请确认 core/bot/event.py 已更新")
    groups = _bot_manager_ref.get_full_access_groups()
    if not groups:
        return await event.reply(f"<@{event.user_id}>\n暂无全量群记录")
    lines = []
    for r in groups:
        lines.append(r['group_id'])
    await event.reply(
        f"<@{event.user_id}>\n"
        f"全量群列表（共 {len(groups)} 个）：\n```群列表\n" +
        "\n".join(lines) + "\n```"
    )
