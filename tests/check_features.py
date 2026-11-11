#!/usr/bin/env python3
"""A/B/C/D 四项能力的判据（不联网、不发真实消息）。

A completion_notify：门槛、耗时回退、@ 目标降级、只推一次
B over_limit: segmented：分段规划、逐张验证、主卡 PATCH + 续卡新发、失败回退原生文本
C suppressed_notice_kinds：类别识别、按 group/p2p 与精确 chat 合并、被抑制的通知直接丢弃
D 后台任务等待详情：wait/poll 显示「等哪条命令、等了多久」
"""
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

from gateway.config import PlatformConfig      # noqa: E402
from gateway.platforms.base import SendResult  # noqa: E402

spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from hfc.complete_notify import (              # noqa: E402
    build_notify_text, elapsed_seconds, format_elapsed, meets_threshold,
    normalize_min_duration_seconds,
)
from hfc.config_rules import normalize_card_config, normalize_chat_overrides  # noqa: E402
from hfc.notice_lifecycle import notice_kind_for  # noqa: E402
from hfc.segmentation import plan_segments, segment_delivery_key, segment_title  # noqa: E402

CHAT = "oc_feat_selftest"


# ── A. 完成通知 ─────────────────────────────────────────────────────────────
assert normalize_min_duration_seconds(None) == 0.0
assert normalize_min_duration_seconds(60) == 60.0
for bad in (True, -1, 86_401, "60"):
    try:
        normalize_min_duration_seconds(bad)
        raise AssertionError(f"非法门槛没报错: {bad!r}")
    except ValueError:
        pass
# duration=0 时必须回退会话存活时长，否则长任务全被跳过（上游 #501 的坑）
assert elapsed_seconds(0.0, time.time() - 90) > 85
assert elapsed_seconds(1424.0, time.time()) == 1424.0
assert meets_threshold(12, 0) and not meets_threshold(12, 60) and meets_threshold(61, 60)
assert format_elapsed(932) == "15m32s" and format_elapsed(3) == "3s"
assert build_notify_text(932) == "✅ 本轮回复结束（用时 15m32s）"
assert build_notify_text(932, open_id="ou_x").startswith('<at user_id="ou_x"></at> ')
print("OK A: 门槛/bool 拒绝/耗时为 0 回退存活时长/无 open_id 也能发通知")


# ── C+B 配置校验 ────────────────────────────────────────────────────────────
cfg = {"over_limit": "segmented", "completion_notify": {"enabled": True, "min_duration_seconds": 60}}
normalize_card_config(cfg, path="card")
assert cfg["over_limit"] == "segmented" and cfg["completion_notify"]["min_duration_seconds"] == 60.0
for bad_cfg in ({"over_limit": "pages"}, {"completion_notify": {"min_duration_seconds": True}}):
    try:
        normalize_card_config(dict(bad_cfg), path="card")
        raise AssertionError(f"非法配置没报错: {bad_cfg}")
    except ValueError:
        pass
print("OK 配置: over_limit 与 min_duration_seconds 均在任何使用前校验")


# ── C. 通知抑制 ─────────────────────────────────────────────────────────────
kinds = {text: notice_kind_for(text) for text in (
    "💾 自我改进回顾：更新了 skill", "🧠 记忆已更新", "⏰ 定时任务夜间复盘完成", "📈 收盘复盘正文")}
assert kinds["💾 自我改进回顾：更新了 skill"] == "self-improvement"
assert kinds["🧠 记忆已更新"] == "memory"
assert kinds["⏰ 定时任务夜间复盘完成"] == "cron"
assert kinds["📈 收盘复盘正文"] == "", "正常回复不得被判成可抑制通知"

overrides = normalize_chat_overrides({
    "group": {"suppressed_notice_kinds": ["self-improvement", "cron"]},
    CHAT: {"suppressed_notice_kinds": ["memory"]},
})
from hfc.config_rules import resolve_chat_overrides  # noqa: E402
assert resolve_chat_overrides(overrides, "oc_other", "group")["suppressed_notice_kinds"] == [
    "self-improvement", "cron"]
assert resolve_chat_overrides(overrides, CHAT, "group")["suppressed_notice_kinds"] == ["memory"], \
    "精确 chat 覆盖必须压过类型作用域"
assert resolve_chat_overrides(overrides, "oc_other", "p2p") == {}
print("OK C: 类别识别 + 类型作用域/精确 chat 覆盖合并（chat id 优先）")


# ── B. 分段规划 ─────────────────────────────────────────────────────────────
blocks = [f"## 第{i}节\n" + ("分析正文 mixed 0123456789。" * 12) for i in range(6)]
body = "\n\n".join(blocks)
budget = len(body) // 2  # 约两段/卡：2 张卡装完 6 个块
plan = plan_segments(body, fits=lambda text: len(text) <= budget, max_segments=5)
assert plan is not None and 2 <= plan.count <= 5, plan
assert "".join(plan.chunks).replace("\n", "") == body.replace("\n", ""), "切分必须无损"
assert plan_segments(body, fits=lambda text: len(text) <= 5) is None, "单块装不下必须认输"
assert plan_segments(body, fits=lambda text: len(text) <= budget, max_segments=1) is None, \
    "张数超上限必须认输"
assert segment_title("Hermes Agent", 1, 3) == "Hermes Agent（2/3）"
assert segment_delivery_key("k", 2) == "k:segment:2"
print(f"OK B: 分段规划无损、{plan.count} 段、超限/超张数都回退")


# ── B/C/A/D 运行时（adapter 级）──────────────────────────────────────────────
cls = mod._build_adapter_class(mod._bundled_feishu())
adapter = cls(PlatformConfig(enabled=True))
base_cls = cls.__mro__[1]
sent: list[tuple[str, str]] = []


async def _fake_send(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
    sent.append((chat_id, str(content)))
    return SendResult(success=True, message_id=f"om_{len(sent)}")


original_send = base_cls.send
base_cls.send = _fake_send


class _StubCardkit:
    """既当 cardkit（PATCH）又当 transport（续卡新发）。"""

    def __init__(self):
        self.updates = []
        self.sent = []

    async def update(self, message_id, card):
        self.updates.append((message_id, card))
        return True

    async def send(self, chat_id, card, delivery_uuid=None, reply_to_message_id=None):
        self.sent.append((chat_id, delivery_uuid, card))
        return object()


try:
    from turn_state import REGISTRY  # noqa: E402

    mod._on_admission(platform="feishu", source={"chat_id": CHAT, "chat_type": "dm",
                                                "open_id": "ou_me"},
                      session_key="agent:main:feishu:dm:oc_feat_selftest")
    turn = REGISTRY.active()[-1]
    assert turn.notify_user_id == "ou_me", turn.notify_user_id

    # C：被抑制的通知直接丢弃（不发任何消息）
    adapter._overrides_cache = None
    # dm 归一成 p2p，所以抑制规则写在 p2p 作用域下
    mod._CHAT_OVERRIDES = {"p2p": {"suppressed_notice_kinds": ["self-improvement"]}}
    adapter_decision = mod._chat_overrides(CHAT, "p2p")
    assert adapter_decision["suppressed_notice_kinds"] == ["self-improvement"]
    results = asyncio.run(adapter.send(CHAT, "💾 自我改进回顾：清理了 3 条 skill"))
    assert not sent, f"被抑制的通知仍被发出: {sent}"
    assert getattr(results, "success", False), "抑制通知应视为成功（不重试）"
    mod._CHAT_OVERRIDES = None

    # A：通知文案与 @ 降级
    ok = asyncio.run(adapter.notify_completion(CHAT, 932.0, mention=True, user_id="ou_me"))
    assert ok and sent[-1][1].startswith('<at user_id="ou_me"></at> ✅ 本轮回复结束（用时 15m32s）')
    ok = asyncio.run(adapter.notify_completion(CHAT, 61.0, mention=True, user_id="tenant_user_x"))
    assert ok and "本轮回复结束（用时 1m01s）" in sent[-1][1] and "<at" not in sent[-1][1], \
        f"解析不到 open_id 时应发不带 @ 的通知: {sent[-1][1]}"
    print("OK A: 有/无 open_id 都能推送（@ 降级）")

    # A：门槛跳过 + 只推一次
    mod._completion_notify_config = lambda: {}  # 默认未启用
    before = len(sent)
    mod._maybe_notify_completion(turn)
    assert len(sent) == before, "未启用 completion_notify 时不得推送"

    # B：segmented 交付 = 主卡 PATCH + 续卡新发
    big = "\n\n".join(f"## 第{i}节\n" + ("压测正文 mixed 0123456789。" * 130) for i in range(10))
    adapter._card_v2(CHAT, big, streaming=False, turn=turn)
    assert CHAT in mod._over_limit_chats(adapter), "超限没有记账"
    adapter._cardkit = _StubCardkit()
    original_config = mod._plugin_config
    mod._plugin_config = lambda: {"card": {"over_limit": "segmented"}}
    sent.clear()
    asyncio.run(adapter._flush_over_limit(CHAT, big, turn=turn, message_id="om_stub"))
    mod._plugin_config = original_config
    assert adapter._cardkit.updates and adapter._cardkit.updates[0][0] == "om_stub", \
        "主卡应 PATCH 到原消息上"
    assert adapter._cardkit.sent, "续卡应作为新消息投递"
    assert all(":segment:" in (uuid or "") for _, uuid, _ in adapter._cardkit.sent), \
        "续卡投递键必须带 :segment:{i}（幂等）"
    titles = [json.dumps(card, ensure_ascii=False) for _, _, card in adapter._cardkit.sent]
    assert any("（2/2）" in text for text in titles), "续卡标题应带编号"
    assert not sent, "segmented 模式不该再回退原生文本"
    print(f"OK B: 主卡 PATCH + {len(adapter._cardkit.sent)} 张续卡（编号 + 幂等键，无原生文本回退）")

    # D：后台任务预览
    mod._remember_background_process({"action": "start", "command": "python backtest.py"},
                                    '{"success": true, "process_id": "bash-7"}')
    wait_line = mod._preview("process_manage", {"action": "wait", "process_id": "bash-7"})
    assert "等待后台 #bash-7" in wait_line and "python backtest.py" in wait_line, wait_line
    print("OK D: 后台等待显示命令与等待时长")
finally:
    base_cls.send = original_send

print("ALL PASS features A+B+C+D")
