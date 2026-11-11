# 本地补丁清单（lark-card-for-hermes v1.7.7 + ops layer）

上游：`ywarmy/lark-card-for-hermes`（fork 自 `Keithin/lark-card-for-hermes`）。
以下补丁不改变插件对外行为契约，只补 HFC 有、lark-card 缺的运维层与两处投递修正。

| # | 模块 | 类型 | 说明 |
|---|---|---|---|
| 1 | `hfc/bridge.py` + `__init__.py` | 修复 | 回合外通知卡继承上一回合 `duration/tokens` → 用 `notice_lifecycle` 区分「本回合终稿 / 独立通知」 |
| 2 | `hfc/metrics.py` | 新增 | 有界计数器 + 错误码（HTTP 400/429 等），线程安全，不存正文 |
| 3 | `hfc/observed_rate.py` | 新增 | 实际编辑速率与 1s 峰值，用于判断是否撞限流 |
| 4 | `hfc/config_snapshot.py` | 新增 | 运行态配置 vs 磁盘配置差异（回答「改了要不要重启」） |
| 5 | `hfc/interaction_receipts.py` | 新增 | 按钮点击幂等闸门（clarify / 命令中心复投） |
| 6 | `hfc/notice_lifecycle.py` | 新增 | 通知分类：producer > 角标前缀 > 正文同源 |
| 7 | `hfc/maintenance.py` | 新增 | 维护态：跳过流式编辑（终稿照发），含 drain |
| 8 | `hfc/diagnostics.py` | 新增 | 支持报告（脱敏 JSON）+ `/card-status` 文本渲染 |
| 9 | `__init__.py` | 命令 | 新增 `/card-status`、`/card-doctor`、`/card-maintenance` |
| 10 | `tests/check_ops.py` | 判据 | 7 组断言覆盖上述模块（run_all 自动发现，共 14 项） |

## 私有依赖硬化（1.7.9）

上游把三个「Hermes/lark-oapi 内部实现」当作外部契约；这里全部改成「先试上游，失败用本地实现」，
降级时写日志并由 `/card-status` 展示（`私有 import 降级：…`）：

| 私有面 | 原来的失效形态 | 现在的兜底 |
|---|---|---|
| `agent.display.build_tool_preview` | 时间线整片没有参数预览 | `hfc/preview.py` 本地摘要（含敏感键打码） |
| `hermes_cli.plugin_choices.ask_choice` | 菜单退化成用法文本 | 记录降级并保留「带参数直接切换」路径 |
| `hermes_cli.commands.COMMAND_REGISTRY` | 命令中心整卡空白 | AST 直读 `commands.py` 的 `CommandDef` 字面量（本机 69 条） |
| `hermes_cli.plugins.get_plugin_commands` / `agent.skill_commands.get_skill_commands` | 插件/技能命令缺失 | 与静态注册表合并，缺失只是少一类 |
| lark-oapi `Client._handle_data_frame` | 函数体哈希不匹配就整段放弃 | 先哈希精确匹配，再「CARD 分支存在且是纯丢弃」结构匹配；SDK 已处理时报 `already_ok` |
| 内置 feishu 平台声明 `recorded_platform_kwargs()` | 录音失败直接放弃注册（降级纯文本） | 失败时 AST 静态解析 `register_platform(...)`，字面量+模块名解析，解析不了才放弃 |

判据：`tests/check_compat.py`（6 组）——预览兜底、结构修补命中、already_ok、真干活分支不改写、
静态平台声明、静态命令注册表。`run_all.py` 13 → **15** 项。

## 回合归属不再依赖 admission 钩子（1.7.13，实测定性）

钩子探针证明：`post_gateway_admission` 在本版本**从不派发**（`pre_tool_call` /
`post_tool_call` / `post_api_request` / `on_stream_delta` / `post_llm_call` /
`on_session_end` 都正常触发）。于是回合登记换成真正会响的钩子，投递侧负责认领：

| 环节 | 做法 |
|---|---|
| 建回合 | `_on_post_api_request` → `_ensure_turn()`（session_key = session_id），usage 直接挂上 |
| 认领 | `TurnRegistry.adopt()`：本平台**恰好一个**活动回合且 15 分钟内才认领，并补 `chat_id`/`thread_id` |
| 之后 | 严格 `for_chat()` 命中，整轮 `notice=0`；多回合/过期一律不认（不赌） |
| 观测 | 每个钩子首次触发打 `hook fired: <name>`；`send` 打 metadata 键名；`edit` 打 `turn=` |

实测（重启后一轮真实问答）：
```
09:03:39 hook fired: post_api_request
09:03:40 turn adopted by delivery (id=d0ce7873… chat=oc_60db…)
09:03:40 send … turn=d0ce7873… notice=0
09:06:20 card_v2 text=7127 rendered=7127 segs=4 notice=0
检查点：status=completed dur=47.6 model=cn:deepseek-v4.1-flash
        tokens={input 563, output 905, cache_read 164992}
```
`notice=0` 54 帧、`notice=1` 仅启动通知 1 帧。

## 回合登记的两处硬化（1.7.12 后续）

现场：重启后的某些回合仍被判成通知卡（`notice=1`），说明 `_turn_for` 连兜底都命不中——
即 REGISTRY 里**没有这个 chat 的回合**，问题在登记侧而非匹配侧。

| 硬化 | 说明 |
|---|---|
| `_source_field()` | `post_gateway_admission` 的 `source` 可能是 dict / dataclass / 普通对象，原来只认 dict → chat_id 空 |
| `_chat_from_session_key()` | 钩子没给 chat_id 时从 `agent:main:feishu:dm:oc_xxx` 反推（只认 `oc_`/`on_` 前缀） |
| `platform` 判断放宽 | `platform` 为空时不再直接 return（部分路径不带该字段） |
| `_note_turn_miss()` | 查找失败打一条 warning（同 chat 60s 一条），带「当前活动回合的 chat/thread」快照，下次复发能直接定位 |

## 回合查找兜底（1.7.12，现场日志发现）

实测日志：一整轮正常回复的每帧都是 `turn=-`，被判成回合外通知卡（`notice=1`），footer 因此没有耗时/token：

```
08:41:05 send ... turn=- len=600      ← 真实回合，但 turn 判定失败
08:41:05 card_v2 text=600 notice=1
08:41:09 edit mid=om_… len=1117       ← 后续帧仍在同一张「已完成」卡上流式
```

根因：`for_chat()` 要求 `(chat_id, thread_id)` **严格**匹配，两侧 thread_id 不一致（一侧有值一侧空）就返回 None。

修法：新增 `TurnRegistry.for_chat_loose()` —— 严格匹配失败时，同 chat **恰好一个**活动回合才认它；
多候选（同 chat 多线程并发）仍返回 None，不赌。命中时打 debug 日志。

## 自审后的收尾（1.7.11）

随 P1/P2 一起修掉的 P2/P3：

| 项 | 修法 |
|---|---|
| 终稿与流式正文不同源会误判成通知卡（footer 丢耗时/token） | 新增 `same_delivery()` 保险丝：首 40 字相同 + 本键 120s 内活动 → 视为终稿；`card_v2` 日志加 `notice=0/1` 便于事后核对 |
| 未接线的死代码 | 删 `MaintenanceGate.drain()`、`TURN_PRODUCERS` / `classify(producer=)`（metadata 里根本没有 producer 字段） |
| 未用 import | `diagnostics.format_diff`、`notice_lifecycle.Iterable` |
| `/card-doctor` 与支持报告里的异常原文可能带 token | `note_error` 走 `_sanitize()`：核心脱敏器 + 本地 `access_token=/token:` 规则 + 200 字截断 |
| `METRICS.inc("send")` 语义 | `/card-status` 文案改为「投递（send=尝试数，edit=成功编辑）」 |
| 速率事件上限 8 条/次 | 提到 32 条/次 |
| 峰值速率不衰减 | 超一个窗口无活动即归零 |
| `LARK_CARD_STATE_DIR` 指向符号链接（如 TMPDIR）会静默不落盘 | 状态目录先 `resolve()` |
| `.gitignore` 的 `state/` | 已冗余，删除 |

## 静态兜底的解析缺陷（1.7.11，自审发现）

1.9 声称「动态录音失败 → AST 静态解析 register_platform」，实际是死代码：

1. 上游把 `register()` 放在 `adapter.py`（不是 `__init__.py`），旧实现只读 `__init__.py` → 永远找不到；
2. 真实声明里 kwargs 用 `**SEND_ENTRY_KWARGS` / `**DASHBOARD_CARD` / `**gateway_env.AUTHZ` 展开，旧实现见 `**` 直接返回 None。

现在：扫描包内所有顶层模块找 `register()`；支持 `**mapping`（解析成 dict 后合并）、
函数内相对 import（`from . import gateway_env` / `from .x import Y`）、位置参数。
实测与动态录音**逐字段一致**（29 个键，`adapter_factory` 同一对象）。
同一处还修了 glob 兜底顺序：原来放在候选之前且用 `**` 递归，白付 0.46s 启动时间；现在只在候选失败时做浅层 glob（0.002s）。

## 运行时状态移出插件目录（1.7.10）

检查点目录从 `<插件>/state/` 改为 `$HERMES_HOME/state/lark-card-for-hermes/`（可用 `LARK_CARD_STATE_DIR` 覆盖），原因两条：

1. `hermes plugins validate` 的安全扫描会读插件目录里的所有 JSON——检查点含正文快照，会被判成 `destructive_home_rm` / `dump_all_env`，**整个插件校验失败**；
2. 插件目录可能被 `git clean -fdx` / 重装整体替换，运行时检查点不该跟着消失（否则网关崩溃后没人收尾那张「执行中」卡）。

历史检查点已迁移（36 个），插件目录内 `state/` 现在为空。

## 生效与回滚

- 命令注册与代码改动需要重启网关（`/card-preset`、`/card-activity` 这类只改 config 的才是热加载）。
- 回滚：`git checkout -- __init__.py hfc/bridge.py` 并删除 7 个新模块与 `tests/check_ops.py`；
  或直接用 `~/.hermes/builds/lark-card-ops-layer.patch` 反向 `git apply -R`。

## 维护态语义（重要）

`/card-maintenance on` 只跳过**流式编辑**，终稿、通知卡、命令卡照常投递；不会把消息降级成纯文本。
用于 `hermes update` 或大改配置的窗口期。

## 已知边界

- `/card-status` 的配置差异以「注册时快照」为基准；`/card-preset`、`/card-activity` 会刷新快照，
  其余命令若绕过这两条路径改 config.json，需要重启才能消除差异提示。
- `notice_lifecycle` 判定仍是启发式（前缀 + 正文同源）；上游若有 producer 字段可替换为精确判定。

## 页脚补丁（2026-11-11）：task 预设不再吞掉「工具 #N」

上游 `hfc/render.py::_render_footer` 用 `compact=presentation=="task"` 判定，task 阅读预设下
三处 `if session.tool_count and not compact` 全部不成立 ⇒ 正常回复卡页脚只剩
`已完成 · 10s · model · ↑in · ↓out · ctx`，「工具 #N」整段消失（用户报「底栏少了一部分内容」）。

| 位置 | 改动 |
|---|---|
| `_render_footer` waiting 分支（约 2745 行） | 去掉 `not compact` 条件，保留 `tool_activity != "hidden"` |
| `_render_footer` running 分支（约 2771 行） | 同上 |
| `_render_footer` completed 分支（约 2841 行） | 去掉 `not compact`，工具数重新领先其余指标 |

`not compact` 仍保留在「上一次动作文案」分支（长文案在 task 卡里确实挤）。
备份：`hfc/render.py.bak-20261111_095210`。离线验证（用真实 checkpoint 会话 render）：

```
TASK(patched): 已完成 · 工具 #5 · 2m52s · custom/cn:deepseek-v4.1-flash · ↑922 · ↓704
```

## 页脚 ctx 段补丁（2026-11-11 第二轮）：`ctx 4k/1m 0%` 恒缺失

用户重启后仍无 ctx 段。根因不在渲染层，而在**取数**：`_on_post_api_request` 只读 `usage["context"]`，
但 Hermes `post_api_request` 载荷把窗口上限放在**顶层 `context_length`**，usage 里根本没有 `context` 键
（`agent/turn_response_intake.py::_fire_post_api_request_hook`，usage = `normalize_usage()` 的 asdict，
只有 input/output/prompt/total/cache_* 等）；因此 `session.context` 恒 `{}`，渲染层
`available["context"] = max_context > 0 and "used_tokens" in context` 永假 ⇒ 整段不显示。
checkpoint 实测：`tokens.prompt_tokens=152986` 有值，`context={}`。

| 位置 | 改动 |
|---|---|
| `__init__.py::_api_context`（新增） | `used_tokens` ← `usage.prompt_tokens`（回退 `total_tokens` → `input_tokens+cache_read+cache_write`）；`max_tokens` ← 顶层 `context_length`；若调用方已给 `usage["context"]` 则以其为准 |
| `__init__.py::_on_post_api_request` | 新增 `context_length` 形参（`**_kw` 本来会接住，但显式声明后语义清楚），把 `_api_context()` 结果传入 `REGISTRY.set_usage(context=…)` |

备份 `__init__.py.bak-20261111_095714`。离线全链路验证（hook → TurnRegistry → HfcTurnBridge → render）：
```
turn.usage.context = {"used_tokens": 152986, "max_tokens": 1000000}
footer = 已完成 · 10s · custom/cn:deepseek-v4.1-flash · ↑922 · ↓704 · ctx 153k/1m 15%
tests/run_all.py = ALL PASS (15 checks)
```
生效需用户在外部 shell 重启网关（插件跑在网关进程内，改文件不会自动重载）。

## 回合串行收尾：新消息进来即封口旧回合（2026-11-11 第三轮）

用户报「我发的命令跑到了插件卡片后面」。**不是消息排序错乱**：飞书按发送时间排，卡片位置在创建时就固定。
真因是上一回合迟迟不收尾 —— 日志实测（2026-11-11）：

```
10:09:58  turn=9d9612ec 建卡（_interim_send）
10:24:32  admission -> turn=07926773（用户发「mytdx运行状态」）
10:24:36  send ... turn=9d9612ec len=275      ← 旧回合仍在活跃
10:25:11  edit mid=...2264b8c finalize turn=07926773（1405 字终稿）
```

旧卡活了 932s（footer 15m32s），用户新消息只能排在它下面 ⇒ 看起来像「插件跑到命令前面」。

上游 HFC 的做法（`hermes_feishu_card/server.py`）：`_abandon_stale_sessions_for_chat()`（4746/4843）
在**同 chat 出现新 message.started 时**把旧 session `timeline.complete()` + `status=failed` +
正文追加 `> 本轮已被新对话替代，任务尚未确认完成。`，再由
`_schedule_abandoned_session_terminal_update()` 立刻把旧卡推成终态（日志 `Abandoning stale session …`）。
即：卡片生命周期严格串行。

本插件缺这一步，补齐四处：

| 位置 | 改动 |
|---|---|
| `turn_state.py::supersede()`（新增） | 封口同 chat 全部未收尾回合并返回；`begin()` 替换同 key 回合时也一并 final（否则它继续参与 chat 匹配） |
| `__init__.py::_seal_previous_turns()`（新增） | admission 里先封口再登记：`_TURNS.fail()` + `note(SEAL_NOTE)` + **同步** render 出终态卡（此刻 bridge 仍持有旧 session），网络更新丢给事件循环 |
| `__init__.py::CardFeishuAdapter.seal_superseded_card()`（新增） | 用 `_turn_card[chat]` 里记录的旧消息 id 调 `cardkit.update()` 推终态；`_adapter_factory` 登记 `_LIVE_ADAPTER` 供 hooks 定位 |
| `__init__.py::_turn_for()` | 显式 `metadata["turn_id"]` 优先（同 chat 并发两回合时不再靠 chat 猜） |

开关：`LARK_CARD_SEAL_ON_NEW_MESSAGE=0` 关闭封口（默认开启）。文案 `SEAL_NOTE` 与 HFC 逐字一致。
判据：`tests/check_seal.py`（5 组断言：supersede 语义、begin 同 key 收尾、封口卡含替代文案+已停止+保留正文、
开关关闭不封口、真实 admission 后只剩一个 active），`run_all.py` = ALL PASS (16 checks)。

已知代价：同 chat 真并发（用户连发两条）时前一条会**立刻**显示「已被新对话替代」并转失败态 ——
与 HFC 行为一致；不能接受时用上面的环境变量关掉。
