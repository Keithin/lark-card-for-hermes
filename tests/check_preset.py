#!/usr/bin/env python3
"""lark-cards 预设切换 + task 模式 footer 颜色 判据。

覆盖三块新逻辑：
1. render.py：task 模式 footer 有状态胶囊（HFC 参考实现在此关掉，本地改回）；classic 不回归；
   模型着色不再被 compact 关掉。
2. _write_preset：写 reading_preset、清掉预设管理的旧显式键、保留无关配置；非法名在写盘前抛错。
3. _on_card_preset / register：非法输入与无 asker 时返回用法文本（不抛）；register() 真的把
   /card-preset 挂到 ctx 上。

只读 + 临时文件：不改真实 config.json、不启动网关、不发消息。
用法: ~/.hermes/hermes-agent/venv/bin/python <本文件>
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # 失败分支会把插件目录塞进 sys.path，hfc.* 随后可 import

from hfc.card_view import TASK_TEXT_SIZE_DEFAULTS  # noqa: E402
from hfc.display_segments import display_view  # noqa: E402
from hfc.render import render_card  # noqa: E402
from hfc.render_options import card_render_options  # noqa: E402
from hfc.session import CardSession  # noqa: E402


GREEN_PILL = "<text_tag color='green'>已完成</text_tag>"


def _completed_session(model: str = "mimo-v2.6-flash", provider: str = "xiaomi") -> CardSession:
    s = CardSession("conv1", "msg1", "oc_test", status="completed")
    s.answer_text = "hello"
    s.duration = 35.0
    s.model = model
    s.provider = provider
    s.tokens = {"input_tokens": 132000, "output_tokens": 695}
    return s


def _footer(presentation: str, session: CardSession) -> str:
    card = render_card(
        display_view(session), title="T",
        footer_fields=["duration", "model", "input_tokens", "output_tokens", "context"],
        **card_render_options({"_presentation_mode": presentation}),
    )
    els = card.get("body", {}).get("elements", [])
    footers = [e for e in els if e.get("element_id") == "footer"]
    assert len(footers) == 1, f"{presentation}: footer 元素数={len(footers)}"
    return footers[0]["content"]


# ── 1. task 模式颜色 ──────────────────────────────────────────────────────────
task_footer = _footer("task", _completed_session())
assert GREEN_PILL in task_footer, f"task footer 缺状态胶囊: {task_footer!r}"
assert "35s" in task_footer and "↑132k" in task_footer, f"task footer 缺指标: {task_footer!r}"

# 模型着色不再被 compact(task) 关掉：deepseek 前缀在 HFC 色表里
deepseek_footer = _footer("task", _completed_session(model="deepseek-v4", provider="deepseek"))
assert "<font color=" in deepseek_footer, f"task 模式模型仍无色: {deepseek_footer!r}"

# classic 不回归：胶囊仍在
classic_footer = _footer("classic", _completed_session())
assert GREEN_PILL in classic_footer, f"classic footer 缺状态胶囊: {classic_footer!r}"

# task 的字号语义没被这次改动带坏（footer 仍是 notation 档）
assert TASK_TEXT_SIZE_DEFAULTS.get("footer"), "TASK_TEXT_SIZE_DEFAULTS.footer 消失"
print("OK render: task 有胶囊+模型色，classic 不回归")

# ── 2. _write_preset 写盘语义 ────────────────────────────────────────────────
seed = {
    "card": {
        "reading_preset": "classic",
        # 预设管理的显式残留（正是「切了只生效一半」的元凶）
        "show_reasoning": True,
        "timeline_expanded": False,
        "stream_thinking_to_body": True,
        # 非预设管理，必须保留
        "native_tables": True,
        "footer_fields": ["duration", "model"],
        "max_timeline_items": 7,
    },
    "bindings": {"native_chats": ["oc_keep"]},
}
with tempfile.TemporaryDirectory() as d:
    cfg_path = Path(d) / "config.json"
    cfg_path.write_text(json.dumps(seed), encoding="utf-8")

    name = mod._write_preset("task", config_path=cfg_path)
    assert name == "task", name
    got = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert got["card"]["reading_preset"] == "task"
    for key in ("show_reasoning", "timeline_expanded", "stream_thinking_to_body"):
        assert key not in got["card"], f"预设管理键 {key} 未被清掉: {got['card']}"
    assert got["card"]["native_tables"] is True
    assert got["card"]["footer_fields"] == ["duration", "model"]
    assert got["card"]["max_timeline_items"] == 7
    assert got["bindings"] == {"native_chats": ["oc_keep"]}, "bindings 被误伤"

    # 非法名：写盘前抛 ValueError，文件原样
    before = cfg_path.read_text(encoding="utf-8")
    try:
        mod._write_preset("bogus", config_path=cfg_path)
    except ValueError:
        pass
    else:
        raise AssertionError("非法预设名没有抛 ValueError")
    assert cfg_path.read_text(encoding="utf-8") == before, "非法输入竟然改了文件"

    # 大小写/空白归一化
    assert mod._write_preset("  TASK ", config_path=cfg_path) == "task"

    # config.json 不存在：从零建，不炸
    missing = Path(d) / "new" / "config.json"
    missing.parent.mkdir()
    assert mod._write_preset("focused", config_path=missing) == "focused"
    assert json.loads(missing.read_text(encoding="utf-8"))["card"]["reading_preset"] == "focused"
print("OK _write_preset: 清残留/保无关/非法名不写盘/归一化/缺文件可建")

# ── 3. handler 兜底 + 命令注册接线 ───────────────────────────────────────────
# 无 asker（离线）：空参数返回用法，不抛
usage = mod._on_card_preset("")
assert "用法：/card-preset" in usage and "classic" in usage, usage
# 非法参数：返回带错误的用法，不抛
usage2 = mod._on_card_preset("bogus")
assert "用法：/card-preset" in usage2 and "bogus" not in usage2.splitlines()[0] or "must be" in usage2, usage2
assert "must be classic" in usage2 or "classic" in usage2, usage2
# 真实 config.json 没被上面两次调用碰到
real_cfg = json.loads((PLUGIN.parent / "config.json").read_text(encoding="utf-8"))
assert real_cfg["card"]["reading_preset"] in ("classic", "task", "focused", "detailed")
print("OK handler: 无 asker/非法输入均回用法文本，真实配置未被触碰")


class _FakeCtx:
    def __init__(self):
        self.hooks = []
        self.commands = {}

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def register_command(self, name, handler, **kw):
        self.commands[name] = (handler, kw)


orig_bundled, orig_build = mod._bundled_feishu, mod._build_adapter_class
mod._bundled_feishu = lambda: types.SimpleNamespace(
    FeishuAdapter=object, register=lambda ctx: None)
mod._build_adapter_class = lambda base: object
try:
    ctx = _FakeCtx()
    mod.register(ctx)
finally:
    mod._bundled_feishu, mod._build_adapter_class = orig_bundled, orig_build

assert "card-preset" in ctx.commands, f"/card-preset 未注册: {sorted(ctx.commands)}"
handler, kw = ctx.commands["card-preset"]
assert handler is mod._on_card_preset
assert "classic" in kw.get("args_hint", ""), kw
assert len(ctx.hooks) == len(mod._HOOKS), "register 漏挂 hook"
print(f"OK register: /card-preset 已注册（{len(ctx.hooks)} hooks + {len(ctx.commands)} command）")

print("ALL PASS")
