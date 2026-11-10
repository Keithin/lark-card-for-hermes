#!/usr/bin/env python3
"""判据：运维层（借鉴 HFC 的 metrics / config_snapshot / receipts / lifecycle / maintenance）。

覆盖 7 个新模块 + 2 条命令输出，要求：
  1. 回执幂等：同一 (卡片, 动作) 只放行一次，重复点击被吞并计数；
  2. 配置快照：能指出「运行态 ≠ 磁盘」的具体键；
  3. 通知分类：同源正文=本回合终稿，角标前缀=独立通知；
  4. 计数与错误码：send/edit 计数、HTTP 400 落到 errors；
  5. 维护闸门：enter/exit 不自锁，suppressed 计数正确；
  6. 速率观测：peak 记录到 1s 内的真实峰值；
  7. 支持报告：写出文件且敏感键被 REDACTED。
"""
from pathlib import Path
import json
import sys
import tempfile

REPO_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO_ROOT)

from hfc.config_snapshot import ConfigLedger, diff, flatten, format_diff  # noqa: E402
from hfc.diagnostics import Diagnostics, _redact_tree  # noqa: E402
from hfc.interaction_receipts import ReceiptStore, action_key  # noqa: E402
from hfc.maintenance import MaintenanceGate  # noqa: E402
from hfc.metrics import CardMetrics  # noqa: E402
from hfc.notice_lifecycle import INDEPENDENT, TURN_FINAL, classify  # noqa: E402
from hfc.observed_rate import ObservedRate  # noqa: E402


def check_receipts():
    store = ReceiptStore(ttl=60, max_items=8)
    key = action_key("clarify", "c1", "A")
    assert store.claim(key) is True, "首次点击必须放行"
    assert store.claim(key) is False, "重复点击必须被吞"
    stats = store.stats()
    assert stats["claims"] == 1 and stats["duplicates"] == 1, stats
    assert action_key("a", None, "b") == "a|b", action_key("a", None, "b")


def check_config_snapshot():
    loaded = {"card": {"tool_activity": "full", "max_timeline_items": 12}}
    disk = {"card": {"tool_activity": "summary", "max_timeline_items": 12}}
    entries = diff(loaded, disk)
    assert len(entries) == 1 and entries[0]["key"] == "card.tool_activity", entries
    assert "需重启" in format_diff(entries)
    assert format_diff(diff(loaded, loaded)).startswith("配置：运行态 = 磁盘")
    ledger = ConfigLedger()
    assert ledger.diff_against(disk) == [], "未 capture 时不得报差异"
    ledger.capture(loaded)
    assert len(ledger.diff_against(disk)) == 1
    assert flatten({"a": [1, 2]}) == {"a": [1, 2]}, flatten({"a": [1, 2]})


def check_notice_lifecycle():
    assert classify("结论：不追高。", previous="结论：不追高。") == TURN_FINAL
    assert classify("结论：不追高，等待回踩。", previous="结论：不追高") == TURN_FINAL, "前缀超集也算同源"
    assert classify("💾 自我改进回顾：…") == INDEPENDENT
    assert classify("⚠️ 网关重启") == INDEPENDENT
    assert classify("随便一句", producer="turn") == TURN_FINAL
    assert classify("随便一句", previous="完全不同") == INDEPENDENT


def check_metrics():
    metrics = CardMetrics()
    metrics.inc("send")
    metrics.inc("edit", n=3)
    metrics.inc("card_v1_fallback", code="HTTP 400")
    snap = metrics.snapshot()
    assert snap["counters"]["send"] == 1 and snap["counters"]["edit"] == 3, snap["counters"]
    assert snap["errors"]["HTTP 400"] == 1, snap["errors"]
    assert snap["send_rate_per_s"] > 0, snap


def check_maintenance():
    gate = MaintenanceGate()
    assert gate.is_active() is False
    state = gate.enter("test")
    assert state["active"] is True, state
    assert gate.suppress_streaming() is True
    assert gate.suppress_streaming() is True
    out = gate.exit()
    assert out["active"] is False and out["suppressed"] == 2, out
    assert gate.suppress_streaming() is False


def check_rate():
    rate = ObservedRate(window_seconds=60)
    for _ in range(4):
        rate.observe()
    snap = rate.snapshot()
    assert snap["last_second"] == 4, snap
    assert snap["peak_per_second"] >= 4, snap


def check_diagnostics():
    diag = Diagnostics()
    diag.register("plugin_version", lambda: "1.7.7")
    diag.register("broken", lambda: 1 / 0)
    facts = diag.collect()
    assert facts["plugin_version"] == "1.7.7", facts
    assert str(facts["broken"]).startswith("<probe failed"), facts["broken"]
    text = diag.render_text(facts, disk_config={"card": {"tool_activity": "summary"}})
    for token in ("运行态", "投递计数", "交互与维护", "配置"):
        assert token in text, token
    redacted = _redact_tree({"api_key": "sk-secret", "nested": {"token": "t0ken"}, "ok": 1})
    assert redacted["api_key"] == "[REDACTED]" and redacted["nested"]["token"] == "[REDACTED]"
    with tempfile.TemporaryDirectory() as tmp:
        path = diag.write_support_report(facts, directory=Path(tmp))
        assert path.exists(), path
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict) and "metrics" in payload


def main() -> int:
    for fn in (check_receipts, check_config_snapshot, check_notice_lifecycle, check_metrics,
               check_maintenance, check_rate, check_diagnostics):
        fn()
        print(f"ok {fn.__name__}")
    print("ops layer ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
