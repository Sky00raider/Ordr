"""Simulated UPI PIN. Not a store tool: it calls the demo store's POST /demo/pay route.

No real money. Kept out of StoreClient so nothing that talks to a store can also pay.
"""

from urllib.parse import urlsplit, urlunsplit

import httpx


class UpiUnclear(Exception):
    """The simulated payment did not clearly succeed. Ask the store what happened."""


class SimulatedUpi:
    def __init__(
        self,
        url: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._transport = transport

    @classmethod
    def for_store(cls, store_mcp_url: str) -> "SimulatedUpi":
        parts = urlsplit(store_mcp_url)
        return cls(urlunsplit((parts.scheme, parts.netloc, "/demo/pay", "", "")))

    async def pay(self, paas_id: str) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(self._url, json={"paasId": paas_id})
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpiUnclear(type(exc).__name__) from exc
        if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            raise UpiUnclear("refused")
