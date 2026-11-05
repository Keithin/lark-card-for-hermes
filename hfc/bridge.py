"""HFC 渲染栈的桥：把适配器与 lifecycle hooks 的事实翻成 HFC 的 SidecarEvent 流。

显示与状态机不在本文件里——原封不动用 HFC 的 `session.py`（`CardSession.apply`）和
`render.py`（`render_card`）。这里只负责两件事：
1. 事件翻译：正文累积帧 → `answer.delta`（算增量），工具/子代理/回合边界 → 对应事件；
2. 每个 chat 一个 `CardSession`，跨帧保持 HFC 的时间线、状态、footer 记账。

HFC 原来靠源码 hook + sidecar 投递，这里的事件源是适配器自己和进程内 hooks，所以不再需要
`native_handoff`（抑制原生发送）那套东西。
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from .config_rules import DEFAULT_CONFIG, merge_card_config, normalize_card_config
from .display_segments import display_view
from .events import SidecarEvent
from .render import render_card
from .render_options import card_render_options
from .session import CardSession

SCHEMA_VERSION = "1"

# 每个 chat 一个 CardSession，只按 chat 累积（同一 chat 的下一回合会整条替换）。
# 长跑网关里往来的 chat 会一直涨，所以按 LRU 保留最近这么多个，淘汰最久未用的。
MAX_SESSIONS = 64

# 显示开关照 HFC 的方式：DEFAULT_CONFIG["card"] 打底，用户的
# ~/.hermes/plugins/feishu-card/config.json 覆盖，再走 HFC 的 merge + normalize 校验。
# 只用 HFC schema 里已有的键（native_tables / show_reasoning / timeline_order …），不自造开关。
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load_card_config() -> dict[str, Any]:
    """读 card 配置：HFC 默认 → config.json 覆盖 → merge → 校验，失败退回默认。"""
    base = dict(DEFAULT_CONFIG["card"])
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    user = raw.get("card") if isinstance(raw, dict) and "card" in raw else raw
    card = merge_card_config(base, user if isinstance(user, dict) else {})
    try:
        normalize_card_config(card, path="card")
    except ValueError as exc:
        logger.warning("[feishu-card] %s 校验失败，退回 HFC 默认显示参数: %s", CONFIG_PATH, exc)
        return base
    return card

# 网关给 interim 帧加的流式光标（后缀）。不剥掉就会当成正文留在卡上 —— 用户看到的黑方块。
try:
    from gateway.config import DEFAULT_STREAMING_CURSOR as _CURSOR
except Exception:  # 脱离网关进程（判据脚本）时用同一个默认值
    _CURSOR = " ▉"


def strip_cursor(text: str) -> str:
    """只剥**帧尾**的光标字符串（`cfg.cursor`，默认 " ▉"）。

    网关注入光标的地方只有一处且永远在末尾（`stream_consumer.py:833 display_text += self.cfg.cursor`），
    剥它的地方也只认这一个值、且只剥末尾（`stream_consumer.py:301`、`stream_consumer_fallback.py:46`）。
    所以这里照同样的规则做：正文中间的 ▉ / ▌ / █ 是用户内容，必须原样保留 —— 全文 replace 会误伤。
    """
    if not text or not _CURSOR:
        return text
    # 只有真的剥到光标才返回裁剪后的值；没光标时必须原样返回（否则会吃掉尾部换行）
    out = text
    while True:
        stripped = out.rstrip()
        if not stripped.endswith(_CURSOR):
            return out
        out = stripped[: -len(_CURSOR)]


class HfcTurnBridge:
    """一个 chat 一个 HFC CardSession；正文按累积帧换算成 delta 喂进去。"""

    def __init__(self, title: str = "Hermes") -> None:
        self.title = title
        self.config = load_card_config()
        self._sessions: dict[str, CardSession] = {}
        self._pushed: dict[str, int] = {}   # chat -> 已经喂过的累积正文长度
        self._order: OrderedDict[str, None] = OrderedDict()   # LRU：最近用过的 chat 排最后
        self._seq = 0

    def _touch(self, chat_id: str) -> None:
        """把 chat 标记为最近使用，超过 MAX_SESSIONS 时淘汰最久未用的会话状态。"""
        self._order.pop(chat_id, None)
        self._order[chat_id] = None
        while len(self._order) > MAX_SESSIONS:
            stale, _ = self._order.popitem(last=False)
            self._sessions.pop(stale, None)
            self._pushed.pop(stale, None)

    # ── 事件 ────────────────────────────────────────────────────────────────
    def _apply(self, chat_id: str, name: str, data: dict[str, Any]) -> None:
        session = self._sessions.get(chat_id)
        if session is None:
            return
        self._seq += 1
        session.apply(SidecarEvent(
            schema_version=SCHEMA_VERSION, event=name, conversation_id=chat_id,
            message_id=session.message_id, chat_id=chat_id, platform="feishu",
            sequence=self._seq, created_at=time.time(), data=dict(data),
        ))

    def start(self, chat_id: str, *, reply_to: str = "", delivery_kind: str = "live") -> None:
        """一次入站消息 = 一个回合：新 session，时间线/正文归零。"""
        self._sessions[chat_id] = CardSession(
            conversation_id=chat_id, message_id=f"turn-{chat_id}-{int(time.time() * 1000)}", chat_id=chat_id,
        )
        self._pushed[chat_id] = 0
        self._touch(chat_id)
        self._apply(chat_id, "message.started",
                    {"reply_to_message_id": reply_to, "delivery_kind": delivery_kind})

    def answer_frame(self, chat_id: str, cumulative: str, *, create_notice: bool = False) -> None:
        """适配器拿到的是累积正文（可能带流式光标）；HFC 要的是增量。

        create_notice：回合外发送（通知/命令回复）自动建卡时按 notice 渲染并立即
        置 completed，否则 session 停在 thinking、没有 complete 事件，卡片永远「执行中」。
        """
        if chat_id not in self._sessions:
            self.start(chat_id, delivery_kind="notice" if create_notice else "live")
            if create_notice:
                self.complete(chat_id)
        else:
            self._touch(chat_id)
        text = strip_cursor(cumulative or "")
        # 基准取 session 自己的正文，不用累计长度：HFC 在工具事件后可能重置正文段，
        # 那时 pushed 还停在旧长度，只追加 delta 会把工具之前的内容整段吃掉。
        current = self._sessions[chat_id].answer_text or ""
        if text.startswith(current):
            delta = text[len(current):]
            if delta:
                self._apply(chat_id, "answer.delta", {"text": delta})
                self._pushed[chat_id] = len(text)
                landed = self._sessions[chat_id].answer_text or ""
                if landed != text:            # HFC 没把 delta 接上：以这一帧为准
                    self._sessions[chat_id].answer_text = text
        else:
            # 真回退，或被事件重置：以这一帧为准重建，别把旧尾巴重复追加
            self._sessions[chat_id].answer_text = text
            self._pushed[chat_id] = len(text)

    def tool(self, chat_id: str, *, tool_id: str, name: str, status: str,
             detail: str = "", duration: Optional[float] = None) -> None:
        self._apply(chat_id, "tool.updated", {
            "tool_id": tool_id or name, "name": name, "status": status,
            "detail": detail, "duration": duration,
        })

    def subagent(self, chat_id: str, *, child_id: str, name: str, status: str,
                 detail: str = "", duration: Optional[float] = None) -> None:
        self._apply(chat_id, "subagent.updated", {
            "child_id": child_id or name, "name": name, "status": status,
            "detail": detail, "duration": duration,
        })

    def complete(self, chat_id: str, *, outcome: str = "completed", duration: float = 0.0,
                 tokens: Optional[dict[str, Any]] = None, model: str = "", provider: str = "",
                 context: Optional[dict[str, Any]] = None, rate: Any = None) -> None:
        session = self._sessions.get(chat_id)
        if session is None:
            return
        # model/tokens/context 喂进去，HFC 的 footer（duration·model·tokens·context）才有数据。
        self._apply(chat_id, "message.completed", {
            "answer": session.answer_text, "turn_outcome": outcome, "duration": duration,
            "tokens": tokens or {}, "model": model, "provider": provider,
            "context": context or {}, "rate": rate,
        })

    def thinking(self, chat_id: str, text: str, *, mode: str = "delta") -> None:
        """推理流 → HFC 的 thinking.delta；是不是显示成面板由 render 的配置决定。"""
        if not text:
            return
        self._apply(chat_id, "thinking.delta", {"text": text, "mode": mode})

    def fail(self, chat_id: str, reason: str = "") -> None:
        """中断/失败 → message.failed，卡片走失败态而不是永远转圈。"""
        if chat_id not in self._sessions:
            return
        self._apply(chat_id, "message.failed", {
            "reason": reason, "error": reason, "turn_outcome": "failed",
        })

    # ── 渲染 ────────────────────────────────────────────────────────────────
    def render(self, chat_id: str, *, streaming: bool) -> Optional[dict[str, Any]]:
        """HFC `render_card` 出卡片 JSON —— 参数走 HFC 自己的 `card_render_options` 边界。"""
        session = self._sessions.get(chat_id)
        if session is None:
            return None
        card = dict(render_card(display_view(session), title=self.config["title"],
                                footer_fields=self.config["footer_fields"],
                                **card_render_options(self.config)))
        config = card.setdefault("config", {})
        config["streaming_mode"] = bool(streaming)
        config.setdefault("update_multi", True)
        return card

    def answer_text(self, chat_id: str) -> str:
        session = self._sessions.get(chat_id)
        return session.answer_text if session is not None else ""


__all__ = ["HfcTurnBridge"]
