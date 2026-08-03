"""LGTBot 编译 API 客户端 — 审核通过部署后自动请求单目标编译。

对接主框架插件 LGTBot_ElainaBot 开放的编译 API
(``plugins/LGTBot_ElainaBot/mod/webui/build_api.py``, 路由以 auth=False 注册,
靠独立 token 认证; token 在 LGTBot 面板「引擎编译」标签有一键复制按钮):

  · ``POST /api/ext/lgtbot/build/compile``   body ``{"target": "<游戏名>", "new": bool}``
    同步等待编译结束才响应 (服务端自身等待上限 600s)。
    Header: ``Authorization: Bearer <token>`` (或 ``X-API-Token``)。
    ``new=true`` 时 build.sh 不带 ``-i``, 走完整流程 (依赖自检 + CMake 重新
    配置) —— 新增游戏目录只有重跑 configure 才进 CMake 缓存, 耗时更长;
    缺省 / false 走增量编译 (秒级~分钟级)。**老游戏更新不带 new 参数**。
    - 200  {success: true, target, message, elapsed_sec, active_matches}
           elapsed_sec = 编译用时; active_matches = 进行中对局数 (重启需等它归零)
    - 400  缺 target / target 名非法 (仅字母/数字/下划线/连字符, 1-63 字符,
           首字符为字母或下划线)
    - 401  token 缺失或错误
    - 409  已有编译在进行 / build 目录缺失
    - 500  编译失败 {error, target, returncode, elapsed_sec, log_tail}
    - 503  引擎在用预编译包 / build.sh 缺失 (编译服务不可用)
    - 504  服务端等待 600s 仍未结束 (进程保留)
  · ``POST /api/ext/lgtbot/build/terminate`` 强制中断当前编译
    - 200 {success: true, message} / 401 / 409 没有编译在进行 / 500 终止失败

本客户端按配置 ``compile_timeout`` (默认 180s) 等待; 超时即调 terminate 取消
编译, 返回 status='timeout'。所有结果归一为:

    {'status': 'success'|'failed'|'timeout'|'error'|'invalid'|'disabled',
     'ok': bool, 'new': bool, 'error': str, 'http_status': int,
     'elapsed_sec': float|None, 'active_matches': int|None,
     'returncode': int|None, 'log_tail': str, 'terminate': dict|None, 'raw': str}

status 含义: success=编译成功; failed=API 明确返回失败(含 4xx/5xx);
timeout=等待超时已发取消; error=网络/未知异常; invalid=目标名不符合 API 白名单
(含中文等, 不发请求); disabled=面板未启用自动编译。new 回显本次是否按新游戏
完整编译。
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import aiohttp

from . import config

# 与 LGTBot page_build._TARGET_RE 一致: 不合规的目标名连请求都不必发
_TARGET_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_\-]{0,62}$')

_COMPILE_PATH = '/api/ext/lgtbot/build/compile'
_TERMINATE_PATH = '/api/ext/lgtbot/build/terminate'
_TERMINATE_TIMEOUT = 15


def base_url(cfg: dict) -> str:
    """编译 API 地址: 配置留空时自动指向本机框架端口 (编译插件同进程)。"""
    url = str((cfg or {}).get('compile_url') or '').strip().rstrip('/')
    if url:
        return url
    port = 5200
    try:
        from core.base.config import cfg as _core_cfg
        port = int(_core_cfg.get('settings', 'server.port', 5200))
    except Exception:  # noqa: BLE001
        pass
    return f'http://127.0.0.1:{port}'


def _headers(cfg: dict) -> dict:
    return {'Authorization': f'Bearer {str(cfg.get("compile_key") or "").strip()}',
            'Content-Type': 'application/json'}


def _result(status: str, **kw) -> dict:
    base = {'status': status, 'ok': status == 'success', 'new': False, 'error': '',
            'http_status': 0, 'elapsed_sec': None, 'active_matches': None,
            'returncode': None, 'log_tail': '', 'terminate': None, 'raw': ''}
    base.update(kw)
    return base


async def terminate(cfg: dict) -> dict:
    """取消当前编译。返回 {'ok', 'message'} (409「没有编译在进行」也算成功收尾)。"""
    url = base_url(cfg) + _TERMINATE_PATH
    timeout = aiohttp.ClientTimeout(total=_TERMINATE_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s, \
                s.post(url, json={}, headers=_headers(cfg)) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            ok = resp.status == 200 or resp.status == 409
            return {'ok': ok, 'status': resp.status,
                    'message': str(data.get('message') or data.get('error') or text[:200])}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'status': 0, 'message': f'{type(e).__name__}: {e}'}


async def request_compile(game: str, cfg: dict, is_new: bool = False) -> dict:
    """请求编译单个游戏目标并同步等待结果 (超时自动取消)。

    ``is_new=True`` (新游戏, 目标目录此前不存在) 时请求体附加 ``new: true``,
    API 走完整流程编译 (CMake 重新配置, 耗时更长); 老游戏更新不带 new 参数,
    走增量编译。
    """
    is_new = bool(is_new)
    if not cfg.get('compile_enabled', True):
        return _result('disabled', new=is_new, error='自动编译未启用')
    if not _TARGET_RE.match(game or ''):
        return _result('invalid', new=is_new,
                       error=f'目标名「{game}」不符合编译 API 命名规则 '
                             '(仅字母/数字/下划线/连字符, 首字符为字母或下划线)')

    wait = max(5, int(cfg.get('compile_timeout') or 180))
    url = base_url(cfg) + _COMPILE_PATH
    payload = {'target': game}
    if is_new:
        payload['new'] = True
    started = time.time()
    try:
        timeout = aiohttp.ClientTimeout(total=wait)
        async with aiohttp.ClientSession(timeout=timeout) as s, \
                s.post(url, json=payload, headers=_headers(cfg)) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            raw = text[:4000]
            if resp.status == 200 and data.get('success'):
                return _result('success', new=is_new, http_status=200,
                               elapsed_sec=data.get('elapsed_sec'),
                               active_matches=data.get('active_matches'), raw=raw)
            if resp.status == 504:
                # 服务端等待上限已到但进程仍在跑 — 与客户端超时同样处理: 取消
                term = await terminate(cfg)
                return _result('timeout', new=is_new, http_status=504, terminate=term,
                               error=str(data.get('error') or '编译超时未结束'), raw=raw)
            return _result('failed', new=is_new, http_status=resp.status,
                           error=str(data.get('error') or f'HTTP {resp.status}: {text[:200]}'),
                           returncode=data.get('returncode'),
                           elapsed_sec=data.get('elapsed_sec'),
                           log_tail=str(data.get('log_tail') or ''), raw=raw)
    except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
        term = await terminate(cfg)
        return _result('timeout', new=is_new, terminate=term,
                       error=f'等待 {wait} 秒无响应, 已发送取消编译请求',
                       elapsed_sec=round(time.time() - started, 1))
    except Exception as e:  # noqa: BLE001
        return _result('error', new=is_new, error=f'{type(e).__name__}: {e}')


# ==================== 展示辅助 ====================

STATUS_LABELS = {
    'success': '编译成功',
    'failed': '编译失败',
    'timeout': '编译超时',
    'error': '编译异常',
    'invalid': '目标名非法',
    'disabled': '未启用编译',
    'skipped': '未编译',
}


def describe(result: dict) -> str:
    """留档用的编译结果解析 (含 API 原始返回)。"""
    if not result:
        return '(无编译信息)'
    lines = [
        f'- 状态: {STATUS_LABELS.get(result.get("status"), result.get("status", ""))}',
        f'- 编译模式: {"新游戏完整编译 (new=true)" if result.get("new") else "增量编译"}',
        f'- HTTP: {result.get("http_status") or "-"}',
    ]
    if result.get('elapsed_sec') is not None:
        lines.append(f'- 用时: {result["elapsed_sec"]}s')
    if result.get('active_matches') is not None:
        lines.append(f'- 剩余进行中对局: {result["active_matches"]}')
    if result.get('returncode') is not None:
        lines.append(f'- 退出码: {result["returncode"]}')
    if result.get('error'):
        lines.append(f'- 错误: {result["error"]}')
    term = result.get('terminate')
    if term:
        lines.append(f'- 取消请求: {"成功" if term.get("ok") else "失败"} ({term.get("message", "")})')
    if result.get('log_tail'):
        lines.append('- 编译日志尾部:\n```\n' + result['log_tail'] + '\n```')
    if result.get('raw'):
        lines.append('- API 原始返回:\n```json\n' + result['raw'] + '\n```')
    return '\n'.join(lines)
