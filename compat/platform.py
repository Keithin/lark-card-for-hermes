"""内置 Feishu 平台条目与适配器类：插件唯一的私有依赖面。

为什么还要依赖内置适配器：卡片能力是「替换出站的两个原语（send / edit_message）」，而
配对、允许名单、准入、环境变量桥、dashboard、setup 这些规则都声明在内置插件自己的
`register()` 里。本插件**按文件直接加载内置插件、把它 register() 声明的条目 kwargs 原样录下**，
只把 `adapter_factory` 换成本插件的子类，因此这些规则与上游逐字段一致（不新增
trusted_inbound，不改准入）。

为什么不用 `platform_registry.get("feishu")` 读已注册条目：feishu 是 **deferred** 条目，
`get()` 会替我们跑它的 deferred loader，而 loader 要拿 discovery 锁——本插件自己的加载
deadline 期间，这把锁正被父线程的 `worker.join()` 持有 → 必然死等到 10s 超时、全部注册
被拒（2026-11-06 网关实测）。读已注册条目只允许在加载完成之后（见 `registered_entry`
仅供诊断）。

支持范围：行为测试跑在 `TESTED_HERMES` 上；超范围不保证，但失效形态是「卡片降级为原生
文本」，消息照常送达。
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 版本协调：这是**已跑过本仓库 tests/ 的**范围，不是「只在这段内可用」的硬门禁。
TESTED_HERMES = ">=0.22.7,<0.24"

_BASE_PACKAGE = "hermes_feishu_bundled"


def _bundled_feishu_root() -> Path:
    """定位内置 feishu 插件目录；多源兜底，降低 Hermes 内部包布局变动的风险。"""
    candidates: list[Path] = []

    # 1. 优先用运行时代码树（gateway 在哪，插件树就在哪）
    for mod_name in ("gateway", "hermes_cli", "hermes_agent"):
        try:
            mod = importlib.import_module(mod_name)
            file = getattr(mod, "__file__", None)
            if file:
                base = Path(file).resolve().parent
                # gateway/ 和 hermes_cli/ 平级；hermes_agent/ 可能在根下
                roots = [base.parent, base] if mod_name == "hermes_agent" else [base.parent]
                for root in roots:
                    candidates.append(root / "plugins" / "platforms" / "feishu")
        except Exception:
            continue

    # 2. 从 Hermes home 推断（稳定兜底）
    try:
        from hermes_constants import get_hermes_home
        candidates.append(get_hermes_home() / "hermes-agent" / "plugins" / "platforms" / "feishu")
    except Exception:
        pass

    for root in candidates:
        init = root / "__init__.py"
        if init.exists():
            return root

    # 3. 最后才 glob：包布局变了（plugins 换层级/多版本共存）也能找到。
    #    只用浅层模式，不用 ``**``——那会在 hermes-agent（含 node_modules）里递归，
    #    实测白付 0.4s 启动时间。
    try:
        from hermes_constants import get_hermes_home
        home: Path | None = Path(get_hermes_home()) / "hermes-agent"
    except Exception:
        home = Path.home() / ".hermes" / "hermes-agent"
    if home is not None and home.exists():
        for pattern in ("plugins/platforms/feishu", "plugins/*/platforms/feishu",
                        "*/plugins/platforms/feishu"):
            try:
                for hit in sorted(home.glob(pattern))[:1]:
                    if (hit / "__init__.py").exists():
                        return hit
            except OSError:
                continue

    raise ImportError(
        f"cannot locate bundled feishu plugin; tried: {', '.join(str(r) for r in candidates)}"
    )


def adapter_module() -> Any:
    """导入内置 feishu 插件（私有包名下，避免与网关自己的模块冲突）。"""
    if _BASE_PACKAGE not in sys.modules:
        root = _bundled_feishu_root()
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


class _RegistrationRecorder:
    """录下内置插件 register() 声明的平台 kwargs；其他 register_* 调用一律吞掉不外传。

    内置插件的 register() 是**纯声明**（组装 kwargs → 调一次 ctx.register_platform），
    对它录音即可拿到与上游完全一致的字段集，且全程不碰注册表/发现锁。
    """

    __slots__ = ("platforms",)

    def __init__(self) -> None:
        self.platforms: list[dict[str, Any]] = []

    def register_platform(self, *args: Any, **kwargs: Any) -> None:
        # 上游签名：register_platform(name, label, adapter_factory, check_fn, …)——
        # 目前内置 feishu 全用关键字；位置参数只兜前四个。
        if args:
            kwargs = {**dict(zip(("name", "label", "adapter_factory", "check_fn"), args)),
                      **kwargs}
        self.platforms.append(kwargs)

    def __getattr__(self, _name: str):   # register_hook/register_command/…：空操作
        def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None
        return _noop


def _scan_register_calls(root: Path):
    """在插件包里找出 ``def register(ctx)`` 及其 ``ctx.register_platform(...)`` 调用。

    上游把 register() 放在 ``adapter.py``（不是 ``__init__.py``），并且 kwargs 大量用
    ``**SEND_ENTRY_KWARGS`` / ``**DASHBOARD_CARD`` / ``**gateway_env.AUTHZ`` 展开，
    所以只看 __init__ 或拒收 ``**`` 都等于没有兜底。这里逐个顶层模块扫。
    """
    for path in sorted(root.glob("*.py"))[:80]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        mod_name = _BASE_PACKAGE if path.stem == "__init__" else f"{_BASE_PACKAGE}.{path.stem}"
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "register":
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "register_platform"):
                    yield node, inner, mod_name


def _local_imports(func: ast.AST) -> dict[str, tuple[str, Optional[str]]]:
    """函数体内的相对 import → {``本地名``: (子模块, 属性或 None)}。

    ``from .send_targets import SEND_ENTRY_KWARGS`` → ("send_targets", "SEND_ENTRY_KWARGS")
    ``from . import gateway_env``                  → ("gateway_env", None)
    """
    found: dict[str, tuple[str, Optional[str]]] = {}
    for node in ast.walk(func):
        if not (isinstance(node, ast.ImportFrom) and node.level):
            continue
        for alias in node.names:
            name = alias.asname or alias.name
            if node.module:
                found[name] = (node.module, alias.name)
            else:                      # from . import x  → x 本身是子模块
                found[name] = (alias.name, None)
    return found


def _static_platform_kwargs() -> Optional[dict[str, Any]]:
    """静态兜底：AST 解析内置插件 ``register()`` 里的 ``ctx.register_platform(...)``。

    动态录音要求 ``adapter.register(recorder)`` 能跑完；它可能因为配置/环境变化抛错。
    这里退一步只做静态解析：字面量直接取值，``Name``/``Attribute`` 用模块命名空间与
    函数内 ``from .x import Y`` 解析，``**mapping`` 解析成 dict 后合并。任何解析不了的
    表达式整体失败返回 None，**绝不猜准入字段**。
    """
    try:
        root = _bundled_feishu_root()
        adapter_module()      # 先让 `hermes_feishu_bundled` 进 sys.modules，否则子模块 import 不动
    except ImportError:
        return None

    pick: Optional[tuple[ast.AST, ast.Call, str]] = None
    for func, call, mod_name in _scan_register_calls(root):
        pick = (func, call, mod_name)
        break
    if pick is None:
        return None
    func, call_node, mod_name = pick

    def _module(name: str) -> Any:
        try:
            return importlib.import_module(name)
        except Exception:
            return None

    # mod_name 为包本身时（合成/测试布局）直接用 adapter_module() 的结果做解析底座
    base_module = adapter_module() if mod_name == _BASE_PACKAGE else _module(mod_name)
    if base_module is None:
        return None          # 包导不进来时连字段语义都无法确认：放弃而不是猜
    imports = _local_imports(func)

    def _resolve(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Dict):
            out: dict[str, Any] = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    raise LookupError("dict-splat")
                out[str(_resolve(key))] = _resolve(value)
            return out
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [_resolve(elt) for elt in node.elts]
        if isinstance(node, ast.Name):
            if node.id in imports:
                sub, attr = imports[node.id]
                sub_mod = _module(f"{_BASE_PACKAGE}.{sub}")
                if sub_mod is None:
                    raise LookupError(node.id)
                if attr is None:
                    return sub_mod
                if not hasattr(sub_mod, attr):
                    raise LookupError(node.id)
                return getattr(sub_mod, attr)
            if hasattr(base_module, node.id):
                return getattr(base_module, node.id)
            raise LookupError(node.id)
        if isinstance(node, ast.Attribute):
            parts = _dotted(node).split(".")
            head = parts[0]
            if head in imports:
                sub, attr = imports[head]
                sub_mod = _module(f"{_BASE_PACKAGE}.{sub}")
                if sub_mod is None:
                    raise LookupError(head)
                if attr is None:          # from . import gateway_env
                    base: Any = sub_mod
                    parts = parts[1:]
                else:
                    base = getattr(sub_mod, attr, None)
                    parts = parts[1:]
            elif hasattr(base_module, head):
                base = getattr(base_module, head)
                parts = parts[1:]
            else:
                raise LookupError(head)
            for part in parts:
                if base is None or not hasattr(base, part):
                    raise LookupError(part)
                base = getattr(base, part)
            return base
        raise LookupError(type(node).__name__)

    def _dotted(node: ast.Attribute) -> str:
        parts = [node.attr]
        cur = node.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))

    kwargs: dict[str, Any] = {}
    try:
        for kw in call_node.keywords:
            if kw.arg:
                kwargs[kw.arg] = _resolve(kw.value)
            else:                       # **mapping
                spread = _resolve(kw.value)
                if not isinstance(spread, dict):
                    raise LookupError("spread-not-dict")
                kwargs.update(spread)
    except LookupError as exc:
        logger.debug("[lark-card] static register_platform resolve failed: %s", exc)
        return None

    if positionals := getattr(call_node, "args", []):
        names = ("name", "label", "adapter_factory", "check_fn")
        for key, node in zip(names, positionals):
            kwargs.setdefault(key, _resolve(node))

    if str(kwargs.get("name") or "") != "feishu" or "adapter_factory" not in kwargs:
        return None
    kwargs.pop("trusted_inbound", None)
    return kwargs


def recorded_platform_kwargs() -> Optional[dict[str, Any]]:
    """加载路径唯一入口：按文件加载内置插件并录下它的条目 kwargs（零锁、零注册表）。

    返回 None = 内置插件缺失/声明变了：调用方必须放弃注册并降级为原文本，不猜造字段。
    """
    try:
        adapter = adapter_module()
        recorder = _RegistrationRecorder()
        adapter.register(recorder)
    except Exception as exc:
        logger.warning("[lark-card] bundled feishu registration could not be recorded: %s", exc)
        static_kwargs = _static_platform_kwargs()
        if static_kwargs is not None:
            logger.info("[lark-card] 已改用静态解析 register_platform 声明（动态录音失败兜底）")
        return static_kwargs
    matches = [k for k in recorder.platforms if k.get("name") == "feishu"]
    if len(matches) != 1:
        logger.warning("[lark-card] expected exactly one feishu platform declaration, got %d",
                       len(matches))
        return _static_platform_kwargs()
    kwargs = dict(matches[0])
    kwargs.pop("trusted_inbound", None)   # 永不豁免准入（公开 API 对 user 插件也会拒）
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
