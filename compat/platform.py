"""内置 Feishu 平台条目与适配器类：插件唯一的私有依赖面。

为什么还要依赖内置适配器：卡片能力是「替换出站的两个原语（send / edit_message）」，而
配对、允许名单、准入、环境变量桥、dashboard、setup 这些规则都在内置平台条目里。本插件
**复制该条目的全部字段**，只把 `adapter_factory` 换成本插件的子类，因此这些规则保持原样
（不新增 trusted_inbound，不改准入）。

支持范围：行为测试跑在 `TESTED_HERMES` 上；超范围不保证，但失效形态是「卡片降级为原生
文本」，消息照常送达。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 版本协调：这是**已跑过本仓库 tests/ 的**范围，不是「只在这段内可用」的硬门禁。
TESTED_HERMES = ">=0.22.7,<0.23"

_BASE_PACKAGE = "hermes_feishu_bundled"
# 这些字段由注册方自己决定（name/label 是条目身份，factory/check 由本插件提供，
# source/plugin_name 由 plugin 上下文写入），不复制。
_NOT_COPIED = {"name", "label", "adapter_factory", "check_fn", "source", "plugin_name"}


def adapter_module() -> Any:
    """导入内置 feishu 插件（私有包名下，避免与网关自己的模块冲突）。"""
    if _BASE_PACKAGE not in sys.modules:
        import gateway  # 拥有 gateway/ 的那棵树也是插件树

        root = Path(gateway.__file__).resolve().parent.parent / "plugins" / "platforms" / "feishu"
        spec = importlib.util.spec_from_file_location(
            _BASE_PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)],
        )
        if spec is None or spec.loader is None:   # 布局变了：交给调用方降级
            raise ImportError(f"cannot load bundled feishu plugin at {root}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_BASE_PACKAGE] = module
        spec.loader.exec_module(module)
    return importlib.import_module(_BASE_PACKAGE + ".adapter")


def registered_entry(name: str = "feishu") -> Optional[Any]:
    """当前已注册的 feishu PlatformEntry（会按需解析 bundled 的延迟加载器）。

    返回 None = 找不到条目（内置插件缺失/被改名）：调用方必须放弃注册并降级为原文本，
    不能凭猜造一个条目——那会改变配对与准入规则。
    """
    try:
        from gateway.platform_registry import platform_registry

        entry = platform_registry.get(name)
    except Exception as exc:  # 注册表内部结构变了
        logger.warning("[lark-card] platform registry lookup failed: %s", exc)
        return None
    return entry


def entry_kwargs(entry: Any) -> dict[str, Any]:
    """把条目字段翻成 ``ctx.register_platform`` 的 kwargs（原样复制，只换 factory）。"""
    from dataclasses import MISSING, fields

    from gateway.platform_registry import PlatformEntry

    kwargs: dict[str, Any] = {}
    for field in fields(PlatformEntry):
        if field.name in _NOT_COPIED:
            continue
        if hasattr(entry, field.name):
            kwargs[field.name] = getattr(entry, field.name)
        elif field.default is not MISSING:      # 老/新条目缺字段：用数据类默认值
            kwargs[field.name] = field.default
        elif field.default_factory is not MISSING:
            kwargs[field.name] = field.default_factory()
    return kwargs


def compatibility_report() -> dict[str, Any]:
    """只读自检：供测试与 ``/card-preset`` 之外的诊断使用。"""
    entry = registered_entry()
    return {
        "tested_hermes": TESTED_HERMES,
        "bundled_entry": None if entry is None else {
            "name": entry.name, "label": entry.label, "source": entry.source,
            "required_env": list(entry.required_env),
            "allowed_users_env": entry.allowed_users_env,
            "allow_all_env": entry.allow_all_env,
        },
    }
