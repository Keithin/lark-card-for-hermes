#!/usr/bin/env python3
"""lark-cards 斜杠菜单自检（A 档：命令中心 + 指标结果卡 + 按钮回调）。

只读：不联网、不发消息。验证
  1. 核心命令目录读得到（/commands 的数据源）；
  2. 命令中心三层（概览 → 分类 → 详情）结构正确；
  3. 指标结果卡（/status 一类）有指标列；
  4. pre_gateway_dispatch 记下 feishu 命令，send() 侧按 chat 认领且一次性消费；
  5. 进行中回合的正文不会被误认成命令反馈；
  6. 回调导航（category / back / run）返回新卡或复投命令。

用法: cd ~/.hermes/hermes-agent && ./venv/bin/python <本文件>
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])

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

from hfc.native_commands import (  # noqa: E402
    build_command_center_card, build_native_result_card, collect_hermes_command_catalog,
)

CHAT = "oc_command_center_selftest"


def _element_ids(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False)


def _fake_event(command: str, chat_id: str = CHAT) -> SimpleNamespace:
    return SimpleNamespace(
        source=SimpleNamespace(platform="feishu", chat_id=chat_id, thread_id=""),
        text=f"/{command}",
        get_command=lambda: command,
    )


def main() -> None:
    # 1. 目录
    catalog = collect_hermes_command_catalog()
    names = {entry.get("name") for entry in catalog}
    assert catalog and "commands" in names and "status" in names, sorted(names)[:20]

    # 2. 命令中心三层
    overview = build_command_center_card(catalog, center_id="c1")
    text = _element_ids(overview)
    assert "select_static" in text and "hfc_command_center_nav" in text, "概览缺分类下拉"
    assert "原生命令" in text, "概览缺统计"
    category = next(entry["category"] for entry in catalog)
    level2 = build_command_center_card(catalog, center_id="c1", selected_category=category)
    text2 = _element_ids(level2)
    assert "返回首页" in text2 and "查看命令详情" in text2, "分类层缺入口"
    some_name = next(entry["name"] for entry in catalog if entry["category"] == category)
    level3 = build_command_center_card(
        catalog, center_id="c1", selected_category=category, selected_command=some_name,
    )
    text3 = _element_ids(level3)
    assert "返回分类" in text3, "详情层缺返回"

    # 3. 指标结果卡
    metric = build_native_result_card("status", "运行时长: 2h\n模型: deepseek-v4\n工具: 3")
    assert metric["header"]["title"]["content"] == "Hermes 运行状态", metric["header"]
    assert "column_set" in _element_ids(metric), "指标卡没抽到列"

    # 4. 钩子记录 + 一次性认领
    assert mod._on_pre_gateway_dispatch(event=_fake_event("commands")) is None
    card, center_id = mod._command_card("命令清单……", CHAT, turn_live=False)
    assert card is not None and center_id, "命令中心卡没生成"
    assert mod._command_card("命令清单……", CHAT, turn_live=False) == (None, ""), "上下文应一次性消费"

    # 5. 进行中回合不认领
    mod._on_pre_gateway_dispatch(event=_fake_event("status"))
    assert mod._command_card("正文", CHAT, turn_live=True) == (None, "")
    native, _ = mod._command_card("状态正文", CHAT, turn_live=False)
    assert native is not None, "回合外的 /status 回复应出指标卡"

    # 6. 非 feishu 平台不记录
    other = _fake_event("status")
    other.source.platform = "telegram"
    mod._on_pre_gateway_dispatch(event=other)
    assert mod._command_card("随便", CHAT, turn_live=False) == (None, "")

    # 7. 回调导航
    base = mod._bundled_feishu()
    adapter = mod._build_adapter_class(base)(PlatformConfig(enabled=True))
    captured: dict = {}

    def _capture(card=None):
        captured["card"] = card
        return None

    adapter._card_response = _capture  # type: ignore[assignment]

    mod._on_pre_gateway_dispatch(event=_fake_event("commands"))
    mod._command_card("命令清单……", CHAT, turn_live=False)   # 建 center state（带 event）
    center_id = next(iter(mod._CENTER_STATES))
    click = SimpleNamespace(context=SimpleNamespace(open_chat_id=CHAT))
    out = adapter._handle_command_center_action(
        event=click, loop=None,
        action_value={"hfc_action": "command_center", "hfc_command_center_id": center_id,
                      "hfc_command_center_nav": "category", "hfc_command_center_category": category},
    )
    assert out is None and captured.get("card"), "分类导航没返回新卡"
    assert "返回首页" in _element_ids(captured["card"]), "导航卡不是分类层"

    ran: list = []
    adapter._submit_on_loop = lambda loop, coro: (ran.append(coro) or True, coro.close(), True)[2]  # type: ignore[assignment]
    adapter._handle_command_center_action(
        event=click, loop=object(),
        action_value={"hfc_action": "command_center", "hfc_command_center_id": center_id,
                      "hfc_command_center_nav": "run", "hfc_command_center_command": "status"},
    )
    assert ran, "run 没有复投命令"

    print("OK command center: catalog + 3 levels + metric card + chat claim + callbacks")


if __name__ == "__main__":
    main()