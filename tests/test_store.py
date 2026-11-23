"""Dummy store behaviour that the gate relies on (PRD section 7, TRD N2)."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from ordr.contracts import TRANSITIONS, State
from ordr.store.server import STORE, mcp
from ordr.store.state import StoreError

ATTA = {"skuId": "sku_sunrise_atta_5kg", "spinId": "spin_sunrise_atta_5kg", "quantity": 1}
MILK = {"skuId": "sku_nandini_toned_1l", "spinId": "spin_nandini_toned_1l", "quantity": 1}
SALT = {"skuId": "sku_saltmark_salt_1kg", "spinId": "spin_saltmark_salt_1kg", "quantity": 1}
HOME = "addr_home"


@pytest.fixture(autouse=True)
def fresh_store():
    STORE.reset()
    now = [1_000_000.0]
    STORE.clock = lambda: now[0]
    STORE.now = now  # type: ignore[attr-defined]  # tests move time by editing now[0]
    yield STORE


async def call(name: str, **args):
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    assert result.structured_content["success"] is True
    return result.structured_content["data"]


async def fill(*items):
    return await call("update_cart", selectedAddressId=HOME, items=list(items))


def test_waiting_for_cart_state_exists_and_goes_only_to_prepared_aborted_expired():
    assert TRANSITIONS[State.WAITING_FOR_CART] == {State.PREPARED, State.ABORTED, State.EXPIRED}
    assert State.WAITING_FOR_CART not in {s for t in TRANSITIONS.values() for s in t}


async def test_search_gives_two_milks_with_two_pack_sizes_and_stock_fields():
    data = await call("search_products", addressId=HOME, query="milk")
    names = {p["displayName"] for p in data["products"]}
    assert {"Nandini Toned Milk", "Greenmeadow Full Cream Milk"} <= names
    nandini = next(p for p in data["products"] if p["brand"] == "Nandini")
    assert [v["quantityDescription"] for v in nandini["variations"]] == ["500 ml", "1 L"]
    variation = nandini["variations"][1]
    assert {"skuId", "spinId", "maxQuantity", "isInStockAndAvailable", "price"} <= set(variation)
    assert variation["price"] == {"mrp": 56.0, "offerPrice": 55.0}
    assert "nextOffset" in data


async def test_search_needs_a_known_address_and_a_query():
    with pytest.raises(ToolError):
        await call("search_products", addressId="addr_nope", query="milk")
    with pytest.raises(ToolError):
        await call("search_products", addressId=HOME, query="  ")


async def test_update_cart_replaces_the_whole_cart():
    await fill(ATTA, MILK)
    cart = await fill(SALT)
    assert [i["spinId"] for i in cart["items"]] == [SALT["spinId"]]


async def test_update_cart_caps_quantity_and_drops_out_of_stock():
    STORE.switch_out_of_stock(SALT["skuId"])
    cart = await fill({**MILK, "quantity": 99}, SALT)
    assert cart["items"][0]["quantity"] == 10
    assert cart["reducedQuantityItems"][0]["cappedQuantity"] == 10
    assert [i["skuId"] for i in cart["removedOutOfStockItems"]] == [SALT["skuId"]]
    assert [i["skuId"] for i in cart["items"]] == [MILK["skuId"]]


async def test_update_cart_refuses_ids_the_store_never_returned():
    with pytest.raises(ToolError):
        await fill({"skuId": "sku_made_up", "spinId": "spin_made_up", "quantity": 1})
    with pytest.raises(ToolError):
        await fill({"skuId": MILK["skuId"], "spinId": ATTA["spinId"], "quantity": 1})


async def test_demo_cart_is_rs_345():
    cart = await fill(ATTA, MILK)
    assert cart["cartTotalAmount"] == "345.00"
    labels = {row["label"]: row["value"] for row in cart["billBreakdown"]["lineItems"]}
    assert labels == {"Item total": "340.00", "Delivery fee": "0.00", "Handling fee": "5.00"}


async def test_checkout_buys_the_server_side_cart_and_takes_no_items():
    await fill(ATTA, MILK)
    out = await call("checkout", addressId=HOME)
    assert out["status"] == "PENDING_PAYMENT"
    assert out["paymentMethod"] == "UPI"
    assert out["cartTotal"] == 345.0
    assert out["paasId"] and out["upiIntentUrl"].startswith("upi://")
    orders = (await call("get_orders"))["orders"]
    assert [(i["itemId"], i["quantity"]) for i in orders[0]["items"]] == [
        (ATTA["skuId"], 1),
        (MILK["skuId"], 1),
    ]
    assert (await call("get_cart"))["cartAbsent"] is True


async def test_checkout_buys_an_item_another_assistant_added():
    await fill(ATTA, MILK)
    STORE.switch_add_expensive_item()
    out = await call("checkout", addressId=HOME)
    assert out["cartTotal"] == 2344.0
    items = (await call("get_orders"))["orders"][0]["items"]
    assert "Diwali Dry Fruit Gift Hamper 1 pc" in {i["name"] for i in items}


async def test_checkout_delivers_to_the_cart_address_after_a_silent_change():
    await fill(ATTA, MILK)
    STORE.switch_change_address()
    out = await call("checkout", addressId=HOME)
    assert out["addressId"] == "addr_other"


@pytest.mark.parametrize(("fee", "total"), [(18, "363.00"), (250, "595.00")])
async def test_extra_fee_switch_changes_the_bill(fee, total):
    await fill(ATTA, MILK)
    STORE.switch_extra_fee(fee)
    cart = await call("get_cart")
    assert cart["cartTotalAmount"] == total
    assert {"label": "Surge fee", "value": f"{fee}.00"} in cart["billBreakdown"]["lineItems"]


async def test_out_of_stock_after_filling_is_flagged_and_blocks_checkout():
    await fill(ATTA, MILK)
    STORE.switch_out_of_stock(MILK["skuId"])
    cart = await call("get_cart")
    assert [i["isInStockAndAvailable"] for i in cart["items"]] == [True, False]
    assert cart["unserviceableItems"][0]["skuId"] == MILK["skuId"]
    with pytest.raises(ToolError, match="out of stock"):
        await call("checkout", addressId=HOME)
    assert STORE.orders == []


async def test_preload_switch_puts_items_in_the_kids_cart():
    STORE.switch_preload_cart()
    assert len((await call("get_cart"))["items"]) == 2


async def test_minimum_order_is_rs_99():
    cart = await fill(SALT)
    assert "Minimum order" in cart["cartWarning"]["message"]
    with pytest.raises(ToolError, match="Minimum order"):
        await call("checkout", addressId=HOME)


async def test_empty_cart_and_bad_inputs_place_nothing():
    with pytest.raises(ToolError, match="empty"):
        await call("checkout", addressId=HOME)
    await fill(ATTA, MILK)
    with pytest.raises(ToolError):
        await call("checkout", addressId="addr_nope")
    with pytest.raises(ToolError, match="Unsupported"):
        await call("checkout", addressId=HOME, paymentMethod="Bitcoin")
    assert STORE.orders == []


async def test_upi_order_is_pending_until_paid_and_confirmed():
    await fill(ATTA, MILK)
    out = await call("checkout", addressId=HOME)
    ids = {"paasId": out["paasId"], "orderId": out["orderId"]}
    status = await call("check_payment_status", **ids)
    assert (status["status"], status["terminal"]) == ("PENDING", False)
    STORE.pay(out["paasId"])
    status = await call("check_payment_status", **ids)
    assert status["isTerminalSuccess"] is True
    assert "confirmed" not in status
    order = (await call("get_orders"))["orders"][0]
    assert (order["status"], order["paymentStatus"]) == ("PENDING_PAYMENT", "SUCCESS")
    done = await call("confirm_order", orderId=out["orderId"], paasId=out["paasId"])
    assert done["result"] == "success"
    status = await call("check_payment_status", **ids)
    assert status["confirmed"] is True
    order = (await call("get_orders"))["orders"][0]
    assert (order["status"], order["paymentStatus"]) == ("CONFIRMED", "SUCCESS")


async def test_confirm_order_does_not_complete_an_unpaid_order():
    await fill(ATTA, MILK)
    out = await call("checkout", addressId=HOME)
    done = await call("confirm_order", orderId=out["orderId"], paasId=out["paasId"])
    assert done["result"] == "failed"
    order = (await call("get_orders"))["orders"][0]
    assert (order["status"], order["paymentStatus"]) == ("PENDING_PAYMENT", "PENDING")


async def test_a_paid_order_never_expires(fresh_store):
    await fill(ATTA, MILK)
    out = await call("checkout", addressId=HOME)
    STORE.pay(out["paasId"])
    fresh_store.now[0] += 10_000
    status = await call("check_payment_status", paasId=out["paasId"], orderId=out["orderId"])
    assert status["isTerminalSuccess"] is True
    done = await call("confirm_order", orderId=out["orderId"], paasId=out["paasId"])
    assert done["result"] == "success"
    assert (await call("get_orders"))["orders"][0]["status"] == "CONFIRMED"


async def test_cannot_pay_twice_or_after_expiry(fresh_store):
    await fill(ATTA, MILK)
    first = await call("checkout", addressId=HOME)
    STORE.pay(first["paasId"])
    with pytest.raises(StoreError):
        STORE.pay(first["paasId"])
    await fill(ATTA)
    second = await call("checkout", addressId=HOME)
    fresh_store.now[0] += 301
    with pytest.raises(StoreError):
        STORE.pay(second["paasId"])
    with pytest.raises(StoreError):
        STORE.pay("paas_unknown")


async def test_confirm_order_refuses_a_mismatched_pair():
    await fill(ATTA, MILK)
    out = await call("checkout", addressId=HOME)
    with pytest.raises(ToolError):
        await call("confirm_order", orderId="IM-999999", paasId=out["paasId"])
    with pytest.raises(ToolError):
        await call("confirm_order", orderId=out["orderId"], paasId="paas_wrong")


async def test_unpaid_upi_order_fails_after_five_minutes(fresh_store):
    await fill(ATTA, MILK)
    out = await call("checkout", addressId=HOME)
    fresh_store.now[0] += 301
    status = await call("check_payment_status", paasId=out["paasId"], orderId=out["orderId"])
    assert status["isTerminalFailure"] is True
    done = await call("confirm_order", orderId=out["orderId"], paasId=out["paasId"])
    assert done["result"] == "failed"
    assert (
        await call(
            "get_orders",
        )
    )["orders"][0]["status"] == "FAILED"


async def test_probable_duplicate_is_refused_unless_confirmed():
    await fill(ATTA, MILK)
    await call("checkout", addressId=HOME)
    await fill(ATTA, MILK)
    with pytest.raises(ToolError, match="DUPLICATE_ORDER"):
        await call("checkout", addressId=HOME)
    assert len(STORE.orders) == 1
    await call("checkout", addressId=HOME, confirmDuplicate=True)
    assert len(STORE.orders) == 2


async def test_get_orders_filters_active_and_limits_count():
    await fill(ATTA, MILK)
    out = await call("checkout", addressId=HOME)
    await call("confirm_order", orderId=out["orderId"], paasId=out["paasId"])
    await fill(ATTA)
    await call("checkout", addressId=HOME)
    data = await call("get_orders", count=1, activeOnly=True)
    assert len(data["orders"]) == 1 and data["hasMore"] is True


def test_demo_switch_route():
    client = TestClient(mcp.http_app())
    assert client.post("/demo/switch", json={"switch": "extra_fee", "rupees": 18}).json() == {
        "ok": True
    }
    assert STORE.extra_fee_paise == 1800
    assert client.post("/demo/switch", json={"switch": "nope"}).status_code == 400
    assert client.post("/demo/switch", json={"switch": "reset"}).json() == {"ok": True}
    assert STORE.extra_fee_paise == 0


def test_demo_pay_route_marks_payment_success_but_not_the_order():
    STORE.cart = {"spin_sunrise_atta_5kg": 1, "spin_nandini_toned_1l": 1}
    paas_id = STORE.checkout(HOME)["paasId"]
    client = TestClient(mcp.http_app())
    assert client.post("/demo/pay", json={"paasId": "nope"}).status_code == 400
    assert client.post("/demo/pay", json={"paasId": paas_id}).json()["status"] == "SUCCESS"
    assert client.post("/demo/pay", json={"paasId": paas_id}).status_code == 400
    order = STORE.get_orders()["orders"][0]
    assert (order["status"], order["paymentStatus"]) == ("PENDING_PAYMENT", "SUCCESS")
