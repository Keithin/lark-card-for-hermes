#!/usr/bin/env python3
"""判据：注册走公开的 ``ctx.register_platform``，字段与内置插件的声明逐项一致。

1. 不重绑内置模块的 ``FeishuAdapter`` 全局（旧做法会污染同进程的其他使用者）；
2. 加载路径**绝不读 platform_registry**——feishu 是 deferred 条目，`get()` 会跑它的
   deferred loader，loader 要拿 discovery 锁，而锁被父线程的 deadline join 持有 →
   必死等到 10s 超时、注册全被拒（2026-11-06 网关实测的回归）；
3. 传给 ctx.register_platform 的 kwargs == 内置插件自己 register() 声明的 kwargs，
   只换 adapter_factory；准入字段（allowed_users_env / allow_all_env /
   allowlist_suppresses_pairing）逐项等于上游源码里的值；永不带 trusted_inbound；
4. 录音失败时**放弃注册**并降级（不猜字段、不改准入），hooks 照常注册。
"""
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[1])
import importlib.util
import sys

HERMES = Path.home() / ".hermes/hermes-agent"   # 只读源码树：插件与网关的 import 根
sys.path.insert(0, str(HERMES))
sys.path.insert(0, REPO_ROOT)                   # compat/ 是插件自己的包

from compat import platform as _compat_first      # register() 内部走同一顶层导入 → 同一实例


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


def _compat_modules():
    """插件可能把 compat 解析成 ``compat.platform`` 或 ``lcf_reg.compat.platform``。"""
    return [m for k, m in list(sys.modules.items())
            if k == "compat.platform" or k.endswith(".compat.platform")]


def main() -> None:
    mod = _load_plugin()

    # 内置适配器模块：记录注册前后它的 FeishuAdapter 是否被改写
    base = mod._bundled_feishu()
    original = base.FeishuAdapter
    compat = _compat_first
    assert compat in _compat_modules(), "test and plugin must share one compat instance"

    # 上游的参考声明：内置插件自己 register() 录出来的 kwargs
    ref = compat._RegistrationRecorder()
    base.register(ref)
    upstream = [k for k in ref.platforms if k.get("name") == "feishu"]
    assert len(upstream) == 1, f"bundled feishu must declare exactly one platform, got {len(upstream)}"
    upstream = upstream[0]

    # 回归闸：加载期间谁都不许读注册表（读 = 触发 deferred loader = 死锁到 10s 超时）
    from gateway.platform_registry import platform_registry
    orig_get = platform_registry.get

    def _registry_boom(*_args, **_kwargs):
        raise AssertionError("platform_registry.get() during plugin load — deferred-loader deadlock")

    platform_registry.get = _registry_boom
    try:
        ctx = _Ctx()
        mod.register(ctx)
    finally:
        platform_registry.__dict__.pop("get", None)   # 还原成类方法，不留实例属性

    assert base.FeishuAdapter is original, "bundled FeishuAdapter must not be rebound"
    assert len(ctx.platforms) == 1, ctx.platforms
    call = ctx.platforms[0]

    # 逐项等于上游声明，只换 adapter_factory
    assert set(call) == set(upstream), (sorted(set(call) ^ set(upstream)))
    for key in upstream:
        if key == "adapter_factory":
            continue
        assert call[key] == upstream[key], f"field drift on {key!r}: {call[key]!r} != {upstream[key]!r}"
    assert call.get("trusted_inbound") in (None, False), "must never waive admission/allowlists"
    # 准入字段的具名核对（不依赖 dict 对比的盲区）
    assert call["allowed_users_env"] == "FEISHU_ALLOWED_USERS"
    assert call["allow_all_env"] == "FEISHU_ALLOW_ALL_USERS"
    assert call["allowlist_suppresses_pairing"] is True
    assert call["required_env"] == ["FEISHU_APP_ID", "FEISHU_APP_SECRET"]
    assert callable(call["adapter_factory"]) and callable(call["check_fn"])
    assert call["adapter_factory"].__name__ == "build_lark_card_adapter"
    assert call["name"] == "feishu" and call["label"] == "Feishu / Lark"

    # factory 造出来的是子类（不联网）
    cls = mod._build_adapter_class(base)
    assert issubclass(cls, original)
    assert cls is not original
    assert set(ctx.hooks) >= {"post_gateway_admission", "pre_tool_call", "on_session_end"}
    assert "card-preset" in ctx.commands

    # 录音失败：放弃平台注册，但仍注册 hooks（原生文本兜底）
    for compat_mod in _compat_modules():
        compat_mod.recorded_platform_kwargs = lambda: None
    ctx2 = _Ctx()
    mod.register(ctx2)
    assert ctx2.platforms == [], "must not invent a platform entry"
    assert ctx2.hooks, "hooks still register so nothing else breaks"

    print("OK registration: 公开 ctx.register_platform(feishu)、字段=上游声明原样（仅换 factory）、"
          "加载期零注册表读取、不重绑内置类")


if __name__ == "__main__":
    main()
