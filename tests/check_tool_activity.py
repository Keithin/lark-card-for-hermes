#!/usr/bin/env python3
"""lark-cards tool_activity 模式自检：验证 full/summary/hidden 三档在正文区的表现。

用法: cd ~/.hermes/hermes-agent && ./venv/bin/python <本文件>
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])

import sys
from pathlib import Path

HERMES = Path.home() / ".hermes/hermes-agent"
sys.path.insert(0, str(HERMES))
sys.path.insert(0, REPO_ROOT)

from hfc.render import render_card
from hfc.session import CardSession, ToolState
from hfc.card_timeline import CardTimeline


def body_text(card: dict) -> str:
    return "\n".join(
        el.get("content", "")
        for el in card.get("body", {}).get("elements", [])
        if el.get("tag") == "markdown"
    )


def make_live_session() -> CardSession:
    session = CardSession(
        conversation_id="c1",
        message_id="m1",
        chat_id="oc_tool_activity_selftest",
        status="thinking",
        _tool_call_count=2,
        tools={
            "t1": ToolState(
                tool_id="t1", name="terminal", status="running",
                detail="echo hi\n参数: {\"command\": \"echo hi\"}",
                ordinal=1, started_at=__import__("time").time(),
            ),
            "t2": ToolState(
                tool_id="t2", name="read_file", status="completed",
                detail="session.py\n参数: {\"path\": \"session.py\"}",
                ordinal=2, duration_ms=1200,
            ),
        },
        timeline=CardTimeline(),
    )
    return session


def main() -> None:
    session = make_live_session()

    full_card = render_card(session, tool_activity="full")
    full_body = body_text(full_card)
    assert "terminal" in full_body, f"full 应该显示工具名: {full_body}"
    assert "执行命令" in full_body, f"full 应该显示动作: {full_body}"

    summary_card = render_card(session, tool_activity="summary")
    summary_body = body_text(summary_card)
    assert "执行中" in summary_body and "第 2 步" in summary_body, f"summary 应该只显示步数: {summary_body}"
    assert "terminal" not in summary_body, f"summary 不应该显示工具名: {summary_body}"
    assert "执行命令" not in summary_body, f"summary 不应该显示具体动作: {summary_body}"

    hidden_card = render_card(session, tool_activity="hidden")
    hidden_body = body_text(hidden_card)
    assert "terminal" not in hidden_body, f"hidden 不应该显示工具名: {hidden_body}"
    assert "执行命令" not in hidden_body, f"hidden 不应该显示动作: {hidden_body}"
    assert "第 2 步" not in hidden_body, f"hidden 不应该显示进度: {hidden_body}"

    # focused preset = hide finished tools: a completed card must not resurrect the failed rows
    # (that put the same two red 失败 rows on every card); a failed turn keeps them.
    finished = make_live_session()
    finished.status = "completed"
    finished.tools["t1"].status = "failed"
    finished.tools["t2"].status = "failed"
    finished_body = body_text(render_card(finished, hide_successful_tool_activity=True))
    assert "执行命令" not in finished_body, f"completed 卡不应恢复失败工具行: {finished_body}"
    finished.status = "failed"
    failed_body = body_text(render_card(finished, hide_successful_tool_activity=True))
    assert "执行命令" in failed_body, f"failed 卡应保留失败工具行: {failed_body}"

    print("OK tool_activity: full shows tools, summary shows progress only, hidden shows nothing live, "
          "focused hides failed rows once completed")


if __name__ == "__main__":
    main()
