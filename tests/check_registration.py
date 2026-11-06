#!/usr/bin/env python3
"""判据：注册走公开的 ``ctx.register_platform``，且沿用内置条目的准入/配对字段。

1. 不重绑内置模块的 ``FeishuAdapter`` 全局（旧做法会污染同进程的其他使用者）；
2. ``register_platform`` 收到的 name/label/required_env/allowed_users_env/allow_all_env
   与内置 feishu 条目一致，只换 adapter_factory；
3. factory 造出来的是本插件的子类，而不是内置类本身；
4. 内置条目不可用时**放弃注册**并降级（不猜条目、不改变准入）。
"""
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[1])
import importlib.util
import sys
from dataclasses import dataclass, field

HERMES = Path.home() / ".hermes/hermes-agent"   # 只读源码树：插件与网关的 import 根
sys.path.insert(0, str(HERMES))
sys.path.insert(0, REPO_ROOT)                   # compat/ 是插件自己的包


@dataclass
class _Entry:
    name: str = "feishu"
    label: str = "Feishu / Lark"
    adapter_factory: object = None
    check_fn: object = lambda: True
    required_env: list = field(default_factory=lambda: ["FEISHU_APP_ID", "FEISHU_APP_SECRET"])
    allowed_users_env: str = "FEISHU_ALLOWED_USERS"
    allow_all_env: str = "FEISHU_ALLOW_ALL"
    install_hint: str = "hint"
    max_message_length: int = 8000
    source: str = "builtin"
    plugin_name: str = ""
    trusted_inbound: bool = False


class _Ctx:
    def __init__(self):
        self.platforms = []
        self.hooks = []
        self.commands = []

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)
        return object()

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def register_command(self, name, handler, **kwargs):
        self.commands.append(name)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("lcf_reg", f"{REPO_ROOT}/__init__.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lcf_reg"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    mod = _load_plugin()

    # 内置适配器模块：记录注册前后它的 FeishuAdapter 是否被改写
    base = mod._bundled_feishu()
    original = base.FeishuAdapter

    entry = _Entry()
    mod_registry = sys.modules["lcf_reg"]
    # 插件可能把 compat 解析成 ``compat.platform`` 或 ``<模块名>.compat.platform``：
    # 两个都是同一份代码的不同实例，测试把找到的都打上桩。
    compat_mods = [m for k, m in list(sys.modules.items())
                   if k == "compat.platform" or k.endswith(".compat.platform")]
    if not compat_mods:      # register() 内部才 import：先跑一遍让它加载
        mod_registry.register(_Ctx())
        compat_mods = [m for k, m in list(sys.modules.items())
                       if k == "compat.platform" or k.endswith(".compat.platform")]
    assert compat_mods, "plugin did not import its compat layer"
    compat = compat_mods[0]

    for compat_mod in compat_mods:
        compat_mod.registered_entry = lambda name="feishu": entry  # 假的内置条目
    ctx = _Ctx()
    mod_registry.register(ctx)

    assert base.FeishuAdapter is original, "bundled FeishuAdapter must not be rebound"
    assert len(ctx.platforms) == 1, ctx.platforms
    call = ctx.platforms[0]
    assert call["name"] == "feishu" and call["label"] == "Feishu / Lark"
    assert call.get("trusted_inbound") in (None, False), "must never waive admission/allowlists"
    assert call["required_env"] == entry.required_env
    assert call["allowed_users_env"] == entry.allowed_users_env
    assert call["allow_all_env"] == entry.allow_all_env
    assert call["max_message_length"] == entry.max_message_length
    assert callable(call["adapter_factory"]) and callable(call["check_fn"])
    assert call["adapter_factory"].__name__ == "build_lark_card_adapter"

    # factory 造出来的是子类（用假 config 走构造，不联网）
    cls = mod._build_adapter_class(base)
    assert issubclass(cls, original)
    assert cls is not original
    assert set(ctx.hooks) >= {"post_gateway_admission", "pre_tool_call", "on_session_end"}
    assert "card-preset" in ctx.commands

    # 内置条目缺失：放弃注册，但仍注册 hooks（原生文本兜底）
    for compat_mod in compat_mods:
        compat_mod.registered_entry = lambda name="feishu": None
    ctx2 = _Ctx()
    mod_registry.register(ctx2)
    assert ctx2.platforms == [], "must not invent a platform entry"
    assert ctx2.hooks, "hooks still register so nothing else breaks"

    print("OK registration: ctx.register_platform(feishu) + 条目字段原样继承，不重绑内置类")


if __name__ == "__main__":
    main()
