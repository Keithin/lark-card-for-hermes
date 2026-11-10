"""运行态诊断与支持报告（借鉴 HFC diagnostics.py / support_report.py）。

排障时的目标：一条命令说清楚「插件现在到底装成什么样」，而不是去 grep 几百万行
网关日志。报告只含计数器、布尔状态与配置差异，不含消息正文、chat_id、token。
"""
from __future__ import annotations

import json
import logging
import platform
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .config_snapshot import LEDGER, format_diff
from .interaction_receipts import RECEIPTS
from .maintenance import GATE
from .metrics import METRICS
from .observed_rate import RATE

logger = logging.getLogger("hermes_plugins.lark_card_for_hermes")

_SENSITIVE_HINTS = ("token", "secret", "password", "api_key", "apikey", "cookie", "authorization")


class Diagnostics:
    """注册期收集一堆「读的时候才取值」的探针，/card-doctor 时统一求值。"""

    def __init__(self) -> None:
        self._probes: dict[str, Callable[[], Any]] = {}
        self._errors: deque[dict[str, Any]] = deque(maxlen=12)
        self._started = time.time()

    def register(self, name: str, probe: Callable[[], Any]) -> None:
        self._probes[name] = probe

    def note_error(self, where: str, exc: BaseException | str, *, code: Any = None) -> None:
        self._errors.append({
            "ts": time.strftime("%H:%M:%S"),
            "where": str(where),
            "error": f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc),
            "code": code,
        })
        METRICS.inc("diagnostic_error")

    def recent_errors(self, limit: int = 6) -> list[dict[str, Any]]:
        return list(self._errors)[-limit:]

    def collect(self) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "uptime_s": round(time.time() - self._started, 1),
            "python": platform.python_version(),
            "platform": platform.platform(terse=True),
            "metrics": METRICS.snapshot(),
            "rate": RATE.snapshot(),
            "receipts": RECEIPTS.stats(),
            "maintenance": GATE.snapshot(),
            "recent_errors": self.recent_errors(),
        }
        for name, probe in self._probes.items():
            try:
                facts[name] = probe()
            except Exception as exc:  # 探针坏掉不能拖垮诊断
                facts[name] = f"<probe failed: {type(exc).__name__}: {exc}>"
        return facts

    # ── 文本渲染 ────────────────────────────────────────────────────────────
    def render_text(self, facts: dict[str, Any] | None = None, *, disk_config: dict[str, Any] | None = None) -> str:
        facts = facts or self.collect()
        metrics = facts.get("metrics", {}) or {}
        counters = metrics.get("counters", {}) or {}
        errors = metrics.get("errors", {}) or {}
        rate = facts.get("rate", {}) or {}
        receipts = facts.get("receipts", {}) or {}
        gate = facts.get("maintenance", {}) or {}
        lines: list[str] = []

        def head(title: str) -> None:
            lines.append(f"**{title}**")

        head("运行态")
        version = facts.get("plugin_version") or "unknown"
        lines.append(f"· 插件 v{version} · Python {facts.get('python')} · 已运行 {facts.get('uptime_s')}s")
        hooks = facts.get("hooks")
        repair = facts.get("card_frame_repair")
        if hooks is not None:
            lines.append(f"· hooks={hooks} · CARD 帧修补={'on' if repair else 'off'}")

        head("投递计数")
        order = ("send", "edit", "card_v1_fallback", "text_fallback", "notice_card",
                 "dedupe_hit", "maintenance_skip")
        parts = [f"{k}={counters.get(k, 0)}" for k in order if counters.get(k, 0)]
        lines.append("· " + (" · ".join(parts) if parts else "尚无投递"))
        lines.append(f"· 速率：send {metrics.get('send_rate_per_s', 0)}/s · edit {metrics.get('edit_rate_per_s', 0)}/s"
                     f" · 近 1s 峰值 {rate.get('peak_per_second', 0)}")
        if errors:
            top = " · ".join(f"{k}×{v}" for k, v in sorted(errors.items(), key=lambda kv: -kv[1])[:4])
            lines.append(f"· 接口错误：{top}")

        head("交互与维护")
        lines.append(f"· 回执：放行 {receipts.get('claims', 0)} · 重复点击吞掉 {receipts.get('duplicates', 0)}")
        lines.append("· 维护态：" + ("**开启**（跳过流式编辑）" if gate.get("active")
                                     else "关闭") + f" · 已跳过 {gate.get('suppressed', 0)} 次")

        head("配置")
        if disk_config is not None:
            lines.append(LEDGER.summary(disk_config))
        else:
            lines.append("配置：未持有磁盘副本")

        recent = facts.get("recent_errors") or []
        if recent:
            head("最近错误")
            for item in recent[-3:]:
                lines.append(f"· {item.get('ts')} {item.get('where')}: {str(item.get('error'))[:90]}")
        return "\n".join(lines)

    def write_support_report(self, facts: dict[str, Any] | None = None, *,
                             directory: Path | None = None) -> Path:
        facts = facts or self.collect()
        safe = _redact_tree(facts)
        target_dir = Path(directory) if directory else Path.home() / ".hermes" / "logs"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"lark-card-support-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path


def _redact_tree(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(hint in str(key).lower() for hint in _SENSITIVE_HINTS):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = _redact_tree(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_tree(item) for item in value]
    if isinstance(value, str) and len(value) > 400:
        return value[:400] + "…"
    return value


DIAG = Diagnostics()
