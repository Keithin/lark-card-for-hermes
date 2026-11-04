# Lark Card for Hermes（Hermes Agent飞书卡片回复）

**English** · [简体中文](#简体中文)

---

## English

Renders each Hermes reply in Feishu/Lark as **one live card** that updates in place — status header, tool timeline, collapsible reasoning panel, native tables, token footer, streaming body. One message per turn.

This is a Hermes **platform plugin**. It overrides only the Feishu adapter's outbound `send` / `edit_message` primitives and never touches Hermes source code.

![A real Feishu card: status header, a Markdown table rendered as a native table, and a token footer](docs/screenshot-card.png)

*Screenshot taken from Feishu. The Markdown table renders as a **native table component** — header shading, cell borders, aligned columns — not a wall of `|` pipes. The footer shows duration, model and token usage.*

### What you get

- **Status header** — `⏳ running` → `✅ done` / `⛔ failed`, with distinct terminal states for failure and interruption.
- **Tool timeline** — tool name, status, duration, redacted argument preview. Subagents show up too. Collapsed by default in `task` mode.
- **Reasoning panel** — thinking goes into a collapsible block instead of the answer body.
- **Native tables** — Markdown tables become Feishu `table` components.
- **Footer** — duration, model, input/output tokens, context usage.
- **Streaming body** — updates as it generates, no leftover cursor glyph when it finishes.
- **Interactive clarify** — `clarify` options render as real buttons that send the answer back; expired or superseded prompts have their buttons withdrawn.

### Why it does not break when Hermes upgrades

This is the design premise. The existing alternative (HFC) **injects hooks into Hermes source** and verifies those files by whole-file byte hash, so every new Hermes release invalidates the patch — its own changelog pins the exact commit it targets (e.g. `9b904dcc22c23...`).

This project does the opposite:

| | Source-injection approach | This project |
| --- | --- | --- |
| Modifies Hermes source | Yes | **No** |
| Whole-file byte verification | Yes (breaks on upgrade) | No |
| Event source | Injected hooks | Official plugin hooks (10 of them, all in `VALID_HOOKS`) |
| Surface area | Many private symbols | 7 imports + 3 adapter private functions |

The rendering logic reuses HFC's rendering stack (MIT, see `hfc/`). That code was **byte-identical** across HFC 5.0.9 → 5.0.13 — upstream's maintenance cost all goes into the source-adaptation layer, which this project does not need.

Even if upstream changes a private function we depend on, the failure mode is **the card degrading to bundled text** (every `send` / `edit` has a fallback path), not Feishu going down.

### Requirements

- Hermes Agent with a plugin system and hooks such as `post_gateway_admission`
- `lark-oapi` **1.6.8** — that version's `Client._handle_data_frame` silently drops CARD frames, which makes **card button clicks do nothing**. `hfc/feishu_ws_compat.py` matches that function body by AST hash and repairs it; on a version mismatch it declines automatically (`connect()` logs whether the repair was applied).

### Install

```bash
git clone https://github.com/Keithin/lark-card-for-hermes.git ~/.hermes/plugins/feishu-card
```

Then restart the gateway **from a separate shell** (you cannot restart it from inside the gateway process):

```bash
hermes gateway restart
```

The plugin registers at gateway startup; editing plugin code requires a restart to take effect.

Credentials reuse Hermes' existing Feishu configuration (environment variables or keychain). This plugin stores none of its own.

### Configuration

`config.json` is validated by `hfc/config_rules.py`. Every key follows HFC's existing schema:

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

#### Reading presets

`reading_preset` has four levels, each changing the whole card layout:

| preset | layout | thinking into body | timeline expanded | hide finished tools |
| --- | --- | --- | --- | --- |
| `classic` | classic | ✓ | ✗ | ✗ |
| `focused` | classic | ✗ | ✗ | ✓ |
| `detailed` | classic | ✗ | ✓ | ✗ |
| `task` | task | ✗ | ✓ | ✓ |

> **Note:** values written explicitly under `card` **override** the preset defaults. When switching presets, delete the conflicting old keys — otherwise only half the preset applies.

#### Per-chat: card or native text

```json
"bindings": { "native_chats": ["oc_xxxxxxxx"] }
```

Listed chats get native Feishu text; everything else keeps cards. An empty array or a missing key means all chats use cards.

### Layout

```
__init__.py          # plugin entry: adapter overrides + 10 hooks + clarify button cards
plugin.yaml          # manifest (internal ID stays feishu-card)
hfc/                 # HFC rendering stack (MIT), used as-is
  bridge.py          #   ← new here: event translation layer
  client.py          #   ← new here: minimal REST channel
  render.py session.py cardkit.py card_timeline.py native_tables.py ...
```

The rendering and state-machine code under `hfc/` comes from [Hermes Feishu Streaming Card](https://github.com/baileyh8/hermes-feishu-streaming-card) (MIT). The only code added by this project is the wiring layer (`bridge.py` / `client.py`) and the adapter overrides (`__init__.py`).

### Known limitations

- Session state is kept for the most recent 64 chats (LRU). A chat that falls off the list starts its card from scratch when it returns.
- `observed_rate` is imported but not wired up, so the footer never shows a rate.

### License

MIT. The `hfc/` directory retains its upstream copyright notice — see `LICENSE`.

---

## 简体中文

把 Hermes 在飞书里的回复渲染成**一张持续更新的卡片**——状态头、工具时间线、可折叠思考面板、原生表格、token 统计、流式正文，一回合一条消息。

这是一个 Hermes **平台插件**：只覆写飞书 adapter 的 `send` / `edit_message` 出口，**不修改 Hermes 源码**。

![飞书真实卡片：状态头 + Markdown 表格渲染成原生表格 + token footer](docs/screenshot-card.png)

*飞书实拍。Markdown 表格渲染成**原生 table 组件**——有表头底色、单元格边框、按列对齐——而不是一堆 `|` 竖线。底部 footer 是用时、模型和 token 用量。*

### 你能得到什么

- **状态头**：`⏳ 执行中` → `✅ 完成` / `⛔ 失败`，失败与中断各有独立终态。
- **工具时间线**：工具名、状态、耗时、脱敏后的参数预览；子代理同样上榜。`task` 模式下默认折叠。
- **思考面板**：推理进可折叠区域，不占正文。
- **原生表格**：Markdown 表格渲染成飞书 `table` 组件。
- **footer**：用时、模型、输入/输出 token、上下文用量。
- **流式正文**：边生成边更新，结束不留光标字符。
- **交互式 clarify**：选项渲染成真按钮，点了会真的回传；超时或被取代的提问会把按钮撤下。

### 为什么不会因为 Hermes 升级而挂

这是设计前提。已有的同类方案（HFC）**往 Hermes 源码里注入 hook** 并对源码做整文件字节校验，所以 Hermes 每发一个新版本就打不上补丁——它的 changelog 里直接写死要适配的 commit（如 `9b904dcc22c23...`）。

本项目反过来：

| | 源码注入方案 | 本项目 |
| --- | --- | --- |
| 改动 Hermes 源码 | 是 | **否** |
| 整文件字节校验 | 是（升级即失效） | 否 |
| 事件来源 | 注入 hook | 官方插件 hook（`VALID_HOOKS` 内的 10 个） |
| 接口面 | 大量私有符号 | 7 个 import + 3 个 adapter 私有函数 |

渲染逻辑直接复用 HFC 的渲染栈（MIT，见 `hfc/`）。那部分代码在 5.0.9 → 5.0.13 之间**逐字节未变**——上游的维护成本全花在源码适配层，而本项目不需要那一层。

即便上游改了被依赖的私有函数，失败方式是**卡片退化成内置文本**（每条 `send` / `edit` 都有回落路径），而不是飞书不可用。

### 依赖

- 带插件系统和 `post_gateway_admission` 等 hooks 的 Hermes Agent
- `lark-oapi` **1.6.8** —— 该版本的 `Client._handle_data_frame` 会静默丢弃 CARD 帧，导致**卡片按钮点击无效**。`hfc/feishu_ws_compat.py` 用 AST 精确匹配那段函数体并修掉；版本不符会自动放弃（`connect()` 成功后日志会写明是否修复）。

### 安装

```bash
git clone https://github.com/Keithin/lark-card-for-hermes.git ~/.hermes/plugins/feishu-card
```

然后在**独立 shell** 里重启网关（不能在网关进程内部重启）：

```bash
hermes gateway restart
```

插件在网关启动时注册；改了插件代码必须重启才生效。

凭据沿用 Hermes 已有的飞书配置（环境变量或钥匙串），本插件不额外存凭据。

### 配置

`config.json` 会被 `hfc/config_rules.py` 校验，字段名全部沿用 HFC 的原有 schema：

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

#### 阅读预设

`reading_preset` 四档，每档改变整张卡的布局：

| preset | 布局 | 思考进正文 | 时间线展开 | 隐藏已完成工具 |
| --- | --- | --- | --- | --- |
| `classic` | classic | ✓ | ✗ | ✗ |
| `focused` | classic | ✗ | ✗ | ✓ |
| `detailed` | classic | ✗ | ✓ | ✗ |
| `task` | task | ✗ | ✓ | ✓ |

> **注意**：显式写在 `card` 里的值会**覆盖**预设默认。切预设时要把冲突的旧键删掉，否则只生效一半。

#### 按 chat 选择卡片或原生文本

```json
"bindings": { "native_chats": ["oc_xxxxxxxx"] }
```

列进去的 chat 走飞书原生文本，其余仍走卡片。留空数组或不写该键 = 全部走卡片。

### 结构

```
__init__.py          # 插件入口：adapter 覆写 + 10 个 hook + clarify 按钮卡
plugin.yaml          # 插件清单（内部 ID 保持 feishu-card）
hfc/                 # HFC 渲染栈（MIT），原样复用
  bridge.py          #   ← 本项目新增：事件翻译层
  client.py          #   ← 本项目新增：最小 REST 通道
  render.py session.py cardkit.py card_timeline.py native_tables.py ...
```

`hfc/` 下的渲染与状态机代码来自 [Hermes Feishu Streaming Card](https://github.com/baileyh8/hermes-feishu-streaming-card)（MIT）。本项目新增的只有接线层（`bridge.py` / `client.py`）与 adapter 覆写（`__init__.py`）。

### 已知限制

- 会话状态按 LRU 保留最近 64 个 chat，超出的 chat 再回来时卡片会从头开始。
- `observed_rate` 已引入但未接线，footer 不显示速率。

### 许可

MIT。`hfc/` 目录保留上游版权声明，详见 `LICENSE`。
