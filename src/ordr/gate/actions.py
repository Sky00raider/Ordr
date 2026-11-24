"""Button presses: Confirm to PAID, and every way out (TRD section 5, N6; PRD section 6).

`Actions.act` is called by the webhook through the internal API, never by the model.

Rules kept here (CLAUDE.md 2, 6, 7, 8, 9, 10):
- A press counts only from a registered number, for its own household's mandate, in its own role.
  Anyone else gets `not_found` and nothing is written.
- `checkout` is called in one place, after the move APPROVED to COMMITTING changed one row. No
  state leads back to APPROVED, so it runs at most once per mandate. It is never retried.
- Before COMMITTING every failure is BLOCKED with zero checkout calls. After it, NEEDS_REVIEW.
- No await inside a transaction. Approve, Replace, Pay, Don't pay and the sweep take the account
  lock. Stop and Reject do not wait for it: the atomic state moves decide who wins.
- The kid's number is bound into the approval and never written to the log.

Not checked: that the cart becomes one store order. The demo store's `get_cart` cannot say. The
read-back refuses anything but exactly one new order before payment.
"""

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ordr.contracts import ACTION_ROLES, TRANSITIONS, Action, Cart, CartLine, Role, State
from ordr.gate import db as gdb
from ordr.gate.binding import (
    Approval,
    cart_hash,
    check_server_key,
    sign_approval,
    verify_approval,
)
from ordr.gate.db import OutboxMsg, append_audit, enqueue, transition
from ordr.gate.service import Gate, GateError
from ordr.gate.store_client import (
    PENDING_PAYMENT,
    UPI,
    PaymentStatus,
    Placed,
    StoreOrder,
    StoreUnavailable,
)
from ordr.gate.upi import SimulatedUpi, UpiUnclear

log = logging.getLogger(__name__)

# The store fails unpaid UPI orders after 5 minutes (our assumption, PRD section 7). The extra
# 30 s lets it do that before we ask.
PAYMENT_WINDOW = timedelta(minutes=5, seconds=30)

# The press did what its button says. Every other code is a refusal or a bad ending.
OK_CODES = frozenset(
    {
        "confirmed",
        "rejected",
        "not_now",
        "prepared",
        "placed",
        "stopped",
        "stop_requested",
        "paid",
        "unpaid",
        "reviewed",
    }
)
# What a press that lost a race reports.
LOST = {
    State.EXPIRED: "expired",
    State.BLOCKED: "blocked",
    State.NEEDS_REVIEW: "needs_review",
}
BOTH = (Role.KID, Role.PARENT)


class _Blocked(Exception):
    """A final check failed."""

    def __init__(self, reason: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.data = dict(data or {})


@dataclass(frozen=True)
class _Plan:
    """What the final checks approved. Read from the store and the sealed row only."""

    cart: Cart
    max_total_paise: int
    known_orders: frozenset[str]


def _result(code: str) -> dict[str, Any]:
    return {"ok": code in OK_CODES, "code": code}


class Actions:
    def __init__(self, gate: Gate, upi: SimulatedUpi, server_key: str) -> None:
        check_server_key(server_key)
        self.gate = gate
        self.db = gate.db
        self.store = gate.store
        self.upi = upi
        self._key = server_key
        gate.before_prepare = self.sweep_payments
        self._handlers = {
            Action.CONFIRM: self._confirm,
            Action.STOP: self._stop,
            Action.APPROVE: self._approve,
            Action.REJECT: self._reject,
            Action.PAY: self._pay,
            Action.DONT_PAY: self._dont_pay,
            Action.REPLACE_CART: self._replace,
            Action.NOT_NOW: self._not_now,
            Action.REVIEWED: self._reviewed,
            # Open in store is N8
            Action.OPEN_IN_STORE: self._not_built,
            Action.FILL_CART: self._not_built,
            Action.CANCEL_FILL: self._not_built,
        }

    # ----- entry -----------------------------------------------------------------------

    async def act(
        self, number: str, message_id: str, action: str, mandate_id: str
    ) -> dict[str, Any]:
        """One button press. Never raises for a bad press. A repeat does nothing."""
        try:
            return await self._act(number, message_id, action, mandate_id)
        except Exception as exc:  # no details: they could hold a number
            log.error("press failed: %s", type(exc).__name__)
            return _result("error")

    async def _act(
        self, number: str, message_id: str, action: str, mandate_id: str
    ) -> dict[str, Any]:
        wanted = self._check_press(number, message_id, action, mandate_id)
        if wanted is None:
            return _result("not_found")
        with self.db.tx() as conn:
            first = gdb.claim_action(conn, message_id)
            earlier = None if first else gdb.action_code(conn, message_id)
        if not first:
            return _result(earlier or "in_progress")
        code = "error"
        try:
            code = await self._handlers[wanted](mandate_id)
        except GateError as exc:
            code = "not_current" if exc.code == "not_found" else exc.code
        finally:
            try:
                with self.db.tx() as conn:
                    gdb.set_action_code(conn, message_id, code)
            except sqlite3.Error as exc:  # the press is done; a repeat will say in_progress
                log.error("could not save a press code: %s", type(exc).__name__)
        return _result(code)

    def _check_press(
        self, number: str, message_id: str, action: str, mandate_id: str
    ) -> Action | None:
        """Registered number, own household's mandate, right role. Writes nothing."""
        if not all(
            isinstance(v, str) and v.strip() for v in (number, message_id, action, mandate_id)
        ):
            return None
        conn = self.db.conn()
        found = gdb.household_for_number(conn, number.strip())
        if found is None:
            return None
        household, role = found
        row = gdb.get_mandate(conn, mandate_id)
        if row is None or row["household_id"] != household["id"]:
            return None
        try:
            wanted = Action(action)
        except ValueError:
            return None
        return wanted if ACTION_ROLES[wanted] is role else None

    # ----- small helpers ---------------------------------------------------------------

    def _now(self) -> datetime:
        return self.gate.clock()

    def _row(self, mandate_id: str) -> sqlite3.Row:
        row = gdb.get_mandate(self.db.conn(), mandate_id)
        if row is None:
            raise GateError("not_found")
        return row

    def _lost(self, mandate_id: str) -> str:
        """Someone else moved the mandate first. Report where it is."""
        return LOST.get(State(self._row(mandate_id)["state"]), "not_current")

    def _move(
        self,
        mandate_id: str,
        old: State,
        new: State,
        event: str,
        code: str,
        *,
        data: Mapping[str, Any] | None = None,
        tell: Mapping[Role, tuple[str, Mapping[str, Any]]] | None = None,
        reason: str | None = None,
    ) -> str:
        """One atomic move. Returns `code` if this call made it, else where the mandate is."""
        outbox = [OutboxMsg(role, kind, body) for role, (kind, body) in (tell or {}).items()]
        with self.db.tx() as conn:
            moved = transition(
                conn,
                mandate_id,
                old,
                new,
                event=event,
                data=data,
                set_fields={"reason": reason} if reason else None,
                outbox=outbox,
                now=self._now(),
            )
        return code if moved else self._lost(mandate_id)

    # ----- before the kid approves -----------------------------------------------------

    async def _not_built(self, mandate_id: str) -> str:
        return "not_built"

    async def _confirm(self, mandate_id: str) -> str:
        now = self._now()
        with self.db.tx() as conn:
            row = gdb.get_mandate(conn, mandate_id)
            if row["state"] != State.PREPARED.value:
                return "not_current"
            if gdb.expire_one(conn, row, now):
                return "expired"
            card = _card(row)
            transition(
                conn,
                mandate_id,
                State.PREPARED,
                State.PARENT_CONFIRMED,
                event="parent_confirmed",
                outbox=[
                    # No Open in store button until N8.
                    OutboxMsg(
                        Role.KID,
                        "kid_card",
                        {**card, "buttons": [Action.APPROVE.value, Action.REJECT.value]},
                    ),
                    OutboxMsg(Role.PARENT, "sent_to_kid", {**card, "buttons": [Action.STOP.value]}),
                ],
                now=now,
            )
        return "confirmed"

    async def _reject(self, mandate_id: str) -> str:
        return self._move(
            mandate_id,
            State.PARENT_CONFIRMED,
            State.REJECTED,
            "rejected",
            "rejected",
            tell={Role.PARENT: ("rejected", {})},
            reason="rejected by kid",
        )

    async def _not_now(self, mandate_id: str) -> str:
        return self._move(
            mandate_id,
            State.WAITING_FOR_CART,
            State.ABORTED,
            "not_now",
            "not_now",
            tell={Role.PARENT: ("order_failed", {"code": "not_now"})},
            reason="kid kept their cart",
        )

    async def _replace(self, mandate_id: str) -> str:
        with self.db.tx() as conn:
            row = gdb.get_mandate(conn, mandate_id)
            if row["state"] != State.WAITING_FOR_CART.value:
                return "not_current"
            if gdb.expire_one(conn, row, self._now()):
                return "expired"
        return (await self.gate.fill_cart(mandate_id))["status"]

    async def _reviewed(self, mandate_id: str) -> str:
        with self.db.tx() as conn:
            done = gdb.mark_reviewed(conn, mandate_id, self._now())
        return "reviewed" if done else "not_current"

    # ----- Approve ---------------------------------------------------------------------

    async def _approve(self, mandate_id: str) -> str:
        async with self.gate.account_lock(self._row(mandate_id)["store_account"]):
            refused = self._sign(mandate_id)
            if refused:
                return refused
            try:
                plan = await self._final_checks(mandate_id)
                started = self._start_commit(mandate_id, plan)
            except _Blocked as exc:
                return self._block(mandate_id, exc.reason, exc.data)
            except StoreUnavailable as exc:
                return self._block(mandate_id, "store_error", {"detail": str(exc)})
            except BaseException as exc:
                code = self._block(mandate_id, "internal", {"error": type(exc).__name__})
                if not isinstance(exc, Exception):  # cancelled: zero checkout calls, pass it on
                    raise
                return code
            if not started:
                return self._lost(mandate_id)
            return await self._commit(mandate_id, plan)

    def _sign(self, mandate_id: str) -> str | None:
        """Not expired, sign, move to APPROVED. One transaction. A code if refused."""
        now = self._now()
        with self.db.tx() as conn:
            row = gdb.get_mandate(conn, mandate_id)
            if row["state"] != State.PARENT_CONFIRMED.value:
                return "not_current"
            if gdb.expire_one(conn, row, now):
                return "expired"
            transition(
                conn,
                mandate_id,
                State.PARENT_CONFIRMED,
                State.APPROVED,
                event="approved",
                data={
                    "cart_hash": row["cart_hash"],
                    "max_total_paise": row["max_total_paise"],
                    "quote": json.loads(row["quote_json"]),
                },
                set_fields={"approval_sig": sign_approval(self._key, self._approval(conn, row))},
                now=now,
            )
        return None

    def _approval(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Approval:
        kid = conn.execute(
            "SELECT kid_number FROM households WHERE id = ?", (row["household_id"],)
        ).fetchone()["kid_number"]
        return Approval(
            mandate_id=row["id"],
            cart_hash=row["cart_hash"],
            max_total_paise=row["max_total_paise"],
            approver_id=kid,
            nonce=row["nonce"],
            expires_at=row["expires_at"],
        )

    def _check_approval(self, conn: sqlite3.Connection, mandate_id: str) -> sqlite3.Row:
        row = gdb.get_mandate(conn, mandate_id)
        if row is None or row["state"] != State.APPROVED.value:
            raise _Blocked("not_approved")
        if not verify_approval(self._key, self._approval(conn, row), row["approval_sig"]):
            raise _Blocked("bad_signature")
        return row

    def _check_caps(self, conn: sqlite3.Connection, row: sqlite3.Row) -> None:
        policy, ceiling = self.gate.policy, row["max_total_paise"]
        if ceiling > policy.per_order_cap_paise:
            raise _Blocked("over_cap", {"cap": "per_order"})
        spent = self.gate.spent_this_month(conn, row, self._now())
        if spent + ceiling > policy.monthly_cap_paise:
            raise _Blocked("over_cap", {"cap": "per_month"})
        if UPI not in policy.allowed_payment:
            raise _Blocked("payment_not_allowed")

    async def _final_checks(self, mandate_id: str) -> _Plan:
        """PRD section 6, "Final checks". Reads only. Raises _Blocked or StoreUnavailable."""
        with self.db.tx() as conn:
            row = self._check_approval(conn, mandate_id)
        approved = self._approved_cart(row)
        live = await self.store.get_cart()
        seen = {
            "total_paise": live.total_paise,
            "max_total_paise": row["max_total_paise"],
            "live_items": [[ln.sku_id, ln.spin_id, ln.qty] for ln in live.lines],
            "live_fees_paise": live.fees_paise,
        }
        if not live.lines or cart_hash(live, store=self.store.store_id) != row["cart_hash"]:
            raise _Blocked("cart_changed", seen)
        if not 0 < live.total_paise <= row["max_total_paise"]:
            raise _Blocked("over_ceiling", seen)
        if not live.all_in_stock:
            raise _Blocked("out_of_stock", seen)
        if not live.serviceable:
            raise _Blocked("not_serviceable", seen)
        with self.db.tx() as conn:
            self._check_caps(conn, row)
        known = frozenset(order.order_id for order in await self.store.get_orders())
        return _Plan(approved, row["max_total_paise"], known)

    def _approved_cart(self, row: sqlite3.Row) -> Cart:
        """Items and address the kid approved, checked against the signed hash."""
        saved = json.loads(row["cart_json"])
        cart = Cart(
            lines=tuple(CartLine(sku, spin, qty, "", "", 0) for sku, spin, qty in saved["items"]),
            total_paise=row["total_paise"],
            address_id=saved["address_id"],
            address_text=saved["address_text"],
        )
        if cart_hash(cart, store=self.store.store_id) != row["cart_hash"]:
            raise _Blocked("cart_changed", {"detail": "saved cart does not match its hash"})
        return cart

    def _start_commit(self, mandate_id: str, plan: _Plan) -> bool:
        """APPROVED to COMMITTING, the single use. False if anyone moved it first."""
        with self.db.tx() as conn:
            current = gdb.get_mandate(conn, mandate_id)
            if current is None or current["state"] != State.APPROVED.value:
                return False
            self._check_caps(conn, self._check_approval(conn, mandate_id))  # again, last moment
            return transition(
                conn,
                mandate_id,
                State.APPROVED,
                State.COMMITTING,
                event="committing",
                data={"known_order_ids": sorted(plan.known_orders)},
                now=self._now(),
            )

    def _block(self, mandate_id: str, reason: str, data: Mapping[str, Any]) -> str:
        told = {"reason": reason} | {
            k: data[k] for k in ("total_paise", "max_total_paise") if k in data
        }
        return self._move(
            mandate_id,
            State.APPROVED,
            State.BLOCKED,
            "blocked",
            "blocked",
            data={**data, "reason": reason},
            tell={role: ("blocked", told) for role in BOTH},
            reason=reason,
        )

    # ----- checkout and read-back ------------------------------------------------------

    async def _commit(self, mandate_id: str, plan: _Plan) -> str:
        """COMMITTING only. The one and only checkout call. Every bad end is NEEDS_REVIEW."""
        placed: Placed | None = None
        try:
            try:
                placed = await self.store.checkout(plan.cart.address_id)
            except StoreUnavailable as exc:
                return await self._checkout_unclear(mandate_id, plan, exc)
            with self.db.tx() as conn:
                gdb.record_order(conn, mandate_id, placed.order_id, placed.paas_id, self._now())
            try:
                orders = await self.store.get_orders()
            except StoreUnavailable as exc:
                return self._review(
                    mandate_id,
                    State.COMMITTING,
                    "readback_failed",
                    placed.order_id,
                    {"detail": str(exc)},
                )
            new = [order for order in orders if order.order_id not in plan.known_orders]
            problem = _readback_problem(plan, placed, new)
            if problem:
                return self._review(
                    mandate_id,
                    State.COMMITTING,
                    problem,
                    placed.order_id,
                    {"new_orders": [_order_log(order) for order in new]},
                )
            return self._placed(mandate_id, placed)
        except BaseException as exc:
            code = self._review(
                mandate_id,
                State.COMMITTING,
                "internal",
                placed.order_id if placed else None,
                {"error": type(exc).__name__},
            )
            if not isinstance(exc, Exception):
                raise
            return code

    async def _checkout_unclear(self, mandate_id: str, plan: _Plan, exc: StoreUnavailable) -> str:
        """Checkout did not clearly place the order. Read what is known, never call again."""
        data: dict[str, Any] = {"detail": str(exc)}
        order_id = None
        try:
            orders = await self.store.get_orders()
        except StoreUnavailable as read_error:
            data["read_error"] = str(read_error)
        else:
            new = [order for order in orders if order.order_id not in plan.known_orders]
            data["new_orders"] = [_order_log(order) for order in new]
            if len(new) == 1:
                order_id = new[0].order_id
        reason = "duplicate_order" if exc.duplicate else "checkout_unclear"
        return self._review(mandate_id, State.COMMITTING, reason, order_id, data)

    def _placed(self, mandate_id: str, placed: Placed) -> str:
        """The order is exactly what was approved. Payment is asked for only if nobody stopped."""
        now = self._now()
        with self.db.tx() as conn:
            row = gdb.get_mandate(conn, mandate_id)
            stopped = row["stop_requested_at"] is not None
            if stopped:
                moved = transition(
                    conn,
                    mandate_id,
                    State.COMMITTING,
                    State.NEEDS_REVIEW,
                    event="needs_review",
                    data={"reason": "stopped_during_checkout", "order_id": placed.order_id},
                    set_fields={
                        "reason": "stopped_during_checkout",
                        "order_id": placed.order_id,
                        "paas_id": placed.paas_id,
                    },
                    now=now,
                )
            else:
                payment = {
                    **_card(row),
                    "order_id": placed.order_id,
                    "total_paise": placed.total_paise,
                    "buttons": [Action.PAY.value, Action.DONT_PAY.value],
                }
                del payment["fees_paise"]  # quoted fees; the order may have a small new one
                moved = transition(
                    conn,
                    mandate_id,
                    State.COMMITTING,
                    State.PLACED_PENDING_PAYMENT,
                    event="placed",
                    data={"order_id": placed.order_id, "total_paise": placed.total_paise},
                    set_fields={
                        "order_id": placed.order_id,
                        "paas_id": placed.paas_id,
                        "pay_by": gdb.iso(now + PAYMENT_WINDOW),
                    },
                    outbox=[OutboxMsg(Role.KID, "payment", payment)],
                    now=now,
                )
        if not moved:
            return self._lost(mandate_id)
        return "needs_review" if stopped else "placed"

    def _review(
        self,
        mandate_id: str,
        old: State,
        reason: str,
        order_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        """To NEEDS_REVIEW. `transition` tells both people."""
        fields = {"reason": reason} | ({"order_id": order_id} if order_id else {})
        with self.db.tx() as conn:
            moved = transition(
                conn,
                mandate_id,
                old,
                State.NEEDS_REVIEW,
                event="needs_review",
                data={**(data or {}), **fields},
                set_fields=fields,
                now=self._now(),
            )
        return "needs_review" if moved else self._lost(mandate_id)

    # ----- Stop ------------------------------------------------------------------------

    async def _stop(self, mandate_id: str) -> str:
        """Never waits for the account lock. Reads and moves in one transaction."""
        now = self._now()
        with self.db.tx() as conn:
            row = gdb.get_mandate(conn, mandate_id)
            state = State(row["state"])
            if state is State.COMMITTING:
                gdb.request_stop(conn, mandate_id, now)
                return "stop_requested"
            if state is not State.PLACED_PENDING_PAYMENT:
                if State.ABORTED not in TRANSITIONS.get(state, frozenset()):
                    return "not_current"
                outbox = [OutboxMsg(Role.PARENT, "stopped", {})]
                if state not in (State.WAITING_FOR_CART, State.PREPARED):
                    outbox.append(OutboxMsg(Role.KID, "stopped", {}))
                transition(
                    conn,
                    mandate_id,
                    state,
                    State.ABORTED,
                    event="stopped",
                    data={"by": "parent"},
                    set_fields={"reason": "stopped by parent"},
                    outbox=outbox,
                    now=now,
                )
                return "stopped"
            if gdb.request_stop(conn, mandate_id, now):
                body = {"mandate_id": mandate_id, "order_id": row["order_id"], "do_not_pay": True}
                enqueue(conn, row["household_id"], OutboxMsg(Role.KID, "stopped", body))
        code = await self._close_unpaid(mandate_id, "stopped_before_payment")
        return "stopped" if code == "unpaid" else code

    # ----- payment ---------------------------------------------------------------------

    async def _pay(self, mandate_id: str) -> str:
        async with self.gate.account_lock(self._row(mandate_id)["store_account"]):
            with self.db.tx() as conn:
                row = gdb.get_mandate(conn, mandate_id)
                if row["state"] != State.PLACED_PENDING_PAYMENT.value or row["stop_requested_at"]:
                    return "not_current"
                append_audit(conn, row["household_id"], mandate_id, "pay_tapped", {}, self._now())
            try:
                try:
                    await self.upi.pay(row["paas_id"])
                except UpiUnclear as exc:
                    log.warning("simulated UPI unclear: %s", exc)  # the store decides
                status = await self._payment_status(row)
                if status.success:
                    return await self._complete(row, status)
                if status.failed:
                    return self._unpaid(mandate_id, "payment failed", "payment_failed")
                return self._review(mandate_id, State.PLACED_PENDING_PAYMENT, "payment_unclear")
            except BaseException as exc:
                return self._payment_error(mandate_id, exc)

    async def _dont_pay(self, mandate_id: str) -> str:
        async with self.gate.account_lock(self._row(mandate_id)["store_account"]):
            if self._row(mandate_id)["state"] != State.PLACED_PENDING_PAYMENT.value:
                return "not_current"
            return await self._close_unpaid(mandate_id, "kid_did_not_pay")

    async def _close_unpaid(self, mandate_id: str, reason: str) -> str:
        """UNPAID only if the store says the order failed, else NEEDS_REVIEW."""
        try:
            status = await self._payment_status(self._row(mandate_id))
            if status.failed:
                return self._unpaid(mandate_id, reason, "unpaid")
            return self._review(
                mandate_id,
                State.PLACED_PENDING_PAYMENT,
                reason,
                None,
                {"payment_status": status.status, "order_status": status.order_status},
            )
        except BaseException as exc:
            return self._payment_error(mandate_id, exc)

    def _payment_error(self, mandate_id: str, exc: BaseException) -> str:
        """Call from an except block. NEEDS_REVIEW, then a cancellation is passed on."""
        reason = "store_error" if isinstance(exc, StoreUnavailable) else "internal"
        data = {"error": type(exc).__name__}
        code = self._review(mandate_id, State.PLACED_PENDING_PAYMENT, reason, None, data)
        if not isinstance(exc, Exception):
            raise exc
        return code

    async def _payment_status(self, row: sqlite3.Row) -> PaymentStatus:
        return await self.store.check_payment_status(row["order_id"], row["paas_id"])

    def _unpaid(self, mandate_id: str, reason: str, code: str) -> str:
        order_id = self._row(mandate_id)["order_id"]
        return self._move(
            mandate_id,
            State.PLACED_PENDING_PAYMENT,
            State.UNPAID,
            "unpaid",
            code,
            data={"order_id": order_id, "reason": reason},
            tell={role: ("unpaid", {"order_id": order_id}) for role in BOTH},
            reason=reason,
        )

    async def _complete(self, row: sqlite3.Row, status: PaymentStatus) -> str:
        """The store says paid. Confirm, read the order once more, then PAID and receipts."""
        mandate_id = row["id"]
        if self._row(mandate_id)["state"] != State.PLACED_PENDING_PAYMENT.value:
            # A late Stop won while the PIN was being entered.
            with self.db.tx() as conn:
                append_audit(
                    conn,
                    row["household_id"],
                    mandate_id,
                    "paid_after_stop",
                    {"order_id": row["order_id"]},
                    self._now(),
                )
            return self._lost(mandate_id)
        await self.store.confirm_order(row["order_id"], row["paas_id"])
        found = [o for o in await self.store.get_orders() if o.order_id == row["order_id"]]
        problem = "order_count"
        if len(found) == 1:
            problem = _order_problem(
                self._approved_cart(row),
                row["max_total_paise"],
                found[0],
                status="CONFIRMED",
                payment_status="SUCCESS",
                total_paise=status.total_paise,
            )
        if problem:
            return self._review(
                mandate_id,
                State.PLACED_PENDING_PAYMENT,
                problem,
                None,
                {"orders": [_order_log(order) for order in found]},
            )
        receipt = {
            **_card(row),
            "order_id": row["order_id"],
            "total_paise": status.total_paise,
        }
        del receipt["fees_paise"]
        return self._move(
            mandate_id,
            State.PLACED_PENDING_PAYMENT,
            State.PAID,
            "paid",
            "paid",
            data={"order_id": row["order_id"], "total_paise": status.total_paise},
            tell={role: ("receipt", receipt) for role in BOTH},
        )

    async def sweep_payments(self) -> list[str]:
        """Close payments past their window. Run often, and before every prepare_order."""
        now = gdb.iso(self._now())
        due = (
            self.db.conn()
            .execute(
                "SELECT id, store_account FROM mandates WHERE state = 'PLACED_PENDING_PAYMENT' "
                "AND (pay_by IS NULL OR pay_by <= ?)",
                (now,),
            )
            .fetchall()
        )
        for entry in due:
            async with self.gate.account_lock(entry["store_account"]):
                await self._sweep_one(entry["id"])
        return [entry["id"] for entry in due]

    async def _sweep_one(self, mandate_id: str) -> None:
        row = self._row(mandate_id)
        if row["state"] != State.PLACED_PENDING_PAYMENT.value:
            return
        try:
            status = await self._payment_status(row)
            if status.success and not row["stop_requested_at"]:
                await self._complete(row, status)
            elif status.failed:
                self._unpaid(mandate_id, "payment window passed", "unpaid")
            else:
                self._review(mandate_id, State.PLACED_PENDING_PAYMENT, "payment_unclear")
        except BaseException as exc:
            self._payment_error(mandate_id, exc)


# ----- pure helpers ------------------------------------------------------------------------


def _card(row: sqlite3.Row) -> dict[str, Any]:
    """What people are shown, from the saved quote. Never from the model."""
    quote = json.loads(row["quote_json"])
    return {
        "lines": [
            {k: line[k] for k in ("name", "pack", "qty", "unit_price_paise")}
            for line in quote["lines"]
        ],
        "fees_paise": quote["fees_paise"],
        "total_paise": quote["total_paise"],
        "max_total_paise": quote["max_total_paise"],
        "address_text": json.loads(row["cart_json"])["address_text"],
        "is_duplicate": bool(row["is_duplicate"]),
    }


def _order_log(order: StoreOrder) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "status": order.status,
        "payment_status": order.payment_status,
        "items": [list(item) for item in order.items],
        "address_id": order.address_id,
        "total_paise": order.total_paise,
    }


def _readback_problem(plan: _Plan, placed: Placed, new: list[StoreOrder]) -> str | None:
    if len(new) != 1:
        return "order_count"
    if new[0].order_id != placed.order_id:
        return "order_id_differs"
    return _order_problem(
        plan.cart,
        plan.max_total_paise,
        new[0],
        status=PENDING_PAYMENT,
        payment_status="PENDING",
        total_paise=placed.total_paise,
    )


def _order_problem(
    cart: Cart,
    max_total_paise: int,
    order: StoreOrder,
    *,
    status: str,
    payment_status: str,
    total_paise: int,
) -> str | None:
    """Why the store's order is not the approved one, or None if it is."""
    wanted: Counter[str] = Counter()
    for line in cart.lines:
        wanted[line.sku_id] += line.qty
    if order.status != status or order.payment_status != payment_status:
        return "order_status_differs"
    if order.payment_method != UPI:
        return "payment_method_differs"
    if dict(order.items) != dict(wanted):
        return "items_differ"
    if order.address_id != cart.address_id or order.address_text != cart.address_text:
        return "address_differs"
    if order.total_paise != total_paise:
        return "total_differs"
    if not 0 < order.total_paise <= max_total_paise:
        return "over_ceiling"
    return None
