#!/usr/bin/env python3
"""lark-cards 装载自检：插件能否被网关发现/放行，注册的 6 个 hook 是否都在 VALID_HOOKS 里。

只读：不启动网关、不注册真实适配器、不发消息。
用法: cd ~/.hermes/hermes-agent && ./venv/bin/python <本文件>
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])

import importlib.util
import sys
from pathlib import Path

HERMES = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / ".hermes/plugins/lark-card-for-hermes/__init__.py"
sys.path.insert(0, str(HERMES))

from hermes_cli.plugins import VALID_HOOKS  # noqa: E402
from hermes_cli.plugins_discovery import (  # noqa: E402

    collect_directory_manifests, gate_manifest, resolve_manifest_winners,
)

winners = resolve_manifest_winners(collect_directory_manifests())
manifest = winners.get("lark-card-for-hermes")
assert manifest is not None, f"lark-card-for-hermes 未被发现；已发现: {sorted(winners)}"
gate = gate_manifest(manifest, set(), {"lark-card-for-hermes"})
assert gate.action == "load", f"gate 结果不是 load: {gate.action} ({getattr(gate, 'error', '')})"

spec = importlib.util.spec_from_file_location("lark_cards_plugin", PLUGIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

hooks = dict(mod._HOOKS)
missing = sorted(set(hooks) - VALID_HOOKS)
assert not missing, f"这些 hook 名不被 Hermes 支持: {missing}"
for name, cb in hooks.items():
    assert callable(cb), name

print(f"OK discovery: lark-card-for-hermes → {gate.action}, {len(hooks)} hooks all in VALID_HOOKS")
