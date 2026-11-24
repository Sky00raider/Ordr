"""Settings for the bot process only.

The bot never imports `ordr.gate` and never holds the gate's signing key. It has its own settings
class so a new gate-only setting can never leak in by accident. `.env` is shared with the gate,
so unknown names are ignored.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

CHANNELS = ("meta", "telegram")
LANGS = ("kn", "en")
MAX_NUMBER_DIGITS = 20


class StartupError(Exception):
    """The bot must not start. The message names settings, never their values."""


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    internal_token: str = ""
    gate_url: str = "http://127.0.0.1:8002"
    channel: str = "meta"  # meta or telegram
    parent_number: str = ""
    kid_number: str = ""
    parent_lang: str = "kn"  # kn or en. The kid always gets English
    bot_db_path: Path = Path("bot.db")
    mcp_log_path: Path = Path("mcp_calls.jsonl")

    telegram_bot_token: str = ""

    meta_token: str = ""
    meta_app_secret: str = ""
    meta_phone_id: str = ""
    meta_verify_token: str = ""

    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"


def _digits_only(value: str) -> bool:
    return value.isascii() and value.isdigit() and len(value) <= MAX_NUMBER_DIGITS


def check_settings(settings: BotSettings) -> None:
    """Refuse to start if a key the chosen channel needs is empty or a setting is unusable."""
    if settings.channel not in CHANNELS:
        raise StartupError("CHANNEL must be meta or telegram")
    needed = {
        "INTERNAL_TOKEN": settings.internal_token,
        "PARENT_NUMBER": settings.parent_number,
        "KID_NUMBER": settings.kid_number,
        "ANTHROPIC_API_KEY": settings.anthropic_api_key,
        "LLM_MODEL": settings.llm_model,
    }
    if settings.channel == "meta":
        needed |= {
            "META_TOKEN": settings.meta_token,
            "META_APP_SECRET": settings.meta_app_secret,
            "META_PHONE_ID": settings.meta_phone_id,
            "META_VERIFY_TOKEN": settings.meta_verify_token,
        }
    else:
        needed["TELEGRAM_BOT_TOKEN"] = settings.telegram_bot_token
    empty = [name for name, value in needed.items() if not value.strip()]
    if empty:
        raise StartupError(f"these settings are empty: {', '.join(empty)}")
    parent, kid = settings.parent_number.strip(), settings.kid_number.strip()
    if not (_digits_only(parent) and _digits_only(kid)):
        raise StartupError("PARENT_NUMBER and KID_NUMBER must be digits only")
    if parent == kid:
        raise StartupError("PARENT_NUMBER and KID_NUMBER must be different")
    if settings.parent_lang not in LANGS:
        raise StartupError("PARENT_LANG must be kn or en")
