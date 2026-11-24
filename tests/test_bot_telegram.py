"""The Telegram adapter and the button ids it shares with Meta. No network."""

import asyncio
import json

import httpx
import pytest
from bot_fakes import KID, MANDATE, PARENT

from ordr.bot.channel import ChannelError, decode_button, encode_button
from ordr.bot.telegram import Telegram
from ordr.contracts import LABELS, Action, Button

TOKEN = "123456789:AAH-secret_token_value_that_must_not_leak"
SECRET = "app-secret-for-tests"
PHONE_ID = "555000111"


def buttons(*actions: Action) -> list[Button]:
    return [Button(a, MANDATE, LABELS[a]) for a in actions]


# ----- button ids ----------------------------------------------------------------------------


def test_button_ids_round_trip_and_fit_telegrams_limit_for_every_action():
    for action in Action:
        data = encode_button(Button(action, MANDATE, "x"))
        assert len(data.encode()) <= 64
        assert decode_button(data) == (action, MANDATE)


@pytest.mark.parametrize("bad", ["", "confirm", "confirm:", "nope:abc", ":abc", "a" * 300])
def test_foreign_button_ids_are_refused(bad):
    assert decode_button(bad) is None


# ----- Telegram ------------------------------------------------------------------------------


def tg_client(handler) -> Telegram:
    return Telegram(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def tg_message(chat_id=int(PARENT), text="hello", chat_type="private", sender=None, mid=7):
    return {
        "update_id": 1,
        "message": {
            "message_id": mid,
            "chat": {"id": chat_id, "type": chat_type},
            "from": sender or {"id": chat_id, "is_bot": False},
            "text": text,
        },
    }


def tg_callback(data, user=int(KID), chat_type="private", cb_id="cb1"):
    return {
        "update_id": 2,
        "callback_query": {
            "id": cb_id,
            "from": {"id": user, "is_bot": False},
            "message": {"message_id": 3, "chat": {"id": user, "type": chat_type}},
            "data": data,
        },
    }


async def test_telegram_parses_a_private_text_with_the_chat_id_as_the_number():
    channel = tg_client(lambda r: httpx.Response(200))
    (got,) = channel.parse_update(tg_message(text="milk"))
    assert (got.number, got.text, got.action) == (PARENT, "milk", None)
    assert got.message_id == f"tg:{PARENT}:7"


def test_telegram_drops_groups_bots_and_odd_updates():
    channel = Telegram(TOKEN)
    assert channel.parse_update(tg_message(chat_type="group")) == []
    assert channel.parse_update(tg_message(sender={"id": int(PARENT), "is_bot": True})) == []
    assert channel.parse_update(tg_message(sender={"id": 999})) == []  # sender is not the chat
    assert channel.parse_update({"update_id": 3, "edited_message": {}}) == []
    assert channel.parse_update("junk") == []
    assert channel.parse_update(tg_callback("confirm:" + MANDATE, chat_type="supergroup")) == []


def test_telegram_parses_a_button_press_and_a_typed_yes_is_just_text():
    channel = Telegram(TOKEN)
    (press,) = channel.parse_update(tg_callback(f"approve:{MANDATE}"))
    assert (press.action, press.mandate_id, press.number) == (Action.APPROVE, MANDATE, KID)
    assert press.message_id == "tgcb:cb1" and press.text is None
    (typed,) = channel.parse_update(tg_message(text="yes"))
    assert typed.action is None and typed.mandate_id is None
    assert channel.parse_update(tg_callback("approve")) == []
    assert channel.parse_update(tg_callback("hack:1")) == []


def test_telegram_marks_a_voice_note():
    update = tg_message()
    del update["message"]["text"]
    update["message"]["voice"] = {"mime_type": "audio/ogg", "file_id": "f"}
    (got,) = Telegram(TOKEN).parse_update(update)
    assert got.audio == b"" and got.audio_type == "audio/ogg" and got.text is None


async def test_telegram_parse_reads_a_get_updates_answer():
    body = json.dumps({"ok": True, "result": [tg_message(), tg_callback(f"stop:{MANDATE}")]})
    got = await Telegram(TOKEN).parse(body.encode())
    assert [item.action for item in got] == [None, Action.STOP]
    assert await Telegram(TOKEN).parse(b"not json") == []


async def test_telegram_sends_buttons_as_one_inline_row():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {}})

    channel = tg_client(handler)
    await channel.send_buttons(PARENT, "Your order", buttons(Action.CONFIRM, Action.STOP))
    assert seen["path"].endswith("/sendMessage")
    assert seen["body"]["chat_id"] == int(PARENT) and seen["body"]["text"] == "Your order"
    row = seen["body"]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in row] == ["Confirm", "Stop"]
    assert [b["callback_data"] for b in row] == [f"confirm:{MANDATE}", f"stop:{MANDATE}"]


async def test_telegram_refuses_buttons_it_cannot_show_as_given():
    channel = tg_client(lambda r: httpx.Response(200, json={"ok": True}))
    too_many = buttons(Action.CONFIRM, Action.STOP, Action.APPROVE, Action.REJECT)
    with pytest.raises(ChannelError):
        await channel.send_buttons(PARENT, "x", too_many)
    with pytest.raises(ChannelError):
        await channel.send_buttons(PARENT, "x", [Button(Action.STOP, MANDATE, "L" * 21)])
    with pytest.raises(ChannelError):
        await channel.send_buttons(PARENT, "x", [Button(Action.STOP, "m" * 100, "Stop")])
    with pytest.raises(ChannelError):
        await channel.send_text(PARENT, "x" * 5000)


async def test_a_telegram_error_never_holds_the_bot_token():
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    channel = tg_client(broken)
    with pytest.raises(ChannelError) as info:
        await channel.send_text(PARENT, "hello")
    assert TOKEN not in str(info.value) and "AAH" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__

    channel = tg_client(lambda r: httpx.Response(403, json={"ok": False}))
    with pytest.raises(ChannelError) as info:
        await channel.send_text(PARENT, "hello")
    assert info.value.permanent is True and TOKEN not in str(info.value)

    channel = tg_client(lambda r: httpx.Response(429, json={"ok": False}))
    with pytest.raises(ChannelError) as info:
        await channel.send_text(PARENT, "hello")
    assert info.value.permanent is False


async def test_polling_answers_every_callback_and_hands_messages_on_in_order():
    answered: list[str] = []
    polls: list[dict] = []
    updates = [
        tg_message(text="milk", mid=1) | {"update_id": 10},
        tg_callback(f"confirm:{MANDATE}", cb_id="c1") | {"update_id": 11},
        tg_callback(f"confirm:{MANDATE}", user=424242, cb_id="stranger") | {"update_id": 12},
        tg_callback("junk", cb_id="c3") | {"update_id": 13},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        if method == "answerCallbackQuery":
            answered.append(body["callback_query_id"])
            return httpx.Response(200, json={"ok": True, "result": True})
        assert method == "getUpdates"
        polls.append(body)
        if len(polls) == 1:
            return httpx.Response(200, json={"ok": True, "result": updates})
        await asyncio.sleep(3600)

    channel = tg_client(handler)
    got = []

    async def collect(incoming):
        got.append(incoming)

    task = asyncio.create_task(channel.poll(collect))
    for _ in range(100):
        if len(polls) >= 2:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    task.cancel()
    assert sorted(answered) == ["c1", "c3", "stranger"]  # every callback, even junk and strangers
    assert polls[0]["allowed_updates"] == ["message", "callback_query"]
    assert polls[1]["offset"] == 14  # the next poll skips what was handed on
    assert [(i.number, i.action) for i in got] == [
        (PARENT, None),
        (KID, Action.CONFIRM),
        ("424242", Action.CONFIRM),
    ]


def test_telegram_never_accepts_a_webhook_post():
    assert Telegram(TOKEN).verify({"x-telegram-bot-api-secret-token": "x"}, b"{}") is False
