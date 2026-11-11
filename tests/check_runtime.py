#!/usr/bin/env python3
"""lark-cards 运行时构造自检：真正 import gateway + 内置 feishu 插件，构造子类实例，
验证 send/edit 路径的方法都在、卡片能造出来、工具进度行会脱敏。不联网、不发消息。

用法: cd ~/.hermes/hermes-agent && ./venv/bin/python <本文件>
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])

import importlib.util
import json
import sys
from pathlib import Path

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

from gateway.config import PlatformConfig  # noqa: E402
from gateway.stream_events import ToolCallChunk  # noqa: E402


spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

base = mod._bundled_feishu()
cls = mod._build_adapter_class(base)
adapter = cls(PlatformConfig(enabled=True))

CHAT = "oc_runtime_selftest"
from turn_state import REGISTRY  # noqa: E402

mod._on_admission(platform="feishu", source={"chat_id": CHAT},
                  session_key="agent:main:feishu:dm:oc_runtime_selftest")
TURN = REGISTRY.active()[-1]
mod._on_pre_tool_call(tool_name="terminal", args={"command": "echo hi"}, tool_call_id="t1")
mod._on_post_tool_call(tool_name="terminal", duration_ms=1200, status="ok", tool_call_id="t1")
mod._on_session_end(platform="feishu", session_key="agent:main:feishu:dm:oc_runtime_selftest")

card = adapter._card_v2(CHAT, "正文", streaming=True, turn=TURN)
json.dumps(card)  # 必须能被卡片 JSON 序列化
text = adapter.format_message("正文")
assert text == "正文", text

# 工具进度行：走内置格式，但凭据必须被打码
secret = "sk-live-abcdef0123456789abcdef"
event = ToolCallChunk(tool_name="terminal", preview=f"curl -H 'Authorization: Bearer {secret}'", args={})
line = adapter.format_tool_event(event, mode="all", preview_max_len=80)
assert line and secret not in line, line

v1_body = adapter._card_v1(CHAT, "正文", turn=TURN)["elements"][0]["content"]
assert "1. terminal" in v1_body and "完成" not in v1_body, v1_body
assert "完成" in adapter._card_v1(CHAT, "正文", final=True, turn=TURN)["elements"][0]["content"]

print("OK runtime: adapter builds, card JSON serializes, tool progress line redacted")


# ── 回合封口：adapter 真的能把终态卡推到旧消息上（不联网，stub transport）────────
import asyncio  # noqa: E402


class _StubCardkit:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict]] = []

    async def update(self, message_id: str, card: dict) -> bool:
        self.updates.append((message_id, card))
        return True


seal_adapter = cls(PlatformConfig(enabled=True))
seal_adapter._cardkit = _StubCardkit()
seal_adapter._turn_card[CHAT] = ("turn-old", "om_old_msg")
sealed_card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "已停止"}]}}
asyncio.run(seal_adapter.seal_superseded_card(CHAT, "turn-old", sealed_card))
assert seal_adapter._cardkit.updates == [("om_old_msg", sealed_card)], seal_adapter._cardkit.updates
assert CHAT not in seal_adapter._turn_card, "封口后没清掉回合卡记录"
# 回合不匹配 → 什么都不做（别改别人的卡）
seal_adapter._turn_card[CHAT] = ("turn-new", "om_new_msg")
asyncio.run(seal_adapter.seal_superseded_card(CHAT, "turn-old", sealed_card))
assert len(seal_adapter._cardkit.updates) == 1, "回合 id 不匹配时不该写卡"
assert seal_adapter._turn_card[CHAT] == ("turn-new", "om_new_msg")
print("OK runtime: seal_superseded_card 只改属于该回合的旧卡")


# ── reply 锚：新卡要挂到触发它的那条用户消息上 ────────────────────────────────
from types import SimpleNamespace as _NS  # noqa: E402

anchor_adapter = cls(PlatformConfig(enabled=True))
_anchor_turn = _NS(turn_id="turn-anchor", trigger_message_id="om_trigger", thread_id="")
assert anchor_adapter._reply_anchor(None, _anchor_turn) == "om_trigger", "未挂 reply 锚"
assert anchor_adapter._reply_anchor("om_quoted", _anchor_turn) == "om_quoted", "显式 reply_to 应优先"
assert anchor_adapter._reply_anchor(None, None) is None, "回合外的通知卡不该锚定"
assert anchor_adapter._reply_anchor(None, _NS(turn_id="t", trigger_message_id="", thread_id="")) is None
import os as _os  # noqa: E402

_os.environ["LARK_CARD_REPLY_ANCHOR"] = "0"
assert anchor_adapter._reply_anchor(None, _anchor_turn) is None, "开关关闭后仍锚定"
del _os.environ["LARK_CARD_REPLY_ANCHOR"]
print("OK runtime: reply 锚只在有触发消息时生效，显式 reply_to 优先，可开关关闭")


# ── reply 锚真的进了投递层（不是只算出来）─────────────────────────────────────
class _StubTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, chat_id, card, *, delivery_uuid=None, reply_to_message_id=None):
        self.sent.append({"chat_id": chat_id, "uuid": delivery_uuid,
                          "reply_to": reply_to_message_id, "card": card})
        return _NS(success=True, message_id="om_card")


tx = _StubTransport()
tx_adapter = cls(PlatformConfig(enabled=True))
tx_adapter._cardkit = tx
asyncio.run(tx_adapter._send_streaming_card(CHAT, "正文", "om_trigger", {}, turn=None))
assert tx.sent and tx.sent[0]["reply_to"] == "om_trigger", tx.sent
print("OK runtime: _send_streaming_card 把 reply 锚传给了 transport")


# ── reply 锚失败不能让卡片发不出去：自动降级为无锚重试一次 ────────────────────
class _FlakyTransport:
    def __init__(self) -> None:
        self.calls: list = []

    async def send(self, chat_id, card, *, delivery_uuid=None, reply_to_message_id=None):
        self.calls.append(reply_to_message_id)
        if reply_to_message_id:
            raise RuntimeError("reply target unavailable")
        return _NS(success=True, message_id="om_card_unanchored")


flaky = _FlakyTransport()
flaky_adapter = cls(PlatformConfig(enabled=True))
flaky_adapter._cardkit = flaky
_anchor_turn2 = _NS(turn_id="turn-anchor2", trigger_message_id="om_trigger2", thread_id="",
                    final=False)
res = asyncio.run(flaky_adapter._send_streaming_card(CHAT, "正文", "om_trigger2", {},
                                                     turn=_anchor_turn2))
assert flaky.calls == ["om_trigger2", None], flaky.calls
assert res.success and res.message_id == "om_card_unanchored"
print("OK runtime: reply 锚失败自动无锚重试，卡片仍能发出")
