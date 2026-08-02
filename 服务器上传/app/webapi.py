"""Web 面板路由 /api/ext/svrupload/* — 配置、审核记录、目录权限、数据文件浏览。

路由由框架 register_route 动态查表执行, 插件热重载即时生效, 卸载时框架自动
注销, 无需手动清理。默认 auth=True: 请求需带后台登录 token。
"""

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from . import config, deploy, review, store

log = get_logger(PLUGIN, '服务器上传')

PREFIX = '/api/ext/svrupload'


def register_routes():
    register_route('GET', PREFIX + '/config', _get_config)
    register_route('POST', PREFIX + '/config', _set_config)
    register_route('POST', PREFIX + '/test', _test_connection)
    register_route('GET', PREFIX + '/models', _get_models)
    register_route('GET', PREFIX + '/records', _get_records)
    register_route('GET', PREFIX + '/record', _get_record)
    register_route('POST', PREFIX + '/records/clear', _clear_records)
    register_route('GET', PREFIX + '/perms', _get_perms)
    register_route('POST', PREFIX + '/perms/save', _save_perm)
    register_route('POST', PREFIX + '/perms/delete', _delete_perm)
    register_route('GET', PREFIX + '/files', _get_files)
    register_route('GET', PREFIX + '/file', _get_file)
    register_route('POST', PREFIX + '/file/delete', _delete_file)
    log.info(f'服务器上传面板路由已注册: {PREFIX}/*')


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ==================== 配置 ====================

async def _get_config(request: web.Request):
    return web.json_response({'success': True, 'config': config.public_config()})


async def _set_config(request: web.Request):
    body = await _json(request)
    updates = {k: v for k, v in body.items() if k in config.WRITABLE}
    if not updates:
        return web.json_response({'success': False, 'error': '没有可保存的字段'})
    config.update(updates)
    return web.json_response({'success': True, 'config': config.public_config()})


async def _test_connection(request: web.Request):
    """用一句最小请求验证密钥 / 地址 / 模型是否可用。"""
    result = await review.probe()
    return web.json_response({'success': result.get('ok', False), **result})


async def _get_models(request: web.Request):
    """拉取上游模型列表 (可用 query 传临时 base_url/api_key 试连)。"""
    import aiohttp

    base = (request.query.get('base_url') or '').strip().rstrip('/') or config.base_url()
    key = (request.query.get('api_key') or '').strip() or config.api_key()
    if not key:
        return web.json_response({'success': False, 'error': '未配置密钥', 'models': []})
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s, \
                s.get(base + '/models', headers={'Authorization': f'Bearer {key}'}) as r:
            data = await r.json()
        if not isinstance(data, dict) or 'data' not in data:
            return web.json_response({'success': False, 'error': f'上游返回异常: {str(data)[:200]}',
                                      'models': []})
        models = sorted(m.get('id', '') for m in data.get('data', []) if m.get('id'))
        return web.json_response({'success': True, 'models': models})
    except Exception as e:  # noqa: BLE001
        return web.json_response({'success': False, 'error': str(e), 'models': []})


# ==================== 审核记录 ====================

async def _get_records(request: web.Request):
    try:
        limit = min(max(int(request.query.get('limit', 200)), 1), 1000)
    except ValueError:
        limit = 200
    records = store.list_records(limit)
    stats = {
        'total': len(records),
        'passed': sum(1 for r in records if r.get('stage') == 'deployed'),
        'forced': sum(1 for r in records if r.get('forced')),
        'rejected': sum(1 for r in records if r.get('verdict') == 'reject'),
        'manual': sum(1 for r in records if r.get('manual') or r.get('stage') in ('error', 'download', 'extract', 'deploy')),
    }
    return web.json_response({'success': True, 'records': records, 'stats': stats,
                              'labels': review.CATEGORY_LABELS})


async def _get_record(request: web.Request):
    rid = request.query.get('id', '')
    record = store.get_record(rid)
    if not record:
        return web.json_response({'success': False, 'error': '记录不存在'})
    return web.json_response({'success': True, 'record': record,
                              'review_text': store.get_review_text(rid)})


async def _clear_records(request: web.Request):
    n = store.clear_records()
    return web.json_response({'success': True, 'cleared': n})


# ==================== 目录更新权限 ====================

async def _get_perms(request: web.Request):
    return web.json_response({'success': True, 'perms': store.perm_list()})


async def _save_perm(request: web.Request):
    """新增/修改绑定: {folder, user_id, username}。folder 名按部署同一套规则校验。"""
    body = await _json(request)
    folder = str(body.get('folder') or '').strip()
    user_id = str(body.get('user_id') or '').strip()
    err = deploy.bad_name(folder) if folder else '目录名为空'
    if err:
        return web.json_response({'success': False, 'error': f'目录{err}'})
    if not user_id:
        return web.json_response({'success': False, 'error': '用户 openid 为空'})
    store.perm_set(folder, user_id, str(body.get('username') or ''))
    return web.json_response({'success': True, 'perms': store.perm_list()})


async def _delete_perm(request: web.Request):
    body = await _json(request)
    ok = store.perm_delete(str(body.get('folder') or '').strip())
    if not ok:
        return web.json_response({'success': False, 'error': '该目录没有绑定记录'})
    return web.json_response({'success': True, 'perms': store.perm_list()})


# ==================== 数据文件 ====================

async def _get_files(request: web.Request):
    """文件列表 (备份聚合为整夹条目) + 各模块目录大小统计。"""
    data = store.list_entries()
    return web.json_response({'success': True, 'files': data['entries'],
                              'stats': data['stats'], 'data_dir': store.DATA_DIR})


async def _get_file(request: web.Request):
    content, err = store.read_file(request.query.get('path', ''))
    if err:
        return web.json_response({'success': False, 'error': err})
    return web.json_response({'success': True, 'content': content})


async def _delete_file(request: web.Request):
    """删除单个留档文件, 或 backups/ 下的整个备份文件夹。"""
    body = await _json(request)
    err = store.delete_entry(str(body.get('path', '')))
    if err:
        return web.json_response({'success': False, 'error': err})
    return web.json_response({'success': True})
