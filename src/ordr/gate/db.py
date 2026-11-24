"""Gate database: schema, atomic state moves, audit rows, outbox rows (TRD section 5, N3).

Rules kept here:
- Every state change is `UPDATE mandates SET state=:new WHERE id=:id AND state=:old`. Only a
  change of exactly one row counts. The audit row and outbox rows go in the same transaction.
- A partial unique index allows one open mandate per store account. Open means any state with
  a way out, plus NEEDS_REVIEW until the kid taps "I have checked it" (`reviewed_at`).
- A mandate starts in WAITING_FOR_CART with no cart. Cart, hash, quote, totals, duplicate flag
  and expiry are set once, on the move to PREPARED. `approval_sig` is set only on the move from
  PARENT_CONFIRMED to APPROVED. Triggers refuse any other change to them.
- Transactions start with BEGIN IMMEDIATE and never stay open across a store call.
- Money is whole paise. Times are UTC, fixed-width ISO text, so text order is time order.
"""

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ordr.contracts import TRANSITIONS, Role, State

# A state is open if the mandate can still move. NEEDS_REVIEW is an end, but blocks the account
# until reviewed_at is set, so the index below adds it separately.
OPEN_STATES: frozenset[State] = frozenset(TRANSITIONS)

# Columns any transition may set along with the state. The rest are fixed at creation or may be
# set by one move only.
SETTABLE = frozenset({"order_id", "paas_id", "reason"})

# Needed on the move to PREPARED, and fixed from then on.
CART_FIELDS = ("cart_json", "cart_hash", "quote_json", "total_paise", "max_total_paise")
SEALED_FIELDS = (*CART_FIELDS, "is_duplicate", "expires_at")

PREPARE_MOVE = (State.WAITING_FOR_CART, State.PREPARED)
APPROVE_MOVE = (State.PARENT_CONFIRMED, State.APPROVED)
SETTABLE_ON: dict[tuple[State, State], frozenset[str]] = {
    PREPARE_MOVE: frozenset(SEALED_FIELDS),
    APPROVE_MOVE: frozenset({"approval_sig"}),
}

GENESIS = "0" * 64


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _open_list() -> str:
    return ", ".join(f"'{s.value}'" for s in sorted(OPEN_STATES))


def _guard_trigger(name: str, columns: Sequence[str], move: tuple[State, State]) -> str:
    """Refuse a change to these columns unless the same update makes this move."""
    changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns)
    return f"""
CREATE TRIGGER IF NOT EXISTS {name} BEFORE UPDATE ON mandates
WHEN ({changed}) AND NOT (OLD.state = '{move[0].value}' AND NEW.state = '{move[1].value}')
BEGIN SELECT RAISE(ABORT, '{name}: column is fixed'); END;
"""


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS households (
    id TEXT PRIMARY KEY,
    parent_number TEXT NOT NULL UNIQUE,
    kid_number TEXT NOT NULL UNIQUE,
    store_account TEXT NOT NULL,
    address_id TEXT NOT NULL,
    address_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mandates (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    store_account TEXT NOT NULL,
    state TEXT NOT NULL,
    cart_json TEXT,
    cart_hash TEXT,
    quote_json TEXT,
    total_paise INTEGER,
    max_total_paise INTEGER,
    nonce TEXT NOT NULL UNIQUE,
    approval_sig TEXT,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    order_id TEXT,
    paas_id TEXT,
    reason TEXT,
    reviewed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_mandate ON mandates(store_account)
    WHERE state IN ({_open_list()})
       OR (state = 'NEEDS_REVIEW' AND reviewed_at IS NULL);
{_guard_trigger("seal_cart", SEALED_FIELDS, PREPARE_MOVE)}
{_guard_trigger("seal_approval", ("approval_sig",), APPROVE_MOVE)}
CREATE TABLE IF NOT EXISTS search_results (
    household_id TEXT NOT NULL REFERENCES households(id),
    sku_id TEXT NOT NULL,
    spin_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (household_id, sku_id, spin_id)
);
CREATE TABLE IF NOT EXISTS defaults (
    household_id TEXT NOT NULL REFERENCES households(id),
    phrase TEXT NOT NULL,
    sku_id TEXT NOT NULL,
    spin_id TEXT NOT NULL,
    pack TEXT NOT NULL,
    qty INTEGER NOT NULL,
    last_confirmed TEXT NOT NULL,
    PRIMARY KEY (household_id, phrase)
);
CREATE TABLE IF NOT EXISTS actions_seen (
    message_id TEXT PRIMARY KEY,
    code TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_role TEXT NOT NULL,
    household_id TEXT NOT NULL REFERENCES households(id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    household_id TEXT NOT NULL,
    mandate_id TEXT,
    event TEXT NOT NULL,
    data_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
"""


class IllegalTransition(Exception):
    """The pair is not in TRANSITIONS. A bug in the caller, never a race."""


class OrderAlreadyOpen(Exception):
    """This store account already has an open mandate (error code `order_already_open`)."""


@dataclass(frozen=True)
class OutboxMsg:
    """A message for the webhook to send. Text is written later by code from `kind`."""

    to_role: Role
    kind: str
    payload: Mapping[str, Any]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Database:
    """One SQLite file. Each thread gets its own connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._guard = threading.Lock()
        self.conn().executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._guard:
                self._all.append(conn)
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction. Commits on exit, rolls back on any error. Keep store calls out."""
        conn = self.conn()
        if conn.in_transaction:
            raise RuntimeError("nested transaction")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def close(self) -> None:
        with self._guard:
            for conn in self._all:
                conn.close()
            self._all.clear()
        self._local = threading.local()


# ----- audit log and outbox ----------------------------------------------------------------


def _row_hash(
    prev: str, ts: str, household_id: str, mandate_id: str | None, event: str, data: str
) -> str:
    material = _json([prev, ts, household_id, mandate_id, event, data])
    return hashlib.sha256(material.encode()).hexdigest()


def append_audit(
    conn: sqlite3.Connection,
    household_id: str,
    mandate_id: str | None,
    event: str,
    data: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Add a hash-chained row. Call inside a transaction, so the chain has no gaps."""
    assert conn.in_transaction, "audit rows are written inside the same transaction as the change"
    last = conn.execute("SELECT row_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    prev = last["row_hash"] if last else GENESIS
    ts = iso(now or utc_now())
    data_json = _json(data or {})
    conn.execute(
        "INSERT INTO audit_log (ts, household_id, mandate_id, event, data_json, prev_hash, "
        "row_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            household_id,
            mandate_id,
            event,
            data_json,
            prev,
            _row_hash(prev, ts, household_id, mandate_id, event, data_json),
        ),
    )


def verify_chain(conn: sqlite3.Connection) -> bool:
    """True if every row links to the one before and hashes to what it says."""
    prev = GENESIS
    for row in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
        expected = _row_hash(
            prev, row["ts"], row["household_id"], row["mandate_id"], row["event"], row["data_json"]
        )
        if row["prev_hash"] != prev or row["row_hash"] != expected:
            return False
        prev = row["row_hash"]
    return True


def enqueue(conn: sqlite3.Connection, household_id: str, msg: OutboxMsg) -> None:
    conn.execute(
        "INSERT INTO outbox (to_role, household_id, kind, payload_json) VALUES (?, ?, ?, ?)",
        (msg.to_role.value, household_id, msg.kind, _json(msg.payload)),
    )


# ----- households --------------------------------------------------------------------------


def create_household(
    conn: sqlite3.Connection,
    *,
    parent_number: str,
    kid_number: str,
    store_account: str,
    address_id: str,
    address_text: str,
) -> str:
    household_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO households VALUES (?, ?, ?, ?, ?, ?)",
        (household_id, parent_number, kid_number, store_account, address_id, address_text),
    )
    return household_id


def household_for_number(conn: sqlite3.Connection, number: str) -> tuple[sqlite3.Row, Role] | None:
    """Who is this number? Household and role, from the registered numbers only."""
    row = conn.execute(
        "SELECT * FROM households WHERE parent_number = ? OR kid_number = ?", (number, number)
    ).fetchone()
    if row is None:
        return None
    return row, Role.PARENT if row["parent_number"] == number else Role.KID


# ----- mandates ----------------------------------------------------------------------------


def create_mandate(
    conn: sqlite3.Connection,
    *,
    household_id: str,
    ttl: timedelta,
    now: datetime | None = None,
) -> str:
    """Reserve the mandate row in WAITING_FOR_CART, with no cart, hash, quote or totals.

    `ttl` sets the first expiry. The move to PREPARED may set a new one. Raises
    OrderAlreadyOpen if the account has an open mandate.
    """
    state = State.WAITING_FOR_CART
    household = conn.execute("SELECT * FROM households WHERE id = ?", (household_id,)).fetchone()
    if household is None:
        raise ValueError("unknown household")
    now = now or utc_now()
    mandate_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO mandates (id, household_id, store_account, state, nonce, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mandate_id,
                household_id,
                household["store_account"],
                state.value,
                secrets.token_hex(16),
                iso(now),
                iso(now + ttl),
            ),
        )
    except sqlite3.IntegrityError as exc:
        if "store_account" in str(exc):
            raise OrderAlreadyOpen(household["store_account"]) from exc
        raise
    append_audit(conn, household_id, mandate_id, "mandate_created", {"state": state.value}, now)
    return mandate_id


def get_mandate(conn: sqlite3.Connection, mandate_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM mandates WHERE id = ?", (mandate_id,)).fetchone()


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _prepare_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Check and convert what the move to PREPARED saves. `expires_at` is an aware datetime."""
    out = dict(fields)
    if missing := [name for name in CART_FIELDS if out.get(name) in (None, "")]:
        raise ValueError(f"PREPARED needs {missing}")
    if not (_is_whole(out["total_paise"]) and _is_whole(out["max_total_paise"])):
        raise ValueError("totals must be whole paise")
    if not 0 < out["total_paise"] <= out["max_total_paise"]:
        raise ValueError("total must be above zero and at or below the ceiling")
    if "is_duplicate" in out:
        out["is_duplicate"] = int(bool(out["is_duplicate"]))
    if "expires_at" in out:
        expires = out["expires_at"]
        if not isinstance(expires, datetime) or expires.tzinfo is None:
            raise ValueError("expires_at must be a datetime with a timezone")
        out["expires_at"] = iso(expires)
    return out


def transition(
    conn: sqlite3.Connection,
    mandate_id: str,
    old: State,
    new: State,
    *,
    event: str,
    data: Mapping[str, Any] | None = None,
    outbox: Sequence[OutboxMsg] = (),
    set_fields: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Move `old` to `new` in one atomic update. True only if this call made the move.

    False means the mandate was no longer in `old` (someone else won). Nothing is written
    then: no audit row, no outbox rows. Call inside `Database.tx()`.

    The move to PREPARED must set every column in CART_FIELDS. Each outbox payload gets this
    `mandate_id`.
    """
    if new not in TRANSITIONS.get(old, frozenset()):
        raise IllegalTransition(f"{old} to {new}")
    assert conn.in_transaction, "state changes run inside a transaction"
    extra = dict(set_fields or {})
    if unknown := set(extra) - SETTABLE - SETTABLE_ON.get((old, new), frozenset()):
        raise ValueError(f"cannot set {sorted(unknown)} on {old} to {new}")
    if (old, new) == PREPARE_MOVE:
        extra = _prepare_fields(extra)
    assignments = ", ".join(["state = ?", *(f"{column} = ?" for column in extra)])
    cursor = conn.execute(
        f"UPDATE mandates SET {assignments} WHERE id = ? AND state = ?",  # noqa: S608
        (new.value, *extra.values(), mandate_id, old.value),
    )
    if cursor.rowcount != 1:
        return False
    household_id = conn.execute(
        "SELECT household_id FROM mandates WHERE id = ?", (mandate_id,)
    ).fetchone()["household_id"]
    append_audit(
        conn,
        household_id,
        mandate_id,
        event,
        {**(data or {}), "from": old.value, "to": new.value},
        now,
    )
    for msg in outbox:
        payload = {**msg.payload, "mandate_id": mandate_id}
        enqueue(conn, household_id, OutboxMsg(msg.to_role, msg.kind, payload))
    return True


def mark_reviewed(conn: sqlite3.Connection, mandate_id: str, now: datetime | None = None) -> bool:
    """The kid tapped "I have checked it". Unblocks the account. True if this call did it."""
    now = now or utc_now()
    cursor = conn.execute(
        "UPDATE mandates SET reviewed_at = ? "
        "WHERE id = ? AND state = 'NEEDS_REVIEW' AND reviewed_at IS NULL",
        (iso(now), mandate_id),
    )
    if cursor.rowcount != 1:
        return False
    row = get_mandate(conn, mandate_id)
    assert row is not None
    append_audit(conn, row["household_id"], mandate_id, "reviewed", {}, now)
    return True


# ----- actions seen ------------------------------------------------------------------------


def claim_action(conn: sqlite3.Connection, message_id: str) -> bool:
    """True the first time a message ID is seen, False for every repeat."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO actions_seen (message_id) VALUES (?)", (message_id,)
    )
    return cursor.rowcount == 1


def set_action_code(conn: sqlite3.Connection, message_id: str, code: str) -> None:
    conn.execute("UPDATE actions_seen SET code = ? WHERE message_id = ?", (code, message_id))


def action_code(conn: sqlite3.Connection, message_id: str) -> str | None:
    row = conn.execute(
        "SELECT code FROM actions_seen WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row["code"] if row else None


# ----- restart and expiry ------------------------------------------------------------------


def recover_on_start(db: Database, now: datetime | None = None) -> list[tuple[str, State]]:
    """Run once at start. APPROVED to EXPIRED. COMMITTING to NEEDS_REVIEW, never a retry."""
    plan = {State.APPROVED: State.EXPIRED, State.COMMITTING: State.NEEDS_REVIEW}
    done: list[tuple[str, State]] = []
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT id, state FROM mandates WHERE state IN ('APPROVED', 'COMMITTING')"
        ).fetchall()
        for row in rows:
            old, new = State(row["state"]), plan[State(row["state"])]
            if new is State.EXPIRED:
                outbox = [OutboxMsg(Role.KID, "expired", {})]
            else:
                outbox = [
                    OutboxMsg(Role.KID, "needs_review", {}),
                    OutboxMsg(Role.PARENT, "needs_review", {}),
                ]
            if transition(
                conn,
                row["id"],
                old,
                new,
                event="restart_recovery",
                set_fields={"reason": "restart"},
                outbox=outbox,
                now=now,
            ):
                done.append((row["id"], new))
    return done


def expire_due(db: Database, now: datetime | None = None) -> list[str]:
    """Expire mandates past `expires_at` that have not reached COMMITTING."""
    now = now or utc_now()
    expirable = [s for s in State if State.EXPIRED in TRANSITIONS.get(s, frozenset())]
    marks = ", ".join("?" for _ in expirable)
    expired: list[str] = []
    with db.tx() as conn:
        rows = conn.execute(
            f"SELECT id, state FROM mandates WHERE state IN ({marks}) AND expires_at <= ?",  # noqa: S608
            (*[s.value for s in expirable], iso(now)),
        ).fetchall()
        for row in rows:
            old = State(row["state"])
            outbox = [OutboxMsg(Role.PARENT, "expired", {})]
            if old not in (State.PREPARED, State.WAITING_FOR_CART):
                outbox.append(OutboxMsg(Role.KID, "expired", {}))
            if transition(
                conn, row["id"], old, State.EXPIRED, event="expired", outbox=outbox, now=now
            ):
                expired.append(row["id"])
    return expired
