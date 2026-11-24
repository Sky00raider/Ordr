"""Guards for the rules in CLAUDE.md. If one fails, fix the code, not the test."""

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from pydantic import ValidationError

from ordr.bot.app import app
from ordr.config import Policy, load_policy
from ordr.contracts import ACTION_ROLES, DUPLICATE_LABELS, LABELS, TRANSITIONS, Action, State
from ordr.gate.server import OrderItemIn
from ordr.gate.server import mcp as gate
from ordr.store.server import mcp as store

ASSISTANT_TOOLS = {
    "search_products": {"query"},
    "prepare_order": {"items"},
    "get_order_status": {"mandate_id"},
    "abort_order": {"mandate_id"},
    "get_audit_log": {"limit"},
}


async def _inputs(server) -> dict[str, set[str]]:
    async with Client(server) as client:
        tools = await client.list_tools()
    out = {}
    for tool in tools:
        dumped = tool.model_dump(by_alias=True)
        schema = dumped.get("inputSchema") or dumped["input_schema"]
        out[tool.name] = set(schema.get("properties", {}))
    return out


async def test_assistant_gets_only_five_tools_with_exact_inputs():
    assert await _inputs(gate) == ASSISTANT_TOOLS


def test_order_item_takes_no_price_total_or_address():
    good = {"sku_id": "a", "spin_id": "b", "qty": 1, "heard_as": "haalu"}
    assert set(OrderItemIn.model_fields) == set(good)
    for extra in ("price", "total", "address_id"):
        with pytest.raises(ValidationError):
            OrderItemIn.model_validate({**good, extra: 1})


async def test_store_checkout_takes_no_items_and_there_is_no_cancel():
    tools = await _inputs(store)
    assert "items" not in tools["checkout"]
    assert not [name for name in tools if "cancel" in name]


def test_every_action_has_a_role_and_a_label_that_fits():
    assert set(LABELS) == set(Action) == set(ACTION_ROLES)
    for label in [*LABELS.values(), *DUPLICATE_LABELS.values()]:
        assert len(label) <= 20


def test_order_can_only_be_placed_after_parent_confirm_and_kid_approve():
    def sources(target: State) -> list[State]:
        return [s for s, following in TRANSITIONS.items() if target in following]

    assert sources(State.COMMITTING) == [State.APPROVED]
    assert sources(State.APPROVED) == [State.PARENT_CONFIRMED]
    assert sources(State.PARENT_CONFIRMED) == [State.PREPARED, State.HANDOFF_WARNED]


def test_policy_is_default_deny_and_rejects_typos():
    policy = load_policy()
    assert policy.auto_approve is False
    assert policy.allowed_payment == ["UPI"]
    with pytest.raises(ValidationError):
        Policy.model_validate({**policy.model_dump(), "per_order_cap": 1})


def test_webhook_refuses_unsigned_posts():
    client = TestClient(app)
    assert client.get("/health").json() == {"ok": True}
    assert client.post("/webhook").status_code == 403
