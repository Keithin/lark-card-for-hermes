"""维护闸门（借鉴 HFC maintenance_* / drain 的意图，收敛成一个开关）。

用途：``hermes update`` 或大改配置的那几十秒里，卡片更新注定半坏；打开维护态后
**跳过流式编辑**（终稿照常投递），避免连续失败把飞书侧打成限流，也避免出现
「更新到一半的卡」。drain 只做一次在途刷新，不改任何投递语义。
"""
from __future__ import annotations

import threading
import time
from typing import Any


class MaintenanceGate:
    def __init__(self) -> None:
        # enter()/exit() 会回调 snapshot()，必须可重入，否则自锁死
        self._lock = threading.RLock()
        self._active = False
        self._reason = ""
        self._since = 0.0
        self._suppressed = 0

    def enter(self, reason: str = "manual") -> dict[str, Any]:
        with self._lock:
            self._active = True
            self._reason = str(reason or "manual")
            self._since = time.time()
            return self.snapshot()

    def exit(self) -> dict[str, Any]:
        with self._lock:
            self._active = False
            self._reason = ""
            self._since = 0.0
            return self.snapshot()

    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def suppress_streaming(self) -> bool:
        """流式编辑是否该跳过；命中时计数一次。"""
        with self._lock:
            if not self._active:
                return False
            self._suppressed += 1
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "reason": self._reason,
                "since_s": round(time.time() - self._since, 1) if self._active else 0.0,
                "suppressed": self._suppressed,
            }


GATE = MaintenanceGate()
