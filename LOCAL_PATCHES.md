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
