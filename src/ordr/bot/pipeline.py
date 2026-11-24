"""The message pipeline (TRD section 4). Channel independent.

Order of work for every message, no exceptions:
1. Unknown number: dropped. A repeated message ID: dropped (saved in the bot's own SQLite file).
2. Button press: straight to the gate. Never the model, never queued behind anything.
3. Kid's text: "log" or a fixed reply. Never the model.
4. Parent's voice: a fixed reply for now.
5. Parent's text: the model, one message at a time per person, in arrival order.

The channel's signature check happens before this (see `app.py`).
"""

import asyncio
import logging
from typing import Protocol

from ordr.bot import texts
from ordr.bot.channel import ChannelError
from ordr.bot.gate_client import GateApi, GateUnavailable
from ordr.bot.seen import SeenStore
from ordr.contracts import Button, Channel, Incoming

log = logging.getLogger("ordr.bot.pipeline")

LOG_TEXT_MAX = 3800  # Telegram allows 4096 per message. We send the newest part
STALE_CODES = frozenset({"not_found", "not_current"})


class Replier(Protocol):
    """The model side. Returns text to send to the parent, or None if the gate already spoke."""

    async def reply(self, number: str, text: str) -> str | None: ...


class Pipeline:
    def __init__(
        self,
        *,
        parent_number: str,
        kid_number: str,
        parent_lang: str,
        seen: SeenStore,
        gate: GateApi,
        channel: Channel,
        llm: Replier,
    ) -> None:
        self.parent = parent_number.strip()
        self.kid = kid_number.strip()
        self.lang = parent_lang
        self.seen = seen
        self.gate = gate
        self.channel = channel
        self.llm = llm
        self._locks: dict[str, asyncio.Lock] = {}

    # ----- entry ------------------------------------------------------------------------

    async def handle(self, incoming: Incoming) -> None:
        """Handle one verified message. Never raises."""
        try:
            await self._handle(incoming)
        except Exception as exc:  # one bad message must not stop the bot. No details: PII
            log.error("message handling failed: %s", type(exc).__name__)

    async def _handle(self, inc: Incoming) -> None:
        number = (inc.number or "").strip()
        if number not in (self.parent, self.kid):
            return  # a stranger
        if not inc.message_id or not self.seen.claim(inc.message_id):
            return  # a repeat
        if inc.action is not None:
            await self._press(inc, number)
            return
        # Everything else keeps order per person. asyncio locks wake waiters first come, first
        # served, and this line is reached without awaiting, so arrival order is kept.
        async with self._locks.setdefault(number, asyncio.Lock()):
            if number == self.kid:
                await self._kid_message(inc, number)
            else:
                await self._parent_message(inc, number)

    # ----- presses ----------------------------------------------------------------------

    async def _press(self, inc: Incoming, number: str) -> None:
        if not inc.mandate_id:
            return
        try:
            result = await self.gate.act(number, inc.message_id, inc.action.value, inc.mandate_id)
        except GateUnavailable:
            await self._say(number, texts.GATE_DOWN)
            return
        code = result.get("code") if isinstance(result, dict) else None
        if isinstance(result, dict) and result.get("ok"):
            return  # the gate writes what comes next into its outbox
        if code in STALE_CODES:
            await self._say(number, texts.BUTTON_STALE)
        elif code == "error":
            await self._say(number, texts.PRESS_FAILED)
        # Any other refusal (blocked, expired, ...) already has its own message from the gate.

    # ----- the kid ----------------------------------------------------------------------

    async def _kid_message(self, inc: Incoming, number: str) -> None:
        if inc.text is not None and inc.text.strip().lower() == "log":
            try:
                text = await self.gate.log(number)
            except GateUnavailable:
                await self._say(number, texts.GATE_DOWN)
                return
            await self._say(number, _newest(text))
            return
        await self._say(number, texts.KID_TEXT)

    # ----- the parent -------------------------------------------------------------------

    async def _parent_message(self, inc: Incoming, number: str) -> None:
        if inc.audio is not None:
            await self._say(number, texts.pick(texts.VOICE_SOON, self.lang))
            return
        text = (inc.text or "").strip()
        if not text:
            return
        try:
            answer = await self.llm.reply(number, text)
        except Exception as exc:  # the model side should not raise. If it does: fixed text
            log.error("model step failed: %s", type(exc).__name__)
            answer = texts.pick(texts.TRY_AGAIN, self.lang)
        if answer:
            await self._say(number, answer)

    # ----- sending ----------------------------------------------------------------------

    async def _say(self, number: str, text: str, buttons: list[Button] | None = None) -> None:
        try:
            if buttons:
                await self.channel.send_buttons(number, text, buttons)
            else:
                await self.channel.send_text(number, text)
        except ChannelError as exc:
            log.error("reply not sent: %s", exc)


def _newest(text: str) -> str:
    """The end of a long log. Whole lines only, so no half line is shown."""
    if len(text) <= LOG_TEXT_MAX:
        return text
    lines = text.splitlines()
    kept: list[str] = []
    size = 0
    for line in reversed(lines[1:]):
        if size + len(line) + 1 > LOG_TEXT_MAX - 60:
            break
        kept.append(line)
        size += len(line) + 1
    kept.reverse()
    return "\n".join([lines[0] if lines else "", "(older entries left out)", *kept])
