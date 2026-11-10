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
