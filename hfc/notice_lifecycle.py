"""回合外通知的生命周期分类（借鉴 HFC notice_lifecycle.py / notice_producers.py）。

区分两类 ``turn is None`` 投递：
  * ``turn_final``：本回合终稿（正文与刚流式内容同源）→ 复用 session，保留耗时/token。
  * ``independent``：真·回合外通知（自我改进回顾、cron 结果、系统告警）→ 独立 notice。

判定顺序：显式 producer > 已知前缀 > 与上一轮正文的同源比较。
"""
from __future__ import annotations

TURN_FINAL = "turn_final"
INDEPENDENT = "independent"

# 明确属于「回合外生产者」的角标/前缀（命中即独立卡）
INDEPENDENT_PREFIXES: tuple[str, ...] = (
    "💾", "⚠️", "🚨", "🛠", "🧹", "📊", "📈", "🔔", "🧭", "📦",
    "自我改进", "记忆", "定时任务", "任务完成", "系统通知", "后台任务",
)

def _same_delivery(current: str, previous: str) -> bool:
    cur = (current or "").strip()
    prev = (previous or "").strip()
    if not cur or not prev:
        return False
    return cur == prev or cur.startswith(prev) or prev.startswith(cur)


def same_delivery(current: str, previous: str, *, head: int = 40) -> bool:
    """终稿 vs 流式正文的宽松同源判据：相等/互为前缀，或前 ``head`` 字相同。

    只看前 N 字是为了挡住「终稿被 markdown 归一后与流式尾巴不一致」这种误判——
    那会让一张正常的回合卡被当成通知卡，footer 只剩「已完成」。
    """
    if _same_delivery(current, previous):
        return True
    cur = (current or "").strip()
    prev = (previous or "").strip()
    if len(cur) < 20 or len(prev) < 20:
        return False
    return cur[:head] == prev[:head]


def classify(text: str, *, previous: str = "") -> str:
    """返回 ``turn_final`` 或 ``independent``。"""
    if _same_delivery(text, previous):
        return TURN_FINAL
    head = (text or "").strip()[:24]
    for prefix in INDEPENDENT_PREFIXES:
        if head.startswith(prefix):
            return INDEPENDENT
    return INDEPENDENT


def is_independent(text: str, *, previous: str = "") -> bool:
    return classify(text, previous=previous) == INDEPENDENT


def describe(kind: str) -> str:
    return {"turn_final": "本回合终稿", "independent": "回合外通知"}.get(kind, kind)


# ── 通知类别（供 card.chat_overrides.suppressed_notice_kinds 抑制）───────────────
# 与 config_rules.SUPPRESSIBLE_NOTICE_KINDS 一一对应；只认「明确的生产者特征」，
# 认不出来的一律返回空串 = 不可抑制（宁可多发，不可误杀正常回复）。
_NOTICE_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("💾", "self-improvement"),
    ("自我改进", "self-improvement"),
    ("🧠", "memory"),
    ("记忆", "memory"),
    ("更新记忆", "memory"),
    ("⏰", "cron"),
    ("定时任务", "cron"),
    ("定时", "cron"),
    ("🧹", "system"),
    ("系统通知", "system"),
    ("后台任务", "background-task"),
    ("任务完成", "background-task"),
)


def notice_kind_for(text: str) -> str:
    """把通知正文映射成可抑制的类别名；无法判定返回 ``""``。"""
    head = (text or "").strip()[:32]
    if not head:
        return ""
    for prefix, kind in _NOTICE_KIND_PREFIXES:
        if head.startswith(prefix):
            return kind
    return ""
