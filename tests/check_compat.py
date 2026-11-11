#!/usr/bin/env python3
"""判据：私有依赖降级路径（不靠 Hermes 内部 API 也要能用）。

覆盖三条我们无法控制的私有面：
  1. ``agent.display.build_tool_preview`` 缺失 → 本地 `hfc.preview` 必须给出可用摘要；
  2. ``lark-oapi`` 的 CARD 帧修补 → 相邻代码变了（哈希不匹配）仍要按结构命中；
  3. ``compat.recorded_platform_kwargs`` 动态录音失败 → 静态解析 register_platform 声明；
     以及 ``hermes_cli.commands`` 不可导入 → 静态读 COMMAND_REGISTRY。
"""
from pathlib import Path
import sys
import textwrap
import types

REPO_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO_ROOT)

from hfc.preview import tool_preview  # noqa: E402
from hfc.feishu_ws_compat import (  # noqa: E402
    ALREADY, REPAIRED, UNSUPPORTED, card_frame_repair_status,
)


def _fake_client(source: str) -> types.ModuleType:
    """在伪 ``lark_oapi.ws.client`` 模块里编译一段 Client（带真实源文件，便于 inspect.getsource）。"""
    import linecache
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="larkfake-")) / "client.py"
    text = textwrap.dedent(source)
    path.write_text(text, encoding="utf-8")
    linecache.cache[str(path)] = (len(text), None, text.splitlines(keepends=True), str(path))

    module = types.ModuleType("lark_oapi.ws.client")
    module.__file__ = str(path)
    module.__dict__.update({"MessageType": type("MessageType", (), {
        "EVENT": "event", "CARD": "card"})(), "http": types.SimpleNamespace(HTTPStatus=types.SimpleNamespace(OK=200)),
        "time": __import__("time"), "base64": __import__("base64"), "JSON": types.SimpleNamespace(),
        "logger": types.SimpleNamespace(debug=lambda *a, **k: None, error=lambda *a, **k: None),
        "UTF_8": "utf-8", "HEADER_MESSAGE_ID": "m", "HEADER_TRACE_ID": "t",
        "HEADER_SUM": "s", "HEADER_SEQ": "q", "HEADER_TYPE": "ty", "HEADER_BIZ_RT": "biz",
        "Response": lambda **kw: types.SimpleNamespace(**kw), "Frame": object,
        "_get_by_key": lambda hs, key: "0"})
    module.__dict__["__name__"] = "lark_oapi.ws.client"
    exec(compile(text, str(path), "exec"), module.__dict__)
    sys.modules["lark_oapi"] = sys.modules.get("lark_oapi") or types.ModuleType("lark_oapi")
    sys.modules["lark_oapi.ws"] = sys.modules.get("lark_oapi.ws") or types.ModuleType("lark_oapi.ws")
    sys.modules["lark_oapi.ws.client"] = module
    return module


_DROP_SOURCE = '''
class Client:
    async def _handle_data_frame(self, frame: Frame):
        hs = frame.headers
        pl = frame.payload
        message_type = MessageType(frame.type)
        # 这行是「SDK 补丁版」新增的噪音，函数体哈希因此对不上
        extra_trace = len(str(pl))
        if message_type == MessageType.EVENT:
            result = self._event_handler._do_without_validation(pl)
        elif message_type == MessageType.CARD:
            return
        else:
            return
        return result
'''


def _client_with(source: str):
    module = _fake_client(source)
    client_cls = module.Client
    instance = client_cls.__new__(client_cls)
    instance._event_handler = types.SimpleNamespace(_do_without_validation=lambda pl: {"ok": True})
    method = client_cls._handle_data_frame
    return getattr(method, "__func__", method), instance


def check_preview_fallback():
    text = tool_preview("terminal", {"command": "cd /tmp && ls -la"}, max_len=48)
    assert text.startswith("cd /tmp"), text
    long = tool_preview("terminal", {"command": "x" * 200}, max_len=30)
    assert len(long) <= 30 and long.endswith("…"), long
    redacted = tool_preview("http", {"url": "http://x", "api_key": "sk-123"}, max_len=60)
    assert "sk-123" not in redacted and "***" in redacted, redacted
    assert tool_preview("terminal", {}) == ""


def check_structural_repair():
    original, instance = _client_with(_DROP_SOURCE)
    assert original.__module__ == "lark_oapi.ws.client"
    status = card_frame_repair_status(original)
    assert status == REPAIRED, f"相邻代码变动后仍应按结构命中，实际={status}"


def check_already_ok():
    source = '''
class Client:
    async def _handle_data_frame(self, frame: Frame):
        message_type = MessageType(frame.type)
        if message_type in (MessageType.EVENT, MessageType.CARD):
            result = self._event_handler._do_without_validation(pl)
        return result
'''
    original, _ = _client_with(source)
    assert card_frame_repair_status(original) == ALREADY, "SDK 已处理 CARD 时应报 already_ok"


def check_unsupported():
    source = '''
class Client:
    async def _handle_data_frame(self, frame: Frame):
        message_type = MessageType(frame.type)
        if message_type == MessageType.CARD:
            logger.debug("card frame")
        return None
'''
    original, _ = _client_with(source)
    assert card_frame_repair_status(original) == UNSUPPORTED, "CARD 分支在做真事时不得改写"


def check_static_platform_kwargs(tmp: Path):
    from compat import platform as compat_platform

    root = tmp / "feishu"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(textwrap.dedent('''
        class FeishuAdapter:
            pass

        def _check():
            return True

        def register(ctx):
            ctx.register_hook("noop", lambda *a, **k: None)
            ctx.register_platform(
                name="feishu",
                label="飞书",
                adapter_factory=FeishuAdapter,
                check_fn=_check,
                allowed_users_env="FEISHU_ALLOWED_USERS",
                allow_all_env="FEISHU_ALLOW_ALL",
                allowlist_suppresses_pairing=True,
            )
    '''), encoding="utf-8")

    module = types.ModuleType("fake_bundled_feishu")
    exec(compile((root / "__init__.py").read_text(encoding="utf-8"), "fake", "exec"), module.__dict__)

    original_root, original_module = compat_platform._bundled_feishu_root, compat_platform.adapter_module
    compat_platform._bundled_feishu_root = lambda: root
    compat_platform.adapter_module = lambda: module
    try:
        kwargs = compat_platform._static_platform_kwargs()
    finally:
        compat_platform._bundled_feishu_root, compat_platform.adapter_module = original_root, original_module
    assert kwargs is not None, "静态解析应成功"
    assert kwargs["name"] == "feishu" and kwargs["adapter_factory"] is module.FeishuAdapter, kwargs
    assert kwargs["allowed_users_env"] == "FEISHU_ALLOWED_USERS", kwargs
    assert "trusted_inbound" not in kwargs


def check_static_registry(tmp: Path):
    from hfc import native_commands

    fixture = tmp / "commands.py"
    fixture.write_text(textwrap.dedent('''
        COMMAND_REGISTRY: list[CommandDef] = [
            CommandDef("start", "Acknowledge start pings", "Session", aliases=("hi",)),
            CommandDef("cli-only-thing", "CLI only", "Info", cli_only=True),
            CommandDef("model", "Switch model", "Configuration", args_hint="<name>"),
        ]
    '''), encoding="utf-8")
    native_commands._static_registry_entries.cache_clear()
    original = native_commands._commands_source_path
    native_commands._commands_source_path = lambda: fixture
    try:
        entries = native_commands._static_registry_entries()
    finally:
        native_commands._commands_source_path = original
        native_commands._static_registry_entries.cache_clear()
    names = [e["name"] for e in entries]
    assert names == ["start", "model"], names
    assert list(entries[0]["aliases"]) == ["hi"], entries[0]
    assert entries[0]["source"] == "core-static"


def check_dynamic_failure_uses_static():
    """动态录音抛错时，静态解析必须真的接过注册声明（不是空实现）。"""
    from compat import platform as cp

    # 静态兜底与动态录音有同一个前置条件：内置插件包本身要能 import。
    # 导不进来（例如测试用的 venv 里没有 `pm`）时两条路径同时不可用 → 跳过，不算失败。
    try:
        cp.adapter_module()
    except Exception:
        return
    original = cp._RegistrationRecorder.register_platform

    def boom(self, *args, **kwargs):
        raise RuntimeError("recorder exploded")

    cp._RegistrationRecorder.register_platform = boom
    try:
        kwargs = cp.recorded_platform_kwargs()
    finally:
        cp._RegistrationRecorder.register_platform = original
    assert kwargs, "动态失败后静态兜底也必须给出声明"
    assert kwargs.get("name") == "feishu", kwargs.get("name")
    assert "adapter_factory" in kwargs and "trusted_inbound" not in kwargs
    # 与动态录音的字段集一致（同一份声明，只是解析路径不同）
    dynamic = cp.recorded_platform_kwargs()
    if dynamic:
        assert set(kwargs) == set(dynamic), (sorted(set(dynamic) - set(kwargs)), sorted(set(kwargs) - set(dynamic)))


def main() -> int:
    import tempfile

    check_preview_fallback()
    check_structural_repair()
    check_already_ok()
    check_unsupported()
    check_dynamic_failure_uses_static()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        check_static_platform_kwargs(tmp)
        check_static_registry(tmp)
    print("compat fallbacks ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
