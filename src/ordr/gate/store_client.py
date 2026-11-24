"""Async adapter over the store's MCP tools (TRD section 5: "Convert money once, here").

Money leaves this file as whole paise. Anything missing, mistyped or odd raises StoreUnavailable,
so the gate never works from a half-read store response. There is no `checkout` here yet: only
the final-check code (N6) may add it.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from fastmcp import Client

from ordr.contracts import Cart, CartLine

STORE_ID = "demo-instamart"
ITEM_TOTAL_LABEL = "Item total"


class StoreUnavailable(Exception):
    """The store failed, timed out, refused, or answered in a shape we do not trust."""


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
            raise StoreUnavailable(f"{tool} failed: {type(exc).__name__}") from exc
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
