"""有界、无敏感内容的卡片投递计数器（借鉴 HFC metrics.py 的思路，去掉 sidecar 依赖）。

只记计数与速率，不存 payload、不存 chat_id：目的是回答「为什么今天卡慢/卡丢」，
并且能被 /card-status 与支持报告直接读取。
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from typing import Any


class CardMetrics:
    """线程安全的计数器 + 滑动窗口速率。"""

    def __init__(self, *, window_seconds: float = 60.0, max_events: int = 2000) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()
        self._events: deque[tuple[float, str]] = deque(maxlen=max_events)
        self._window = float(window_seconds)
        self._started = time.time()

    def inc(self, name: str, *, n: int = 1, code: Any = None) -> None:
        now = time.time()
        with self._lock:
            self._counters[name] += n
            if code is not None:
                self._errors[str(code)] += n
            for _ in range(min(n, 8)):
                self._events.append((now, name))

    def rate(self, name: str | None = None, *, seconds: float | None = None) -> float:
        window = float(seconds if seconds is not None else self._window)
        if window <= 0:
            return 0.0
        cutoff = time.time() - window
        with self._lock:
            hits = [1 for ts, ev in self._events if ts >= cutoff and (name is None or ev == name)]
        return len(hits) / window

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            errors = dict(self._errors)
            uptime = time.time() - self._started
        return {
            "uptime_s": round(uptime, 1),
            "counters": counters,
            "errors": errors,
            "send_rate_per_s": round(self.rate("send"), 3),
            "edit_rate_per_s": round(self.rate("edit"), 3),
        }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._errors.clear()
            self._events.clear()
            self._started = time.time()


METRICS = CardMetrics()
