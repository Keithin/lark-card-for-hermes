#!/usr/bin/env python3
"""lark-cards 攒批刷新自检：多个快速 edit 应该合并成少数几次 cardkit mutation。

用法: cd ~/.hermes/hermes-agent && ./venv/bin/python <本文件>
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

from gateway.config import PlatformConfig  # noqa: E402


spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CHAT = "oc_coalesce_selftest"
log: list[tuple] = []


class FakeRest:
    def __init__(self) -> None:
        self.created = 0
        self.messages = 0

    async def _tenant_token(self) -> str:
        return "tok"

    async def _request_json(self, method, path, *, token="", json_body=None):
        body = json_body or {}
        if method == "POST" and path == "/cardkit/v1/cards":
            self.created += 1
            log.append(("create",))
            return {"code": 0, "data": {"card_id": f"c{self.created}"}}
        if path.endswith("/settings"):
            kind = "settings"
        elif "/elements/" in path:
            kind = "content"
        else:
            kind = "whole"
        log.append(("mutation", kind))
        return {"code": 0, "data": {}}

    async def send_card_delivery(self, chat_id, card, **kw):
        self.messages += 1
        return SimpleNamespace(success=True, message_id=f"om_{self.messages}")


async def main() -> None:
    from gateway.stream_events import ToolCallChunk

    base = mod._bundled_feishu()
    adapter = mod._build_adapter_class(base)(PlatformConfig(enabled=True))
    fake = FakeRest()
    mod.RestClient = lambda *a, **k: fake

    mod._on_admission(platform="feishu", source={"chat_id": CHAT})
    mod._on_pre_tool_call(tool_name="terminal", args={"command": "echo hi"}, tool_call_id="c1")
    line = adapter.format_tool_event(ToolCallChunk(tool_name="terminal", preview="echo hi", args={}))

    first = await adapter.send(CHAT, line)
    assert fake.created == 1, fake.created

    # 快速发多个 edit，模拟 LLM 流式 delta
    edits = 8
    for i in range(edits):
        await adapter.edit_message(CHAT, first.message_id, f"第 {i} 段内容 ▉")

    # 最后 finalize
    mod._on_session_end(platform="feishu")
    await adapter.edit_message(CHAT, first.message_id, "最终答复", finalize=True)

    mutations = [e for e in log if e[0] == "mutation"]
    assert mutations, "至少应该有一次 mutation"
    # 8 次快速 edit 被合并后，mutation 次数应该远小于 8；
    # 实际会触发 1 次 settings + 1 次 whole（或 content）收尾，最多再加 1 次中间刷新。
    assert len(mutations) <= 4, f"攒批效果不明显: {len(mutations)} mutations for {edits} edits: {mutations}"

    print(f"OK coalesce: {edits} edits -> {len(mutations)} mutations ({[m[1] for m in mutations]})")


asyncio.run(main())
