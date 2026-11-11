"""运行态诊断与支持报告（借鉴 HFC diagnostics.py / support_report.py）。

排障时的目标：一条命令说清楚「插件现在到底装成什么样」，而不是去 grep 几百万行
网关日志。报告只含计数器、布尔状态与配置差异，不含消息正文、chat_id、token。
"""
from __future__ import annotations

import json
import logging
import platform
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .config_snapshot import LEDGER
from .interaction_receipts import RECEIPTS
from .maintenance import GATE
from .metrics import METRICS
from .observed_rate import RATE

logger = logging.getLogger("hermes_plugins.lark_card_for_hermes")

_SENSITIVE_HINTS = ("token", "secret", "password", "api_key", "apikey", "cookie", "authorization")


#: 核心脱敏器不覆盖的形态：URL 查询串 / ``key: value`` 形式的凭据
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|token|api[_-]?key|apikey|secret|password|passwd"
    r"|authorization|cookie|session[_-]?id)(\s*[:=]\s*)([^\s&,;\"']+)")


def _sanitize(text: str, *, limit: int = 200) -> str:
    """异常文本进卡片/报告前先脱敏再截断：异常里常带完整 URL（含 access_token 参数）。"""
    raw = str(text)
    try:
        from agent.redact import redact_sensitive_text
        raw = redact_sensitive_text(raw, force=True)
    except Exception:                     # 脱敏器不可用时靠下面的本地规则
        pass
    raw = _KEY_VALUE_SECRET_RE.sub(
        lambda m: m.group(0) if m.group(3).startswith("***")
        else f"{m.group(1)}{m.group(2)}***", raw)
    # Local patch 2026-11-11 (port of HFC #462): 结构化脱敏 —— key=value / 带引号值 /
    # Authorization 头 / URL userinfo。异常文本里这四种最常见，且旧的单条正则漏得多。
    try:
        from .display_redaction import redact_diagnostic_text
        raw = redact_diagnostic_text(raw)
    except Exception:
        pass
    return raw if len(raw) <= limit else raw[: limit - 1] + "…"


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
            "error": _sanitize(f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc)),
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
        repair_status = facts.get("card_frame_repair_status")
        if hooks is not None:
            label = {"repaired": "已修补", "already_ok": "SDK 自带", "unsupported": "未识别"}
            shown = label.get(str(repair_status), "on" if repair else "off")
            lines.append(f"· hooks={hooks} · CARD 帧修补={shown}")
        fallbacks = facts.get("private_import_fallbacks") or []
        if fallbacks:
            lines.append("· 私有 import 降级：" + " · ".join(str(f) for f in fallbacks[:3]))

        head("投递（send=尝试数，edit=成功编辑）")
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
                lines.append(f"· {item.get('ts')} {item.get('where')}: {_sanitize(str(item.get('error')), limit=90)}")
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
    if isinstance(value, str):
        # Local patch 2026-11-11: 报告里的每个字符串都过一遍结构化脱敏（key=value /
        # 带引号值 / Authorization / URL userinfo），再做长度截断。
        try:
            from .display_redaction import redact_diagnostic_text
            value = redact_diagnostic_text(value)
        except Exception:
            pass
        return value if len(value) <= 400 else value[:400] + "…"
    return value


DIAG = Diagnostics()
