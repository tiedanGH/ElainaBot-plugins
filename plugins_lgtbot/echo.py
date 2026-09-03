"""echo: 主人专用消息回放 (text / markdown)

- echo text XXX  → 纯文本输出 (msg_type=0)
- echo md XXX    → markdown 输出 (msg_type=2)
- 支持字面 `\\n` / `\\r\\n` 自动转为真实换行 (用户在输入框打不出真换行也能用)
- 若消息携带 QQ 引用 (message_reference 等字段), 改用引用消息的内容作为输入
  (探测 event.raw 中可能的引用字段; QQ 官方 OpenAPI 是否传原文取决于协议版本,
   探测不到则 fallback 到 echo 后的参数)
"""

from core.plugin.decorators import handler
from core.message._http import MSG_TYPE_TEXT, MSG_TYPE_MARKDOWN


def _extract_quoted_content(event):
    """从 event.msg_elements 探测被引用消息的原文; 返回 str 或 None.

    QQ 群消息携带引用时, 引用消息以 dict 形式塞在 d.msg_elements 里
    (典型字段: author/content/message_type/msg_idx)。框架在
    parse_message_generic 中已把 d.msg_elements 拷到 event.msg_elements,
    这里直接读第一条带 content 的元素即可。
    """
    elements = getattr(event, 'msg_elements', None) or []
    if not isinstance(elements, list):
        return None
    for el in elements:
        if not isinstance(el, dict):
            continue
        content = el.get('content')
        if content:
            return str(content)
    return None


def _unescape(s):
    """字面 \\r\\n / \\n / \\r 转真换行 (\\r\\n 优先, 避免双重转义)"""
    return s.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\r', '')


@handler(r'^/?echo\s+text(?:\s+(\S.*?))?\s*$', name='echo-text',
         desc='主人指令: 以纯文本回显',
         owner_only=True, ignore_at_check=True, block=True)
async def cmd_echo_text(event, match):
    # 优先用引用内容; 没引用就用 echo text 后面的参数; 两者都没 → 静默跳过
    content = _extract_quoted_content(event) or match.group(1)
    if not content:
        return
    content = _unescape(content)
    await event.reply(content, msg_type=MSG_TYPE_TEXT)


@handler(r'^/?echo\s+md(?:\s+(\S.*?))?\s*$', name='echo-md',
         desc='主人指令: 以 markdown 回显',
         owner_only=True, ignore_at_check=True, block=True)
async def cmd_echo_md(event, match):
    content = _extract_quoted_content(event) or match.group(1)
    if not content:
        return
    content = _unescape(content)
    await event.reply(content, msg_type=MSG_TYPE_MARKDOWN)
