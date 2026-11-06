#!/usr/bin/env python3
"""lark-cards 离线自检：脱敏 + 时间线渲染 + 卡片形状（不联网、不启网关、不读凭据）。

用法: ~/.hermes/hermes-agent/venv/bin/python lark-cards-timeline-check.py
退出码 0 = 全过；非 0 = 断言失败（脚本内 assert 直接抛）。
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])

import importlib.util
import json
import sys
from pathlib import Path

PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
AGENT_TREE = Path.home() / ".hermes/hermes-agent"
sys.path.insert(0, str(AGENT_TREE))

spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # 模块级只用 stdlib，可脱离 gateway 载入

# ── 1. 脱敏：工具参数里的凭据不能上卡片 ─────────────────────────────────────────
secret = "sk-ant-api03-ZZZZzzzz1234567890abcdef"
line = mod._redact(f'curl -H "Authorization: Bearer {secret}" https://x.test')
assert secret not in line, f"token 未脱敏: {line}"

preview = mod._preview("terminal", {"command": f'env ACCESS_KEY={secret} hermes status'})
assert secret not in preview, f"preview 未脱敏: {preview}"
assert preview, "preview 不该为空"

# ── 2. 回合注册表：时间线 / 并行同名工具 / 双 chat 隔离 / 收尾 ──────────────────
from turn_state import REGISTRY  # noqa: E402

CHAT = "oc_selftest"
mod._on_admission(platform="feishu", source={"chat_id": CHAT}, session_key="agent:main:feishu:dm:oc_selftest")
turn = REGISTRY.active()[-1]
assert mod._timeline_text(turn, final=False) == "", "回合刚开始时不该有内容"
assert mod._timeline_text(None, final=False) == "", "没有回合时不该渲染时间线"

# 并行同名工具：按 tool_call_id 收尾，不能按名字配错
mod._on_pre_tool_call(tool_name="terminal", args={"command": "a"}, tool_call_id="t1")
mod._on_pre_tool_call(tool_name="terminal", args={"command": "b"}, tool_call_id="t2")
mod._on_post_tool_call(tool_name="terminal", duration_ms=1500, status="ok", tool_call_id="t2")
rows = {r["id"]: r for r in turn.rows}
assert rows["t2"]["status"] == "ok" and rows["t1"]["status"] == "running", rows
mod._on_post_tool_call(tool_name="terminal", duration_ms=250, status="error", tool_call_id="t1")

mod._on_subagent_start(child_role="researcher", child_goal="勘察现场", child_subagent_id="s1")
mod._on_subagent_stop(child_status="ok", duration_ms=4200, child_subagent_id="s1")
rendered = mod._timeline_text(turn, final=False)
assert "1. terminal" in rendered and "1.5s" in rendered and "❌" in rendered, rendered
assert "子代理·researcher" in rendered, rendered
assert "完成" not in rendered, "未 finalize 不该出现收尾行"

# 双 chat 隔离：第二个 chat 有自己的回合，互不串行
mod._on_admission(platform="feishu", source={"chat_id": "oc_other"}, session_key="agent:main:feishu:dm:oc_other")
other = REGISTRY.active()[-1]
assert other is not turn and other.rows == []
# 两个活动回合 + payload 无 turn/session 归属 → 事件必须被丢弃（不猜、不跨轮合并）
mod._on_pre_tool_call(tool_name="web_search", args={"q": "x"}, tool_call_id="t3")
assert turn.rows and other.rows == [], "多候选时不能把行塞进任一回合"

# 收尾：按回合结束，别的回合不受影响
mod._on_session_end(platform="feishu", session_key="agent:main:feishu:dm:oc_selftest")
final = mod._timeline_text(turn, final=True)
assert "✅ 完成 · 3 步" in final, final
assert final.rstrip().endswith("s"), final
assert not other.final, "另一个回合不能被一起收尾"

# 收尾之后的工具事件不再追加（t3 属于 other，不属于已结束的 turn）
mod._on_pre_tool_call(tool_name="late_tool", args={}, tool_call_id="t4")
assert "late_tool" not in mod._timeline_text(turn, final=True), "收尾后仍追加了行"

# 折叠：超过 TIMELINE_MAX_ROWS 折叠早期行
for i in range(mod.TIMELINE_MAX_ROWS + 3):
    REGISTRY.append_row(other, {"kind": "tool", "id": f"f{i}", "name": f"t{i}",
                                "preview": "", "status": "ok", "ms": 1})
folded = mod._timeline_text(other, final=True)
assert "前 3 步已折叠" in folded and f"{mod.TIMELINE_MAX_ROWS + 3}. t" in folded, folded

# 归属不可判定：两个活动回合 + 无 session_id → 事件被丢弃，不跨轮合并
counts = {t.turn_id: len(t.rows) for t in REGISTRY.active()}
mod._on_pre_tool_call(tool_name="ambiguous", args={}, tool_call_id="t9", platform="feishu")
assert {t.turn_id: len(t.rows) for t in REGISTRY.active()} == counts, "多候选时不能猜回合"

print("OK lark-cards self-check: 注册表时间线、并行同名工具、双 chat 隔离、收尾、折叠、多候选不猜")

# 只剩一个活动回合时，无归属事件才被接受
only = REGISTRY.active()[-1] if REGISTRY.active() else None
for t in list(REGISTRY.active()):
    if t is not only:
        REGISTRY.end(t)
mod._on_pre_tool_call(tool_name="unique_tool", args={}, tool_call_id="t7")
assert [r["name"] for r in REGISTRY.active()[0].rows][-1] == "unique_tool"

print("OK timeline/registry checks")
