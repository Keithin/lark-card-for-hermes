"""CardKitTransport 需要的三个 REST 方法，用内置适配器已有的凭据实现。

HFC 在自己的 FeishuClient 上做同样三件事；这里只补接口，不重写任何卡片逻辑。

重试策略照抄 HFC 的 `feishu_client.py`：瞬时失败（网络异常、429/502/503/504）在
**同一张卡上重试**，只有确定性的业务失败才交给上层回落。回落是兜底，不是常态：
一次 "Server disconnected" 就降级成另一张 v1 卡，等于把瞬时抖动变成用户可见的形状变化。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp

from .delivery import FeishuAPIError

_API = {"feishu": "https://open.feishu.cn/open-apis", "lark": "https://open.larksuite.com/open-apis"}

# HFC `feishu_client.py`: _RETRYABLE_HTTP_STATUSES / _SEND_MAX_ATTEMPTS /
# _SEND_RETRY_DELAYS_SECONDS / _MAX_RETRY_AFTER_SECONDS
_RETRYABLE_HTTP_STATUSES = {429, 502, 503, 504}
_MAX_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (0.4, 1.2)
_MAX_RETRY_AFTER_SECONDS = 2.0


def _retry_after_seconds(value: str | None) -> float | None:
    try:
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if seconds is None or seconds < 0:
        return None
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    configured = _RETRY_DELAYS_SECONDS[min(attempt, len(_RETRY_DELAYS_SECONDS) - 1)]
    if retry_after is None:
        return configured
    return min(max(retry_after, configured), _MAX_RETRY_AFTER_SECONDS)


class RestClient:
    """im/cardkit 的最小 REST 通道：tenant token 缓存 + JSON 请求 + 卡片消息投递。"""

    def __init__(self, app_id: str, app_secret: str, *, domain: str = "feishu") -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._api = _API.get(domain, _API["feishu"])
        self._session: aiohttp.ClientSession | None = None
        self._token = ""
        self._expires_at = 0.0

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def _tenant_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at - 60:
            return self._token
        data = await self._request_json(
            "POST", "/auth/v3/tenant_access_token/internal",
            json_body={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        self._token = str(data["tenant_access_token"])
        self._expires_at = time.monotonic() + float(data.get("expire", 7200))
        return self._token

    async def _request_json(self, method: str, path: str, *, token: str = "",
                            json_body: Any = None) -> dict[str, Any]:
        """一次 API 调用，瞬时失败在同一请求内重试（HFC 同款 3 次 / 0.4s→1.2s 退避）。

        幂等操作（token、卡片实体、元素变更）任何可重试失败都重试；IM 发消息只在
        「确定没发出去」（连接建立失败/显式 429·5xx）时重试，避免一条回合发出两张卡。
        """
        idempotent = not path.startswith("/im/")
        attempt = 0
        while True:
            session = await self._http()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                async with session.request(method, f"{self._api}{path}",
                                           json=json_body, headers=headers) as resp:
                    payload = await resp.json(content_type=None)
                    status = resp.status
                    retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                if not isinstance(payload, dict):
                    raise FeishuAPIError(f"{method} {path}: non-object response",
                                         status_code=status, retryable=False, outcome="unknown")
                if status >= 400:
                    retryable = status in _RETRYABLE_HTTP_STATUSES
                    raise FeishuAPIError(
                        f"{method} {path}: HTTP {status}", status_code=status,
                        api_code=(payload or {}).get("code"), retryable=retryable,
                        outcome="unknown" if retryable else "not_sent",
                        retry_after_seconds=retry_after)
                if payload.get("code") != 0:
                    # 业务错误（卡片非法、权限、配额）：重试没有意义，交给回落链
                    raise FeishuAPIError(f"{method} {path}: {payload.get('msg')}",
                                         status_code=status, api_code=payload.get("code"),
                                         retryable=False, outcome="not_sent")
                return payload
            except FeishuAPIError as exc:
                retryable = exc.retryable and (idempotent or exc.outcome != "unknown")
                if not retryable or attempt + 1 >= _MAX_ATTEMPTS:
                    raise FeishuAPIError(
                        str(exc), status_code=exc.status_code, api_code=exc.api_code,
                        retryable=exc.retryable, outcome=exc.outcome,
                        retry_after_seconds=exc.retry_after_seconds, retry_count=attempt) from exc
                await asyncio.sleep(_retry_delay(attempt, exc.retry_after_seconds))
                attempt += 1
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # 连接没建立 = 一定没发出去；已连上后断开 = 结果未知，不盲目重发 IM
                preflight = isinstance(exc, aiohttp.ClientConnectorError)
                outcome: Any = "not_sent" if preflight else "unknown"
                retryable = idempotent or preflight
                if not retryable or attempt + 1 >= _MAX_ATTEMPTS:
                    raise FeishuAPIError(
                        f"{method} {path}: {exc.__class__.__name__}", retryable=retryable,
                        outcome=outcome, retry_count=attempt) from exc
                await asyncio.sleep(_retry_delay(attempt, None))
                attempt += 1

    async def send_card_delivery(self, chat_id: str, card: dict[str, Any], **kwargs: Any) -> Any:
        token = await self._tenant_token()
        body = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        for key in ("reply_to_message_id",):
            if kwargs.get(key):
                body["reply_message_id"] = kwargs[key]
        data = await self._request_json("POST", "/im/v1/messages?receive_id_type=chat_id",
                                        token=token, json_body=body)

        class _Result:
            success = True
            message_id = (data.get("data") or {}).get("message_id")
            raw_response = data.get("data")

        result = _Result()
        if not result.message_id:
            # 规范响应里一定有 message_id；没有 = 不知道发没发出去
            raise FeishuAPIError("im send returned no message_id", retryable=False, outcome="unknown")
        return result
