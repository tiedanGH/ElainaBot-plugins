# -*- coding: utf-8 -*-
"""审核未通过 / 编译失败的静态 HTML 报告页。

群消息里放不下的明细 (每条 finding 的完整说明、编译器日志尾部) 原本只进后台留档,
而上传者进不了后台 —— 于是每次失败都要管理员转述一遍。这里把这些明细渲染成一个
静态页落到 ``data/reports/``, 群消息里附一条 markdown 链接, 上传者自己点开就能看。

本插件**只负责生成文件与拼出链接**: 页面怎么对外提供由站点侧的反向代理决定, 插件
只需要一个 ``report_base_url`` 前缀 (面板配置), 链接 = 前缀 + 文件名。

两点安全约束, 改这个模块时别丢:

1. **文件名带随机串** (``<记录号>-<16位随机>.html``)。reports 目录是被 Web 服务器
   直接对外提供的, 没有任何鉴权; 用纯记录号命名等于让任何人按时间戳枚举出全部失败
   报告 —— 里面有上传者昵称、源码片段与违规说明。随机串把 URL 变成不可枚举的能力
   凭证 (加上 ``noindex`` 防搜索引擎收录)。
2. **页面里所有动态文字一律 ``html.escape``**。报告内容全部来自不可信来源: 模型输出
   的 reason、上传包内的文件路径、编译器打印的源码片段 —— 直接拼进 HTML 就是一个
   存储型 XSS, 而这个页面还挂在你自己的域名下。本模块只在 ``_esc()`` 之后拼串,
   任何新增字段都必须走它。
"""

from __future__ import annotations

import html
import os
import time

from core.base.logger import PLUGIN, get_logger

from . import review, store
from .config import DATA_DIR

log = get_logger(PLUGIN, 'LGTBot自动部署')

LINK_TEXT = '📄 点击查看完整报告'

_STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2328;--dim:#656d76;--line:#d8dee4;
--bad:#cf222e;--warn:#bc4c00;--tagbg:#fff1f0;--pre:#f6f8fa}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;
--dim:#9198a1;--line:#30363d;--bad:#ff7b72;--warn:#d29922;--tagbg:#2d1618;--pre:#0d1117}}
*{box-sizing:border-box}
body{margin:0;padding:20px 14px 48px;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto}
header{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--bad);
border-radius:8px;padding:18px 20px;margin-bottom:16px}
header.warn{border-left-color:var(--warn)}
h1{margin:6px 0 4px;font-size:21px}
.sub{margin:0;color:var(--dim);font-size:14px;word-break:break-all}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;
font-weight:600;background:var(--tagbg);color:var(--bad)}
header.warn .badge{color:var(--warn)}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:16px 20px;margin-bottom:16px}
h2{margin:0 0 12px;font-size:15px;color:var(--dim);font-weight:600;
letter-spacing:.04em;text-transform:none}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:7px 18px;font-size:14px}
.kv .k{color:var(--dim);white-space:nowrap}
.kv .v{word-break:break-all}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:13px}
tr:last-child td{border-bottom:none}
td.n{color:var(--dim);width:1%}
.tag{display:inline-block;padding:1px 8px;border-radius:4px;font-size:12.5px;
white-space:nowrap;background:var(--tagbg);color:var(--bad)}
.mono,pre{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace}
.mono{font-size:13px;word-break:break-all}
pre{background:var(--pre);border:1px solid var(--line);border-radius:6px;
padding:12px 14px;overflow-x:auto;font-size:12.5px;line-height:1.55;margin:0;
white-space:pre-wrap;word-break:break-word}
.tip{margin:0;padding:11px 14px;border-radius:6px;background:var(--pre);
border:1px solid var(--line);font-size:14px}
.tip code{background:var(--tagbg);padding:1px 6px;border-radius:4px}
footer{color:var(--dim);font-size:12.5px;text-align:center;line-height:1.8}
"""

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{title} · {rid}</title>
<style>{style}</style>
</head>
<body>
<div class="wrap">
<header class="{cls}">
<div class="badge">{badge}</div>
<h1>{title}</h1>
<p class="sub">{sub}</p>
</header>
{body}
<footer>本页由 LGTBot 自动部署插件自动生成 · {now}<br>记录号 {rid}</footer>
</div>
</body>
</html>
"""


# ==================== 渲染基元 (全部经转义) ====================

def _esc(v) -> str:
    return html.escape(str('' if v is None else v), quote=True)


def _sec(title: str, inner: str) -> str:
    return f'<section><h2>{_esc(title)}</h2>{inner}</section>' if inner else ''


def _kv(rows) -> str:
    """键值表; 值为空的行整行略去, 免得页面上一排「-」。"""
    cells = ''.join(f'<div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div>'
                    for k, v in rows if v not in (None, ''))
    return f'<div class="kv">{cells}</div>' if cells else ''


def _pre(text: str) -> str:
    return f'<pre>{_esc(text)}</pre>' if str(text or '').strip() else ''


def _tip(inner_html: str) -> str:
    """提示条。**唯一**允许传入 HTML 的地方, 调用方只拼固定文案与已转义片段。"""
    return f'<p class="tip">{inner_html}</p>'


def _size(n) -> str:
    return f'{(n or 0) / 1048576:.2f} MB'


def _meta_rows(record: dict) -> list:
    """两种报告共用的提交信息。不放 user_id —— openid 没有展示价值, 少露一样是一样。"""
    folder = record.get('folder') or ''
    target = f'{record.get("target", "")}/{folder}' if folder else record.get('target', '')
    return [
        ('提交时间', record.get('time')),
        ('提交者', record.get('username') or '(未知)'),
        ('文件', record.get('filename')),
        ('大小', _size(record.get('size'))),
        ('落地目标', target),
        ('上传模式', '单文件增量' if record.get('mode') == 'file' else '压缩包'),
    ]


# ==================== 审核未通过 ====================

def build_review(record: dict, result: dict) -> str:
    findings = record.get('findings') or result.get('findings') or []
    cats = record.get('categories') or result.get('categories') or []
    suspect_only = bool(findings) and all(f.get('suspect') for f in findings)

    rows = _meta_rows(record)
    if record.get('game_name'):
        rows.insert(0, ('游戏名称', record['game_name']))
    rows += [
        ('未通过分类', review.labels(cats)),
        ('审查标准', review.criteria_labels(record.get('criteria') or [])),
        ('审核模型', record.get('model')),
    ]

    body = _sec('提交信息', _kv(rows)) + _findings_table(findings) + _sec(
        '接下来怎么办',
        _tip('按上表逐条修改后重新上传即可。注意<b>内容必须实际改动</b> —— '
             '与上次字节完全相同的压缩包会被查重直接拒收。'
             '若认为是误判, 请联系管理员人工复核。'))

    return _render(
        title='审核未通过' + (' (疑似违规, 待人工复核)' if suspect_only else ''),
        badge='内容审核', cls='' if not suspect_only else 'warn',
        sub=f'{record.get("filename", "")} · {review.labels(cats)}',
        rid=record.get('id', ''), body=body)


def _findings_table(findings) -> str:
    if not findings:
        return ''
    rows = []
    for i, f in enumerate(findings, 1):
        label = review.CATEGORY_LABELS.get(f.get('category'), '其他')
        if f.get('suspect'):
            label += ' · 疑似'
        loc = str(f.get('target') or '(未标注位置)')
        if f.get('line'):
            loc += f':{f["line"]}'
        rows.append(f'<tr><td class="n">{i}</td>'
                    f'<td><span class="tag">{_esc(label)}</span></td>'
                    f'<td class="mono">{_esc(loc)}</td>'
                    f'<td>{_esc(f.get("reason") or "")}</td></tr>')
    table = ('<table><tr><th>#</th><th>分类</th><th>位置</th><th>说明</th></tr>'
             + ''.join(rows) + '</table>')
    return _sec(f'违规明细 ({len(findings)} 处)', table)


# ==================== 编译失败 ====================

def build_compile(record: dict, result: dict, game: str) -> str:
    from . import compile as compilemod          # 延迟导入: compile 不依赖本模块

    st = result.get('status')
    timeout = st == 'timeout'
    rows = [('游戏目录', game),
            ('编译模式', '新游戏完整编译' if result.get('new') else '增量编译'),
            ('状态', compilemod.STATUS_LABELS.get(st, st)),
            ('HTTP', result.get('http_status') or '-')]
    if result.get('returncode') is not None:
        rows.append(('退出码', result['returncode']))
    if result.get('elapsed_sec') is not None:
        rows.append(('用时', f'{result["elapsed_sec"]}s'))
    term = result.get('terminate')
    if term:
        rows.append(('取消请求', ('成功' if term.get('ok') else '失败')
                     + f' ({term.get("message", "")})'))
    rows += _meta_rows(record)

    # error 与 log_tail 都可能带出编译机的 IP:端口 —— 这页在公网上, 先遮蔽
    body = _sec('编译信息', _kv(rows))
    body += _sec('失败原因', _pre(review.mask_addr(result.get('error'))))
    body += _sec('编译日志 (尾部)', _pre(review.mask_addr(result.get('log_tail'))))
    # 出路按失败性质分岔, 别把两条路一起摆出来 (判据见 compile.is_transient)
    if compilemod.is_transient(result):
        body += _sec('接下来怎么办', _tip(
            '这属于<b>临时问题</b>（编译进程被占用、编译服务未就绪等），源码本身没问题、'
            f'也已经在服务器上，<b>无需重传</b> —— 在群里发 <code>/compile {_esc(game)}</code> '
            '重试即可。'))
    elif st == 'invalid':
        body += _sec('接下来怎么办', _tip(
            '编译接口只接受纯英文目录名（字母 / 数字 / 下划线 / 连字符，首字符为字母或下划线）。'
            '重新编译一样过不去，请改用合规的目录名后重新 <code>/upload</code> 上传。'))
    else:
        body += _sec('接下来怎么办', _tip(
            '编译器报错，<b>代码本身无法编译</b>。请按上面的编译日志修改源码，'
            '再重新 <code>/upload</code> 上传。'))
    return _render(
        title='编译超时' if timeout else '编译失败',
        badge='自动编译', cls='warn' if timeout else '',
        sub=f'{game} · {compilemod.STATUS_LABELS.get(st, st or "")}',
        rid=record.get('id', ''), body=body)


# ==================== 落盘与链接 ====================

def _render(title: str, badge: str, cls: str, sub: str, rid: str, body: str) -> str:
    return _PAGE.format(style=_STYLE, title=_esc(title), badge=_esc(badge), cls=_esc(cls),
                        sub=_esc(sub), rid=_esc(rid), body=body,
                        now=time.strftime('%Y-%m-%d %H:%M:%S'))


def _filename(rid: str) -> str:
    """``<记录号>-<16 位随机>.html`` —— 随机串让 URL 不可枚举 (见模块 docstring)。"""
    return f'{store.safe_filename(rid) or "report"}-{os.urandom(8).hex()}.html'


def generate(cfg: dict, record: dict, kind: str, result: dict | None = None,
             game: str = '') -> str:
    """生成报告页, 登记 ``record['report_file'] / ['report_url']``, 返回 markdown 链接。

    没配 ``report_base_url`` 就整套跳过 —— 没有外部访问地址时, 报告文件只是白占磁盘。
    生成失败一律返回空串: 报告是锦上添花, 不能因为它让失败结论发不出去。
    """
    base = str((cfg or {}).get('report_base_url') or '').strip()
    if not base:
        return ''
    try:
        page = (build_review(record, result or {}) if kind == 'review'
                else build_compile(record, result or {}, game))
        name = _filename(str(record.get('id') or ''))
        store.init()
        path = os.path.join(store.REPORTS_DIR, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(page)
    except Exception as e:                       # noqa: BLE001 - 见 docstring
        # 吞掉但不静默: 报告页出不来只是少个链接, 失败结论照发; 但如果是长期坏的
        # (比如目录权限不对), 只看群消息永远发现不了
        log.warning(f'生成失败报告页失败 ({record.get("id")}): {type(e).__name__}: {e}')
        return ''
    record['report_file'] = os.path.relpath(path, DATA_DIR).replace('\\', '/')
    record['report_url'] = base.rstrip('/') + '/' + name
    return f'[{LINK_TEXT}]({record["report_url"]})'
