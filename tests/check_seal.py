#!/usr/bin/env python3
"""「新消息进来 → 旧回合封口」判据（对齐 HFC 的 abandon stale session）。

覆盖四件事，全部离线、不发消息：
1. `TurnRegistry.supersede()`：同 chat 未收尾回合被 final，`for_chat_loose` 不再返回它；
2. `TurnRegistry.begin()`：同 session_key 的旧回合被替换时必须一起 final；
3. `_seal_previous_turns()`：把旧卡渲染成终态（含替代文案 + 已停止胶囊），并把
   「更新那张旧消息」交给 `_schedule_seal`；渲染发生在 begin() 之前，内容才算旧回合的；
4. `_on_admission()`：走一遍真实入站登记，旧回合 final、新回合 active，只有一个活动回合；
   开关 `LARK_CARD_SEAL_ON_NEW_MESSAGE=0` 时完全不封口（可回退行为）。

用法: ~/.hermes/hermes-agent/venv/bin/python <本文件>
"""
from pathlib import Path

import importlib.util
import os
import sys

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)          # 失败分支会把插件目录塞进 sys.path，hfc.* 随后可 import

from turn_state import TurnRegistry  # noqa: E402  （插件目录已在 sys.path）

CHAT = "oc_seal_test"
KEY_OLD = f"feishu:{CHAT}:"
SESSION = f"agent:main:feishu:dm:{CHAT}"


def _elements(card: dict) -> list:
    return card.get("body", {}).get("elements", [])


def _text(card: dict) -> str:
    return "\n".join(str(e.get("content", "")) for e in _elements(card))


# ── 1. supersede / begin 的注册表语义 ────────────────────────────────────────
reg = TurnRegistry()
old = reg.begin(platform="feishu", session_key=SESSION, chat_id=CHAT)
reg.resolve(platform="feishu", session_id=SESSION)          # 绑定 session → old
newer = reg.begin(platform="feishu", session_key=SESSION + ":second", chat_id=CHAT)
assert not old.final, "换 session_key 的 begin() 不应静默替代旧回合（由 supersede 负责）"
assert not newer.final

doomed = reg.supersede(platform="feishu", chat_id=CHAT)
assert {t.turn_id for t in doomed} == {old.turn_id, newer.turn_id}, \
    f"supersede 未命中的全部未收尾回合: {[t.turn_id for t in doomed]}"
assert old.final and newer.final and old.ended_at > 0
assert reg.for_chat_loose(platform="feishu", chat_id=CHAT) is None, "封口后不应再有活动回合"
assert reg.resolve(platform="feishu", session_id=SESSION) is None, \
    "已封口回合不得再被 session 绑定命中（否则新回合的帧会投进旧卡）"
assert reg.supersede(platform="feishu", chat_id="") == [], "没有 chat 归属时不得封口"
assert reg.supersede(platform="feishu", chat_id="oc_other") == []
print("OK registry: supersede 封口同 chat 全部未收尾回合，封口后不再被匹配")

# 同 session_key 再来一条：旧回合必须被 begin() 一起 final（否则继续参与 chat 匹配）
reg2 = TurnRegistry()
a = reg2.begin(platform="feishu", session_key=SESSION, chat_id=CHAT)
b = reg2.begin(platform="feishu", session_key=SESSION, chat_id=CHAT)
assert a.final and not b.final and not reg2.resolve(platform="feishu", session_id=SESSION).final
print("OK registry: 同 session_key 的旧回合被 begin() 收尾")


# ── 2. 封口渲染 + 调度 ──────────────────────────────────────────────────────
class _FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def seal_superseded_card(self, chat_id, turn_id, card):   # pragma: no cover
        self.calls.append((chat_id, turn_id, card))

    def __getattr__(self, name):        # `_TURNS.fail/render` 之外的属性一律不碰
        raise AttributeError(name)


scheduled: list[tuple] = []
mod._schedule_seal = lambda adapter, chat_id, turn_id, card: scheduled.append((chat_id, turn_id, card))

fake = _FakeAdapter()
mod._LIVE_ADAPTER = fake
mod.REGISTRY.clear() if hasattr(mod.REGISTRY, "clear") else None
mod.REGISTRY.begin(platform="feishu", session_key=SESSION, chat_id=CHAT)
mod._TURNS.start(KEY_OLD)
mod._TURNS.answer_frame(KEY_OLD, "上一回合已经跑了一半的正文", create_notice=False)

mod._seal_previous_turns(CHAT)
assert len(scheduled) == 1, f"没有调度封口: {scheduled}"
chat_arg, turn_arg, card = scheduled[0]
assert chat_arg == CHAT
sealed_text = _text(card)
assert mod.SEAL_NOTE in sealed_text, f"封口卡缺替代说明: {sealed_text!r}"
assert "已停止" in sealed_text, f"封口卡没有终态胶囊: {sealed_text!r}"
assert "上一回合已经跑了一半的正文" in sealed_text, "封口卡丢了旧回合正文"
print("OK seal: 旧卡渲染成终态（替代文案 + 已停止 + 保留正文）并调度更新")

# 开关关掉 → 不封口（可回退行为）
mod.REGISTRY.begin(platform="feishu", session_key=SESSION + ":off", chat_id=CHAT)
scheduled.clear()
os.environ["LARK_CARD_SEAL_ON_NEW_MESSAGE"] = "0"
mod._seal_previous_turns(CHAT)
assert scheduled == [], "开关关闭后仍在封口"
del os.environ["LARK_CARD_SEAL_ON_NEW_MESSAGE"]
print("OK seal: LARK_CARD_SEAL_ON_NEW_MESSAGE=0 时完全不封口")


# ── 3. 真实入站登记：admission 里先封口再建新回合 ────────────────────────────
mod.REGISTRY.begin(platform="feishu", session_key=SESSION + ":old", chat_id=CHAT)
scheduled.clear()
mod._on_admission(platform="feishu", source={"chat_id": CHAT},
                  session_key=SESSION + ":incoming", message_id="om_trigger_msg")
active = [t for t in mod.REGISTRY.active() if t.platform == "feishu"]
assert len(active) == 1, f"入站后仍有多个活动回合: {[t.turn_id for t in active]}"
assert active[0].session_key.endswith(":incoming")
assert active[0].trigger_message_id == "om_trigger_msg", "admission 没记下触发消息 id（reply 锚要用）"
assert scheduled and {c for c, _t, _card in scheduled} == {CHAT}, f"封口调度异常: {scheduled}"
print("OK admission: 先封口旧回合，再登记新回合（只剩一个 active）")

print("ALL PASS seal")
