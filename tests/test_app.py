"""The gate app over real HTTP: MCP at /mcp, /internal, outbox, start-up (TRD N7)."""

import asyncio
import json
import logging
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from test_service import ATTA, MILK, item

from ordr.config import Settings
from ordr.gate import app as gate_app
from ordr.gate import db as gdb
from ordr.gate import server as gate_server
from ordr.gate.app import StartupError, create_app, sweep_forever
from ordr.gate.messages import money
from ordr.gate.service import Gate
from ordr.gate.store_client import StoreClient
from ordr.gate.upi import SimulatedUpi
from ordr.store.server import STORE
from ordr.store.server import mcp as store_mcp
from ordr.store.state import ADDRESSES

ROOT = Path(__file__).parent.parent
TOKEN = "i" * 40
KEY = "s" * 40
PARENT, KID, STRANGER = "919800000001", "919800000002", "919800000003"
PHRASE = "purple tiger"


def settings_for(tmp_path, **over) -> Settings:
    base = {
        "server_key": KEY,
        "internal_token": TOKEN,
        "db_path": tmp_path / "gate.db",
        "policy_path": ROOT / "policy.json",
        "parent_number": PARENT,
        "kid_number": KID,
        "safe_phrase": PHRASE,
        "parent_lang": "en",
    }
    return Settings(_env_file=None, **{**base, **over})


def make_app(tmp_path, sweep_seconds=60.0, **over):
    STORE.reset()
    upi = SimulatedUpi(
        "http://store.test/demo/pay", transport=httpx.ASGITransport(app=store_mcp.http_app())
    )
    return create_app(
        settings_for(tmp_path, **over),
        store=StoreClient(store_mcp),
        upi=upi,
        sweep_seconds=sweep_seconds,
    )


class Live:
    """The app on a free local port, in a thread, with its real start-up and shutdown."""

    def __init__(self, app) -> None:
        self.app = app
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def actions(self):
        return self.app.state.actions

    @property
    def db(self):
        return self.app.state.actions.db

    def __enter__(self) -> "Live":
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the gate did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(15)
        self.db.close()

    # ----- calls ---------------------------------------------------------------------

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, trust_env=False, timeout=20)

    async def call(self, method, path, *, number=None, token=TOKEN, **kwargs) -> httpx.Response:
        headers = {}
        if token is not None:
            headers["x-ordr-internal"] = token
        if number is not None:
            headers["x-ordr-number"] = number
        async with self.http() as client:
            return await client.request(method, path, headers=headers, **kwargs)

    async def press(self, action, mandate_id, number=KID, mid=None, **kwargs) -> dict:
        body = {
            "message_id": mid or f"wamid.{time.monotonic_ns()}",
            "action": action,
            "mandate_id": mandate_id,
        }
        response = await self.call("POST", "/internal/action", number=number, json=body, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    async def outbox(self) -> list[dict]:
        response = await self.call("GET", "/internal/outbox")
        assert response.status_code == 200
        return response.json()

    async def drain(self) -> list[dict]:
        """Fetch and acknowledge, as the webhook does."""
        rows = await self.outbox()
        if rows:
            ack = await self.call(
                "POST", "/internal/outbox/ack", json={"ids": [r["id"] for r in rows]}
            )
            assert ack.json() == {"ok": True}
        return rows

    async def tool(self, name, args=None, *, number=PARENT, token=TOKEN):
        headers = {}
        if token is not None:
            headers["x-ordr-internal"] = token
        if number is not None:
            headers["x-ordr-number"] = number
        transport = StreamableHttpTransport(f"{self.url}/mcp", headers=headers)
        async with Client(transport) as client:
            return await client.call_tool(name, args or {})

    async def prepared(self) -> str:
        """Search for atta and milk, then prepare both, in one MCP session."""
        headers = {"x-ordr-internal": TOKEN, "x-ordr-number": PARENT}
        async with Client(StreamableHttpTransport(f"{self.url}/mcp", headers=headers)) as client:
            await client.call_tool("search_products", {"query": "atta"})
            await client.call_tool("search_products", {"query": "milk"})
            result = await client.call_tool("prepare_order", {"items": [item(ATTA), item(MILK)]})
        assert result.structured_content["status"] == "prepared"
        return result.structured_content["mandate_id"]

    def state(self, mandate_id) -> str:
        return gdb.get_mandate(self.db.conn(), mandate_id)["state"]

    def events(self) -> list[str]:
        return [r[0] for r in self.db.conn().execute("SELECT event FROM audit_log ORDER BY seq")]


@pytest.fixture
def live(tmp_path):
    with Live(make_app(tmp_path)) as running:
        yield running


# ----- the whole typed flow ----------------------------------------------------------------


async def test_full_typed_flow_over_http_from_search_to_receipt(live, caplog):
    caplog.set_level(logging.DEBUG)
    async with Client(StreamableHttpTransport(f"{live.url}/mcp")) as anon:
        assert {t.name for t in await anon.list_tools()} == {
            "search_products",
            "prepare_order",
            "get_order_status",
            "abort_order",
            "get_audit_log",
        }
    mandate_id = await live.prepared()
    row = gdb.get_mandate(live.db.conn(), mandate_id)

    readback = await live.drain()
    assert [r["kind"] for r in readback] == ["readback"]
    first = readback[0]
    assert (first["number"], first["speak"], first["lang"]) == (PARENT, True, "en")
    assert money(row["total_paise"]) in first["text"]
    assert f"up to {money(row['max_total_paise'])}" in first["text"]
    assert "Sunrise Whole Wheat Atta" in first["text"] and PHRASE not in first["text"]
    assert first["buttons"] == [
        {"action": "confirm", "mandate_id": mandate_id, "label": "Confirm"},
        {"action": "stop", "mandate_id": mandate_id, "label": "Stop"},
    ]
    assert await live.outbox() == []  # acknowledged

    assert await live.press("confirm", mandate_id, PARENT) == {"ok": True, "code": "confirmed"}
    cards = {r["kind"]: r for r in await live.drain()}
    assert set(cards) == {"kid_card", "sent_to_kid"}
    assert cards["kid_card"]["number"] == KID and PHRASE in cards["kid_card"]["text"]
    assert cards["kid_card"]["speak"] is False and cards["kid_card"]["lang"] == "en"
    assert [b["label"] for b in cards["kid_card"]["buttons"]] == ["Approve and pay", "Reject"]
    assert cards["sent_to_kid"]["number"] == PARENT and PHRASE not in cards["sent_to_kid"]["text"]

    assert await live.press("approve", mandate_id, KID) == {"ok": True, "code": "placed"}
    assert len(STORE.orders) == 1
    pay = await live.drain()
    assert [r["kind"] for r in pay] == ["payment"] and pay[0]["number"] == KID
    assert "simulated" in pay[0]["text"]
    assert [b["action"] for b in pay[0]["buttons"]] == ["pay", "dont_pay"]

    assert await live.press("pay", mandate_id, KID) == {"ok": True, "code": "paid"}
    receipts = await live.drain()
    assert sorted(r["number"] for r in receipts) == [PARENT, KID]
    assert {r["kind"] for r in receipts} == {"receipt"}
    assert len(STORE.orders) == 1

    status = await live.tool("get_order_status", {"mandate_id": mandate_id})
    assert status.structured_content["state"] == "PAID"
    entries = (await live.tool("get_audit_log", {})).structured_content["entries"]
    assert "paid" in {e["event"] for e in entries}

    for number in (PARENT, KID):
        text = (await live.call("GET", "/internal/log", number=number)).text
        assert "PLACED_PENDING_PAYMENT -> PAID" in text
        assert PARENT not in text and KID not in text

    heard = " ".join(rec.getMessage() for rec in caplog.records)
    for secret in (PARENT, KID, TOKEN, KEY, PHRASE):
        assert secret not in heard


async def test_the_default_parent_language_is_kannada_and_the_kid_gets_english(tmp_path):
    with Live(make_app(tmp_path, parent_lang="kn")) as live:
        mandate_id = await live.prepared()
        parent = (await live.drain())[0]
        assert parent["lang"] == "kn" and parent["speak"] is True
        assert any("ಀ" <= ch <= "೿" for ch in parent["text"])
        await live.press("confirm", mandate_id, PARENT)
        kid = next(r for r in await live.drain() if r["kind"] == "kid_card")
        assert kid["lang"] == "en" and "Safe phrase" in kid["text"]
    assert Settings(_env_file=None).parent_lang == "kn"


# ----- who may call -----------------------------------------------------------------------


async def test_a_wrong_or_missing_token_gets_403_on_every_internal_route(live):
    mandate_id = await live.prepared()
    good = {"message_id": "w1", "action": "confirm", "mandate_id": mandate_id}
    for token in (None, "", "x" * 40, TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
        for method, path, body in [
            ("POST", "/internal/action", good),
            ("POST", "/internal/action", {"nonsense": 1}),  # bad body: still 403, not 422
            ("GET", "/internal/outbox", None),
            ("POST", "/internal/outbox/ack", {"ids": [1]}),
            ("GET", "/internal/log", None),
        ]:
            response = await live.call(method, path, number=PARENT, token=token, json=body)
            assert response.status_code == 403, (path, token, response.text)
    assert live.state(mandate_id) == "PREPARED"
    assert len(await live.outbox()) == 1  # nothing was acknowledged either


async def test_mcp_refuses_a_wrong_token_the_kids_number_and_strangers(live):
    await live.tool("search_products", {"query": "atta"})
    for token, number in [
        (TOKEN, KID),
        (TOKEN, STRANGER),
        (TOKEN, None),
        ("x" * 40, PARENT),
        (None, PARENT),
    ]:
        for name, args in [
            ("search_products", {"query": "atta"}),
            ("prepare_order", {"items": [item(ATTA)]}),
            ("get_audit_log", {}),
        ]:
            with pytest.raises(ToolError, match="not_found"):
                await live.tool(name, args, number=number, token=token)
    assert live.db.conn().execute("SELECT COUNT(*) FROM mandates").fetchone()[0] == 0
    assert "update_cart" not in STORE.__dict__ and STORE.cart == {}


async def test_the_number_in_the_body_is_refused_and_only_the_header_counts(live):
    mandate_id = await live.prepared()
    body = {"message_id": "n1", "action": "confirm", "mandate_id": mandate_id}
    refused = await live.call(
        "POST", "/internal/action", number=STRANGER, json={**body, "number": PARENT}
    )
    assert refused.status_code == 422
    assert live.state(mandate_id) == "PREPARED"
    # The kid's number in the header cannot press a parent button, whatever the body says.
    assert await live.press("confirm", mandate_id, KID, mid="n2") == {
        "ok": False,
        "code": "not_found",
    }
    assert await live.press("confirm", mandate_id, STRANGER, mid="n3") == {
        "ok": False,
        "code": "not_found",
    }
    assert await live.press("confirm", mandate_id, None, mid="n4") == {
        "ok": False,
        "code": "not_found",
    }
    assert live.state(mandate_id) == "PREPARED"
    assert await live.press("confirm", mandate_id, PARENT, mid="n5") == {
        "ok": True,
        "code": "confirmed",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"message_id": "a", "action": "confirm"},
        {"message_id": "a", "action": "confirm", "mandate_id": "m", "extra": 1},
        {"message_id": 5, "action": "confirm", "mandate_id": "m"},
        {"message_id": "", "action": "confirm", "mandate_id": "m"},
        {"message_id": "a" * 201, "action": "confirm", "mandate_id": "m"},
    ],
)
async def test_action_bodies_are_strict(live, body):
    response = await live.call("POST", "/internal/action", number=PARENT, json=body)
    assert response.status_code == 422


async def test_a_repeated_message_id_counts_once(live):
    mandate_id = await live.prepared()
    await live.press("confirm", mandate_id, PARENT)
    await live.drain()
    first = await live.press("approve", mandate_id, KID, mid="wamid.same")
    again = await live.press("approve", mandate_id, KID, mid="wamid.same")
    assert first == again == {"ok": True, "code": "placed"}
    assert len(STORE.orders) == 1
    assert [r["kind"] for r in await live.outbox()] == ["payment"]
    # A different message for the same button is a new press, and the order has moved on.
    other = await live.press("approve", mandate_id, KID)
    assert other == {"ok": False, "code": "not_current"}
    assert len(STORE.orders) == 1


# ----- outbox ------------------------------------------------------------------------------


async def test_outbox_is_in_id_order_and_repeats_until_acknowledged(live):
    mandate_id = await live.prepared()
    await live.press("confirm", mandate_id, PARENT)
    first = await live.outbox()
    again = await live.outbox()
    assert [r["id"] for r in first] == sorted(r["id"] for r in first)
    # The read-back is gone: Confirm moved the mandate on, so it is no longer true.
    assert first == again and [r["kind"] for r in first] == ["kid_card", "sent_to_kid"]
    assert set(first[0]) == {"id", "number", "kind", "text", "speak", "lang", "buttons"}
    # Acknowledge one. Unknown and repeated IDs do no harm.
    done = await live.call(
        "POST", "/internal/outbox/ack", json={"ids": [first[0]["id"], first[0]["id"], 999999]}
    )
    assert done.json() == {"ok": True}
    assert [r["id"] for r in await live.outbox()] == [first[1]["id"]]
    await live.call("POST", "/internal/outbox/ack", json={"ids": [first[1]["id"]]})
    assert await live.outbox() == []


@pytest.mark.parametrize(
    "body", [{}, {"ids": "1"}, {"ids": ["1"]}, {"ids": [True]}, {"ids": [0]}, {"ids": [1], "x": 1}]
)
async def test_ack_bodies_are_strict(live, body):
    assert (await live.call("POST", "/internal/outbox/ack", json=body)).status_code == 422


async def _dropped(live) -> list[dict]:
    rows = live.db.conn().execute(
        "SELECT data_json FROM audit_log WHERE event = 'outbox_dropped' ORDER BY seq"
    )
    return [json.loads(r[0]) for r in rows]


async def test_a_stale_payment_row_is_never_sent(live):
    mandate_id = await live.prepared()
    await live.drain()
    await live.press("confirm", mandate_id, PARENT)
    await live.drain()
    assert (await live.press("approve", mandate_id, KID))["code"] == "placed"
    # The payment message is queued but not fetched yet. The parent stops the order.
    assert (await live.press("stop", mandate_id, PARENT))["code"] == "needs_review"
    assert live.state(mandate_id) == "NEEDS_REVIEW"
    rows = await live.drain()
    kinds = [r["kind"] for r in rows]
    assert "payment" not in kinds
    assert kinds == ["stopped", "needs_review", "needs_review"]
    assert "Do not pay" in rows[0]["text"] and rows[0]["number"] == KID
    kid_review = next(r for r in rows if r["kind"] == "needs_review" and r["number"] == KID)
    assert [b["label"] for b in kid_review["buttons"]] == ["I have checked it"]
    sent = live.db.conn().execute("SELECT sent_at FROM outbox WHERE kind = 'payment'").fetchone()
    assert sent["sent_at"] is not None
    dropped = await _dropped(live)
    assert [d["kind"] for d in dropped] == ["payment"]
    assert dropped[0]["why"] in ("stop_requested", "state_NEEDS_REVIEW")
    assert "outbox_dropped" in live.events()
    assert await live.outbox() == []


async def test_a_stale_readback_is_never_sent_after_stop(live):
    mandate_id = await live.prepared()
    assert (await live.press("stop", mandate_id, PARENT))["code"] == "stopped"
    kinds = [r["kind"] for r in await live.drain()]
    assert kinds == ["stopped"]
    assert [d["kind"] for d in await _dropped(live)] == ["readback"]


async def test_a_stale_kid_card_is_never_sent_after_stop(live):
    mandate_id = await live.prepared()
    await live.drain()
    await live.press("confirm", mandate_id, PARENT)
    assert (await live.press("stop", mandate_id, PARENT))["code"] == "stopped"
    rows = await live.drain()
    assert "kid_card" not in [r["kind"] for r in rows]
    assert [d["kind"] for d in await _dropped(live)] == ["kid_card"]
    assert await live.press("approve", mandate_id, KID) == {"ok": False, "code": "not_current"}
    assert STORE.orders == []


async def test_a_stale_replace_cart_row_is_never_sent(live):
    STORE.switch_preload_cart()
    await live.tool("search_products", {"query": "atta"})
    result = await live.tool("prepare_order", {"items": [item(ATTA)]})
    assert result.structured_content["status"] == "waiting_for_kid"
    mandate_id = result.structured_content["mandate_id"]
    assert (await live.press("not_now", mandate_id, KID))["code"] == "not_now"
    kinds = [r["kind"] for r in await live.drain()]
    assert "replace_cart" not in kinds
    assert kinds == ["waiting_for_kid", "order_failed"]
    assert [d["kind"] for d in await _dropped(live)] == ["replace_cart"]


async def test_a_fresh_replace_cart_row_is_sent_with_its_buttons(live):
    STORE.switch_preload_cart()
    await live.tool("search_products", {"query": "atta"})
    result = await live.tool("prepare_order", {"items": [item(ATTA)]})
    rows = {r["kind"]: r for r in await live.outbox()}
    assert [b["label"] for b in rows["replace_cart"]["buttons"]] == ["Replace", "Not now"]
    assert rows["replace_cart"]["number"] == KID and PHRASE in rows["replace_cart"]["text"]
    assert rows["waiting_for_kid"]["number"] == PARENT
    assert result.structured_content["status"] == "waiting_for_kid"


async def test_a_row_with_no_template_is_closed_and_logged_not_sent(live):
    mandate_id = await live.prepared()
    await live.drain()
    with live.db.tx() as conn:
        household = conn.execute("SELECT id FROM households").fetchone()["id"]
        gdb.enqueue(
            conn,
            household,
            gdb.OutboxMsg(gdb.Role.PARENT, "invented_kind", {"mandate_id": mandate_id}),
        )
    assert await live.outbox() == []
    assert [d["why"] for d in await _dropped(live)] == ["unrenderable"]


# ----- one Gate -----------------------------------------------------------------------------


async def test_no_new_gate_is_made_per_request(live, monkeypatch):
    gate = live.actions.gate
    made = []
    real_init = Gate.__init__

    def counting(self, *args, **kwargs):
        made.append(1)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Gate, "__init__", counting)

    def refuse(*args, **kwargs):
        raise AssertionError("a Gate must not be built while serving")

    monkeypatch.setattr(gate_server, "build_gate", refuse)
    monkeypatch.setattr(gate_server, "build_actions", refuse)
    mandate_id = await live.prepared()
    await live.tool("get_order_status", {"mandate_id": mandate_id})
    await live.press("confirm", mandate_id, PARENT)
    await live.outbox()
    await live.call("GET", "/internal/log", number=PARENT)
    assert made == []
    assert gate_server.get_gate() is gate is live.app.state.actions.gate
    assert gate.account_lock("acct") is gate.account_lock("acct")
    # MCP and /internal see the same mandate through the same Gate and database.
    assert live.state(mandate_id) == "PARENT_CONFIRMED"


def test_the_gate_serves_on_loopback_only_with_one_worker():
    assert gate_app.HOST == "127.0.0.1" and gate_app.PORT == 8002


# ----- log ---------------------------------------------------------------------------------


async def test_log_is_plain_text_for_either_person_and_for_their_household_only(live):
    mandate_id = await live.prepared()
    with live.db.tx() as conn:
        other = live.db.conn().execute("SELECT id FROM households").fetchone()["id"]
        stranger_household = gdb.create_household(
            conn,
            parent_number="911111111111",
            kid_number="912222222222",
            store_account="acct-other",
            address_id="addr_home",
            address_text="Somewhere else",
        )
        gdb.append_audit(conn, stranger_household, "other-mandate-id", "secret_event", {})
    for number in (PARENT, KID):
        response = await live.call("GET", "/internal/log", number=number)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "mandate_created" in response.text and mandate_id[:8] in response.text
        assert "secret_event" not in response.text and "other-man" not in response.text
        assert PARENT not in response.text and KID not in response.text
        assert "IST" in response.text
    assert other
    for number in (STRANGER, None):
        assert (await live.call("GET", "/internal/log", number=number)).status_code == 403
    theirs = await live.call("GET", "/internal/log", number="911111111111")
    assert "secret_event" in theirs.text and mandate_id[:8] not in theirs.text


# ----- start-up ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["server_key", "internal_token", "parent_number", "kid_number", "safe_phrase"]
)
def test_the_gate_refuses_to_start_when_a_needed_key_is_empty(tmp_path, name):
    for blank in ("", "   "):
        with pytest.raises(StartupError, match=name.upper()):
            make_app(tmp_path, **{name: blank})


@pytest.mark.parametrize(
    ("over", "word"),
    [
        ({"parent_number": "+919800000001"}, "digits"),
        ({"kid_number": "98 00 00"}, "digits"),
        ({"parent_number": "abc"}, "digits"),
        ({"kid_number": PARENT}, "different"),
        ({"parent_lang": "fr"}, "PARENT_LANG"),
        ({"safe_phrase": "w" * 41}, "SAFE_PHRASE"),
        ({"server_key": TOKEN}, "different"),
        ({"internal_token": "short"}, "INTERNAL_TOKEN"),
        ({"server_key": "short"}, "SERVER_KEY"),
    ],
)
def test_the_gate_refuses_unusable_settings_without_echoing_them(tmp_path, over, word):
    with pytest.raises(StartupError, match=word) as err:
        make_app(tmp_path, **over)
    for value in over.values():
        if len(value) > 5:
            assert value not in str(err.value)


def test_a_safe_phrase_of_the_longest_allowed_size_starts(tmp_path):
    app = make_app(tmp_path, safe_phrase="  " + "w" * 40 + "  ")
    app.state.actions.db.close()


def test_the_demo_household_is_made_once_with_the_stores_own_address(tmp_path):
    with Live(make_app(tmp_path)) as live:
        rows = live.db.conn().execute("SELECT * FROM households").fetchall()
        assert len(rows) == 1
        household = rows[0]
        assert (household["parent_number"], household["kid_number"]) == (PARENT, KID)
        assert household["address_id"] == ADDRESSES[0]["id"]
        assert household["address_text"] == ADDRESSES[0]["addressLine"]
        first_id = household["id"]
    with Live(make_app(tmp_path)) as live:  # a restart on the same database
        rows = live.db.conn().execute("SELECT id FROM households").fetchall()
        assert [r["id"] for r in rows] == [first_id]


def test_changed_numbers_say_to_delete_the_database(tmp_path):
    with Live(make_app(tmp_path)):
        pass
    for over in ({"kid_number": "919800000009"}, {"parent_number": "919800000008"}):
        with pytest.raises(StartupError, match="delete") as err:
            make_app(tmp_path, **over)
        assert "gate.db" in str(err.value)
        for number in (*over.values(), PARENT, KID):
            assert number not in str(err.value)


def test_the_timer_runs_in_the_app_and_stops_with_it(tmp_path):
    app = make_app(tmp_path, sweep_seconds=0.02)
    calls = []
    real = app.state.actions.sweep_payments

    async def counting():
        calls.append(1)
        return await real()

    app.state.actions.sweep_payments = counting
    with Live(app):
        deadline = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) >= 3
    stopped = len(calls)
    time.sleep(0.2)
    assert len(calls) == stopped
    assert gate_server._gate is None


async def test_one_failure_does_not_stop_the_timer(monkeypatch, caplog):
    sweeps, expiries = [], []

    class Actions:
        db = None

        class gate:  # noqa: N801
            clock = staticmethod(lambda: None)

        async def sweep_payments(self):
            sweeps.append(1)
            if len(sweeps) in (1, 2):
                raise RuntimeError("5551234567 secret store failure")

    def expire(db, now):
        expiries.append(1)
        if len(expiries) == 2:
            raise sqlite_error()

    def sqlite_error():
        import sqlite3

        return sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gdb, "expire_due", expire)
    caplog.set_level(logging.INFO)
    task = asyncio.create_task(sweep_forever(Actions(), 0.01))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if len(sweeps) >= 5 and len(expiries) >= 5:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sweeps) >= 5 and len(expiries) >= 5
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "RuntimeError" in logged and "OperationalError" in logged
    assert "5551234567" not in logged and "secret" not in logged
