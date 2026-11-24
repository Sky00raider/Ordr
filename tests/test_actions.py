"""Button presses against the dummy store, in process, counting checkout calls (TRD N6)."""

import asyncio
import dataclasses
import json
from datetime import timedelta

import httpx
import pytest
from test_service import (
    ATTA,
    HOME_TEXT,
    KID,
    MILK,
    PARENT,
    STRANGER,
    T0,
    TOKEN,
    World,
    item,
)

from ordr.config import Settings
from ordr.contracts import State
from ordr.gate import actions as actions_module
from ordr.gate import db as gdb
from ordr.gate import server as gate_server
from ordr.gate.actions import Actions
from ordr.gate.binding import WeakServerKey
from ordr.gate.service import GateError
from ordr.gate.store_client import StoreUnavailable
from ordr.gate.upi import SimulatedUpi, UpiUnclear
from ordr.store.server import STORE
from ordr.store.server import mcp as store_mcp
from ordr.store.state import ADDRESSES

KEY = "k" * 40


class ActWorld(World):
    """World plus Actions, a store clock that follows the gate clock, and two checkout counters.

    `attempts` counts what the gate sent. `placed` counts what reached the store's own checkout.
    """

    def __init__(self, tmp_path, monkeypatch) -> None:
        super().__init__(tmp_path)
        monkeypatch.setattr(STORE, "clock", lambda: self.now[0].timestamp())
        self.placed: list[tuple] = []
        real = STORE.checkout

        def counted(*args):
            self.placed.append(args)
            return real(*args)

        monkeypatch.setattr(STORE, "checkout", counted)
        self.pay_calls = 0
        upi = SimulatedUpi(
            "http://store.test/demo/pay", transport=httpx.ASGITransport(app=store_mcp.http_app())
        )
        real_pay = upi.pay

        async def counted_pay(paas_id):
            self.pay_calls += 1
            await real_pay(paas_id)

        upi.pay = counted_pay  # type: ignore[method-assign]
        self.upi = upi
        self.actions = Actions(self.gate, upi, KEY)
        self._n = 0
        self.act_codes: list[str] = []

    @property
    def attempts(self) -> int:
        return self.calls.count("checkout")

    async def press(self, action: str, mandate_id: str, number: str = KID, mid: str | None = None):
        self._n += 1
        return await self.actions.act(number, mid or f"m{self._n}", action, mandate_id)

    async def code(self, action: str, mandate_id: str, number: str = KID) -> str:
        out = await self.press(action, mandate_id, number)
        assert set(out) == {"ok", "code"}
        return out["code"]

    async def prepared(self) -> str:
        await self.search("atta", "milk")
        return (await self.prepare(item(ATTA), item(MILK)))["mandate_id"]

    async def confirmed(self) -> str:
        mandate_id = await self.prepared()
        assert await self.code("confirm", mandate_id, PARENT) == "confirmed"
        return mandate_id

    async def waiting_for_payment(self) -> str:
        mandate_id = await self.confirmed()
        assert await self.code("approve", mandate_id) == "placed"
        return mandate_id

    def reason(self, mandate_id: str) -> str:
        return self.row(mandate_id)["reason"]

    def told(self, kind: str) -> set[str]:
        return {m["to"] for m in self.outbox(kind)}

    def events(self) -> list[str]:
        return [r[0] for r in self.db.conn().execute("SELECT event FROM audit_log ORDER BY seq")]

    def blocked(self, mandate_id: str, reason: str) -> None:
        """BLOCKED for this reason, both told, zero checkout calls."""
        assert self.state(mandate_id) is State.BLOCKED
        assert self.reason(mandate_id) == reason
        assert self.told("blocked") == {"kid", "parent"}
        assert (self.attempts, self.placed, STORE.orders) == (0, [], [])
        assert self.outbox("payment") == []

    def in_review(self, mandate_id: str, reason: str, order_id: str | None) -> None:
        """NEEDS_REVIEW, both told once with the order ID if known, account blocked."""
        assert self.state(mandate_id) is State.NEEDS_REVIEW
        assert self.reason(mandate_id) == reason
        assert self.row(mandate_id)["order_id"] == order_id
        told = self.outbox("needs_review")
        assert sorted(m["to"] for m in told) == ["kid", "parent"]
        assert all(m["order_id"] == order_id for m in told)

    def on_call(self, tool: str, before=None, after=None, replace=None) -> None:
        """Run something around one store tool call."""
        real = self.store._call

        async def hooked(name, args):
            if name != tool:
                return await real(name, args)
            if before:
                await before()
            if replace:
                return await replace(real, args)
            data = await real(name, args)
            return after(data) if after else data

        self.store._call = hooked  # type: ignore[method-assign]


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = ActWorld(tmp_path, monkeypatch)
    yield world
    committed = world.events().count("committing")
    assert world.attempts <= committed, "checkout without a move to COMMITTING, or a retry"
    assert len(world.placed) <= committed
    assert all(args[1] == "UPI" and args[4] is None for args in world.placed)
    assert gdb.verify_chain(world.db.conn())
    everything = json.dumps(
        [list(r) for r in world.db.conn().execute("SELECT * FROM audit_log")], default=str
    )
    assert KID not in everything and PARENT not in everything
    world.db.close()


# ----- happy path ----------------------------------------------------------------------------


async def test_happy_path_to_paid(w):
    mandate_id = await w.prepared()
    assert await w.code("confirm", mandate_id, PARENT) == "confirmed"
    [card] = w.outbox("kid_card")
    assert card["to"] == "kid" and card["mandate_id"] == mandate_id
    assert (card["total_paise"], card["max_total_paise"]) == (34500, 37500)
    assert card["address_text"] == HOME_TEXT and card["is_duplicate"] is False
    assert card["buttons"] == ["approve", "reject"]  # no Open in store (N8)
    [sent] = w.outbox("sent_to_kid")
    assert sent["to"] == "parent" and sent["buttons"] == ["stop"]

    assert await w.code("approve", mandate_id) == "placed"
    assert w.attempts == len(w.placed) == 1
    row = w.row(mandate_id)
    assert row["state"] == "PLACED_PENDING_PAYMENT"
    assert row["order_id"] == "IM-100001" and row["paas_id"] == "paas100001"
    [payment] = w.outbox("payment")
    assert payment["to"] == "kid" and payment["total_paise"] == 34500
    assert payment["buttons"] == ["pay", "dont_pay"]
    assert STORE.orders[0]["status"] == "PENDING_PAYMENT"

    assert await w.code("pay", mandate_id) == "paid"
    assert w.state(mandate_id) is State.PAID
    assert STORE.orders[0]["status"] == "CONFIRMED"
    assert w.told("receipt") == {"kid", "parent"}
    assert w.attempts == 1 and w.pay_calls == 1
    assert w.events() == [
        "mandate_created",
        "prepared",
        "parent_confirmed",
        "approved",
        "committing",
        "checkout_returned",
        "placed",
        "pay_tapped",
        "paid",
    ]
    assert (await w.prepare(item(ATTA)))["status"] == "prepared"  # account free


async def test_the_quote_is_logged_with_the_approval(w):
    await w.waiting_for_payment()
    row = w.db.conn().execute("SELECT data_json FROM audit_log WHERE event = 'approved'").fetchone()
    assert json.loads(row[0])["quote"]["total_paise"] == 34500


async def test_duplicate_warning_reaches_the_kids_card(w):
    first = await w.waiting_for_payment()
    assert await w.code("pay", first) == "paid"
    w.now[0] += timedelta(minutes=12)  # past the store's own duplicate window, inside ours
    second = await w.confirmed()
    assert w.outbox("kid_card")[-1]["is_duplicate"] is True
    assert await w.code("approve", second) == "placed"
    assert w.attempts == 2


# ----- final checks: BLOCKED with zero checkout calls ----------------------------------------


async def test_item_added_after_confirm_is_blocked(w):
    mandate_id = await w.confirmed()
    STORE.switch_add_expensive_item()
    assert await w.press("approve", mandate_id) == {"ok": False, "code": "blocked"}
    w.blocked(mandate_id, "cart_changed")


async def test_address_text_edited_is_blocked(w, monkeypatch):
    mandate_id = await w.confirmed()
    monkeypatch.setitem(ADDRESSES[0], "addressLine", "12, 3rd Cross, Jayanagar, Bengaluru 560011")
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "cart_changed")


async def test_address_changed_is_blocked(w):
    mandate_id = await w.confirmed()
    STORE.switch_change_address()
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "cart_changed")


async def test_an_emptied_cart_is_blocked(w):
    mandate_id = await w.confirmed()
    STORE.clear_cart()
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "cart_changed")


async def test_rs_18_fee_passes_at_rs_363(w):
    mandate_id = await w.confirmed()
    STORE.switch_extra_fee(18)
    assert await w.code("approve", mandate_id) == "placed"
    assert w.outbox("payment")[0]["total_paise"] == 36300
    assert await w.code("pay", mandate_id) == "paid"
    assert w.outbox("receipt")[0]["total_paise"] == 36300


async def test_the_ceiling_itself_passes_and_one_rupee_more_does_not(w):
    mandate_id = await w.confirmed()
    STORE.switch_extra_fee(31)
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "over_ceiling")
    STORE.switch_extra_fee(0)
    second = await w.confirmed()
    STORE.switch_extra_fee(30)
    assert await w.code("approve", second) == "placed"


async def test_rs_250_jump_is_blocked(w):
    mandate_id = await w.confirmed()
    STORE.switch_extra_fee(250)
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "over_ceiling")
    assert w.outbox("blocked")[0]["total_paise"] == 59500


async def test_out_of_stock_is_blocked(w):
    mandate_id = await w.confirmed()
    STORE.switch_out_of_stock(MILK[0])
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "out_of_stock")


async def test_not_serviceable_is_blocked(w):
    mandate_id = await w.confirmed()
    w.on_call("get_cart", after=lambda d: {**d, "cartWarning": {"message": "no slots"}})
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "not_serviceable")


@pytest.mark.parametrize("cap", ["per_order", "per_month"])
async def test_caps_are_checked_again_at_approval(w, cap):
    mandate_id = await w.confirmed()
    key = "per_order_cap_paise" if cap == "per_order" else "monthly_cap_paise"
    w.gate.policy = w.gate.policy.model_copy(update={key: 37499})
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "over_cap")


async def test_other_orders_this_month_count_at_approval(w):
    mandate_id = await w.confirmed()
    w.fake(State.PAID, max_total_paise=600000 - 37499)
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "over_cap")


async def test_upi_not_allowed_by_policy_is_blocked(w):
    mandate_id = await w.confirmed()
    w.gate.policy = w.gate.policy.model_copy(update={"allowed_payment": []})
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "payment_not_allowed")


async def test_expired_card(w):
    mandate_id = await w.confirmed()
    w.now[0] = T0 + timedelta(minutes=60)
    before = list(w.calls)
    assert await w.press("approve", mandate_id) == {"ok": False, "code": "expired"}
    assert w.state(mandate_id) is State.EXPIRED
    assert w.told("expired") == {"kid", "parent"}
    assert w.row(mandate_id)["approval_sig"] is None
    assert w.attempts == 0 and w.calls == before


async def test_expired_confirm_and_replace(w):
    mandate_id = await w.prepared()
    w.now[0] = T0 + timedelta(minutes=61)
    assert await w.code("confirm", mandate_id, PARENT) == "expired"
    STORE.switch_preload_cart()
    before = dict(STORE.cart)
    await w.search("atta")
    waiting = (await w.prepare(item(ATTA)))["mandate_id"]
    w.now[0] += timedelta(minutes=61)
    assert await w.code("replace_cart", waiting) == "expired"
    assert STORE.cart == before


@pytest.mark.parametrize("column", ["approval_sig", "max_total_paise"])
async def test_a_tampered_approval_is_blocked(w, column):
    mandate_id = await w.confirmed()
    value = "0" * 64 if column == "approval_sig" else 999900

    async def tamper():  # after signing, before COMMITTING
        conn = w.db.conn()
        conn.execute("DROP TRIGGER IF EXISTS seal_cart")
        conn.execute("DROP TRIGGER IF EXISTS seal_approval")
        conn.execute(f"UPDATE mandates SET {column} = ?", (value,))  # noqa: S608

    w.on_call("get_cart", before=tamper)
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "bad_signature")


async def test_a_wrong_signature_is_blocked_before_any_store_read(w, monkeypatch):
    mandate_id = await w.confirmed()
    monkeypatch.setattr(actions_module, "sign_approval", lambda *_: "0" * 64)
    reads = len(w.calls)
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "bad_signature")
    assert len(w.calls) == reads


async def test_another_households_kid_number_does_not_verify(w):
    mandate_id = await w.confirmed()

    async def swap():
        w.db.conn().execute("UPDATE households SET kid_number = 'kid-9'")

    w.on_call("get_cart", before=swap)
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "bad_signature")


@pytest.mark.parametrize("tool", ["get_cart", "get_orders"])
async def test_a_store_error_before_committing_is_blocked(w, tool):
    mandate_id = await w.confirmed()

    async def down(*_):
        raise StoreUnavailable("down")

    w.on_call(tool, replace=down)
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "store_error")


async def test_a_bug_before_committing_is_blocked(w):
    mandate_id = await w.confirmed()

    async def bug():
        raise RuntimeError("bug")

    w.on_call("get_orders", before=bug)
    assert await w.code("approve", mandate_id) == "blocked"
    w.blocked(mandate_id, "internal")


async def test_cancelled_during_the_checks_is_blocked(w):
    mandate_id = await w.confirmed()
    reached = asyncio.Event()

    async def hang():
        reached.set()
        await asyncio.sleep(60)

    w.on_call("get_cart", before=hang)
    task = asyncio.create_task(w.press("approve", mandate_id))
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    w.blocked(mandate_id, "internal")


# ----- checkout and read-back: NEEDS_REVIEW, one call, no payment ----------------------------


async def test_checkout_timeout_where_the_order_was_placed(w):
    mandate_id = await w.confirmed()

    async def lost_reply(real, args):
        await real("checkout", args)
        raise StoreUnavailable("checkout failed: TimeoutError")

    w.on_call("checkout", replace=lost_reply)
    assert await w.press("approve", mandate_id) == {"ok": False, "code": "needs_review"}
    w.in_review(mandate_id, "checkout_unclear", "IM-100001")  # found, but no paasId
    assert w.attempts == len(w.placed) == len(STORE.orders) == 1
    assert w.calls[-1] == "get_orders"
    assert w.outbox("payment") == []
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA))
    assert err.value.code == "needs_review"


async def test_checkout_timeout_where_it_was_not_placed(w):
    mandate_id = await w.confirmed()

    async def never_sent(*_):
        w.calls.append("checkout")
        raise StoreUnavailable("checkout failed: TimeoutError")

    w.on_call("checkout", replace=never_sent)
    assert await w.code("approve", mandate_id) == "needs_review"
    w.in_review(mandate_id, "checkout_unclear", None)
    assert w.attempts == 1 and w.placed == [] and STORE.orders == []
    assert w.calls[-1] == "get_orders"
    assert w.outbox("payment") == []
    assert await w.code("approve", mandate_id) == "not_current"
    assert w.attempts == 1


async def test_checkout_timeout_and_get_orders_down(w):
    mandate_id = await w.confirmed()
    reads = []

    async def lost_reply(real, args):
        await real("checkout", args)
        raise StoreUnavailable("timeout")

    async def second_read_fails(real, args):
        reads.append(1)
        if len(reads) == 2:
            raise StoreUnavailable("down")
        return await real("get_orders", args)

    w.on_call("checkout", replace=lost_reply)
    w.on_call("get_orders", replace=second_read_fails)
    assert await w.code("approve", mandate_id) == "needs_review"
    w.in_review(mandate_id, "checkout_unclear", None)
    assert w.attempts == 1


async def test_store_duplicate_order_is_needs_review_and_never_overridden(w):
    first = await w.waiting_for_payment()
    assert await w.code("pay", first) == "paid"
    second = await w.confirmed()
    assert await w.code("approve", second) == "needs_review"
    assert w.reason(second) == "duplicate_order"
    assert w.attempts == 2 and len(STORE.orders) == 1


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d.pop("paasId"),
        lambda d: d.update(paasId=""),
        lambda d: d.pop("orderId"),
        lambda d: d.update(status="CONFIRMED"),
        lambda d: d.update(paymentMethod="Cash"),
        lambda d: d.update(cartTotal="345.001"),
    ],
)
async def test_an_odd_checkout_reply_is_needs_review(w, damage):
    mandate_id = await w.confirmed()

    def odd(data):
        damage(data)
        return data

    w.on_call("checkout", after=odd)
    assert await w.code("approve", mandate_id) == "needs_review"
    w.in_review(mandate_id, "checkout_unclear", "IM-100001")
    assert w.attempts == 1 and w.outbox("payment") == []


def _edit_order(**changes):
    def edit(data):
        if data["orders"]:
            data["orders"][0].update(changes)
        return data

    return edit


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"items": [{"itemId": ATTA[0], "quantity": 1}], "itemCount": 1}, "items_differ"),
        (
            {"items": [{"itemId": s, "quantity": 2} for s in (ATTA[0], MILK[0])], "itemCount": 4},
            "items_differ",
        ),
        ({"deliveryAddress": {"id": "addr_other", "addressLine": HOME_TEXT}}, "address_differs"),
        ({"deliveryAddress": {"id": "addr_home", "addressLine": "x"}}, "address_differs"),
        ({"totalAmount": 350.0}, "total_differs"),
        ({"status": "CONFIRMED"}, "order_status_differs"),
        ({"paymentStatus": "SUCCESS"}, "order_status_differs"),
        ({"paymentMethod": "Cash"}, "payment_method_differs"),
        ({"orderId": "IM-999"}, "order_id_differs"),
        ({"itemCount": "2"}, "readback_failed"),
        ({"items": None}, "readback_failed"),
    ],
)
async def test_order_differs_on_read_back(w, change, reason):
    mandate_id = await w.confirmed()
    w.on_call("get_orders", after=_edit_order(**change))
    assert await w.code("approve", mandate_id) == "needs_review"
    w.in_review(mandate_id, reason, "IM-100001")
    assert w.attempts == 1 and w.outbox("payment") == []


async def test_cart_changed_between_check_and_checkout_is_caught_on_read_back(w):
    mandate_id = await w.confirmed()

    async def add():
        STORE.switch_add_expensive_item()

    w.on_call("checkout", before=add)
    assert await w.code("approve", mandate_id) == "needs_review"
    w.in_review(mandate_id, "items_differ", "IM-100001")
    assert w.outbox("payment") == []


async def test_fee_jump_between_check_and_checkout_is_caught_on_read_back(w):
    mandate_id = await w.confirmed()

    async def surge():
        STORE.switch_extra_fee(250)

    w.on_call("checkout", before=surge)
    assert await w.code("approve", mandate_id) == "needs_review"
    w.in_review(mandate_id, "over_ceiling", "IM-100001")


async def test_two_new_orders_is_needs_review(w):
    mandate_id = await w.confirmed()

    def twice(data):
        if data["orders"]:
            data["orders"].append({**data["orders"][0], "orderId": "IM-7"})
        return data

    w.on_call("get_orders", after=twice)
    assert await w.code("approve", mandate_id) == "needs_review"
    w.in_review(mandate_id, "order_count", "IM-100001")


async def test_a_bug_after_checkout_is_needs_review_with_the_order_id(w, monkeypatch):
    mandate_id = await w.confirmed()

    def bug(*_):
        raise RuntimeError("bug")

    monkeypatch.setattr(actions_module, "_readback_problem", bug)
    assert await w.code("approve", mandate_id) == "needs_review"
    w.in_review(mandate_id, "internal", "IM-100001")
    assert w.attempts == 1


async def test_cancelled_during_checkout_is_needs_review(w):
    mandate_id = await w.confirmed()
    reached = asyncio.Event()

    async def hang():
        reached.set()
        await asyncio.sleep(60)

    w.on_call("checkout", before=hang)
    task = asyncio.create_task(w.press("approve", mandate_id))
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    w.in_review(mandate_id, "internal", None)


# ----- Stop ----------------------------------------------------------------------------------


@pytest.mark.parametrize("stop_first", [True, False])
async def test_stop_and_approve_together_have_one_winner(w, stop_first):
    mandate_id = await w.confirmed()
    presses = [w.press("stop", mandate_id, PARENT), w.press("approve", mandate_id)]
    if not stop_first:
        presses.reverse()
    results = await asyncio.gather(*presses)
    # Stop is one transaction, Approve awaits the store, so Stop lands before COMMITTING.
    assert sorted(r["ok"] for r in results) == [False, True]
    assert w.state(mandate_id) is State.ABORTED
    assert w.attempts == 0 and STORE.orders == []
    assert w.outbox("payment") == [] and w.outbox("blocked") == []


async def test_stop_does_not_wait_for_the_account_lock(w):
    mandate_id = await w.confirmed()
    reached, go = asyncio.Event(), asyncio.Event()

    async def wait():
        reached.set()
        await go.wait()

    w.on_call("get_cart", before=wait)
    approving = asyncio.create_task(w.press("approve", mandate_id))
    await asyncio.wait_for(reached.wait(), 5)
    assert w.state(mandate_id) is State.APPROVED
    stop = await asyncio.wait_for(w.press("stop", mandate_id, PARENT), 2)
    assert stop == {"ok": True, "code": "stopped"}
    go.set()
    assert (await approving) == {"ok": False, "code": "not_current"}
    assert w.state(mandate_id) is State.ABORTED
    assert w.attempts == 0
    assert w.told("stopped") == {"kid", "parent"}


async def test_approve_after_stop_is_refused(w):
    mandate_id = await w.confirmed()
    assert await w.code("stop", mandate_id, PARENT) == "stopped"
    assert await w.code("approve", mandate_id) == "not_current"
    assert w.state(mandate_id) is State.ABORTED and w.attempts == 0


async def test_stop_during_checkout_means_no_payment_message(w):
    mandate_id = await w.confirmed()
    reached, go = asyncio.Event(), asyncio.Event()

    async def wait():
        reached.set()
        await go.wait()

    w.on_call("checkout", before=wait)
    approving = asyncio.create_task(w.press("approve", mandate_id))
    await asyncio.wait_for(reached.wait(), 5)
    assert w.state(mandate_id) is State.COMMITTING
    stop = await asyncio.wait_for(w.press("stop", mandate_id, PARENT), 2)
    assert stop == {"ok": True, "code": "stop_requested"}
    assert w.state(mandate_id) is State.COMMITTING
    go.set()
    assert (await approving)["code"] == "needs_review"
    w.in_review(mandate_id, "stopped_during_checkout", "IM-100001")
    assert w.outbox("payment") == [] and w.attempts == 1
    assert await w.code("pay", mandate_id) == "not_current"
    assert w.pay_calls == 0


async def test_stop_after_the_payment_message(w):
    mandate_id = await w.waiting_for_payment()
    assert await w.code("stop", mandate_id, PARENT) == "needs_review"  # store says pending
    [told] = w.outbox("stopped")
    assert told["to"] == "kid" and told["do_not_pay"] is True
    w.in_review(mandate_id, "stopped_before_payment", "IM-100001")
    kinds = [m["kind"] for m in w.outbox()]
    assert kinds.index("stopped") < kinds.index("needs_review")
    assert await w.code("pay", mandate_id) == "not_current" and w.pay_calls == 0


async def test_stop_after_the_store_failed_the_order_is_unpaid(w):
    mandate_id = await w.waiting_for_payment()
    w.now[0] += timedelta(minutes=5)
    assert await w.code("stop", mandate_id, PARENT) == "stopped"
    assert w.state(mandate_id) is State.UNPAID
    assert w.outbox("needs_review") == []


async def test_pay_is_refused_while_a_stop_is_being_checked(w):
    mandate_id = await w.waiting_for_payment()
    reached, go = asyncio.Event(), asyncio.Event()

    async def wait():
        reached.set()
        await go.wait()

    w.on_call("check_payment_status", before=wait)
    stopping = asyncio.create_task(w.press("stop", mandate_id, PARENT))
    await asyncio.wait_for(reached.wait(), 5)
    assert (await asyncio.wait_for(w.press("pay", mandate_id), 5))["code"] == "not_current"
    assert w.pay_calls == 0
    go.set()
    assert (await stopping)["code"] == "needs_review"


async def test_stop_while_the_pin_is_being_entered(w):
    mandate_id = await w.waiting_for_payment()
    real_pay = w.upi.pay

    async def pay_then_stop(paas_id):
        assert await w.code("stop", mandate_id, PARENT) == "needs_review"
        await real_pay(paas_id)

    w.upi.pay = pay_then_stop
    assert await w.code("pay", mandate_id) == "needs_review"
    assert w.state(mandate_id) is State.NEEDS_REVIEW
    assert "paid_after_stop" in w.events() and "confirm_order" not in w.calls
    assert w.outbox("receipt") == []


async def test_stop_before_confirm_tells_only_the_parent(w):
    mandate_id = await w.prepared()
    assert await w.code("stop", mandate_id, PARENT) == "stopped"
    assert w.told("stopped") == {"parent"}


async def test_stop_after_paid_does_nothing(w):
    mandate_id = await w.waiting_for_payment()
    await w.code("pay", mandate_id)
    assert await w.code("stop", mandate_id, PARENT) == "not_current"
    assert w.state(mandate_id) is State.PAID


async def test_a_stop_request_cannot_be_cleared(w):
    mandate_id = await w.waiting_for_payment()
    await w.code("stop", mandate_id, PARENT)
    with pytest.raises(Exception, match="seal_stop"):
        w.db.conn().execute("UPDATE mandates SET stop_requested_at = NULL")


# ----- repeats, double taps, old buttons -----------------------------------------------------


async def test_double_approve_gives_one_checkout(w):
    mandate_id = await w.confirmed()
    results = await asyncio.gather(*(w.press("approve", mandate_id) for _ in range(3)))
    assert sorted(r["code"] for r in results) == ["not_current", "not_current", "placed"]
    assert w.attempts == len(w.placed) == len(STORE.orders) == 1
    assert len(w.outbox("payment")) == 1


async def test_a_repeated_message_returns_the_first_code_and_does_nothing(w):
    mandate_id = await w.confirmed()
    first = await w.press("approve", mandate_id, mid="wamid.1")
    before = (w.outbox(), w.events(), list(w.calls))
    assert await w.press("approve", mandate_id, mid="wamid.1") == first
    # The same ID with another button is still a repeat.
    assert await w.press("dont_pay", mandate_id, mid="wamid.1") == first
    assert (w.outbox(), w.events(), w.calls) == before
    assert w.attempts == 1


async def test_a_repeat_while_the_first_is_running_is_in_progress(w):
    mandate_id = await w.confirmed()
    results = await asyncio.gather(*(w.press("approve", mandate_id, mid="same") for _ in range(2)))
    assert results == [{"ok": True, "code": "placed"}, {"ok": False, "code": "in_progress"}]
    assert w.attempts == 1


async def test_a_repeated_refusal_keeps_its_code(w):
    mandate_id = await w.confirmed()
    STORE.switch_add_expensive_item()
    first = await w.press("approve", mandate_id, mid="m-x")
    STORE.reset()
    assert await w.press("approve", mandate_id, mid="m-x") == first == _refused("blocked")


def _refused(code: str) -> dict:
    return {"ok": False, "code": code}


async def test_an_old_mandate_id_does_nothing(w):
    old = await w.prepared()
    new = await w.confirmed()  # replaced the draft
    assert await w.code("confirm", old, PARENT) == "not_current"
    assert await w.code("approve", old) == "not_current"
    assert await w.code("stop", old, PARENT) == "not_current"
    assert w.state(old) is State.ABORTED and w.state(new) is State.PARENT_CONFIRMED
    assert w.attempts == 0


async def test_every_button_is_refused_outside_its_state(w):
    mandate_id = await w.confirmed()
    for action in ("pay", "dont_pay", "reviewed", "replace_cart", "not_now"):
        assert await w.code(action, mandate_id) == "not_current"
    assert await w.code("confirm", mandate_id, PARENT) == "not_current"
    assert w.state(mandate_id) is State.PARENT_CONFIRMED
    assert w.pay_calls == 0 and w.attempts == 0


# ----- who may press -------------------------------------------------------------------------

KID_ACTIONS = ["approve", "reject", "pay", "dont_pay", "replace_cart", "not_now", "reviewed"]


@pytest.mark.parametrize(
    ("number", "action"),
    [(PARENT, a) for a in KID_ACTIONS] + [(KID, "confirm"), (KID, "stop")],
)
async def test_the_wrong_role_gets_not_found_and_changes_nothing(w, number, action):
    mandate_id = await w.confirmed()
    await _changes_nothing(w, number, action, mandate_id)


@pytest.mark.parametrize("number", [STRANGER, "", "  ", None, 7])
async def test_a_stranger_gets_not_found_and_changes_nothing(w, number):
    mandate_id = await w.confirmed()
    for action in ("approve", "stop", "confirm"):
        await _changes_nothing(w, number, action, mandate_id)


async def test_another_households_people_get_not_found(w):
    mandate_id = await w.confirmed()
    with w.db.tx() as conn:
        w.household(conn, "parent-2", "kid-2", "acct2")
    await _changes_nothing(w, "kid-2", "approve", mandate_id)
    await _changes_nothing(w, "kid-2", "reject", mandate_id)
    await _changes_nothing(w, "parent-2", "stop", mandate_id)


@pytest.mark.parametrize(
    ("mid", "action", "mandate_id"),
    [
        ("", "approve", None),
        (None, "approve", None),
        ("m", "checkout", None),
        ("m", "APPROVE", None),
        ("m", "", None),
        ("m", None, None),
        ("m", "approve", "no-such-id"),
        ("m", "approve", ""),
        ("m", "approve", 5),
    ],
)
async def test_junk_presses_never_raise(w, mid, action, mandate_id):
    real = await w.confirmed()
    await _changes_nothing(w, KID, action, real if mandate_id is None else mandate_id, mid)


async def _changes_nothing(w, number, action, mandate_id, mid="fresh-id"):
    def dump():
        return [
            [tuple(r) for r in w.db.conn().execute(f"SELECT * FROM {t}")]  # noqa: S608
            for t in ("mandates", "outbox", "audit_log", "actions_seen")
        ]

    before, calls = dump(), len(w.calls)
    assert await w.actions.act(number, mid, action, mandate_id) == _refused("not_found")
    assert dump() == before and len(w.calls) == calls


async def test_a_strangers_press_does_not_use_up_the_message_id(w):
    mandate_id = await w.confirmed()
    assert (await w.press("approve", mandate_id, STRANGER, "m-1"))["code"] == "not_found"
    assert (await w.press("approve", mandate_id, KID, "m-1"))["code"] == "placed"


# ----- the simple buttons --------------------------------------------------------------------


async def test_reject(w):
    mandate_id = await w.confirmed()
    assert await w.press("reject", mandate_id) == {"ok": True, "code": "rejected"}
    assert w.state(mandate_id) is State.REJECTED and w.told("rejected") == {"parent"}
    assert await w.code("approve", mandate_id) == "not_current"
    assert w.attempts == 0


async def test_not_now_and_replace(w):
    STORE.switch_preload_cart()
    before = dict(STORE.cart)
    await w.search("atta", "milk")
    first = (await w.prepare(item(ATTA)))["mandate_id"]
    assert await w.code("not_now", first) == "not_now"
    assert w.state(first) is State.ABORTED and STORE.cart == before
    assert w.told("order_failed") == {"parent"}
    assert await w.code("replace_cart", first) == "not_current" and STORE.cart == before
    second = (await w.prepare(item(ATTA), item(MILK)))["mandate_id"]
    results = await asyncio.gather(*(w.press("replace_cart", second) for _ in range(2)))
    assert sorted(r["code"] for r in results) == ["not_current", "prepared"]
    assert w.state(second) is State.PREPARED
    assert STORE.cart == {ATTA[1]: 1, MILK[1]: 1}
    assert await w.code("confirm", second, PARENT) == "confirmed"
    assert await w.code("approve", second) == "placed"


async def test_replace_failure_returns_the_code(w):
    STORE.switch_preload_cart()
    await w.search("atta")
    mandate_id = (await w.prepare(item(ATTA, 5)))["mandate_id"]
    assert await w.press("replace_cart", mandate_id) == _refused("not_available")
    assert w.state(mandate_id) is State.ABORTED


async def test_open_in_store_is_not_built(w):
    mandate_id = await w.confirmed()
    for action in ("open_in_store", "fill_cart", "cancel_fill"):
        assert await w.press(action, mandate_id) == _refused("not_built")
    assert w.state(mandate_id) is State.PARENT_CONFIRMED


# ----- payment -------------------------------------------------------------------------------


async def test_two_pay_taps_pay_once(w):
    mandate_id = await w.waiting_for_payment()
    results = await asyncio.gather(*(w.press("pay", mandate_id) for _ in range(2)))
    assert sorted(r["code"] for r in results) == ["not_current", "paid"]
    assert await w.code("pay", mandate_id) == "not_current"
    assert w.pay_calls == 1
    assert len(w.outbox("receipt")) == 2  # one each


async def test_dont_pay_while_the_store_says_pending_is_needs_review(w):
    mandate_id = await w.waiting_for_payment()
    assert await w.code("dont_pay", mandate_id) == "needs_review"
    w.in_review(mandate_id, "kid_did_not_pay", "IM-100001")
    assert w.pay_calls == 0


async def test_dont_pay_after_the_store_failed_it_is_unpaid(w):
    mandate_id = await w.waiting_for_payment()
    w.now[0] += timedelta(minutes=5)
    assert await w.press("dont_pay", mandate_id) == {"ok": True, "code": "unpaid"}
    assert w.state(mandate_id) is State.UNPAID and w.told("unpaid") == {"kid", "parent"}
    assert w.pay_calls == 0


async def test_pay_after_the_store_failed_it_is_unpaid(w):
    mandate_id = await w.waiting_for_payment()
    w.now[0] += timedelta(minutes=5)
    assert await w.press("pay", mandate_id) == _refused("payment_failed")
    assert w.state(mandate_id) is State.UNPAID
    assert w.outbox("receipt") == []


async def test_a_failed_payment_on_a_live_order_is_not_unpaid(w):
    mandate_id = await w.waiting_for_payment()
    STORE.orders[0]["payment_status"] = "FAILED"  # the order itself is still PENDING_PAYMENT
    assert await w.code("dont_pay", mandate_id) == "needs_review"


async def test_a_lost_upi_reply_that_paid_still_completes(w):
    mandate_id = await w.waiting_for_payment()
    real_pay = w.upi.pay

    async def lost(paas_id):
        await real_pay(paas_id)
        raise UpiUnclear("timeout")

    w.upi.pay = lost
    assert await w.code("pay", mandate_id) == "paid"


async def test_a_upi_failure_that_left_it_pending_is_needs_review(w):
    mandate_id = await w.waiting_for_payment()

    async def down(paas_id):
        raise UpiUnclear("down")

    w.upi.pay = down
    assert await w.code("pay", mandate_id) == "needs_review"
    w.in_review(mandate_id, "payment_unclear", "IM-100001")
    assert w.outbox("receipt") == []


@pytest.mark.parametrize("tool", ["check_payment_status", "confirm_order", "get_orders"])
async def test_a_store_error_while_paying_is_needs_review(w, tool):
    mandate_id = await w.waiting_for_payment()

    async def down(*_):
        raise StoreUnavailable("down")

    w.on_call(tool, replace=down)
    assert await w.code("pay", mandate_id) == "needs_review"
    w.in_review(mandate_id, "store_error", "IM-100001")
    assert w.outbox("receipt") == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status": "PENDING_PAYMENT"}, "order_status_differs"),
        ({"totalAmount": 1.0}, "total_differs"),
        ({"items": [{"itemId": ATTA[0], "quantity": 2}], "itemCount": 2}, "items_differ"),
        ({"orderId": "IM-999"}, "order_count"),
    ],
)
async def test_an_order_that_differs_after_payment_is_needs_review(w, change, reason):
    mandate_id = await w.waiting_for_payment()
    w.on_call("get_orders", after=_edit_order(**change))
    assert await w.code("pay", mandate_id) == "needs_review"
    w.in_review(mandate_id, reason, "IM-100001")
    assert w.outbox("receipt") == []


async def test_a_bug_while_paying_is_needs_review(w):
    mandate_id = await w.waiting_for_payment()

    async def bug(paas_id):
        raise RuntimeError("bug")

    w.upi.pay = bug
    assert await w.code("pay", mandate_id) == "needs_review"
    w.in_review(mandate_id, "internal", "IM-100001")


# ----- ignored payments ----------------------------------------------------------------------


async def test_an_ignored_payment_is_swept_to_unpaid(w):
    mandate_id = await w.waiting_for_payment()
    w.now[0] += timedelta(minutes=5, seconds=29)
    assert await w.actions.sweep_payments() == []
    assert w.state(mandate_id) is State.PLACED_PENDING_PAYMENT
    w.now[0] += timedelta(seconds=1)
    assert await w.actions.sweep_payments() == [mandate_id]
    assert w.state(mandate_id) is State.UNPAID
    assert w.told("unpaid") == {"kid", "parent"}
    assert await w.actions.sweep_payments() == []


async def test_an_ignored_payment_does_not_block_the_next_order(w):
    mandate_id = await w.waiting_for_payment()
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA))
    assert err.value.code == "order_already_open"
    w.now[0] += timedelta(minutes=6)
    assert (await w.prepare(item(ATTA)))["status"] == "prepared"  # no one called the sweep
    assert w.state(mandate_id) is State.UNPAID


async def test_the_sweep_completes_a_payment_that_succeeded(w):
    mandate_id = await w.waiting_for_payment()
    STORE.pay("paas100001")  # the gate died before it saw this
    w.now[0] += timedelta(minutes=6)
    await w.actions.sweep_payments()
    assert w.state(mandate_id) is State.PAID
    assert w.told("receipt") == {"kid", "parent"}
    assert STORE.orders[0]["status"] == "CONFIRMED" and w.attempts == 1


@pytest.mark.parametrize("unclear", ["pending", "down", "odd"])
async def test_the_sweep_sends_unclear_payments_to_review(w, unclear):
    mandate_id = await w.waiting_for_payment()
    if unclear == "pending":
        w.on_call("check_payment_status", before=_async(lambda: _touch_created(w)))
    elif unclear == "down":

        async def down(*_):
            raise StoreUnavailable("down")

        w.on_call("check_payment_status", replace=down)
    else:
        w.on_call("check_payment_status", after=lambda d: {**d, "status": "MAYBE"})
    w.now[0] += timedelta(minutes=6)
    await w.actions.sweep_payments()
    reason = "payment_unclear" if unclear == "pending" else "store_error"
    w.in_review(mandate_id, reason, "IM-100001")


def _async(fn):
    async def run():
        fn()

    return run


def _touch_created(w):
    STORE.orders[0]["created_at"] = w.now[0].timestamp()


# ----- review --------------------------------------------------------------------------------


async def test_reviewed_is_for_the_kid_in_needs_review_only(w):
    mandate_id = await w.waiting_for_payment()
    assert await w.code("reviewed", mandate_id) == "not_current"
    await w.code("dont_pay", mandate_id)
    assert (await w.press("reviewed", mandate_id, PARENT))["code"] == "not_found"
    with pytest.raises(GateError) as err:
        await w.prepare(item(ATTA))
    assert err.value.code == "needs_review"
    assert await w.press("reviewed", mandate_id) == {"ok": True, "code": "reviewed"}
    assert await w.code("reviewed", mandate_id) == "not_current"
    assert w.state(mandate_id) is State.NEEDS_REVIEW
    assert (await w.prepare(item(ATTA)))["status"] == "prepared"


# ----- restart -------------------------------------------------------------------------------


async def _crash_at(w, mandate_id, tool):
    """Stop the world inside one store call, as if the process died there."""
    reached = asyncio.Event()

    async def hang():
        reached.set()
        await asyncio.sleep(60)

    w.on_call(tool, before=hang)
    task = asyncio.create_task(w.press("approve", mandate_id))
    await asyncio.wait_for(reached.wait(), 5)
    return task


async def _kill(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_restart_in_approved_expires_and_never_checks_out(w):
    mandate_id = await w.confirmed()
    task = await _crash_at(w, mandate_id, "get_cart")
    assert w.state(mandate_id) is State.APPROVED
    assert gdb.recover_on_start(w.db, w.now[0]) == [(mandate_id, State.EXPIRED)]
    assert w.told("expired") == {"kid"}
    await _kill(task)
    assert w.state(mandate_id) is State.EXPIRED
    assert await w.code("approve", mandate_id) == "not_current"
    assert w.attempts == 0 and STORE.orders == []


async def test_restart_in_committing_is_needs_review_and_never_a_retry(w):
    mandate_id = await w.confirmed()
    task = await _crash_at(w, mandate_id, "checkout")
    assert w.state(mandate_id) is State.COMMITTING
    assert gdb.recover_on_start(w.db, w.now[0]) == [(mandate_id, State.NEEDS_REVIEW)]
    w.in_review(mandate_id, "restart", None)
    await _kill(task)
    assert await w.code("approve", mandate_id) == "not_current"
    assert w.attempts == 0 and w.outbox("payment") == []


async def test_restart_after_checkout_returned_keeps_the_order_id(w):
    mandate_id = await w.confirmed()
    reads = []
    reached = asyncio.Event()

    async def hang_on_readback():
        reads.append(1)
        if len(reads) == 2:
            reached.set()
            await asyncio.sleep(60)

    w.on_call("get_orders", before=hang_on_readback)
    task = asyncio.create_task(w.press("approve", mandate_id))
    await asyncio.wait_for(reached.wait(), 5)
    gdb.recover_on_start(w.db, w.now[0])
    w.in_review(mandate_id, "restart", "IM-100001")
    await _kill(task)
    assert w.attempts == 1


# ----- setup ---------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["", "  ", "short", "k" * 31])
def test_a_weak_server_key_stops_the_start(w, key):
    with pytest.raises(WeakServerKey):
        Actions(w.gate, w.upi, key)


def test_build_actions_recovers_and_points_upi_at_the_store(tmp_path):
    settings = Settings(
        _env_file=None, internal_token=TOKEN, server_key=KEY, db_path=tmp_path / "b.db"
    )
    actions = gate_server.build_actions(settings)
    assert actions.upi._url == "http://127.0.0.1:8001/demo/pay"
    actions.db.close()
    with pytest.raises(WeakServerKey):
        gate_server.build_actions(settings.model_copy(update={"server_key": ""}))


def test_an_approved_cart_must_match_its_hash(w):
    digest = "0" * 64
    mandate_id = w.fake(
        State.APPROVED,
        cart_json=json.dumps({"items": [], "address_id": "a", "address_text": "b"}),
        cart_hash=digest,
        total_paise=1,
        max_total_paise=1,
    )
    with pytest.raises(actions_module._Blocked):
        w.actions._approved_cart(w.row(mandate_id))


def test_order_problem_merges_lines_of_one_sku():
    from ordr.contracts import Cart, CartLine
    from ordr.gate.store_client import StoreOrder

    cart = Cart(
        lines=(CartLine("s", "x", 1, "", "", 0), CartLine("s", "y", 2, "", "", 0)),
        total_paise=100,
        address_id="a",
        address_text="t",
    )
    order = StoreOrder("o", "P", "PENDING", "UPI", (("s", 3),), "a", "t", 100)
    args = {"status": "P", "payment_status": "PENDING", "total_paise": 100}
    assert actions_module._order_problem(cart, 100, order, **args) is None
    assert actions_module._order_problem(cart, 99, order, **args) == "over_ceiling"
    fewer = dataclasses.replace(order, items=(("s", 2),))
    assert actions_module._order_problem(cart, 100, fewer, **args) == "items_differ"
