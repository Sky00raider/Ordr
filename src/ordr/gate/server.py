"""Ordr MCP server. Exactly five tools. No checkout, ever.

The household comes from the authenticated session, never from an argument.
"""

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

mcp = FastMCP("ordr")


class OrderItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_id: str
    spin_id: str
    qty: Annotated[int, Field(gt=0)]
    heard_as: str


def _todo() -> Any:
    raise ToolError("Not built yet")


@mcp.tool
def search_products(query: str) -> dict:
    """Search the store. Marks the household's usual item."""
    return _todo()


@mcp.tool
def prepare_order(items: Annotated[list[OrderItemIn], Field(min_length=1)]) -> dict:
    """Build the cart. Ordr sends the read-back to the parent."""
    return _todo()


@mcp.tool
def get_order_status(mandate_id: str) -> dict:
    """Status of this household's order."""
    return _todo()


@mcp.tool
def abort_order(mandate_id: str) -> dict:
    """Stop this household's order. Always safe."""
    return _todo()


@mcp.tool
def get_audit_log(limit: Annotated[int, Field(gt=0, le=100)] = 20) -> dict:
    """Recent log entries for this household."""
    return _todo()


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8002)
