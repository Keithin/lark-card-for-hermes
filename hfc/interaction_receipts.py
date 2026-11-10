"""交互回执：按钮点击的幂等闸门（借鉴 HFC interaction_receipts.py）。

飞书卡片按钮可能被重复投递（连点/回调重试）；同一个 (card, action) 只允许
第一次真正提交，后续点击直接被吞掉并计数。这里只存键与时间戳，不存答案正文，
也不做任何授权判断——授权仍由网关的 clarify/approval 逻辑负责。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class ReceiptStore:
    def __init__(self, *, ttl: float = 120.0, max_items: int = 512) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, float] = OrderedDict()
        self._ttl = float(ttl)
        self._max = int(max_items)
        self._hits = 0
        self._claims = 0

    def _prune(self, now: float) -> None:
        while self._items:
            key, ts = next(iter(self._items.items()))
            if now - ts > self._ttl:
                self._items.popitem(last=False)
            else:
                break
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def claim(self, key: str) -> bool:
        """第一次 claim 返回 True（放行），重复 claim 返回 False（吞掉）。"""
        now = time.time()
        with self._lock:
            self._prune(now)
            if key in self._items:
                self._hits += 1
                return False
            self._items[key] = now
            self._claims += 1
            return True

    def forget(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"claims": self._claims, "duplicates": self._hits, "tracked": len(self._items)}


RECEIPTS = ReceiptStore()


def action_key(*parts: object) -> str:
    return "|".join(str(p) for p in parts if p is not None)
