"""Fakes for the bot tests. No network, no paid calls."""

import asyncio
from types import SimpleNamespace
from typing import Any

from ordr.bot.channel import ChannelError
from ordr.bot.gate_client import GateUnavailable
from ordr.bot.mcp_tools import ToolResult
from ordr.contracts import Action, Button, Incoming

PARENT = "1000000001"
KID = "2000000002"
MANDATE = "11111111-2222-3333-4444-555555555555"


def press(action: Action, who: str = PARENT, mid: str = "m1", mandate: str = MANDATE) -> Incoming:
    return Incoming(message_id=mid, number=who, action=action, mandate_id=mandate)


def text(body: str, who: str = PARENT, mid: str = "t1") -> Incoming:
    return Incoming(message_id=mid, number=who, text=body)


class FakeChannel:
    """Records what was sent. `fail` makes the next sends raise, `fail_for` fails one number."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, list[Button] | None]] = []
        self.fail = 0
        self.fail_for: set[str] = set()
        self.permanent = False

    def _maybe_fail(self, number: str) -> None:
        if number in self.fail_for or self.fail > 0:
            if self.fail > 0 and number not in self.fail_for:
                self.fail -= 1
            raise ChannelError("down", permanent=self.permanent)

    async def send_text(self, number: str, text: str) -> None:
        self._maybe_fail(number)
        self.sent.append(("text", number, text, None))

    async def send_buttons(self, number: str, text: str, buttons: list[Button]) -> None:
        self._maybe_fail(number)
        self.sent.append(("buttons", number, text, list(buttons)))

    async def send_voice(self, number: str, audio: bytes, audio_type: str) -> None:
        raise ChannelError("no voice", permanent=True)

    def verify(self, headers: dict[str, str], body: bytes) -> bool:
        return True

    async def parse(self, body: bytes) -> list[Incoming]:
        return []

    def texts(self, number: str) -> list[str]:
        return [item[2] for item in self.sent if item[1] == number]


class FakeGate:
    """Stands in for the gate's /internal API."""

    def __init__(self) -> None:
        self.acts: list[tuple[str, str, str, str]] = []
        self.act_result: dict[str, Any] = {"ok": True, "code": "confirmed"}
        self.act_error: Exception | None = None
        self.rows: list[dict[str, Any]] = []
        self.acked: list[int] = []
        self.log_text = (
            "Ordr log, newest last. Times are IST.\n2026-11-24 10:00:00  prepared  abcd1234\n"
        )
        self.outbox_errors = 0
        self.ack_error = False

    async def act(self, number: str, message_id: str, action: str, mandate_id: str) -> dict:
        self.acts.append((number, message_id, action, mandate_id))
        if self.act_error is not None:
            raise self.act_error
        return self.act_result

    async def outbox(self) -> list[dict[str, Any]]:
        if self.outbox_errors > 0:
            self.outbox_errors -= 1
            raise GateUnavailable("down")
        return [row for row in self.rows if row["id"] not in self.acked]

    async def ack(self, ids: list[int]) -> None:
        if self.ack_error:
            raise GateUnavailable("down")
        self.acked.extend(ids)

    async def log(self, number: str) -> str:
        return self.log_text


class FakeReplier:
    """The model side of the pipeline. `gate` makes `reply` wait until set. `boom` raises."""

    def __init__(self, answer: str | None = "Which milk?") -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.finished = 0
        self.boom = False

    async def reply(self, number: str, text: str) -> str | None:
        self.calls.append((number, text))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.boom:
            raise RuntimeError("model broke")
        self.finished += 1
        return self.answer


class ForbiddenReplier:
    """Fails the test if the model is ever reached."""

    async def reply(self, number: str, text: str) -> str | None:
        raise AssertionError("the model was reached")


def row(
    row_id: int,
    number: str,
    text: str = "hello",
    buttons: list[dict] | None = None,
    kind: str = "k",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "number": number,
        "kind": kind,
        "text": text,
        "speak": False,
        "lang": "en",
        "buttons": buttons or [],
    }


# ----- model fakes ---------------------------------------------------------------------------


def text_block(body: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=body)


def thinking_block() -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def tool_use(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=arguments)


def response(blocks: list[Any], stop: str) -> SimpleNamespace:
    return SimpleNamespace(content=blocks, stop_reason=stop)


class FakeAnthropic:
    """`client.beta.messages.create(...)` that plays back scripted responses."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item


TOOL_DEFS = [
    {"name": name, "description": name, "input_schema": {"type": "object", "properties": {}}}
    for name in (
        "search_products",
        "prepare_order",
        "get_order_status",
        "abort_order",
        "get_audit_log",
    )
]


class FakeTools:
    """A fake MCP connection. `results` maps a tool name to its ToolResult."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "FakeTools":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        return TOOL_DEFS

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append((name, arguments))
        return self.results.get(name, ToolResult(True, data={"ok": True}))
