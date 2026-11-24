"""WhatsApp through Vonage's Messages Sandbox. Same Channel interface as `meta.py`.

Send: POST https://messages-sandbox.nexmo.com/v1/messages with HTTP Basic auth (API key and
secret). Answer is 202. Text is message_type "text". Buttons are message_type "custom" holding the
same `interactive` object Meta takes. A number that has not joined the sandbox gets 403.
Limits: 1 message per second and 100 per month (then 429). So sends are spaced 1.1 s apart.

Receive: POST /vonage/inbound and /vonage/status, JSON, `Authorization: Bearer <JWT>`. The JWT is
HS256 with the signature secret. Claims we check: api_key, iat, payload_hash (SHA-256 hex of the
raw body). Nothing in the body is read before every check passes.

Inbound shapes: text (`message_type` "text", `text` a string) and button reply (`message_type`
"reply", `reply.id`) are from Vonage's docs and a live test of the reply. Audio is not documented
where we looked: we treat `message_type` "audio" as "a voice note" and fetch nothing yet. Not
tested with a real voice note.
"""

import asyncio
import base64
import binascii
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any

import httpx

from ordr.bot.channel import (
    MAX_BUTTON_BODY,
    ChannelError,
    check_buttons,
    decode_button,
    encode_button,
)
from ordr.contracts import Button, Incoming

log = logging.getLogger("ordr.bot.vonage")

SEND_URL = "https://messages-sandbox.nexmo.com/v1/messages"
SEND_TIMEOUT = 15.0
MIN_GAP = 1.1  # seconds between sends. The sandbox allows 1 per second
MAX_TEXT = 4096
BUTTON_ID_BYTES = 256
AUTH_HEADER = "authorization"
MAX_TOKEN_CHARS = 4096
MAX_AGE = 300.0  # iat may be at most 5 minutes old
CLOCK_SKEW = 60.0  # and at most 1 minute ahead
FAILED_STATUSES = ("failed", "rejected")


class Vonage:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        signature_secret: str,
        sender: str,
        client: httpx.AsyncClient | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self._secret = signature_secret.strip().encode("utf-8")
        self.sender = sender.strip()
        self._client = client or httpx.AsyncClient()
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._send_lock = asyncio.Lock()
        self._last_send: float | None = None

    # ----- the webhook: checks -----------------------------------------------------------

    @property
    def can_receive(self) -> bool:
        """No webhook unless both the signature secret and the API key are set."""
        return bool(self._secret) and bool(self._api_key)

    def verify(self, headers: dict[str, str], body: bytes) -> bool:
        """Every check on the token and the raw body. Any failure: False. Reads nothing else."""
        if not self.can_receive:
            return False
        token = _bearer(headers)
        if token is None:
            return False
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            return False
        header = _json_object(_b64url(parts[0]))
        if header is None or header.get("alg") != "HS256":  # exactly. Never "none"
            return False
        signature = _b64url(parts[2])
        if signature is None:
            return False
        expected = hmac.new(self._secret, f"{parts[0]}.{parts[1]}".encode("ascii"), sha256)
        if not hmac.compare_digest(signature, expected.digest()):
            return False
        # The signature is good, so the claims are ours to read.
        claims = _json_object(_b64url(parts[1]))
        if claims is None:
            return False
        key = claims.get("api_key")
        if not isinstance(key, str) or not hmac.compare_digest(
            key.encode("utf-8"), self._api_key.encode("utf-8")
        ):
            return False
        issued = claims.get("iat")
        if isinstance(issued, bool) or not isinstance(issued, int | float):
            return False
        age = self._wall_clock() - issued
        if not -CLOCK_SKEW <= age <= MAX_AGE:  # also False for NaN
            return False
        body_hash = claims.get("payload_hash")
        if not isinstance(body_hash, str):
            return False
        return hmac.compare_digest(
            body_hash.strip().lower().encode("utf-8"), sha256(body).hexdigest().encode("ascii")
        )

    # ----- the webhook: parsing ----------------------------------------------------------

    async def parse(self, body: bytes) -> list[Incoming]:
        """The message in an inbound payload (zero or one). Call only after `verify`."""
        data = _json_object(body)
        if data is None or data.get("channel") != "whatsapp":
            return []
        recipient = data.get("to")
        if not isinstance(recipient, str) or recipient.strip() != self.sender:
            return []  # not our number
        sender, message_id = data.get("from"), data.get("message_uuid")
        if not isinstance(sender, str) or not sender.strip():
            return []
        if not isinstance(message_id, str) or not message_id:
            return []
        sender = sender.strip()
        kind = data.get("message_type")
        if kind == "text":
            text = data.get("text")
            if isinstance(text, str):  # typed words are text, even if they match a button
                return [Incoming(message_id=message_id, number=sender, text=text)]
        elif kind == "audio":
            return [
                Incoming(message_id=message_id, number=sender, audio=b"", audio_type="audio/ogg")
            ]
        elif kind == "reply":
            reply = data.get("reply")
            reply_id = reply.get("id") if isinstance(reply, dict) else None
            decoded = decode_button(reply_id) if isinstance(reply_id, str) else None
            if decoded is not None:
                action, mandate_id = decoded
                return [
                    Incoming(
                        message_id=message_id, number=sender, action=action, mandate_id=mandate_id
                    )
                ]
        return []

    def log_status(self, body: bytes) -> None:
        """A delivery report. Only a failure is logged, and only its status and error code."""
        data = _json_object(body)
        status = data.get("status") if data else None
        if status not in FAILED_STATUSES:
            return
        code = ""
        error = data.get("error") if data else None
        if isinstance(error, dict):
            value = error.get("code", error.get("title"))
            if isinstance(value, bool):
                value = None
            if isinstance(value, int) or (
                isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 8
            ):
                code = f" (code {value})"
        log.warning("vonage reported a %s message%s", status, code)

    # ----- sending ----------------------------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        body = {"from": self.sender, "channel": "whatsapp", **payload}
        async with self._send_lock:  # one at a time, spaced
            if self._last_send is not None:
                wait = self._last_send + MIN_GAP - self._monotonic()
                if wait > 0:
                    await self._sleep(wait)
            try:
                response = await self._client.post(
                    SEND_URL,
                    json=body,
                    auth=httpx.BasicAuth(self._api_key, self._api_secret),
                    timeout=SEND_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                # No retry here: a timed out send may have arrived, and a second one would
                # double it. The outbox decides whether to try again.
                raise ChannelError(f"vonage send failed: {type(exc).__name__}") from None
            finally:
                self._last_send = self._monotonic()
        status = response.status_code
        if status in (200, 202):
            return
        # 401, 403 (not joined) and 422 never work on a retry. 408 and 429 might.
        permanent = 400 <= status < 500 and status not in (408, 429)
        raise ChannelError(f"vonage send answered {status}", permanent=permanent)

    async def send_text(self, number: str, text: str) -> None:
        if not text or len(text) > MAX_TEXT:
            raise ChannelError("whatsapp text is empty or over 4096 characters", permanent=True)
        await self._send({"to": number, "message_type": "text", "text": text})

    async def send_buttons(self, number: str, text: str, buttons: list[Button]) -> None:
        if not text or len(text) > MAX_BUTTON_BODY:
            raise ChannelError(
                "a button message body is empty or over 1024 characters", permanent=True
            )
        check_buttons(buttons, id_bytes=BUTTON_ID_BYTES)
        interactive = {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": encode_button(button), "title": button.label},
                    }
                    for button in buttons
                ]
            },
        }
        await self._send(
            {
                "to": number,
                "message_type": "custom",
                "custom": {"type": "interactive", "interactive": interactive},
            }
        )

    async def send_voice(self, number: str, audio: bytes, audio_type: str) -> None:
        raise ChannelError("voice is not built yet", permanent=True)

    async def close(self) -> None:
        await self._client.aclose()


def _bearer(headers: dict[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == AUTH_HEADER:
            scheme, _, token = value.strip().partition(" ")
            token = token.strip()
            if scheme.lower() != "bearer" or not token:
                return None
            if len(token) > MAX_TOKEN_CHARS or not token.isascii():
                return None
            return token
    return None


def _b64url(segment: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, binascii.Error):
        return None


def _json_object(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
