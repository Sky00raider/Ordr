"""The webhook app: 403 before parsing, 200 at once, handling in the background."""

import hmac
import json
from hashlib import sha256

import pytest
from bot_fakes import KID, MANDATE, PARENT
from fastapi.testclient import TestClient

from ordr.bot import app as app_module
from ordr.bot.app import create_app
from ordr.bot.meta import Meta
from ordr.bot.settings import BotSettings

SECRET = "app-secret-for-tests"
PHONE_ID = "555000111"
HEADER = "X-Hub-Signature-256"


class RecordingPipeline:
    def __init__(self) -> None:
        self.handled = []

    async def handle(self, incoming) -> None:
        self.handled.append(incoming)


def make(secret: str = SECRET):
    pipeline = RecordingPipeline()
    meta = Meta(token="t", app_secret=secret, phone_id=PHONE_ID, verify_token="vt-123")
    app = create_app(BotSettings(), channel=meta, pipeline=pipeline)
    return TestClient(app), pipeline


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()


def payload(messages=None, statuses=None, phone_id=PHONE_ID) -> bytes:
    value = {"metadata": {"phone_number_id": phone_id}}
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": value}]}],
        }
    ).encode()


def text(body="hi", sender=PARENT, mid="wamid.1"):
    return {"from": sender, "id": mid, "type": "text", "text": {"body": body}}


def post(client, body: bytes, signature: str | None):
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers[HEADER] = signature
    return client.post("/webhook", content=body, headers=headers)


def test_a_signed_message_gets_200_and_is_handled():
    client, pipeline = make()
    body = payload([text("milk")])
    assert post(client, body, sign(body)).status_code == 200
    assert [(m.number, m.text) for m in pipeline.handled] == [(PARENT, "milk")]


def test_two_messages_in_one_payload_are_both_handled():
    client, pipeline = make()
    body = payload([text("a", PARENT, "wamid.1"), text("log", KID, "wamid.2")])
    assert post(client, body, sign(body)).status_code == 200
    assert {m.message_id for m in pipeline.handled} == {"wamid.1", "wamid.2"}


def test_a_button_reply_is_handled_as_a_press():
    client, pipeline = make()
    reply = {
        "from": KID,
        "id": "wamid.9",
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": f"approve:{MANDATE}"}},
    }
    body = payload([reply])
    assert post(client, body, sign(body)).status_code == 200
    (got,) = pipeline.handled
    assert got.action.value == "approve" and got.mandate_id == MANDATE and got.text is None


def test_a_status_only_payload_gets_200_and_nothing_is_handled():
    client, pipeline = make()
    body = payload(statuses=[{"id": "wamid.s", "status": "read"}])
    assert post(client, body, sign(body)).status_code == 200
    assert pipeline.handled == []


def test_a_payload_for_another_phone_number_id_is_ignored():
    client, pipeline = make()
    body = payload([text()], phone_id="12345")
    assert post(client, body, sign(body)).status_code == 200
    assert pipeline.handled == []


@pytest.mark.parametrize("case", ["missing", "empty", "wrong_key", "changed_body", "no_prefix"])
def test_a_bad_signature_gets_403_and_nothing_is_parsed_or_handled(case):
    client, pipeline = make()
    body = payload([text("milk")])
    signatures = {
        "missing": None,
        "empty": "",
        "wrong_key": sign(body, "other-secret"),
        "changed_body": sign(payload([text("tea")])),
        "no_prefix": sign(body).removeprefix("sha256="),
    }
    assert post(client, body, signatures[case]).status_code == 403
    assert pipeline.handled == []


def test_the_signature_is_checked_before_the_json_is_read():
    client, pipeline = make()
    assert post(client, b"not json at all", "sha256=00").status_code == 403
    assert post(client, b"not json at all", sign(b"not json at all")).status_code == 200


def test_an_oversize_body_is_refused_before_anything_else(monkeypatch):
    client, pipeline = make()
    big = b"x" * (app_module.MAX_BODY_BYTES + 1)
    assert post(client, big, sign(big)).status_code == 413
    assert pipeline.handled == []


def test_no_webhook_at_all_without_the_app_secret():
    client, pipeline = make(secret="")
    body = payload([text()])
    assert post(client, body, sign(body, "")).status_code == 403
    assert post(client, body, None).status_code == 403
    response = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "vt-123", "hub.challenge": "c"},
    )
    assert response.status_code == 403
    assert pipeline.handled == []


def test_the_handshake_returns_the_challenge_only_for_the_right_token():
    client, _ = make()
    ok = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "vt-123",
            "hub.challenge": "1158201444",
        },
    )
    assert ok.status_code == 200 and ok.text == "1158201444"
    for params in (
        {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.challenge": "1"},
        {"hub.mode": "unsubscribe", "hub.verify_token": "vt-123", "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.verify_token": "vt-123"},
    ):
        assert client.get("/webhook", params=params).status_code == 403


def test_health_still_works_and_docs_are_off():
    client, _ = make()
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_the_module_level_app_refuses_unsigned_posts():
    assert TestClient(app_module.app).post("/webhook").status_code == 403
