"""Puts the bot's parts together for one channel. Used by `__main__` (Telegram) and `app` (Meta)."""

import asyncio
import contextlib
from dataclasses import dataclass

import anthropic

from ordr.bot.gate_client import HttpGate
from ordr.bot.llm import Llm
from ordr.bot.mcp_tools import CallLog, McpTools, Tools
from ordr.bot.outbox import Outbox
from ordr.bot.pipeline import Pipeline
from ordr.bot.seen import SeenStore
from ordr.bot.settings import BotSettings
from ordr.contracts import Channel


@dataclass
class Runtime:
    pipeline: Pipeline
    outbox: Outbox
    gate: HttpGate
    seen: SeenStore
    _task: asyncio.Task | None = None

    def start(self) -> None:
        """Start sending the gate's outbox. Call from inside the running event loop."""
        self._task = asyncio.create_task(self.outbox.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.gate.close()
        self.seen.close()


def build_runtime(settings: BotSettings, channel: Channel) -> Runtime:
    gate = HttpGate(settings.gate_url, settings.internal_token)
    calls = CallLog(settings.mcp_log_path)

    def tools_for(number: str) -> Tools:
        return McpTools(settings.gate_url, settings.internal_token, number, calls)

    llm = Llm(
        client=anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=1),
        model=settings.llm_model,
        tools_for=tools_for,
        lang=settings.parent_lang,
    )
    seen = SeenStore(settings.bot_db_path)
    pipeline = Pipeline(
        parent_number=settings.parent_number,
        kid_number=settings.kid_number,
        parent_lang=settings.parent_lang,
        seen=seen,
        gate=gate,
        channel=channel,
        llm=llm,
    )
    return Runtime(pipeline=pipeline, outbox=Outbox(gate, channel), gate=gate, seen=seen)


def secrets_of(settings: BotSettings) -> list[str]:
    """Values to blank if one ever reaches a log line."""
    return [
        settings.internal_token,
        settings.telegram_bot_token,
        settings.meta_token,
        settings.meta_app_secret,
        settings.meta_verify_token,
        settings.anthropic_api_key,
    ]
