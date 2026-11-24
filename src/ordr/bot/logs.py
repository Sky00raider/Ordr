"""Logging for the bot. No tokens, phone numbers or message text (TRD section 8).

HTTP libraries log full URLs, and a Telegram URL holds the bot token. So their loggers are set to
WARNING, and a filter also blanks anything that looks like a token or a secret we were given.
"""

import logging
import re
from collections.abc import Iterable

QUIET = ("httpx", "httpx2", "httpcore", "anthropic", "mcp", "fastmcp", "uvicorn.access")
TELEGRAM_URL = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")
BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


class Redact(logging.Filter):
    """Blanks secrets in every record, as a last guard behind never logging them."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in (item.strip() for item in secrets) if len(s) >= 6]

    def clean(self, text: str) -> str:
        text = TELEGRAM_URL.sub("/bot<hidden>", text)
        text = BEARER.sub("Bearer <hidden>", text)
        for secret in self._secrets:
            text = text.replace(secret, "<hidden>")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.clean(record.getMessage())
        record.args = ()
        return True


def configure_logging(secrets: Iterable[str] = ()) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    redact = Redact(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
