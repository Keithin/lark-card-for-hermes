"""Local tool-argument preview (fallback for the private ``agent.display`` helper).

Hermes owns the canonical one-line tool preview used by the native CLI. Importing it
is a private-API dependency, so this module keeps a small, dependency-free
implementation with the same job: turn a tool name + args mapping into one short,
redacted line. It is only used when the core helper is unavailable *or* returned
nothing, so upstream behaviour still wins when it exists.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

_PRIMARY_KEYS = (
    "command", "cmd", "script", "code", "query", "q", "url", "path", "file_path",
    "pattern", "goal", "prompt", "text", "name", "to", "subject",
)
_SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|auth|cookie|session)", re.I)
_LONG_VALUE_KEYS = {"code", "script", "prompt", "text", "command", "query"}


def _shorten(value: Any, limit: int) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)) or value is None:
        return json.dumps(value) if not isinstance(value, str) else value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def tool_preview(tool_name: str, args: Any, *, max_len: int = 48) -> str:
    """One-line preview of ``args`` for ``tool_name``; empty string when nothing useful."""
    if not isinstance(args, Mapping) or not args:
        return ""
    name = (tool_name or "").strip().lower()

    ordered: list[tuple[str, Any]] = []
    for key in _PRIMARY_KEYS:
        if key in args:
            ordered.append((key, args[key]))
    for key, value in args.items():
        if key not in {k for k, _ in ordered}:
            ordered.append((key, value))

    parts: list[str] = []
    for key, value in ordered:
        if _SECRET_KEY_RE.search(str(key)):
            parts.append(f"{key}=***")
            continue
        rendered = _shorten(value, 32 if key in _LONG_VALUE_KEYS else 24)
        if not rendered:
            continue
        # 单参数工具（terminal/code 类）直接给值，多参数才带 key=
        parts.append(rendered if len(ordered) == 1 else f"{key}={rendered}")
        if len(", ".join(parts)) >= max_len:
            break
    joined = ", ".join(parts)
    if not joined:
        return ""
    if name and not joined.lower().startswith(name.lower()) and len(ordered) == 1:
        return _trim(joined, max_len)
    return _trim(joined, max_len)


def _trim(text: str, max_len: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= max_len else text[: max(1, max_len - 1)] + "…"
