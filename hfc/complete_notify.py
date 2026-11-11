"""完成通知（completion_notify）：门槛判定与文案。

为什么需要单独一段逻辑：飞书卡片是「先发一条消息、再 PATCH 编辑」，**编辑已有消息不会产生新消息**，
既不推送也不亮红点。所以「长任务跑完了叫我」只能靠额外发一条新消息实现。

两个已踩过的坑（上游 #501/#502/#508）：
1. `message.completed` 的 `duration` 有时是 0（续接/被中断的终止路径），直接拿它和门槛比会
   **恰好把最该通知的长任务全部跳过**；因此耗时优先用实测值，没有就回退「现在 - 会话创建时刻」。
2. Hermes v0.23 起事件里不再带发起人的 `open_id`（只有租户 `user_id`）；拿不到 `ou_` 时
   **发不带 @ 的通知**，绝不能让整条通知消失。
"""

from __future__ import annotations

NOTIFY_PLACEMENTS = ("card", "message")
MAX_MIN_DURATION_SECONDS = 86_400


def normalize_min_duration_seconds(
    value: object, *, path: str = "card.completion_notify.min_duration_seconds"
) -> float:
    """0..86400 的数字；`bool` 显式拒绝（True 不等于 1 秒）。"""
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number of seconds")
    seconds = float(value)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise ValueError(f"{path} must be finite")
    if seconds < 0 or seconds > MAX_MIN_DURATION_SECONDS:
        raise ValueError(f"{path} must be within 0..{MAX_MIN_DURATION_SECONDS}")
    return seconds


def elapsed_seconds(
    duration: float, created_at: float, *, now: float | None = None
) -> float:
    """实测耗时优先；`duration` 缺失/为 0 时回退会话存活时长。"""
    measured = float(duration or 0.0)
    if measured > 0:
        return measured
    import time as _time

    current = _time.time() if now is None else float(now)
    return max(0.0, current - float(created_at or current))


def meets_threshold(elapsed: float, min_duration_seconds: float) -> bool:
    """门槛 0 = 每轮都通知（与上游默认、向后兼容）。"""
    return float(min_duration_seconds or 0) <= 0 or float(elapsed) >= float(min_duration_seconds)


def format_elapsed(seconds: float) -> str:
    total = int(max(0.0, float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def build_notify_text(seconds: float, *, open_id: str = "") -> str:
    """`@你 ✅ 本轮回复结束（用时 15m32s）`；拿不到 open_id 就走无 @ 版本。"""
    mention = f'<at user_id="{open_id}"></at> ' if open_id else ""
    return f"{mention}✅ 本轮回复结束（用时 {format_elapsed(seconds)}）"
