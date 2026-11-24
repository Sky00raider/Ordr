"""WhatsApp Cloud API adapter (Meta). Checked against Meta's docs on 24 Nov 2026.

Send: POST https://graph.facebook.com/v27.0/<phone id>/messages with a bearer token. Reply
buttons: at most 3, title 20 characters, id 256 characters, body 1024 characters.
Receive: a signed webhook. `X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw body, app secret>`.
A message can come as text, audio or an interactive button reply. Status updates (sent, delivered,
read) are dropped. Only our own phone number id is accepted.

Not tested on a real number yet. Without a template, a person must have messaged us in the last
24 hours, so both phones send "hi" first.
"""

import hmac
import json
import logging
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

log = logging.getLogger("ordr.bot.meta")

GRAPH = "https://graph.facebook.com/v27.0"
SEND_TIMEOUT = 15.0
MAX_TEXT = 4096
BUTTON_ID_BYTES = 256
SIGNATURE_HEADER = "x-hub-signature-256"


class Meta:
    def __init__(
        self,
        *,
        token: str,
        app_secret: str,
        phone_id: str,
        verify_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token.strip()
        self._secret = app_secret.strip().encode("utf-8")
        self.phone_id = phone_id.strip()
        self._verify_token = verify_token.strip().encode("utf-8")
        self._client = client or httpx.AsyncClient()

    # ----- the webhook: checks -----------------------------------------------------------

    @property
    def can_receive(self) -> bool:
        """No webhook unless the app secret is set."""
        return bool(self._secret)

    def verify(self, headers: dict[str, str], body: bytes) -> bool:
        """The signature on the raw body, in constant time. Missing or wrong: False."""
        if not self._secret:
            return False
        sent = ""
        for name, value in headers.items():
            if name.lower() == SIGNATURE_HEADER:
                sent = value.strip()
                break
        prefix = "sha256="
        if not sent.startswith(prefix) or not sent.isascii():
            return False
        expected = hmac.new(self._secret, body, sha256).hexdigest()
        return hmac.compare_digest(sent[len(prefix) :].lower().encode("ascii"), expected.encode())

    def check_verify_token(self, mode: str | None, token: str | None) -> bool:
        """Meta's GET handshake: mode `subscribe` and our verify token, in constant time."""
        if not self._verify_token or mode != "subscribe" or token is None:
            return False
        return hmac.compare_digest(token.encode("utf-8"), self._verify_token)

    # ----- the webhook: parsing ----------------------------------------------------------

    async def parse(self, body: bytes) -> list[Incoming]:
        """Messages in a payload, in order. Call only after `verify`."""
        try:
            data = json.loads(body)
        except ValueError:
            return []
        if not isinstance(data, dict) or data.get("object") != "whatsapp_business_account":
            return []
        out: list[Incoming] = []
        for entry in _list(data.get("entry")):
            for change in _list(entry.get("changes") if isinstance(entry, dict) else None):
                value = change.get("value") if isinstance(change, dict) else None
                if not isinstance(value, dict) or change.get("field") != "messages":
                    continue
                metadata = value.get("metadata")
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("phone_number_id") != self.phone_id
                ):
                    continue  # not our number
                for message in _list(value.get("messages")):  # `statuses` is not read at all
                    incoming = _incoming(message)
                    if incoming is not None:
                        out.append(incoming)
        return out

    # ----- sending ----------------------------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        try:
            response = await self._client.post(
                f"{GRAPH}/{self.phone_id}/messages",
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=SEND_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ChannelError(f"meta send failed: {type(exc).__name__}") from None
        if response.status_code == 200:
            return
        status = response.status_code
        code = ""
        try:
            error = response.json().get("error", {})
            if isinstance(error.get("code"), int):
                code = f" (code {error['code']})"
        except (ValueError, AttributeError):
            pass
        permanent = 400 <= status < 500 and status not in (408, 429)
        raise ChannelError(f"meta send answered {status}{code}", permanent=permanent)

    async def send_text(self, number: str, text: str) -> None:
        if not text or len(text) > MAX_TEXT:
            raise ChannelError("whatsapp text is empty or over 4096 characters", permanent=True)
        await self._send(
            {"to": number, "type": "text", "text": {"body": text, "preview_url": False}}
        )

    async def send_buttons(self, number: str, text: str, buttons: list[Button]) -> None:
        if not text or len(text) > MAX_BUTTON_BODY:
            raise ChannelError(
                "a button message body is empty or over 1024 characters", permanent=True
            )
        check_buttons(buttons, id_bytes=BUTTON_ID_BYTES)
        await self._send(
            {
                "to": number,
                "type": "interactive",
                "interactive": {
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
                },
            }
        )

    async def send_voice(self, number: str, audio: bytes, audio_type: str) -> None:
        raise ChannelError("voice is not built yet", permanent=True)

    async def close(self) -> None:
        await self._client.aclose()


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _incoming(message: Any) -> Incoming | None:
    if not isinstance(message, dict):
        return None
    sender, message_id = message.get("from"), message.get("id")
    if not isinstance(sender, str) or not sender or not isinstance(message_id, str):
        return None  # `from` is missing for people hidden behind a username. We cannot match them
    kind = message.get("type")
    if kind == "text":
        text = (message.get("text") or {}).get("body")
        if isinstance(text, str):
            return Incoming(message_id=message_id, number=sender, text=text)
    elif kind == "audio":
        audio = message.get("audio") or {}
        mime = audio.get("mime_type") if isinstance(audio.get("mime_type"), str) else "audio/ogg"
        # Only marks "a voice note" for now. The file is fetched in the voice step.
        return Incoming(message_id=message_id, number=sender, audio=b"", audio_type=mime)
    elif kind == "interactive":
        interactive = message.get("interactive") or {}
        reply = (
            interactive.get("button_reply") if interactive.get("type") == "button_reply" else None
        )
        decoded = (
            decode_button(reply["id"])
            if isinstance(reply, dict) and isinstance(reply.get("id"), str)
            else None
        )
        if decoded is not None:
            action, mandate_id = decoded
            return Incoming(
                message_id=message_id, number=sender, action=action, mandate_id=mandate_id
            )
    return None
