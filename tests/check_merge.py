#!/usr/bin/env python3
"""lark-cards 单卡自检：HFC 的 CardKitTransport + HFC 的 render，全程不联网。

用假的 REST 通道把 adapter 的 send→edit→finalize 序列跑一遍，断言：
一个回合只建一张卡（第二次 send 改成 update 并复用 message_id）、增量只推正文元素、
收尾先关 streaming、sequence 严格递增、正文不带流式光标、v2 卡不退 v1 PATCH、
回合外的每条通知各建一张新卡（不复用已绑定的 cardkit 卡，否则飞书回 200780）。

用法: cd ~/.hermes/hermes-agent && ./venv/bin/python <本文件>
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

from gateway.config import PlatformConfig  # noqa: E402
from gateway.stream_events import ToolCallChunk  # noqa: E402


spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CHAT = "oc_merge_selftest"
log: list[tuple] = []


class FakeRest:
    """CardKitTransport 只依赖这三个方法。"""

    def __init__(self) -> None:
        self.created = 0
        self.messages = 0

    async def _tenant_token(self) -> str:
        return "tok"

    async def _request_json(self, method, path, *, token="", json_body=None):
        body = json_body or {}
        if method == "POST" and path == "/cardkit/v1/cards":
            self.created += 1
            log.append(("create", json.loads(body["data"])))
            return {"code": 0, "data": {"card_id": f"c{self.created}"}}
        if path.endswith("/settings"):
            kind = "settings"
        elif "/elements/" in path:
            kind = "content"
        else:
            kind = "whole"
        log.append(("mutation", body.get("sequence"), body.get("uuid"), kind, path))
        return {"code": 0, "data": {}}

    async def send_card_delivery(self, chat_id, card, **kw):
        self.messages += 1
        log.append(("im", self.messages))
        return SimpleNamespace(success=True, message_id=f"om_{self.messages}")


def _answer() -> str:
    for entry in reversed(log):
        if entry[0] in ("create", "whole"):
            card = entry[1] if entry[0] == "create" else entry[1]
            body = json.dumps(card, ensure_ascii=False)
            return body
    return ""


async def main() -> None:
    base = mod._bundled_feishu()
    adapter = mod._build_adapter_class(base)(PlatformConfig(enabled=True))
    fake = FakeRest()
    mod.RestClient = lambda *a, **k: fake          # adapter._transport() 用它

    mod._on_admission(platform="feishu", source={"chat_id": CHAT})
    mod._on_pre_tool_call(tool_name="terminal", args={"command": "hermes version"}, tool_call_id="c1")
    line1 = adapter.format_tool_event(ToolCallChunk(tool_name="terminal", preview="hermes version", args={}))
    assert line1 and line1 in adapter._tool_lines, line1

    first = await adapter.send(CHAT, line1)
    assert fake.created == 1 and first.message_id == "om_1", (fake.created, first)

    mod._on_post_tool_call(tool_name="terminal", duration_ms=1200, status="ok", tool_call_id="c1")
    await adapter.edit_message(CHAT, first.message_id, line1 + " ▉")

    # 答复首发：本回合已有卡 → 转 update，复用同一个 message_id（网关靠它 adopt）
    second = await adapter.send(CHAT, "第一段答复 ▉")
    assert fake.created == 1, f"同回合不该建第二张卡: {fake.created}"
    assert second.message_id == first.message_id, (second.message_id, first.message_id)
    assert fake.messages == 1, f"不该多发消息: {fake.messages}"
    assert "▉" not in mod._TURNS.answer_text(CHAT), mod._TURNS.answer_text(CHAT)

    mod._on_session_end(platform="feishu")
    await adapter.edit_message(CHAT, first.message_id, "第一段答复，继续写", finalize=True)

    kinds = [e[3] for e in log if e[0] == "mutation"]
    assert "settings" in kinds, f"finalize 没有关 streaming: {kinds}"
    seqs = [e[1] for e in log if e[0] == "mutation"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"sequence 必须严格递增: {seqs}"
    assert all(len(e[2]) == 40 for e in log if e[0] == "mutation"), "uuid 必须 40 字符"
    assert not adapter._card_messages, "v2 卡不能退回 v1 PATCH"

    # 回合已结束（final=True）：每条通知/定时任务都要新建卡。
    # 复用旧 key 会把新消息绑到已绑定的 cardkit 卡上 → 飞书 200780 → 回落 v1 裸 markdown。
    n1 = await adapter.send(CHAT, "回合外通知一")
    n2 = await adapter.send(CHAT, "回合外通知二")
    assert fake.created == 3, f"回合外每次都要新建卡: created={fake.created}"
    assert n1.message_id != n2.message_id, (n1.message_id, n2.message_id)
    assert n1.message_id != first.message_id, f"回合外消息不能改写上一回合的卡: {n1.message_id}"

    print(f"OK single-card: 一回合一张卡（created={fake.created}, im={fake.messages}, "
          f"sequence={len(seqs)}, kinds={sorted(set(kinds))}）")


asyncio.run(main())
