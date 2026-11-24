"""Async adapter over the store's MCP tools (TRD section 5: "Convert money once, here").

Money leaves this file as whole paise. Anything missing, mistyped or odd raises StoreUnavailable,
so the gate never works from a half-read store response. Only `actions.py` may call `checkout`.
Paying is not here. The simulated UPI PIN is in `upi.py`.
"""

import asyncio
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from fastmcp import Client

from ordr.contracts import Cart, CartLine

STORE_ID = "demo-instamart"
ITEM_TOTAL_LABEL = "Item total"
UPI = "UPI"
PENDING_PAYMENT = "PENDING_PAYMENT"
ORDERS_READ = 50
PAYMENT_STATUSES = frozenset({"PENDING", "SUCCESS", "FAILED"})


class StoreUnavailable(Exception):
    """The store failed, timed out, refused, or answered in a shape we do not trust."""

    duplicate = False  # the store refused a probable duplicate (DUPLICATE_ORDER)


@dataclass(frozen=True)
class StoreProduct:
    """One buyable variation from a search. This is what the gate remembers as "seen"."""

    sku_id: str
    spin_id: str
    name: str
    brand: str
    pack: str
    price_paise: int
    in_stock: bool
    max_qty: int


@dataclass(frozen=True)
class Placed:
    """What checkout returned for a UPI order waiting for payment."""

    order_id: str
    paas_id: str
    total_paise: int


@dataclass(frozen=True)
class StoreOrder:
    """One order as get_orders shows it. It has no spinId (TRD section 5)."""

    order_id: str
    status: str
    payment_status: str
    payment_method: str
    items: tuple[tuple[str, int], ...]  # (sku_id, qty), merged and sorted
    address_id: str
    address_text: str
    total_paise: int


@dataclass(frozen=True)
class PaymentStatus:
    status: str
    order_status: str
    total_paise: int

    @property
    def success(self) -> bool:
        return self.status == "SUCCESS"

    @property
    def failed(self) -> bool:
        """The store says both the payment and the order are dead. Nothing less is UNPAID."""
        return self.status == "FAILED" and self.order_status in ("FAILED", "CANCELLED")


# ----- parsing helpers ---------------------------------------------------------------------


def _fail(what: str) -> StoreUnavailable:
    return StoreUnavailable(f"odd store response: {what}")


def _dict(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise _fail(what)
    return value


def _list(value: Any, what: str) -> list:
    if not isinstance(value, list):
        raise _fail(what)
    return value


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(what)
    return value


def _whole(value: Any, what: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _fail(what)
    return value


def _flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(what)
    return value


def paise(value: Any, what: str = "money") -> int:
    """Rupees as the store shows them (number or text) to whole paise, exactly. Never a float."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise _fail(what)
    try:
        amount = Decimal(str(value).strip()) * 100
    except InvalidOperation:
        raise _fail(what) from None
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise _fail(what)
    return int(amount)


def parse_cart(data: dict) -> Cart:
    """Build a Cart from a get_cart or update_cart view."""
    details = _dict(data.get("selectedAddressDetails"), "address details")
    items = _list(data.get("items"), "items")
    lines: list[CartLine] = []
    in_stock = True
    for raw in items:
        item = _dict(raw, "cart item")
        lines.append(
            CartLine(
                sku_id=_text(item.get("skuId"), "skuId"),
                spin_id=_text(item.get("spinId"), "spinId"),
                qty=_whole(item.get("quantity"), "quantity", minimum=1),
                name=_text(item.get("itemName"), "itemName"),
                pack=_text(item.get("itemVariant"), "itemVariant"),
                unit_price_paise=paise(item.get("discountedFinalPrice"), "line price"),
            )
        )
        in_stock = in_stock and _flag(item.get("isInStockAndAvailable"), "stock flag")
    bill = _dict(data.get("billBreakdown"), "bill")
    fees: dict[str, int] = {}
    for raw in _list(bill.get("lineItems"), "bill lines"):
        row = _dict(raw, "bill line")
        label = _text(row.get("label"), "bill label")
        if label != ITEM_TOTAL_LABEL:
            fees[label] = paise(row.get("value"), "fee")
    return Cart(
        lines=tuple(lines),
        total_paise=paise(data.get("cartTotalAmount"), "cart total"),
        address_id=_text(data.get("selectedAddress"), "address id"),
        address_text=_text(details.get("address"), "address text"),
        fees_paise=fees,
        all_in_stock=in_stock,
        # No unserviceable items and no cart warning (such as the minimum order value).
        serviceable=not data.get("unserviceableItems") and not data.get("cartWarning"),
    )


def parse_products(data: dict) -> list[StoreProduct]:
    out: list[StoreProduct] = []
    for raw in _list(data.get("products"), "products"):
        product = _dict(raw, "product")
        brand = _text(product.get("brand"), "brand")
        for raw_variation in _list(product.get("variations"), "variations"):
            v = _dict(raw_variation, "variation")
            price = _dict(v.get("price"), "price")
            out.append(
                StoreProduct(
                    sku_id=_text(v.get("skuId"), "skuId"),
                    spin_id=_text(v.get("spinId"), "spinId"),
                    name=_text(v.get("displayName"), "displayName"),
                    brand=_text(v.get("brandName", brand), "brandName"),
                    pack=_text(v.get("quantityDescription"), "pack"),
                    price_paise=paise(price.get("offerPrice"), "offer price"),
                    in_stock=_flag(v.get("isInStockAndAvailable"), "stock flag"),
                    max_qty=_whole(v.get("maxQuantity"), "maxQuantity"),
                )
            )
    return out


def _same(value: Any, expected: str, what: str) -> None:
    if value != expected:
        raise _fail(what)


def parse_checkout(data: dict) -> Placed:
    """Only a UPI order that is waiting for payment is accepted."""
    _same(data.get("paymentMethod"), UPI, "payment method")
    _same(data.get("status"), PENDING_PAYMENT, "checkout status")
    return Placed(
        order_id=_text(data.get("orderId"), "orderId"),
        paas_id=_text(data.get("paasId"), "paasId"),
        total_paise=paise(data.get("cartTotal"), "checkout total"),
    )


def parse_orders(data: dict) -> list[StoreOrder]:
    out: list[StoreOrder] = []
    for raw in _list(data.get("orders"), "orders"):
        order = _dict(raw, "order")
        address = _dict(order.get("deliveryAddress"), "delivery address")
        items: Counter[str] = Counter()
        for raw_item in _list(order.get("items"), "order items"):
            line = _dict(raw_item, "order item")
            items[_text(line.get("itemId"), "itemId")] += _whole(
                line.get("quantity"), "order quantity", minimum=1
            )
        if _whole(order.get("itemCount"), "itemCount") != items.total():
            raise _fail("itemCount")
        out.append(
            StoreOrder(
                order_id=_text(order.get("orderId"), "orderId"),
                status=_text(order.get("status"), "order status"),
                payment_status=_text(order.get("paymentStatus"), "payment status"),
                payment_method=_text(order.get("paymentMethod"), "payment method"),
                items=tuple(sorted(items.items())),
                address_id=_text(address.get("id"), "order address id"),
                address_text=_text(address.get("addressLine"), "order address"),
                total_paise=paise(order.get("totalAmount"), "order total"),
            )
        )
    if len({order.order_id for order in out}) != len(out):
        raise _fail("repeated order id")
    return out


def parse_payment_status(data: dict, order_id: str, paas_id: str) -> PaymentStatus:
    _same(data.get("orderId"), order_id, "orderId")
    _same(data.get("paasId"), paas_id, "paasId")
    status = _text(data.get("status"), "payment status")
    if status not in PAYMENT_STATUSES:
        raise _fail("payment status")
    for name, expected in (
        ("terminal", status != "PENDING"),
        ("isTerminalSuccess", status == "SUCCESS"),
        ("isTerminalFailure", status == "FAILED"),
    ):
        if _flag(data.get(name), name) is not expected:
            raise _fail(name)
    return PaymentStatus(
        status=status,
        order_status=_text(data.get("orderStatus"), "order status"),
        total_paise=paise(data.get("cartTotal"), "payment total"),
    )


# ----- the client --------------------------------------------------------------------------


class StoreClient:
    """`target` is a store MCP URL, or a FastMCP server object for in-process tests."""

    store_id = STORE_ID

    def __init__(self, target: Any, timeout: float = 15.0) -> None:
        self._target = target
        self._timeout = timeout

    async def _call(self, tool: str, args: dict[str, Any]) -> dict:
        try:
            async with asyncio.timeout(self._timeout), Client(self._target) as client:
                result = await client.call_tool(tool, args)
        except Exception as exc:  # tool error, network, timeout, protocol: all the same to us
            error = StoreUnavailable(f"{tool} failed: {type(exc).__name__}")
            error.duplicate = "DUPLICATE_ORDER" in str(exc)
            raise error from exc
        body = result.structured_content
        if not isinstance(body, dict) or body.get("success") is not True:
            raise _fail(f"{tool} envelope")
        return _dict(body.get("data"), f"{tool} data")

    async def search_products(self, address_id: str, query: str) -> list[StoreProduct]:
        data = await self._call("search_products", {"addressId": address_id, "query": query})
        return parse_products(data)

    async def get_cart(self) -> Cart:
        return parse_cart(await self._call("get_cart", {}))

    async def update_cart(self, address_id: str, items: Sequence[tuple[str, str, int]]) -> None:
        """Replaces the whole store cart with (sku_id, spin_id, qty) lines. Read it back after."""
        body = [{"skuId": sku, "spinId": spin, "quantity": qty} for sku, spin, qty in items]
        data = await self._call("update_cart", {"selectedAddressId": address_id, "items": body})
        parse_cart(data)

    async def checkout(self, address_id: str) -> Placed:
        """Buys the server-side cart. UPI is always explicit and confirmDuplicate is never sent.

        Never call this twice for one mandate. An error does not mean nothing was ordered.
        """
        args = {"addressId": address_id, "paymentMethod": UPI}
        return parse_checkout(await self._call("checkout", args))

    async def get_orders(self) -> list[StoreOrder]:
        args = {"count": ORDERS_READ, "orderType": "INSTAMART"}
        return parse_orders(await self._call("get_orders", args))

    async def check_payment_status(self, order_id: str, paas_id: str) -> PaymentStatus:
        data = await self._call("check_payment_status", {"paasId": paas_id, "orderId": order_id})
        return parse_payment_status(data, order_id, paas_id)

    async def confirm_order(self, order_id: str, paas_id: str) -> None:
        """Completes a paid order. Anything but a clear success raises."""
        data = await self._call("confirm_order", {"orderId": order_id, "paasId": paas_id})
        _same(data.get("orderId"), order_id, "orderId")
        _same(data.get("paasId"), paas_id, "paasId")
        _same(data.get("result"), "success", "confirm result")
        _same(data.get("orderStatus"), "CONFIRMED", "confirm status")
