"""Store adapter: money in whole paise, odd shapes refused (TRD section 5, N5)."""

import inspect
import json

import pytest

from ordr.gate.store_client import (
    StoreClient,
    StoreUnavailable,
    paise,
    parse_cart,
    parse_orders,
    parse_payment_status,
)
from ordr.store.server import STORE, mcp


@pytest.fixture(autouse=True)
def fresh_store():
    STORE.reset()


@pytest.mark.parametrize(
    ("shown", "expected"),
    [("345.00", 34500), (345.0, 34500), (19.99, 1999), (0.1, 10), ("0.30", 30), (7, 700), (0, 0)],
)
def test_money_becomes_exact_paise(shown, expected):
    assert paise(shown) == expected
    assert isinstance(paise(shown), int)


@pytest.mark.parametrize("bad", [0.001, "1.005", -1, "-0.01", "nan", "inf", "abc", True, None, [1]])
def test_money_that_is_not_whole_paise_is_refused(bad):
    with pytest.raises(StoreUnavailable):
        paise(bad)


async def test_get_cart_returns_a_contract_cart_in_paise():
    client = StoreClient(mcp)
    empty = await client.get_cart()
    assert empty.lines == () and empty.total_paise == 0 and empty.all_in_stock
    await client.update_cart(
        "addr_home",
        [
            ("sku_sunrise_atta_5kg", "spin_sunrise_atta_5kg", 1),
            ("sku_nandini_toned_1l", "spin_nandini_toned_1l", 1),
        ],
    )
    cart = await client.get_cart()
    assert cart.total_paise == 34500  # 285 + 55 + handling 5, delivery free
    assert cart.fees_paise == {"Delivery fee": 0, "Handling fee": 500}
    atta = next(line for line in cart.lines if line.sku_id == "sku_sunrise_atta_5kg")
    assert (atta.qty, atta.unit_price_paise, atta.pack) == (1, 28500, "5 kg")
    assert cart.address_id == "addr_home" and cart.all_in_stock and cart.serviceable


async def test_a_cart_under_the_minimum_is_not_serviceable():
    client = StoreClient(mcp)
    await client.update_cart("addr_home", [("sku_nandini_toned_1l", "spin_nandini_toned_1l", 1)])
    assert (await client.get_cart()).serviceable is False


async def test_search_gives_one_product_per_variation_in_paise():
    products = await StoreClient(mcp).search_products("addr_home", "milk")
    one = next(p for p in products if p.sku_id == "sku_nandini_toned_1l")
    assert (one.price_paise, one.pack, one.in_stock, one.max_qty) == (5500, "1 L", True, 10)


async def test_store_errors_become_store_unavailable():
    client = StoreClient(mcp)
    with pytest.raises(StoreUnavailable):
        await client.search_products("addr_nope", "milk")
    with pytest.raises(StoreUnavailable):
        await client.update_cart("addr_home", [("sku_nope", "spin_nope", 1)])


async def test_a_dead_store_url_becomes_store_unavailable():
    with pytest.raises(StoreUnavailable):
        await StoreClient("http://127.0.0.1:9/mcp", timeout=3).get_cart()


def _view() -> dict:
    STORE.update_cart(
        "addr_home",
        [{"skuId": "sku_sunrise_atta_5kg", "spinId": "spin_sunrise_atta_5kg", "quantity": 1}],
    )
    return json.loads(json.dumps(STORE.get_cart()))


@pytest.mark.parametrize(
    "damage",
    [
        lambda v: v.pop("items"),
        lambda v: v.update(cartTotalAmount="12.345"),
        lambda v: v.update(cartTotalAmount=None),
        lambda v: v["items"][0].update(quantity=0),
        lambda v: v["items"][0].update(quantity="1"),
        lambda v: v["items"][0].update(quantity=True),
        lambda v: v["items"][0].update(isInStockAndAvailable="yes"),
        lambda v: v["items"][0].pop("skuId"),
        lambda v: v["items"][0].update(discountedFinalPrice=-5),
        lambda v: v.pop("selectedAddressDetails"),
        lambda v: v["billBreakdown"].update(lineItems="none"),
    ],
)
def test_odd_cart_shapes_raise(damage):
    view = _view()
    parse_cart(view)  # the undamaged view parses
    damage(view)
    with pytest.raises(StoreUnavailable):
        parse_cart(view)


# ----- checkout, orders, payment -------------------------------------------------------------

LINES = [
    ("sku_sunrise_atta_5kg", "spin_sunrise_atta_5kg", 1),
    ("sku_nandini_toned_1l", "spin_nandini_toned_1l", 1),
]


async def placed_order():
    client = StoreClient(mcp)
    await client.update_cart("addr_home", LINES)
    return client, await client.checkout("addr_home")


async def test_checkout_always_sends_upi_and_never_confirm_duplicate():
    client = StoreClient(mcp)
    sent = []
    real = client._call

    async def spy(tool, args):
        sent.append((tool, args))
        return await real(tool, args)

    client._call = spy
    await client.update_cart("addr_home", LINES)
    await client.checkout("addr_home")
    assert sent[-1] == ("checkout", {"addressId": "addr_home", "paymentMethod": "UPI"})
    assert [p for p in inspect.signature(StoreClient.checkout).parameters] == ["self", "address_id"]


async def test_order_round_trip_in_paise():
    client, placed = await placed_order()
    assert placed.total_paise == 34500
    [order] = await client.get_orders()
    assert order.order_id == placed.order_id
    assert order.items == (("sku_nandini_toned_1l", 1), ("sku_sunrise_atta_5kg", 1))
    assert (order.total_paise, order.status) == (34500, "PENDING_PAYMENT")
    status = await client.check_payment_status(placed.order_id, placed.paas_id)
    assert (status.success, status.failed) == (False, False)
    with pytest.raises(StoreUnavailable):  # not paid yet
        await client.confirm_order(placed.order_id, placed.paas_id)
    STORE.pay(placed.paas_id)
    assert (await client.check_payment_status(placed.order_id, placed.paas_id)).success
    await client.confirm_order(placed.order_id, placed.paas_id)
    assert (await client.get_orders())[0].status == "CONFIRMED"


async def test_a_duplicate_refusal_is_marked():
    client, _ = await placed_order()
    await client.update_cart("addr_home", LINES)
    with pytest.raises(StoreUnavailable) as err:
        await client.checkout("addr_home")
    assert err.value.duplicate
    with pytest.raises(StoreUnavailable) as err:
        await client.checkout("addr_nope")
    assert not err.value.duplicate


async def test_a_payment_reply_about_another_order_is_refused():
    client, placed = await placed_order()
    data = STORE.check_payment_status(placed.paas_id)
    parse_payment_status(data, placed.order_id, placed.paas_id)
    for damage in (
        {"orderId": "IM-1"},
        {"paasId": "x"},
        {"status": "DONE"},
        {"status": "SUCCESS"},  # flags still say pending
        {"terminal": "no"},
        {"cartTotal": None},
        {"orderStatus": ""},
    ):
        with pytest.raises(StoreUnavailable):
            parse_payment_status({**data, **damage}, placed.order_id, placed.paas_id)


@pytest.mark.parametrize(
    "damage",
    [
        lambda o: o.pop("orderId"),
        lambda o: o.update(totalAmount="1.001"),
        lambda o: o.update(itemCount=99),
        lambda o: o.update(items="none"),
        lambda o: o["items"][0].update(quantity=True),
        lambda o: o["items"][0].pop("itemId"),
        lambda o: o["deliveryAddress"].pop("addressLine"),
        lambda o: o.update(paymentStatus=None),
    ],
)
async def test_odd_orders_raise(damage):
    await placed_order()
    data = json.loads(json.dumps(STORE.get_orders()))
    parse_orders(data)
    with pytest.raises(StoreUnavailable):
        parse_orders({"orders": [*data["orders"], *data["orders"]]})  # one ID twice
    damage(data["orders"][0])
    with pytest.raises(StoreUnavailable):
        parse_orders(data)


def test_the_adapter_cannot_pay():
    assert not [name for name in dir(StoreClient) if name.lower() in ("pay", "demo_pay")]
