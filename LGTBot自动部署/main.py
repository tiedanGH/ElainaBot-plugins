"""LGTBot自动部署 — 自动部署插件: 引用文件消息 → 下载 → 内容审核 → 上传到 lgtbot 目录。

用法 (指定群内所有人可用):
    /upload                    引用压缩包消息 → 解压到 <上传目录>/<压缩包名>/
    /upload <文件夹名>          引用单文件消息 → 写入 <上传目录>/<文件夹名>/<文件名>
    /upload force [文件夹名]    仅主人: 跳过内容审核直传 (force 可简写 f)
    /upload help               查看指令帮助
    /compile <文件夹名>         重新编译 (仅上次编译失败时可用, 无需重传文件)

内容查重 (app/flow.py): 下载后按 sha256 比对历史记录, 字节完全一致的包**直接拒收**
且不进审核 —— 内容一个字节没改, 重传不会得出不同结论。提示里给出上次的记录号与
那次的结果 (无论上次是否过审)。上次只是编译失败时, 提示引导到 /compile。

重新编译 (/compile): 因为查重会拦下重传, 「审核过了但编译失败」只能靠这条补救。
仅当该目录最近一条落地记录的编译**未成功**时可用, 编译已成功则拒绝 (否则会变成
无限重编按钮)。权限沿用目录绑定: 绑定用户或上次的上传者本人。每次重编另写一条
stage='recompile' 记录, 编译成功后该指令自然失效。

唯一上传目录 (lgtbot games 目录) 在面板配置; 同名目录 / 同名文件一律直接替换
(旧内容按配置备份到 data/backups)。

流程: 定位引用消息里的文件 → 先回一条确认消息 → 下载 (压缩包再安全解压到暂存
目录) → 把文件清单与文本内容送审 (仅文字, 不送图片) → 通过则落到上传目录, 未通过
或出错则不落地并标记人工处理 → 完成后在群里 @ 部署人员 (force 由主人发起, 不通知)。
模型的完整回复只留档到 data/reviews/, 群里只输出结论与未通过分类。

审查标准按模式裁剪 (详见 app/review.py): 压缩包查「原作出处标注 + 内容合规 + 版权」;
单文件不是完整游戏、通常不含 rule.md, 只查「内容合规 + 版权」。内容合规基于 QQ 青少年
保护方案从严判定 (明确违规与擦边疑似都不通过; 政治口径与 LGTBot游戏问答 的输出审核对齐 ——
现实与近现代政治人物一律违规、不因历史介绍或中立讨论放行, 古代历史人物作桌游题材放行), 未通过时输出违规分类与位置 (文件:行号),
绝不输出违规内容原文。版权仅依据文字内容判断 —— 图片/字体等二进制资源不送审, 来源未知
不构成拒绝理由。force 则完全跳过内容审核 —— 但压缩包成员名校验、体积限额、必需文件
清单、落地路径越界校验照做, 不因主人身份放宽。

送审文本**不做单文件截断**: 整包文本要么全文送审、要么整包拒收 —— 截断意味着截掉的
那段从未被审查, 违规内容完全可能藏在后半段。总量超过面板配置的「送审文本上限」时
直接报「送审文本超出上限」且不送审, 并列出最大的几个文本文件供上传者精简。该上限
本质是模型上下文窗口的约束。

压缩包完整性检查 (非 AI, force 同样拒绝): 必须包含面板配置的必需文件清单 (默认
achievements.h / board.h / icon.png / mygame.cc / option.cmake / options.h /
rule.md / unittest.cc), 可以多出其他文件, 缺少任一项即拒绝并汇报缺少哪些。

自动编译 (app/compile.py): 部署成功后按游戏名请求 LGTBot_ElainaBot 的编译 API,
超时 (默认 180s) 自动发送取消请求; 编译成功的常规更新只提示上传者已热更新, 并说明
规则/成就/游戏描述/倍率等属性的修改仍需重启 (完全不 @ 开发者); 编译失败/超时
@ 开发者复查。编译结果 (含 API 返回解析) 进审核记录与留档。

自动计划重启 (app/compile.py): 新游戏编译成功后自动请求 LGTBot 的计划重启 API
(``POST /api/ext/lgtbot/planned-restart``, 与编译 API 同一枚 token), 开启维护模式 +
自动模式, 维护原因填「新游戏《游戏中文名》」(取不到中文名则用目录名)。请求成功即
不再 @ 开发者 —— 剩余对局清空后由 LGTBot 自行重启, 本插件**只发起请求、不跟踪
重启是否发生**; 只有请求失败才展示原因并 @ 开发者手动安排。老游戏更新走热更新,
不请求重启也不 @ 开发者。请求结果进审核记录与留档。

人工送审提示词 (审核提示词.txt): 与自动审核**同一套标准**的独立提示词, 供人工把游戏包
贴给任意 AI 模型自行复核。三条标准正文摘自 app/review.py 的 _CRITERIA, 逐字一致;
只省略了 nonce / 定界符 / 行号前缀这些自动流程专用的防伪造机制。改动审核标准后
需同步更新该文件, 否则人工与自动会给出不同结论。

送审前对上传内容做提示词注入防御 (五层, 详见 app/review.py 模块 docstring): 信道中和 +
nonce 定界 + 隔离声明 + pass 强制 nonce 回显 + 确定性预扫描 (命中即不送审直接拒绝)。
审核结果附带 mygame.cc 的 k_properties 游戏名称/描述: 名称显示在通过与编译提示里
(如「情书(love_letter)」), 描述只进审核留档。

目录更新权限 (data/permissions.json): 新游戏上传成功 (审核通过, 编译成败不限)
自动把目录绑定给上传者, 此后仅绑定用户可更新该目录, 其他用户直接报权限不足且
不进审核; force 不受限也不触发绑定。面板「权限管理」页可增删改。

审核走框架 LLM 中央模块 (modules/ai_llm, 见 app/central.py): 接口地址、API Key、
模型目录、优先级与故障切换统一由该模块管理, 与 AI 开发插件 / AI 聊天陪伴共用同一套
配置。本插件**不保存任何密钥**, 只在配置里存 provider_id / model 两个选择, 留空即
交给中央自动选; 面板的接口与模型下拉全部来自中央的公开配置, 不可手输。中央模块未
安装 / 未启用 / 无可用接口时按「需人工处理」收尾且不触发重试 (重试救不了配置问题)。

配置与记录: 后台侧边栏「LGTBot 自动部署」页 (上传目录 / 审核模型 / 编译密钥 / 群聊 /
通知人员 / 审核记录 / 权限管理 / 数据文件浏览)。
"""

import os

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import config, flow, store, webapi

__plugin_meta__ = {
    'name': 'LGTBot 自动部署',
    'author': '铁蛋',
    'description': '/upload 引用群文件上传到 lgtbot 目录, 自动内容审核 + 请求编译 + 目录权限管理',
    'version': '1.9.4',
}

log = get_logger(PLUGIN, 'LGTBot自动部署')

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


# 重新编译: 内容完全相同的包会被查重拒收, 所以「审核过了但编译失败」只能靠这条补救。
# 上次编译已成功时 flow 会拒绝, 免得它变成无限重编按钮 (见 flow.handle_recompile)。
@handler(r'^/?compile(?:\s+([\s\S]+))?$', name='LGTBot重新编译',
         desc='/compile <文件夹名> — 重新编译 (仅上次编译失败时可用, 无需重传文件)',
         group_only=True, ignore_at_check=True, priority=5)
async def cmd_recompile(event, match):
    await flow.handle_recompile(event, (match.group(1) or '').strip())


@on_load
async def _on_load():
    store.init()
    config.all_config(refresh=True)
    webapi.register_routes()
    register_page(
        key=_PAGE_KEY,
        label='LGTBot 自动部署',
        source='plugin',
        source_name='LGTBot自动部署',
        icon=_ICON,
        html_file=_HTML_PATH,
    )
    log.info('LGTBot 自动部署插件已加载')


@on_unload
async def _on_unload():
    flow.cancel_all()
    unregister_page(_PAGE_KEY)
    log.info('LGTBot 自动部署插件已卸载')
