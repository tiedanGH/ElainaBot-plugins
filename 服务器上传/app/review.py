"""内容审核 — 调用 OpenAI 兼容接口对上传包做合规判定。

接口调用形式沿用框架内 AI 开发插件 (``plugins/AI开发插件/app/relay.py``):
``POST {base_url}/chat/completions`` + ``Authorization: Bearer <key>``。这里不带
任何工具调用, 只要一次判定结果。

**判定结果只取结构化 JSON 里的 verdict / categories**, 模型的自然语言说明与
思考过程一律不进入群消息, 由 store.py 落盘到 data/reviews/。任何异常
(HTTP 失败 / 超时 / 非 JSON / 字段缺失) 都返回 ``verdict='reject'`` +
``manual=True``, 即「默认不通过, 需人工处理」。
"""

from __future__ import annotations

import base64
import json
import re
import time

import aiohttp

from . import config

# 拒绝分类 → 群内展示名
CATEGORY_LABELS = {
    'origin': '原作出处标注',
    'compliance': '内容合规',
    'copyright': '版权素材',
    'other': '其他',
}
_ALIAS = {
    '原作出处标注': 'origin', '出处': 'origin', '署名': 'origin', 'attribution': 'origin',
    '内容合规': 'compliance', '合规': 'compliance', '赌博': 'compliance',
    '色情': 'compliance', '政治': 'compliance',
    '版权素材': 'copyright', '版权': 'copyright', '素材': 'copyright', 'license': 'copyright',
}

SYSTEM_PROMPT = """你是 QQ 机器人游戏插件的上架审核员。用户提交一个游戏插件压缩包, 你要按下面三条标准逐条审查, 然后给出可否上架的判定。

【标准一 · 原作出处标注】
游戏的规则说明 (rule.md) 中必须注明游戏原型出自何处 (如棋类的传统出处、改编自的桌游/电子游戏名称等)。部分版权素材的授权条件要求署名。缺少出处标注 → 不通过, 分类 origin。

【标准二 · 内容合规审查】
审查游戏的全部输出内容, 包括文字消息和图片 UI, 确保符合中国社交软件平台的内容规范。重点排查以下类别: 赌博相关的语言或暗示 (如"押注"、"赔率"等措辞)、色情或擦边内容、政治敏感内容。如发现潜在违规, 必须调整措辞或设计, 不得以"仅供娱乐"为由保留 —— 即发现此类内容即判不通过, 分类 compliance。

【标准三 · 版权审查】
检查游戏使用的全部美术作品、图片素材、字体等资源是否受版权保护。默认不得使用任何受版权保护的素材。若某素材声称可用, 需进一步判断其授权范围是否覆盖"在 QQ 机器人中公开、非盈利使用" —— 注意有版权 ≠ 禁止使用, 许多授权允许非商业用途, 但也有许多要求署名、禁止再分发或禁止任何形式公开使用。判断不确定或授权不明确时, 一律拒绝使用该素材, 并明确指出拒绝原因 (具体是哪个素材、属于什么版权、为何不可用), 等待提交者提供合规替代方案 → 分类 copyright。

【输出格式】
只输出一个 JSON 对象, 不要任何解释文字、不要 markdown 代码块以外的内容:
{
  "verdict": "pass" 或 "reject",
  "categories": ["origin" | "compliance" | "copyright" | "other"],
  "summary": "一句话结论",
  "findings": [{"category": "...", "target": "具体文件或素材", "reason": "为什么不通过"}]
}
verdict 为 pass 时 categories 与 findings 必须为空数组。三条标准中任意一条不满足即 reject, 并在 categories 中列出全部命中的分类。判断依据不足时按 reject 处理并说明缺什么。"""


# 单文件模式的附加说明: 增量替换已有游戏目录里的某个文件, 不该因为"没有 rule.md"被拒
SINGLE_FILE_NOTE = """【本次为单文件增量上传】
提交者是把这一个文件放进服务器上一个**已存在**的游戏目录里 (替换或新增), 没有随附完整游戏包。
因此标准一 (原作出处标注) 只在本文件本身就是 rule.md / 说明文档时才适用; 若本文件不是说明文档,
不要因为"看不到 rule.md""缺少出处标注"而判不通过。标准二 (内容合规) 与标准三 (版权素材) 照常严格执行,
仅就本文件的内容与素材来源做判断; 信息不足以确认素材来源合法时按 reject 处理并说明缺什么。"""


def _build_digest(pkg: dict, meta: dict) -> str:
    """把文件清单 + 文本内容拼成送审正文。"""
    if meta.get('mode') == 'file':
        lines = [
            '# 待审核单文件 (增量上传)',
            f'文件: {meta.get("filename", "")}  ({meta.get("size", 0) / 1024:.1f} KB)',
            f'目标位置: {meta.get("target", "")} / {meta.get("folder", "")}/',
            '',
            SINGLE_FILE_NOTE,
            '',
            '## 文件清单',
        ]
    else:
        lines = [
            '# 待审核游戏插件包',
            f'压缩包: {meta.get("filename", "")}  ({meta.get("size", 0) / 1048576:.2f} MB)',
            f'解压后: {len(pkg["tree"])} 个文件, {meta.get("total_size", 0) / 1048576:.2f} MB',
            f'rule 说明文件: {", ".join(pkg["rule_files"]) or "（未发现 rule.md）"}',
            '',
            '## 文件清单',
        ]
    for item in pkg['tree']:
        lines.append(f'- {item["path"]}  [{item["kind"]}, {item["size"]} B]')
    imgs = [t for t in pkg['tree'] if t['kind'] == 'image']
    fonts = [t for t in pkg['tree'] if t['kind'] == 'font']
    lines += [
        '',
        f'## 美术/字体资源统计: 图片 {len(imgs)} 个, 字体 {len(fonts)} 个',
    ]
    if fonts:
        lines += [f'- 字体: {f["path"]}' for f in fonts]
    if pkg['images']:
        lines += ['', '## 随附图片 (已作为图像一并上送, 顺序同下)']
        lines += [f'- {i + 1}. {im["path"]} ({im["width"]}x{im["height"]})'
                  for i, im in enumerate(pkg['images'])]
    lines += ['', '## 文本内容']
    for t in pkg['texts']:
        lines.append(f'\n### {t["path"]}' + ('  (已截断)' if t['truncated'] else ''))
        lines.append('```')
        lines.append(t['content'])
        lines.append('```')
    if not pkg['texts']:
        lines.append('（包内没有可读的文本文件）')
    return '\n'.join(lines)


def _build_content(pkg: dict, meta: dict):
    """无图片时返回纯文本; 有图片时返回 OpenAI 多模态 content 数组。"""
    digest = _build_digest(pkg, meta)
    if not pkg['images']:
        return digest
    content = [{'type': 'text', 'text': digest}]
    for im in pkg['images']:
        ext = im['path'].rsplit('.', 1)[-1].lower()
        mime = 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'
        b64 = base64.b64encode(im['data']).decode('ascii')
        content.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}})
    return content


def _extract_json(text: str) -> dict | None:
    """从模型回复里取出 JSON 对象 (兼容 ```json 围栏与前后夹杂文字)。"""
    if not text:
        return None
    fence = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    raw = fence.group(1) if fence else None
    if raw is None:
        start = text.find('{')
        end = text.rfind('}')
        raw = text[start:end + 1] if 0 <= start < end else ''
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _norm_categories(value) -> list:
    """分类归一到 origin/compliance/copyright/other, 去重保序。"""
    if isinstance(value, str):
        value = [value]
    out = []
    for item in value or []:
        key = str(item).strip().lower()
        if key in CATEGORY_LABELS:
            norm = key
        else:
            norm = next((v for k, v in _ALIAS.items() if k.lower() in key), 'other')
        if norm not in out:
            out.append(norm)
    return out


def labels(categories: list) -> str:
    return '、'.join(CATEGORY_LABELS.get(c, c) for c in categories) or '未标注分类'


async def _post(messages: list, cfg: dict) -> tuple[dict | None, str]:
    """单次 /chat/completions 调用, 返回 (响应 JSON, 错误信息)。"""
    key = config.api_key()
    if not key:
        return None, '未配置接口密钥'
    url = config.base_url() + '/chat/completions'
    payload = {
        'model': cfg.get('model') or 'gpt-4.1-nano',
        'messages': messages,
        'temperature': cfg.get('temperature', 0.2),
    }
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    timeout = aiohttp.ClientTimeout(total=max(30, int(cfg.get('request_timeout', 180))))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            for attempt in range(2):
                async with s.post(url, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        try:
                            return json.loads(text), ''
                        except json.JSONDecodeError:
                            return None, f'上游返回非 JSON: {text[:200]}'
                    # 个别推理模型拒绝自定义 temperature: 去掉后重试一次
                    if attempt == 0 and resp.status == 400 and 'temperature' in text.lower():
                        payload.pop('temperature', None)
                        continue
                    return None, f'HTTP {resp.status}: {text[:300]}'
    except Exception as e:  # noqa: BLE001 — 网络异常一律按「不通过, 需人工」处理
        return None, f'{type(e).__name__}: {e}'
    return None, '调用失败'


async def review(pkg: dict, meta: dict, cfg: dict) -> dict:
    """执行一次审核。

    返回 ``{verdict, categories, manual, error, raw, model, elapsed, summary, findings}``。
    ``manual=True`` 表示这次结果来自异常兜底而非模型的正常判定 —— 需人工处理。
    """
    start = time.time()
    extra = (cfg.get('review_prompt') or '').strip()
    system = SYSTEM_PROMPT + (f'\n\n【补充要求】\n{extra}' if extra else '')
    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': _build_content(pkg, meta)},
    ]
    resp, err = await _post(messages, cfg)
    elapsed = round(time.time() - start, 1)
    base = {'verdict': 'reject', 'categories': ['other'], 'manual': True, 'error': err,
            'raw': '', 'model': cfg.get('model', ''), 'elapsed': elapsed,
            'summary': '', 'findings': []}
    if resp is None:
        return base

    choice = (resp.get('choices') or [{}])[0]
    msg = choice.get('message') or {}
    raw = msg.get('content') or ''
    base['raw'] = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    base['model'] = resp.get('model') or base['model']
    data = _extract_json(base['raw'])
    if data is None:
        base['error'] = '无法从回复中解析出审核结论 JSON'
        return base

    verdict = str(data.get('verdict', '')).strip().lower()
    if verdict not in ('pass', 'reject'):
        base['error'] = f'审核结论字段非法: {verdict!r}'
        return base

    findings = data.get('findings') if isinstance(data.get('findings'), list) else []
    cats = _norm_categories(data.get('categories'))
    if verdict == 'reject' and not cats:
        cats = _norm_categories([f.get('category') for f in findings if isinstance(f, dict)]) or ['other']
    return {
        'verdict': verdict,
        'categories': [] if verdict == 'pass' else cats,
        'manual': False,
        'error': '',
        'raw': base['raw'],
        'model': base['model'],
        'elapsed': elapsed,
        'summary': str(data.get('summary', ''))[:500],
        'findings': findings,
    }


async def probe() -> dict:
    """面板「测试连接」: 用一句最小请求验证密钥/地址/模型可用。"""
    cfg = config.all_config()
    resp, err = await _post([{'role': 'user', 'content': '回复 ok'}], cfg)
    if resp is None:
        return {'ok': False, 'error': err}
    text = ((resp.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    return {'ok': True, 'model': resp.get('model') or cfg.get('model', ''), 'reply': str(text)[:100]}
