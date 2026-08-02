"""内容审核 — 调用 OpenAI 兼容接口对上传内容做合规判定 (纯文本, 不送图片)。

接口调用形式沿用框架内 AI 开发插件 (``plugins/AI开发插件/app/relay.py``):
``POST {base_url}/chat/completions`` + ``Authorization: Bearer <key>``。这里不带
任何工具调用, 只要一次判定结果。

**送审内容只有文字**: 文件清单 + 文本文件内容。图片/字体等二进制资源不上送,
版权标准也只依据文字判断 —— 图片来源未知**不构成**版权拒绝理由 (deepseek 等
纯文本模型因此可直接使用, 无需多模态支持)。

**审查标准按上传模式裁剪** (见 ``_CRITERIA`` / ``build_system_prompt``):
  · 压缩包 (完整游戏包) —— 原作出处标注 + 内容合规 + 版权(仅文字), 三条全查。
  · 单文件 (增量替换)  —— 只查内容合规 + 版权(仅文字)。单文件不是完整游戏,
    通常不含规则说明 (rule.md), 出处标注无从判断, 所以这条标准根本不进提示词,
    也不接受模型返回 origin 分类, 避免每次传补丁文件都被"缺少出处"挡下来。

**判定结果只取结构化 JSON 里的 verdict / categories**, 模型的自然语言说明与
思考过程一律不进入群消息, 由 store.py 落盘到 data/reviews/。任何异常
(HTTP 失败 / 超时 / 非 JSON / 字段缺失) 都返回 ``verdict='reject'`` +
``manual=True``, 即「默认不通过, 需人工处理」。
"""

from __future__ import annotations

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

# 审查标准正文 (按上传模式挑选组合)。内容合规一条基于 QQ 青少年保护方案核心规则,
# 从严判定: 明确违规与擦边疑似都判不通过, 疑似在 findings 里标 suspect 供人工复核。
_CRITERIA = {
    'origin': ('原作出处标注',
               '游戏的规则说明 (rule.md) 中必须注明游戏原型出自何处 (如棋类的传统出处、改编自的桌游/电子游戏名称等)。'
               '部分版权素材的授权条件要求署名。缺少出处标注 → 不通过, 分类 origin。'),
    'compliance': ('内容合规审查 (QQ 青少年保护方案, 从严判定)',
                   '审查送审的全部文字内容, 包括规则说明、代码内嵌文案与输出消息文本, 判断是否危害青少年身心健康。\n'
                   '以下为【明确违规】, 发现即判不通过 (findings 中 suspect=false):\n'
                   '  - 任何形式的色情、低俗、性暗示、裸体、性行为描写或色情链接/资源;\n'
                   '  - 暴力、血腥、恐怖、虐待、自杀、自残等直接描述或诱导;\n'
                   '  - 赌博、毒品、违禁药品、管制刀具/枪支等违法信息 (赌博相关的措辞如"押注"、"赔率"同样计入);\n'
                   '  - 诱导青少年进行危险行为、恶作剧、侵犯隐私或泄露个人信息;\n'
                   '  - 针对未成年人的网络欺凌、侮辱、歧视、仇恨言论;\n'
                   '  - 传播谣言、虚假信息, 可能引发社会恐慌或损害未成年人身心健康;\n'
                   '  - 政治敏感内容 (如分裂国家、攻击政府、歪曲历史等);\n'
                   '  - 直接提供违法工具、黑客技术、翻墙软件等。\n'
                   '以下为【疑似违规】, 同样判不通过, 但在 findings 中标 suspect=true (需人工复核):\n'
                   '  - 内容擦边、隐晦暗示但未直接明说 (如隐喻性暗示、软色情、暧昧邀请);\n'
                   '  - 不良价值观引导 (如拜金、攀比、厌学、早恋过度渲染), 但未直接教唆;\n'
                   '  - 含有轻微暴力词汇 (如游戏化打斗), 但无血腥或具体伤害描述;\n'
                   '  - 含有不确定的链接、二维码、外链, 但未明确标注其内容;\n'
                   '  - 使用变体字、谐音、符号、拼音等刻意规避检测, 但语义模糊;\n'
                   '  - 涉及未成年人交友、约见, 但未明确不良意图。\n'
                   '判断原则: 从严判定 —— 内容介于违规和疑似之间时按疑似处理 (仍判不通过); 结合上下文 —— '
                   '单句看似正常但整体语境异常 (如连续诱导) 时按最高风险级别判定; 不得以"仅供娱乐"为由保留。'
                   '→ 分类 compliance。'),
    'copyright': ('版权审查 (仅依据文字内容)',
                  '仅依据送审的**文字内容**做版权判断:\n'
                  '  - 文本/代码/规则说明中出现受版权保护的长段文字 (如歌词、小说或文章原文、他人教程整段照搬);\n'
                  '  - 文字中明确声明使用了未经授权的素材 (如"图片取自某游戏截图""字体为某商用字体"却无授权说明);\n'
                  '  - 文字中声称的授权与使用方式明显冲突 (如标注 CC BY-NC-ND 却再分发修改版)。\n'
                  '以上情况 → 不通过, 分类 copyright。\n'
                  '注意: 本次审核**不检查图片、字体等二进制资源本身**。图片来源未知、无法确认图片版权、'
                  '文件清单里存在图片/字体 —— 这些都**不构成**拒绝理由, 不得仅因此判 copyright 不通过; '
                  '只有文字内容中存在上述明确证据时才计入。'),
}

# 压缩包 = 完整游戏包, 三条全查; 单文件 = 增量替换, 只查合规与版权
ARCHIVE_CRITERIA = ('origin', 'compliance', 'copyright')
FILE_CRITERIA = ('compliance', 'copyright')

_HEAD_ARCHIVE = ('你是 QQ 机器人游戏插件的上架审核员。提交者上传了一个完整的游戏插件压缩包, '
                 '你要按下面 {n} 条标准逐条审查, 然后给出可否上架的判定。')
_HEAD_FILE = ('你是 QQ 机器人游戏插件的上架审核员。提交者只上传了**一个文件**, 要放进服务器上一个'
              '**已存在**的游戏目录里 (替换或新增), 没有随附完整游戏包。\n'
              '由于单文件不是完整游戏、通常不包含规则说明 (rule.md), 本次**只审查下面 {n} 条标准: '
              '内容合规与版权**, 不审查原作出处标注 —— 不要因为"看不到 rule.md""没有说明规则"'
              '"缺少出处标注"而判不通过, 也不要返回 origin 分类。请仅就本文件自身的内容与素材来源作判断。')

_OUTPUT_FORMAT = """【输出格式】
只输出一个 JSON 对象, 不要任何解释文字、不要 markdown 代码块以外的内容:
{{
  "verdict": "pass" 或 "reject",
  "categories": [{cats}],
  "summary": "一句话结论",
  "findings": [{{"category": "...", "target": "包内文件相对路径", "line": 行号整数, "suspect": true或false, "reason": "为什么不通过"}}]
}}
verdict 为 pass 时 categories 与 findings 必须为空数组。上述 {n} 条标准中任意一条不满足即 reject, 并在 categories 中列出全部命中的分类。categories 只允许使用上面列出的取值。判断依据不足时按 reject 处理并说明缺什么。

findings 填写要求 (每一处问题单独一条):
- target: 只写文件在包内的相对路径 (与文件清单一致), **严禁把违规内容原文写进 target**;
- line: 违规所在行号 (送审文本每行已带 "行号|" 前缀, 直接引用该数字); 整个文件性质的问题填 0;
- suspect: 明确违规填 false, 疑似违规 (擦边、需人工复核) 填 true;
- reason: 简要说明违反哪条规则, 仅供后台留档, 不会公开展示。"""

_CN_NUM = ('一', '二', '三', '四', '五')


def criteria_keys(mode: str) -> tuple:
    """本次审核适用的标准 (也就是允许出现的拒绝分类)。"""
    return FILE_CRITERIA if mode == 'file' else ARCHIVE_CRITERIA


def build_system_prompt(mode: str, extra: str = '') -> str:
    """按上传模式拼装系统提示词: 不适用的标准根本不进提示词。"""
    keys = criteria_keys(mode)
    head = (_HEAD_FILE if mode == 'file' else _HEAD_ARCHIVE).format(n=len(keys))
    parts = [head]
    for i, key in enumerate(keys):
        title, body = _CRITERIA[key]
        parts.append(f'【标准{_CN_NUM[i]} · {title}】\n{body}')
    cats = ' | '.join(f'"{k}"' for k in keys + ('other',))
    parts.append(_OUTPUT_FORMAT.format(cats=cats, n=len(keys)))
    extra = (extra or '').strip()
    if extra:
        parts.append(f'【补充要求】\n{extra}')
    return '\n\n'.join(parts)


def _build_digest(pkg: dict, meta: dict) -> str:
    """把文件清单 + 文本内容拼成送审正文。"""
    if meta.get('mode') == 'file':
        lines = [
            '# 待审核单文件 (增量上传, 只审内容合规与版权)',
            f'文件: {meta.get("filename", "")}  ({meta.get("size", 0) / 1024:.1f} KB)',
            f'目标位置: {meta.get("target", "")} / {meta.get("folder", "")}/',
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
    lines += ['', '## 文本内容 (每行前缀为 "行号|", findings.line 直接引用该数字)']
    for t in pkg['texts']:
        lines.append(f'\n### {t["path"]}' + ('  (已截断)' if t['truncated'] else ''))
        lines.append('```')
        lines.append(_number_lines(t['content']))
        lines.append('```')
    if not pkg['texts']:
        lines.append('（没有可读的文本文件）')
    return '\n'.join(lines)


def _number_lines(content: str) -> str:
    """给送审文本加 "行号|" 前缀, 让模型能精确报告违规行号。"""
    return '\n'.join(f'{i}|{line}' for i, line in enumerate((content or '').split('\n'), 1))


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


def _norm_categories(value, allowed: tuple = ARCHIVE_CRITERIA) -> list:
    """分类归一到 origin/compliance/copyright/other, 去重保序。

    ``allowed`` 之外的分类一律丢弃 —— 单文件模式不审出处标注, 即使模型无视提示词
    返回了 origin 也不会显示成「原作出处标注」(见模块 docstring)。
    """
    if isinstance(value, str):
        value = [value]
    out = []
    for item in value or []:
        key = str(item).strip().lower()
        if key in CATEGORY_LABELS:
            norm = key
        else:
            norm = next((v for k, v in _ALIAS.items() if k.lower() in key), 'other')
        if norm != 'other' and norm not in allowed:
            continue
        if norm not in out:
            out.append(norm)
    return out


def labels(categories: list) -> str:
    return '、'.join(CATEGORY_LABELS.get(c, c) for c in categories) or '未标注分类'


_TARGET_BAD = re.compile(r'[\r\n\t"\'`<>]')


def _norm_findings(value, allowed: tuple) -> list:
    """规整 findings 为 [{category, target, line, suspect}]。

    group 消息与面板只展示这里清洗过的字段: target 剥掉引号/换行等非路径字符并
    截断, line 强转非负整数 —— 即使模型违规把原文塞进来, 也只会剩下一段截断的
    "疑似路径", 不会把违规内容原样带进群聊。reason 不进入该结构 (完整原文在
    data/reviews/ 留档里)。
    """
    out = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        cats = _norm_categories([item.get('category')], allowed)
        target = _TARGET_BAD.sub('', str(item.get('target') or '')).strip()[:100]
        try:
            line = max(0, int(item.get('line') or 0))
        except (TypeError, ValueError):
            line = 0
        out.append({
            'category': cats[0] if cats else 'other',
            'target': target or '(未标注位置)',
            'line': line,
            'suspect': bool(item.get('suspect')),
        })
    return out[:20]


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
    """执行一次审核 (纯文本)。``meta['mode']`` 决定审查哪几条标准 (见模块 docstring)。

    返回 ``{verdict, categories, manual, error, raw, model, elapsed, summary,
    findings, criteria}``。``manual=True`` 表示这次结果来自异常兜底而非模型的
    正常判定 —— 需人工处理。
    """
    start = time.time()
    mode = meta.get('mode') or 'archive'
    allowed = criteria_keys(mode)
    messages = [
        {'role': 'system', 'content': build_system_prompt(mode, cfg.get('review_prompt'))},
        {'role': 'user', 'content': _build_digest(pkg, meta)},
    ]
    resp, err = await _post(messages, cfg)
    elapsed = round(time.time() - start, 1)
    base: dict = {'verdict': 'reject', 'categories': ['other'], 'manual': True, 'error': err,
                  'raw': '', 'model': cfg.get('model', ''), 'elapsed': elapsed,
                  'summary': '', 'findings': [], 'criteria': list(allowed)}
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

    findings = _norm_findings(data.get('findings'), allowed)
    cats = _norm_categories(data.get('categories'), allowed)
    if verdict == 'reject' and not cats:
        cats = _norm_categories([f['category'] for f in findings], allowed) or ['other']
    return {
        'verdict': verdict,
        'categories': [] if verdict == 'pass' else cats,
        'manual': False,
        'error': '',
        'raw': base['raw'],
        'model': base['model'],
        'elapsed': elapsed,
        'summary': str(data.get('summary', ''))[:500],
        'findings': [] if verdict == 'pass' else findings,
        'criteria': list(allowed),
    }


async def probe() -> dict:
    """面板「测试连接」: 用一句最小请求验证密钥/地址/模型可用。"""
    cfg = config.all_config()
    resp, err = await _post([{'role': 'user', 'content': '回复 ok'}], cfg)
    if resp is None:
        return {'ok': False, 'error': err}
    text = ((resp.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    return {'ok': True, 'model': resp.get('model') or cfg.get('model', ''), 'reply': str(text)[:100]}
