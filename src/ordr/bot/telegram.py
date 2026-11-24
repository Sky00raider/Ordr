"""Telegram adapter: long polling with getUpdates. No public URL. For testing and as the backup.

The chat id is the "number". Private chats only. Buttons are inline buttons whose callback_data
is `action:mandate_id` (64 bytes at most). Every callback is answered.

The bot token is part of every Telegram URL. So no URL, and no exception text from the HTTP
library, is ever logged or put in an error message here.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from ordr.bot.channel import (
    TELEGRAM_DATA_BYTES,
    ChannelError,
    check_buttons,
    decode_button,
    encode_button,
)
from ordr.contracts import Button, Incoming

log = logging.getLogger("ordr.bot.telegram")

API = "https://api.telegram.org"
POLL_SECONDS = 30
SEND_TIMEOUT = 15.0
MAX_TEXT = 4096
ALLOWED_UPDATES = ["message", "callback_query"]


class Telegram:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._base = f"{API}/bot{token.strip()}"
        self._client = client or httpx.AsyncClient()
        self._tasks: set[asyncio.Task] = set()

    # ----- the Bot API ------------------------------------------------------------------

    async def _call(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        try:
            response = await self._client.post(
                f"{self._base}/{method}", json=payload, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise ChannelError(f"telegram {method} failed: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            status = response.status_code
            permanent = 400 <= status < 500 and status not in (408, 429)
            raise ChannelError(f"telegram {method} answered {status}", permanent=permanent)
        return body.get("result")

    async def send_text(self, number: str, text: str) -> None:
        if not text or len(text) > MAX_TEXT:
            raise ChannelError("telegram text is empty or over 4096 characters", permanent=True)
        await self._call("sendMessage", {"chat_id": _chat_id(number), "text": text}, SEND_TIMEOUT)

    async def send_buttons(self, number: str, text: str, buttons: list[Button]) -> None:
        if not text or len(text) > MAX_TEXT:
            raise ChannelError("telegram text is empty or over 4096 characters", permanent=True)
        check_buttons(buttons, id_bytes=TELEGRAM_DATA_BYTES)
        keyboard = [
            [{"text": button.label, "callback_data": encode_button(button)} for button in buttons]
        ]
        await self._call(
            "sendMessage",
            {
                "chat_id": _chat_id(number),
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            },
            SEND_TIMEOUT,
        )

    async def send_voice(self, number: str, audio: bytes, audio_type: str) -> None:
        raise ChannelError("voice is not built yet", permanent=True)

    async def answer_callback(self, callback_id: str) -> None:
        """Stops the button's spinner. Best effort: a late answer must not stop anything."""
        try:
            await self._call("answerCallbackQuery", {"callback_query_id": callback_id}, 5.0)
        except ChannelError as exc:
            log.warning("callback not answered: %s", exc)

    # ----- the Channel protocol's webhook half: not used with polling -------------------

    def verify(self, headers: dict[str, str], body: bytes) -> bool:
        return False  # Telegram is polled. Nothing is accepted from a webhook

    async def parse(self, body: bytes) -> list[Incoming]:
        """Parse a getUpdates answer (or one update). For tests and tools."""
        try:
            data = json.loads(body)
        except ValueError:
            return []
        updates = data.get("result", data) if isinstance(data, dict) else data
        if isinstance(updates, dict):
            updates = [updates]
        out: list[Incoming] = []
        for update in updates if isinstance(updates, list) else []:
            out.extend(self.parse_update(update))
        return out

    # ----- incoming ---------------------------------------------------------------------

    def parse_update(self, update: Any) -> list[Incoming]:
        """One update to zero or one message. Private chats only."""
        if not isinstance(update, dict):
            return []
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            return _callback(callback)
        message = update.get("message")
        if isinstance(message, dict):
            return _message(message)
        return []

    async def poll(
        self, on_incoming: Callable[[Incoming], Awaitable[None]], *, answer: bool = True
    ) -> None:
        """Poll forever. Each message is handled in its own task, so polling never waits."""
        offset: int | None = None
        failures = 0
        while True:
            payload: dict[str, Any] = {"timeout": POLL_SECONDS, "allowed_updates": ALLOWED_UPDATES}
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = await self._call("getUpdates", payload, POLL_SECONDS + 10)
                failures = 0
            except ChannelError as exc:
                failures += 1
                log.warning("polling failed: %s", exc)
                await asyncio.sleep(min(2.0**failures, 30.0))
                continue
            for update in updates or []:
                if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                    continue
                offset = update["update_id"] + 1
                callback = update.get("callback_query")
                if answer and isinstance(callback, dict) and isinstance(callback.get("id"), str):
                    self._spawn(self.answer_callback(callback["id"]))
                for incoming in self.parse_update(update):
                    self._spawn(on_incoming(incoming))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self._client.aclose()


def _chat_id(number: str) -> int:
    try:
        return int(number)
    except ValueError:
        raise ChannelError("telegram number is not a chat id", permanent=True) from None


def _private_user(chat: Any, sender: Any) -> str | None:
    """The user's chat id if this is a private chat with that user, else None."""
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    chat_id, user_id = chat.get("id"), sender.get("id")
    if chat.get("type") != "private" or not isinstance(chat_id, int) or chat_id != user_id:
        return None
    if sender.get("is_bot"):
        return None
    return str(chat_id)


def _message(message: dict[str, Any]) -> list[Incoming]:
    number = _private_user(message.get("chat"), message.get("from"))
    message_no = message.get("message_id")
    if number is None or not isinstance(message_no, int):
        return []
    message_id = f"tg:{number}:{message_no}"
    text = message.get("text")
    if isinstance(text, str):
        return [Incoming(message_id=message_id, number=number, text=text)]
    voice = message.get("voice") or message.get("audio")
    if isinstance(voice, dict):
        # The audio itself is fetched in the voice step. Here it only marks "a voice note".
        kind = voice.get("mime_type") if isinstance(voice.get("mime_type"), str) else "audio/ogg"
        return [Incoming(message_id=message_id, number=number, audio=b"", audio_type=kind)]
    return []


def _callback(callback: dict[str, Any]) -> list[Incoming]:
    callback_id = callback.get("id")
    data = callback.get("data")
    chat = (callback.get("message") or {}).get("chat")
    number = _private_user(chat, callback.get("from"))
    if number is None or not isinstance(callback_id, str) or not isinstance(data, str):
        return []
    decoded = decode_button(data)
    if decoded is None:
        return []
    action, mandate_id = decoded
    return [
        Incoming(
            message_id=f"tgcb:{callback_id}",
            number=number,
            action=action,
            mandate_id=mandate_id,
        )
    ]


async def whoami(token: str) -> None:
    """Print the chat id of whoever messages the bot (for PARENT_NUMBER and KID_NUMBER)."""
    channel = Telegram(token)
    seen: set[str] = set()

    async def show(incoming: Incoming) -> None:
        if incoming.number in seen:
            return
        seen.add(incoming.number)
        print(f"chat id: {incoming.number}", flush=True)
        try:
            await channel.send_text(incoming.number, f"Your Ordr number is {incoming.number}")
        except ChannelError as exc:
            log.warning("whoami reply not sent: %s", exc)

    print("Message the bot from each phone. Press Ctrl+C when done.", flush=True)
    try:
        await channel.poll(show)
    finally:
        await channel.close()
