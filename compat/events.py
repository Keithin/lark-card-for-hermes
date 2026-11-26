"""Read task-local gateway routing; unavailable routing never means 'the only active chat'."""
from __future__ import annotations


def event_route(platform: str, session_key: str) -> tuple[str, str]:
    if platform and platform != "feishu":
        return platform, ""
    try:
        from gateway.session_context import bound_session_env
        context_platform = bound_session_env("HERMES_SESSION_PLATFORM")
        context_key = bound_session_env("HERMES_SESSION_KEY")
    except ImportError:
        # v0.23.3 lacks bound_session_env; ContextVars still carry the exact task route.
        from contextvars import copy_context
        values = {var.name: value for var, value in copy_context().items()}
        context_platform = values.get("HERMES_SESSION_PLATFORM", "")
        context_key = values.get("HERMES_SESSION_KEY", "")
        context_platform = context_platform if isinstance(context_platform, str) else ""
        context_key = context_key if isinstance(context_key, str) else ""
    if context_platform and context_platform != "feishu":
        return context_platform, ""
    if session_key:
        return platform, session_key
    return platform or context_platform, context_key if context_platform == "feishu" else ""
