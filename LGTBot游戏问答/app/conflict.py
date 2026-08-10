"""同 bot 部署冲突检测。

单独成模块的原因：框架把 main.py 直接当成包 ``plugins.<插件名>`` 本身载入
（core/plugin/_loader.py:258），并不存在 ``plugins.<插件名>.main`` 子模块，
所以 app/ 下的模块**无法** ``from ..main import ...``。main.py 与 webpanel.py
都要用这个检测，就放在这里让双方各自 import。

检测的是什么：本插件在 at / both 模式下注册 ``.*`` + ``block=True`` 的兜底
handler，而 LGTBot 靠 priority=-100 的兜底把玩家的游戏输入送进 C++ 引擎。
block 在匹配阶段就 break（core/plugin/_dispatch.py:269），两者同 bot 必然
互斥。框架的 bot 白名单在 block 判定之前生效（同文件 :231），所以绑到不同
bot 就能共存 —— 这里就是检查有没有绑。
"""
from __future__ import annotations

import os

PLUGIN_DIR_NAME = os.path.basename(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
LGTBOT_PLUGIN = 'LGTBot_ElainaBot'

WARNING = (
    '⚠️ 检测到 LGTBot_ElainaBot 与本插件可能跑在同一个 bot 上。当前触发方式会注册 '
    'block=True 的兜底 handler，将掐断 LGTBot 的游戏消息派发（priority=-100），'
    '导致所有对局失联。请到「插件管理 → bot 绑定」把两个插件绑到不同 bot，'
    '或把本插件的触发方式改成「前缀触发」。'
)


def binding_warning(current: dict) -> str:
    """有冲突风险返回警告文案，否则返回空串。"""
    if str(current.get('trigger_mode') or 'at') == 'prefix':
        return ''          # 前缀模式只在命中前缀时拦截，不会碰到游戏输入
    try:
        from core.application import get_app

        manager = getattr(get_app(), 'plugin_manager', None)
        if manager is None or not hasattr(manager, 'get_plugin_bots'):
            return ''
        loaded = {str(item.get('name') or '') for item in manager.get_plugin_list()}
        if LGTBOT_PLUGIN not in loaded:
            return ''      # 没跟 LGTBot 同框架，随便 block
        bindings = manager.get_plugin_bots()
        mine = set(bindings.get(PLUGIN_DIR_NAME) or [])
        theirs = set(bindings.get(LGTBOT_PLUGIN) or [])
        if mine and theirs and not (mine & theirs):
            return ''      # 两边都绑了且不重叠 —— 正确部署
    except Exception:
        return ''          # 拿不到管理器时不误报
    return WARNING
