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
