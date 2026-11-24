"""The gate process: MCP at /mcp and the webhook's /internal API, in one app on 127.0.0.1:8002.

Run it with `python -m ordr.gate.app`, one process only. MCP and /internal share ONE Gate and ONE
Actions, because the per-account locks live there. A second process or worker would have its own
locks and break the "one order at a time" rule.

Rules kept here (CLAUDE.md 2, 3, 6, 11):
- Every /internal call needs `x-ordr-internal`, checked in constant time before anything else is
  read. The number of a press comes from `x-ordr-number`, never from the body.
- Message text is written by `messages.py` from the outbox payload, at the moment it is fetched.
- A payment, kid card, read-back or replace-cart row whose mandate has moved on is never sent.
- Nothing logged holds a number, token, safe-phrase or message text.
"""

import asyncio
import hmac
import json
import logging
import sqlite3
import sys
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from ordr.config import Settings
from ordr.contracts import Role, State
from ordr.gate import db as gdb
from ordr.gate.actions import Actions
from ordr.gate.binding import WeakServerKey
from ordr.gate.messages import LANGS, MAX_SAFE_PHRASE, MissingTemplate, render
from ordr.gate.server import build_actions, mcp, set_gate
from ordr.gate.service import IST, Gate, WeakInternalToken
from ordr.gate.store_client import StoreClient, StoreUnavailable
from ordr.gate.upi import SimulatedUpi

log = logging.getLogger("ordr.gate")

HOST = "127.0.0.1"  # never anything else. Port 8002 must not be tunnelled
PORT = 8002
SWEEP_SECONDS = 5.0
DEMO_ACCOUNT = "demo-instamart-account"
MAX_NUMBER_DIGITS = 20
OUTBOX_BATCH = 50
OUTBOX_SCAN = 200
LOG_ROWS = 200

# Kinds whose row is only true while the mandate is in this state. Anything else is dropped.
FRESH_STATE: dict[str, State] = {
    "readback": State.PREPARED,
    "kid_card": State.PARENT_CONFIRMED,
    "replace_cart": State.WAITING_FOR_CART,
    "payment": State.PLACED_PENDING_PAYMENT,
}


class StartupError(Exception):
    """The gate must not start. The message names settings, never their values."""


# ----- start-up checks ---------------------------------------------------------------------


def _digits_only(value: str) -> bool:
    return value.isascii() and value.isdigit() and len(value) <= MAX_NUMBER_DIGITS


def check_settings(settings: Settings) -> None:
    """Refuse to start if a needed key is empty or a setting is unusable."""
    needed = {
        "SERVER_KEY": settings.server_key,
        "INTERNAL_TOKEN": settings.internal_token,
        "PARENT_NUMBER": settings.parent_number,
        "KID_NUMBER": settings.kid_number,
        "SAFE_PHRASE": settings.safe_phrase,
    }
    empty = [name for name, value in needed.items() if not value.strip()]
    if empty:
        raise StartupError(f"these settings are empty: {', '.join(empty)}")
    parent, kid = settings.parent_number.strip(), settings.kid_number.strip()
    if not (_digits_only(parent) and _digits_only(kid)):
        raise StartupError("PARENT_NUMBER and KID_NUMBER must be digits only")
    if parent == kid:
        raise StartupError("PARENT_NUMBER and KID_NUMBER must be different")
    if len(" ".join(settings.safe_phrase.split())) > MAX_SAFE_PHRASE:
        raise StartupError(f"SAFE_PHRASE must be at most {MAX_SAFE_PHRASE} characters")
    if settings.parent_lang not in LANGS:
        raise StartupError("PARENT_LANG must be kn or en")
    if settings.server_key.strip() == settings.internal_token.strip():
        raise StartupError("SERVER_KEY and INTERNAL_TOKEN must be different values")


def _numbers_changed(settings: Settings) -> StartupError:
    return StartupError(
        "PARENT_NUMBER or KID_NUMBER differs from the household saved in "
        f"{settings.db_path}. To start again, stop the gate and delete that file "
        "(and its -wal and -shm files, if any)."
    )


def check_household(db: gdb.Database, settings: Settings) -> bool:
    """True if the demo household exists. Raises if it is not this .env's household."""
    rows = db.conn().execute("SELECT parent_number, kid_number FROM households").fetchall()
    if not rows:
        return False
    same = (settings.parent_number.strip(), settings.kid_number.strip())
    if len(rows) != 1 or (rows[0]["parent_number"], rows[0]["kid_number"]) != same:
        raise _numbers_changed(settings)
    return True


async def ensure_household(gate: Gate, settings: Settings) -> None:
    """Create the demo household once, with the store's own first saved address, as given."""
    if check_household(gate.db, settings):
        return
    try:
        addresses = await gate.store.get_addresses()
    except StoreUnavailable as exc:
        raise StartupError(f"cannot read the store's saved addresses: {exc}") from exc
    if not addresses:
        raise StartupError("the store has no saved address")
    address_id, address_text = addresses[0]
    try:
        with gate.db.tx() as conn:
            gdb.create_household(
                conn,
                parent_number=settings.parent_number.strip(),
                kid_number=settings.kid_number.strip(),
                store_account=DEMO_ACCOUNT,
                address_id=address_id,
                address_text=address_text,
            )
    except sqlite3.IntegrityError:
        check_household(gate.db, settings)  # someone else made it first, or it is not ours


# ----- background timer --------------------------------------------------------------------


async def sweep_forever(actions: Actions, seconds: float) -> None:
    """Close late payments and expire old mandates. One failure never stops the timer."""
    while True:
        try:
            await actions.sweep_payments()
        except Exception as exc:
            log.error("payment sweep failed: %s", type(exc).__name__)
        try:
            gdb.expire_due(actions.db, actions.gate.clock())
        except Exception as exc:
            log.error("expiry sweep failed: %s", type(exc).__name__)
        await asyncio.sleep(seconds)


# ----- /internal guard ---------------------------------------------------------------------


class InternalAuth:
    """Pure ASGI middleware. Runs before routing and before any body is read."""

    def __init__(self, app: ASGIApp, token: bytes) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/internal" or path.startswith("/internal/")):
            sent = b""
            for name, value in scope["headers"]:
                if name == b"x-ordr-internal":
                    sent = value.strip()
                    break
            if not hmac.compare_digest(sent, self.token):
                response = JSONResponse({"detail": "forbidden"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ----- outbox ------------------------------------------------------------------------------


def _stale(conn: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> str | None:
    """Why this row must not be sent any more, or None if it is still true."""
    needed = FRESH_STATE.get(kind)
    if needed is None:
        return None
    mandate_id = payload.get("mandate_id")
    row = gdb.get_mandate(conn, mandate_id) if isinstance(mandate_id, str) else None
    if row is None:
        return "no_mandate"
    if row["state"] != needed.value:
        return f"state_{row['state']}"
    if row["stop_requested_at"] is not None:
        return "stop_requested"
    return None


def pull_outbox(actions: Actions, settings: Settings) -> list[dict[str, Any]]:
    """Unsent rows in id order, with final text. Stale and unrenderable rows are closed here."""
    db = actions.db
    if db.conn().execute("SELECT 1 FROM outbox WHERE sent_at IS NULL LIMIT 1").fetchone() is None:
        return []
    now = actions.gate.clock()
    out: list[dict[str, Any]] = []
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT o.id, o.to_role, o.household_id, o.kind, o.payload_json, "
            "h.parent_number, h.kid_number FROM outbox o "
            "JOIN households h ON h.id = o.household_id "
            "WHERE o.sent_at IS NULL ORDER BY o.id LIMIT ?",
            (OUTBOX_SCAN,),
        ).fetchall()
        for row in rows:
            if len(out) >= OUTBOX_BATCH:
                break
            payload = json.loads(row["payload_json"])
            role = Role(row["to_role"])
            lang = settings.parent_lang if role is Role.PARENT else "en"
            mandate_id = payload.get("mandate_id")
            why = _stale(conn, row["kind"], payload)
            if why is None and not isinstance(mandate_id, str):
                why = "no_mandate"  # buttons need it
            message = None
            if why is None:
                try:
                    message = render(
                        row["kind"], role, payload, lang=lang, phrase=settings.safe_phrase
                    )
                except (MissingTemplate, KeyError, TypeError, ValueError) as exc:
                    log.error(
                        "outbox row %s (%s) not rendered: %s",
                        row["id"],
                        row["kind"],
                        type(exc).__name__,
                    )
                    why = "unrenderable"
            if message is None:
                conn.execute(
                    "UPDATE outbox SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
                    (gdb.iso(now), row["id"]),
                )
                gdb.append_audit(
                    conn,
                    row["household_id"],
                    mandate_id if isinstance(mandate_id, str) else None,
                    "outbox_dropped",
                    {"outbox_id": row["id"], "kind": row["kind"], "why": why},
                    now,
                )
                continue
            number = row["parent_number"] if role is Role.PARENT else row["kid_number"]
            out.append(
                {
                    "id": row["id"],
                    "number": number,
                    "kind": row["kind"],
                    "text": message.text,
                    "speak": message.speak,
                    "lang": lang,
                    "buttons": [
                        {"action": action.value, "mandate_id": mandate_id, "label": text}
                        for action, text in message.buttons
                    ],
                }
            )
    return out


# ----- log text ----------------------------------------------------------------------------


def _when(stamp: str) -> str:
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    return moment.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")


def log_text(db: gdb.Database, household: sqlite3.Row) -> str:
    """This household's audit rows as plain text. No numbers: they are cut out as a last guard."""
    rows = (
        db.conn()
        .execute(
            "SELECT ts, event, mandate_id, data_json FROM audit_log WHERE household_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (household["id"], LOG_ROWS),
        )
        .fetchall()
    )
    lines = ["Ordr log, newest last. Times are IST."]
    for row in reversed(rows):
        data = json.loads(row["data_json"])
        parts = [_when(row["ts"]), row["event"], (row["mandate_id"] or "-")[:8]]
        if data.get("from") and data.get("to"):
            parts.append(f"{data['from']} -> {data['to']}")
        for key in ("code", "reason", "why", "kind"):
            if isinstance(data.get(key), str):
                parts.append(f"{key}={data[key][:60]}")
        lines.append("  ".join(parts))
    if len(lines) == 1:
        lines.append("No entries yet.")
    text = "\n".join(lines) + "\n"
    for number in (household["parent_number"], household["kid_number"]):
        text = text.replace(number, "***")
    return text


# ----- routes ------------------------------------------------------------------------------


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message_id: Annotated[str, Field(min_length=1, max_length=200)]
    action: Annotated[str, Field(min_length=1, max_length=50)]
    mandate_id: Annotated[str, Field(min_length=1, max_length=100)]


class AckIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ids: Annotated[list[Annotated[int, Field(ge=1)]], Field(max_length=500)]


router = APIRouter()


@router.post("/action")
async def internal_action(
    body: ActionIn,
    request: Request,
    x_ordr_number: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    actions: Actions = request.app.state.actions
    return await actions.act(x_ordr_number or "", body.message_id, body.action, body.mandate_id)


@router.get("/outbox")
async def internal_outbox(request: Request) -> list[dict[str, Any]]:
    return pull_outbox(request.app.state.actions, request.app.state.settings)


@router.post("/outbox/ack")
async def internal_ack(body: AckIn, request: Request) -> dict[str, bool]:
    actions: Actions = request.app.state.actions
    stamp = gdb.iso(actions.gate.clock())
    with actions.db.tx() as conn:
        conn.executemany(
            "UPDATE outbox SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
            [(stamp, outbox_id) for outbox_id in body.ids],
        )
    return {"ok": True}


@router.get("/log", response_class=PlainTextResponse)
async def internal_log(
    request: Request, x_ordr_number: Annotated[str | None, Header()] = None
) -> Any:
    actions: Actions = request.app.state.actions
    found = gdb.household_for_number(actions.db.conn(), (x_ordr_number or "").strip())
    if found is None:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return PlainTextResponse(log_text(actions.db, found[0]))


# ----- the app -----------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    store: StoreClient | None = None,
    upi: SimulatedUpi | None = None,
    clock: Any = gdb.utc_now,
    sweep_seconds: float = SWEEP_SECONDS,
) -> FastAPI:
    """Build the one Gate and one Actions, then the app. `store`, `upi`, `clock`: for tests."""
    settings = settings or Settings()
    check_settings(settings)
    try:
        actions = build_actions(settings, store=store, upi=upi, clock=clock)
    except (WeakInternalToken, WeakServerKey) as exc:
        raise StartupError(str(exc)) from exc
    try:
        check_household(actions.db, settings)
    except StartupError:
        actions.db.close()
        raise
    gate = actions.gate
    set_gate(gate)
    mcp_app = mcp.http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with mcp_app.lifespan(app):
            await ensure_household(gate, settings)
            timer = asyncio.create_task(sweep_forever(actions, sweep_seconds))
            try:
                yield
            finally:
                timer.cancel()
                with suppress(asyncio.CancelledError):
                    await timer
                set_gate(None)

    app = FastAPI(
        title="Ordr gate", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.actions = actions
    app.state.settings = settings
    app.add_middleware(InternalAuth, token=settings.internal_token.strip().encode("utf-8"))
    app.include_router(router, prefix="/internal")
    app.mount("/", mcp_app)  # last, so /internal is matched first. MCP is at /mcp
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        app = create_app()
        asyncio.run(ensure_household(app.state.actions.gate, app.state.settings))
    except StartupError as exc:
        print(f"The gate cannot start: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    uvicorn.run(app, host=HOST, port=PORT, workers=1, access_log=False, log_level="info")


if __name__ == "__main__":
    main()
