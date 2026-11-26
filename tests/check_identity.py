#!/usr/bin/env python3
"""Regression: unknown events never acquire the only active Feishu turn."""
from pathlib import Path
import importlib.util
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.environ.get("LCH_HERMES_DIR", str(Path.home() / ".hermes/hermes-agent")))
spec = importlib.util.spec_from_file_location("lark_cards_identity", ROOT / "__init__.py")
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from turn_state import TurnRegistry
from gateway.session_context import set_session_vars, clear_session_vars

r = TurnRegistry()
a = r.begin(platform="feishu", session_key="route-a", chat_id="a")
for kwargs in ({}, {"session_id": "unknown"}, {"turn_id": "unknown"}):
    assert r.resolve(platform="feishu", **kwargs) is None
assert r.resolve(platform="feishu", session_key="route-a", session_id="sa", turn_id="core-a") is a
assert r.resolve(platform="", session_id="sa", turn_id="core-a") is a
assert r.resolve(platform="desktop", turn_id="core-a") is None
b = r.begin(platform="feishu", session_key="route-b", chat_id="b")
assert r.resolve(platform="feishu", session_key="route-b", turn_id="core-a") is None
assert r.resolve(platform="feishu", session_key="route-a", turn_id="different-core") is None
r.end(a)
assert r.resolve(platform="feishu", turn_id="core-a") is None

mod._on_admission(platform="feishu", source={"chat_id": "identity-chat"}, session_key="identity-key")
turn = mod.REGISTRY.for_chat(platform="feishu", chat_id="identity-chat")
# Empty, unknown and explicitly non-Feishu payloads must not append, mutate metrics or end it.
for payload in ({}, {"session_id": "desktop-session"}, {"platform": "desktop"},
                {"turn_id": "unknown"}, {"platform": "cli", "turn_id": turn.turn_id}):
    mod._on_pre_tool_call(tool_name="terminal", args={}, **payload)
    mod._on_subagent_start(child_role="other", **payload)
    mod._on_post_api_request(usage={"input_tokens": 999}, **payload)
    mod._on_stream_delta(delta="unrelated reasoning", kind="reasoning", **payload)
    mod._on_session_end(**payload)
    assert not turn.rows and not turn.final and not turn.usage["tokens"], payload
# Real gateway ContextVars provide the exact route for platform-less tool hook payloads.
tokens = set_session_vars(platform="feishu", session_key="identity-key")
try:
    mod._on_pre_tool_call(tool_name="terminal", args={}, session_id="bound-session", turn_id="bound-core")
    assert len(turn.rows) == 1
finally:
    clear_session_vars(tokens)
mod._on_post_tool_call(tool_name="terminal", session_id="bound-session", turn_id="bound-core")
assert turn.rows[0]["status"] == "ok"
# A new admission revokes old session-only bindings even when the previous turn did not finish.
for finish in (False, True):
    registry = TurnRegistry()
    old = registry.begin(platform="feishu", session_key="same", message_id="old")
    assert registry.resolve(platform="feishu", session_key="same", session_id="s", turn_id="old-core") is old
    if finish:
        registry.end(old)
    new = registry.begin(platform="feishu", session_key="same", message_id="new")
    assert registry.resolve(platform="feishu", session_key="same", session_id="s") is new
    assert registry.resolve(platform="feishu", session_key="same", session_id="s", turn_id="old-core") is None
# A fresh task cannot borrow process-global routing left by another surface.
from contextvars import Context
from unittest.mock import patch
from compat.events import event_route
with patch.dict(os.environ, {"HERMES_SESSION_PLATFORM": "feishu", "HERMES_SESSION_KEY": "identity-key"}):
    assert Context().run(event_route, "", "") == ("", "")
    Context().run(mod._on_pre_tool_call, tool_name="unowned", session_id="foreign")
assert len(turn.rows) == 1
print("OK identity: exact route binds ids; unknown/desktop/CLI/late/stale-env/previous-turn events rejected")
