#!/usr/bin/env python3
"""超限降级：卡片装不下时正文必须走原生文本，不能拿占位卡换掉整篇回答。

背景（上游 #511/#513 描述的同一段降级链）：`render_card` 在 28KB JSON / 200 元素 / 5 表超限时
返回一张「完整内容已切换为 Hermes 原生消息发送」的占位卡 + `disposition: native`。
HFC 由 server 侧再投递全文；本插件原来只取了 `.card`，于是超限轮次的正文直接丢失。
判据：bridge 暴露处置 → adapter 记标记 → 卡片发完后补发原生文本（且只补一次）。
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

from gateway.config import PlatformConfig          # noqa: E402
from gateway.platforms.base import SendResult      # noqa: E402

spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from hfc.bridge import HfcTurnBridge               # noqa: E402

CHAT = "oc_overlimit_selftest"


# ── 1. bridge: 超限的会话渲染出 native 处置 + 占位卡 ───────────────────────────
bridge = HfcTurnBridge.__new__(HfcTurnBridge)
bridge._sessions, bridge._pushed, bridge._seq, bridge._order = {}, {}, 0, {}
bridge.config = {"title": "T", "footer_fields": ["duration", "model"]}

small = "短回答 " * 20
bridge.start(CHAT)
bridge.answer_frame(CHAT, small)
bridge.complete(CHAT, duration=3.0, tokens={"input_tokens": 10, "output_tokens": 20}, model="m")
card = bridge.render(CHAT, streaming=False)
assert bridge.last_disposition(CHAT) == "card", bridge.last_disposition(CHAT)
assert len(json.dumps(card, ensure_ascii=False)) < 28_000

big = "\n\n".join(f"## 第{i}节\n" + ("压测正文 mixed 0123456789。" * 30) for i in range(60))
bridge.start(CHAT)
bridge.answer_frame(CHAT, big)
bridge.complete(CHAT, duration=60.0, tokens={"input_tokens": 1000, "output_tokens": 5000}, model="m")
stub = bridge.render(CHAT, streaming=False)
stub_text = json.dumps(stub, ensure_ascii=False)
assert bridge.last_disposition(CHAT) == "native", bridge.last_disposition(CHAT)
assert "已切换" in stub_text and len(stub_text) < 2000, f"占位卡异常: {stub_text[:200]}"
print(f"OK bridge: 超限渲染为 native 处置（占位卡 {len(stub_text)} 字节，正文 {len(big)} 字符）")


# ── 2. adapter: 标记 + 只补发一次全文 ─────────────────────────────────────────
cls = mod._build_adapter_class(mod._bundled_feishu())
adapter = cls(PlatformConfig(enabled=True))
base_cls = cls.__mro__[1]

sent: list[tuple[str, str]] = []


async def _fake_send(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
    sent.append((chat_id, content))
    return SendResult(success=True, message_id=f"om_text_{len(sent)}")


original_send = base_cls.send
base_cls.send = _fake_send
try:
    from turn_state import REGISTRY  # noqa: E402

    mod._on_admission(platform="feishu", source={"chat_id": CHAT, "chat_type": "dm"},
                      session_key="agent:main:feishu:dm:oc_overlimit_selftest")
    turn = REGISTRY.active()[-1]

    # 短回答：不该触发补发
    adapter._card_v2(CHAT, small, streaming=False, turn=turn)
    asyncio.run(adapter._flush_over_limit(CHAT, small))
    assert sent == [], f"短回答不该补发原生文本: {sent}"

    # 超限回答：_card_v2 打标记，flush 补发全文一次
    adapter._card_v2(CHAT, big, streaming=False, turn=turn)
    assert CHAT in mod._over_limit_chats(adapter), "超限没有记账"
    asyncio.run(adapter._flush_over_limit(CHAT, big))
    assert sent and sent[-1] == (CHAT, big), "补发的不是完整正文"
    # 重复 flush（终稿再来一次）不得重发
    asyncio.run(adapter._flush_over_limit(CHAT, big))
    assert len(sent) == 1, f"重复补发: {len(sent)} 次"
    print(f"OK adapter: 超限正文补发一次原生文本（{len(sent)} 次发送，{len(sent[0][1])} 字符）")
finally:
    base_cls.send = original_send

print("ALL PASS overlimit")
