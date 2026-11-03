"""CardKitTransport 需要的三个 REST 方法，用内置适配器已有的凭据实现。

HFC 在自己的 FeishuClient 上做同样三件事；这里只补接口，不重写任何卡片逻辑。
"""

from __future__ import annotations

import json
import time
from typing import Any

import aiohttp

from .delivery import FeishuAPIError

_API = {"feishu": "https://open.feishu.cn/open-apis", "lark": "https://open.larksuite.com/open-apis"}


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
        session = await self._http()
        async with session.post(
            f"{self._api}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        ) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") != 0:
            raise FeishuAPIError(f"tenant token failed: {data.get('msg')}", api_code=data.get("code"))
        self._token = str(data["tenant_access_token"])
        self._expires_at = time.monotonic() + float(data.get("expire", 7200))
        return self._token

    async def _request_json(self, method: str, path: str, *, token: str = "",
                            json_body: Any = None) -> dict[str, Any]:
        session = await self._http()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with session.request(method, f"{self._api}{path}", json=json_body, headers=headers) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") != 0:
            raise FeishuAPIError(f"{method} {path}: {data.get('msg')}",
                                 status_code=resp.status, api_code=data.get("code"))
        return data

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
            raise FeishuAPIError("im send returned no message_id", outcome="unknown")
        return result
