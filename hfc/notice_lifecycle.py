"""回合外通知的生命周期分类（借鉴 HFC notice_lifecycle.py / notice_producers.py）。

区分两类 ``turn is None`` 投递：
  * ``turn_final``：本回合终稿（正文与刚流式内容同源）→ 复用 session，保留耗时/token。
  * ``independent``：真·回合外通知（自我改进回顾、cron 结果、系统告警）→ 独立 notice。

判定顺序：显式 producer > 已知前缀 > 与上一轮正文的同源比较。
"""
from __future__ import annotations

from typing import Iterable

TURN_FINAL = "turn_final"
INDEPENDENT = "independent"

# 明确属于「回合外生产者」的角标/前缀（命中即独立卡）
INDEPENDENT_PREFIXES: tuple[str, ...] = (
    "💾", "⚠️", "🚨", "🛠", "🧹", "📊", "📈", "🔔", "🧭", "📦",
    "自我改进", "记忆", "定时任务", "任务完成", "系统通知", "后台任务",
)

# 明确属于「本回合」的 producer 名
TURN_PRODUCERS = {"turn", "reply", "final", "stream", "assistant"}


def _same_delivery(current: str, previous: str) -> bool:
    cur = (current or "").strip()
    prev = (previous or "").strip()
    if not cur or not prev:
        return False
    return cur == prev or cur.startswith(prev) or prev.startswith(cur)


def classify(text: str, *, previous: str = "", producer: str | None = None) -> str:
    """返回 ``turn_final`` 或 ``independent``。"""
    if producer and producer.strip().lower() in TURN_PRODUCERS:
        return TURN_FINAL
    if _same_delivery(text, previous):
        return TURN_FINAL
    head = (text or "").strip()[:24]
    for prefix in INDEPENDENT_PREFIXES:
        if head.startswith(prefix):
            return INDEPENDENT
    return INDEPENDENT


def is_independent(text: str, *, previous: str = "", producer: str | None = None) -> bool:
    return classify(text, previous=previous, producer=producer) == INDEPENDENT


def describe(kind: str) -> str:
    return {"turn_final": "本回合终稿", "independent": "回合外通知"}.get(kind, kind)
