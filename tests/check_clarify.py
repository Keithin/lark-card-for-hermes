#!/usr/bin/env python3
"""lark-cards clarify 按钮卡自检（不联网、不启网关）。

断言：
1. send_clarify 有选项时发出带按钮的卡，按钮 value 带 clarify_id，且不塞「其他…」；
2. 点按钮 → resolve_gateway_clarify(clarify_id, choice)，校验走真实的 cards.authz 路径；
3. 未知/已解析的 clarify_id 点击什么都不做；retire_clarify_card 把卡改写成失效提示。

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


spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CHAT = "oc_clarify_selftest"
sent: list[dict] = []
resolved: list[tuple] = []
patched: list[tuple] = []


async def main() -> None:
    base = mod._bundled_feishu()
    adapter = mod._build_adapter_class(base)(PlatformConfig(enabled=True))

    async def _fake_send(**kwargs):
        sent.append(json.loads(kwargs["payload"]))
        return {"ok": True}

    adapter._feishu_send_with_retry = _fake_send
    adapter._finalize_send_result = lambda resp, label: SimpleNamespace(
        success=True, message_id=f"om_clarify_{len(sent)}", error=None)
    adapter._loop = SimpleNamespace(is_closed=lambda: False)
    adapter._submit_on_loop = lambda loop, coro: (asyncio.ensure_future(coro), True)[1]
    # 走真实的点击校验（上游 cards.authz.validate_card_action），不 stub：
    # 上游把 adapter 方法搬走时这里会直接红，而不是等线上按钮点不动才发现。
    adapter._admins = {"*"}

    import tools.clarify_gateway as cg

    cg.resolve_gateway_clarify = lambda cid, resp: (resolved.append((cid, resp)), True)[1]

    async def _fake_super_edit(*args, **kw):
        # 调用方是 base.FeishuAdapter.edit_message(self, chat_id, message_id, text)
        patched.append(tuple(args[1:4]))

    # retire_clarify_card 调的是 super().edit_message（内置实现），这里替换掉父类方法
    base.FeishuAdapter.edit_message = _fake_super_edit

    # 1) 发卡
    result = await adapter.send_clarify(CHAT, "选哪个方案？", ["方案A", "方案B"], "cid-1", "sess-1")
    assert result.success, result
    card = sent[-1]
    actions = [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]
    labels = [a["text"]["content"] for a in actions]
    assert labels == ["方案A", "方案B"], labels
    assert all(a["value"]["clarify_id"] == "cid-1" for a in actions), actions

    # 2) 点「方案B」
    def _event(choice: str) -> SimpleNamespace:
        return SimpleNamespace(event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_test"),
            action=SimpleNamespace(value={"hermes_clarify_action": "choice",
                                          "clarify_id": "cid-1", "clarify_choice": choice})))

    adapter._on_card_action_trigger(_event("方案B"))
    await asyncio.sleep(0)
    assert resolved == [("cid-1", "方案B")], resolved
    assert not adapter._clarify_state, "解析后必须删掉状态，防止重复点击"

    # 3) 已解析的卡再点：什么都不做
    adapter._on_card_action_trigger(_event("方案B"))
    await asyncio.sleep(0)
    assert resolved == [("cid-1", "方案B")], "重复点击不能二次解析"

    # 4) 旧卡残留的「其他…」点击（没有 clarify_choice）：不再进文字拦截，也不解析
    await adapter.send_clarify(CHAT, "还有一个问题", ["X"], "cid-2", "sess-1")
    adapter._on_card_action_trigger(SimpleNamespace(event=SimpleNamespace(
        operator=SimpleNamespace(open_id="ou_test"),
        action=SimpleNamespace(value={"hermes_clarify_action": "other", "clarify_id": "cid-2"}))))
    await asyncio.sleep(0)
    assert resolved == [("cid-1", "方案B")], resolved
    assert "cid-2" in adapter._clarify_state, "无选项的点击不能清掉待答状态"

    # 5) retire：超时的卡被改写成失效提示
    await adapter.send_clarify(CHAT, "会超时的问题", ["Y"], "cid-3", "sess-1")
    assert "cid-3" in adapter._clarify_state, adapter._clarify_state
    assert adapter._clarify_state["cid-3"].get("message_id"), adapter._clarify_state
    await adapter.retire_clarify_card("cid-3", "该问题已超时")
    assert patched and patched[-1][2] == "该问题已超时", patched

    # 6) 开放式提问（无选项）仍走内置文字路径：不该出现按钮 action
    await adapter.send_clarify(CHAT, "随便说点什么", None, "cid-4", "sess-1")
    assert "actions" not in sent[-1], "无选项时不该发按钮卡"

    # 7) 所有选项卡都只有选项按钮：不再额外塞「其他…」
    def _labels(card: dict) -> list:
        return [a["text"]["content"] for a in
                [e for e in card["elements"] if e.get("tag") == "action"][0]["actions"]]

    closed = adapter._clarify_card(f"{mod._PRESET_QUESTION}（当前：focused）",
                                   ["classic", "focused"], "cid-5")
    assert _labels(closed) == ["classic", "focused"], _labels(closed)
    closed2 = adapter._clarify_card(f"{mod._ACTIVITY_QUESTION}（当前：summary）",
                                    ["full", "summary", "hidden"], "cid-6")
    assert _labels(closed2) == ["full", "summary", "hidden"], _labels(closed2)
    assert _labels(adapter._clarify_card("选哪个方案？", ["A"], "cid-7")) == ["A"]
    assert all(a["value"]["clarify_id"] == "cid-5" for a in
               [e for e in closed["elements"] if e.get("tag") == "action"][0]["actions"])

    print("OK clarify: 按钮卡 → 解析；无「其他…」；retire 改写；无选项走文字")


asyncio.run(main())
