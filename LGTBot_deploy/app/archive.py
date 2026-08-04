"""压缩包安全解压 + 待审核内容采集。

解压前逐条校验成员名, 拦截 zip-slip (``../`` / 绝对路径 / 盘符) 与
tar 里的软链接、设备文件, 并对文件数、解压后总体积设硬上限 —— 上传者是普通
群成员, 压缩包必须当成不可信输入处理。

解压落到 data/staging/<记录号>/, 审核通过后才由 deploy.py 移入正式目录。
"""

from __future__ import annotations

import io
import os
import shutil
import tarfile
import zipfile

# 上送审核的文本类扩展名 (源码 / 说明 / 配置)
TEXT_EXTS = ('.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg',
             '.py', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.js', '.ts', '.html',
             '.css', '.sh', '.cmake', '.rst', '.csv', '.lua')
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')
FONT_EXTS = ('.ttf', '.otf', '.ttc', '.woff', '.woff2')

_MAX_TEXT_PER_FILE = 20000       # 单个文本文件上送的字符上限

# 游戏属性源文件: k_properties 里的 .name_ / .description_ 由 review.parse_game_props
# 解析。它**不受上面的上送上限约束**, 单独完整留存一份 —— k_properties 常写在
# mygame.cc 末尾 (社区游戏按 MakeMainStage → k_properties 的顺序收尾, 如 blokus
# 34884 字的文件里属性在 33695 处), 从截断到 20000 字的送审文本里根本读不到。
PROPS_FILE = 'mygame.cc'
_MAX_PROPS_READ = 1000000        # 属性源文件完整读取上限 (防超大文件占内存)


class ArchiveError(Exception):
    """压缩包非法 / 超限 (错误信息可直接回给群里)。"""


# ==================== 成员名校验 ====================

def _safe_member_name(name: str) -> str:
    """校验并归一压缩包内的相对路径; 非法时抛 ArchiveError。"""
    raw = (name or '').replace('\\', '/').strip()
    if not raw or raw in ('.', './'):
        return ''
    if raw.startswith('/') or (len(raw) > 1 and raw[1] == ':'):
        raise ArchiveError(f'压缩包含绝对路径成员: {name}')
    parts = []
    for part in raw.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            raise ArchiveError(f'压缩包含路径穿越成员: {name}')
        parts.append(part)
    return '/'.join(parts)


# ==================== 解压 ====================

def _extract_zip(data: bytes, dest: str, limits: dict) -> int:
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > limits['max_files']:
            raise ArchiveError(f'压缩包成员数 {len(infos)} 超过上限 {limits["max_files"]}')
        for info in infos:
            name = _safe_member_name(info.filename)
            if not name:
                continue
            target = os.path.join(dest, *name.split('/'))
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            total += info.file_size
            if total > limits['max_uncompressed']:
                raise ArchiveError(f'解压后体积超过上限 {limits["max_uncompressed"] / 1048576:.0f} MB')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, 65536)
    return total


def _extract_tar(data: bytes, dest: str, limits: dict) -> int:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as tf:
        members = tf.getmembers()
        if len(members) > limits['max_files']:
            raise ArchiveError(f'压缩包成员数 {len(members)} 超过上限 {limits["max_files"]}')
        for m in members:
            name = _safe_member_name(m.name)
            if not name:
                continue
            if m.issym() or m.islnk():
                raise ArchiveError(f'压缩包含链接成员 (不允许): {m.name}')
            if not (m.isfile() or m.isdir()):
                raise ArchiveError(f'压缩包含特殊文件成员 (不允许): {m.name}')
            target = os.path.join(dest, *name.split('/'))
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            total += m.size
            if total > limits['max_uncompressed']:
                raise ArchiveError(f'解压后体积超过上限 {limits["max_uncompressed"] / 1048576:.0f} MB')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, 65536)
    return total


def extract(data: bytes, filename: str, dest: str, limits: dict) -> dict:
    """解压到 dest (必须为空目录)。返回 {'total_size', 'root'}; 失败抛 ArchiveError。

    ``root`` 是压缩包唯一顶层目录名 (若压缩包直接把文件平铺在根则为空串),
    用于决定部署后的目录名。
    """
    low = (filename or '').lower()
    os.makedirs(dest, exist_ok=True)
    if low.endswith('.zip') or data[:2] == b'PK':
        total = _extract_zip(data, dest, limits)
    elif low.endswith(('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.tar')):
        total = _extract_tar(data, dest, limits)
    elif low.endswith(('.rar', '.7z')):
        raise ArchiveError('暂不支持 rar / 7z, 请改用 zip 或 tar.gz 重新打包')
    else:
        # 无扩展名兜底: 依次尝试 zip / tar
        try:
            total = _extract_zip(data, dest, limits)
        except zipfile.BadZipFile:
            try:
                total = _extract_tar(data, dest, limits)
            except tarfile.TarError as e:
                raise ArchiveError(f'无法识别的压缩格式: {e}') from e
    entries = [e for e in os.listdir(dest)]
    root = entries[0] if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])) else ''
    return {'total_size': total, 'root': root}


# ==================== 待审核内容采集 ====================

def _read_text(path: str, limit: int = _MAX_TEXT_PER_FILE) -> str:
    """读文本 (最多 ``limit`` + 1 个字符, 多读一个用于判断是否发生截断)。"""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return f.read(limit + 1)
    except OSError:
        return ''


def _is_rule_file(low_name: str) -> bool:
    return (low_name in ('rule.md', 'readme.md', 'rule.txt', 'readme')
            or low_name.startswith('rule.'))


def _text_priority(low_name: str) -> int:
    """文本文件领取 ``text_budget`` 的优先级 (数字越小越先拿)。

    预算是先到先得的, 按 ``os.walk`` 顺序发放会让大游戏翻车: ``mygame.cc`` 前面
    排着 achievements.h / board.h / card.h… 每个最多吃 20000 字, 预算耗尽后关键
    文件**整份不进 texts** —— 游戏名称/描述取不到 (本地兜底正则也没得解析),
    rule.md 更是连内容都没送审, 标准一只能瞎判。所以这两类必须先领预算。
    """
    if _is_rule_file(low_name):
        return 0    # 规则说明: 标准一「原作标注」的判断依据
    if low_name == 'mygame.cc':
        return 1    # 游戏属性: game_name / game_desc 的唯一来源
    return 2


def collect(root_dir: str, limits: dict) -> dict:
    """遍历解压结果, 产出送审素材 (仅文字: 图片/字体等二进制资源只进清单, 不读内容)。

    返回:
      ``tree``    [{path, size, kind}]      全部文件清单 (kind: text/image/font/binary)
      ``texts``   [{path, content, truncated}]  文本内容 (受 text_budget 预算约束)
      ``rule_files`` 命中 rule*.md / readme 的路径列表
      ``props_source`` mygame.cc 完整原文 (供本地解析游戏属性, 不受预算/截断约束)

    文本内容按 ``_text_priority`` 的优先级领取预算 (见那里的说明), 清单 ``tree``、
    ``rule_files`` 与 ``props_source`` 不受预算影响, 始终完整。
    """
    tree, rule_files, pending = [], [], []
    props_source = ''
    budget = max(0, int(limits.get('text_budget', 0)))
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in ('.git', '__pycache__', 'node_modules'))
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_dir).replace('\\', '/')
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            low = fn.lower()
            if low.endswith(IMAGE_EXTS):
                kind = 'image'
            elif low.endswith(FONT_EXTS):
                kind = 'font'
            elif low.endswith(TEXT_EXTS):
                kind = 'text'
            else:
                kind = 'binary'
            tree.append({'path': rel, 'size': size, 'kind': kind})
            if _is_rule_file(low):
                rule_files.append(rel)
            if low == PROPS_FILE and not props_source:
                props_source = _read_text(full, _MAX_PROPS_READ)
            if kind == 'text':
                # 排序键带上遍历序号, 同优先级内仍按原来的目录遍历顺序发放
                pending.append((_text_priority(low), len(pending), rel, full))

    texts = []
    for _, _, rel, full in sorted(pending):
        if budget <= 0:
            break
        content = _read_text(full)
        if not content:
            continue
        truncated = len(content) > _MAX_TEXT_PER_FILE
        content = content[:_MAX_TEXT_PER_FILE]
        if len(content) > budget:
            content, truncated = content[:budget], True
        budget -= len(content)
        texts.append({'path': rel, 'content': content, 'truncated': truncated})
    # rule.md 必须优先送审: 把它排到文本列表最前面
    texts.sort(key=lambda t: (0 if t['path'].lower().endswith('rule.md') else 1, t['path']))
    return {'tree': tree, 'texts': texts, 'rule_files': rule_files,
            'props_source': props_source}


def missing_required(tree: list, required: list) -> list:
    """压缩包完整性检查: 必需文件按**文件名**在任意层级匹配 (大小写不敏感)。

    返回缺少的文件名列表 (空 = 完整)。这是纯结构检查, 与 AI 审核无关,
    force 上传同样执行。
    """
    present = {os.path.basename(item.get('path', '')).lower() for item in tree}
    out = []
    for name in required or []:
        name = str(name or '').strip()
        if name and name.lower() not in present and name not in out:
            out.append(name)
    return out


def collect_single(data: bytes, filename: str, limits: dict) -> dict:
    """单文件上传的送审素材, 结构与 ``collect`` 一致 (只有一个成员, 仅文字送审)。"""
    low = (filename or '').lower()
    if low.endswith(IMAGE_EXTS):
        kind = 'image'
    elif low.endswith(FONT_EXTS):
        kind = 'font'
    elif low.endswith(TEXT_EXTS):
        kind = 'text'
    else:
        kind = 'binary'
    tree = [{'path': filename, 'size': len(data), 'kind': kind}]
    texts, raw = [], ''
    if kind == 'text':
        raw = data.decode('utf-8', errors='replace')
        if limits.get('text_budget', 0) > 0:
            budget = min(int(limits['text_budget']), _MAX_TEXT_PER_FILE)
            texts.append({'path': filename, 'content': raw[:budget],
                          'truncated': len(raw) > budget})
    rule_files = [filename] if low.endswith('rule.md') else []
    # 单独替换 mygame.cc 时同样要拿到完整原文 (送审那份可能已被截断)
    props_source = raw[:_MAX_PROPS_READ] if os.path.basename(low) == PROPS_FILE else ''
    return {'tree': tree, 'texts': texts, 'rule_files': rule_files,
            'props_source': props_source}
