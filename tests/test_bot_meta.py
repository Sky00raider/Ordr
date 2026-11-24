"""The Meta (WhatsApp) adapter: signatures, parsing, sending. No network."""

import hmac
import json
from hashlib import sha256

import httpx
import pytest
from bot_fakes import KID, MANDATE, PARENT

from ordr.bot.channel import ChannelError
from ordr.bot.meta import Meta
from ordr.contracts import LABELS, Action, Button

SECRET = "app-secret-for-tests"
PHONE_ID = "555000111"


def buttons(*actions: Action) -> list[Button]:
    return [Button(a, MANDATE, LABELS[a]) for a in actions]


# ----- Meta ----------------------------------------------------------------------------------


def meta_client(handler=None) -> Meta:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler or (lambda r: httpx.Response(200)))
    )
    return Meta(
        token="meta-token", app_secret=SECRET, phone_id=PHONE_ID, verify_token="vt", client=client
    )


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()


def wa_payload(messages=None, statuses=None, phone_id=PHONE_ID) -> bytes:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001111", "phone_number_id": phone_id},
    }
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    body = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}],
    }
    return json.dumps(body).encode()


def wa_text(body="hello", sender=PARENT, mid="wamid.1"):
    return {"from": sender, "id": mid, "type": "text", "text": {"body": body}}


def test_meta_accepts_only_the_exact_signature():
    meta = meta_client()
    body = wa_payload([wa_text()])
    assert meta.verify({"X-Hub-Signature-256": sign(body)}, body) is True
    assert meta.verify({"x-hub-signature-256": sign(body)}, body) is True  # header case
    assert meta.verify({}, body) is False  # missing
    assert meta.verify({"x-hub-signature-256": ""}, body) is False
    assert meta.verify({"x-hub-signature-256": sign(body, "wrong-key")}, body) is False
    assert meta.verify({"x-hub-signature-256": sign(body).removeprefix("sha256=")}, body) is False
    assert meta.verify({"x-hub-signature-256": "sha1=" + sign(body)[7:]}, body) is False
    changed = body.replace(b"hello", b"HELLO")
    assert meta.verify({"x-hub-signature-256": sign(body)}, changed) is False
    assert meta.verify({"x-hub-signature-256": "sha256=éé"}, body) is False


def test_meta_with_no_app_secret_accepts_nothing():
    meta = Meta(token="t", app_secret="", phone_id=PHONE_ID, verify_token="vt")
    body = b"{}"
    assert meta.can_receive is False
    assert meta.verify({"x-hub-signature-256": sign(body, "")}, body) is False


def test_meta_signature_is_compared_in_constant_time():
    import inspect

    from ordr.bot import meta as meta_module

    assert "compare_digest" in inspect.getsource(meta_module.Meta.verify)
    assert "compare_digest" in inspect.getsource(meta_module.Meta.check_verify_token)


def test_meta_handshake_checks_mode_and_token():
    meta = meta_client()
    assert meta.check_verify_token("subscribe", "vt") is True
    assert meta.check_verify_token("subscribe", "VT") is False
    assert meta.check_verify_token("unsubscribe", "vt") is False
    assert meta.check_verify_token("subscribe", None) is False
    assert meta.check_verify_token(None, "vt") is False
    blank = Meta(token="t", app_secret=SECRET, phone_id=PHONE_ID, verify_token="")
    assert blank.check_verify_token("subscribe", "") is False


async def test_meta_parses_text_and_two_messages_in_one_payload_in_order():
    body = wa_payload([wa_text("milk", PARENT, "wamid.A"), wa_text("log", KID, "wamid.B")])
    got = await meta_client().parse(body)
    assert [(i.message_id, i.number, i.text) for i in got] == [
        ("wamid.A", PARENT, "milk"),
        ("wamid.B", KID, "log"),
    ]


async def test_meta_parses_a_button_reply_as_a_press_and_never_as_text():
    reply = {
        "from": KID,
        "id": "wamid.C",
        "type": "interactive",
        "interactive": {
            "type": "button_reply",
            "button_reply": {"id": f"approve:{MANDATE}", "title": "Approve and pay"},
        },
    }
    (got,) = await meta_client().parse(wa_payload([reply]))
    assert (got.action, got.mandate_id, got.number, got.text) == (
        Action.APPROVE,
        MANDATE,
        KID,
        None,
    )
    forged = {**reply, "id": "wamid.D"}
    forged["interactive"] = {
        "type": "button_reply",
        "button_reply": {"id": "approve", "title": "x"},
    }
    assert await meta_client().parse(wa_payload([forged])) == []
    lst = {
        **reply,
        "interactive": {"type": "list_reply", "list_reply": {"id": f"approve:{MANDATE}"}},
    }
    assert await meta_client().parse(wa_payload([lst])) == []


async def test_meta_parses_a_voice_note_and_drops_other_types():
    audio = {
        "from": PARENT,
        "id": "wamid.E",
        "type": "audio",
        "audio": {"id": "media1", "mime_type": "audio/ogg; codecs=opus", "voice": True},
    }
    image = {"from": PARENT, "id": "wamid.F", "type": "image", "image": {"id": "x"}}
    got = await meta_client().parse(wa_payload([audio, image]))
    assert [(i.message_id, i.audio, i.audio_type) for i in got] == [
        ("wamid.E", b"", "audio/ogg; codecs=opus")
    ]


async def test_meta_drops_status_updates():
    statuses = [{"id": "wamid.G", "status": "delivered", "recipient_id": PARENT}]
    assert await meta_client().parse(wa_payload(statuses=statuses)) == []


async def test_meta_accepts_only_its_own_phone_number_id():
    body = wa_payload([wa_text()], phone_id="999999")
    assert await meta_client().parse(body) == []


async def test_meta_drops_a_message_without_a_sender_and_junk():
    nameless = {"id": "wamid.H", "type": "text", "text": {"body": "hi"}}
    assert await meta_client().parse(wa_payload([nameless])) == []
    assert await meta_client().parse(b"not json") == []
    assert await meta_client().parse(b'{"object": "page"}') == []
    assert await meta_client().parse(b"[1, 2]") == []


async def test_meta_sends_text_and_reply_buttons_as_documented():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            (str(request.url), request.headers["authorization"], json.loads(request.content))
        )
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    meta = meta_client(handler)
    await meta.send_text(PARENT, "hello")
    await meta.send_buttons(KID, "Approve this?", buttons(Action.APPROVE, Action.REJECT))
    url, auth, text_body = sent[0]
    assert url == f"https://graph.facebook.com/v27.0/{PHONE_ID}/messages"
    assert auth == "Bearer meta-token"
    assert text_body["type"] == "text" and text_body["to"] == PARENT
    assert text_body["text"]["body"] == "hello" and text_body["messaging_product"] == "whatsapp"
    interactive = sent[1][2]["interactive"]
    assert interactive["type"] == "button" and interactive["body"] == {"text": "Approve this?"}
    assert interactive["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": f"approve:{MANDATE}", "title": "Approve and pay"}},
        {"type": "reply", "reply": {"id": f"reject:{MANDATE}", "title": "Reject"}},
    ]


async def test_meta_refuses_what_whatsapp_would_cut():
    meta = meta_client()
    with pytest.raises(ChannelError):
        await meta.send_buttons(PARENT, "x" * 1025, buttons(Action.STOP))
    with pytest.raises(ChannelError):
        await meta.send_buttons(PARENT, "x", buttons(*list(Action)[:4]))
    with pytest.raises(ChannelError):
        await meta.send_buttons(PARENT, "x", [Button(Action.STOP, MANDATE, "L" * 21)])
    with pytest.raises(ChannelError):
        await meta.send_text(PARENT, "")


async def test_meta_send_errors_say_the_status_and_nothing_secret():
    meta = meta_client(
        lambda r: httpx.Response(400, json={"error": {"code": 131047, "message": "secret words"}})
    )
    with pytest.raises(ChannelError) as info:
        await meta.send_text(PARENT, "hello")
    assert "400" in str(info.value) and "131047" in str(info.value)
    assert "secret words" not in str(info.value) and info.value.permanent is True

    meta = meta_client(lambda r: httpx.Response(503))
    with pytest.raises(ChannelError) as info:
        await meta.send_text(PARENT, "hello")
    assert info.value.permanent is False

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("meta-token in here")

    with pytest.raises(ChannelError) as info:
        await meta_client(broken).send_text(PARENT, "hello")
    assert "meta-token" not in str(info.value)
