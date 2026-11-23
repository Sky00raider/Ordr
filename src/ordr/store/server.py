"""Demo store modelled on Swiggy Instamart's published MCP tools.

Not affiliated with Swiggy. No real orders. Parameter names follow the docs, simplified:
https://mcp.swiggy.com/builders/llms.txt (check each response shape before building it).
"""

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

mcp = FastMCP("demo-store")


def _todo() -> Any:
    raise ToolError("Not built yet")


@mcp.tool
def get_addresses() -> dict:
    """Saved addresses on the account."""
    return _todo()


@mcp.tool
def search_products(addressId: str, query: str) -> dict:
    """Products with variations, pack sizes and stock."""
    return _todo()


@mcp.tool
def update_cart(selectedAddressId: str, items: list[dict]) -> dict:
    """Replaces the whole cart."""
    return _todo()


@mcp.tool
def get_cart() -> dict:
    """Current cart and bill."""
    return _todo()


@mcp.tool
def clear_cart() -> dict:
    """Empty the cart."""
    return _todo()


@mcp.tool
def get_payment_options() -> dict:
    """Payment methods for this cart."""
    return _todo()


@mcp.tool
def checkout(
    addressId: str,
    paymentMethod: str | None = None,
    intentApp: str | None = None,
    generateUPIQR: bool | None = None,
    confirmDuplicate: bool | None = None,
) -> dict:
    """Buys whatever is in the cart. Takes no items, like the documented tool."""
    return _todo()


@mcp.tool
def check_payment_status(paasId: str, orderId: str) -> dict:
    """Payment state for a UPI order."""
    return _todo()


@mcp.tool
def confirm_order(orderId: str, paasId: str) -> dict:
    """Completes a paid order."""
    return _todo()


@mcp.tool
def get_orders() -> dict:
    """Past and pending orders."""
    return _todo()


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8001)
