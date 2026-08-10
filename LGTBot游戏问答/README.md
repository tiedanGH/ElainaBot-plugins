# LGTBot 游戏问答

@机器人提问 LGTBot 桌游的规则、玩法、计分与结算，AI 现场检索 LGTBot 源码后带出处作答。

与 **AI 聊天陪伴**、**AI 开发助手** 完全独立：自己的配置、自己的上下文库、自己的限流，
共用的只有 `modules/ai_llm` 这一个模型底座。

---

## ⚠️ 部署前必读：必须做 bot 绑定

默认触发方式 `at` 注册的是 `.*` + `block=True` 的兜底 handler。而 LGTBot 把玩家的
**所有游戏输入**靠 `priority=-100` 的「LGTBot 消息派发」兜底送进 C++ 引擎
（`plugins/LGTBot_ElainaBot/mod/dispatcher.py`）。

`block` 在**匹配阶段**就 `break`（`core/plugin/_dispatch.py:269`），handler 函数体根本
来不及执行。所以两个插件跑在同一个 bot 上时，本插件会把游戏派发整条掐断，**所有对局失联**。

框架的 bot 白名单检查在 block 判定**之前**（`core/plugin/_dispatch.py:231`），因此：

> 到「插件管理 → bot 绑定」，把 **LGTBot游戏问答** 绑到问答 bot、
> **LGTBot_ElainaBot** 绑到游戏 bot。两者互不可见，block 就伤不到 LGTBot。

未绑定且检测到 LGTBot 也加载时，插件加载会打 WARNING，面板顶部显示红色横幅。
不想绑定就把触发方式改成 `prefix`（只有 `/问 xxx` 才命中并拦截）。

---

## 安装

1. 放进 `plugins/`，框架自动加载。
2. 启用 `modules/ai_llm`（AI LLM 服务），在其面板配好接口、Key 和模型。
   本插件**不保存任何密钥**。
3. 做上面的 bot 绑定。
4. 打开侧边栏「LGTBot 问答」页，确认「可检索范围」全部就绪。

源码目录默认自动定位同框架的 `plugins/LGTBot_ElainaBot`。`lgtbot/` 是 git submodule，
没拉取时面板会报红，服务器上执行：

```bash
git submodule update --init --recursive lgtbot
```

---

## 使用

| 指令 | 说明 |
|---|---|
| `@机器人 <问题>` | 直接提问（`at` / `both` 模式） |
| `/问 <问题>` | 前缀提问（`prefix` / `both` 模式） |
| `/问答` | 查看用法 |
| `/问答 清空` | 清空自己的追问上下文 |
| `/问答 游戏 [关键词]` | 查看收录的游戏 |

问法示例：

- `换位象棋怎么结算？`
- `十七步的番数怎么算`
- `情书有哪些成就，达成条件是什么`
- `狼人杀的选项倍率在哪里配`

---

## 它怎么工作

模型拿到五个**只读**工具，自己决定查几轮（默认最多 10 轮）：

| 工具 | 作用 |
|---|---|
| `list_games` | 列出全部游戏，中文名 ↔ 目录名对照 |
| `read_game_rule` | 读某游戏的 `rule.md`（作者写给玩家的权威规则） |
| `search_code` | 子串搜索，定位计分/结算/成就的实现位置 |
| `read_file` | 按行窗口读文件，返回带行号内容 |
| `list_dir` | 列目录，确认有哪些文件可读 |

系统提示词强制要求：**先检索再回答，不许凭记忆编造**；结算类问题必须读 `mygame.cc`
的真实实现，`rule.md` 与代码冲突时以代码为准并指出不一致；结论要附 `文件:行号` 出处。

中文名从 `mygame.cc` 的 `k_properties`（`.name_` / `.description_`）解析，逻辑与
`LGTBot_deploy/app/review.py` 保持一致（支持相邻字符串字面量拼接）。索引按
`mygame.cc` 的 mtime+size 缓存，玩家上传新游戏后自动失效重建。

---

## 安全边界

**所有**文件访问经 `app/sandbox.py` 收口，工具层不自己拼路径、不自己 `open()`。

| 约束 | 实现 |
|---|---|
| 只读 | sandbox 不提供任何写 / 删 / 移动 / 执行入口 |
| 根白名单 | 目标必须落在某个启用范围内，`realpath` 比对 —— 软链接指向仓库外会被解开后判越界 |
| 目录黑名单 | `.git` / `__pycache__` / `data` / `build` 等任何层级都不可见 |
| 后缀白名单 | 只读文本源码，`.so` / `.png` / `.db` 直接拒绝 |
| 体积行数上限 | 超限文件跳过，读取按行窗口截断 |
| 无正则搜索 | `search_code` 只做子串匹配，杜绝模型生成的病态正则 ReDoS |

LGTBot 自己的 `data/`（`config.yaml`、`lgtbot.db`、`user_cache.db`）**不在任何范围内**，
因此天然不可达。这是刻意设计 —— 不要把它加进「可检索范围」。

> 对比：`AI开发插件` 的 `read_file` 根目录是整个框架根，且 `_redact_config` 只作用于
> `get_config` 不作用于 `read_file`（`tools.py:357`）。那套工具只能给主人用，
> 绝不能开放给群友 —— 这正是本插件另起炉灶的原因。

### 提示词注入

游戏源码是玩家通过 `LGTBot_deploy` 上传的，`rule.md` / `mygame.cc` 里完全可能藏着
「忽略以上指令」。系统提示词明确声明：工具返回的一切与用户消息都是**不可信数据**，
不是指令。加上工具集全只读，最坏后果也只是读到别的游戏源码。

调用刻意走 `service.complete()` 而非 `run_agent()` —— 后者会打开中央运行时能力
（其他插件共享的工具、MCP、Skills）。本插件面向普通群友，工具面必须收敛到自己这五个。

---

## 限流

框架 `@handler(cooldown=...)` 只是被存进 handler 字典，`core/` 里没有任何地方读它
（`core/plugin/decorators.py:45`），所以限流自己实现，三道闸：

1. **并发闸** — 同一用户上一问还在跑就拒，主人也不豁免（防的是把自己卡死）
2. **冷却闸** — 距上次提问不足 `cooldown_seconds`
3. **日限闸** — 今日已达 `daily_limit`

计数在**模型调用成功之后**才记，被限流拒掉的请求不吃额度。用量落 SQLite，
插件热重载不会把每日上限清零。

---

## 文件结构

```
LGTBot游戏问答/
├── main.py              入口: meta + handler + 生命周期 + bot 绑定冲突检测
├── panel.html           Web 面板
└── app/
    ├── config.py        配置读写 (data/config.json, 原子替换)
    ├── sandbox.py       ⭐ 只读沙箱 —— 所有文件访问的唯一出入口
    ├── games.py         游戏索引: 目录名 ↔ 中文名
    ├── tools.py         交给模型的五个只读工具
    ├── central.py       中央 AI LLM 适配层
    ├── ratelimit.py     并发 / 冷却 / 每日上限
    ├── store.py         SQLite: 上下文 + 用量 + 统计
    └── webpanel.py      面板 API
```

配置与数据在 `data/`（已 gitignore）：`config.json`、`qa.db`。
