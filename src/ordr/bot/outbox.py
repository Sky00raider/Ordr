"""Sends the gate's outbox to the channel (TRD section 6).

The gate writes the final text. This sends it exactly as given: no edits, no cuts. A row is
acknowledged only after the channel accepted it. If a send fails, the rest of that person's rows
wait, so their order is kept, and the row is tried again on the next round. Another person's rows
go on. One failure never stops the loop.
"""

import asyncio
import logging
from typing import Any

from ordr.bot.channel import ChannelError
from ordr.bot.gate_client import GateApi, GateUnavailable
from ordr.contracts import Action, Button, Channel

log = logging.getLogger("ordr.bot.outbox")

INTERVAL = 0.5
MAX_BACKOFF = 10.0


def _buttons(row: dict[str, Any]) -> list[Button]:
    return [
        Button(Action(item["action"]), str(item["mandate_id"]), str(item["label"]))
        for item in row.get("buttons") or []
    ]


class Outbox:
    def __init__(self, gate: GateApi, channel: Channel, *, interval: float = INTERVAL) -> None:
        self.gate = gate
        self.channel = channel
        self.interval = interval
        self._failures = 0

    async def send_row(self, row: dict[str, Any]) -> None:
        """Send one row. Raises ChannelError (or ValueError/KeyError/TypeError for a bad row)."""
        number = str(row["number"])
        text = str(row["text"])
        buttons = _buttons(row)
        if buttons:
            await self.channel.send_buttons(number, text, buttons)
        else:
            await self.channel.send_text(number, text)

    async def tick(self) -> bool:
        """One round. True if every row was sent and acknowledged."""
        try:
            rows = await self.gate.outbox()
        except GateUnavailable as exc:
            log.warning("outbox not read: %s", exc)
            return False
        clean = True
        blocked: set[str] = set()
        for row in rows:
            number = str(row.get("number"))
            if number in blocked:
                clean = False
                continue
            try:
                await self.send_row(row)
            except (ChannelError, ValueError, KeyError, TypeError) as exc:
                log.error("outbox row %s (%s) not sent: %s", row.get("id"), row.get("kind"), exc)
                blocked.add(number)
                clean = False
                continue
            try:
                await self.gate.ack([int(row["id"])])
            except GateUnavailable as exc:
                # It may be sent again next round. Buttons are single use, so that is safe.
                log.warning("outbox row %s not acknowledged: %s", row.get("id"), exc)
                return False
        return clean

    async def run(self) -> None:
        while True:
            try:
                clean = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # last guard. No details: they could hold text
                log.error("outbox round failed: %s", type(exc).__name__)
                clean = False
            self._failures = 0 if clean else self._failures + 1
            delay = (
                min(self.interval * 2**self._failures, MAX_BACKOFF)
                if self._failures
                else (self.interval)
            )
            await asyncio.sleep(delay)
