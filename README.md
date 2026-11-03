# Lark Card for Hermes

把 Hermes 在飞书里的回复渲染成**一张持续更新的卡片**：状态头、工具时间线、思考面板、原生表格、token 统计、流式正文，一回合一条消息。

这是一个 Hermes **平台插件**，只覆写飞书 adapter 的 `send` / `edit_message` 等出口，不修改 Hermes 源码。

## 效果

一次带工具的回合产出一条卡片：

```
Hermes Agent · 已完成
| 项 | 值 |
|------|------|
| echo hello | hello |
| date +%Y | 2026 |
两条命令均执行成功，退出码 0，当前年份为 2026。
▶ 执行过程（点按展开/收起）
    ✓ terminal · #1 · 179ms
    　echo hello + 1 条命令
---
22s · provider/model · ↑116 · ↓72
```

- **状态头**：`⏳ 执行中` → `✅/⛔ 完成/失败`，失败与中断都有独立终态
- **工具时间线**：工具名、状态、耗时、脱敏后的参数预览；子代理同样上榜
- **思考面板**：推理进可折叠区域，不占正文
- **原生表格**：Markdown 表格渲染成飞书 table 组件，而不是一堆管道符
- **footer**：用时、模型、输入/输出 token、上下文用量
- **流式正文**：边生成边更新，结束不留光标字符
- **交互卡片**：`clarify` 的选项渲染成按钮，点了真的能回传；超时/被取代会把按钮撤下

## 为什么不会因为 Hermes 升级而挂

这一点是设计前提。已有的同类方案（HFC）需要**往 Hermes 源码里注入 hook** 并对源码做整文件字节校验，Hermes 每发一个新版本就打不上补丁——它的 changelog 里甚至直接写死要适配的 commit（如 `9b904dcc22c23...`）。

本项目反过来：

| | 源码注入方案 | 本项目 |
|---|---|---|
| 改动 Hermes 源码 | 是 | **否** |
| 整文件字节校验 | 是（升级即失效） | 否 |
| 事件来源 | 注入 hook | 官方插件 hook（`VALID_HOOKS` 内的 10 个） |
| 接口面 | 大量私有符号 | 7 个 import + 3 个 adapter 私有函数 |

渲染逻辑直接复用 HFC 的渲染栈（MIT，见 `hfc/`），那部分代码在其 5.0.9 → 5.0.13 之间**逐字节未变**——上游的维护成本全部花在源码适配层，而我们不需要那一层。

即便上游改了被我们依赖的私有函数，失败方式是**卡片退化成内置文本**（每条 `send`/`edit` 都有回落路径），而不是飞书不可用。

## 依赖

- Hermes Agent（带插件系统与 `post_gateway_admission` 等 hooks）
- `lark-oapi` **1.6.8** —— 该版本的 `Client._handle_data_frame` 会静默丢弃 CARD 帧，导致**卡片按钮点击无效**。`hfc/feishu_ws_compat.py` 用 AST 精确匹配那段函数体并修掉，版本不符会自动放弃（`connect()` 成功后日志会写明是否修复）。

## 安装

```bash
git clone https://github.com/Keithin/lark-card-for-hermes.git ~/.hermes/plugins/feishu-card
```

然后在独立 shell 里重启网关（不能在网关进程内部重启）：

```bash
hermes gateway restart
```

插件在网关启动时注册；改了插件代码必须重启才生效。

凭据沿用 Hermes 已有的飞书配置（环境变量或钥匙串），本插件不额外存凭据。

## 配置

`config.json` 会被 `hfc/config_rules.py` 校验，字段名全部是 HFC 的原有 schema：

```json
{
  "card": {
    "reading_preset": "task",
    "native_tables": true,
    "show_reasoning": true,
    "timeline_order": "newest_first",
    "timeline_expanded": false,
    "max_timeline_items": 12,
    "max_tool_result_chars": 600,
    "table_overflow_mode": "compact",
    "footer_fields": ["duration", "model", "input_tokens", "output_tokens", "context"]
  },
  "bindings": {
    "native_chats": []
  }
}
```

### 阅读预设

`reading_preset` 四档，影响整个卡片布局：

| preset | 布局 | 思考进正文 | 时间线展开 | 隐藏已完成工具 |
|---|---|---|---|---|
| `classic` | classic | ✓ | ✗ | ✗ |
| `focused` | classic | ✗ | ✗ | ✓ |
| `detailed` | classic | ✗ | ✓ | ✗ |
| `task` | task | ✗ | ✓ | ✓ |

> **注意**：显式写在 `card` 里的值会**覆盖**预设默认。切预设时要把冲突的旧键删掉，否则只生效一半。

### 按 chat 选择卡片或原生文本

```json
"bindings": { "native_chats": ["oc_xxxxxxxx"] }
```

列进去的 chat 走飞书原生文本，其余仍走卡片。留空数组或不写该键 = 全部走卡片。

## 结构

```
__init__.py          # 插件入口：adapter 覆写 + 10 个 hook + clarify 按钮卡
plugin.yaml          # 插件清单（内部 ID 保持 feishu-card）
hfc/                 # HFC 渲染栈（MIT），原样复用
  bridge.py          #   ← 本项目新增：事件翻译层
  client.py          #   ← 本项目新增：最小 REST 通道
  render.py session.py cardkit.py card_timeline.py native_tables.py ...
```

`hfc/` 下的渲染与状态机代码来自 [Hermes Feishu Streaming Card](https://github.com/baileyh8/hermes-feishu-streaming-card)（MIT）。本项目新增的只有接线层（`bridge.py` / `client.py`）与 adapter 覆写（`__init__.py`）。

## 已知限制

- 同一 chat 并发两条消息时，卡片状态按 chat 合并，可能互相干扰（未实测）
- 会话状态按 LRU 保留最近 64 个 chat，超出的 chat 再回来时卡片会从头开始
- `observed_rate` 模块已引入但未接线（footer 不显示速率）

## 许可

MIT。`hfc/` 目录保留上游版权声明，详见 `LICENSE`。
