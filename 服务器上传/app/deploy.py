"""审核通过后把文件落到上传目标目录。

两种落地方式, 对应两种子指令用法:
  · 压缩包  ``/server <target>``            → 解压到 ``<target.path>/<压缩包名>/``
  · 单文件  ``/server <target> <文件夹名>``  → 写入 ``<target.path>/<文件夹名>/<文件名>``

同名目录 / 同名文件一律**直接替换** (不需要 force 参数); 替换前旧内容按配置
备份到 ``data/backups/<target.key>/``。目标路径先经名称合法性校验, 落地前再用
realpath 确认没有跳出 ``target.path``, 防止靠构造压缩包名 / 文件夹名穿越目录。
"""

from __future__ import annotations

import os
import shutil
import time

from . import store

_ARCHIVE_SUFFIXES = ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz',
                     '.tar', '.zip', '.rar', '.7z')


def strip_archive_ext(name: str) -> str:
    """去掉压缩包扩展名 (含 .tar.gz 这类复合扩展)。"""
    base = (name or '').strip()
    low = base.lower()
    for ext in _ARCHIVE_SUFFIXES:
        if low.endswith(ext):
            return base[: -len(ext)]
    return base


def bad_name(name: str) -> str:
    """校验用户可控的目录名 / 压缩包名; 返回错误信息, 空串表示合法。"""
    if not name:
        return '名称为空'
    if '/' in name or '\\' in name or '..' in name:
        return f'名称不合法: {name}'
    if name.startswith('.') or name in ('.', '..'):
        return f'名称不合法: {name}'
    if any(c in name for c in ':*?"<>|'):
        return f'名称含非法字符: {name}'
    return ''


def check_target(target: dict) -> str:
    """校验目标路径配置; 返回错误信息, 空串表示可用。"""
    path = (target or {}).get('path') or ''
    if not path:
        return f'目标「{(target or {}).get("key", "")}」未配置服务器路径, 请在后台面板填写'
    if not os.path.isabs(path):
        return f'目标路径必须是绝对路径: {path}'
    if not os.path.isdir(path):
        return f'目标路径不存在或不是目录: {path}'
    return ''


def _inside(base_real: str, path_real: str) -> bool:
    return path_real == base_real or path_real.startswith(base_real + os.sep)


def _backup_dir(target: dict) -> str:
    return os.path.join(store.BACKUPS_DIR, target.get('key') or 'unknown')


def _stash(src: str, target: dict, cfg: dict, rel: str) -> str:
    """备份 (或直接删除) 将被替换的旧目录 / 旧文件, 返回备份路径 (未备份时为空串)。"""
    if not cfg.get('keep_replaced_backup', True):
        if os.path.isdir(src):
            shutil.rmtree(src, ignore_errors=True)
        else:
            os.remove(src)
        return ''
    dest = os.path.join(_backup_dir(target), f'{rel}.{time.strftime("%Y%m%d-%H%M%S")}')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(src, dest)
    return dest


def deploy_archive(staging: str, root: str, base_name: str, target: dict, cfg: dict) -> dict:
    """解压结果 → ``<target.path>/<base_name>/``。

    ``root`` 是压缩包内唯一顶层目录名: 当它与 ``base_name`` 同名时把它的内容提上来
    (避免出现 ``五子棋/五子棋/...`` 的重复层级), 其余情况保持压缩包原结构。

    返回 ``{ok, dest, name, backup, note, error}``。
    """
    err = check_target(target) or bad_name(base_name)
    if err:
        return {'ok': False, 'error': err, 'dest': '', 'name': base_name, 'backup': '', 'note': ''}

    base_real = os.path.realpath(target['path'])
    dest = os.path.realpath(os.path.join(base_real, base_name))
    if os.path.dirname(dest) != base_real:
        return {'ok': False, 'error': f'目标路径越界: {base_name}', 'dest': '',
                'name': base_name, 'backup': '', 'note': ''}

    backup = ''
    if os.path.exists(dest):
        try:
            backup = _stash(dest, target, cfg, base_name)
        except OSError as e:
            return {'ok': False, 'error': f'替换旧目录失败: {e}', 'dest': dest,
                    'name': base_name, 'backup': '', 'note': ''}

    note = ''
    src = staging
    if root and root == base_name:
        src = os.path.join(staging, root)
        note = '（已提升压缩包内同名文件夹）'
    try:
        shutil.move(src, dest)
    except OSError as e:
        try:
            shutil.copytree(src, dest)
        except OSError as e2:
            return {'ok': False, 'error': f'部署失败: {e} / {e2}', 'dest': dest,
                    'name': base_name, 'backup': backup, 'note': ''}
    return {'ok': True, 'error': '', 'dest': dest, 'name': base_name,
            'backup': backup, 'note': note}


def deploy_single(data: bytes, filename: str, folder: str, target: dict, cfg: dict) -> dict:
    """单文件 → ``<target.path>/<folder>/<filename>``。

    目标文件夹**必须已存在** (避免打错字凭空建目录); 同名文件直接替换。
    返回 ``{ok, dest, name, backup, note, error}``。
    """
    err = check_target(target) or bad_name(folder) or bad_name(filename)
    if err:
        return {'ok': False, 'error': err, 'dest': '', 'name': folder, 'backup': '', 'note': ''}

    base_real = os.path.realpath(target['path'])
    folder_real = os.path.realpath(os.path.join(base_real, folder))
    if os.path.dirname(folder_real) != base_real:
        return {'ok': False, 'error': f'目标路径越界: {folder}', 'dest': '',
                'name': folder, 'backup': '', 'note': ''}
    if not os.path.isdir(folder_real):
        return {'ok': False, 'error': f'目标下不存在文件夹「{folder}」, 请检查文件夹名称',
                'dest': '', 'name': folder, 'backup': '', 'note': ''}

    dest = os.path.realpath(os.path.join(folder_real, filename))
    if not _inside(folder_real, dest) or os.path.dirname(dest) != folder_real:
        return {'ok': False, 'error': f'目标路径越界: {filename}', 'dest': '',
                'name': folder, 'backup': '', 'note': ''}

    backup, note = '', ''
    if os.path.exists(dest):
        if os.path.isdir(dest):
            return {'ok': False, 'error': f'目标下已存在同名文件夹「{filename}」', 'dest': dest,
                    'name': folder, 'backup': '', 'note': ''}
        try:
            backup = _stash(dest, target, cfg, os.path.join(folder, filename))
        except OSError as e:
            return {'ok': False, 'error': f'替换旧文件失败: {e}', 'dest': dest,
                    'name': folder, 'backup': '', 'note': ''}
        note = '（已替换同名文件）'
    try:
        with open(dest, 'wb') as f:
            f.write(data)
    except OSError as e:
        return {'ok': False, 'error': f'写入失败: {e}', 'dest': dest,
                'name': folder, 'backup': backup, 'note': ''}
    return {'ok': True, 'error': '', 'dest': dest, 'name': folder,
            'backup': backup, 'note': note}
