"""The bot's calls to the gate's /internal API (TRD section 6).

Both headers are set here, by code. The model never sees or sets them.
"""

from typing import Any, Protocol

import httpx

ACT_TIMEOUT = 60.0  # a press can run the final checks and `checkout`
QUICK_TIMEOUT = 10.0


class GateUnavailable(Exception):
    """The gate did not answer properly. The message holds no URL, number or text."""


class GateApi(Protocol):
    async def act(
        self, number: str, message_id: str, action: str, mandate_id: str
    ) -> dict[str, Any]: ...

    async def outbox(self) -> list[dict[str, Any]]: ...

    async def ack(self, ids: list[int]) -> None: ...

    async def log(self, number: str) -> str: ...


class HttpGate:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = token.strip()
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        number: str | None = None,
        json: Any = None,
        timeout: float = QUICK_TIMEOUT,
    ) -> httpx.Response:
        headers = {"x-ordr-internal": self._token}
        if number is not None:
            headers["x-ordr-number"] = number
        try:
            response = await self._client.request(
                method, f"/internal{path}", headers=headers, json=json, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise GateUnavailable(f"gate call failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise GateUnavailable(f"gate answered {response.status_code}")
        return response

    async def act(
        self, number: str, message_id: str, action: str, mandate_id: str
    ) -> dict[str, Any]:
        body = {"message_id": message_id, "action": action, "mandate_id": mandate_id}
        response = await self._request(
            "POST", "/action", number=number, json=body, timeout=ACT_TIMEOUT
        )
        return _json(response)

    async def outbox(self) -> list[dict[str, Any]]:
        rows = _json(await self._request("GET", "/outbox"))
        if not isinstance(rows, list):
            raise GateUnavailable("outbox was not a list")
        return rows

    async def ack(self, ids: list[int]) -> None:
        await self._request("POST", "/outbox/ack", json={"ids": ids})

    async def log(self, number: str) -> str:
        return (await self._request("GET", "/log", number=number)).text

    async def close(self) -> None:
        await self._client.aclose()


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise GateUnavailable("gate answer was not JSON") from None
