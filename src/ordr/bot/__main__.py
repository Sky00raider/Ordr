"""`python -m ordr.bot` starts the channel in CHANNEL (meta or telegram).

    python -m ordr.bot             start the bot
    python -m ordr.bot --whoami    Telegram only: print the chat id of whoever messages the bot

Refuses to start if a key the channel needs is empty.
"""

import argparse
import asyncio
import logging
import sys

from ordr.bot.logs import configure_logging
from ordr.bot.runtime import build_runtime, secrets_of
from ordr.bot.settings import BotSettings, StartupError, check_settings
from ordr.bot.telegram import Telegram, whoami

log = logging.getLogger("ordr.bot")


async def run_telegram(settings: BotSettings) -> None:
    channel = Telegram(settings.telegram_bot_token)
    runtime = build_runtime(settings, channel)
    runtime.start()
    log.info("telegram bot polling. Press Ctrl+C to stop")
    try:
        await channel.poll(runtime.pipeline.handle)
    finally:
        await runtime.stop()
        await channel.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ordr.bot")
    parser.add_argument("--whoami", action="store_true", help="Telegram: print chat ids")
    args = parser.parse_args(argv)
    settings = BotSettings()
    configure_logging(secrets_of(settings))
    try:
        if args.whoami:
            if not settings.telegram_bot_token.strip():
                raise StartupError("these settings are empty: TELEGRAM_BOT_TOKEN")
        else:
            check_settings(settings)
    except StartupError as exc:
        print(f"The bot cannot start: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    try:
        if args.whoami:
            asyncio.run(whoami(settings.telegram_bot_token))
        elif settings.channel == "telegram":
            asyncio.run(run_telegram(settings))
        else:
            import uvicorn  # only the WhatsApp webhook needs a web server

            from ordr.bot.app import HOST, PORT, create_app

            uvicorn.run(create_app(settings), host=HOST, port=PORT, workers=1, access_log=False)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
