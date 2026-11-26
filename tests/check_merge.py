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
import os

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HERMES = Path(os.environ.get("LCH_HERMES_DIR", str(Path.home() / ".hermes/hermes-agent")))
PLUGIN = Path(REPO_ROOT) / "__init__.py"
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

    mod._on_admission(platform="feishu", source={"chat_id": CHAT}, session_key="merge-session", message_id="input-first")
    mod._on_pre_tool_call(tool_name="terminal", args={"command": "hermes version"}, tool_call_id="c1", turn_id=mod.REGISTRY.for_chat(platform="feishu", chat_id=CHAT).turn_id)
    line1 = adapter.format_tool_event(ToolCallChunk(tool_name="terminal", preview="hermes version", args={}))
    assert line1 and line1 in adapter._tool_lines, line1

    first = await adapter.send(CHAT, line1, metadata={"expect_edits": True})
    assert fake.created == 1 and first.message_id == "om_1", (fake.created, first)

    mod._on_post_tool_call(tool_name="terminal", duration_ms=1200, status="ok", tool_call_id="c1", turn_id=mod.REGISTRY.for_chat(platform="feishu", chat_id=CHAT).turn_id)
    await adapter.edit_message(CHAT, first.message_id, line1 + " ▉")

    # 答复首发：本回合已有卡 → 转 update，复用同一个 message_id（网关靠它 adopt）
    second = await adapter.send(CHAT, "第一段答复 ▉", metadata={"expect_edits": True})
    assert fake.created == 1, f"同回合不该建第二张卡: {fake.created}"
    assert second.message_id == first.message_id, (second.message_id, first.message_id)
    assert fake.messages == 1, f"不该多发消息: {fake.messages}"
    assert mod._TURNS.answer_text(mod._bridge_key(adapter._turn_for(CHAT))) == "第一段答复"

    # 无归属的中间 edit 不得删除回合卡映射（真实流式帧会经过这里）。
    assert adapter._turn_card_id(CHAT, None) is None
    assert adapter._turn_card_id(CHAT, adapter._turn_for(CHAT)) == first.message_id
    mod._on_session_end(platform="feishu", turn_id=mod.REGISTRY.for_chat(platform="feishu", chat_id=CHAT).turn_id)
    ended = mod.REGISTRY.by_turn_id(adapter._turn_card[CHAT][0])
    assert ended.final
    # 保留真实最终入口，只隔离其 ledger 网络调用；捕获传给 send 的身份。
    original_final = base.FeishuAdapter.send_final_ledgered
    async def deliver(self, event, session_key, text_content, metadata, **kwargs):
        assert metadata["_lch_final_turn_id"] == ended.turn_id
        return await self.send(event.source.chat_id, text_content, metadata=metadata), self
    base.FeishuAdapter.send_final_ledgered = deliver
    try:
        result, _ = await adapter.send_final_ledgered(
            SimpleNamespace(source=SimpleNamespace(chat_id=CHAT), message_id=ended.message_id), ended.session_key,
            "第一段答复，继续写", {}, reply_to=None)
    finally:
        base.FeishuAdapter.send_final_ledgered = original_final
    assert result.success and result.message_id == first.message_id
    assert fake.created == 1 and fake.messages == 1, (fake.created, fake.messages)
    final_card = adapter._card_v2(CHAT, "第一段答复，继续写", streaming=False, turn=ended)
    assert final_card["config"].get("streaming_mode") is not True
    assert "第一段答复" in json.dumps(final_card["header"], ensure_ascii=False)
    assert "hermes version" in json.dumps(final_card, ensure_ascii=False)
    # 限频/失败只能报告失败，不能另发新卡；恢复后仍更新原卡。
    original_update = adapter._cardkit.update
    async def refused(*args, **kwargs):
        return False
    adapter._cardkit.update = refused
    failed = await adapter.send(CHAT, "重试最终答复", metadata={"_lch_final_turn_id": ended.turn_id})
    assert not failed.success and fake.messages == 1
    failed_edit = await adapter.edit_message(CHAT, first.message_id, "重试最终答复", finalize=True)
    assert not failed_edit.success and fake.messages == 1
    adapter._cardkit.update = original_update
    retried = await adapter.send(CHAT, "第一段答复，继续写", metadata={"_lch_final_turn_id": ended.turn_id})
    assert retried.success and retried.message_id == first.message_id
    assert fake.messages == 1
    # Final content is authoritative even when it looks exactly like tool chrome.
    for answer in ("昨日结果\n今日计划\n```text\n销售额待核对\n```",
                   "📊 昨日结果\n✅ 今日计划\n```\n正常正文\n```",
                   "💻 terminal\n```\ndate\n```\n📖 读取文件 README.md"):
        preserved = await adapter.send(CHAT, answer, metadata={"_lch_final_turn_id": ended.turn_id})
        assert preserved.success and preserved.message_id == first.message_id
        assert mod._TURNS.answer_text(mod._bridge_key(ended)) == answer, (repr(mod._TURNS.answer_text(mod._bridge_key(ended))), repr(answer))
        assert fake.messages == 1

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

    # Active-turn notices get their own completed card and cannot replace its mapping or answer.
    mod._on_admission(platform="feishu", source={"chat_id": CHAT}, session_key="continuous", message_id="input-a")
    a = mod.REGISTRY.for_chat(platform="feishu", chat_id=CHAT)
    preview_a = await adapter.send(CHAT, "回答 A", metadata={"expect_edits": True})
    notice = await adapter.send(CHAT, "独立通知")
    assert notice.message_id != preview_a.message_id
    assert adapter._turn_card_id(CHAT, a) == preview_a.message_id
    assert mod._TURNS.answer_text(mod._bridge_key(a)) == "回答 A"
    mod._on_session_end(platform="feishu", turn_id=a.turn_id)
    mod._on_admission(platform="feishu", source={"chat_id": CHAT}, session_key="continuous", message_id="input-b")
    b = mod.REGISTRY.for_chat(platform="feishu", chat_id=CHAT)
    mod._on_session_end(platform="feishu", turn_id=b.turn_id)
    async def exact_final(self, event, session_key, text_content, metadata, **kwargs):
        assert metadata.get("_lch_final_turn_id") == b.turn_id
        return await self.send(event.source.chat_id, text_content, metadata=metadata), self
    base.FeishuAdapter.send_final_ledgered = exact_final
    try:
        final_b, _ = await adapter.send_final_ledgered(
            SimpleNamespace(source=SimpleNamespace(chat_id=CHAT), message_id="input-b"),
            "continuous", "回答 B（无预览）", {}, reply_to=None)
    finally:
        base.FeishuAdapter.send_final_ledgered = original_final
    assert final_b.message_id != preview_a.message_id
    assert adapter._cardkit.entities[final_b.message_id].card["config"].get("streaming_mode") is not True
    assert adapter._cardkit.entities[notice.message_id].card["config"].get("streaming_mode") is not True
    assert mod._TURNS.answer_text(mod._bridge_key(a)) == "回答 A"
    assert mod._TURNS.answer_text(mod._bridge_key(b)) == "回答 B（无预览）"
    assert mod.REGISTRY.for_delivery(platform="feishu", session_key="continuous", message_id="input-a") is None
    # A delayed edit to an old message cannot borrow the current turn's state/checkpoint.
    mod._on_admission(platform="feishu", source={"chat_id": CHAT}, session_key="continuous", message_id="input-c")
    c = mod.REGISTRY.for_chat(platform="feishu", chat_id=CHAT)
    preview_c = await adapter.send(CHAT, "回答 C", metadata={"expect_edits": True})
    checkpoints = []
    original_checkpoint = mod._TURNS.checkpoint
    mod._TURNS.checkpoint = lambda *args: checkpoints.append(args)
    try:
        delayed = await adapter.edit_message(CHAT, preview_a.message_id, "A 延迟最终帧", finalize=True)
    finally:
        mod._TURNS.checkpoint = original_checkpoint
    assert delayed.success and delayed.message_id == preview_a.message_id
    assert mod._TURNS.answer_text(mod._bridge_key(c)) == "回答 C"
    assert mod._TURNS.answer_text(mod._bridge_key(a)) == "A 延迟最终帧"
    assert not checkpoints
    assert adapter._turn_card_id(CHAT, c) == preview_c.message_id
    print("OK delivery identity: notices/next-input/delayed-edit isolated; no-preview final streaming closed")

    print(f"OK single-card: 一回合一张卡（created={fake.created}, im={fake.messages}, "
          f"sequence={len(seqs)}, kinds={sorted(set(kinds))}）")


asyncio.run(main())
