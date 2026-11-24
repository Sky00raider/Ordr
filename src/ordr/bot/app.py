"""Webhook for WhatsApp (Meta). Parent text goes to the model. Buttons go to the gate.

Order of work for every request, no exceptions:
1. No webhook at all unless META_APP_SECRET is set (403).
2. Body size cap, then X-Hub-Signature-256 on the raw body in constant time. Fail: 403.
3. Reply 200 at once. Everything else runs in the background (`pipeline.py`):
   drop strangers and repeated message IDs; a press goes to the gate, never the model;
   the kid's text is "log" or a fixed reply; the parent's text goes to the model.

Binds to 127.0.0.1:8003 only. Reach it through the tunnel (see CONTRIBUTING.md).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from ordr.bot.meta import SIGNATURE_HEADER, Meta
from ordr.bot.pipeline import Pipeline
from ordr.bot.runtime import Runtime, build_runtime
from ordr.bot.settings import BotSettings
from ordr.contracts import Incoming

log = logging.getLogger("ordr.bot.app")

HOST = "127.0.0.1"
PORT = 8003
MAX_BODY_BYTES = 256 * 1024  # a payload with a few messages is a few KB


def _forbidden() -> Response:
    return JSONResponse({"detail": "forbidden"}, status_code=403)


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The raw body, or None if it is over `limit` bytes. Stops reading at the limit."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _process(pipeline: Pipeline, messages: list[Incoming]) -> None:
    """Runs after the 200 is sent. Messages are queued in arrival order, then run together."""
    await asyncio.gather(*(pipeline.handle(message) for message in messages))


def create_app(
    settings: BotSettings | None = None,
    *,
    channel: Meta | None = None,
    pipeline: Pipeline | None = None,
) -> FastAPI:
    """`channel` and `pipeline` are for tests. Otherwise they are built when the app starts."""
    settings = settings or BotSettings()
    meta = channel or Meta(
        token=settings.meta_token,
        app_secret=settings.meta_app_secret,
        phone_id=settings.meta_phone_id,
        verify_token=settings.meta_verify_token,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime: Runtime | None = None
        if app.state.pipeline is None and meta.can_receive:
            runtime = build_runtime(settings, meta)
            app.state.pipeline = runtime.pipeline
            runtime.start()
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.stop()
            with suppress(Exception):
                await meta.close()

    app = FastAPI(
        title="Ordr webhook", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.pipeline = pipeline
    app.state.meta = meta

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/webhook")
    def verify_subscription(request: Request) -> Response:
        """Meta's handshake when the callback URL is saved in the app dashboard."""
        params = request.query_params
        if not meta.can_receive or not meta.check_verify_token(
            params.get("hub.mode"), params.get("hub.verify_token")
        ):
            return _forbidden()
        challenge = params.get("hub.challenge")
        if challenge is None:
            return _forbidden()
        return PlainTextResponse(challenge)

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> Response:
        if not meta.can_receive or SIGNATURE_HEADER not in request.headers:
            return _forbidden()
        body = await _read_capped(request, MAX_BODY_BYTES)
        if body is None:
            return JSONResponse({"detail": "too large"}, status_code=413)
        if not meta.verify(dict(request.headers), body):
            return _forbidden()
        messages = await meta.parse(body)
        running: Any = app.state.pipeline
        if messages and running is not None:
            background.add_task(_process, running, messages)
        elif messages:
            log.error("a signed message arrived but the bot is not running")
        return JSONResponse({"ok": True})

    return app


app = create_app()
