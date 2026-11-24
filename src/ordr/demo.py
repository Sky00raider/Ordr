"""Terminal demo. No WhatsApp, no API key, no internet: `python -m ordr.demo`.

It starts the demo store and the gate on free localhost ports and talks to them over real MCP and
the real internal API. Two things are scripted here and nothing else: the assistant's choice of
items (no AI model runs) and the button presses. Every message shown is the text the gate would
send to the phones. Payment is simulated.

`--pause` waits for Enter between scenes, for recording. `--lang kn` shows the parent's messages
in Kannada. Every scene checks its own result, and the exit code is 1 if one fails.
"""

import argparse
import asyncio
import contextlib
import logging
import secrets
import socket
import sys
import tempfile
import textwrap
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from ordr.config import Settings
from ordr.gate.app import create_app
from ordr.gate.store_client import StoreClient
from ordr.gate.upi import SimulatedUpi
from ordr.store.server import STORE
from ordr.store.server import mcp as store_mcp

PARENT, KID = "1000000001", "1000000002"  # not real phone numbers
NAMES = {PARENT: "PARENT'S PHONE", KID: "KID'S PHONE"}
LABEL = (
    "Demo store modelled on Swiggy Instamart's published MCP tools. Not affiliated with Swiggy. "
    "No real orders. Live access pending. Payment is simulated."
)
WIDTH = 78


class DemoFailed(Exception):
    """A scene did not end the way the design says it must."""


def check(condition: bool, what: str) -> None:
    if not condition:
        raise DemoFailed(what)
    print(f"    [ok] {what}")


class TracedStore(StoreClient):
    """The gate's own store adapter, with each tool name written down."""

    def __init__(self, target: Any) -> None:
        super().__init__(target)
        self.calls: list[str] = []

    async def _call(self, tool: str, args: dict[str, Any]) -> dict:
        self.calls.append(tool)
        return await super()._call(tool, args)


def _free_socket() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


@contextlib.asynccontextmanager
async def _serve(app: Any):
    """Serve an ASGI app on a free localhost port. Yields the base URL."""
    sock = _free_socket()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(400):
            if server.started:
                break
            if task.done():
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise DemoFailed("a server did not start")
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, 10)
        sock.close()


class Demo:
    def __init__(self, store_url: str, gate_url: str, token: str, traced: TracedStore) -> None:
        self.store_url, self.gate_url = store_url, gate_url
        self.token, self.traced = token, traced
        self.http = httpx.AsyncClient(timeout=60, headers={"x-ordr-internal": token})
        self.presses = 0

    # ----- the two sides ---------------------------------------------------------------

    def assistant(self) -> Client:
        """The AI side: an MCP client of the gate, with the headers the webhook would set."""
        headers = {"x-ordr-internal": self.token, "x-ordr-number": PARENT}
        return Client(StreamableHttpTransport(f"{self.gate_url}/mcp", headers=headers))

    def other_assistant(self) -> Client:
        """Any other program connected to the same store account. Ordr is not involved."""
        return Client(f"{self.store_url}/mcp")

    async def press(self, number: str, action: str, mandate_id: str, label: str) -> str:
        self.presses += 1
        print(f"\n  >> {NAMES[number]}: taps [{label}]")
        response = await self.http.post(
            f"{self.gate_url}/internal/action",
            headers={"x-ordr-number": number},
            json={"message_id": f"d{self.presses}", "action": action, "mandate_id": mandate_id},
        )
        return response.json()["code"]

    async def show_messages(self) -> list[dict[str, Any]]:
        """Fetch, print and acknowledge what the gate wants to send."""
        rows = (await self.http.get(f"{self.gate_url}/internal/outbox")).json()
        for row in rows:
            print(f"\n  +-- {NAMES[row['number']]} " + "-" * 40)
            for line in row["text"].split("\n"):
                for part in textwrap.wrap(line, WIDTH - 6) or [""]:
                    print(f"  |  {part}")
            if row["buttons"]:
                print("  |  " + "  ".join(f"[ {b['label']} ]" for b in row["buttons"]))
            print("  +" + "-" * 56)
        ids = [row["id"] for row in rows]
        await self.http.post(f"{self.gate_url}/internal/outbox/ack", json={"ids": ids})
        return rows

    async def switch(self, **body: Any) -> None:
        response = await self.http.post(f"{self.store_url}/demo/switch", json=body)
        response.raise_for_status()

    # ----- steps -----------------------------------------------------------------------

    async def order(self, *wanted: tuple[str, str]) -> dict[str, str]:
        """Search, then prepare_order. `wanted` is (search words, pack)."""
        items = []
        async with self.assistant() as client:
            for query, pack in wanted:
                found = (await client.call_tool("search_products", {"query": query})).data
                pick = next(p for p in found["products"] if p["pack"] == pack)
                print(f'  AI -> Ordr: search_products("{query}") -> {pick["name"]}')
                items.append(
                    {
                        "sku_id": pick["sku_id"],
                        "spin_id": pick["spin_id"],
                        "qty": 1,
                        "heard_as": query,
                    }
                )
            out = (await client.call_tool("prepare_order", {"items": items})).data
        print(f"  AI -> Ordr: prepare_order -> {out['status']}")
        return out

    def calls_since(self, mark: int) -> list[str]:
        return self.traced.calls[mark:]

    def show_calls(self, mark: int) -> Counter[str]:
        calls = self.calls_since(mark)
        print("\n  MCP calls Ordr made to the store after the tap:")
        print("    " + (" > ".join(calls) or "none"))
        return Counter(calls)

    async def status(self, mandate_id: str) -> str:
        async with self.assistant() as client:
            return (await client.call_tool("get_order_status", {"mandate_id": mandate_id})).data[
                "state"
            ]

    # ----- scenes ----------------------------------------------------------------------

    async def scene_tools(self) -> None:
        async with self.assistant() as client:
            tools = sorted(t.name for t in await client.list_tools())
        for name in tools:
            print(f"    {name}")
        check(len(tools) == 5 and "checkout" not in tools, "five tools, and no checkout")

    async def scene_happy(self) -> None:
        print('  Parent asks for "milk and atta".')
        out = await self.order(("nandini toned milk", "1 L"), ("atta", "5 kg"))
        mandate = out["mandate_id"]
        await self.show_messages()
        check(await self.press(PARENT, "confirm", mandate, "Confirm") == "confirmed", "confirmed")
        await self.show_messages()
        mark = len(self.traced.calls)
        code = await self.press(KID, "approve", mandate, "Approve and pay")
        calls = self.show_calls(mark)
        check(code == "placed" and calls["checkout"] == 1, "placed, with exactly one checkout")
        await self.show_messages()
        check(await self.press(KID, "pay", mandate, "Pay") == "paid", "paid (simulated UPI)")
        await self.show_messages()
        check(await self.status(mandate) == "PAID", "order is PAID")

    async def scene_tamper(self) -> None:
        print('  Parent asks for "rice and dal". The parent confirms, and the card sits unread.')
        out = await self.order(("rice", "5 kg"), ("toor dal", "1 kg"))
        mandate = out["mandate_id"]
        await self.show_messages()
        await self.press(PARENT, "confirm", mandate, "Confirm")
        await self.show_messages()

        print("\n  !! Another AI assistant on the same store account adds a Rs 1,999 item.")
        async with self.other_assistant() as other:
            cart = (await other.call_tool("get_cart", {})).structured_content["data"]
            hamper = (
                await other.call_tool(
                    "search_products", {"addressId": cart["selectedAddress"], "query": "hamper"}
                )
            ).structured_content["data"]["products"][0]["variations"][0]
            lines = [
                {"skuId": i["skuId"], "spinId": i["spinId"], "quantity": i["quantity"]}
                for i in cart["items"]
            ]
            lines.append({"skuId": hamper["skuId"], "spinId": hamper["spinId"], "quantity": 1})
            await other.call_tool(
                "update_cart", {"selectedAddressId": cart["selectedAddress"], "items": lines}
            )
            total = (await other.call_tool("get_cart", {})).structured_content["data"][
                "cartTotalAmount"
            ]
        print(f"     The store cart is now Rs {float(total):,.0f}.")
        print("     The kid's card still shows the old order.")

        mark, orders = len(self.traced.calls), len(STORE.orders)
        code = await self.press(KID, "approve", mandate, "Approve and pay")
        calls = self.show_calls(mark)
        await self.show_messages()
        check(code == "blocked", "refused: the live cart is not the approved cart")
        check(calls["checkout"] == 0 and len(STORE.orders) == orders, "zero checkout calls")

    async def scene_price(self) -> None:
        print("  The kid's cart still holds the other assistant's item, so Ordr asks first.")
        out = await self.order(("toor dal", "1 kg"), ("atta", "5 kg"))
        mandate = out["mandate_id"]
        check(out["status"] == "waiting_for_kid", "the kid's cart is not touched without a tap")
        await self.show_messages()
        code = await self.press(KID, "replace_cart", mandate, "Replace")
        check(code == "prepared", "cart replaced after the kid's tap")
        await self.show_messages()
        await self.press(PARENT, "confirm", mandate, "Confirm")
        await self.show_messages()
        print("\n  !! The store adds a Rs 250 surge fee while the card is unread.")
        await self.switch(switch="extra_fee", rupees=250)
        mark = len(self.traced.calls)
        code = await self.press(KID, "approve", mandate, "Approve and pay")
        calls = self.show_calls(mark)
        await self.show_messages()
        check(code == "blocked" and calls["checkout"] == 0, "over the ceiling, zero checkout calls")
        await self.switch(switch="extra_fee", rupees=0)

    async def scene_stop(self) -> None:
        print("  The parent changes their mind.")
        out = await self.order(("sugar", "1 kg"), ("rice", "5 kg"))
        mandate = out["mandate_id"]
        await self.show_messages()
        await self.press(PARENT, "confirm", mandate, "Confirm")
        await self.show_messages()
        check(await self.press(PARENT, "stop", mandate, "Stop") == "stopped", "stopped")
        await self.show_messages()
        mark = len(self.traced.calls)
        code = await self.press(KID, "approve", mandate, "Approve and pay")
        check(code == "not_current", "a late Approve does nothing")
        check(self.calls_since(mark) == [], "no store call at all")

    async def scene_log(self) -> None:
        response = await self.http.get(
            f"{self.gate_url}/internal/log", headers={"x-ordr-number": KID}
        )
        lines = response.text.strip().split("\n")
        for line in [lines[0], "...", *lines[-8:]]:
            print(f"    {line[: WIDTH - 4]}")
        check(PARENT not in response.text and KID not in response.text, "no phone numbers in it")
        check(len(STORE.orders) == 1, "one order at the store in the whole run")


SCENES = [
    ("What the AI can do", Demo.scene_tools),
    ("A normal order", Demo.scene_happy),
    ("The cart is changed after the parent confirms", Demo.scene_tamper),
    ("The price jumps", Demo.scene_price),
    ("Stop", Demo.scene_stop),
    ("The log", Demo.scene_log),
]


async def run(lang: str = "en", pause: bool = False) -> None:
    STORE.reset()
    token = secrets.token_hex(32)
    print("=" * WIDTH)
    print("Ordr: it can shop, but it can't buy.")
    print("\n".join(textwrap.wrap(LABEL, WIDTH)))
    print("Scripted here: the assistant's item choices (no AI model runs) and the taps.")
    print("=" * WIDTH)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        async with _serve(store_mcp.http_app(path="/mcp")) as store_url:
            traced = TracedStore(f"{store_url}/mcp")
            settings = Settings(
                _env_file=None,
                server_key=secrets.token_hex(32),
                internal_token=token,
                db_path=Path(folder) / "demo.db",
                policy_path=Path("policy.json"),
                store_mcp_url=f"{store_url}/mcp",
                parent_number=PARENT,
                kid_number=KID,
                safe_phrase="blue mango",
                parent_lang=lang,
            )
            app = create_app(
                settings, store=traced, upi=SimulatedUpi.for_store(settings.store_mcp_url)
            )
            try:
                async with _serve(app) as gate_url:
                    demo = Demo(store_url, gate_url, token, traced)
                    async with demo.http:
                        for n, (title, scene) in enumerate(SCENES, 1):
                            if pause:
                                await asyncio.to_thread(input, "\n[Enter for the next scene] ")
                            print(f"\n{'-' * WIDTH}\n{n}. {title}\n{'-' * WIDTH}")
                            await scene(demo)
            finally:
                app.state.actions.db.close()
    print(f"\n{'=' * WIDTH}\nAll scenes ended as designed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ordr terminal demo")
    parser.add_argument("--pause", action="store_true", help="wait for Enter between scenes")
    parser.add_argument("--lang", choices=("en", "kn"), default="en", help="parent's language")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.INFO)
    try:
        asyncio.run(run(args.lang, args.pause))
    except DemoFailed as exc:
        print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
