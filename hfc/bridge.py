"""HFC 渲染栈的桥：把适配器与 lifecycle hooks 的事实翻成 HFC 的 SidecarEvent 流。

显示与状态机不在本文件里——原封不动用 HFC 的 `session.py`（`CardSession.apply`）和
`render.py`（`render_card`）。这里只负责两件事：
1. 事件翻译：正文累积帧 → `answer.delta`（算增量），工具/子代理/回合边界 → 对应事件；
2. 每个 chat 一个 `CardSession`，跨帧保持 HFC 的时间线、状态、footer 记账。

HFC 原来靠源码 hook + sidecar 投递，这里的事件源是适配器自己和进程内 hooks，所以不再需要
`native_handoff`（抑制原生发送）那套东西。
"""

from __future__ import annotations

import os

import json
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from .config_rules import DEFAULT_CONFIG, merge_card_config, normalize_card_config
from .display_segments import display_view
from .events import SidecarEvent
from .render import render_card_result
from .render_options import card_render_options
from .session import CardSession
from .session_store import SessionStore

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"

# 卡片状态检查点：崩溃/重启后能把它们收尾，而不是留在「执行中」。
#
# 放在 HERMES_HOME 下而不是插件目录里，有两个原因：
#   1. 插件目录可能被 `git clean -fdx` / 重装 / `hermes plugins update` 整体替换，
#      运行时检查点不该跟着消失（否则网关崩了以后再没人收尾那张卡）；
#   2. 检查点里含正文快照，`hermes plugins validate` 的安全扫描会把它们当成插件内容
#      报 `destructive_home_rm` / `dump_all_env` 并判失败。
# 可用 LARK_CARD_STATE_DIR 覆盖（测试/多实例用）。
def _state_root() -> Path:
    override = os.environ.get("LARK_CARD_STATE_DIR")
    if override:
        # resolve()：macOS 的 /var → /private/var 之类符号链接路径会被私有目录守卫拒绝，
        # 归一化后再用，避免「静默不落盘 + 每 2 秒一条 warning」
        return Path(override).expanduser().resolve()
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return (Path(home).expanduser() / "state" / "lark-card-for-hermes").resolve()


STATE_DIR = _state_root()
CHECKPOINT_INTERVAL = 2.0   # 秒；每帧都落盘等于每帧一次磁盘写
STALE_AFTER = 600.0         # 秒；比这更老的「执行中」检查点一定是上一个进程留下的

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


NOTICE_TITLE_CHARS = 24


def notice_title(text: str) -> str:
    """回合外通知的卡片标题：正文首行去掉 markdown 记号，超长截断。"""
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("#>-*· ").strip()
        if not line:
            continue
        return line if len(line) <= NOTICE_TITLE_CHARS else line[:NOTICE_TITLE_CHARS] + "…"
    return ""


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

    def __init__(self, title: str = "Hermes", *, store_dir: Optional[Path] = None) -> None:
        self.title = title
        self.config = load_card_config()
        self._sessions: dict[str, CardSession] = {}
        self._pushed: dict[str, int] = {}   # chat -> 已经喂过的累积正文长度
        # 单 chat 的显示覆盖（上游 #492 的 card.chat_overrides）：会话创建时固定，
        # 只允许显示类字段，进行中的卡片不中途改外观。
        self._overrides: dict[str, dict[str, Any]] = {}
        self._order: OrderedDict[str, None] = OrderedDict()   # LRU：最近用过的 chat 排最后
        self._seq = 0
        self._store_dir = Path(store_dir) if store_dir is not None else STATE_DIR
        self._store: Optional[SessionStore] = None
        self._checkpointed: dict[str, float] = {}   # chat -> 上次落盘的时刻

    @property
    def _disposition_map(self) -> dict[str, str]:
        """chat -> 最近一次渲染的处置（card / native / deferred_native）。"""
        mapping = getattr(self, "_dispositions", None)
        if mapping is None:
            mapping = {}
            self._dispositions = mapping
        return mapping

    def last_disposition(self, chat_id: str) -> str:
        """最近一次渲染是否还装得下：`native` = 卡片装不下，正文必须走原生文本。"""
        return self._disposition_map.get(str(chat_id), "card")

    @property
    def _chat_overrides_map(self) -> dict[str, dict[str, Any]]:
        """延迟初始化：测试/检查脚本会用 __new__ 造实例并手工塞属性。"""
        mapping = getattr(self, "_overrides", None)
        if mapping is None:
            mapping = {}
            self._overrides = mapping
        return mapping

    def _touch(self, chat_id: str) -> None:
        """把 chat 标记为最近使用，超过 MAX_SESSIONS 时淘汰最久未用的会话状态。"""
        self._order.pop(chat_id, None)
        self._order[chat_id] = None
        while len(self._order) > MAX_SESSIONS:
            stale, _ = self._order.popitem(last=False)
            self._sessions.pop(stale, None)
            self._pushed.pop(stale, None)

    # ── 检查点（HFC 的 session_store）────────────────────────────────────────
    def store(self) -> SessionStore:
        """懒建：目录只在真的要落盘时才创建，判据脚本里不碰磁盘。"""
        if self._store is None:
            self._store = SessionStore(self._store_dir)
        return self._store

    def checkpoint(self, chat_id: str, message_id: str) -> None:
        """把这个回合的卡片状态落盘（限流）。没有它，进程一挂，卡就永远停在执行中。"""
        session = self._sessions.get(chat_id)
        if not session or not message_id or session.delivery_kind != "chat":
            return
        now = time.monotonic()
        if now - self._checkpointed.get(chat_id, 0.0) < CHECKPOINT_INTERVAL:
            return
        try:
            self.store().save(session.message_id, session, message_id, "", "", [], "")
            self._checkpointed[chat_id] = now
        except Exception as exc:   # 检查点只是兜底，不能影响投递
            logger.warning("[feishu-card] checkpoint save failed: %s", exc)

    def drop_checkpoint(self, chat_id: str) -> None:
        """回合正常收尾：检查点已无用，删掉免得下次启动再收尾一次。"""
        session = self._sessions.get(chat_id)
        getattr(self, "_checkpointed", {}).pop(chat_id, None)
        if session is None or getattr(self, "_store", None) is None:
            return   # 没落过盘就没有要删的记录（判据脚本里 bridge 可能没初始化存储）
        try:
            self.store().remove(session.message_id)
        except Exception as exc:
            logger.warning("[feishu-card] checkpoint remove failed: %s", exc)

    def stale_checkpoints(self, *, older_than: float = STALE_AFTER) -> list[dict[str, Any]]:
        """上一个进程留下的、还停在「执行中」的卡片（进程被杀时无法收尾的那些）。"""
        try:
            records = self.store().load()
        except Exception as exc:
            logger.warning("[feishu-card] checkpoint load failed: %s", exc)
            return []
        cutoff = time.time() - older_than
        # load() 已经把 session 还原成 CardSession 对象，这里按属性读，不按 dict 读。
        return [r for r in records
                if r.get("message_id")
                and float(r.get("saved_at") or 0) < cutoff
                and getattr(r.get("session"), "status", "") == "thinking"]

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

    def start(self, chat_id: str, *, reply_to: str = "", delivery_kind: str = "chat",
              overrides: Optional[dict[str, Any]] = None) -> None:
        """一次入站消息 = 一个回合：新 session，时间线/正文归零。"""
        self._chat_overrides_map[chat_id] = dict(overrides or {})
        self._sessions[chat_id] = CardSession(
            conversation_id=chat_id, message_id=f"turn-{chat_id}-{int(time.time() * 1000)}", chat_id=chat_id,
        )
        self._pushed[chat_id] = 0
        self._touch(chat_id)
        self._apply(chat_id, "message.started",
                    {"reply_to_message_id": reply_to, "delivery_kind": delivery_kind})

    def answer_frame(self, chat_id: str, cumulative: str, *, create_notice: bool = False,
                     fresh: bool = False) -> None:
        """适配器拿到的是累积正文（可能带流式光标）；HFC 要的是增量。

        create_notice：回合外发送（通知/命令回复）自动建卡时按 notice 渲染并立即
        置 completed，否则 session 停在 thinking、没有 complete 事件，卡片永远「执行中」。

        fresh：真正回合外的投递（turn is None）必须开新 session。否则同一 chat 键上
        还留着的上一回 session 会被复用，通知卡于是继承了那一轮的 duration / tokens /
        时间线，读起来像「同一条回复发了两遍」。回合内的收尾发送（turn.final）仍复用，
        这样终稿落在同一张卡上，不会变两张。
        """
        if chat_id not in self._sessions:
            self.start(chat_id, delivery_kind="notice" if create_notice else "chat")
            if create_notice:
                # HFC 的 notice 卡标题来自事件自带的 title；这里没有事件源，取正文首行，
                # 否则回合外通知永远顶着配置名（用户看到的「标题都是 Hermes」）。
                self._sessions[chat_id].notice_title = notice_title(cumulative)
                self.complete(chat_id)
        elif fresh and create_notice:
            # 调用方已判定这是“真·回合外通知”（正文与本回合无关），开新 session，
            # 避免继承上一轮的 duration / tokens / 时间线。
            self.start(chat_id, delivery_kind="notice")
            self._sessions[chat_id].notice_title = notice_title(cumulative)
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
        self.drop_checkpoint(chat_id)
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

    def note(self, chat_id: str, text: str) -> None:
        """给当前 session 的正文追加一段说明（封口旧回合时用，对齐 HFC 的替代文案）。"""
        session = self._sessions.get(chat_id)
        if session is None or not text:
            return
        body = (session.answer_text or "").rstrip()
        session.answer_text = f"{body}\n\n{text}" if body else text

    def fail(self, chat_id: str, reason: str = "") -> None:
        """中断/失败 → message.failed，卡片走失败态而不是永远转圈。"""
        if chat_id not in self._sessions:
            return
        self.drop_checkpoint(chat_id)
        self._apply(chat_id, "message.failed", {
            "reason": reason, "error": reason, "turn_outcome": "failed",
        })

    # ── 渲染 ────────────────────────────────────────────────────────────────
    def _config_for(self, chat_id: str) -> dict[str, Any]:
        """这个会话生效的卡片配置：全局配置 + 单 chat 显示覆盖（仅白名单字段）。"""
        overrides = self._chat_overrides_map.get(chat_id)
        if not overrides:
            return self.config
        effective = dict(self.config)
        effective.update(overrides)
        return effective

    def render(self, chat_id: str, *, streaming: bool) -> Optional[dict[str, Any]]:
        """HFC `render_card` 出卡片 JSON —— 参数走 HFC 自己的 `card_render_options` 边界。"""
        session = self._sessions.get(chat_id)
        if session is None:
            return None
        config = self._config_for(chat_id)
        result = render_card_result(display_view(session), title=config["title"],
                                    footer_fields=config["footer_fields"],
                                    **card_render_options(config))
        # 超限处置必须传出去：只看 card 会把「装不下」错当成普通卡片，
        # 结果用一张 400 字节的占位卡换掉整篇正文。
        self._disposition_map[str(chat_id)] = str(result.disposition)
        card = dict(result.card)
        config = card.setdefault("config", {})
        config["streaming_mode"] = bool(streaming)
        config.setdefault("update_multi", True)
        return card

    def answer_text(self, chat_id: str) -> str:
        session = self._sessions.get(chat_id)
        return session.answer_text if session is not None else ""

    def recent(self, chat_id: str, *, seconds: float = 180.0) -> bool:
        """这个键上的 session 是否刚刚活动过（用于分辨本回合终稿 / 真·回合外通知）。"""
        session = self._sessions.get(chat_id)
        if session is None:
            return False
        try:
            updated = float(getattr(session, "updated_at", 0) or 0)
        except (TypeError, ValueError):
            return False
        return (time.time() - updated) <= seconds


__all__ = ["HfcTurnBridge"]
