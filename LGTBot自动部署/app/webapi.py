"""Web 面板路由 /api/ext/lgtbotdeploy/* — 配置、审核记录、目录权限、数据文件浏览。

路由由框架 register_route 动态查表执行, 插件热重载即时生效, 卸载时框架自动
注销, 无需手动清理。默认 auth=True: 请求需带后台登录 token。
"""

from aiohttp import web

from core.base.logger import PLUGIN, get_logger
from core.plugin.web_pages import register_route

from . import central, config, deploy, review, store

log = get_logger(PLUGIN, 'LGTBot自动部署')

PREFIX = '/api/ext/lgtbotdeploy'


def register_routes():
    register_route('GET', PREFIX + '/config', _get_config)
    register_route('POST', PREFIX + '/config', _set_config)
    register_route('POST', PREFIX + '/test', _test_connection)
    register_route('GET', PREFIX + '/models', _get_models)
    register_route('GET', PREFIX + '/records', _get_records)
    register_route('GET', PREFIX + '/record', _get_record)
    register_route('POST', PREFIX + '/record/delete', _delete_record)
    register_route('POST', PREFIX + '/records/clear', _clear_records)
    register_route('GET', PREFIX + '/perms', _get_perms)
    register_route('POST', PREFIX + '/perms/save', _save_perm)
    register_route('POST', PREFIX + '/perms/delete', _delete_perm)
    register_route('GET', PREFIX + '/files', _get_files)
    register_route('GET', PREFIX + '/file', _get_file)
    register_route('POST', PREFIX + '/file/delete', _delete_file)
    log.info(f'LGTBot 自动部署面板路由已注册: {PREFIX}/*')


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
    """用一句最小请求验证中央 AI LLM 链路是否可用。

    面板会把**当前选中**的接口/模型一起提交 (可能还没保存), 这样点「测试连接」
    测的就是眼前选的那个, 而不是已保存的、更不是中央自动挑的。
    """
    body = await _json(request)
    result = await central.probe(str(body.get('provider_id') or ''),
                                 str(body.get('model') or ''))
    return web.json_response({'success': result.get('ok', False), **result})


async def _get_models(request: web.Request):
    """让中央 AI LLM 重新同步模型目录, 回传最新的接口/模型供面板选择。

    本插件不再直连上游, 也不再持有密钥 —— 接口与密钥统一由中央模块管理。
    """
    provider_id = (request.query.get('provider_id') or '').strip()
    try:
        result = await central.refresh_models(provider_id)
    except Exception as e:  # noqa: BLE001
        return web.json_response({'success': False, 'error': str(e)[:300], 'providers': []})
    return web.json_response({'success': True, **result})


# ==================== 审核记录 ====================

async def _get_records(request: web.Request):
    try:
        limit = min(max(int(request.query.get('limit', 200)), 1), 1000)
    except ValueError:
        limit = 200
    records = store.list_records(limit)
    # 「未通过」与「需人工处理」互斥, 与列表里的 verdictPill 同一口径:
    # 异常兜底的 reject (manual=True, 如审核服务不可用) 只算需人工, 不算未通过 ——
    # 那不是模型给出的明确判定, 混进去会让「未通过」虚高、两块统计还重复计数。
    def _manual(r: dict) -> bool:
        return bool(r.get('manual')) or r.get('stage') in ('error', 'download', 'extract', 'deploy')

    stats = {
        'total': len(records),
        'passed': sum(1 for r in records if r.get('stage') == 'deployed'),
        'forced': sum(1 for r in records if r.get('forced')),
        'rejected': sum(1 for r in records
                        if r.get('verdict') == 'reject' and not _manual(r)),
        'manual': sum(1 for r in records if _manual(r)),
    }
    return web.json_response({'success': True, 'records': records, 'stats': stats,
                              'labels': review.CATEGORY_LABELS,
                              'criteria_labels': review.criteria_label_map()})


async def _get_record(request: web.Request):
    rid = request.query.get('id', '')
    record = store.get_record(rid)
    if not record:
        return web.json_response({'success': False, 'error': '记录不存在'})
    return web.json_response({'success': True, 'record': record,
                              'review_text': store.get_review_text(rid)})


async def _delete_record(request: web.Request):
    """删除单条审核记录; ``keep_files=true`` 时只删索引、保留留档文件。"""
    body = await _json(request)
    result = store.delete_record(str(body.get('id') or ''),
                                 with_files=not body.get('keep_files'))
    if not result['ok']:
        return web.json_response({'success': False, 'error': result['error']})
    return web.json_response({'success': True, 'files': result['files']})


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
