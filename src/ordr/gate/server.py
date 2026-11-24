"""Ordr MCP server. Exactly five tools. No checkout, ever.

Served by `app.py`, which also adds `/internal`. Run that, not this file.

The household comes from the webhook's headers (`x-ordr-internal`, `x-ordr-number`), never from
an argument. The work is in `service.py`.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from pydantic import BaseModel, ConfigDict, Field

from ordr.config import Settings, load_policy
from ordr.gate import db as gdb
from ordr.gate.actions import Actions
from ordr.gate.db import Database, recover_on_start
from ordr.gate.service import Caller, Gate, GateError
from ordr.gate.store_client import StoreClient
from ordr.gate.upi import SimulatedUpi

mcp = FastMCP("ordr")

_gate: Gate | None = None


class OrderItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: str
    spin_id: str
    qty: Annotated[int, Field(gt=0)]
    heard_as: str


def build_gate(
    settings: Settings | None = None,
    *,
    store: StoreClient | None = None,
    clock: Callable[[], datetime] = gdb.utc_now,
) -> Gate:
    """Gate from .env and policy.json. Raises if INTERNAL_TOKEN is empty or too short.

    `store` and `clock` are for tests. The app builds exactly one Gate (see `app.py`).
    """
    settings = settings or Settings()
    return Gate(
        Database(settings.db_path),
        store or StoreClient(settings.store_mcp_url),
        load_policy(settings.policy_path),
        settings.internal_token,
        clock,
    )


def build_actions(
    settings: Settings | None = None,
    *,
    store: StoreClient | None = None,
    upi: SimulatedUpi | None = None,
    clock: Callable[[], datetime] = gdb.utc_now,
) -> Actions:
    """Gate plus button presses. Call once per process: it also cleans up after a restart.

    Raises if INTERNAL_TOKEN or SERVER_KEY is empty or too short.
    """
    settings = settings or Settings()
    gate = build_gate(settings, store=store, clock=clock)
    actions = Actions(
        gate, upi or SimulatedUpi.for_store(settings.store_mcp_url), settings.server_key
    )
    recover_on_start(gate.db)
    return actions


def set_gate(gate: Gate | None) -> None:
    global _gate
    _gate = gate


def get_gate() -> Gate:
    """The one Gate of this process, set by `app.py` at start. Never built per request."""
    if _gate is None:
        raise RuntimeError("the gate has not started")
    return _gate


def _caller() -> tuple[Gate, Caller]:
    headers = get_http_headers()
    gate = get_gate()
    return gate, gate.authenticate(headers.get("x-ordr-internal"), headers.get("x-ordr-number"))


def _refuse(exc: GateError) -> ToolError:
    return ToolError(exc.code)


@mcp.tool
async def search_products(query: str) -> dict:
    """Search the store. Marks the household's usual item."""
    try:
        gate, caller = _caller()
        return await gate.search_products(caller, query)
    except GateError as exc:
        raise _refuse(exc) from exc


@mcp.tool
async def prepare_order(items: Annotated[list[OrderItemIn], Field(min_length=1)]) -> dict:
    """Build the cart. Ordr sends the read-back to the parent."""
    try:
        gate, caller = _caller()
        return await gate.prepare_order(caller, [item.model_dump() for item in items])
    except GateError as exc:
        raise _refuse(exc) from exc


@mcp.tool
async def get_order_status(mandate_id: str) -> dict:
    """Status of this household's order."""
    try:
        gate, caller = _caller()
        return await gate.get_order_status(caller, mandate_id)
    except GateError as exc:
        raise _refuse(exc) from exc


@mcp.tool
async def abort_order(mandate_id: str) -> dict:
    """Stop this household's order. Always safe."""
    try:
        gate, caller = _caller()
        return await gate.abort_order(caller, mandate_id)
    except GateError as exc:
        raise _refuse(exc) from exc


@mcp.tool
async def get_audit_log(limit: Annotated[int, Field(gt=0, le=100)] = 20) -> dict[str, Any]:
    """Recent log entries for this household."""
    try:
        gate, caller = _caller()
        return await gate.get_audit_log(caller, limit)
    except GateError as exc:
        raise _refuse(exc) from exc
