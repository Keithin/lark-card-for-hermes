#!/usr/bin/env python3
"""判据：回合外发送（启动通知/cron/命令回复）自动建卡必须按 notice 渲染并立即 completed。

回归背景 2026-11-05：answer_frame 自动 start 的 session 停在 thinking、收不到
message.completed，卡片永远显示「⏳ 执行中」（甚至带着几小时前的计时）。
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])
import json
import sys
from pathlib import Path
import threading

sys.path.insert(0, REPO_ROOT)
from hfc.bridge import HfcTurnBridge  # noqa: E402


b = HfcTurnBridge.__new__(HfcTurnBridge)
b._sessions, b._pushed, b._seq = {}, {}, 0
b._lock = threading.RLock()
b._order = {}
b.config = {"title": "Hermes Agent",
            "footer_fields": ["duration", "model", "input_tokens", "output_tokens", "context"]}

# 1) 回合外：create_notice=True → notice + completed，卡片不得出现 执行中/⏳
b.answer_frame("oc_notice", "启动通知内容", create_notice=True)
s = b._sessions["oc_notice"]
assert s.delivery_kind == "notice" and s.status == "completed", (s.delivery_kind, s.status)
card = b.render("oc_notice", streaming=False)
text = json.dumps(card, ensure_ascii=False)
assert card, "notice render returned None"
assert "执行中" not in text and "⏳" not in text, text[:400]

# 2) 正常回合：默认 live → thinking，complete 后 → completed
b.answer_frame("oc_live", "回答中…")
s2 = b._sessions["oc_live"]
assert s2.delivery_kind == "chat" and s2.status == "thinking"
card2 = b.render("oc_live", streaming=True)
assert "执行中" in json.dumps(card2, ensure_ascii=False) or "思考中" in json.dumps(card2, ensure_ascii=False)
b.complete("oc_live", duration=1.2)
assert s2.status == "completed"

print("OK notice: 回合外建卡=notice+completed（无 ⏳/执行中），live 回合不受影响")
