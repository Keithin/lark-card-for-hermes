"""运行态配置 vs 磁盘配置的快照与差异（借鉴 HFC config_snapshot.py）。

插件配置多数键只在注册时读一次，改完必须重启才生效；这个模块让 /card-status
直接回答「哪些键改了但还没生效」，不用再靠猜。
"""
from __future__ import annotations

import copy
import json
from hashlib import sha256
from typing import Any, Mapping


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """把嵌套配置压成 ``a.b.c -> value`` 的扁平表，标量/list 当叶子。"""
    out: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten(value[key], path))
    elif isinstance(value, (list, tuple)):
        out[prefix] = list(value)
    else:
        out[prefix] = value
    return out


def fingerprint(value: Any) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(blob.encode("utf-8")).hexdigest()[:12]


def diff(loaded: Mapping[str, Any], disk: Mapping[str, Any]) -> list[dict[str, Any]]:
    """返回 ``[{key, loaded, disk}]``：只列真变了的键（含只在一边出现的键）。"""
    flat_loaded = flatten(loaded)
    flat_disk = flatten(disk)
    changed: list[dict[str, Any]] = []
    for key in sorted(set(flat_loaded) | set(flat_disk)):
        a = flat_loaded.get(key, "<absent>")
        b = flat_disk.get(key, "<absent>")
        if a != b:
            changed.append({"key": key, "loaded": a, "disk": b})
    return changed


def format_diff(entries: list[dict[str, Any]], *, limit: int = 10) -> str:
    if not entries:
        return "配置：运行态 = 磁盘（无需重启）"
    head = f"配置差异 {len(entries)} 项（需重启才生效）"
    lines = []
    for item in entries[:limit]:
        lines.append(f"· {item['key']}：运行 {item['loaded']!r} → 磁盘 {item['disk']!r}")
    if len(entries) > limit:
        lines.append(f"· …还有 {len(entries) - limit} 项")
    return head + "\n" + "\n".join(lines)


class ConfigLedger:
    """记录注册时读到的配置副本，之后任何时刻都能和磁盘对比。"""

    def __init__(self) -> None:
        self._loaded: dict[str, Any] | None = None
        self._fingerprint = ""

    def capture(self, config: Mapping[str, Any]) -> None:
        self._loaded = copy.deepcopy(dict(config))
        self._fingerprint = fingerprint(self._loaded)

    @property
    def captured(self) -> bool:
        return self._loaded is not None

    @property
    def loaded(self) -> dict[str, Any]:
        return copy.deepcopy(self._loaded or {})

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def diff_against(self, disk: Mapping[str, Any]) -> list[dict[str, Any]]:
        if self._loaded is None:
            return []
        return diff(self._loaded, disk)

    def summary(self, disk: Mapping[str, Any], *, limit: int = 10) -> str:
        return format_diff(self.diff_against(disk), limit=limit)


LEDGER = ConfigLedger()
