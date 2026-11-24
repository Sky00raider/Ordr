"""The five assistant tools against the dummy store, in process (TRD N5, PRD sections 5 and 6)."""

import asyncio
import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ordr.config import load_policy
from ordr.contracts import CartLine, State
from ordr.gate import db as gdb
from ordr.gate import server as gate_server
from ordr.gate.db import Database, transition
from ordr.gate.service import AccessDenied, Gate, GateError, WeakInternalToken
from ordr.gate.store_client import StoreClient, StoreUnavailable
from ordr.store.server import STORE
from ordr.store.server import mcp as store_mcp

TOKEN = "t" * 40
PARENT, KID, STRANGER = "parent-1", "kid-1", "stranger-1"
HOME_TEXT = "12, 3rd Cross, Jayanagar 4th Block, Bengaluru 560011"
T0 = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)

ATTA = ("sku_sunrise_atta_5kg", "spin_sunrise_atta_5kg")
MILK = ("sku_nandini_toned_1l", "spin_nandini_toned_1l")
OIL5 = ("sku_brightfield_oil_5l", "spin_brightfield_oil_5l")


def item(ids: tuple[str, str], qty: int = 1) -> dict:
    return {"sku_id": ids[0], "spin_id": ids[1], "qty": qty, "heard_as": "test"}


class World:
    """A gate, a database, one household (parent-1 and kid-1) and a frozen clock."""

    def __init__(self, tmp_path) -> None:
        STORE.reset()
        self.now = [T0]
        self.db = Database(tmp_path / "gate.db")
        self.calls: list[str] = []
        self.store = StoreClient(store_mcp)
        real_call = self.store._call

        async def spy(tool, args):
            self.calls.append(tool)
            return await real_call(tool, args)

        self.store._call = spy  # type: ignore[method-assign]
        self.gate = Gate(self.db, self.store, load_policy(), TOKEN, clock=lambda: self.now[0])
        with self.db.tx() as conn:
            self.hid = self.household(conn, PARENT, KID, "acct1")
        self.parent = self.gate.authenticate(TOKEN, PARENT)

    def household(self, conn, parent: str, kid: str, account: str) -> str:
        return gdb.create_household(
            conn,
            parent_number=parent,
            kid_number=kid,
            store_account=account,
            address_id="addr_home",
            address_text=HOME_TEXT,
        )

    async def search(self, *queries: str, caller=None) -> None:
        for query in queries:
            await self.gate.search_products(caller or self.parent, query)

    async def prepare(self, *items: dict) -> dict:
        return await self.gate.prepare_order(self.parent, list(items))

    def row(self, mandate_id: str):
        with self.db.tx() as conn:
            return gdb.get_mandate(conn, mandate_id)

    def state(self, mandate_id: str) -> State:
        return State(self.row(mandate_id)["state"])

    def open_count(self) -> int:
        marks = ", ".join(f"'{s.value}'" for s in gdb.OPEN_STATES)
        return (
            self.db.conn()
            .execute(
                f"SELECT COUNT(*) FROM mandates WHERE state IN ({marks})"  # noqa: S608
            )
            .fetchone()[0]
        )

    def outbox(self, kind: str | None = None) -> list[dict]:
        rows = self.db.conn().execute("SELECT * FROM outbox ORDER BY id").fetchall()
        return [
            {"to": r["to_role"], "kind": r["kind"], **json.loads(r["payload_json"])}
            for r in rows
            if kind in (None, r["kind"])
        ]

    def fake(self, state: State, **cols) -> str:
        """An earlier mandate written straight into the table."""
        mandate_id = uuid.uuid4().hex
        created = cols.pop("created", self.now[0])
        values = {
            "id": mandate_id,
            "household_id": self.hid,
            "store_account": "acct1",
            "state": state.value,
            "request_json": "{}",
            "nonce": mandate_id,
            "created_at": gdb.iso(created),
            "expires_at": gdb.iso(created + timedelta(hours=1)),
            "reviewed_at": gdb.iso(created) if state is State.NEEDS_REVIEW else None,
            **cols,
        }
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self.db.tx() as conn:
            conn.execute(f"INSERT INTO mandates ({names}) VALUES ({marks})", list(values.values()))  # noqa: S608
        return mandate_id


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.db.close()


# ----- who may call --------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["", "   ", "short", "t" * 31])
def test_a_missing_or_short_internal_token_stops_the_gate_starting(tmp_path, token):
    with pytest.raises(WeakInternalToken):
        Gate(Database(tmp_path / "x.db"), StoreClient(store_mcp), load_policy(), token)


def test_only_the_parent_with_the_right_token_gets_in(w):
    assert w.gate.authenticate(TOKEN, PARENT).household_id == w.hid
    for token, number in [
        (TOKEN, KID),
        (TOKEN, STRANGER),
        (TOKEN, ""),
        (TOKEN, None),
        ("x" * 40, PARENT),
        ("", PARENT),
        (None, PARENT),
        (TOKEN[:-1], PARENT),
        (TOKEN + "x", PARENT),
    ]:
        with pytest.raises(AccessDenied):
            w.gate.authenticate(token, number)


@pytest.fixture
def over_mcp(w, monkeypatch):
    """Call the real MCP tools with chosen webhook headers."""
    gate_server.set_gate(w.gate)
    headers: dict[str, str] = {}
    monkeypatch.setattr(gate_server, "get_http_headers", lambda: dict(headers))

    async def call(tool, args, token=TOKEN, number=PARENT):
        headers.clear()
        if token is not None:
            headers["x-ordr-internal"] = token
        if number is not None:
            headers["x-ordr-number"] = number
        async with Client(gate_server.mcp) as client:
            return await client.call_tool(tool, args)

    yield call
    gate_server.set_gate(None)


async def test_mcp_accepts_the_parent_only(w, over_mcp):
    ok = await over_mcp("search_products", {"query": "atta"})
    assert ok.structured_content["products"]
    for token, number in [
        (TOKEN, KID),
        (TOKEN, STRANGER),
        (TOKEN, None),
        (None, PARENT),
        ("bad", PARENT),
    ]:
        with pytest.raises(ToolError, match="not_found"):
            await over_mcp("search_products", {"query": "atta"}, token, number)
        with pytest.raises(ToolError, match="not_found"):
            await over_mcp("prepare_order", {"items": [item(ATTA)]}, token, number)
        with pytest.raises(ToolError, match="not_found"):
            await over_mcp("get_audit_log", {}, token, number)
    assert w.calls.count("update_cart") == 0
    assert w.open_count() == 0


async def test_mcp_prepare_order_fills_the_cart_for_the_parent(w, over_mcp):
    await over_mcp("search_products", {"query": "atta"})
    await over_mcp("search_products", {"query": "milk"})
    result = await over_mcp("prepare_order", {"items": [item(ATTA), item(MILK)]})
    assert result.structured_content["status"] == "prepared"
    with pytest.raises(ToolError, match="item_not_from_search"):
        await over_mcp("prepare_order", {"items": [item(OIL5)]})


# ----- search_products -----------------------------------------------------------------------


async def test_search_returns_the_contract_shape_and_remembers_what_it_showed(w):
    out = await w.gate.search_products(w.parent, "  milk ")
    one = next(p for p in out["products"] if p["sku_id"] == MILK[0])
    assert one == {
        "sku_id": MILK[0],
        "spin_id": MILK[1],
        "name": "Nandini Toned Milk 1 L",
        "brand": "Nandini",
        "pack": "1 L",
        "price_paise": 5500,
        "in_stock": True,
        "max_qty": 10,
        "household_default": False,
    }
    assert w.db.conn().execute("SELECT COUNT(*) FROM search_results").fetchone()[0] == len(
        out["products"]
    )


async def test_search_marks_the_households_usual_item(w):
    with w.db.tx() as conn:
        conn.execute("INSERT INTO defaults VALUES (?, 'milk', ?, ?, '1 L', 2, 't')", (w.hid, *MILK))
    out = await w.gate.search_products(w.parent, "milk")
    flags = {p["sku_id"]: p["household_default"] for p in out["products"]}
    assert flags[MILK[0]] is True
    assert sum(flags.values()) == 1


async def test_search_refuses_empty_and_giant_queries_and_reports_store_errors(w):
    for query in ("", "   ", "x" * 101):
        with pytest.raises(GateError):
            await w.gate.search_products(w.parent, query)
    assert w.calls == []

    async def boom(*_):
        raise StoreUnavailable("down")

    w.store.search_products = boom  # type: ignore[method-assign]
    with pytest.raises(GateError) as err:
        await w.gate.search_products(w.parent, "milk")
    assert err.value.code == "store_error"


# ----- item IDs ------------------------------------------------------------------------------


async def test_an_unknown_item_id_is_refused_before_anything_is_reserved_or_written(w):
    await w.search("atta")
    for bad in [
        item(("sku_made_up", "spin_made_up")),
        item((ATTA[0], "spin_made_up")),
        item(("sku_made_up", ATTA[1])),
        item(MILK),  # real, but never searched
    ]:
        with pytest.raises(GateError) as err:
            await w.prepare(item(ATTA), bad)
        assert err.value.code == "item_not_from_search"
    assert w.db.conn().execute("SELECT COUNT(*) FROM mandates").fetchone()[0] == 0
    assert "update_cart" not in w.calls


async def test_a_search_older_than_fifteen_minutes_is_stale(w):
    await w.search("atta")
    w.now[0] = T0 + timedelta(minutes=14, seconds=59)
    assert (await w.prepare(item(ATTA)))["status"] == "prepared"
    w.now[0] = T0 + timedelta(minutes=15, seconds=1)
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA))
    assert err.value.code == "item_not_from_search"
    await w.search("atta")  # searching again refreshes it
    w.now[0] += timedelta(minutes=1)
    await w.prepare(item(ATTA, 2))


async def test_another_households_search_does_not_count(w):
    with w.db.tx() as conn:
        other = w.household(conn, "parent-2", "kid-2", "acct2")
    stranger = w.gate.authenticate(TOKEN, "parent-2")
    await w.search("atta", caller=stranger)
    assert other == stranger.household_id
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA))
    assert err.value.code == "item_not_from_search"


# ----- prepare_order: the fill ---------------------------------------------------------------


async def test_prepare_fills_an_empty_cart_and_queues_the_readback(w):
    await w.search("atta", "milk")
    out = await w.prepare(item(ATTA), item(MILK))
    assert out["status"] == "prepared"
    row = w.row(out["mandate_id"])
    assert row["state"] == "PREPARED"
    assert (row["total_paise"], row["max_total_paise"]) == (34500, 37500)
    assert row["cart_hash"] and row["quote_json"] and row["is_duplicate"] == 0
    assert row["expires_at"] == gdb.iso(T0 + timedelta(minutes=60))
    assert json.loads(row["request_json"])["items"][0]["heard_as"] == "test"
    assert STORE.cart == {ATTA[1]: 1, MILK[1]: 1}
    [readback] = w.outbox("readback")
    assert readback["to"] == "parent" and readback["mandate_id"] == out["mandate_id"]
    assert readback["total_paise"] == 34500 and readback["address_text"] == HOME_TEXT
    assert {line["name"] for line in readback["lines"]} == {
        "Sunrise Whole Wheat Atta",
        "Nandini Toned Milk",
    }
    assert "checkout" not in w.calls and STORE.orders == []


async def test_repeated_items_are_merged_before_they_reach_the_store(w):
    await w.search("atta")
    out = await w.prepare(item(ATTA), item(ATTA, 2))
    assert STORE.cart == {ATTA[1]: 3}
    assert json.loads(w.row(out["mandate_id"])["request_json"])["items"][0]["qty"] == 3


async def test_the_model_cannot_pass_a_price_or_address(w):
    await w.search("atta")
    # The MCP model refuses extras (test_contracts). The service only reads the four fields.
    out = await w.prepare({**item(ATTA), "price": 1, "address_id": "addr_other", "total": 1})
    assert w.row(out["mandate_id"])["total_paise"] == 29000


async def test_the_total_ceiling_and_address_come_from_the_store_not_the_caller(w):
    await w.search("atta")
    out = await w.prepare(item(ATTA, 2))
    row = w.row(out["mandate_id"])
    assert row["total_paise"] == 57500  # 570 + handling 5
    assert row["max_total_paise"] == 60500
    assert json.loads(row["cart_json"])["address_id"] == "addr_home"


# ----- prepare_order: the kid's cart ---------------------------------------------------------


async def test_the_kids_existing_cart_is_not_touched(w):
    STORE.switch_preload_cart()
    before = dict(STORE.cart)
    await w.search("atta")
    out = await w.prepare(item(ATTA))
    assert out["status"] == "waiting_for_kid"
    assert STORE.cart == before
    assert "update_cart" not in w.calls and "clear_cart" not in w.calls
    assert w.state(out["mandate_id"]) is State.WAITING_FOR_CART
    row = w.row(out["mandate_id"])
    assert row["cart_hash"] is None
    [ask] = w.outbox("replace_cart")
    assert ask["to"] == "kid" and ask["mandate_id"] == out["mandate_id"]
    assert ask["item_count"] == 3 and ask["line_count"] == 2
    [notice] = w.outbox("waiting_for_kid")
    assert notice["to"] == "parent" and notice["mandate_id"] == out["mandate_id"]
    assert w.outbox("readback") == []


async def test_after_replace_fill_cart_prepares_the_waiting_mandate(w):
    STORE.switch_preload_cart()
    await w.search("atta")
    out = await w.prepare(item(ATTA))
    result = await w.gate.fill_cart(out["mandate_id"])
    assert result == {"mandate_id": out["mandate_id"], "status": "prepared"}
    assert w.state(out["mandate_id"]) is State.PREPARED
    assert STORE.cart == {ATTA[1]: 1}
    assert len(w.outbox("readback")) == 1
    with pytest.raises(GateError):  # no longer waiting
        await w.gate.fill_cart(out["mandate_id"])


async def test_fill_cart_failure_aborts_and_tells_both_people(w):
    STORE.switch_preload_cart()
    await w.search("atta")
    out = await w.prepare(item(ATTA, 5))  # the store allows 4
    with pytest.raises(GateError) as err:
        await w.gate.fill_cart(out["mandate_id"])
    assert err.value.code == "not_available"
    assert w.state(out["mandate_id"]) is State.ABORTED
    assert {(m["to"], m["code"]) for m in w.outbox("order_failed")} == {
        ("parent", "not_available"),
        ("kid", "not_available"),
    }
    assert w.open_count() == 0


async def test_a_cart_ordr_filled_is_ours_but_one_the_kid_edited_is_not(w):
    await w.search("atta", "milk")
    await w.prepare(item(ATTA))
    again = await w.prepare(item(MILK, 2))
    assert again["status"] == "prepared"  # our own earlier fill is replaced
    STORE.cart[ATTA[1]] = 1  # the kid adds a line
    third = await w.prepare(item(ATTA))
    assert third["status"] == "waiting_for_kid"
    assert STORE.cart == {MILK[1]: 2, ATTA[1]: 1}


async def test_another_assistant_adding_an_item_means_the_cart_is_not_ours(w):
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    STORE.switch_add_expensive_item()
    second = await w.prepare(item(ATTA))
    assert second["status"] == "waiting_for_kid"
    assert w.state(first["mandate_id"]) is State.ABORTED
    assert dict(STORE.cart)[ATTA[1]] == 1 and len(STORE.cart) == 2


# ----- prepare_order: one open order ---------------------------------------------------------


async def test_a_second_prepare_replaces_the_draft_in_one_step(w):
    await w.search("atta", "milk")
    first = await w.prepare(item(ATTA))
    second = await w.prepare(item(MILK, 3), item(ATTA))
    assert first["mandate_id"] != second["mandate_id"]
    assert w.state(first["mandate_id"]) is State.ABORTED
    assert w.state(second["mandate_id"]) is State.PREPARED
    assert w.open_count() == 1
    assert STORE.cart == {MILK[1]: 3, ATTA[1]: 1}


async def test_a_second_prepare_replaces_a_waiting_mandate_too(w):
    STORE.switch_preload_cart()
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    second = await w.prepare(item(ATTA, 2))
    assert w.state(first["mandate_id"]) is State.ABORTED
    assert w.state(second["mandate_id"]) is State.WAITING_FOR_CART
    assert w.open_count() == 1


async def test_a_failed_replace_keeps_the_old_draft(w):
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    with pytest.raises(GateError):
        await w.prepare(item(ATTA), item(MILK))  # milk was never searched
    assert w.state(first["mandate_id"]) is State.PREPARED


@pytest.mark.parametrize(
    "state",
    [
        State.PARENT_CONFIRMED,
        State.HANDOFF_WARNED,
        State.APPROVED,
        State.COMMITTING,
        State.PLACED_PENDING_PAYMENT,
    ],
)
async def test_prepare_is_refused_once_the_parent_has_confirmed(w, state):
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    with w.db.tx() as conn:
        conn.execute(
            "UPDATE mandates SET state = ? WHERE id = ?", (state.value, first["mandate_id"])
        )
    calls_before = list(w.calls)
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA, 2))
    assert err.value.code == "order_already_open"
    assert w.state(first["mandate_id"]) is state  # not aborted
    assert w.calls == calls_before  # no store call at all
    assert w.open_count() == 1


async def test_an_unreviewed_needs_review_blocks_until_the_kid_has_checked(w):
    await w.search("atta")
    bad = w.fake(State.NEEDS_REVIEW, reviewed_at=None)
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA))
    assert err.value.code == "needs_review"
    with w.db.tx() as conn:
        assert gdb.mark_reviewed(conn, bad)
    assert (await w.prepare(item(ATTA)))["status"] == "prepared"


async def test_an_expired_draft_does_not_block_a_new_prepare(w):
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    w.now[0] = T0 + timedelta(minutes=61)
    await w.search("atta")
    second = await w.prepare(item(ATTA))
    assert w.state(first["mandate_id"]) is State.EXPIRED
    assert w.state(second["mandate_id"]) is State.PREPARED


async def test_two_prepares_at_once_leave_exactly_one_open_order(w):
    await w.search("atta", "milk")
    results = await asyncio.gather(
        w.prepare(item(ATTA)), w.prepare(item(MILK, 3), item(ATTA)), return_exceptions=True
    )
    assert all(isinstance(r, dict) for r in results)
    assert w.open_count() == 1
    [live] = [r for r in results if w.state(r["mandate_id"]) is State.PREPARED]
    # The cart in the store is the surviving mandate's cart.
    request = json.loads(w.row(live["mandate_id"])["request_json"])["items"]
    assert STORE.cart == {i["spin_id"]: i["qty"] for i in request}


# ----- caps ----------------------------------------------------------------------------------


async def test_over_the_per_order_cap_is_refused_and_aborted(w):
    await w.search("atta", "oil")
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA, 4), item(OIL5))  # Rs 1,885
    assert err.value.code == "over_cap"
    assert w.open_count() == 0
    assert w.outbox("readback") == []


async def test_the_cap_is_checked_on_the_ceiling_not_the_total(w):
    # Rs 1,485 is under the Rs 1,500 cap, but the Rs 30 ceiling takes it to Rs 1,515.
    await w.search("atta", "tea", "sugar")
    tea = ("sku_hillside_tea_250g", "spin_hillside_tea_250g")
    sugar = ("sku_sweetway_sugar_1kg", "spin_sweetway_sugar_1kg")
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA, 4), item(tea, 2), item(sugar, 2))
    assert err.value.code == "over_cap"
    assert STORE.get_cart()["cartTotalAmount"] == "1485.00"  # the store did fill it
    assert w.open_count() == 0


@pytest.mark.parametrize("state", [State.PAID, State.NEEDS_REVIEW])
async def test_the_monthly_cap_counts_these_orders(w, state):
    w.gate.policy = w.gate.policy.model_copy(update={"monthly_cap_paise": 100000})
    await w.search("atta", "milk")
    w.fake(state, max_total_paise=70000, total_paise=67000)
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA), item(MILK))  # ceiling Rs 375, 700 + 375 > 1000
    assert err.value.code == "over_cap"
    assert w.open_count() == 0


@pytest.mark.parametrize(
    "state",
    [State.UNPAID, State.REJECTED, State.ABORTED, State.EXPIRED, State.HANDED_OFF, State.BLOCKED],
)
async def test_the_monthly_cap_ignores_orders_that_did_not_happen(w, state):
    w.gate.policy = w.gate.policy.model_copy(update={"monthly_cap_paise": 100000})
    await w.search("atta", "milk")
    w.fake(state, max_total_paise=90000)
    assert (await w.prepare(item(ATTA), item(MILK)))["status"] == "prepared"


async def test_the_month_is_the_ist_calendar_month(w):
    w.gate.policy = w.gate.policy.model_copy(update={"monthly_cap_paise": 100000})
    await w.search("atta", "milk")
    # 23:30 on 31 Oct IST is last month. 00:30 on 1 Nov IST is this month.
    w.fake(State.PAID, max_total_paise=90000, created=datetime(2026, 10, 31, 18, 0, tzinfo=UTC))
    assert (await w.prepare(item(ATTA), item(MILK)))["status"] == "prepared"
    w.fake(State.PAID, max_total_paise=90000, created=datetime(2026, 10, 31, 19, 0, tzinfo=UTC))
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA), item(MILK))
    assert err.value.code == "over_cap"


async def test_the_next_month_starts_fresh(w):
    w.gate.policy = w.gate.policy.model_copy(update={"monthly_cap_paise": 100000})
    w.fake(State.PAID, max_total_paise=90000)
    w.now[0] = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)  # 05:30 IST on 1 Dec
    await w.search("atta", "milk")
    assert (await w.prepare(item(ATTA), item(MILK)))["status"] == "prepared"


# ----- duplicate warning ---------------------------------------------------------------------


async def prepared_hash(w) -> str:
    await w.search("atta", "milk")
    out = await w.prepare(item(ATTA), item(MILK))
    return w.row(out["mandate_id"])["cart_hash"]


@pytest.mark.parametrize("state", [State.PAID, State.NEEDS_REVIEW, State.HANDED_OFF])
async def test_same_cart_within_the_window_is_flagged_as_a_duplicate(w, state):
    digest = await prepared_hash(w)
    w.fake(state, cart_hash=digest, created=T0 - timedelta(minutes=119))
    out = await w.prepare(item(ATTA), item(MILK))
    assert out["status"] == "prepared"  # a warning, not a refusal
    assert w.row(out["mandate_id"])["is_duplicate"] == 1
    assert w.outbox("readback")[-1]["is_duplicate"] is True


@pytest.mark.parametrize("state", [State.UNPAID, State.REJECTED, State.ABORTED, State.EXPIRED])
async def test_orders_that_did_not_happen_are_not_duplicates(w, state):
    digest = await prepared_hash(w)
    w.fake(state, cart_hash=digest, created=T0 - timedelta(minutes=5))
    out = await w.prepare(item(ATTA), item(MILK))
    assert w.row(out["mandate_id"])["is_duplicate"] == 0


async def test_outside_the_window_or_a_different_cart_is_not_a_duplicate(w):
    digest = await prepared_hash(w)
    w.fake(State.PAID, cart_hash=digest, created=T0 - timedelta(minutes=121))
    out = await w.prepare(item(ATTA), item(MILK))
    assert w.row(out["mandate_id"])["is_duplicate"] == 0
    other = await w.prepare(item(ATTA), item(MILK, 2))
    assert w.row(other["mandate_id"])["is_duplicate"] == 0


# ----- the read-back must equal the request --------------------------------------------------


async def test_a_reduced_quantity_aborts_with_not_available(w):
    await w.search("atta")
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA, 5))  # the store caps at 4 and says so
    assert err.value.code == "not_available"
    assert w.open_count() == 0
    [row] = w.db.conn().execute("SELECT state, reason FROM mandates").fetchall()
    assert (row["state"], row["reason"]) == ("ABORTED", "quantity_reduced")
    assert w.outbox("readback") == []


async def test_a_dropped_out_of_stock_item_aborts(w):
    await w.search("atta", "milk")
    STORE.switch_out_of_stock(MILK[0])
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA), item(MILK))
    assert err.value.code == "not_available"
    assert w.db.conn().execute("SELECT reason FROM mandates").fetchone()[0] == "item_dropped"
    assert w.open_count() == 0


async def test_a_cart_under_the_store_minimum_is_not_available(w):
    await w.search("milk")
    with pytest.raises(GateError) as err:
        await w.prepare(item(MILK))  # Rs 55 plus fees, store minimum is Rs 99
    assert err.value.code == "not_available"
    assert w.open_count() == 0


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (lambda c: dataclasses.replace(c, address_id="addr_other"), "wrong_address"),
        (lambda c: dataclasses.replace(c, address_text="elsewhere"), "wrong_address_text"),
        (
            lambda c: dataclasses.replace(
                c, lines=(*c.lines, CartLine("sku_x", "spin_x", 1, "Extra", "1", 100))
            ),
            "extra_item",
        ),
        (lambda c: dataclasses.replace(c, lines=c.lines[:-1] or c.lines), "item_dropped"),
        (
            lambda c: dataclasses.replace(
                c, lines=(dataclasses.replace(c.lines[0], qty=c.lines[0].qty + 1), *c.lines[1:])
            ),
            "quantity_changed",
        ),
        (lambda c: dataclasses.replace(c, all_in_stock=False), "out_of_stock"),
        (lambda c: dataclasses.replace(c, serviceable=False), "not_serviceable"),
    ],
)
async def test_any_difference_in_the_readback_aborts(w, damage, reason):
    await w.search("atta", "milk")
    real = w.store.get_cart
    reads = []

    async def tampered():
        cart = await real()
        reads.append(cart)
        return damage(cart) if len(reads) == 2 else cart  # the read after update_cart

    w.store.get_cart = tampered  # type: ignore[method-assign]
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA), item(MILK))
    assert err.value.code == "not_available"
    assert w.db.conn().execute("SELECT reason FROM mandates").fetchone()[0] == reason
    assert w.open_count() == 0


# ----- store failures never leave an open mandate --------------------------------------------


@pytest.mark.parametrize("failing", ["get_cart_first", "update_cart", "get_cart_after"])
async def test_a_store_failure_leaves_no_open_mandate(w, failing):
    await w.search("atta")
    real_get, real_update = w.store.get_cart, w.store.update_cart
    reads = []

    async def get_cart():
        reads.append(1)
        if (failing == "get_cart_first" and len(reads) == 1) or (
            failing == "get_cart_after" and len(reads) == 2
        ):
            raise StoreUnavailable("down")
        return await real_get()

    async def update_cart(*args):
        if failing == "update_cart":
            raise StoreUnavailable("down")
        return await real_update(*args)

    w.store.get_cart, w.store.update_cart = get_cart, update_cart  # type: ignore[method-assign]
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA))
    assert err.value.code == "store_error"
    assert w.open_count() == 0
    assert w.db.conn().execute("SELECT state FROM mandates").fetchone()[0] == "ABORTED"
    w.store.get_cart, w.store.update_cart = real_get, real_update  # type: ignore[method-assign]
    # The account is free again. A cart that Ordr filled but never got to hash is not known to
    # be ours, so the next prepare asks the kid instead of replacing it.
    expected = "waiting_for_kid" if failing == "get_cart_after" else "prepared"
    assert (await w.prepare(item(ATTA)))["status"] == expected


async def test_an_odd_store_response_is_a_store_error(w):
    await w.search("atta")
    real = w.store._call

    async def odd(tool, args):
        data = await real(tool, args)
        if tool == "get_cart":
            data["cartTotalAmount"] = "12.345"
        return data

    w.store._call = odd  # type: ignore[method-assign]
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA))
    assert err.value.code == "store_error"
    assert w.open_count() == 0


async def test_a_bug_or_cancellation_mid_prepare_still_frees_the_account(w):
    await w.search("atta")

    async def crash(*_):
        raise RuntimeError("bug")

    real = w.store.update_cart
    w.store.update_cart = crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await w.prepare(item(ATTA))
    assert w.open_count() == 0

    async def hang(*_):
        await asyncio.sleep(60)

    w.store.update_cart = hang  # type: ignore[method-assign]
    task = asyncio.create_task(w.prepare(item(ATTA)))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert w.open_count() == 0
    w.store.update_cart = real  # type: ignore[method-assign]
    assert (await w.prepare(item(ATTA)))["status"] == "prepared"


# ----- no checkout, ever ---------------------------------------------------------------------


async def test_no_tool_in_this_file_calls_checkout(w):
    await w.search("atta", "milk")
    first = await w.prepare(item(ATTA))
    await w.prepare(item(MILK, 3), item(ATTA))
    await w.gate.abort_order(w.parent, first["mandate_id"])
    await w.gate.get_order_status(w.parent, first["mandate_id"])
    await w.gate.get_audit_log(w.parent)
    STORE.switch_preload_cart()
    await w.prepare(item(ATTA))
    assert set(w.calls) <= {"search_products", "get_cart", "update_cart"}
    assert STORE.orders == []


# ----- get_order_status, abort_order, get_audit_log ------------------------------------------


async def test_status_and_abort_only_for_the_callers_own_household(w):
    await w.search("atta")
    mine = await w.prepare(item(ATTA))
    with w.db.tx() as conn:
        w.household(conn, "parent-2", "kid-2", "acct2")
    other = w.gate.authenticate(TOKEN, "parent-2")
    for call in (w.gate.get_order_status, w.gate.abort_order):
        with pytest.raises(GateError) as err:
            await call(other, mine["mandate_id"])
        assert err.value.code == "not_found"
        with pytest.raises(GateError) as err:
            await call(w.parent, "no-such-id")
        assert err.value.code == "not_found"
    assert w.state(mine["mandate_id"]) is State.PREPARED
    assert await w.gate.get_order_status(w.parent, mine["mandate_id"]) == {
        "mandate_id": mine["mandate_id"],
        "state": "PREPARED",
    }


@pytest.mark.parametrize(
    "state", [State.WAITING_FOR_CART, State.PREPARED, State.PARENT_CONFIRMED, State.HANDOFF_WARNED]
)
async def test_abort_works_until_the_kid_approves(w, state):
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    if state is not State.PREPARED:
        with w.db.tx() as conn:
            conn.execute(
                "UPDATE mandates SET state = ? WHERE id = ?", (state.value, first["mandate_id"])
            )
    out = await w.gate.abort_order(w.parent, first["mandate_id"])
    assert out == {"mandate_id": first["mandate_id"], "state": "ABORTED"}
    assert w.open_count() == 0
    kid_told = [m for m in w.outbox("stopped") if m["to"] == "kid"]
    assert bool(kid_told) is (state in (State.PARENT_CONFIRMED, State.HANDOFF_WARNED))
    assert [m for m in w.outbox("stopped") if m["to"] == "parent"]


@pytest.mark.parametrize("state", [State.APPROVED, State.COMMITTING, State.PLACED_PENDING_PAYMENT])
async def test_abort_is_refused_after_approval(w, state):
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    with w.db.tx() as conn:
        conn.execute(
            "UPDATE mandates SET state = ? WHERE id = ?", (state.value, first["mandate_id"])
        )
    with pytest.raises(GateError) as err:
        await w.gate.abort_order(w.parent, first["mandate_id"])
    assert err.value.code == "order_already_open"
    assert w.state(first["mandate_id"]) is state


async def test_aborting_a_finished_order_changes_nothing(w):
    done = w.fake(State.PAID)
    assert await w.gate.abort_order(w.parent, done) == {"mandate_id": done, "state": "PAID"}
    assert w.state(done) is State.PAID
    again = w.fake(State.ABORTED)
    assert (await w.gate.abort_order(w.parent, again))["state"] == "ABORTED"


async def test_stop_and_approve_cannot_both_win(w):
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    with w.db.tx() as conn:
        assert transition(
            conn, first["mandate_id"], State.PREPARED, State.PARENT_CONFIRMED, event="confirm"
        )
        assert transition(
            conn,
            first["mandate_id"],
            State.PARENT_CONFIRMED,
            State.APPROVED,
            event="approve",
            set_fields={"approval_sig": "sig"},
        )
    with pytest.raises(GateError):
        await w.gate.abort_order(w.parent, first["mandate_id"])
    assert w.state(first["mandate_id"]) is State.APPROVED


async def test_audit_log_is_this_households_only_newest_first_and_limited(w):
    await w.search("atta")
    first = await w.prepare(item(ATTA))
    with w.db.tx() as conn:
        other = w.household(conn, "parent-2", "kid-2", "acct2")
        gdb.append_audit(conn, other, None, "theirs")
    log = await w.gate.get_audit_log(w.parent, 100)
    events = [e["event"] for e in log["entries"]]
    assert events == ["prepared", "mandate_created"]
    assert all(e["mandate_id"] == first["mandate_id"] for e in log["entries"])
    assert set(log["entries"][0]) == {"ts", "event", "mandate_id"}
    assert len((await w.gate.get_audit_log(w.parent, 1))["entries"]) == 1
    assert gdb.verify_chain(w.db.conn())
