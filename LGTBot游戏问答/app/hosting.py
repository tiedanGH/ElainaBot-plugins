"""Image Hosting 模块适配：判断现在能不能发 Markdown 图片消息。

**刻意不做上传。** 图是 AI 画图那边生成的，它在把链接返回给我们之前，已经用
**同一个** `image_hosting` 模块传过一次了（AI画图/app/capability.py：先 upload
再 `delivered_url = 图床链接 or 接口原始链接`）。这边再下载回来传一遍纯属重复，
还多一次失败机会。

所以本模块只回答一个问题：图床可不可用 —— 可用就走 Markdown 图片消息（能带
`<@用户>`），不可用就退回富媒体图片消息。

拿到的链接万一**不是**图床直链（比如上游那次上传失败了），Markdown 会被 QQ 拒收
（域名没在开放平台报备），main.py 那边按「没返回消息 ID」判定未送达并自动退回富媒体，
不需要在这里猜链接的来历。
"""
from __future__ import annotations


def get_module():
    """返回 Image Hosting 模块实例；模块未启用或初始化失败时返回 None。"""
    try:
        from core.application import get_app
    except ImportError:
        return None
    app = get_app()
    manager = getattr(app, 'module_manager', None) if app else None
    return manager.get('image_hosting') if manager is not None else None


def status() -> dict:
    """各图床的可用状态。取不到一律按不可用处理。"""
    module = get_module()
    if module is None:
        return {}
    try:
        return dict(module.status() or {})
    except Exception:  # noqa: BLE001 — 状态查询失败按不可用处理
        return {}


def available() -> bool:
    return any(status().values())


def state() -> dict:
    """面板诊断：图床装没装、有没有可用的。"""
    module = get_module()
    if module is None:
        return {
            'installed': False, 'available': False, 'beds': {},
            'message': '未启用 Image Hosting 模块，画图结果将走富媒体图片消息',
        }
    beds = status()
    usable = [name for name, ok in beds.items() if ok]
    return {
        'installed': True,
        'available': bool(usable),
        'beds': beds,
        'message': (
            f"可用图床：{'、'.join(usable)}（画图结果走 Markdown 图片消息）" if usable
            else 'Image Hosting 已启用但没有可用图床，画图结果将走富媒体图片消息'
        ),
    }
