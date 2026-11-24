"""The outbox sender: exact text, ack only after a send, order kept, one failure never stops it."""

import asyncio

from bot_fakes import KID, PARENT, FakeChannel, FakeGate, row

from ordr.bot.outbox import Outbox
from ordr.contracts import Action


def make():
    gate, channel = FakeGate(), FakeChannel()
    return Outbox(gate, channel, interval=0.01), gate, channel


async def test_text_and_buttons_are_sent_exactly_as_given_and_then_acked():
    outbox, gate, channel = make()
    buttons = [
        {"action": "confirm", "mandate_id": "m-1", "label": "Confirm"},
        {"action": "stop", "mandate_id": "m-1", "label": "Stop"},
    ]
    text = "  Line one\n\nLine   two with  spaces, ಕನ್ನಡ  "
    gate.rows = [row(1, PARENT, text, buttons), row(2, KID, "plain")]
    assert await outbox.tick() is True
    kind, number, sent_text, sent_buttons = channel.sent[0]
    assert (kind, number, sent_text) == ("buttons", PARENT, text)
    assert [(b.action, b.mandate_id, b.label) for b in sent_buttons] == [
        (Action.CONFIRM, "m-1", "Confirm"),
        (Action.STOP, "m-1", "Stop"),
    ]
    assert channel.sent[1] == ("text", KID, "plain", None)
    assert gate.acked == [1, 2]


async def test_a_failed_send_is_not_acked_and_is_tried_again():
    outbox, gate, channel = make()
    gate.rows = [row(1, PARENT, "first")]
    channel.fail = 1
    assert await outbox.tick() is False
    assert gate.acked == [] and channel.sent == []
    assert await outbox.tick() is True
    assert gate.acked == [1] and channel.texts(PARENT) == ["first"]


async def test_order_is_kept_for_a_person_when_an_earlier_row_fails():
    outbox, gate, channel = make()
    gate.rows = [row(1, PARENT, "readback"), row(2, PARENT, "second"), row(3, KID, "kid card")]
    channel.fail_for = {PARENT}
    assert await outbox.tick() is False
    # The parent's row 1 failed, so row 2 waits behind it. The kid's row still goes.
    assert gate.acked == [3]
    assert channel.texts(PARENT) == []
    channel.fail_for = set()
    assert await outbox.tick() is True
    assert channel.texts(PARENT) == ["readback", "second"]
    assert gate.acked == [3, 1, 2]


async def test_a_permanent_failure_is_still_never_acked():
    outbox, gate, channel = make()
    gate.rows = [row(1, PARENT, "x")]
    channel.fail_for = {PARENT}
    channel.permanent = True
    for _ in range(3):
        assert await outbox.tick() is False
    assert gate.acked == []


async def test_a_malformed_row_blocks_only_its_own_number_and_is_not_acked():
    outbox, gate, channel = make()
    bad = row(1, PARENT, "x", [{"action": "not-an-action", "mandate_id": "m", "label": "Hi"}])
    gate.rows = [bad, row(2, KID, "fine")]
    assert await outbox.tick() is False
    assert gate.acked == [2]


async def test_when_the_ack_fails_the_row_is_sent_again_next_round():
    outbox, gate, channel = make()
    gate.rows = [row(1, PARENT, "pay now")]
    gate.ack_error = True
    assert await outbox.tick() is False
    gate.ack_error = False
    assert await outbox.tick() is True
    assert channel.texts(PARENT) == ["pay now", "pay now"]  # safe: buttons are single use
    assert gate.acked == [1]


async def test_gate_down_does_not_stop_the_loop():
    outbox, gate, channel = make()
    gate.outbox_errors = 2
    gate.rows = [row(1, PARENT, "hello")]
    task = asyncio.create_task(outbox.run())
    for _ in range(200):
        if gate.acked:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    assert gate.acked == [1]
    assert channel.texts(PARENT) == ["hello"]


async def test_an_unexpected_error_in_a_round_does_not_stop_the_loop():
    outbox, gate, channel = make()
    calls = {"n": 0}
    real = outbox.tick

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("surprise")
        return await real()

    outbox.tick = flaky
    gate.rows = [row(1, PARENT, "hello")]
    task = asyncio.create_task(outbox.run())
    for _ in range(200):
        if gate.acked:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    assert gate.acked == [1]
