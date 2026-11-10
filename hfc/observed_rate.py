"""观测到的实际写入速率（借鉴 HFC observed_rate.py）。

飞书卡片更新有频率上限；这里记录「实际发出的编辑」并给出当前值/峰值，
用来判断是不是撞限流（撞了就会看到 429 / update 返回 False）。
"""
from __future__ import annotations

import threading
import time
from collections import deque


class ObservedRate:
    def __init__(self, *, window_seconds: float = 60.0, max_events: int = 4096) -> None:
        self._lock = threading.Lock()
        self._events: deque[float] = deque(maxlen=max_events)
        self._window = float(window_seconds)
        self._peak = 0.0

    def observe(self, *, n: int = 1) -> float:
        now = time.time()
        with self._lock:
            for _ in range(max(1, n)):
                self._events.append(now)
            cutoff = now - 1.0
            last_second = sum(1 for ts in self._events if ts >= cutoff)
            if last_second > self._peak:
                self._peak = float(last_second)
            cutoff_w = now - self._window
            recent = sum(1 for ts in self._events if ts >= cutoff_w)
        return recent / self._window

    def per_second(self) -> float:
        now = time.time()
        with self._lock:
            cutoff_w = now - self._window
            recent = sum(1 for ts in self._events if ts >= cutoff_w)
        return recent / self._window

    def last_second(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for ts in self._events if ts >= now - 1.0)

    def peak_per_second(self) -> float:
        with self._lock:
            return self._peak

    def snapshot(self) -> dict[str, float | int]:
        return {
            "per_second": round(self.per_second(), 3),
            "last_second": self.last_second(),
            "peak_per_second": round(self.peak_per_second(), 3),
        }


RATE = ObservedRate()
