"""服务器上传插件 — 引用文件消息 → 下载 → 内容审核 → 上传到服务器目录。

用法 (指定群内所有人可用):
    /server help                 查看帮助与全部可用目标
    /server <目标>               引用压缩包消息 → 解压到 <目标路径>/<压缩包名>/
    /server <目标> <文件夹名>     引用单文件消息 → 写入 <目标路径>/<文件夹名>/<文件名>

「目标」是面板里配置的一行 (key + 别名 + 服务器路径), **面板加一行就多一个子指令**,
不需要改代码; 同名目录 / 同名文件一律直接替换 (旧内容按配置备份到 data/backups)。

流程: 解析目标 → 定位引用消息里的文件 → 先回一条确认消息 → 下载 (压缩包再安全
解压到暂存目录) → 把文件清单 / 文本 / 图片送审 → 通过则落到目标目录, 未通过或出错
则不落地并标记人工处理 → 无论结果如何都在群里 @ 部署人员。模型的完整回复只留档到
data/reviews/, 群里只输出结论与未通过分类。

审查标准按模式裁剪 (详见 app/review.py): 压缩包查「原作出处标注 + 内容合规 + 版权」;
单文件不是完整游戏、通常不含 rule.md, 只查「内容合规 + 版权」。

配置与记录: 后台侧边栏「服务器上传」页 (上传目标 / 密钥 / 群聊 / 通知人员 /
审核记录 / 数据文件浏览)。
"""

import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import config, flow, store, webapi

__plugin_meta__ = {
    'name': '服务器上传',
    'author': 'ElainaBot',
    'description': '引用群文件消息上传到服务器目录, 自动内容审核后落地 (子指令可在面板动态配置)',
    'version': '1.0.1',
}

log = get_logger(PLUGIN, '服务器上传')

_PAGE_KEY = 'server-upload'
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel.html')

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    '<path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>'
)


# 子指令来自配置, 所以正则只认 /server 前缀, 参数交给 flow 动态解析 ——
# 装饰器在 import 期求值, 把子指令写进正则就没法「面板加一行即多一个子指令」了。
@handler(r'^/?server(?:\s+([\s\S]+))?$', name='服务器上传',
         desc='/server <目标> [文件夹名] — 引用群文件上传到服务器 (审核通过后落地)',
         group_only=True, ignore_at_check=True, priority=5)
async def cmd_server(event, match):
    await flow.handle(event, (match.group(1) or '').strip())


@on_load
async def _on_load():
    store.init()
    config.all_config(refresh=True)
    webapi.register_routes()
    register_page(
        key=_PAGE_KEY,
        label='服务器上传',
        source='plugin',
        source_name='server_upload',
        icon=_ICON,
        html_file=_HTML_PATH,
    )
    log.info('服务器上传插件已加载')


@on_unload
async def _on_unload():
    flow.cancel_all()
    unregister_page(_PAGE_KEY)
    log.info('服务器上传插件已卸载')
