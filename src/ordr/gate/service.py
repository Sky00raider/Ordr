"""The five assistant tools, as plain async methods (TRD section 6, N5).

The household always comes from `authenticate`, which reads the webhook's two headers. No method
takes a household, address, total or approval from the model.

Rules kept here (CLAUDE.md 1, 4, 5, 6, 7, 8):
- Item IDs must come from a search by this household in the last `search_result_ttl_minutes`.
- The mandate row is reserved before the store cart is touched. Every failure after that moves it
  to ABORTED, so the account is never stuck.
- The kid's cart is never replaced without their tap. "Ours" means the live cart hash equals the
  hash of the latest mandate that has one. Anything else is treated as the kid's.
- Caps and the price ceiling are checked on `max_total`, which the gate computes from the cart
  it read back, never from the model.
- No store call runs inside a database transaction. One asyncio lock per store account covers
  prepare and fill.
- Nothing here calls `checkout`.
"""

import asyncio
import hmac
import json
import sqlite3
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from ordr.config import Policy
from ordr.contracts import Cart, Role, State
from ordr.gate import db as gdb
from ordr.gate.binding import canonical_json, cart_hash
from ordr.gate.db import Database, OutboxMsg, append_audit, enqueue, transition
from ordr.gate.store_client import StoreClient, StoreProduct, StoreUnavailable

IST = timezone(timedelta(hours=5, minutes=30))
MIN_TOKEN_BYTES = 32
MAX_QUERY_CHARS = 100

# States where abort_order may stop the order. After Approve it is too late to stop.
ABORTABLE = frozenset(
    {State.WAITING_FOR_CART, State.PREPARED, State.PARENT_CONFIRMED, State.HANDOFF_WARNED}
)
# A kid card exists only once the parent has confirmed.
KID_HAS_CARD = frozenset({State.PARENT_CONFIRMED, State.HANDOFF_WARNED})
# Counted against the monthly cap (PRD section 6, "Caps").
MONTH_STATES = (
    State.PLACED_PENDING_PAYMENT,
    State.PAID,
    State.COMMITTING,
    State.APPROVED,
    State.PARENT_CONFIRMED,
    State.NEEDS_REVIEW,
)
# Possible earlier orders for the duplicate warning.
DUPLICATE_STATES = (
    State.PLACED_PENDING_PAYMENT,
    State.PAID,
    State.NEEDS_REVIEW,
    State.HANDED_OFF,
)


class GateError(Exception):
    """A short error code for the assistant (TRD section 6). `reason` goes to the log only."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason or code


class AccessDenied(GateError):
    """Bad internal token, unknown number, or not the parent. Looks like `not_found`."""

    def __init__(self) -> None:
        super().__init__("not_found", "access_denied")


class WeakInternalToken(Exception):
    """INTERNAL_TOKEN is empty or shorter than 32 bytes. The gate does not start."""


@dataclass(frozen=True)
class Caller:
    """Who is calling. Built by `Gate.authenticate`, never from a tool argument."""

    household_id: str


def _marks(states: Sequence[State]) -> str:
    return ", ".join("?" for _ in states)


def _values(states: Sequence[State]) -> list[str]:
    return [s.value for s in states]


class Gate:
    def __init__(
        self,
        db: Database,
        store: StoreClient,
        policy: Policy,
        internal_token: str,
        clock: Callable[[], datetime] = gdb.utc_now,
    ) -> None:
        token = internal_token.strip().encode("utf-8")
        if len(token) < MIN_TOKEN_BYTES:
            raise WeakInternalToken(f"INTERNAL_TOKEN must be at least {MIN_TOKEN_BYTES} bytes")
        self.db = db
        self.store = store
        self.policy = policy
        self._token = token
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        # Set by Actions to sweep_payments, so an ignored payment does not block a new order.
        self.before_prepare: Callable[[], Awaitable[Any]] | None = None

    # ----- caller ----------------------------------------------------------------------

    def authenticate(self, token: str | None, number: str | None) -> Caller:
        """Constant-time token check, then the parent's registered number. Nothing else passes."""
        sent = (token or "").strip().encode("utf-8", errors="replace")
        token_ok = hmac.compare_digest(sent, self._token)
        found = gdb.household_for_number(self.db.conn(), (number or "").strip()) if number else None
        if not token_ok or found is None or found[1] is not Role.PARENT:
            raise AccessDenied
        return Caller(household_id=found[0]["id"])

    def account_lock(self, store_account: str) -> asyncio.Lock:
        return self._locks.setdefault(store_account, asyncio.Lock())

    def _household(self, conn: sqlite3.Connection, caller: Caller) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM households WHERE id = ?", (caller.household_id,)
        ).fetchone()
        if row is None:
            raise AccessDenied
        return row

    def _owned(self, conn: sqlite3.Connection, caller: Caller, mandate_id: str) -> sqlite3.Row:
        row = gdb.get_mandate(conn, mandate_id) if isinstance(mandate_id, str) else None
        if row is None or row["household_id"] != caller.household_id:
            raise GateError("not_found")
        return row

    # ----- search_products -------------------------------------------------------------

    async def search_products(self, caller: Caller, query: str) -> dict[str, Any]:
        query = " ".join(query.split())
        if not query or len(query) > MAX_QUERY_CHARS:
            raise GateError("not_found", "bad_query")
        household = self._household(self.db.conn(), caller)
        try:
            products = await self.store.search_products(household["address_id"], query)
        except StoreUnavailable as exc:
            raise GateError("store_error", str(exc)) from exc
        now = gdb.iso(self.clock())
        out = []
        with self.db.tx() as conn:
            for p in products:
                conn.execute(
                    "INSERT INTO search_results (household_id, sku_id, spin_id, payload_json, "
                    "seen_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (household_id, sku_id, spin_id) "
                    "DO UPDATE SET payload_json = excluded.payload_json, "
                    "seen_at = excluded.seen_at",
                    (household["id"], p.sku_id, p.spin_id, _dump(_product_dict(p)), now),
                )
                usual = conn.execute(
                    "SELECT 1 FROM defaults WHERE household_id = ? AND sku_id = ? AND spin_id = ?",
                    (household["id"], p.sku_id, p.spin_id),
                ).fetchone()
                out.append({**_product_dict(p), "household_default": usual is not None})
        return {"products": out}

    # ----- prepare_order ---------------------------------------------------------------

    async def prepare_order(self, caller: Caller, items: Sequence[Mapping[str, Any]]) -> dict:
        request = _merge_request(items)
        household = self._household(self.db.conn(), caller)
        if self.before_prepare is not None:
            await self.before_prepare()
        async with self.account_lock(household["store_account"]):
            gdb.expire_due(self.db, self.clock())
            mandate_id = self._reserve(household, request)
            try:
                live = await self.store.get_cart()
                if not self._is_ours(household["store_account"], live):
                    self._ask_kid(household, mandate_id, live)
                    return {"mandate_id": mandate_id, "status": "waiting_for_kid"}
            except StoreUnavailable as exc:
                self._abort(mandate_id, "store_error", str(exc))
                raise GateError("store_error", str(exc)) from exc
            except BaseException:
                self._abort(mandate_id, "internal", "error before fill")
                raise
            return await self._fill(mandate_id, notify=False)

    def _reserve(self, household: sqlite3.Row, request: list[dict]) -> str:
        """Check item IDs, replace an open draft, reserve the row. One transaction."""
        now = self.clock()
        cutoff = gdb.iso(now - timedelta(minutes=self.policy.search_result_ttl_minutes))
        with self.db.tx() as conn:
            for item in request:
                seen = conn.execute(
                    "SELECT 1 FROM search_results WHERE household_id = ? AND sku_id = ? "
                    "AND spin_id = ? AND seen_at >= ?",
                    (household["id"], item["sku_id"], item["spin_id"], cutoff),
                ).fetchone()
                if seen is None:
                    raise GateError("item_not_from_search", f"{item['sku_id']}/{item['spin_id']}")
            blocking = conn.execute(
                f"SELECT id, household_id, state, reviewed_at FROM mandates "  # noqa: S608
                f"WHERE store_account = ? AND (state IN ({_marks(list(gdb.OPEN_STATES))}) "
                "OR (state = 'NEEDS_REVIEW' AND reviewed_at IS NULL))",
                (household["store_account"], *_values(sorted(gdb.OPEN_STATES))),
            ).fetchall()
            for row in blocking:
                state = State(row["state"])
                if state is State.NEEDS_REVIEW:
                    raise GateError("needs_review")
                if state not in (State.WAITING_FOR_CART, State.PREPARED) or (
                    row["household_id"] != household["id"]
                ):
                    raise GateError("order_already_open", state.value)
                if not transition(
                    conn,
                    row["id"],
                    state,
                    State.ABORTED,
                    event="replaced",
                    set_fields={"reason": "replaced by a new order"},
                    now=now,
                ):
                    raise GateError("order_already_open", "lost a race")
            try:
                return gdb.create_mandate(
                    conn,
                    household_id=household["id"],
                    request_json=canonical_json({"items": request}).decode("utf-8"),
                    ttl=timedelta(minutes=self.policy.mandate_ttl_minutes),
                    now=now,
                )
            except gdb.OrderAlreadyOpen as exc:
                raise GateError("order_already_open", "unique index") from exc

    def _is_ours(self, store_account: str, live: Cart) -> bool:
        """An empty cart, or one whose hash is the latest Ordr cart for this account."""
        if not live.lines:
            return True
        row = (
            self.db.conn()
            .execute(
                "SELECT cart_hash FROM mandates WHERE store_account = ? AND cart_hash IS NOT NULL "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (store_account,),
            )
            .fetchone()
        )
        return row is not None and row["cart_hash"] == cart_hash(live, store=self.store.store_id)

    def _ask_kid(self, household: sqlite3.Row, mandate_id: str, live: Cart) -> None:
        """Queue Replace / Not now for the kid and a waiting notice for the parent."""
        now = self.clock()
        with self.db.tx() as conn:
            if gdb.get_mandate(conn, mandate_id)["state"] != State.WAITING_FOR_CART.value:
                return  # stopped or expired while we looked
            ask = {
                "mandate_id": mandate_id,
                "item_count": sum(line.qty for line in live.lines),
                "line_count": len(live.lines),
            }
            enqueue(conn, household["id"], OutboxMsg(Role.KID, "replace_cart", ask))
            enqueue(
                conn,
                household["id"],
                OutboxMsg(Role.PARENT, "waiting_for_kid", {"mandate_id": mandate_id}),
            )
            append_audit(conn, household["id"], mandate_id, "waiting_for_kid", {}, now)

    # ----- fill ------------------------------------------------------------------------

    async def fill_cart(self, mandate_id: str) -> dict[str, str]:
        """For the Replace button. Fills the store cart for a WAITING_FOR_CART mandate.

        On failure the mandate is ABORTED, both people are told, and GateError is raised.
        """
        with self.db.tx() as conn:
            row = gdb.get_mandate(conn, mandate_id)
        if row is None:
            raise GateError("not_found")
        async with self.account_lock(row["store_account"]):
            return await self._fill(mandate_id, notify=True)

    async def _fill(self, mandate_id: str, *, notify: bool) -> dict[str, str]:
        """Caller holds the account lock."""
        with self.db.tx() as conn:
            row = gdb.get_mandate(conn, mandate_id)
            if row is None or row["state"] != State.WAITING_FOR_CART.value:
                raise GateError("not_found", "not waiting for a cart")
            household = conn.execute(
                "SELECT * FROM households WHERE id = ?", (row["household_id"],)
            ).fetchone()
        request = json.loads(row["request_json"])["items"]
        try:
            await self.store.update_cart(
                household["address_id"], [(i["sku_id"], i["spin_id"], i["qty"]) for i in request]
            )
            cart = await self.store.get_cart()
            problem = _compare(request, cart, household)
            if problem:
                raise GateError("not_available", problem)
            self._prepare(row, household, request, cart)
        except GateError as exc:
            self._abort(mandate_id, exc.code, exc.reason, notify)
            raise
        except StoreUnavailable as exc:
            self._abort(mandate_id, "store_error", str(exc), notify)
            raise GateError("store_error", str(exc)) from exc
        except BaseException:
            self._abort(mandate_id, "internal", "unexpected error", False)
            raise
        return {"mandate_id": mandate_id, "status": "prepared"}

    def _prepare(
        self, row: sqlite3.Row, household: sqlite3.Row, request: list[dict], cart: Cart
    ) -> None:
        """Caps, duplicate check, then WAITING_FOR_CART to PREPARED with the read-back queued."""
        now = self.clock()
        max_total = cart.total_paise + self.policy.surge_ceiling_paise
        if max_total > self.policy.per_order_cap_paise:
            raise GateError("over_cap", "per_order")
        digest = cart_hash(cart, store=self.store.store_id)
        with self.db.tx() as conn:
            if self.spent_this_month(conn, row, now) + max_total > self.policy.monthly_cap_paise:
                raise GateError("over_cap", "per_month")
            duplicate = self._is_duplicate(conn, row["store_account"], digest, now)
            cart_json = canonical_json(
                {
                    "items": [[ln.sku_id, ln.spin_id, ln.qty] for ln in cart.lines],
                    "address_id": cart.address_id,
                    "address_text": cart.address_text,
                }
            ).decode("utf-8")
            quote_json = canonical_json(
                {
                    "lines": [
                        {
                            "sku_id": ln.sku_id,
                            "spin_id": ln.spin_id,
                            "qty": ln.qty,
                            "name": ln.name,
                            "pack": ln.pack,
                            "unit_price_paise": ln.unit_price_paise,
                        }
                        for ln in cart.lines
                    ],
                    "fees_paise": cart.fees_paise,
                    "total_paise": cart.total_paise,
                    "max_total_paise": max_total,
                }
            ).decode("utf-8")
            readback = {
                "lines": [
                    {
                        "name": ln.name,
                        "pack": ln.pack,
                        "qty": ln.qty,
                        "unit_price_paise": ln.unit_price_paise,
                    }
                    for ln in cart.lines
                ],
                "fees_paise": cart.fees_paise,
                "total_paise": cart.total_paise,
                "max_total_paise": max_total,
                "address_text": cart.address_text,
                "is_duplicate": duplicate,
            }
            moved = transition(
                conn,
                row["id"],
                State.WAITING_FOR_CART,
                State.PREPARED,
                event="prepared",
                data={"total_paise": cart.total_paise, "is_duplicate": duplicate},
                set_fields={
                    "cart_json": cart_json,
                    "cart_hash": digest,
                    "quote_json": quote_json,
                    "total_paise": cart.total_paise,
                    "max_total_paise": max_total,
                    "is_duplicate": duplicate,
                    "expires_at": now + timedelta(minutes=self.policy.mandate_ttl_minutes),
                },
                outbox=[OutboxMsg(Role.PARENT, "readback", readback)],
                now=now,
            )
            if not moved:
                raise GateError("not_found", "stopped while filling")

    def spent_this_month(self, conn: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> int:
        """Ceilings of this account's orders created in the current IST calendar month."""
        local = now.astimezone(IST)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        total = conn.execute(
            "SELECT COALESCE(SUM(max_total_paise), 0) FROM mandates WHERE store_account = ? "
            f"AND id != ? AND state IN ({_marks(MONTH_STATES)}) "  # noqa: S608
            "AND created_at >= ? AND created_at < ?",
            (
                row["store_account"],
                row["id"],
                *_values(MONTH_STATES),
                gdb.iso(start.astimezone(UTC)),
                gdb.iso(end.astimezone(UTC)),
            ),
        ).fetchone()[0]
        return int(total)

    def _is_duplicate(
        self, conn: sqlite3.Connection, store_account: str, digest: str, now: datetime
    ) -> bool:
        """Same items and address as a recent possible order. Warns, never blocks."""
        window = self.policy.duplicate_window_minutes
        if window == 0:
            return False
        found = conn.execute(
            "SELECT 1 FROM mandates WHERE store_account = ? AND cart_hash = ? "  # noqa: S608
            f"AND state IN ({_marks(DUPLICATE_STATES)}) AND created_at >= ? LIMIT 1",
            (
                store_account,
                digest,
                *_values(DUPLICATE_STATES),
                gdb.iso(now - timedelta(minutes=window)),
            ),
        ).fetchone()
        return found is not None

    def _abort(self, mandate_id: str, code: str, reason: str, notify: bool = False) -> None:
        """WAITING_FOR_CART to ABORTED, so the account is free. Does nothing if it moved on."""
        outbox = (
            [
                OutboxMsg(Role.PARENT, "order_failed", {"code": code}),
                OutboxMsg(Role.KID, "order_failed", {"code": code}),
            ]
            if notify
            else []
        )
        with self.db.tx() as conn:
            transition(
                conn,
                mandate_id,
                State.WAITING_FOR_CART,
                State.ABORTED,
                event="aborted",
                data={"code": code, "reason": reason},
                set_fields={"reason": reason[:200]},
                outbox=outbox,
                now=self.clock(),
            )

    # ----- the other three tools -------------------------------------------------------

    async def get_order_status(self, caller: Caller, mandate_id: str) -> dict[str, str]:
        with self.db.tx() as conn:
            row = self._owned(conn, caller, mandate_id)
        return {"mandate_id": row["id"], "state": row["state"]}

    async def abort_order(self, caller: Caller, mandate_id: str) -> dict[str, str]:
        with self.db.tx() as conn:
            row = self._owned(conn, caller, mandate_id)
            state = State(row["state"])
            if state in ABORTABLE:
                outbox = [OutboxMsg(Role.PARENT, "stopped", {})]
                if state in KID_HAS_CARD:
                    outbox.append(OutboxMsg(Role.KID, "stopped", {}))
                if not transition(
                    conn,
                    mandate_id,
                    state,
                    State.ABORTED,
                    event="aborted",
                    data={"by": "parent"},
                    set_fields={"reason": "stopped by parent"},
                    outbox=outbox,
                    now=self.clock(),
                ):
                    raise GateError("order_already_open", "lost a race")
                return {"mandate_id": mandate_id, "state": State.ABORTED.value}
            if state is State.NEEDS_REVIEW and row["reviewed_at"] is None:
                raise GateError("needs_review")
            if state in gdb.OPEN_STATES:
                raise GateError("order_already_open", f"too late: {state.value}")
            return {"mandate_id": mandate_id, "state": state.value}

    async def get_audit_log(self, caller: Caller, limit: int = 20) -> dict[str, list[dict]]:
        limit = max(1, min(int(limit), 100))
        rows = (
            self.db.conn()
            .execute(
                "SELECT ts, event, mandate_id FROM audit_log WHERE household_id = ? "
                "ORDER BY seq DESC LIMIT ?",
                (caller.household_id, limit),
            )
            .fetchall()
        )
        return {"entries": [dict(r) for r in rows]}


# ----- helpers -----------------------------------------------------------------------------


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _product_dict(p: StoreProduct) -> dict[str, Any]:
    return {
        "sku_id": p.sku_id,
        "spin_id": p.spin_id,
        "name": p.name,
        "brand": p.brand,
        "pack": p.pack,
        "price_paise": p.price_paise,
        "in_stock": p.in_stock,
        "max_qty": p.max_qty,
    }


def _merge_request(items: Sequence[Mapping[str, Any]]) -> list[dict]:
    """One line per (sku_id, spin_id), quantities added. Rejects empty or non-whole quantities."""
    if not items:
        raise GateError("not_found", "no items")
    merged: dict[tuple[str, str], dict] = {}
    for item in items:
        sku, spin, qty = item["sku_id"], item["spin_id"], item["qty"]
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise GateError("not_available", "bad quantity")
        line = merged.setdefault(
            (sku, spin), {"sku_id": sku, "spin_id": spin, "qty": 0, "heard_as": []}
        )
        line["qty"] += qty
        if item["heard_as"] not in line["heard_as"]:
            line["heard_as"].append(item["heard_as"])
    return [{**line, "heard_as": "; ".join(line["heard_as"])} for _, line in sorted(merged.items())]


def _compare(request: list[dict], cart: Cart, household: sqlite3.Row) -> str | None:
    """Why the read-back differs from the request, or None if it is exactly the request."""
    if cart.address_id != household["address_id"]:
        return "wrong_address"
    if cart.address_text != household["address_text"]:
        return "wrong_address_text"
    live: Counter[tuple[str, str]] = Counter()
    for line in cart.lines:
        live[(line.sku_id, line.spin_id)] += line.qty
    wanted = {(i["sku_id"], i["spin_id"]): i["qty"] for i in request}
    for key, qty in wanted.items():
        if live.get(key, 0) == 0:
            return "item_dropped"
        if live[key] < qty:
            return "quantity_reduced"
        if live[key] > qty:
            return "quantity_changed"
    if set(live) - set(wanted):
        return "extra_item"
    if not cart.all_in_stock:
        return "out_of_stock"
    if not cart.serviceable:
        return "not_serviceable"
    return None
