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
    assert classify("随便一句", previous="另一件完全无关的事情记录") == INDEPENDENT, "无同源证据就是独立卡"
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
    for token in ("运行态", "投递", "交互与维护", "配置"):
        assert token in text, token
    redacted = _redact_tree({"api_key": "sk-secret", "nested": {"token": "t0ken"}, "ok": 1})
    assert redacted["api_key"] == "[REDACTED]" and redacted["nested"]["token"] == "[REDACTED]"
    with tempfile.TemporaryDirectory() as tmp:
        path = diag.write_support_report(facts, directory=Path(tmp))
        assert path.exists(), path
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict) and "metrics" in payload


def check_notice_same_delivery():
    """终稿被 markdown 归一后仍算「同源」，真通知不算——这是 footer 不丢 token 的保险丝。"""
    from hfc.notice_lifecycle import classify, same_delivery, INDEPENDENT

    head = "今天大盘缩量震荡沪指收在三千三百点附近量能比昨日萎缩百分之十二北向资金净流出三十七亿元"
    tail = "明天重点看三千零五十一线支撑以及量能能否放大到八千亿以上"
    streamed = head + "\n\n[工具] 查询完成\n\n" + tail
    final = head + "\n\n" + tail                      # 终稿丢掉了流式里的工具插入段
    assert classify(final, previous=streamed) == INDEPENDENT, "严格判据（相等/互为前缀）本身不应变宽"
    assert same_delivery(final, streamed), "首 40 字相同 = 同源保险丝，避免误判成通知卡"
    assert not same_delivery("💾 记忆已更新：3 条", streamed)
    assert same_delivery("同一段正文", "同一段正文")
    assert not same_delivery(head, "另一件完全无关的事情，长度也够二十个字以上了")


def check_error_sanitised():
    """异常原文进卡片/报告前必须脱敏（异常里常带含 token 的 URL）。"""
    from hfc.diagnostics import Diagnostics

    diag = Diagnostics()
    diag.note_error("send", RuntimeError(
        "HTTP 400 for https://open.feishu.cn/x?access_token=t-abcdef123456&a=1"), code=400)
    item = diag.recent_errors(1)[0]
    assert "t-abcdef123456" not in item["error"], item["error"]
    assert len(item["error"]) <= 200


def check_rate_peak_decays():
    import time as _t
    from hfc.observed_rate import ObservedRate

    r = ObservedRate(window_seconds=0.25)
    r.observe(n=3)
    assert r.peak_per_second() >= 1
    _t.sleep(0.3)
    assert r.peak_per_second() == 0, "空闲超过窗口后峰值必须归零"


def check_state_dir_resolves_symlink():
    """状态目录落在符号链接下（macOS /var → /private/var）也要能落盘。"""
    import shutil, json
    from hfc.bridge import HfcTurnBridge

    tmp = Path(tempfile.mkdtemp(prefix="lcf-state-"))
    bridge = HfcTurnBridge("Hermes", store_dir=Path(tmp).resolve())
    key = "feishu:stateprobe:"
    bridge.start(key)
    bridge.answer_frame(key, "检查点正文")
    bridge.checkpoint(key, "om_state_probe")
    files = list((Path(tmp).resolve() / "card-checkpoints-v1").glob("*.json"))
    assert files, "检查点必须落盘"
    assert json.loads(files[0].read_text())["record"]["message_id"] == "om_state_probe"
    shutil.rmtree(tmp, ignore_errors=True)


def check_turn_lookup_fallback():
    """thread_id 两侧不一致时不能把整回合判成「回合外」，但多候选时不许赌。"""
    from turn_state import TurnRegistry

    reg = TurnRegistry()
    reg.begin(platform="feishu", session_key="s1", chat_id="c1", thread_id="t1", turn_id="a1")
    assert reg.for_chat(platform="feishu", chat_id="c1", thread_id="") is None, "严格匹配本身不应放宽"
    loose = reg.for_chat_loose(platform="feishu", chat_id="c1")
    assert loose is not None and loose.turn_id == "a1", loose

    reg.begin(platform="feishu", session_key="s2", chat_id="c1", thread_id="t2", turn_id="a2")
    assert reg.for_chat_loose(platform="feishu", chat_id="c1") is None, "多候选必须放弃"
    assert reg.for_chat_loose(platform="feishu", chat_id="c9") is None


def check_adopt_delivery():
    """唯一活动回合可被投递侧认领；多回合/过旧不许赌。"""
    from turn_state import TurnRegistry

    reg = TurnRegistry()
    t = reg.begin(platform="feishu", session_key="s1")        # 钩子建回合，chat 未知
    adopted = reg.adopt(platform="feishu", chat_id="c1", thread_id="")
    assert adopted is t and t.chat_id == "c1", "唯一候选应被认领并补上 chat_id"
    assert reg.for_chat(platform="feishu", chat_id="c1", thread_id="") is t, "认领后严格匹配必须命中"

    reg2 = TurnRegistry()
    reg2.begin(platform="feishu", session_key="a")
    reg2.begin(platform="feishu", session_key="b")
    assert reg2.adopt(platform="feishu", chat_id="c1") is None, "多候选必须放弃"

    reg3 = TurnRegistry()
    old = reg3.begin(platform="feishu", session_key="old")
    old.started -= 10000
    assert reg3.adopt(platform="feishu", chat_id="c1", max_age=900) is None, "过旧回合不许认领"


def main() -> int:
    for fn in (check_receipts, check_config_snapshot, check_notice_lifecycle, check_metrics,
               check_maintenance, check_rate, check_diagnostics, check_notice_same_delivery,
               check_error_sanitised, check_rate_peak_decays, check_state_dir_resolves_symlink,
               check_turn_lookup_fallback, check_adopt_delivery):
        fn()
        print(f"ok {fn.__name__}")
    print("ops layer ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
