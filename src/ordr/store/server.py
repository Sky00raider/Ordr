"""Demo store modelled on Swiggy Instamart's published MCP tools.

Demo store modelled on Swiggy Instamart's published MCP tools. Not affiliated with Swiggy.
No real orders. Live access pending.

Parameter names follow the docs, simplified: https://mcp.swiggy.com/builders/llms.txt
Responses use the docs' envelope, {"success": true, "data": {...}}. The logic is in state.py.
Demo switches are not tools. They are a local HTTP route, POST /demo/switch.
"""

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

from ordr.store.state import DemoStore, StoreError

mcp = FastMCP("demo-store")
STORE = DemoStore()


def _run(fn: Callable[..., dict], *args: Any, **kwargs: Any) -> dict:
    try:
        return {"success": True, "data": fn(*args, **kwargs)}
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def get_addresses() -> dict:
    """Saved addresses on the account."""
    return _run(STORE.get_addresses)


@mcp.tool
def search_products(addressId: str, query: str) -> dict:
    """Products with variations, pack sizes and stock."""
    return _run(STORE.search_products, addressId, query)


@mcp.tool
def update_cart(selectedAddressId: str, items: list[dict]) -> dict:
    """Replaces the whole cart."""
    return _run(STORE.update_cart, selectedAddressId, items)


@mcp.tool
def get_cart() -> dict:
    """Current cart and bill."""
    return _run(STORE.get_cart)


@mcp.tool
def clear_cart() -> dict:
    """Empty the cart."""
    return _run(STORE.clear_cart)


@mcp.tool
def get_payment_options() -> dict:
    """Payment methods for this cart."""
    return _run(STORE.get_payment_options)


@mcp.tool
def checkout(
    addressId: str,
    paymentMethod: str | None = None,
    intentApp: str | None = None,
    generateUPIQR: bool | None = None,
    confirmDuplicate: bool | None = None,
) -> dict:
    """Buys whatever is in the cart. Takes no items, like the documented tool."""
    return _run(
        STORE.checkout, addressId, paymentMethod, intentApp, generateUPIQR, confirmDuplicate
    )


@mcp.tool
def check_payment_status(paasId: str, orderId: str) -> dict:
    """Payment state for a UPI order."""
    return _run(STORE.check_payment_status, paasId, orderId)


@mcp.tool
def confirm_order(orderId: str, paasId: str) -> dict:
    """Completes a paid order."""
    return _run(STORE.confirm_order, orderId, paasId)


@mcp.tool
def get_orders(count: int = 10, orderType: str | None = None, activeOnly: bool = False) -> dict:
    """Past and pending orders."""
    return _run(STORE.get_orders, count, orderType, activeOnly)


@mcp.custom_route("/demo/pay", methods=["POST"])
async def demo_pay(request: Request) -> JSONResponse:
    """Simulated UPI PIN. Body: {"paasId": ...}. Marks a pending, unexpired payment SUCCESS.
    The order stays PENDING_PAYMENT until confirm_order."""
    body = await request.json()
    try:
        return JSONResponse({"ok": True, **STORE.pay(str(body["paasId"]))})
    except (StoreError, KeyError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)


@mcp.custom_route("/demo/switch", methods=["POST"])
async def demo_switch(request: Request) -> JSONResponse:
    """Body: {"switch": name, ...}. Names: add_expensive_item, change_address, extra_fee
    (rupees), out_of_stock (sku_id, out), preload_cart, reset."""
    body = await request.json()
    name = body.get("switch")
    try:
        match name:
            case "add_expensive_item":
                STORE.switch_add_expensive_item()
            case "change_address":
                STORE.switch_change_address(body.get("address_id", "addr_other"))
            case "extra_fee":
                STORE.switch_extra_fee(int(body["rupees"]))
            case "out_of_stock":
                STORE.switch_out_of_stock(body["sku_id"], bool(body.get("out", True)))
            case "preload_cart":
                STORE.switch_preload_cart()
            case "reset":
                STORE.reset()
            case _:
                return JSONResponse({"ok": False, "error": f"unknown switch {name!r}"}, 400)
    except (StoreError, KeyError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, 400)
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8001)
