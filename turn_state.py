"""显式的每回合注册表（替代进程级 `_TURN` / `_USAGE` 单桶）。

为什么需要它：工具/API hook **不带 chat_id**，而投递只带 chat。用「最近活跃的 chat」当
回合身份，在多 chat、双 adapter、并行工具、压缩换会话时会串轮。这里改成：

- 回合身份 = 入站消息（`post_gateway_admission` 的 platform + session_key + chat/thread），
  每次入站是**新回合**，带自己的 rows / usage / 投递记录；
- 公开 `turn_id`（`on_session_end` 等 hook 提供）到达后与回合关联；工具行按 `tool_call_id`、
  子代理按 `subagent_id` 在回合内更新，不按名字配对；
- 归属不可判定时**不猜**：工具/API 事件只有在「同平台恰好一个活动回合」时才绑定，否则丢弃，
  该回合的正文仍走原生投递（不跨轮合并）。

`shortcut:` Hermes 的 `pre_tool_call` payload 不带 chat/turn，只能按 session_id 或「唯一活动
回合」绑定；上游若在 payload 里补 turn_id/chat_id，直接改 `resolve()` 即可，其余逻辑不动。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_TURNS = 64
TURN_TTL_SECONDS = 6 * 3600


@dataclass
class Turn:
    """一轮的展示状态：（platform, session_key）唯一，turn_id 为公开标识。"""

    platform: str
    session_key: str
    chat_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    started: float = field(default_factory=time.monotonic)
    ended_at: float = 0.0
    final: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=lambda: {"tokens": {}, "model": "", "provider": "", "context": {}})
    adapter_token: str = ""
    session_id: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.platform, self.session_key)

    def elapsed(self) -> float:
        return max(0.0, (self.ended_at or time.monotonic()) - self.started)


class TurnRegistry:
    """线程安全的回合注册表；进程内单例（`REGISTRY`）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_key: dict[tuple[str, str], Turn] = {}
        self._by_turn_id: dict[str, Turn] = {}
        self._by_session: dict[tuple[str, str], Turn] = {}
        self._order: list[tuple[str, str]] = []

    # ── 生命周期 ────────────────────────────────────────────────────────────
    def begin(self, *, platform: str, session_key: str, chat_id: str = "",
              thread_id: str = "", turn_id: str = "") -> Turn:
        """一次入站消息 = 一个新回合；同 key 的旧回合（若还没收尾）被替换。"""
        with self._lock:
            turn = Turn(platform=str(platform or ""), session_key=str(session_key or ""),
                        chat_id=str(chat_id or ""), thread_id=str(thread_id or ""),
                        turn_id=str(turn_id or uuid.uuid4().hex))
            self._by_key[turn.key] = turn
            self._by_turn_id[turn.turn_id] = turn
            self._order.append(turn.key)
            self._evict()
            return turn

    def end(self, turn: Turn) -> None:
        with self._lock:
            turn.final = True
            turn.ended_at = time.monotonic()

    def discard(self, turn: Turn) -> None:
        """投递彻底失败/无主：删掉，避免后续事件再挂到它上面。"""
        with self._lock:
            self._by_key.pop(turn.key, None)
            self._by_turn_id.pop(turn.turn_id, None)
            for key, bound in list(self._by_session.items()):
                if bound is turn:
                    self._by_session.pop(key, None)

    def _evict(self) -> None:
        now = time.monotonic()
        while len(self._order) > MAX_TURNS:
            key = self._order.pop(0)
            turn = self._by_key.get(key)
            if turn is not None and not turn.final:
                continue          # 进行中的回合不淘汰：宁可多留
            self._by_key.pop(key, None)
            for turn_key, bound in list(self._by_turn_id.items()):
                if bound is turn:
                    self._by_turn_id.pop(turn_key, None)
        for key, turn in list(self._by_key.items()):
            if turn.final and now - turn.ended_at > TURN_TTL_SECONDS:
                self._by_key.pop(key, None)

    # ── 查询/绑定 ───────────────────────────────────────────────────────────
    def active(self) -> list[Turn]:
        with self._lock:
            return [t for t in self._by_key.values() if not t.final]

    def by_turn_id(self, turn_id: str) -> Optional[Turn]:
        with self._lock:
            return self._by_turn_id.get(str(turn_id or ""))

    def for_chat(self, *, platform: str, chat_id: str, thread_id: str = "") -> Optional[Turn]:
        """投递侧：这个 chat 是否有进行中的回合（用于判断「本回合」）。"""
        with self._lock:
            for turn in reversed(list(self._by_key.values())):
                if turn.final or turn.platform != platform:
                    continue
                if turn.chat_id == chat_id and (turn.thread_id or "") == (thread_id or ""):
                    return turn
            return None

    def for_chat_loose(self, *, platform: str, chat_id: str) -> Optional[Turn]:
        """严格 (chat, thread) 没命中时的兜底：同 chat 恰好一个活动回合就用它。

        触发场景：回合登记时带了 thread_id，而投递侧 metadata 里没有（或反之），
        严格匹配会判成「回合外」→ 正常回复被当通知卡，footer 丢掉耗时/token。
        只在**唯一**候选时生效；多候选（同 chat 多线程并发）仍返回 None，不赌。
        """
        with self._lock:
            hits = [t for t in self._by_key.values()
                    if not t.final and t.platform == platform and t.chat_id == chat_id]
            return hits[0] if len(hits) == 1 else None

    def adopt(self, *, platform: str, chat_id: str, thread_id: str = "",
              max_age: float = 900.0) -> Optional[Turn]:
        """投递侧认领：**唯一**活动回合时把它和这个 chat 绑起来。

        用途：`post_gateway_admission` 不派发时，回合由 API/工具钩子建立（只带
        session_id，没有 chat_id）。投递侧只有 chat_id，两边靠「本平台只剩一个
        活动回合」对接；一旦认领就补齐 chat_id/thread_id，后续严格匹配即可命中。
        多回合并发 / 回合太旧 → 返回 None，不赌。
        """
        with self._lock:
            now = time.monotonic()
            candidates = [t for t in self._by_key.values()
                          if not t.final and t.platform == platform
                          and now - t.started <= max_age
                          and (not t.chat_id or t.chat_id == chat_id)]
            if len(candidates) != 1:
                return None
            turn = candidates[0]
            if not turn.chat_id:
                turn.chat_id = str(chat_id or "")
            if thread_id and not turn.thread_id:
                turn.thread_id = str(thread_id or "")
            return turn

    def bind_adapter(self, turn: Turn, adapter_token: str) -> bool:
        """第一个给这个 chat 投递的 adapter 拥有该回合；别的实例不接管。"""
        with self._lock:
            if not turn.adapter_token:
                turn.adapter_token = str(adapter_token or "")
                return True
            return turn.adapter_token == str(adapter_token or "")

    def resolve(self, *, platform: str, session_id: str = "", turn_id: str = "") -> Optional[Turn]:
        """工具/API/子代理事件 → 回合。

        优先 turn_id；其次已绑定的 session_id；最后只在「同平台恰好一个活动回合」时绑定。
        多候选/无候选都返回 None（调用方丢弃该事件，不跨轮合并）。
        """
        with self._lock:
            if turn_id:
                turn = self._by_turn_id.get(str(turn_id))
                if turn is not None:
                    if session_id and not turn.session_id:
                        turn.session_id = str(session_id)
                        self._by_session[(turn.platform, turn.session_id)] = turn
                    return turn
            if session_id:
                bound = self._by_session.get((str(platform or ""), str(session_id)))
                if bound is not None and not bound.final:
                    return bound
            candidates = [t for t in self._by_key.values()
                          if not t.final and (not platform or t.platform == str(platform))]
            if len(candidates) != 1:
                return None
            turn = candidates[0]
            if session_id:
                turn.session_id = str(session_id)
                self._by_session[(turn.platform, turn.session_id)] = turn
            return turn

    # ── 内容 ────────────────────────────────────────────────────────────────
    def append_row(self, turn: Turn, row: dict[str, Any]) -> None:
        with self._lock:
            if turn.final:
                return
            turn.rows.append(row)
            if len(turn.rows) > 200:
                del turn.rows[:-200]

    def complete_row(self, turn: Turn, *, kind: str, ident: str, status: str,
                     ms: Any = None, name: str = "") -> None:
        """按 id 收尾（工具用 tool_call_id，子代理用 subagent_id），不按名字配对。"""
        with self._lock:
            if turn.final:
                return
            for row in turn.rows:
                if row.get("kind") != kind or row.get("status") != "running":
                    continue
                if row.get("id") and ident:
                    if row["id"] != ident:
                        continue
                elif name and row.get("name") != name:
                    continue
                row["status"] = status
                row["ms"] = ms or None
                return

    def set_usage(self, turn: Turn, **usage: Any) -> None:
        with self._lock:
            if isinstance(usage.get("tokens"), dict):
                turn.usage["tokens"] = {k: v for k, v in usage["tokens"].items()
                                        if isinstance(v, (int, float))}
            if isinstance(usage.get("context"), dict):
                turn.usage["context"] = usage["context"]
            if usage.get("model"):
                turn.usage["model"] = str(usage["model"])
            if usage.get("provider"):
                turn.usage["provider"] = str(usage["provider"])


REGISTRY = TurnRegistry()
