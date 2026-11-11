"""Narrow, instance-local repair for the SDK's verified CARD-frame drop path.

No SDK files or global Client class are changed. Unknown implementations remain
untouched; the ordinary SDK controls fragmentation, response encoding and ACKs.

Three tiers, most specific first:

1. ``exact``     — body AST hash matches the lark-oapi 1.6.8/1.7.5 shape.
2. ``structural``— the CARD branch exists, is a plain drop (``return`` / ``pass``),
                   but neighbouring code changed (SDK patch release). This is the
                   tier that keeps working across SDK upgrades instead of
                   abstaining on a byte mismatch.
3. ``already_ok``— the SDK no longer drops CARD frames (no branch to patch).
   ``unsupported``— unrecognised shape: abstain and let the caller log loudly.
"""
from __future__ import annotations

import ast
from functools import lru_cache
import hashlib
import inspect
import sys
import textwrap
from types import MethodType
from typing import Any

# Body AST of lark-oapi 1.6.8 Client._handle_data_frame. Fast path only: a mismatch
# falls through to the structural tier instead of giving up.
_DROP_BODY_SHA256 = "78ec17eb4a9538e7e3f93fba28c48133303c86df64eb772114d13df68355b858"

REPAIRED = "repaired"
ALREADY = "already_ok"
UNSUPPORTED = "unsupported"
_EXPECTED_TEST = "message_type == MessageType.CARD"
_DISPATCH_STMT = "result = self._event_handler._do_without_validation(pl)"


def _is_drop_body(body: list[ast.stmt]) -> bool:
    """True when this branch only drops the frame (``return`` / ``pass`` / docstring)."""
    if not body:
        return True
    for node in body:
        if isinstance(node, ast.Pass):
            continue
        if isinstance(node, ast.Return) and node.value is None:
            continue
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        return False
    return True


def _card_branches(function: ast.AST) -> list[ast.If]:
    want = ast.dump(ast.parse(_EXPECTED_TEST, mode="eval").body)
    return [n for n in ast.walk(function) if isinstance(n, ast.If) and ast.dump(n.test) == want]


def _body_digest(body: list[ast.stmt]) -> str:
    dump_options = {"show_empty": True} if sys.version_info >= (3, 13) else {}
    blob = "\n".join(ast.dump(n, include_attributes=False, **dump_options) for n in body)
    return hashlib.sha256(blob.encode()).hexdigest()


def _build(tree: ast.Module, function: ast.AsyncFunctionDef, original: Any) -> Any:
    ast.fix_missing_locations(tree)
    namespace = dict(original.__globals__)
    exec(compile(tree, "<hfc-feishu-card-frame-compat>", "exec"), namespace)  # noqa: S102
    repaired = namespace[function.name]
    repaired._hfc_card_frame_repair = True
    return repaired


@lru_cache(maxsize=8)
def _analyse(original: Any) -> tuple[str, Any]:
    """Return ``(status, repaired_function_or_None)`` for the given SDK method."""
    if (not inspect.iscoroutinefunction(original)
            or original.__module__ != "lark_oapi.ws.client"
            or original.__qualname__ != "Client._handle_data_frame"
            or original.__closure__):
        return UNSUPPORTED, None
    try:
        source = textwrap.dedent(inspect.getsource(original))
        tree = ast.parse(source)
        function = tree.body[0]
        if (not isinstance(function, ast.AsyncFunctionDef)
                or ast.dump(function.args) != ast.dump(ast.parse(
                    "async def f(self, frame: Frame): pass").body[0].args)):
            return UNSUPPORTED, None
    except (OSError, TypeError, ValueError, SyntaxError):
        return UNSUPPORTED, None

    branches = _card_branches(function)
    if not branches:
        # SDK already routes CARD frames somewhere: nothing to patch, but only claim
        # "already ok" when it actually mentions CARD (otherwise the shape is unknown).
        mentions = "MessageType.CARD" in source
        return (ALREADY, None) if mentions else (UNSUPPORTED, None)

    if len(branches) != 1:
        return UNSUPPORTED, None
    branch = branches[0]
    if not _is_drop_body(branch.body):
        # Branch does real work: either already patched by someone else, or a shape we
        # must not touch. Both are reported as "already ok" only if it dispatches.
        if "do_without_validation" in ast.dump(branch):
            return ALREADY, None
        return UNSUPPORTED, None

    exact = _body_digest(function.body) == _DROP_BODY_SHA256
    # 已知形态做 exact 校验时只做记录：结构命中即可安全替换，避免 SDK 补丁版被误拒。
    branch.body = ast.parse(_DISPATCH_STMT).body
    try:
        repaired = _build(tree, function, original)
    except (SyntaxError, TypeError, ValueError, KeyError, NameError):
        return UNSUPPORTED, None
    repaired._hfc_card_frame_repair_exact = exact
    return REPAIRED, repaired


def card_frame_repair_status(original: Any) -> str:
    return _analyse(original)[0]


def _repaired_function(original: Any) -> Any:
    return _analyse(original)[1]


def card_frame_dispatch_needs_repair(client: Any) -> bool:
    method = getattr(client, "_handle_data_frame", None)
    original = getattr(method, "__func__", None)
    if original is None:
        return False
    return _analyse(original)[0] == REPAIRED


def repair_card_frame_dispatch(client: Any) -> bool:
    method = getattr(client, "_handle_data_frame", None)
    original = getattr(method, "__func__", None)
    if original is None:
        return False
    status, repaired = _analyse(original)
    if status != REPAIRED or repaired is None:
        return False
    client._handle_data_frame = MethodType(repaired, client)
    return True
