"""The bot as an MCP client of the gate (TRD sections 3 and 4).

The two headers are set here, by code, and are not tool inputs. Tool definitions come from the
gate's `list_tools`. Each call is timed and written to a JSON lines file: tool name, time and
whether it worked. Never the arguments or the result (they hold item names and the parent's words).
"""

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

log = logging.getLogger("ordr.bot.mcp")

CALL_TIMEOUT = 30.0
# Error codes the gate can return (TRD section 6). Anything else becomes "internal".
KNOWN_CODES = (
    "item_not_from_search",
    "order_already_open",
    "over_cap",
    "needs_review",
    "not_found",
    "store_error",
    "not_available",
    "order_too_large",
)


class ToolResult:
    """What a tool call gave back. `code` is set only when it failed."""

    def __init__(self, ok: bool, data: Any = None, code: str | None = None) -> None:
        self.ok = ok
        self.data = data
        self.code = code


class Tools(Protocol):
    """One conversation's connection to the gate's tools, for one verified parent number."""

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...

    async def __aenter__(self) -> "Tools": ...

    async def __aexit__(self, *exc: object) -> None: ...


class CallLog:
    """JSON lines of MCP calls, for the demo panel (`Get-Content -Wait mcp_calls.jsonl`)."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None

    def write(self, tool: str, started: float, ok: bool) -> None:
        if self.path is None:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "tool": tool,
            "ms": round((time.monotonic() - started) * 1000),
            "ok": ok,
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as exc:
            log.warning("call log not written: %s", type(exc).__name__)


def error_code(message: str) -> str:
    """The gate's short error code inside an error message, or "internal"."""
    for code in KNOWN_CODES:
        if re.search(rf"(?<![a-z_]){code}(?![a-z_])", message):
            return code
    return "internal"


class McpTools:
    """The real thing: FastMCP's client over Streamable HTTP to the gate's /mcp."""

    def __init__(self, gate_url: str, token: str, number: str, calls: CallLog) -> None:
        headers = {"x-ordr-internal": token.strip(), "x-ordr-number": number.strip()}
        transport = StreamableHttpTransport(f"{gate_url.rstrip('/')}/mcp", headers=headers)
        self._client = Client(transport, timeout=CALL_TIMEOUT)
        self._calls = calls

    async def __aenter__(self) -> "McpTools":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    async def list_tools(self) -> list[dict[str, Any]]:
        started = time.monotonic()
        ok = False
        try:
            tools = await self._client.list_tools()
            ok = True
        finally:
            self._calls.write("list_tools", started, ok)
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": getattr(tool, "input_schema", None) or tool.inputSchema,
            }
            for tool in tools
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        result = None
        try:
            result = await self._client.call_tool(name, arguments, raise_on_error=False)
        except Exception as exc:  # network or protocol error. No details: they could hold text
            self._calls.write(name, started, False)
            log.warning("tool %s failed: %s", name, type(exc).__name__)
            return ToolResult(False, code="internal")
        failed = bool(result.is_error)
        self._calls.write(name, started, not failed)
        if failed:
            text = " ".join(getattr(block, "text", "") for block in (result.content or []) if block)
            return ToolResult(False, code=error_code(text))
        data = result.structured_content
        if data is None:
            data = result.data
        return ToolResult(True, data=data)
