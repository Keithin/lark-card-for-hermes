#!/usr/bin/env python3
"""按会话类型/单 chat 投递策略（P1）与诊断脱敏硬化（P2）判据。

覆盖：
1. `ChatDeliveryPolicy`：单 chat 名单 → 会话类型默认 → 全局默认；两个名单重叠即报错；
   profile 模式下仍只认 native_chats；
2. `normalize_chat_overrides`：白名单字段、取值类型、非法字段/取值在加载期报错；
3. bridge 的单 chat 显示覆盖真的生效（会话内固定，其它 chat 不受影响）；
4. `redact_diagnostic_text`：key=value / 带引号值 / Authorization 头 / URL userinfo；
   `Diagnostics._sanitize` 与支持报告 `_redact_tree` 都走了这条脱敏。

只读 + 不联网、不发消息。
用法: ~/.hermes/hermes-agent/venv/bin/python <本文件>
"""
from pathlib import Path

import importlib.util
import json
import sys

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from hfc.bridge import HfcTurnBridge              # noqa: E402
from hfc.config_rules import normalize_chat_overrides  # noqa: E402
from hfc.delivery_policy import ChatDeliveryPolicy     # noqa: E402
from hfc.diagnostics import Diagnostics, _redact_tree  # noqa: E402
from hfc.display_redaction import redact_diagnostic_text  # noqa: E402

GROUP, P2P = "oc_group_1", "oc_p2p_1"


# ── 1. 投递策略优先级 ────────────────────────────────────────────────────────
policy = ChatDeliveryPolicy.from_config({
    "bindings": {
        "default_delivery": {"group": "native", "p2p": "card"},
        "native_chats": ["oc_forced_native"],
        "card_chats": ["oc_exception_card"],
    }
})
assert policy.decide("oc_forced_native", chat_type="group").disposition == "native"
assert policy.decide("oc_exception_card", chat_type="group").disposition == "card"
assert policy.decide("oc_exception_card", chat_type="group").reason == "bindings.card_chats"
assert policy.decide(GROUP, chat_type="group").disposition == "native"
assert policy.decide(GROUP, chat_type="group").reason == "bindings.default_delivery.group"
assert policy.decide(P2P, chat_type="p2p").disposition == "card"
# chat_type 缺失/未知 → 类型默认不生效，回全局默认（card）
assert policy.decide(GROUP, chat_type="").disposition == "card"
assert policy.decide(GROUP, chat_type="thread").disposition == "card"
# 空 chat_id 一律原生（没法判定就保守）
assert policy.decide("").disposition == "native"

scalar = ChatDeliveryPolicy.from_config({"bindings": {"default_delivery": "native"}})
assert scalar.decide(GROUP, chat_type="group").disposition == "native"
assert scalar.decide(P2P, chat_type="p2p").disposition == "native"

try:
    ChatDeliveryPolicy.from_config({"bindings": {"native_chats": ["oc_a"], "card_chats": ["oc_a"]}})
    raise AssertionError("重叠名单没有报错")
except ValueError as exc:
    assert "overlap" in str(exc)
print("OK policy: 单 chat → 类型默认 → 全局默认；重叠名单加载即报错")

# profile 模式：仍只认 profile 内 native_chats
profiles = ChatDeliveryPolicy.from_config({
    "profiles": {"work": {"bindings": {"native_chats": ["oc_work_group"]}}}
})
assert profiles.decide("oc_work_group", chat_type="group", profile_id="work").disposition == "native"
assert profiles.decide(GROUP, chat_type="group", profile_id="work").disposition == "card"
assert profiles.decide(GROUP, profile_id="missing").disposition == "native"
print("OK policy: profile 模式只认各自 native_chats")


# ── 2. 显示覆盖白名单 ───────────────────────────────────────────────────────
clean = normalize_chat_overrides({
    GROUP: {"tool_activity": "summary", "show_reasoning": False,
            "stream_thinking_to_body": False, "hide_completed_tool_activity": True}
})
assert clean[GROUP]["tool_activity"] == "summary" and clean[GROUP]["show_reasoning"] is False
for bad, hint in (
    ({GROUP: {"title": "x"}}, "not overridable"),
    ({GROUP: {"tool_activity": "loud"}}, "full / summary / hidden"),
    ({GROUP: {"show_reasoning": "no"}}, "true or false"),
    ({GROUP: {}}, "non-empty mapping"),
    ({GROUP: "summary"}, "non-empty mapping"),
    ("oc_x", "mapping"),
):
    try:
        normalize_chat_overrides(bad)
        raise AssertionError(f"非法覆盖没有报错: {bad!r}")
    except ValueError as exc:
        assert hint in str(exc), (bad, str(exc))
print("OK overrides: 只接受四个显示字段，非法键/值加载即报错")


# ── 3. bridge：覆盖在会话内生效，不影响其它 chat ─────────────────────────────
def _bridge():
    bridge = HfcTurnBridge.__new__(HfcTurnBridge)
    bridge._sessions, bridge._pushed, bridge._seq = {}, {}, 0
    bridge._order = {}
    bridge.config = {"title": "T", "footer_fields": ["duration", "model"],
                     "tool_activity": "full"}
    return bridge


def _ids(card):
    return {str(el.get("element_id") or "") for el in card.get("body", {}).get("elements", [])}


def _feed_tool(bridge, chat_id, *, overrides=None):
    bridge.start(chat_id, overrides=overrides)
    bridge.answer_frame(chat_id, "正在跑工具")
    bridge.tool(chat_id, tool_id="call_1", name="terminal", status="completed",
                detail="ls -l /tmp", duration=0.2)
    return bridge.render(chat_id, streaming=True)


base = _bridge()
assert any(element_id.startswith("tool_activity") for element_id in _ids(_feed_tool(base, GROUP))), \
    "默认 tool_activity=full 时应显示工具行"

hidden = _bridge()
over_hidden = _feed_tool(hidden, P2P, overrides={"tool_activity": "hidden"})
assert not any(element_id.startswith("tool_activity") for element_id in _ids(over_hidden)), \
    f"tool_activity=hidden 应隐藏实时工具行: {sorted(_ids(over_hidden))}"
# 同一 bridge 里另一个没有覆盖的 chat 不受影响
assert any(element_id.startswith("tool_activity") for element_id in _ids(_feed_tool(hidden, GROUP))), \
    "覆盖串到了别的 chat"
print("OK bridge: 单 chat 覆盖生效且不串到其它会话")


# ── 4. 脱敏 ─────────────────────────────────────────────────────────────────
samples = {
    'api_key = "sk-live-1234567890"': "sk-live-1234567890",
    "Authorization: Bearer abcdef123456": "abcdef123456",
    "url https://user:hunter2@example.com/x": "hunter2",
    "{'password': 'p w secret'}": "p w secret",
    "--token 'v a l u e'": "v a l u e",
}
for raw, secret in samples.items():
    out = redact_diagnostic_text(raw)
    assert secret not in out, f"未脱敏: {raw!r} -> {out!r}"
    assert "REDACTED" in out or "***" in out, f"没有替换标记: {out!r}"

diag = Diagnostics()
diag.note_error("probe", RuntimeError("Authorization: Bearer abcdef123456 failed"))
text = diag.render_text({"cfg": "api_key=sk-live-1234567890"})
assert "abcdef123456" not in text and "sk-live-1234567890" not in text, text
report = _redact_tree({"note": "url https://user:hunter2@h/x", "nested": ["--token 'v a l u e'"]})
assert "hunter2" not in json.dumps(report) and "v a l u e" not in json.dumps(report), report
print("OK redaction: 异常文本与支持报告都过结构化脱敏")

print("ALL PASS policy+redaction")
