"""插件配置读写 (data/config.yaml)。

面板与指令共用同一份配置, 全部字段都可在 Web 面板「服务器上传」页修改。
密钥留空时按 aidev 的优先级链回退: 本插件配置 > settings.yaml 的 ai.* > 环境变量,
方便与框架内 AI 开发插件共用同一个中转站。
"""

from __future__ import annotations

import os
import re
import threading

import yaml

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_APP_DIR)
DATA_DIR = os.path.join(_PLUGIN_DIR, 'data')
_CONFIG_FILE = os.path.join(DATA_DIR, 'config.yaml')

DEFAULTS = {
    # ---- 功能开关 ----
    'enabled': True,
    # ---- 权限: 指令所有人可用, 但仅这些群生效 (群 openid; 空 = 任何群都不生效) ----
    'allowed_groups': [],
    # ---- 完成后 @ 通知的部署人员 (用户 openid) ----
    'notify_users': [],
    # ---- 上传目标: 每一项就是一个子指令 /server <key> [文件夹]。
    #      面板里加一行即多一个子指令, 不需要改代码。 ----
    'targets': [
        {'key': 'lgtbot', 'aliases': ['lgt'], 'path': '', 'desc': '上传群文件至 lgtbot'},
    ],
    # ---- 部署 ----
    'keep_replaced_backup': True,  # 替换前把旧目录/旧文件备份到 data/backups
    'keep_archive': True,        # 是否把原压缩包留档到 data/archives
    # ---- 审核 ----
    'review_enabled': True,      # 关闭后不做内容审核, 直接部署 (仅用于上游故障应急)
    'review_images': 4,          # 随包上送审核的图片张数 (0 = 不上送图片, 需模型支持视觉)
    'review_prompt': '',         # 追加到内置审核标准之后的自定义要求
    # ---- 模型接口 (OpenAI 兼容) ----
    'base_url': 'https://api.ytea.top/v1',
    'api_key': '',
    'model': 'gpt-4.1-nano',
    'temperature': 0.2,
    'request_timeout': 180,
    # ---- 限额 ----
    'max_archive_mb': 50,        # 压缩包体积上限
    'max_uncompressed_mb': 200,  # 解压后总体积上限
    'max_files': 2000,           # 解压后文件数上限
    'text_budget': 120000,       # 上送审核的文本总字符预算
    'download_timeout': 60,      # 下载超时 (秒)
}

# 面板可写字段 (api_key 单独处理: 空串=不修改)
WRITABLE = tuple(DEFAULTS.keys())

_COMMENTS = {
    'enabled': '插件总开关',
    'allowed_groups': '允许执行 /server 指令的群 openid, 空列表 = 任何群都不生效',
    'notify_users': '每次执行完成后在群内 @ 的部署人员 openid',
    'targets': '上传目标: 每一项对应一个子指令 /server <key>; key=子指令名, aliases=别名, path=服务器绝对路径, desc=帮助里的说明',
    'keep_replaced_backup': '替换前是否把旧目录/旧文件备份到 data/backups',
    'keep_archive': '是否把原压缩包留档到 data/archives',
    'review_enabled': '是否启用内容审核 (关闭后直接部署)',
    'review_images': '随包上送审核的图片张数, 0 = 不上送',
    'review_prompt': '追加到内置审核标准之后的自定义要求',
    'base_url': 'OpenAI 兼容接口地址',
    'api_key': '接口密钥, 留空则回退 settings.yaml 的 ai.api_key / 环境变量',
    'model': '审核使用的模型',
    'temperature': '采样温度',
    'request_timeout': '单次审核请求超时 (秒)',
    'max_archive_mb': '压缩包体积上限 (MB)',
    'max_uncompressed_mb': '解压后总体积上限 (MB)',
    'max_files': '解压后文件数上限',
    'text_budget': '上送审核的文本总字符预算',
    'download_timeout': '下载超时 (秒)',
}

_lock = threading.Lock()
_cache: dict | None = None

_INT_FIELDS = ('review_images', 'request_timeout', 'max_archive_mb', 'max_uncompressed_mb',
               'max_files', 'text_budget', 'download_timeout')
_BOOL_FIELDS = ('enabled', 'keep_replaced_backup', 'keep_archive', 'review_enabled')
_LIST_FIELDS = ('allowed_groups', 'notify_users')

# 保留字: 不能当子指令名 (会挡住帮助与强制上传)
RESERVED_KEYS = ('help', '帮助', 'list', '列表', 'force', 'f', '强制')
_KEY_BAD = re.compile(r'[\s/\\:*?"<>|]')


def _norm_key(value) -> str:
    """子指令名归一: 去空白、转小写, 拒绝含路径分隔符等危险字符与保留字。"""
    key = str(value or '').strip().lower()
    if not key or _KEY_BAD.search(key) or key in RESERVED_KEYS or key.startswith('.'):
        return ''
    return key[:32]


def _norm_targets(value) -> list:
    """规整上传目标列表: [{key, aliases, path, desc}], key/别名全局去重。"""
    out, seen = [], set()
    if isinstance(value, dict):          # 兼容 {key: path} 写法
        value = [{'key': k, 'path': v} for k, v in value.items()]
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, str):
            item = {'key': item}
        if not isinstance(item, dict):
            continue
        key = _norm_key(item.get('key'))
        if not key or key in seen:
            continue
        raw = item.get('aliases')
        if isinstance(raw, str):
            raw = re.split(r'[\s,，/|]+', raw)
        aliases = []
        for a in raw or []:
            a = _norm_key(a)
            if a and a != key and a not in seen and a not in aliases:
                aliases.append(a)
        seen.add(key)
        seen.update(aliases)
        out.append({
            'key': key,
            'aliases': aliases,
            'path': str(item.get('path') or '').strip(),
            'desc': str(item.get('desc') or '').strip(),
        })
    return out


def _coerce(data: dict) -> dict:
    """按 DEFAULTS 的类型规整读入值, 非法值回退默认。"""
    out = dict(DEFAULTS)
    for k, v in (data or {}).items():
        if k not in DEFAULTS:
            continue
        if k in _BOOL_FIELDS:
            out[k] = bool(v)
        elif k in _INT_FIELDS:
            try:
                out[k] = max(0, int(v))
            except (TypeError, ValueError):
                pass
        elif k == 'temperature':
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
        elif k == 'targets':
            out[k] = _norm_targets(v)
        elif k in _LIST_FIELDS:
            if isinstance(v, str):
                v = [v]
            if isinstance(v, list):
                out[k] = [str(x).strip() for x in v if str(x).strip()]
        else:
            out[k] = '' if v is None else str(v).strip()
    return out


def _write(data: dict):
    """带注释落盘 (原子替换)。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    lines = ['# 服务器上传插件配置 — 可在 Web 面板「服务器上传」页可视化修改', '']
    for key, value in data.items():
        comment = _COMMENTS.get(key, '')
        if comment:
            lines.append(f'# {comment}')
        dumped = yaml.safe_dump({key: value}, allow_unicode=True,
                                default_flow_style=False, sort_keys=False).rstrip('\n')
        lines.append(dumped)
    tmp = _CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp, _CONFIG_FILE)


def all_config(refresh: bool = False) -> dict:
    """读取完整配置 (缺项自动补默认并落盘)。"""
    global _cache
    with _lock:
        if _cache is not None and not refresh:
            return dict(_cache)
        raw = {}
        if os.path.isfile(_CONFIG_FILE):
            try:
                with open(_CONFIG_FILE, encoding='utf-8') as f:
                    loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    raw = loaded
            except Exception:  # noqa: BLE001 — 配置损坏时按默认值继续运行
                raw = {}
        data = _coerce(raw)
        if set(raw) != set(DEFAULTS):
            try:
                _write(data)
            except Exception:  # noqa: BLE001
                pass
        _cache = data
        return dict(data)


def update(updates: dict) -> dict:
    """合并写入面板提交的字段; api_key 传空串表示不修改, 传 null 表示清除。"""
    global _cache
    cur = all_config()
    with _lock:
        for k, v in (updates or {}).items():
            if k not in WRITABLE:
                continue
            if k == 'api_key':
                if v is None:
                    cur[k] = ''
                elif isinstance(v, str) and v.strip():
                    cur[k] = v.strip()
                continue
            cur[k] = v
        data = _coerce(cur)
        _write(data)
        _cache = data
        return dict(data)


def api_key() -> str:
    """密钥解析链: 本插件配置 > settings.yaml 的 ai.api_key > 环境变量。"""
    key = all_config().get('api_key') or ''
    if not key:
        try:
            from core.base.config import cfg
            key = cfg.get('settings', 'ai.api_key', '') or ''
        except Exception:  # noqa: BLE001
            key = ''
    if not key:
        key = os.environ.get('AI_DEV_API_KEY') or os.environ.get('OPENAI_API_KEY') or ''
    return str(key)


def base_url() -> str:
    url = all_config().get('base_url') or DEFAULTS['base_url']
    if not url:
        try:
            from core.base.config import cfg
            url = cfg.get('settings', 'ai.base_url', '') or DEFAULTS['base_url']
        except Exception:  # noqa: BLE001
            url = DEFAULTS['base_url']
    return str(url).rstrip('/')


def is_group_allowed(group_id: str) -> bool:
    return bool(group_id) and group_id in all_config().get('allowed_groups', [])


def targets() -> list:
    """全部上传目标 (即全部可用子指令)。"""
    return all_config().get('targets', [])


def find_target(name: str) -> dict | None:
    """按子指令名或别名查目标 (大小写不敏感); 不存在返回 None。"""
    key = str(name or '').strip().lower()
    if not key:
        return None
    for t in targets():
        if key == t['key'] or key in t['aliases']:
            return t
    return None


def target_names() -> str:
    """帮助/报错里用的可用子指令列表文案。"""
    return '、'.join(t['key'] for t in targets()) or '（未配置）'


def public_config() -> dict:
    """面板展示用配置 (不含密钥明文)。"""
    data = all_config()
    data.pop('api_key', None)
    data['api_key_set'] = bool(api_key())
    data['api_key_source'] = ('plugin' if all_config().get('api_key')
                              else ('inherit' if api_key() else 'none'))
    data['data_dir'] = DATA_DIR
    return data
