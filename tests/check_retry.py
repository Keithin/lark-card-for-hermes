#!/usr/bin/env python3
"""判据：瞬时失败在同一张卡上重试，回落只在「确定没发出去」时发生。

背景（2026-11-05）：`Server disconnected` 一次就把整张卡降级成 v1（标题只剩 "Hermes"）。
  回落是兜底，不是常态 —— 参考实现 feishu_client.py 的策略是 3 次 / 0.4s→1.2s 退避，
  网络异常与 429/502/503/504 都算 retryable。
"""
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parents[1])
import asyncio
import importlib.util
import sys
from pathlib import Path

import aiohttp

P = REPO_ROOT
sys.path.insert(0, P)
spec = importlib.util.spec_from_file_location("lcf", f"{P}/__init__.py")
m = importlib.util.module_from_spec(spec)
sys.modules["lcf"] = m
spec.loader.exec_module(m)

from hfc.client import RestClient, _retry_after_seconds, _retry_delay  # noqa: E402
from hfc.delivery import FeishuAPIError  # noqa: E402



class _Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {"code": 0, "data": {"message_id": "om_1"}}
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """前 N 次按队列抛出/返回，之后一律成功。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def request(self, method, url, json=None, headers=None):
        self.calls += 1
        step = self.script.pop(0) if self.script else _Resp()
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def closed(self):
        return False


def _client(script, *, token="t"):
    c = RestClient("id", "secret")
    c._session = _Session(script)
    c.closed = False
    if token:                      # 预置 token，脚本只脚本化被测的那一次请求
        c._token, c._expires_at = token, float("inf")
    return c


# 1) 卡片实体（幂等）遇瞬时断连：重试后成功，不抛错
c = _client([aiohttp.ServerDisconnectedError(), _Resp()])
payload = asyncio.run(c._request_json("POST", "/cardkit/v1/cards", token="t", json_body={}))
assert payload["data"]["message_id"] == "om_1"
assert c._session.calls == 2, c._session.calls

# 2) 元素变更（幂等）429 → 重试后成功
c = _client([_Resp(status=429, payload={"code": 99991400, "msg": "rate"}, headers={"Retry-After": "1"}),
             _Resp()])
asyncio.run(c._request_json("PATCH", "/cardkit/v1/cards/c1/elements/e1/content", token="t", json_body={}))
assert c._session.calls == 2

# 3) IM 发消息遇「结果未知」断连：不重发（否则同回合两张卡），抛 outcome=unknown
c = _client([aiohttp.ServerDisconnectedError(), _Resp()])
try:
    asyncio.run(c.send_card_delivery("oc_x", {"a": 1}))
    raise AssertionError("unknown outcome must not be retried")
except FeishuAPIError as exc:
    assert exc.outcome == "unknown", exc.outcome
assert c._session.calls == 1, c._session.calls

# 4) 业务错误（卡片非法/权限）不重试，outcome=not_sent
c = _client([_Resp(payload={"code": 230099, "msg": "invalid card"})])
try:
    asyncio.run(c._request_json("POST", "/cardkit/v1/cards", token="t", json_body={}))
    raise AssertionError("business error must raise")
except FeishuAPIError as exc:
    assert exc.retryable is False and exc.outcome == "not_sent", (exc.retryable, exc.outcome)
assert c._session.calls == 1

# 5) 重试上限：连续瞬时失败共 3 次尝试后放弃
c = _client([aiohttp.ServerDisconnectedError() for _ in range(5)])
try:
    asyncio.run(c._request_json("POST", "/cardkit/v1/cards", token="t", json_body={}))
    raise AssertionError("must give up eventually")
except FeishuAPIError:
    pass
assert c._session.calls == 3, c._session.calls

# 6) 退避参数与 HFC 一致
assert _retry_after_seconds("5") == 2.0 and _retry_after_seconds("junk") is None
assert _retry_delay(0, None) == 0.4 and _retry_delay(1, None) == 1.2

# 7) 适配器：outcome=unknown 不允许回落成第二张卡；确定性失败才回落
assert m._outcome_unknown(FeishuAPIError("x", outcome="unknown")) is True
assert m._outcome_unknown(FeishuAPIError("x", outcome="not_sent")) is False
assert m._outcome_unknown(RuntimeError("boom")) is False

print("OK retry: 瞬时失败同卡重试 3 次/退避 0.4-1.2s，unknown 不回落，业务错误直接回落")
