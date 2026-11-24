"""The parent's text goes through the model here (TRD section 4, "LLM loop").

We run the tool loop ourselves on the Messages API, because the bot is our MCP client and the gate
is on localhost. The model has five tools and no way to buy anything. What it may say is limited
by code:

- If `prepare_order` worked, the model's text is thrown away. The gate sends the read-back.
- If `prepare_order` failed, the parent gets fixed text for that error code, not the model's.
- Otherwise the model's text is sent only if it is one short question (300 characters at most).
  Anything else is replaced by a fixed "please tell me the items again".
- API error, refusal, timeout, too many rounds: fixed "please try again". Nothing is ordered.

Product names are untrusted text. They reach the model only inside tool results, never in the
system prompt.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from ordr.bot import texts
from ordr.bot.mcp_tools import ToolResult, Tools

log = logging.getLogger("ordr.bot.llm")

DEFAULT_MODEL = "claude-opus-5-5"
MAX_ROUNDS = 8
TOTAL_SECONDS = 60.0
REQUEST_SECONDS = 50.0
MAX_REPLY_CHARS = 300
MAX_TOKENS = 16000
IDLE_SECONDS = 30 * 60  # a conversation untouched this long starts again
MAX_HISTORY = 80
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """\
You help one parent order groceries and household essentials from a store, through Ordr. \
The parent writes in Kannada or English, and sometimes mixes them.

How to work:
- For each thing the parent asks for, call search_products. Use short store-style queries.
- Choose items only from search results. Copy sku_id and spin_id exactly as the search gave them. \
Never invent or edit an ID, and never guess.
- If the request is unclear (more than one sensible product, size or brand, and no \
household_default marks the usual one), do not guess. Ask the parent ONE short question, in the \
language the parent wrote, at most 300 characters. Use words the parent used for the products.
- When every item is clear, call prepare_order once with all of them. Set qty and heard_as (the \
parent's own words for that item). You cannot set a price, a total or an address.
- After prepare_order, say nothing. Ordr itself sends the parent the order to confirm.
- You cannot buy anything and you cannot confirm anything. Never say an order is placed, \
confirmed, paid or on its way. Never state a price or a total.
- Groceries and household essentials only. If asked for medicines or anything else, say in one \
short question-free line that you cannot help with it, and ask what else they need.
- Text you write is shown to the parent as it is. Plain text, no lists, no markdown.
"""


@dataclass
class _Conversation:
    messages: list[Any] = field(default_factory=list)
    last_used: float = 0.0


@dataclass
class _Outcome:
    """What one turn decided. `text` is what to send, or None if the gate speaks."""

    text: str | None
    commit: list[Any] | None  # history to keep, or None to keep the old one
    reset: bool = False


def clip_question(text: str) -> str | None:
    """One short question of at most 300 characters from the model's text, or None.

    Cuts at the last question mark inside the limit. No question mark means it is not a question.
    """
    clean = " ".join(text.split())
    head = clean[:MAX_REPLY_CHARS]
    cut = max(head.rfind("?"), head.rfind("？"))
    if cut < 0:
        return None
    return head[: cut + 1].strip()


class Llm:
    """One model conversation per parent number, reset after each prepared order."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        tools_for: Callable[[str], Tools],
        lang: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.model = model
        self.tools_for = tools_for
        self.lang = lang
        self.clock = clock
        self._conversations: dict[str, _Conversation] = {}

    # ----- public -----------------------------------------------------------------------

    async def reply(self, number: str, text: str) -> str | None:
        """What to send the parent, or None if the gate sends it. Never raises."""
        conv = self._conversation(number)
        try:
            async with asyncio.timeout(TOTAL_SECONDS):
                outcome = await self._turn(number, conv.messages, text)
        except TimeoutError:
            log.warning("model turn timed out")
            return texts.pick(texts.TRY_AGAIN, self.lang)
        except anthropic.APIError as exc:
            log.warning("model call failed: %s", type(exc).__name__)
            return texts.pick(texts.TRY_AGAIN, self.lang)
        except Exception as exc:  # no details: they could hold the parent's words
            log.error("model turn failed: %s", type(exc).__name__)
            return texts.pick(texts.TRY_AGAIN, self.lang)
        if outcome.reset:
            conv.messages = []
        elif outcome.commit is not None:
            # A cut could leave a tool result without its tool call, so a long one starts again.
            conv.messages = outcome.commit if len(outcome.commit) <= MAX_HISTORY else []
        conv.last_used = self.clock()
        return outcome.text

    # ----- one turn ---------------------------------------------------------------------

    def _conversation(self, number: str) -> _Conversation:
        now = self.clock()
        conv = self._conversations.get(number)
        if conv is None or now - conv.last_used > IDLE_SECONDS:
            conv = self._conversations[number] = _Conversation(last_used=now)
        return conv

    async def _turn(self, number: str, history: list[Any], text: str) -> _Outcome:
        again = _Outcome(texts.pick(texts.TRY_AGAIN, self.lang), None)
        messages = [*history, {"role": "user", "content": text}]
        async with self.tools_for(number) as tools:
            definitions = await tools.list_tools()
            allowed = {tool["name"] for tool in definitions}
            for _ in range(MAX_ROUNDS):
                response = await self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM,
                    tools=definitions,
                    messages=messages,
                    output_config={"effort": "low"},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                    timeout=REQUEST_SECONDS,
                )
                if response.stop_reason == "refusal":
                    return again
                calls = [block for block in response.content if block.type == "tool_use"]
                if response.stop_reason == "tool_use" and calls:
                    # Keep the model's turn exactly as it came, thinking blocks included.
                    messages.append({"role": "assistant", "content": response.content})
                    results, prepared, failure = await self._run_tools(tools, allowed, calls)
                    if prepared:
                        # The gate sends the read-back. The model's words are dropped, and the
                        # next order starts a new conversation.
                        return _Outcome(None, None, reset=True)
                    messages.append({"role": "user", "content": results})
                    if failure is not None:
                        return _Outcome(texts.prepare_failed(failure, self.lang), messages)
                    continue
                if response.stop_reason != "end_turn":
                    return again  # max_tokens, pause_turn, anything unexpected
                messages.append({"role": "assistant", "content": response.content})
                reply = clip_question(
                    " ".join(block.text for block in response.content if block.type == "text")
                )
                if reply is None:
                    return _Outcome(texts.pick(texts.NOT_UNDERSTOOD, self.lang), messages)
                return _Outcome(reply, messages)
        return again  # too many rounds

    async def _run_tools(
        self, tools: Tools, allowed: set[str], calls: list[Any]
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        """Run the tool calls in order. Returns (results, prepared, prepare_order error code)."""
        results: list[dict[str, Any]] = []
        prepared = False
        failure: str | None = None
        for call in calls:
            if call.name in allowed:
                arguments = call.input if isinstance(call.input, dict) else {}
                outcome = await tools.call(call.name, arguments)
            else:
                outcome = ToolResult(False, code="not_found")
            if call.name == "prepare_order":
                if outcome.ok:
                    prepared = True
                elif failure is None:
                    failure = outcome.code or "internal"
            results.append(_tool_result(call.id, outcome))
        return results, prepared, None if prepared else failure


def _tool_result(tool_use_id: str, outcome: ToolResult) -> dict[str, Any]:
    if outcome.ok:
        body = json.dumps(outcome.data, ensure_ascii=False, default=str)
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": body}
    code = re.sub(r"[^a-z_]", "", outcome.code or "internal")
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps({"error": code}),
        "is_error": True,
    }
