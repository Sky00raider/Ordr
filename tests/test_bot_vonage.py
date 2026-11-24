"""The Vonage (WhatsApp sandbox) adapter and its two webhook routes. Mocked HTTP only."""

import base64
import json
import logging
import time
from hashlib import sha256
from hmac import new as hmac_new

import httpx
import pytest
from bot_fakes import KID, MANDATE, PARENT, FakeChannel, FakeGate, FakeReplier
from fastapi.testclient import TestClient

from ordr.bot.app import create_app
from ordr.bot.channel import ChannelError
from ordr.bot.meta import Meta
from ordr.bot.pipeline import Pipeline
from ordr.bot.settings import BotSettings, StartupError, check_settings
from ordr.bot.vonage import MIN_GAP, SEND_URL, Vonage
from ordr.contracts import LABELS, Action, Button

SIG_SECRET = "sig*secret%with^odd-chars"
API_KEY = "key12345"
API_SECRET = "api*secret%x^y"
FROM = "3000000003"
NOW = 1_800_000_000.0


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(
    body: bytes,
    *,
    secret: str = SIG_SECRET,
    alg: str = "HS256",
    claims: dict | None = None,
    drop: tuple[str, ...] = (),
    sign: bool = True,
) -> str:
    payload = {
        "api_key": API_KEY,
        "iat": NOW,
        "iss": "Vonage",
        "jti": "j1",
        "payload_hash": sha256(body).hexdigest(),
    }
    payload.update(claims or {})
    for name in drop:
        payload.pop(name, None)
    head = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    mid = b64(json.dumps(payload).encode())
    signature = (
        hmac_new(secret.encode(), f"{head}.{mid}".encode(), sha256).digest() if sign else b""
    )
    return f"{head}.{mid}.{b64(signature)}"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def inbound(kind: str = "text", sender: str = PARENT, uuid: str = "u1", to: str = FROM, **extra):
    data = {
        "to": to,
        "from": sender,
        "channel": "whatsapp",
        "message_uuid": uuid,
        "message_type": kind,
        **extra,
    }
    return json.dumps(data).encode()


class Calls:
    def __init__(self, status: int = 202, body: dict | None = None, error: Exception | None = None):
        self.status, self.body, self.error = status, body or {}, error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json=self.body)

    def sent(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def make(calls: Calls | None = None, **kwargs) -> tuple[Vonage, Calls, list[float]]:
    calls = calls or Calls()
    naps: list[float] = []
    clock = {"t": 1000.0}

    async def sleep(seconds: float) -> None:
        naps.append(seconds)
        clock["t"] += seconds

    vonage = Vonage(
        api_key=API_KEY,
        api_secret=API_SECRET,
        signature_secret=SIG_SECRET,
        sender=FROM,
        client=httpx.AsyncClient(transport=httpx.MockTransport(calls)),
        wall_clock=lambda: NOW,
        monotonic=lambda: clock["t"],
        sleep=sleep,
        **kwargs,
    )
    return vonage, calls, naps


def buttons(*actions: Action) -> list[Button]:
    return [Button(a, MANDATE, LABELS[a]) for a in actions]


# ----- the JWT -------------------------------------------------------------------------------


def test_a_good_token_passes():
    vonage = make()[0]
    body = inbound()
    assert vonage.verify(auth(jwt(body)), body) is True
    assert vonage.verify({"authorization": "bearer " + jwt(body)}, body) is True  # case


def test_an_altered_body_fails():
    vonage = make()[0]
    body = inbound(text="milk")
    assert vonage.verify(auth(jwt(body)), body.replace(b"milk", b"MILK")) is False
    assert vonage.verify(auth(jwt(body)), body + b" ") is False


def test_a_wrong_signature_secret_fails():
    vonage = make()[0]
    body = inbound()
    assert vonage.verify(auth(jwt(body, secret="another-secret")), body) is False


@pytest.mark.parametrize("alg", ["none", "None", "NONE", "HS384", "HS512", "RS256", "hs256", ""])
def test_any_algorithm_but_exactly_hs256_fails(alg):
    vonage = make()[0]
    body = inbound()
    assert vonage.verify(auth(jwt(body, alg=alg)), body) is False  # even with a valid HS256 MAC


def test_alg_none_with_an_empty_signature_fails():
    vonage = make()[0]
    body = inbound()
    token = jwt(body, alg="none", sign=False)
    assert vonage.verify(auth(token), body) is False
    assert vonage.verify(auth(token.rstrip(".")), body) is False


def test_a_missing_or_malformed_header_fails():
    vonage = make()[0]
    body = inbound()
    token = jwt(body)
    assert vonage.verify({}, body) is False
    assert vonage.verify({"Authorization": ""}, body) is False
    assert vonage.verify({"Authorization": "Bearer"}, body) is False
    assert vonage.verify({"Authorization": token}, body) is False  # no scheme
    assert vonage.verify({"Authorization": "Basic " + token}, body) is False
    assert vonage.verify({"Authorization": "Bearer " + token + "x"}, body) is False
    assert vonage.verify({"Authorization": "Bearer a.b"}, body) is False
    assert vonage.verify({"Authorization": "Bearer a.b.c.d"}, body) is False
    assert vonage.verify({"Authorization": "Bearer é.é.é"}, body) is False
    assert vonage.verify({"Authorization": "Bearer " + "a" * 5000}, body) is False


def test_an_old_or_future_or_missing_iat_fails_and_a_little_skew_is_fine():
    vonage = make()[0]
    body = inbound()

    def ok(**claims) -> bool:
        return vonage.verify(auth(jwt(body, claims=claims)), body)

    assert ok(iat=NOW - 299) is True
    assert ok(iat=NOW - 301) is False
    assert ok(iat=NOW + 30) is True
    assert ok(iat=NOW + 120) is False
    assert ok(iat="1800000000") is False
    assert ok(iat=True) is False
    assert ok(iat=None) is False
    assert vonage.verify(auth(jwt(body, drop=("iat",))), body) is False
    # NaN and Infinity are accepted by Python's JSON reader, so check they are refused
    for odd in ("NaN", "Infinity", "-Infinity"):
        head = b64(b'{"alg":"HS256"}')
        claims = (
            f'{{"api_key":"{API_KEY}","iat":{odd},"payload_hash":"{sha256(body).hexdigest()}"}}'
        )
        mid = b64(claims.encode())
        mac = hmac_new(SIG_SECRET.encode(), f"{head}.{mid}".encode(), sha256).digest()
        assert vonage.verify(auth(f"{head}.{mid}.{b64(mac)}"), body) is False


def test_a_wrong_or_missing_api_key_fails():
    vonage = make()[0]
    body = inbound()
    assert vonage.verify(auth(jwt(body, claims={"api_key": "other"})), body) is False
    assert vonage.verify(auth(jwt(body, claims={"api_key": 12345})), body) is False
    assert vonage.verify(auth(jwt(body, drop=("api_key",))), body) is False


def test_a_wrong_or_missing_payload_hash_fails():
    vonage = make()[0]
    body = inbound()
    assert vonage.verify(auth(jwt(body, claims={"payload_hash": "0" * 64})), body) is False
    assert vonage.verify(auth(jwt(body, claims={"payload_hash": 7})), body) is False
    assert vonage.verify(auth(jwt(body, drop=("payload_hash",))), body) is False


def test_with_no_secret_or_no_api_key_nothing_passes():
    body = inbound()
    for secret, key in (("", API_KEY), (SIG_SECRET, "")):
        vonage = Vonage(api_key=key, api_secret="s", signature_secret=secret, sender=FROM)
        assert vonage.can_receive is False
        token = jwt(body, secret=secret, claims={"api_key": key})
        assert vonage.verify(auth(token), body) is False


def test_the_signature_is_compared_in_constant_time():
    import inspect

    from ordr.bot import vonage as module

    source = inspect.getsource(module.Vonage.verify)
    assert source.count("compare_digest") >= 3  # signature, api key, payload hash


# ----- parsing -------------------------------------------------------------------------------


async def test_parses_text_with_the_uuid_as_message_id():
    (got,) = await make()[0].parse(inbound(text="milk", uuid="abc-1"))
    assert (got.message_id, got.number, got.text, got.action) == ("abc-1", PARENT, "milk", None)


async def test_parses_a_button_reply_as_a_press():
    body = inbound(
        "reply",
        sender=KID,
        reply={"id": f"approve:{MANDATE}", "title": "Approve and pay"},
        context={"message_uuid": "x"},
    )
    (got,) = await make()[0].parse(body)
    assert got.action is Action.APPROVE
    assert (got.mandate_id, got.number, got.text) == (MANDATE, KID, None)


async def test_typed_words_equal_to_a_label_or_an_id_are_text_not_a_press():
    vonage = make()[0]
    for typed in ("Approve and pay", "approve", f"approve:{MANDATE}", "Confirm"):
        (got,) = await vonage.parse(inbound(text=typed))
        assert got.action is None and got.mandate_id is None and got.text == typed


async def test_a_reply_that_is_not_ours_or_malformed_is_ignored():
    vonage = make()[0]
    for reply in (
        {"id": "hello", "title": "x"},
        {"id": f"destroy:{MANDATE}", "title": "x"},
        {"id": f"approve:{'x' * 101}", "title": "x"},
        {"title": "Confirm"},
        {"id": 5},
        "approve",
        None,
    ):
        assert await vonage.parse(inbound("reply", reply=reply)) == []
    assert await vonage.parse(inbound("reply")) == []


async def test_audio_is_a_marker_with_empty_bytes():
    (got,) = await make()[0].parse(inbound("audio", audio={"url": "https://example.test/a"}))
    assert got.audio == b"" and got.text is None and got.action is None


async def test_everything_else_is_ignored():
    vonage = make()[0]
    for kind in ("image", "video", "file", "location", "contact", "unsupported", "button", 5, None):
        assert await vonage.parse(inbound(kind, text="x")) == []


async def test_the_wrong_channel_recipient_or_ids_are_ignored():
    vonage = make()[0]
    assert await vonage.parse(inbound(text="a", to="4000000004")) == []
    assert await vonage.parse(inbound(text="a", to=3000000003)) == []
    for body in (
        json.loads(inbound(text="a")) | {"channel": "sms"},
        json.loads(inbound(text="a")) | {"channel": None},
        json.loads(inbound(text="a")) | {"message_uuid": ""},
        json.loads(inbound(text="a")) | {"message_uuid": 4},
        json.loads(inbound(text="a")) | {"from": ""},
        json.loads(inbound(text="a")) | {"from": 1000000001},
        {k: v for k, v in json.loads(inbound(text="a")).items() if k != "from"},
        json.loads(inbound(text="a")) | {"text": {"body": "a"}},
    ):
        assert await vonage.parse(json.dumps(body).encode()) == []
    for junk in (b"", b"not json", b"[]", b"null", b"\xff\xfe"):
        assert await vonage.parse(junk) == []


# ----- sending -------------------------------------------------------------------------------


async def test_a_text_goes_out_exactly_and_with_basic_auth():
    vonage, calls, _ = make()
    await vonage.send_text(PARENT, "Your milk is on the way")
    (request,) = calls.requests
    assert str(request.url) == SEND_URL
    assert request.method == "POST"
    expected = "Basic " + base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    assert request.headers["authorization"] == expected  # secret with * % ^ kept as is
    assert calls.sent() == {
        "from": FROM,
        "to": PARENT,
        "channel": "whatsapp",
        "message_type": "text",
        "text": "Your milk is on the way",
    }


async def test_buttons_go_out_in_order_with_the_same_object_meta_sends():
    vonage, calls, _ = make()
    chosen = buttons(Action.APPROVE, Action.OPEN_IN_STORE, Action.REJECT)
    await vonage.send_buttons(KID, "Order card", chosen)
    sent = calls.sent()
    assert {k: sent[k] for k in ("from", "to", "channel", "message_type")} == {
        "from": FROM,
        "to": KID,
        "channel": "whatsapp",
        "message_type": "custom",
    }
    assert sent["custom"]["type"] == "interactive"
    interactive = sent["custom"]["interactive"]
    assert interactive["type"] == "button"
    assert interactive["body"] == {"text": "Order card"}
    assert [b["reply"] for b in interactive["action"]["buttons"]] == [
        {"id": f"{b.action.value}:{MANDATE}", "title": b.label} for b in chosen
    ]
    assert all(b["type"] == "reply" for b in interactive["action"]["buttons"])
    # The same object Meta sends, so the two adapters cannot drift apart
    meta_calls = Calls(200)
    meta = Meta(
        token="t",
        app_secret="s",
        phone_id="1",
        verify_token="v",
        client=httpx.AsyncClient(transport=httpx.MockTransport(meta_calls)),
    )
    await meta.send_buttons(KID, "Order card", chosen)
    assert meta_calls.sent()["interactive"] == interactive


async def test_too_long_or_bad_messages_are_refused_without_a_request():
    vonage, calls, _ = make()
    three = buttons(Action.CONFIRM, Action.STOP)
    for coro in (
        vonage.send_text(PARENT, ""),
        vonage.send_text(PARENT, "x" * 4097),
        vonage.send_buttons(PARENT, "", three),
        vonage.send_buttons(PARENT, "x" * 1025, three),
        vonage.send_buttons(PARENT, "ok", []),
        vonage.send_buttons(PARENT, "ok", buttons(*list(Action)[:4])),
        vonage.send_buttons(PARENT, "ok", [Button(Action.CONFIRM, MANDATE, "y" * 21)]),
        vonage.send_buttons(PARENT, "ok", [Button(Action.CONFIRM, "m" * 300, "Confirm")]),
        vonage.send_buttons(
            PARENT, "ok", [Button(Action.CONFIRM, MANDATE, "A"), Button(Action.STOP, MANDATE, "A")]
        ),
    ):
        with pytest.raises(ChannelError) as caught:
            await coro
        assert caught.value.permanent is True
    assert calls.requests == []
    await vonage.send_text(PARENT, "x" * 4096)  # the limit itself is fine
    await vonage.send_buttons(PARENT, "x" * 1024, three)
    assert len(calls.requests) == 2


async def test_sends_are_spaced_at_least_1_1_seconds_and_one_at_a_time():
    import asyncio

    vonage, calls, naps = make()
    await vonage.send_text(PARENT, "a")
    assert naps == []  # the first one goes at once
    await asyncio.gather(*(vonage.send_text(PARENT, str(i)) for i in range(3)))
    assert len(calls.requests) == 4
    assert len(naps) == 3 and all(nap >= MIN_GAP - 1e-9 for nap in naps)
    assert MIN_GAP >= 1.1


async def test_the_spacing_also_holds_after_a_failed_send():
    vonage, _, naps = make(Calls(500))
    with pytest.raises(ChannelError):
        await vonage.send_text(PARENT, "a")
    with pytest.raises(ChannelError):
        await vonage.send_text(PARENT, "b")
    assert len(naps) == 1 and naps[0] >= 1.1 - 1e-9


@pytest.mark.parametrize(
    ("status", "permanent"),
    [
        (400, True),
        (401, True),
        (403, True),
        (404, True),
        (422, True),
        (408, False),
        (429, False),
        (500, False),
        (502, False),
        (503, False),
    ],
)
async def test_each_error_maps_to_a_channel_error(status, permanent):
    vonage, _, _ = make(Calls(status, {"title": "detail with 1000000001 and secret words"}))
    with pytest.raises(ChannelError) as caught:
        await vonage.send_text(PARENT, "private text")
    assert caught.value.permanent is permanent
    assert str(status) in str(caught.value)
    assert "1000000001" not in str(caught.value) and "private" not in str(caught.value)


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("no route")])
async def test_a_timeout_or_network_error_is_not_retried_here_and_is_not_permanent(error):
    vonage, calls, _ = make(Calls(error=error))
    with pytest.raises(ChannelError) as caught:
        await vonage.send_text(PARENT, "a")
    assert caught.value.permanent is False
    assert len(calls.requests) == 1  # one request. The outbox decides about a second
    assert type(error).__name__ in str(caught.value)


async def test_200_and_202_are_success_and_the_voice_is_not_built():
    for status in (200, 202):
        vonage, _, _ = make(Calls(status))
        await vonage.send_text(PARENT, "a")
    with pytest.raises(ChannelError) as caught:
        await vonage.send_voice(PARENT, b"x", "audio/ogg")
    assert caught.value.permanent is True


# ----- the routes ----------------------------------------------------------------------------


class RecordingPipeline:
    def __init__(self) -> None:
        self.handled = []

    async def handle(self, incoming) -> None:
        self.handled.append(incoming)


def make_app(pipeline=None, **settings):
    pipeline = pipeline if pipeline is not None else RecordingPipeline()
    vonage = make()[0]
    app = create_app(BotSettings(**settings), channel=vonage, pipeline=pipeline)
    return TestClient(app), pipeline


def post(client, path: str, body: bytes, token: str | None):
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return client.post(path, content=body, headers=headers)


def test_a_signed_message_gets_200_and_is_handled():
    client, pipeline = make_app()
    body = inbound(text="milk")
    assert post(client, "/vonage/inbound", body, jwt(body)).status_code == 200
    assert [(m.number, m.text) for m in pipeline.handled] == [(PARENT, "milk")]


def test_a_press_reaches_the_pipeline_as_a_press():
    client, pipeline = make_app()
    body = inbound("reply", sender=KID, reply={"id": f"confirm:{MANDATE}", "title": "Confirm"})
    assert post(client, "/vonage/inbound", body, jwt(body)).status_code == 200
    (got,) = pipeline.handled
    assert got.action is Action.CONFIRM and got.text is None


@pytest.mark.parametrize("path", ["/vonage/inbound", "/vonage/status"])
def test_nothing_is_read_before_the_token_is_checked(path):
    client, pipeline = make_app()
    body = inbound(text="milk")
    assert post(client, path, body, None).status_code == 403
    assert post(client, path, body, jwt(body, alg="none")).status_code == 403
    assert post(client, path, body, jwt(body, secret="other")).status_code == 403
    assert post(client, path, body + b" ", jwt(body)).status_code == 403
    assert post(client, path, body, jwt(body, claims={"api_key": "x"})).status_code == 403
    assert post(client, path, body, jwt(body, claims={"iat": NOW - 600})).status_code == 403
    assert client.post(path).status_code == 403
    assert pipeline.handled == []


@pytest.mark.parametrize("path", ["/vonage/inbound", "/vonage/status"])
def test_with_no_secret_set_everything_gets_403(path):
    vonage = Vonage(api_key=API_KEY, api_secret="s", signature_secret="", sender=FROM)
    client = TestClient(create_app(BotSettings(), channel=vonage, pipeline=RecordingPipeline()))
    body = inbound(text="milk")
    assert post(client, path, body, jwt(body)).status_code == 403
    assert post(client, path, body, jwt(body, secret="")).status_code == 403


@pytest.mark.parametrize("path", ["/vonage/inbound", "/vonage/status"])
def test_a_body_over_the_limit_is_refused(path):
    client, pipeline = make_app()
    body = inbound(text="x" * (256 * 1024))
    assert post(client, path, body, jwt(body)).status_code == 413
    assert pipeline.handled == []


def test_a_status_report_is_answered_and_nothing_else_happens(caplog):
    client, pipeline = make_app()
    for status in ("submitted", "delivered", "read"):
        body = json.dumps({"status": status, "message_uuid": "u9", "to": PARENT}).encode()
        assert post(client, "/vonage/status", body, jwt(body)).status_code == 200
    assert pipeline.handled == []
    assert [r for r in caplog.records if r.name == "ordr.bot.vonage"] == []


def test_a_failed_status_logs_only_the_status_and_the_error_code(caplog):
    client, _ = make_app()
    secret_bits = ("private words", PARENT, "u-secret")
    with caplog.at_level(logging.INFO):
        for error in (
            {"code": 131047, "reason": "private words"},
            {"title": "1300", "detail": "private words"},
        ):
            body = json.dumps(
                {"status": "failed", "message_uuid": "u-secret", "to": PARENT, "error": error}
            ).encode()
            assert post(client, "/vonage/status", body, jwt(body)).status_code == 200
        body = json.dumps(
            {"status": "rejected", "to": PARENT, "error": {"title": "Bad news"}}
        ).encode()
        assert post(client, "/vonage/status", body, jwt(body)).status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "ordr.bot.vonage"]
    assert lines == [
        "vonage reported a failed message (code 131047)",
        "vonage reported a failed message (code 1300)",
        "vonage reported a rejected message",
    ]
    assert not any(bit in caplog.text for bit in secret_bits)


class MemorySeen:
    """SeenStore's claim() without SQLite, which the test client's thread cannot share."""

    def __init__(self) -> None:
        self.ids: set[str] = set()

    def claim(self, message_id: str) -> bool:
        if message_id in self.ids:
            return False
        self.ids.add(message_id)
        return True


def test_a_repeated_message_uuid_is_handled_once_and_a_stranger_is_dropped():
    seen = MemorySeen()
    gate, channel, llm = FakeGate(), FakeChannel(), FakeReplier("Which milk?")
    pipeline = Pipeline(
        parent_number=PARENT,
        kid_number=KID,
        parent_lang="en",
        seen=seen,
        gate=gate,
        channel=channel,
        llm=llm,
    )
    client, _ = make_app(pipeline)
    try:
        press = inbound(
            "reply", sender=KID, uuid="p1", reply={"id": f"approve:{MANDATE}", "title": "x"}
        )
        for _ in range(3):
            assert post(client, "/vonage/inbound", press, jwt(press)).status_code == 200
        assert len(gate.acts) == 1
        words = inbound(text="milk", uuid="t1")
        for _ in range(2):
            assert post(client, "/vonage/inbound", words, jwt(words)).status_code == 200
        assert llm.calls == [(PARENT, "milk")]
        stranger = inbound(text="hello", sender="9999999999", uuid="s1")
        stranger_press = inbound(
            "reply",
            sender="9999999999",
            uuid="s2",
            reply={"id": f"approve:{MANDATE}", "title": "x"},
        )
        for body in (stranger, stranger_press):
            assert post(client, "/vonage/inbound", body, jwt(body)).status_code == 200
        assert len(gate.acts) == 1 and len(llm.calls) == 1
        assert [item[1] for item in channel.sent] == [PARENT]  # only the one real reply
    finally:
        assert seen.ids == {"p1", "t1"}  # strangers never reach the seen list


def test_the_vonage_routes_do_not_exist_unless_channel_is_vonage():
    meta = Meta(token="t", app_secret="s", phone_id="1", verify_token="v")
    client = TestClient(create_app(BotSettings(), channel=meta, pipeline=RecordingPipeline()))
    body = inbound(text="milk")
    assert post(client, "/vonage/inbound", body, jwt(body)).status_code == 404
    assert post(client, "/vonage/status", body, jwt(body)).status_code == 404
    # With the setting alone and no channel given, the routes are built too
    on = TestClient(
        create_app(
            BotSettings(
                channel="vonage",
                vonage_api_key=API_KEY,
                vonage_signature_secret=SIG_SECRET,
                vonage_from=FROM,
            ),
            pipeline=RecordingPipeline(),
        )
    )
    fresh = jwt(body, claims={"iat": time.time()})  # this app uses the real clock
    assert post(on, "/vonage/inbound", body, fresh).status_code == 200
    assert on.post("/vonage/inbound").status_code == 403


def test_webhook_is_shut_in_vonage_mode_even_with_a_meta_secret_left_in_env():
    client, _ = make_app(meta_app_secret="left-over-meta-secret")
    body = b"{}"
    signature = "sha256=" + hmac_new(b"left-over-meta-secret", body, sha256).hexdigest()
    reply = client.post("/webhook", content=body, headers={"x-hub-signature-256": signature})
    assert reply.status_code == 403
    assert client.post("/webhook").status_code == 403


# ----- settings ------------------------------------------------------------------------------


def full(**changes) -> BotSettings:
    values = {
        "internal_token": "i" * 32,
        "parent_number": PARENT,
        "kid_number": KID,
        "anthropic_api_key": "k",
        "channel": "vonage",
        "vonage_api_key": API_KEY,
        "vonage_api_secret": API_SECRET,
        "vonage_signature_secret": SIG_SECRET,
        "vonage_from": FROM,
    }
    return BotSettings(**(values | changes))


def test_vonage_settings_are_required_only_for_vonage():
    check_settings(full())
    for name in ("vonage_api_key", "vonage_api_secret", "vonage_signature_secret", "vonage_from"):
        with pytest.raises(StartupError) as caught:
            check_settings(full(**{name: " "}))
        assert name.upper() in str(caught.value)
        assert API_SECRET not in str(caught.value) and SIG_SECRET not in str(caught.value)
    with pytest.raises(StartupError):
        check_settings(full(vonage_from="+91 3000000003"))
    # Telegram and Meta do not need them
    check_settings(full(channel="telegram", telegram_bot_token="t", **{"vonage_api_key": ""}))
    with pytest.raises(StartupError) as caught:
        check_settings(full(channel="meta"))
    assert "VONAGE" not in str(caught.value)


@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_env_loading_keeps_the_secret_characters_exactly(tmp_path, quote):
    secret = "ab*cd%ef^gh&ij!kl~mn=op/qr+st-uv_wx"
    env = tmp_path / ".env"
    env.write_text(
        f"VONAGE_API_KEY={quote}{API_KEY}{quote}\n"
        f"VONAGE_API_SECRET={quote}{secret}{quote}\n"
        f"VONAGE_SIGNATURE_SECRET={quote}{secret}{quote}\n"
        f"VONAGE_FROM={FROM}\n",
        encoding="utf-8",
    )
    loaded = BotSettings(_env_file=env)
    assert loaded.vonage_api_secret == secret
    assert loaded.vonage_signature_secret == secret
    assert (loaded.vonage_api_key, loaded.vonage_from) == (API_KEY, FROM)


def test_the_odd_secret_signs_and_verifies_end_to_end():
    vonage = Vonage(
        api_key=API_KEY,
        api_secret="s",
        signature_secret="*%^",
        sender=FROM,
        wall_clock=lambda: NOW,
    )
    body = inbound(text="a")
    assert vonage.verify(auth(jwt(body, secret="*%^")), body) is True
    assert vonage.verify(auth(jwt(body, secret="*%")), body) is False


# ----- no secrets in logs --------------------------------------------------------------------


async def test_no_secret_number_text_or_body_reaches_a_log(caplog):
    from ordr.bot.outbox import Outbox

    errors = [
        Calls(403, {"detail": "number 1000000001 not joined"}),
        Calls(error=httpx.ReadTimeout("slow")),
    ]
    with caplog.at_level(logging.DEBUG):
        for calls in errors:
            vonage, _, _ = make(calls)
            gate = FakeGate()
            gate.rows = [
                {
                    "id": 1,
                    "number": PARENT,
                    "kind": "k",
                    "text": "very private grocery text",
                    "buttons": [],
                }
            ]
            await Outbox(gate, vonage).tick()
        client, _ = make_app()
        for body in (inbound(text="very private grocery text"), b"junk"):
            post(client, "/vonage/inbound", body, jwt(b"other"))
            post(client, "/vonage/inbound", body, jwt(body))
            post(client, "/vonage/status", body, jwt(body))
    for forbidden in (
        SIG_SECRET,
        API_SECRET,
        API_KEY,
        jwt(b"other"),
        "very private grocery text",
        PARENT,
        FROM,
        "not joined",
    ):
        assert forbidden not in caplog.text
