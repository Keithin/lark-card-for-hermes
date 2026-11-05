"""feishu-card (lark-cards) — Feishu/Lark 出站消息渲染为交互卡片（含 cardkit 流式 + 工具时间线）。

路线 2 的实现：只用官方插件 API 替换内置 Feishu 适配器的两个原语（``send`` /
``edit_message``），不往 Hermes 源码里打任何补丁，因此 ``hermes update`` 的 git reset
和 PM 环境重建都不会冲掉它。

三级渲染，逐级回落：
1. cardkit 流式（打字机）：建卡片实体 → 发消息引用 card_id → 逐帧更新同一元素 →
   ``finalize`` 时关闭 streaming_mode。
2. 全卡 PATCH（v1 卡片）：cardkit 不可用时整卡更新（已验证可用）。
3. 内置适配器文本/post 路径（多消息拆分）。

卡片正文上方的时间线来自进程内 lifecycle hooks（``pre_tool_call`` /
``post_tool_call`` / ``subagent_*``），只更新模块内状态，不调 async API；下一次正文
edit 时一并上屏。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# HFC 的渲染/状态机原样复用（hfc/ 下是从 HFC 备份整份搬来的模块，见 hfc/bridge.py 说明）。
try:
    from .hfc.bridge import HfcTurnBridge, load_card_config
    from .hfc.card_limits import inspect_card_limits
    from .hfc.cardkit import CardKitTransport
    from .hfc.command_cards import (
        hfc_command_result_card, hfc_command_result_template, hfc_command_result_title,
    )
    from .hfc.existing_owner_projection import project_existing_owner
    from .hfc.legacy_owner import static_legacy_receipt
    from .hfc.native_commands import SAFE_QUICK_COMMANDS
    from .hfc.owner_display_policy import OwnerRefreshState, collapsed_transfer_elements
    from .hfc.render import CardRenderResult
    from .hfc.text import TableOverflowResult
    from .hfc.canonical_turn import consume_canonical_turn_id, publish_canonical_turn_id
    from .hfc.client import RestClient
    from .hfc.config_rules import READING_PRESETS, normalize_reading_preset
    from .hfc.delivery_policy import CARD_DISPOSITION, NATIVE_DISPOSITION, ChatDeliveryPolicy
    from .hfc.feishu_ws_compat import _repaired_function as _ws_repaired_function
    from .hfc.interaction_input import InteractionInputError, parse_choice
    from .hfc.observed_rate import ObservedRates
    from .hfc.transient_notices import is_transient_busy_ack, transient_notice_recall_seconds
except ImportError:  # 判据脚本按文件路径加载插件时没有包上下文
    sys.path.insert(0, str(Path(__file__).parent))
    from hfc.bridge import HfcTurnBridge, load_card_config
    from hfc.card_limits import inspect_card_limits
    from hfc.cardkit import CardKitTransport
    from hfc.command_cards import (
        hfc_command_result_card, hfc_command_result_template, hfc_command_result_title,
    )
    from hfc.existing_owner_projection import project_existing_owner
    from hfc.legacy_owner import static_legacy_receipt
    from hfc.native_commands import SAFE_QUICK_COMMANDS
    from hfc.owner_display_policy import OwnerRefreshState, collapsed_transfer_elements
    from hfc.render import CardRenderResult
    from hfc.text import TableOverflowResult
    from hfc.canonical_turn import consume_canonical_turn_id, publish_canonical_turn_id
    from hfc.client import RestClient
    from hfc.config_rules import READING_PRESETS, normalize_reading_preset
    from hfc.delivery_policy import CARD_DISPOSITION, NATIVE_DISPOSITION, ChatDeliveryPolicy
    from hfc.feishu_ws_compat import _repaired_function as _ws_repaired_function
    from hfc.interaction_input import InteractionInputError, parse_choice
    from hfc.observed_rate import ObservedRates
    from hfc.transient_notices import is_transient_busy_ack, transient_notice_recall_seconds

logger = logging.getLogger(__name__)

_BASE_PACKAGE = "hermes_feishu_bundled"
CARD_TITLE = "Hermes"
ELEMENT_ID = "main"
TIMELINE_ID = "timeline"
# Above this the bundled text/post path wins: it splits into several messages, a single card cannot.
CARD_MAX_CHARS = 12000
# cardkit 流式总开关；置 False 即退回「全卡 PATCH」这一级。
STREAMING = True
# 单卡：一个回合一张卡，进度行与答复都写到同一张（网关的 _adopt_message_id 接受复用 id，
# 只有 cfg.fresh_final_after_seconds>0 或 adapter.prefers_fresh_final_streaming 才会另发一条）。
MERGE_TOOL_CARD = True
# 时间线最多渲染这么多行，更早的折叠成一行。
TIMELINE_MAX_ROWS = 12
# 以下三条是 HFC cardkit.py 的实测参数：变更要按 sequence 对账、别打太快、结构没变也要周期性整卡刷新。
MIN_MUTATION_INTERVAL = 0.125
FULL_UPDATE_INTERVAL = 2.0


def _text_partition(card: dict) -> tuple[dict, dict[str, str]]:
    """卡片一分为二：结构（markdown 文本置空）+ 各元素文本。

    HFC `cardkit.py::_text_partition` 的同款手段：结构没变就只推变化的元素文本，避免整卡重建。
    """
    structure = json.loads(json.dumps(card))
    texts: dict[str, str] = {}
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            element_id = node.get("element_id")
            if node.get("tag") == "markdown" and isinstance(element_id, str) and 1 <= len(element_id) <= 20:
                texts[element_id] = node.get("content") or ""
                node["content"] = ""
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(structure)
    return structure, texts


# 进程内唯一的 HFC 回合桥：hooks 喂事件，适配器取渲染结果。
_TURNS = HfcTurnBridge()
_TURNS_LOCK = threading.RLock()

_OBSERVED_RATES = ObservedRates()
_POLICY: Optional[ChatDeliveryPolicy] = None


def _install_card_frame_repair() -> bool:
    """lark-oapi 1.6.8 的 `_handle_data_frame` 对 CARD 帧直接 `return`（读 SDK 源码确认），
    卡片按钮回调（clarify / 审批）会被静默丢弃。这里用 HFC 的 AST 补丁换掉那段分支；
    版本或字节对不上会自动放弃，不改 SDK 文件、不换 Client 类。"""
    try:
        from lark_oapi.ws.client import Client

        repaired = _ws_repaired_function(Client._handle_data_frame)
        if repaired is None:
            return False
        Client._handle_data_frame = repaired
        return True
    except Exception:
        return False


def _plugin_config() -> dict[str, Any]:
    """读插件自己的 config.json（HFC 的 schema：card / bindings / profiles）。"""
    try:
        return json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _delivery_policy() -> ChatDeliveryPolicy:
    """HFC 的 chat 级投递策略：bindings.native_chats 里的 chat 走原生文本，其余走卡片。"""
    global _POLICY
    if _POLICY is None:
        try:
            _POLICY = ChatDeliveryPolicy.from_config(_plugin_config())
        except Exception as exc:
            logger.warning("[feishu-card] delivery policy 配置无效，默认全部走卡片: %s", exc)
            _POLICY = ChatDeliveryPolicy()
    return _POLICY

# ── 回合时间线 ──────────────────────────────────────────────────────────────────
# 工具 hook 不带 chat_id，所以行只进一个「当前回合」桶，渲染时按 chat_id 绑定。
# shortcut: 同一时刻只有一个 chat 在跑工具；多 chat 并发会把行混进同一桶。
_LOCK = threading.RLock()
_TURN: dict[str, Any] = {"chat": None, "rows": [], "started": 0.0, "final": False}
_ICON = {"running": "⏳", "ok": "✅", "error": "❌"}


def _card_title() -> str:
    """卡片标题：配置里的 title（HFC 的 card.title），缺省 Hermes Agent。"""
    return str((_TURNS.config or {}).get("title") or "Hermes Agent")


def _header_for_v1(chat_id: str) -> tuple[str, str]:
    """v1 兜底卡的标题/配色取 HFC 渲染出的 header（状态头+工具数+时长），取不到才退回配置名。"""
    try:
        with _TURNS_LOCK:
            card = _TURNS.render(chat_id, streaming=False)
        header = (card or {}).get("header") or {}
        title = str((header.get("title") or {}).get("content") or "").strip()
        template = str(header.get("template") or "").strip()
        if title:
            return title, (template or "blue")
    except Exception:   # 渲染不可用不能挡住兜底卡
        logger.debug("[feishu-card] v1 header fallback", exc_info=True)
    return _card_title(), "blue"


def _redact(text: str) -> str:
    """Hermes 自己的敏感信息脱敏（force：展示给聊天必须脱，不看 security.redact_secrets）。"""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:  # 脱敏器不可用也不能挡住投递，但要留痕
        logger.debug("[feishu-card] redact unavailable", exc_info=True)
        return text


def _preview(tool_name: str, args: Any) -> str:
    """一行工具参数摘要（已脱敏），摘要构造失败就不显示。"""
    try:
        from agent.display import build_tool_preview

        return _redact(build_tool_preview(tool_name, args or {}, max_len=48) or "")
    except Exception:
        return ""


# ── 命令卡（HFC command_cards / native_commands）───────────────────────────────
# 斜杠命令的回复是「命令反馈」而不是回答：HFC 给它单独的卡（标题/配色随命令与结果）。
# shortcut: 网关送命令回复时不带命令名，所以用「命令 handler 先登记正文」这一条确定性的边，
# 只在插件自己注册的命令上生效；要覆盖内置命令得等上游在 send metadata 里给出命令名。
_COMMAND_REPLY_TTL = 120.0
_COMMAND_REPLIES: dict[str, tuple[float, str]] = {}   # 回复正文 -> (登记时刻, 命令名)


def _mark_command_reply(command: str, text: str) -> None:
    """命令 handler 返回前登记回复正文，send() 据此把它渲染成命令卡。"""
    body = (text or "").strip()
    if not body:
        return
    now = time.monotonic()
    for stale, (ts, _name) in list(_COMMAND_REPLIES.items()):
        if now - ts > _COMMAND_REPLY_TTL:
            _COMMAND_REPLIES.pop(stale, None)
    _COMMAND_REPLIES[body] = (now, str(command or "").strip().lower())


def _command_card(text: str) -> Optional[dict]:
    """命令回复 -> 命令卡；不是命令回复就返回 None（正常卡路径接管）。"""
    hit = _COMMAND_REPLIES.get((text or "").strip())
    if hit is None:
        return None
    ts, command = hit
    if time.monotonic() - ts > _COMMAND_REPLY_TTL:
        return None
    if command and command not in SAFE_QUICK_COMMANDS and command != "card-preset":
        return None
    body = (text or "").strip()
    return hfc_command_result_card(
        title=hfc_command_result_title(command),
        content=body,
        template=hfc_command_result_template(body),
    )


# ── 崩溃遗留卡的收尾（HFC session_store + existing_owner_projection）────────────
def _stale_card(record: dict) -> Optional[dict]:
    """上一个进程留下的「执行中」检查点 → 冻结成收尾卡（schema 2.0，原地更新同一张卡）。

    旧正文保留（`project_existing_owner` 的 legacy 投影），并折叠进「此前内容」面板，
    不留按钮/流式，也不再显示执行态。
    """
    text = str(getattr(record.get("session"), "answer_text", "") or "").strip()
    title = _card_title()
    elements = [{"tag": "markdown", "element_id": "main_content", "content": text or " "}]
    folded = collapsed_transfer_elements(elements)
    if folded is not None:
        elements = folded
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown",
                     "content": "⚠️ 上一个进程在执行中退出，本卡已停止更新。"})
    source = {"schema": "2.0", "header": {"template": "grey",
                                          "title": {"tag": "plain_text", "content": title}},
              "body": {"elements": elements}}
    receipt = static_legacy_receipt(source)
    try:
        result = CardRenderResult(
            card=source, disposition="card", inspection=inspect_card_limits(receipt),
            table_overflow=TableOverflowResult(text=text, source_table_count=0), primary_text=text,
        )
        kept = project_existing_owner(result, receipt=receipt, terminal=True,
                                      live_card=lambda: None, title=lambda: title).card
    except Exception:   # 投影失败也要收尾：退回冻结回执
        kept = receipt
    kept_elements = kept.get("elements") if isinstance(kept, dict) else None
    if not kept_elements:   # 投影返回的是 v2 卡体
        kept_elements = (kept or {}).get("body", {}).get("elements") or elements
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "summary": {"content": title}},
        "header": {"template": "grey",
                   "title": {"tag": "plain_text", "content": f"{title} · 已中断"},
                   "subtitle": {"tag": "plain_text", "content": "上一进程退出，本卡已停止更新"}},
        "body": {"elements": kept_elements},
    }


def _append(row: dict) -> None:
    with _LOCK:
        if not _TURN["chat"] or _TURN["final"]:  # 没有 feishu 回合在跑，或上一回合已收尾
            return
        rows = _TURN["rows"]
        rows.append(row)
        if len(rows) > 200:  # ids/lines only matter for this turn
            del rows[:-200]


def _complete(kind: str, *, name: str, status: str, ms: Any) -> None:
    with _LOCK:
        if _TURN["final"]:
            return
        for row in _TURN["rows"]:
            if row["kind"] == kind and row["status"] == "running" and (not name or row["name"] == name):
                row["status"] = status
                row["ms"] = ms or None
                return


def _on_admission(platform: str = "", source: Any = None, **_kw: Any) -> None:
    """一次入站消息 = 一个回合：重置时间线，并给 HFC 状态机开新 session。"""
    if platform != "feishu":
        return
    chat_id = (source or {}).get("chat_id") if isinstance(source, dict) else None
    with _LOCK:
        _TURN.update(chat=chat_id, rows=[], started=time.monotonic(), final=False)
    if chat_id:
        with _TURNS_LOCK:
            _TURNS.start(chat_id)
        publish_canonical_turn_id(f"feishu:{chat_id}:{int(time.time() * 1000)}")


def _on_pre_tool_call(tool_name: str = "", args: Any = None, tool_call_id: str = "", **_kw: Any) -> None:
    name = tool_name or "tool"
    preview = _preview(name, args)
    _append({"kind": "tool", "name": name, "preview": preview, "status": "running", "ms": None})
    chat = _TURN.get("chat")
    if chat:
        with _TURNS_LOCK:
            _TURNS.tool(chat, tool_id=tool_call_id or name, name=name, status="running", detail=preview)


def _on_post_tool_call(tool_name: str = "", duration_ms: Any = 0, status: str = "",
                       tool_call_id: str = "", **_kw: Any) -> None:
    ok = str(status).lower() not in {"error", "blocked", "cancelled", "canceled"}
    _complete("tool", name=tool_name or "", ms=duration_ms, status="ok" if ok else "error")
    chat = _TURN.get("chat")
    if chat:
        with _TURNS_LOCK:
            _TURNS.tool(chat, tool_id=tool_call_id or (tool_name or "tool"), name=tool_name or "tool",
                        status="completed" if ok else "failed",
                        duration=(duration_ms / 1000) if duration_ms else None)


def _on_subagent_start(child_role: str = "", child_goal: str = "", child_subagent_id: str = "",
                       **_kw: Any) -> None:
    name = f"子代理·{child_role}" if child_role else "子代理"
    goal = _preview("delegate_task", {"goal": child_goal})
    _append({"kind": "agent", "name": name, "preview": goal, "status": "running", "ms": None})
    chat = _TURN.get("chat")
    if chat:
        with _TURNS_LOCK:
            _TURNS.subagent(chat, child_id=child_subagent_id or name, name=name,
                            status="running", detail=goal)


def _on_subagent_stop(child_status: str = "", duration_ms: Any = 0, child_subagent_id: str = "",
                      child_role: str = "", **_kw: Any) -> None:
    ok = str(child_status).lower() in {"", "ok", "done", "completed", "success"}
    _complete("agent", name="", ms=duration_ms, status="ok" if ok else "error")
    chat = _TURN.get("chat")
    if chat:
        name = f"子代理·{child_role}" if child_role else "子代理"
        with _TURNS_LOCK:
            _TURNS.subagent(chat, child_id=child_subagent_id or name, name=name,
                            status="completed" if ok else "failed",
                            duration=(duration_ms / 1000) if duration_ms else None)


def _on_session_end(platform: str = "", **_kw: Any) -> None:
    if platform and platform != "feishu":  # 别的平台（CLI/桌面）结束回合不能冻结飞书的桶
        return
    with _LOCK:
        if not _TURN["chat"]:
            return
        _TURN["final"] = True
        _TURN["ended_at"] = time.monotonic()
        chat = _TURN["chat"]
        elapsed = (_TURN["ended_at"] or 0) - (_TURN.get("started") or 0)
    with _TURNS_LOCK:
        _TURNS.complete(chat, duration=max(0.0, elapsed), **_USAGE)


# 推理流要插件显式订阅 on_stream_delta；正文流已由适配器的 send/edit 喂过，这里只取 reasoning。
# 拿得到推理文本的前提是 Hermes 配置 plugins.stream_reasoning_deltas: true。
_USAGE: dict[str, Any] = {"tokens": {}, "model": "", "provider": "", "context": {}}


def _on_stream_delta(delta: str = "", kind: str = "", **_kw: Any) -> None:
    if kind != "reasoning" or not delta:
        return
    chat = _TURN.get("chat")
    if chat:
        with _TURNS_LOCK:
            _TURNS.thinking(chat, delta)


def _on_post_api_request(usage: Any = None, model: str = "", provider: str = "", **_kw: Any) -> None:
    """HFC 的 footer 要 model/tokens/context；这些只有 API 层知道。"""
    if isinstance(usage, dict):
        tokens = usage.get("tokens") if isinstance(usage.get("tokens"), dict) else usage
        _USAGE["tokens"] = {k: v for k, v in tokens.items() if isinstance(v, (int, float))}
        if isinstance(usage.get("context"), dict):
            _USAGE["context"] = usage["context"]
    if model:
        _USAGE["model"] = str(model)
    if provider:
        _USAGE["provider"] = str(provider)


def _on_agent_loop_stopped(reason: str = "", platform: str = "", **_kw: Any) -> None:
    """被 /stop 或中断打断：卡片必须走失败态，不能一直转圈。"""
    if platform and platform != "feishu":
        return
    chat = _TURN.get("chat")
    if chat:
        with _TURNS_LOCK:
            _TURNS.fail(chat, str(reason or "interrupted"))


def _timeline_text(chat_id: str, *, final: bool) -> str:
    """渲染这个 chat 的回合时间线；空字符串 = 没东西可显示。"""
    with _LOCK:
        if _TURN["chat"] != chat_id or not _TURN["rows"]:
            return ""
        rows = [dict(row) for row in _TURN["rows"]]
        started = _TURN["started"] or time.monotonic()
        ended = _TURN.get("ended_at") or time.monotonic()
    hidden = max(0, len(rows) - TIMELINE_MAX_ROWS)
    lines = [f"_… 前 {hidden} 步已折叠_"] if hidden else []
    for index, row in enumerate(rows[hidden:], hidden + 1):
        parts = [f"{_ICON.get(row['status'], '•')} {index}. {row['name']}"]
        if row.get("preview"):
            parts.append(f"· {row['preview']}")
        if row.get("ms"):
            parts.append(f"· {row['ms'] / 1000:.1f}s")
        lines.append(" ".join(parts))
    if final:
        lines.append(f"✅ 完成 · {len(rows)} 步 · {ended - started:.1f}s")
    return _redact("\n".join(lines))


def merge_segment(segments: list[str], text: str) -> list[str]:
    """答复正文按段合并：同一段的演进一律以最新帧为准，只有真正的新段才追加。

    「演进」= 前缀关系（任一侧）：分帧增长、以及最终帧**去掉流式光标**这种变短都算。
    用长度判断谁长会保留带光标的旧帧，卡片上就永久留一个黑方块。
    """
    if not text:
        return segments
    if not segments:
        return [text]
    last = segments[-1]
    if text.startswith(last) or last.startswith(text):
        return [*segments[:-1], text]
    return [*segments, text]


def looks_like_progress(text: str, known_lines: dict[str, int]) -> bool:
    """整段每一行都是我们自己生成的工具行 = 工具进度气泡的 send/edit。"""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return bool(lines) and all(line in known_lines for line in lines)


def _bundled_feishu():
    """Import the bundled feishu plugin under a private package name.

    The bundled plugin's own relative imports resolve inside this synthetic package, so this
    never collides with the gateway's own ``hermes_plugins.platforms__feishu`` module.
    """
    if _BASE_PACKAGE not in sys.modules:
        import gateway  # the tree that owns gateway/ is also the plugin tree

        root = Path(gateway.__file__).resolve().parent.parent / "plugins" / "platforms" / "feishu"
        spec = importlib.util.spec_from_file_location(
            _BASE_PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[_BASE_PACKAGE] = module
        spec.loader.exec_module(module)
    return importlib.import_module(_BASE_PACKAGE + ".adapter")


def _sdk_models() -> dict[str, Any]:
    """cardkit + PATCH model classes. Older SDKs lack them, so cards degrade to text."""
    from lark_oapi.api.cardkit.v1 import (
        ContentCardElementRequest, ContentCardElementRequestBody,
        CreateCardRequest, CreateCardRequestBody,
        SettingsCardRequest, SettingsCardRequestBody,
        UpdateCardRequest, UpdateCardRequestBody,
    )
    from lark_oapi.api.im.v1 import PatchMessageRequestBody, PatchMessageRequest

    return {
        "create_req": CreateCardRequest, "create_body": CreateCardRequestBody,
        "content_req": ContentCardElementRequest, "content_body": ContentCardElementRequestBody,
        "settings_req": SettingsCardRequest, "settings_body": SettingsCardRequestBody,
        "update_req": UpdateCardRequest, "update_body": UpdateCardRequestBody,
        "patch_req": PatchMessageRequest, "patch_body": PatchMessageRequestBody,
    }


def _build_adapter_class(base: Any) -> type:
    class CardFeishuAdapter(base.FeishuAdapter):
        """Bundled Feishu adapter whose text messages are single, in-place-updated cards."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._card_messages: set[str] = set()      # v1 cards we own (PATCH path)
            self._card_streams: dict[str, dict] = {}   # message_id -> {card_id, element_id: text}
            self._tool_lines: dict[str, int] = {}      # 我们生成的工具行（用于识别进度气泡）
            self._turn_card: dict[str, tuple[float, str]] = {}   # chat_id -> (turn_started, message_id)
            self._segments: dict[str, list[str]] = {}  # message_id -> 答复正文段
            self._dead_remap: dict[str, str] = {}      # 坏卡 message_id -> 顶替它的新消息
            self._cardkit: Optional[CardKitTransport] = None
            self._clarify_state: dict[str, dict[str, str]] = {}   # clarify_id -> 卡片位置
            self._clarify_cards: dict[str, dict[str, Any]] = {}   # clarify_id -> 发出去的那张卡（冻结回执用）

        # ── clarify 按钮卡 ──────────────────────────────────────────────────────
        # 照内置审批卡的线程模型（同步回调 + _submit_on_loop + _card_response）和 _validate_card_action
        # 校验，不另造一套；按钮点击最终落到 tools.clarify_gateway.resolve_gateway_clarify。
        def _clarify_card(self, question: str, choices: list, clarify_id: str) -> dict:
            actions = [
                base._card_button(str(choice), "default",
                                  {"hermes_clarify_action": "choice", "clarify_id": clarify_id,
                                   "clarify_choice": str(choice)})
                for choice in choices
            ]
            actions.append(base._card_button("其他…", "default",
                                             {"hermes_clarify_action": "other", "clarify_id": clarify_id}))
            return base._card(_card_title(), "blue", question, actions=actions)

        async def send_clarify(self, chat_id: str, question: str, choices: Optional[list],
                               clarify_id: str, session_key: str,
                               metadata: Optional[dict[str, Any]] = None):
            """有选项时发按钮卡；无选项（开放式提问）沿用内置的文字拦截。"""
            if not choices:
                return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)
            card = self._clarify_card(question, choices, clarify_id)
            try:
                result = await self._send_interactive_card(
                    chat_id, card, metadata,
                    "send clarify card failed", state_map=self._clarify_state,
                    state_id=clarify_id, session_key=session_key,
                )
                if result.success:
                    self._clarify_cards[clarify_id] = card
                    return result
            except Exception as exc:  # 卡片是外观，提问必须送达
                logger.warning("[feishu-card] clarify card failed, using text: %s", exc)
            return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)

        def _on_card_action_trigger(self, data: Any) -> Any:
            loop = self._loop
            if self._loop_accepts_callbacks(loop):
                action_value = getattr(getattr(getattr(data, "event", None), "action", None), "value", {}) or {}
                if isinstance(action_value, dict) and action_value.get("hermes_clarify_action"):
                    return self._handle_clarify_card_action(
                        event=getattr(data, "event", None), action_value=action_value, loop=loop)
            return super()._on_card_action_trigger(data)

        def _handle_clarify_card_action(self, *, event: Any, action_value: dict[str, Any], loop: Any) -> Any:
            clarify_id = str(action_value.get("clarify_id") or "")
            state = self._clarify_state.get(clarify_id)
            if not state:
                logger.debug("[feishu-card] clarify %s 已解析或未知", clarify_id)
                return self._card_response()
            checked = self._validate_card_action(event=event, state=state, label="clarify", ident=clarify_id)
            if checked is None:
                return self._card_response()
            choice = action_value.get("clarify_choice")
            if choice:
                # HFC 的输入解码层：只接受卡片上真实给出过的选项，防止被篡改成任意值注入
                try:
                    from tools.clarify_gateway import get_pending

                    allowed = [str(c) for c in (getattr(get_pending(clarify_id), "choices", None) or [])]
                    if allowed:
                        choice = parse_choice(
                            {"choice": choice}, {}, mode="single",
                            allowed_values=allowed, allow_custom_input=False,
                        ).value
                except InteractionInputError:
                    logger.warning("[feishu-card] clarify 收到不在选项里的点击，已忽略: %r", choice)
                    return self._card_response()
                except Exception:
                    pass   # 取不到待答项（已超时等）：沿用原值，不因此吞掉点击
            if not self._submit_on_loop(loop, self._resolve_clarify(
                    clarify_id=clarify_id, choice=choice, chat_id=state.get("chat_id", ""))):
                return self._card_response()
            label = f"其他…（请直接回复）" if not choice else str(choice)
            return self._card_response(self._clarify_receipt(clarify_id, label, state))

        def _clarify_receipt(self, clarify_id: str, label: str, state: dict) -> dict:
            """点完的按钮卡冻结成静态回执：只留文字、不留控件。

            `OwnerRefreshState.card_source` 判的是「回执是否该落在另一张消息上」：clarify 卡与
            回合卡是两条消息，所以这里是 separate_receipt —— 回执写回 clarify 卡本身。
            """
            clarify_mid = str(state.get("message_id") or "")
            policy = OwnerRefreshState(
                event_kind="interaction.completed", is_terminal=True,
                owner_message_id=str(self._turn_card_id(state.get("chat_id", "")) or ""),
                interaction_present=True, interaction_status="completed",
                interaction_message_id=clarify_mid,
                has_receipt_fingerprint=False, has_legacy_owner_receipt=False,
            )
            policy.card_source(lambda: "text")   # 记录本机策略判定；clarify 卡恒为独立回执
            source = self._clarify_cards.pop(clarify_id, None) or {}
            elements = [e for e in source.get("elements", []) if e.get("tag") in {"markdown", "hr"}]
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": f"**已选择：** {label}"})
            return static_legacy_receipt({**source, "elements": elements})

        async def _resolve_clarify(self, *, clarify_id: str, choice: Any, chat_id: str) -> None:
            from tools.clarify_gateway import mark_awaiting_text, resolve_gateway_clarify

            self._clarify_state.pop(clarify_id, None)
            if choice:
                resolve_gateway_clarify(clarify_id, str(choice))
            else:
                mark_awaiting_text(clarify_id)

        async def retire_clarify_card(self, clarify_id: str, notice: str = "") -> None:
            """网关在超时/被取代时调：把按钮换成失效提示，别留一张点不动的卡。"""
            state = self._clarify_state.pop(str(clarify_id), None)
            if not state or not state.get("message_id"):
                return
            try:
                # 显式走内置实现：clarify 卡是 v1 卡，不能用我们自己的 schema2 路径改写
                await base.FeishuAdapter.edit_message(
                    self, state["chat_id"], state["message_id"], notice or "该问题已失效，请重新提问。")
            except Exception as exc:
                # 撤按钮失败不该静默：卡上会留下点不动的按钮，要能被看见
                logger.warning("[feishu-card] retire clarify card failed: %s", exc)

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            ok = await super().connect(is_reconnect=is_reconnect)
            if ok and _install_card_frame_repair():
                logger.info("[feishu-card] 已修复 lark-oapi 的 CARD 帧丢弃（卡片按钮回调可达）")
            if ok:
                await self._close_stale_cards()
            return ok

        async def _close_stale_cards(self) -> None:
            """上个进程被杀时留下的「执行中」卡：原地冻结成收尾卡，不留永久转圈。"""
            try:
                records = _TURNS.stale_checkpoints()
            except Exception as exc:
                logger.warning("[feishu-card] stale checkpoint scan failed: %s", exc)
                return
            for record in records:
                card = _stale_card(record)
                if not card:
                    continue
                try:
                    if self._cardkit is not None and await self._cardkit.update(record["message_id"], card):
                        _TURNS.store().remove(record["key"])
                        logger.info("[feishu-card] closed stale card %s", record["message_id"])
                except Exception as exc:
                    logger.warning("[feishu-card] closing stale card failed: %s", exc)

        def _transport(self) -> CardKitTransport:
            """HFC 的 CardKitTransport + 最小 REST 通道；凭据在 connect 后才就绪，故延迟构造。"""
            if self._cardkit is None:
                self._cardkit = CardKitTransport(RestClient(
                    self._app_id, self._app_secret, domain=self._domain_name or "feishu"))
            return self._cardkit

        def _delivery_key(self, chat_id: str) -> str:
            """一个回合一个投递键：回合存活时用 HFC 的 canonical turn id，否则每次新键。

            回合结束后 canonical frame 并未失效，`consume` 仍会返回旧 id；继续拿它当键
            会把新消息绑到已绑定的 cardkit 卡上，飞书回 200780（card binding biz count
            over limit），于是回落 v1 卡，markdown 表格/标题变成裸文本。
            """
            with _LOCK:
                live = bool(_TURN.get("chat")) and not _TURN.get("final")
                turn = consume_canonical_turn_id() if live else None
                if turn:
                    return turn
                if live:
                    started = _TURN.get("started") or 0.0
                    if not started:
                        started = _TURN["started"] = time.monotonic()
                else:
                    started = time.monotonic()
            return f"{chat_id}:{started}"

        # ── 单卡合并（进度气泡 + 答复卡共用一张消息）─────────────────────────────
        def _turn_card_id(self, chat_id: str) -> Optional[str]:
            """本回合属于这个 chat 的合并卡；回合变了就当没有。"""
            entry = self._turn_card.get(chat_id)
            if not entry or entry[0] != _TURN["started"]:
                self._turn_card.pop(chat_id, None)
                return None
            return entry[1]

        def _remember_turn_card(self, chat_id: str, message_id: str) -> None:
            if not message_id:
                return
            self._turn_card[chat_id] = (_TURN["started"], message_id)
            if len(self._segments) > 128:  # ids/segments only matter for this process
                self._segments.clear()

        # ── card shapes ─────────────────────────────────────────────────────────
        def _card_v1(self, chat_id: str, text: str, *, final: bool = False) -> dict:
            timeline = _timeline_text(chat_id, final=final)
            title, template = _header_for_v1(chat_id)
            return base._card(title, template,
                              f"{timeline}\n\n{text}" if timeline else (text or " "))

        def _card_v2(self, chat_id: str, text: str, *, streaming: bool) -> dict:
            """卡片由 HFC 的 `render_card` 生成（显示模式与 HFC 同一份代码）；不可用时退回最小卡。"""
            with _TURNS_LOCK:
                # 回合外的发送（启动通知/cron/命令回复）不是本回合：按 notice 渲染并立即收尾，
                # 否则新建的 session 收不到 complete，卡片永远停在「⏳ 执行中」。
                live = _TURN["chat"] == chat_id and not _TURN["final"]
                _TURNS.answer_frame(chat_id, text, create_notice=not live)
                card = _TURNS.render(chat_id, streaming=streaming)
            if card:
                body_len = next((len(el.get("content", "")) for el in
                                 card.get("body", {}).get("elements", [])
                                 if el.get("element_id") == "main_content"), -1)
                logger.info("[feishu-card] card_v2 text=%d rendered=%d", len(text or ""), body_len)
                return card
            return base._card(_card_title(), "blue", text or " ")

        # ── tool chrome ─────────────────────────────────────────────────────────
        def format_tool_event(self, event: Any, *, mode: str = "all", preview_max_len: int = 40):
            """实时工具进度行：沿用内置格式，只把参数里的敏感内容脱敏，并登记成「已知工具行」。"""
            line = super().format_tool_event(event, mode=mode, preview_max_len=preview_max_len)
            if not line:
                return line
            line = _redact(line)
            if len(self._tool_lines) > 200:
                self._tool_lines.clear()
            self._tool_lines[line] = self._tool_lines.get(line, 0) + 1
            return line

        # ── send ────────────────────────────────────────────────────────────────
        async def send(
            self, chat_id: str, content: str, reply_to: Optional[str] = None,
            metadata: Optional[dict[str, Any]] = None,
        ):
            from gateway.platforms.base import SendResult

            formatted = self.format_message(content)
            # HFC 的两条直通规则：网关的忙/中断/压缩提示不是正文；bindings.native_chats 里的 chat 用原生文本
            if (is_transient_busy_ack(formatted)
                    or transient_notice_recall_seconds(formatted) is not None
                    or _delivery_policy().decide(chat_id).disposition == NATIVE_DISPOSITION):
                return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
            is_progress = MERGE_TOOL_CARD and looks_like_progress(formatted, self._tool_lines)
            logger.info("[feishu-card] send chat=%s len=%d progress=%s known=%d",
                        chat_id, len(formatted), is_progress, len(self._tool_lines))
            # 斜杠命令的反馈不是回答：用 HFC 的命令卡（标题/配色随命令与结果）。
            command_card = None if is_progress else _command_card(formatted)
            if command_card is not None:
                try:
                    response = await self._feishu_send_with_retry(
                        chat_id=chat_id, msg_type="interactive",
                        payload=json.dumps(command_card, ensure_ascii=False),
                        reply_to=reply_to, metadata=metadata,
                    )
                    result = self._finalize_send_result(response, "command card send failed")
                    if result.success:
                        return result
                    logger.warning("[feishu-card] command card rejected, using normal path: %s",
                                   result.error)
                except Exception as exc:
                    logger.warning("[feishu-card] command card failed, using normal path: %s", exc)
            # 回合已收尾（final）就不能再合并：否则回合外的同 chat 消息（定时任务/通知）
            # 会被当成本回合第二次 send，直接改写上一条答复卡。
            if MERGE_TOOL_CARD and _TURN["chat"] == chat_id and not _TURN["final"]:
                existing = self._turn_card_id(chat_id)
                if existing and self._cardkit is not None:
                    # 本回合已有卡：第二次 send 转成对同一张卡的更新（网关会 adopt 返回的 id）
                    try:
                        await self._cardkit.update(
                            existing,
                            self._card_v2(chat_id, "" if is_progress else formatted, streaming=True))
                        return SendResult(success=True, message_id=existing)
                    except Exception as exc:
                        logger.warning("[feishu-card] merged card update failed, sending fresh: %s", exc)
                        self._turn_card.pop(chat_id, None)
            # 进度行只喂 timeline，正文那格留空给答复
            result = await self._send_card(chat_id, "" if is_progress else formatted, content, reply_to, metadata)
            if result is not None and getattr(result, "message_id", ""):
                self._remember_turn_card(chat_id, result.message_id)
                _TURNS.checkpoint(chat_id, result.message_id)
            return result

        async def _send_card(self, chat_id: str, main: str, content: str, reply_to, metadata):
            """三级渲染，逐级回落；None 表示交给内置文本路径。main 是卡片正文（进度卡为空）。"""
            if len(main) <= CARD_MAX_CHARS:
                if STREAMING:
                    try:
                        return await self._send_streaming_card(chat_id, main, reply_to, metadata)
                    except Exception as exc:
                        logger.warning("[feishu-card] cardkit send failed, using v1 card: %s", exc)
                try:
                    result = await self._send_v1_card(chat_id, main, reply_to, metadata)
                    if result is not None:
                        return result
                except Exception as exc:  # presentation must never break delivery
                    logger.warning("[feishu-card] card send failed, using text: %s", exc)
            return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

        async def _send_v1_card(self, chat_id: str, text: str, reply_to, metadata):
            response = await self._feishu_send_with_retry(
                chat_id=chat_id, msg_type="interactive",
                payload=json.dumps(self._card_v1(chat_id, text), ensure_ascii=False),
                reply_to=reply_to, metadata=metadata,
            )
            result = self._finalize_send_result(response, "card send failed")
            if result.success and result.message_id:
                if len(self._card_messages) > 2048:  # ids only matter for this process
                    self._card_messages.clear()
                self._card_messages.add(result.message_id)
                return result
            logger.warning("[feishu-card] card send rejected, using text: %s", result.error)
            return None

        async def _send_streaming_card(self, chat_id: str, text: str, reply_to, metadata):
            """建卡与投递都交给 HFC 的 CardKitTransport：它按 delivery 键做幂等，一张回合一张卡。"""
            key = self._delivery_key(chat_id)
            logger.info("[feishu-card] stream key=%s", key)
            return await self._transport().send(
                chat_id, self._card_v2(chat_id, text, streaming=True),
                delivery_uuid=key, reply_to_message_id=reply_to,
            )

        # ── edit ────────────────────────────────────────────────────────────────
        async def edit_message(
            self, chat_id: str, message_id: str, content: str, *, finalize: bool = False,
        ):
            text = self.format_message(content)
            message_id = self._dead_remap.get(message_id, message_id)  # 上一帧顶替过的新消息
            if (MERGE_TOOL_CARD and message_id == self._turn_card_id(chat_id)
                    and looks_like_progress(text, self._tool_lines)):
                text = ""   # 进度帧只刷时间线：正文由 HFC session 自己保有
            logger.info("[feishu-card] edit mid=%s len=%d finalize=%s blanked=%s",
                        message_id, len(text), finalize, not text)
            if self._cardkit is not None:
                from gateway.platforms.base import SendResult

                try:
                    if await self._cardkit.update(
                        message_id, self._card_v2(chat_id, text, streaming=not finalize)
                    ):
                        _TURNS.checkpoint(chat_id, message_id)
                        return SendResult(success=True, message_id=message_id)
                except Exception as exc:
                    # 卡片更新失败不能让正文丢掉：交回内置编辑/发送路径
                    logger.warning("[feishu-card] cardkit update failed, falling back: %s", exc)
            if message_id in self._card_messages:
                try:
                    models = _sdk_models()
                    request = base._sdk_build(
                        models["patch_req"], message_id=message_id,
                        request_body=base._sdk_build(
                            models["patch_body"],
                            content=json.dumps(
                                self._card_v1(chat_id, text or " ", final=finalize), ensure_ascii=False),
                        ),
                    )
                    response = await self._run_blocking(self._client.im.v1.message.patch, request)
                    result = self._finalize_send_result(response, "card update failed")
                    if result.success:
                        result.message_id = message_id
                        return result
                    logger.warning("[feishu-card] card update rejected, using text: %s", result.error)
                except Exception as exc:
                    logger.warning("[feishu-card] card update failed, using text: %s", exc)
            return await super().edit_message(chat_id, message_id, content, finalize=finalize)

        async def _mutate(self, state: dict, kind: str, fields: dict, element_id: str = "") -> None:
            """一次 cardkit 变更：限速 + sequence + uuid（HFC `cardkit.py::_mutate` 同款）。

            sequence 必须按卡单调递增、uuid 描述这一次变更，否则飞书只回一句 field validation failed。
            """
            delay = MIN_MUTATION_INTERVAL - (time.monotonic() - state["last_mutation"])
            if delay > 0:
                await asyncio.sleep(delay)
            state["seq"] += 1
            state["last_mutation"] = time.monotonic()
            body = dict(fields, sequence=state["seq"],
                        uuid=hashlib.sha256(f"{state['card_id']}:{state['seq']}".encode()).hexdigest()[:40])
            models = _sdk_models()
            if kind == "settings":
                call = self._client.cardkit.v1.card.settings
                request = base._sdk_build(
                    models["settings_req"], card_id=state["card_id"],
                    request_body=base._sdk_build(models["settings_body"], **body))
            elif kind == "card":
                call = self._client.cardkit.v1.card.update
                request = base._sdk_build(
                    models["update_req"], card_id=state["card_id"],
                    request_body=base._sdk_build(models["update_body"], **body))
            else:
                call = self._client.cardkit.v1.card_element.content
                request = base._sdk_build(
                    models["content_req"], card_id=state["card_id"], element_id=element_id,
                    request_body=base._sdk_build(models["content_body"], **body))
            response = await self._run_blocking(call, request)
            if getattr(response, "code", -1) != 0:
                raise RuntimeError(f"cardkit {kind} rejected: {getattr(response, 'msg', '?')}")

        async def _apply_card(self, state: dict, text: str, *, finalize: bool) -> None:
            """推一帧卡片：先关 streaming，再「结构没变只推变了的文本、结构变了整卡 PUT」。"""
            card = self._card_v2(state["chat_id"], text, streaming=not finalize)
            if finalize and state["streaming"]:
                state["streaming"] = False
                # 整卡更新不足以可靠关掉客户端 loading 态，先显式关（HFC 同款顺序）
                await self._mutate(state, "settings", {
                    "settings": json.dumps({"config": {"streaming_mode": False}}, ensure_ascii=False)})
            structure, texts = _text_partition(card)
            now = time.monotonic()
            if (finalize or structure != state["structure"]
                    or now - state["last_full"] >= FULL_UPDATE_INTERVAL):
                await self._mutate(state, "card", {
                    "card": {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)}})
                state["last_full"] = now
                state["structure"] = structure
            else:
                for element_id, content in texts.items():
                    if not content or content == state["texts"].get(element_id):
                        continue
                    await self._mutate(state, "content", {"content": content}, element_id)
            state["texts"] = texts

    return CardFeishuAdapter


_HOOKS = (
    ("post_gateway_admission", _on_admission),
    ("pre_tool_call", _on_pre_tool_call),
    ("post_tool_call", _on_post_tool_call),
    ("subagent_start", _on_subagent_start),
    ("subagent_stop", _on_subagent_stop),
    ("on_session_end", _on_session_end),
    # 推理流（需 plugins.stream_reasoning_deltas）→ HFC 的 thinking.delta
    ("on_stream_delta", _on_stream_delta),
    # 中断/停止 → message.failed
    ("agent_loop_stopped", _on_agent_loop_stopped),
    # footer 的 model / tokens / context
    ("post_api_request", _on_post_api_request),
    ("post_llm_call", _on_post_api_request),
)


# ── /card-preset：切换卡片阅读预设 ────────────────────────────────────────────
# 预设管理的键（来自 READING_PRESETS）：切换时必须从 config.json 删掉，否则残留的显式旧值
# 会覆盖新预设（「切了只生效一半」的坑，见 feishu-streaming-card-ops skill）。
_PRESET_MANAGED_KEYS = frozenset(k for p in READING_PRESETS.values() for k in p)
_PRESET_CHOICES: list = [
    {"value": "classic", "label": "classic", "description": "默认·思考进正文"},
    {"value": "focused", "label": "focused", "description": "思考进面板"},
    {"value": "detailed", "label": "detailed", "description": "时间线展开"},
    {"value": "task", "label": "task", "description": "任务看板"},
]


def _write_preset(preset: str, config_path: Optional[Path] = None) -> str:
    """把 reading_preset 写进 config.json 并清掉预设管理的旧显式键；返回规范化名字。

    非法预设名抛 ValueError（在写盘之前），I/O 失败抛 OSError。
    """
    name = normalize_reading_preset(preset)
    path = config_path or (Path(__file__).parent / "config.json")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    raw_card = cfg.get("card")
    card = dict(raw_card) if isinstance(raw_card, dict) else {}
    for key in _PRESET_MANAGED_KEYS:
        card.pop(key, None)
    card["reading_preset"] = name
    cfg["card"] = card
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return name


def _preset_usage(note: str = "") -> str:
    current = (_TURNS.config or {}).get("reading_preset", "classic")
    head = f"{note}\n" if note else ""
    return (
        f"{head}用法：/card-preset <classic|focused|detailed|task>（当前：{current}）\n"
        "classic=思考进正文 · focused=思考进面板 · detailed=时间线展开 · task=任务看板"
    )


def _on_card_preset(raw_args: str) -> str:
    """``/card-preset [名字]``：无参数弹选择菜单，有参数直接切换。

    写 config.json 后热加载 ``_TURNS.config``——下一条消息即用新预设，无需重启网关
    （命令本身的注册仍需一次重启才出现）。handler 必须自己接住所有异常：抛出去会被
    派发层当成「未处理」，把命令文本转给 agent。
    """
    from hermes_cli.plugin_choices import ask_choice
    arg = (raw_args or "").strip()
    if not arg:
        picked = ask_choice("切换卡片阅读预设", _PRESET_CHOICES)
        if not picked:
            usage = _preset_usage()
            _mark_command_reply("card-preset", usage)
            return usage
        arg = picked
    try:
        name = _write_preset(arg)
    except ValueError as exc:
        return _preset_usage(str(exc))
    except OSError as exc:
        return f"写入 config.json 失败：{exc}"
    with _TURNS_LOCK:
        _TURNS.config = load_card_config()
    reply = f"✅ 卡片预设已切换到 {name}（已写入 config.json，下一条消息生效，无需重启）"
    _mark_command_reply("card-preset", reply)
    return reply


def register(ctx: Any) -> None:
    """Reuse the bundled ``register`` with our subclass swapped in.

    Rebinding the adapter module's ``FeishuAdapter`` global keeps every prompt/env/dashboard
    entry kwarg the bundled plugin declares, instead of duplicating twenty keyword arguments here.
    The hooks only feed the timeline state above; they never send or edit anything.
    """
    base = _bundled_feishu()
    base.FeishuAdapter = _build_adapter_class(base)
    base.register(ctx)
    for hook_name, callback in _HOOKS:
        ctx.register_hook(hook_name, callback)
    ctx.register_command(
        "card-preset", _on_card_preset,
        description="切换飞书卡片阅读预设（classic/focused/detailed/task）",
        args_hint="<classic|focused|detailed|task>",
    )
