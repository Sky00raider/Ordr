"""Gate database: atomic moves, one open order per account, restart handling (TRD N3)."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

from ordr.contracts import TRANSITIONS, Role, State
from ordr.gate import db as gdb
from ordr.gate.db import (
    OPEN_STATES,
    Database,
    IllegalTransition,
    OrderAlreadyOpen,
    OutboxMsg,
    transition,
)

TTL = timedelta(minutes=60)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "gate.db")
    yield database
    database.close()


def household(conn, n: int = 1, account: str | None = None) -> str:
    return gdb.create_household(
        conn,
        parent_number=f"p{n}",
        kid_number=f"k{n}",
        store_account=account or f"acct{n}",
        address_id="addr_home",
        address_text="home",
    )


REQUEST = '{"items":[{"heard_as":"milk","qty":1,"sku_id":"s","spin_id":"p"}]}'

CART = {
    "cart_json": "[]",
    "cart_hash": "h",
    "quote_json": "{}",
    "total_paise": 34500,
    "max_total_paise": 37500,
}


def waiting(conn, household_id: str, ttl: timedelta = TTL) -> str:
    return gdb.create_mandate(conn, household_id=household_id, request_json=REQUEST, ttl=ttl)


def mandate(conn, household_id: str, ttl: timedelta = TTL) -> str:
    """A mandate already in PREPARED."""
    mid = waiting(conn, household_id, ttl)
    assert transition(
        conn, mid, State.WAITING_FOR_CART, State.PREPARED, event="prepared", set_fields=CART
    )
    return mid


def state_of(db, mandate_id: str) -> State:
    with db.tx() as conn:
        return State(gdb.get_mandate(conn, mandate_id)["state"])


def count(db, table: str) -> int:
    return db.conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def move(db, mandate_id, old, new, **kw) -> bool:
    with db.tx() as conn:
        return transition(conn, mandate_id, old, new, event=f"to_{new.value}", **kw)


def confirmed_mandate(db) -> str:
    with db.tx() as conn:
        mid = mandate(conn, household(conn))
    assert move(db, mid, State.PREPARED, State.PARENT_CONFIRMED)
    return mid


def test_stop_and_approve_together_have_one_winner(db):
    for round_ in range(40):
        with db.tx() as conn:
            hid = household(conn, round_)
            mid = mandate(conn, hid)
        assert move(db, mid, State.PREPARED, State.PARENT_CONFIRMED)
        audit_before, outbox_before = count(db, "audit_log"), count(db, "outbox")
        gate, results = threading.Barrier(2), {}

        def press(name, target, kind, mid=mid, gate=gate, results=results):
            gate.wait()
            results[name] = move(
                db,
                mid,
                State.PARENT_CONFIRMED,
                target,
                outbox=[OutboxMsg(Role.PARENT, kind, {})],
            )

        threads = [
            threading.Thread(target=press, args=("stop", State.ABORTED, "stopped")),
            threading.Thread(target=press, args=("approve", State.APPROVED, "approved")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results.values()) == [False, True]
        winner = State.ABORTED if results["stop"] else State.APPROVED
        assert state_of(db, mid) is winner
        # The loser wrote nothing: one audit row and one outbox row, both the winner's.
        assert count(db, "audit_log") == audit_before + 1
        assert count(db, "outbox") == outbox_before + 1


def test_a_move_from_the_wrong_state_changes_nothing(db):
    mid = confirmed_mandate(db)
    audit, outbox = count(db, "audit_log"), count(db, "outbox")
    assert not move(db, mid, State.PREPARED, State.ABORTED, outbox=[OutboxMsg(Role.KID, "x", {})])
    assert state_of(db, mid) is State.PARENT_CONFIRMED
    assert (count(db, "audit_log"), count(db, "outbox")) == (audit, outbox)


def test_moves_not_in_transitions_are_refused_even_with_a_matching_state(db):
    mid = confirmed_mandate(db)
    for new in (State.COMMITTING, State.PAID, State.PLACED_PENDING_PAYMENT):
        with pytest.raises(IllegalTransition):
            move(db, mid, State.PARENT_CONFIRMED, new)
    for end in set(State) - set(TRANSITIONS):
        with pytest.raises(IllegalTransition):
            move(db, mid, end, State.PREPARED)
    assert state_of(db, mid) is State.PARENT_CONFIRMED


def test_nothing_reaches_committing_except_from_approved(db):
    mid = confirmed_mandate(db)
    for old in State:
        if old is State.APPROVED:
            continue
        if State.COMMITTING in TRANSITIONS.get(old, frozenset()):
            pytest.fail(f"{old} may go to COMMITTING")
    assert not move(db, mid, State.APPROVED, State.COMMITTING)  # not in APPROVED yet


def test_state_audit_and_outbox_are_one_transaction(db):
    mid = confirmed_mandate(db)
    audit, outbox = count(db, "audit_log"), count(db, "outbox")

    class Boom(Exception):
        pass

    with pytest.raises(Boom), db.tx() as conn:
        assert transition(
            conn,
            mid,
            State.PARENT_CONFIRMED,
            State.APPROVED,
            event="approved",
            outbox=[OutboxMsg(Role.KID, "kid_card", {})],
        )
        raise Boom
    assert state_of(db, mid) is State.PARENT_CONFIRMED
    assert (count(db, "audit_log"), count(db, "outbox")) == (audit, outbox)


def test_bad_outbox_payload_rolls_the_state_change_back(db):
    mid = confirmed_mandate(db)
    with pytest.raises(TypeError):
        move(
            db,
            mid,
            State.PARENT_CONFIRMED,
            State.APPROVED,
            outbox=[OutboxMsg(Role.KID, "k", {"x": {1}})],
        )
    assert state_of(db, mid) is State.PARENT_CONFIRMED


def test_transition_sets_only_allowed_columns(db):
    mid = confirmed_mandate(db)
    with pytest.raises(ValueError):
        move(db, mid, State.PARENT_CONFIRMED, State.APPROVED, set_fields={"total_paise": 1})
    with pytest.raises(ValueError):
        move(db, mid, State.PARENT_CONFIRMED, State.APPROVED, set_fields={"state": "PAID"})
    assert move(db, mid, State.PARENT_CONFIRMED, State.APPROVED, set_fields={"approval_sig": "sig"})
    with db.tx() as conn:
        assert gdb.get_mandate(conn, mid)["approval_sig"] == "sig"


def test_one_open_mandate_per_store_account(db):
    with db.tx() as conn:
        hid = household(conn)
        first = mandate(conn, hid)
    with pytest.raises(OrderAlreadyOpen), db.tx() as conn:
        mandate(conn, hid)
    assert count(db, "mandates") == 1
    assert move(db, first, State.PREPARED, State.ABORTED)
    with db.tx() as conn:
        mandate(conn, hid)  # the old one ended, so a new one is fine


def test_two_households_on_different_accounts_can_each_have_one(db):
    with db.tx() as conn:
        mandate(conn, household(conn, 1))
        mandate(conn, household(conn, 2))


def test_account_is_blocked_by_every_open_state_and_by_unreviewed_needs_review(db):
    with db.tx() as conn:
        hid = household(conn)
        mid = mandate(conn, hid)
    for state in State:
        conn = db.conn()
        conn.execute("UPDATE mandates SET state = ?, reviewed_at = NULL WHERE id = ?", (state, mid))
        blocking = state in OPEN_STATES or state is State.NEEDS_REVIEW
        # Try to open a second mandate for the account and see if the index refuses it.
        # (The first row's state is what is under test, so insert the second directly.)
        try:
            with db.tx() as c:
                c.execute(
                    "INSERT INTO mandates (id, household_id, store_account, state, request_json, "
                    "nonce, created_at, expires_at) VALUES ('second', ?, 'acct1', "
                    "'WAITING_FOR_CART', '{}', 'n2', 't', 't')",
                    (hid,),
                )
                c.execute("DELETE FROM mandates WHERE id = 'second'")
            refused = False
        except Exception:
            refused = True
        assert refused is blocking, state


def test_needs_review_blocks_until_the_kid_has_checked(db):
    with db.tx() as conn:
        hid = household(conn)
        mid = mandate(conn, hid)
    path = [State.PARENT_CONFIRMED, State.APPROVED, State.COMMITTING, State.NEEDS_REVIEW]
    old = State.PREPARED
    for new in path:
        assert move(db, mid, old, new)
        old = new
    with pytest.raises(OrderAlreadyOpen), db.tx() as conn:
        mandate(conn, hid)
    with db.tx() as conn:
        assert gdb.mark_reviewed(conn, mid)
        assert not gdb.mark_reviewed(conn, mid)
    with db.tx() as conn:
        mandate(conn, hid)


def test_a_mandate_always_starts_waiting_with_no_cart(db):
    with db.tx() as conn:
        mid = waiting(conn, household(conn))
        row = gdb.get_mandate(conn, mid)
    assert row["state"] == State.WAITING_FOR_CART
    assert all(row[name] is None for name in (*CART, "approval_sig"))
    with pytest.raises(TypeError), db.tx() as conn:
        gdb.create_mandate(
            conn, household_id="x", request_json=REQUEST, ttl=TTL, state=State.PREPARED
        )


def test_preparing_sets_the_cart_flag_and_expiry(db):
    expires = gdb.utc_now() + timedelta(minutes=30)
    with db.tx() as conn:
        mid = waiting(conn, household(conn))
    fields = {**CART, "is_duplicate": True, "expires_at": expires}
    assert move(db, mid, State.WAITING_FOR_CART, State.PREPARED, set_fields=fields)
    row = gdb.get_mandate(db.conn(), mid)
    assert {name: row[name] for name in CART} == CART
    assert (row["is_duplicate"], row["expires_at"]) == (1, gdb.iso(expires))


@pytest.mark.parametrize("name", CART)
@pytest.mark.parametrize("bad", ["missing", None])
def test_preparing_needs_cart_hash_quote_and_both_totals(db, name, bad):
    with db.tx() as conn:
        mid = waiting(conn, household(conn))
    fields = {k: v for k, v in CART.items() if k != name}
    if bad is None:
        fields[name] = None
    with pytest.raises(ValueError, match="PREPARED needs"):
        move(db, mid, State.WAITING_FOR_CART, State.PREPARED, set_fields=fields)
    assert state_of(db, mid) is State.WAITING_FOR_CART


@pytest.mark.parametrize(
    "bad",
    [
        {"total_paise": 345.0},
        {"total_paise": True},
        {"total_paise": 0},
        {"max_total_paise": 34499},
        {"cart_hash": ""},
        {"expires_at": "2026-12-01"},
        {"expires_at": datetime(2026, 12, 1)},
    ],
)
def test_preparing_refuses_bad_values(db, bad):
    with db.tx() as conn:
        mid = waiting(conn, household(conn))
    with pytest.raises(ValueError):
        move(db, mid, State.WAITING_FOR_CART, State.PREPARED, set_fields={**CART, **bad})
    assert state_of(db, mid) is State.WAITING_FOR_CART


SEALED_VALUES = {**CART, "is_duplicate": True, "expires_at": gdb.utc_now()}
ALL_MOVES = [(old, new) for old, news in TRANSITIONS.items() for new in sorted(news)]


def test_sealed_values_cover_every_sealed_column():
    assert set(SEALED_VALUES) == set(gdb.SEALED_FIELDS)


@pytest.mark.parametrize("old,new", ALL_MOVES)
def test_only_preparing_may_set_the_cart_and_only_approving_the_signature(db, old, new):
    with db.tx() as conn:
        mid = waiting(conn, household(conn))
    for name, value in {**SEALED_VALUES, "approval_sig": "sig"}.items():
        allowed = (
            (State.PARENT_CONFIRMED, State.APPROVED)
            if name == "approval_sig"
            else (State.WAITING_FOR_CART, State.PREPARED)
        )
        if (old, new) == allowed:
            continue
        with pytest.raises(ValueError, match="cannot set"):
            move(db, mid, old, new, set_fields={name: value})


@pytest.mark.parametrize("name", [*gdb.SEALED_FIELDS, "approval_sig"])
def test_sealed_columns_cannot_change_even_by_direct_sql(db, name):
    mid = confirmed_mandate(db)
    conn = db.conn()
    update = f"UPDATE mandates SET {name} = ? WHERE id = ?"  # noqa: S608

    def refused(value):
        with pytest.raises(sqlite3.IntegrityError, match="column is fixed"):
            conn.execute(update, (value, mid))

    refused(9)
    sig = {"approval_sig": "sig"}
    assert move(db, mid, State.PARENT_CONFIRMED, State.APPROVED, set_fields=sig)
    refused(9)
    refused(None)
    assert move(db, mid, State.APPROVED, State.COMMITTING)
    refused(9)


def test_a_waiting_mandate_can_still_expire(db):
    with db.tx() as conn:
        mid = waiting(conn, household(conn), ttl=timedelta(minutes=1))
    assert gdb.expire_due(db, gdb.utc_now() + timedelta(minutes=5)) == [mid]


def test_every_outbox_payload_has_the_mandate_id(db):
    with db.tx() as conn:
        mid = waiting(conn, household(conn))
    sent = [
        OutboxMsg(Role.PARENT, "read_back", {"a": 1}),
        OutboxMsg(Role.KID, "note", {"mandate_id": "someone-elses"}),
    ]
    assert move(db, mid, State.WAITING_FOR_CART, State.PREPARED, set_fields=CART, outbox=sent)
    gdb.expire_due(db, gdb.utc_now() + timedelta(hours=2))
    payloads = [json.loads(r["payload_json"]) for r in db.conn().execute("SELECT * FROM outbox")]
    assert len(payloads) == 3
    assert payloads[0] == {"a": 1, "mandate_id": mid}
    assert all(p["mandate_id"] == mid for p in payloads)


def test_nonce_is_unique_and_random(db):
    with db.tx() as conn:
        a = mandate(conn, household(conn, 1))
        b = mandate(conn, household(conn, 2))
        nonces = {gdb.get_mandate(conn, m)["nonce"] for m in (a, b)}
    assert len(nonces) == 2 and all(len(n) == 32 for n in nonces)


def test_audit_log_is_a_hash_chain_that_detects_edits(db):
    mid = confirmed_mandate(db)
    move(db, mid, State.PARENT_CONFIRMED, State.APPROVED)
    conn = db.conn()
    assert count(db, "audit_log") == 4
    assert gdb.verify_chain(conn)
    conn.execute("UPDATE audit_log SET event = 'edited' WHERE seq = 2")
    assert not gdb.verify_chain(conn)


def test_audit_rows_record_the_move(db):
    mid = confirmed_mandate(db)
    row = db.conn().execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    assert (row["mandate_id"], row["event"]) == (mid, "to_PARENT_CONFIRMED")
    assert '"from":"PREPARED"' in row["data_json"] and '"to":"PARENT_CONFIRMED"' in row["data_json"]


def test_number_lookup_gives_household_and_role(db):
    with db.tx() as conn:
        hid = household(conn)
        parent = gdb.household_for_number(conn, "p1")
        kid = gdb.household_for_number(conn, "k1")
        stranger = gdb.household_for_number(conn, "p9")
    assert (parent[0]["id"], parent[1]) == (hid, Role.PARENT)
    assert kid[1] is Role.KID
    assert stranger is None


def test_numbers_are_unique_per_household(db):
    with db.tx() as conn:
        household(conn, 1)
    with pytest.raises(Exception, match="UNIQUE"), db.tx() as conn:
        gdb.create_household(
            conn,
            parent_number="p1",
            kid_number="other",
            store_account="x",
            address_id="a",
            address_text="t",
        )


def test_a_repeated_message_id_is_claimed_once(db):
    with db.tx() as conn:
        assert gdb.claim_action(conn, "wamid.1")
        gdb.set_action_code(conn, "wamid.1", "ok")
    with db.tx() as conn:
        assert not gdb.claim_action(conn, "wamid.1")
        assert gdb.action_code(conn, "wamid.1") == "ok"
        assert gdb.claim_action(conn, "wamid.2")


def test_restart_expires_approved_and_sends_committing_to_review_never_retries(db):
    with db.tx() as conn:
        approved = mandate(conn, household(conn, 1))
        committing = mandate(conn, household(conn, 2))
        prepared = mandate(conn, household(conn, 3))
    for mid in (approved, committing):
        for old, new in [
            (State.PREPARED, State.PARENT_CONFIRMED),
            (State.PARENT_CONFIRMED, State.APPROVED),
        ]:
            assert move(db, mid, old, new)
    assert move(db, committing, State.APPROVED, State.COMMITTING)

    done = dict(gdb.recover_on_start(db))
    assert done == {approved: State.EXPIRED, committing: State.NEEDS_REVIEW}
    assert state_of(db, approved) is State.EXPIRED
    assert state_of(db, committing) is State.NEEDS_REVIEW
    assert state_of(db, prepared) is State.PREPARED
    assert gdb.recover_on_start(db) == []  # a second start finds nothing to do
    kinds = [r["kind"] for r in db.conn().execute("SELECT kind FROM outbox")]
    assert sorted(kinds) == ["expired", "needs_review", "needs_review"]


def test_expiry_only_touches_mandates_past_their_time(db):
    with db.tx() as conn:
        old = mandate(conn, household(conn, 1), ttl=timedelta(minutes=1))
        fresh = mandate(conn, household(conn, 2))
    later = gdb.utc_now() + timedelta(minutes=5)
    assert gdb.expire_due(db, later) == [old]
    assert state_of(db, old) is State.EXPIRED
    assert state_of(db, fresh) is State.PREPARED
    assert gdb.expire_due(db, later) == []


def test_expiry_never_touches_committing_or_later(db):
    with db.tx() as conn:
        mid = mandate(conn, household(conn), ttl=timedelta(minutes=1))
    old = State.PREPARED
    for new in (State.PARENT_CONFIRMED, State.APPROVED, State.COMMITTING):
        assert move(db, mid, old, new)
        old = new
    assert gdb.expire_due(db, gdb.utc_now() + timedelta(hours=2)) == []
    assert state_of(db, mid) is State.COMMITTING


def test_request_is_saved_at_creation_and_never_changes(db):
    with db.tx() as conn:
        mid = mandate(conn, household(conn))
        assert gdb.get_mandate(conn, mid)["request_json"] == REQUEST
    for state_change in (False, True):
        with pytest.raises(sqlite3.DatabaseError, match="seal_request"), db.tx() as conn:
            sql = "UPDATE mandates SET request_json = '{}'"
            if state_change:
                sql += ", state = 'ABORTED'"
            conn.execute(sql + " WHERE id = ?", (mid,))
    with pytest.raises(ValueError), db.tx() as conn:
        gdb.create_mandate(conn, household_id=household(conn, 2), request_json="", ttl=TTL)
