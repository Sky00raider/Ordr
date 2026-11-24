"""Webhook for WhatsApp (Meta or Vonage sandbox). Text goes to the model, buttons to the gate.

Order of work for every request, no exceptions:
1. No webhook at all unless META_APP_SECRET is set (403).
2. Body size cap, then X-Hub-Signature-256 on the raw body in constant time. Fail: 403.
3. Reply 200 at once. Everything else runs in the background (`pipeline.py`):
   drop strangers and repeated message IDs; a press goes to the gate, never the model;
   the kid's text is "log" or a fixed reply; the parent's text goes to the model.

With CHANNEL=vonage the same steps run on POST /vonage/inbound (a signed JWT instead of an HMAC
header, see `vonage.py`) and /vonage/status (checked, then only failures are logged). Those two
routes exist only then. In that mode /webhook is built with no Meta secret, so it answers 403.

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
from ordr.bot.vonage import AUTH_HEADER, Vonage
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
    channel: Meta | Vonage | None = None,
    pipeline: Pipeline | None = None,
) -> FastAPI:
    """`channel` and `pipeline` are for tests. Otherwise they are built when the app starts."""
    settings = settings or BotSettings()
    use_vonage = isinstance(channel, Vonage) or (channel is None and settings.channel == "vonage")
    vonage: Vonage | None = None
    if use_vonage:
        vonage = (
            channel
            if isinstance(channel, Vonage)
            else Vonage(
                api_key=settings.vonage_api_key,
                api_secret=settings.vonage_api_secret,
                signature_secret=settings.vonage_signature_secret,
                sender=settings.vonage_from,
            )
        )
        # No Meta secret in this mode, so /webhook stays shut even if one is left in .env.
        meta = Meta(token="", app_secret="", phone_id="", verify_token="")
        active: Meta | Vonage = vonage
    else:
        meta = (
            channel
            if isinstance(channel, Meta)
            else Meta(
                token=settings.meta_token,
                app_secret=settings.meta_app_secret,
                phone_id=settings.meta_phone_id,
                verify_token=settings.meta_verify_token,
            )
        )
        active = meta

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime: Runtime | None = None
        if app.state.pipeline is None and active.can_receive:
            runtime = build_runtime(settings, active)
            app.state.pipeline = runtime.pipeline
            runtime.start()
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.stop()
            with suppress(Exception):
                await meta.close()
            if vonage is not None:
                with suppress(Exception):
                    await vonage.close()

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

    if vonage is not None:
        _add_vonage_routes(app, vonage)

    return app


def _add_vonage_routes(app: FastAPI, vonage: Vonage) -> None:
    async def checked_body(request: Request) -> bytes | Response:
        """The raw body once the JWT passes, else the 403 or 413 to send back."""
        if not vonage.can_receive or AUTH_HEADER not in request.headers:
            return _forbidden()
        body = await _read_capped(request, MAX_BODY_BYTES)
        if body is None:
            return JSONResponse({"detail": "too large"}, status_code=413)
        if not vonage.verify(dict(request.headers), body):
            return _forbidden()
        return body

    @app.post("/vonage/inbound")
    async def vonage_inbound(request: Request, background: BackgroundTasks) -> Response:
        body = await checked_body(request)
        if isinstance(body, Response):
            return body
        messages = await vonage.parse(body)
        running: Any = app.state.pipeline
        if messages and running is not None:
            background.add_task(_process, running, messages)
        elif messages:
            log.error("a signed message arrived but the bot is not running")
        return JSONResponse({"ok": True})

    @app.post("/vonage/status")
    async def vonage_status(request: Request) -> Response:
        body = await checked_body(request)
        if isinstance(body, Response):
            return body
        vonage.log_status(body)
        return JSONResponse({"ok": True})


app = create_app()
