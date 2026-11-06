#!/usr/bin/env python3
"""判据：四组 HFC 能力搬进插件后仍然「真接线」（不是只搬文件）。

1. session_store    —— 检查点能落盘/读回；上个进程遗留的「执行中」卡能被冻结成收尾卡
2. owner_*          —— OwnerRefreshState 判定 clarify 回执另置一消息；collapsed_transfer_elements 折叠旧正文
3. interaction_receipts/legacy_owner —— 点完的按钮卡变成不含控件的静态回执
4. command_cards/native_commands     —— 命令反馈走命令卡（标题/配色），普通回复不误判
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])
import importlib.util
import json
import sys
import tempfile
import threading
from pathlib import Path

P = REPO_ROOT
sys.path.insert(0, P)          # 插件包的父目录：hfc/ 在这里
spec = importlib.util.spec_from_file_location("lcf", f"{P}/__init__.py")
m = importlib.util.module_from_spec(spec)
sys.modules["lcf"] = m
spec.loader.exec_module(m)

from hfc.bridge import HfcTurnBridge          # noqa: E402  (插件的 sys.path 已由插件补上)
from hfc.owner_display_policy import OwnerRefreshState, collapsed_transfer_elements  # noqa: E402

# ── 1. session_store：检查点 + 遗留卡收尾 ─────────────────────────────────────
tmp = tempfile.mkdtemp(prefix="lcf-store-")
b = HfcTurnBridge.__new__(HfcTurnBridge)
b._sessions, b._pushed, b._seq, b._order = {}, {}, 0, {}
b._lock = threading.RLock()
b._store_dir = Path(tmp).resolve()   # /var 是符号链接，SessionStore 会拒绝
b._store = None
b._checkpointed = {}
b.config = {"title": "Hermes Agent", "footer_fields": ["duration"]}

b.start("oc_a")
b.answer_frame("oc_a", "写到一半的回答")
b.checkpoint("oc_a", "om_test_card")          # 落盘
assert b._checkpointed.get("oc_a"), "checkpoint did not record"
records = b.store().load()
assert len(records) == 1, records
assert records[0]["message_id"] == "om_test_card"
assert getattr(records[0]["session"], "status", "") == "thinking"

stale = b.stale_checkpoints(older_than=-1)    # 负阈值 = 把刚写的当成遗留
assert len(stale) == 1 and stale[0]["message_id"] == "om_test_card"
assert b.stale_checkpoints(older_than=600) == [], "fresh checkpoint must not be reaped"

card = m._stale_card(stale[0])
assert card and card.get("schema") == "2.0", card
flat = json.dumps(card, ensure_ascii=False)
assert "已中断" in flat and "停止更新" in flat
assert "写到一半的回答" in flat, "old content must be kept"
assert "\"button\"" not in flat, "receipt must not carry controls"
assert "⏳" not in flat and "已中断" in card["header"]["title"]["content"]

b.complete("oc_a", duration=1.0)              # 正常收尾要删检查点
assert b.store().load() == [], "completed turn left a checkpoint behind"

# ── 2. owner policy ──────────────────────────────────────────────────────────
state = OwnerRefreshState(
    event_kind="interaction.completed", is_terminal=True, owner_message_id="om_turn",
    interaction_present=True, interaction_status="completed",
    interaction_message_id="om_clarify", has_receipt_fingerprint=False,
    has_legacy_owner_receipt=False,
)
assert state.card_source(lambda: "text") == "separate_receipt"
assert state.freezes_inputs is False

folded = collapsed_transfer_elements([{"tag": "markdown", "content": "x"}])
assert folded and folded[0]["tag"] == "collapsible_panel"
assert folded[0]["elements"][0]["content"] == "x"
assert collapsed_transfer_elements([{"tag": "table"}]) is None

# ── 3. 按钮卡冻结成静态回执 ───────────────────────────────────────────────────
cls = m._build_adapter_class.__globals__  # 只取模块级公开 helper，实例化留给别的判据
assert hasattr(m, "static_legacy_receipt")
clarify_card = {"config": {"wide_screen_mode": True},
                "header": {"template": "blue", "title": {"tag": "plain_text", "content": "Hermes"}},
                "elements": [{"tag": "markdown", "content": "选哪个？"},
                             {"tag": "action", "actions": [{"tag": "button", "text": "A"}]}]}
receipt = m.static_legacy_receipt({**clarify_card, "elements": [
    e for e in clarify_card["elements"] if e.get("tag") in {"markdown", "hr"}
] + [{"tag": "markdown", "content": "**已选择：** A"}]})
flat_r = json.dumps(receipt, ensure_ascii=False)
assert "已选择" in flat_r and "button" not in flat_r
from hfc.legacy_owner import valid_legacy_receipt
assert valid_legacy_receipt(receipt), "receipt must satisfy the static-receipt contract"

# ── 4. 命令卡 ────────────────────────────────────────────────────────────────
reply = "✅ 卡片预设已切换到 task"
assert m._command_card(reply) is None, "unregistered text must not become a command card"
m._mark_command_reply("card-preset", reply)
cmd_card = m._command_card(reply)
assert cmd_card, "registered command reply must render as a command card"
flat_c = json.dumps(cmd_card, ensure_ascii=False)
assert "card-preset" in flat_c and "task" in flat_c
assert cmd_card["header"]["template"] == "green"
m._mark_command_reply("card-preset", "❌ 失败")
assert m._command_card("❌ 失败")["header"]["template"] == "red"
assert m._command_card("普通回答") is None

# ── 5. 标题：notice 取正文首行；v1 兜底卡不再硬编码 "Hermes" ─────────────────────
b.answer_frame("oc_n", "抖音热点榜 · Top 50\n\n完整 50 行表格……", create_notice=True)
notice_card = b.render("oc_n", streaming=False)
assert notice_card["header"]["title"]["content"] == "抖音热点榜 · Top 50", notice_card["header"]
long_title = m.notice_title if hasattr(m, "notice_title") else None
from hfc.bridge import notice_title as nt

assert nt("# " + "x" * 40).endswith("…") and len(nt("# " + "x" * 40)) == 25

m._TURNS.start("oc_v1")
m._TURNS.answer_frame("oc_v1", "回答")
m._TURNS.tool("oc_v1", tool_id="t", name="terminal", status="running", detail="ls")
title, template = m._header_for_v1("oc_v1")
assert "Hermes Agent" in title and title != "Hermes", title
assert "执行中" in title or "工具 #1" in title, title   # 状态/指标至少要露一个
assert template in {"blue", "grey", "green", "orange", "red"}, template
assert m._card_title() == "Hermes Agent", m._card_title()

print("OK parity: session_store 检查点/收尾、owner 策略、静态回执、命令卡、标题 全部真接线")
