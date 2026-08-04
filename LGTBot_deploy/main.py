"""LGTBot_deploy — LGTBot 自动部署插件: 引用文件消息 → 下载 → 内容审核 → 上传到 lgtbot 目录。

用法 (指定群内所有人可用):
    /upload                    引用压缩包消息 → 解压到 <上传目录>/<压缩包名>/
    /upload <文件夹名>          引用单文件消息 → 写入 <上传目录>/<文件夹名>/<文件名>
    /upload force [文件夹名]    仅主人: 跳过内容审核直传 (force 可简写 f)
    /upload help               查看指令帮助

唯一上传目录 (lgtbot games 目录) 在面板配置; 同名目录 / 同名文件一律直接替换
(旧内容按配置备份到 data/backups)。

流程: 定位引用消息里的文件 → 先回一条确认消息 → 下载 (压缩包再安全解压到暂存
目录) → 把文件清单与文本内容送审 (仅文字, 不送图片) → 通过则落到上传目录, 未通过
或出错则不落地并标记人工处理 → 完成后在群里 @ 部署人员 (force 由主人发起, 不通知)。
模型的完整回复只留档到 data/reviews/, 群里只输出结论与未通过分类。

审查标准按模式裁剪 (详见 app/review.py): 压缩包查「原作出处标注 + 内容合规 + 版权」;
单文件不是完整游戏、通常不含 rule.md, 只查「内容合规 + 版权」。内容合规基于 QQ 青少年
保护方案从严判定 (明确违规与擦边疑似都不通过), 未通过时输出违规分类与位置 (文件:行号),
绝不输出违规内容原文。版权仅依据文字内容判断 —— 图片/字体等二进制资源不送审, 来源未知
不构成拒绝理由。force 则完全跳过内容审核 —— 但压缩包成员名校验、体积限额、必需文件
清单、落地路径越界校验照做, 不因主人身份放宽。

压缩包完整性检查 (非 AI, force 同样拒绝): 必须包含面板配置的必需文件清单 (默认
achievements.h / board.h / icon.png / mygame.cc / option.cmake / options.h /
rule.md / unittest.cc), 可以多出其他文件, 缺少任一项即拒绝并汇报缺少哪些。

自动编译 (app/compile.py): 部署成功后按游戏名请求 LGTBot_ElainaBot 的编译 API,
超时 (默认 180s) 自动发送取消请求; 编译成功的常规更新只提示上传者已热更新
(完全不 @ 开发者), 新游戏则 @ 上传者等待重启 + @ 开发者安排重启并显示剩余
对局数; 编译失败/超时 @ 开发者复查。编译结果 (含 API 返回解析) 进审核记录与留档。

送审前对上传内容做提示词注入防御 (五层, 详见 app/review.py 模块 docstring): 信道中和 +
nonce 定界 + 隔离声明 + pass 强制 nonce 回显 + 确定性预扫描 (命中即不送审直接拒绝)。
审核结果附带 mygame.cc 的 k_properties 游戏名称/描述: 名称显示在通过与编译提示里
(如「情书(love_letter)」), 描述只进审核留档。

目录更新权限 (data/permissions.json): 新游戏上传成功 (审核通过, 编译成败不限)
自动把目录绑定给上传者, 此后仅绑定用户可更新该目录, 其他用户直接报权限不足且
不进审核; force 不受限也不触发绑定。面板「权限管理」页可增删改。

配置与记录: 后台侧边栏「LGTBot 自动部署」页 (上传目录 / 审核密钥 / 编译密钥 / 群聊 /
通知人员 / 审核记录 / 权限管理 / 数据文件浏览)。
"""

import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import config, flow, store, webapi

__plugin_meta__ = {
    'name': 'LGTBot 自动部署',
    'author': 'ElainaBot',
    'description': '/upload 引用群文件上传到 lgtbot 目录, 自动内容审核 + 请求编译 + 目录权限管理',
    'version': '1.4.1',
}

log = get_logger(PLUGIN, 'LGTBot_deploy')

_PAGE_KEY = 'lgtbot-deploy'
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel.html')

_ICON = (
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
    '<path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/></svg>'
)


# 强制上传: 优先级高于普通指令并 block, 命中即拦截, 不会再落到下面的 cmd_upload。
# owner_only 由框架把关 —— 非主人命中时框架直接回「仅主人」模板并终止匹配链
# (见 core/plugin/_dispatch.py), 所以普通处理器不会把 force 误当成文件夹名。
@handler(r'^/?upload\s+(?i:force|f)(?:\s+([\s\S]+))?$', name='LGTBot强制部署',
         desc='/upload force [文件夹名] — 跳过内容审核直传 lgtbot (仅主人)',
         owner_only=True, group_only=True, ignore_at_check=True, priority=10, block=True)
async def cmd_upload_force(event, match):
    await flow.handle(event, (match.group(1) or '').strip(), force=True)


@handler(r'^/?upload(?:\s+([\s\S]+))?$', name='LGTBot部署',
         desc='/upload [文件夹名] — 引用群文件上传到 lgtbot (审核通过后落地)',
         group_only=True, ignore_at_check=True, priority=5)
async def cmd_upload(event, match):
    await flow.handle(event, (match.group(1) or '').strip())


@on_load
async def _on_load():
    store.init()
    config.all_config(refresh=True)
    webapi.register_routes()
    register_page(
        key=_PAGE_KEY,
        label='LGTBot 自动部署',
        source='plugin',
        source_name='LGTBot_deploy',
        icon=_ICON,
        html_file=_HTML_PATH,
    )
    log.info('LGTBot 自动部署插件已加载')


@on_unload
async def _on_unload():
    flow.cancel_all()
    unregister_page(_PAGE_KEY)
    log.info('LGTBot 自动部署插件已卸载')
