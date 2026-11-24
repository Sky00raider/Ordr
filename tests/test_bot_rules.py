"""Rules for the bot process itself: what it must never import, hold, log or start without."""

import ast
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from ordr.bot.logs import Redact, configure_logging
from ordr.bot.settings import BotSettings, StartupError, check_settings

BOT_DIR = Path(__file__).resolve().parents[1] / "src" / "ordr" / "bot"


def good(**changes) -> BotSettings:
    values = dict(
        internal_token="i" * 40,
        parent_number="1000000001",
        kid_number="2000000002",
        anthropic_api_key="sk-test",
        channel="telegram",
        telegram_bot_token="123:abc",
        _env_file=None,
    )
    return BotSettings(**{**values, **changes})


def meta_ok(**changes) -> BotSettings:
    values = dict(
        channel="meta",
        meta_token="t",
        meta_app_secret="s",
        meta_phone_id="1",
        meta_verify_token="v",
    )
    return good(**{**values, **changes})


def test_the_bot_never_imports_the_gate_or_the_signing_key():
    code = (
        "import sys, ordr.bot.app, ordr.bot.__main__, ordr.bot.runtime, ordr.bot.telegram\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('ordr.gate', 'ordr.store')))\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_bot_source_file_imports_the_gate_or_names_the_signing_key():
    for path in BOT_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("ordr.gate", "ordr.store")), path
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith(("ordr.gate", "ordr.store")) for a in node.names)
        assert "server_key" not in source.lower(), path.name


def test_bot_settings_has_no_signing_key_field():
    assert not [name for name in BotSettings.model_fields if "server" in name or "safe" in name]


def test_valid_settings_start_for_each_channel():
    check_settings(good())
    check_settings(meta_ok())


@pytest.mark.parametrize(
    "name",
    ["internal_token", "parent_number", "kid_number", "anthropic_api_key", "telegram_bot_token"],
)
def test_telegram_refuses_to_start_when_a_needed_key_is_empty(name):
    with pytest.raises(StartupError) as info:
        check_settings(good(**{name: "  "}))
    assert name.upper() in str(info.value)


@pytest.mark.parametrize(
    "name",
    [
        "internal_token",
        "anthropic_api_key",
        "meta_token",
        "meta_app_secret",
        "meta_phone_id",
        "meta_verify_token",
    ],
)
def test_meta_refuses_to_start_when_a_needed_key_is_empty(name):
    with pytest.raises(StartupError) as info:
        check_settings(meta_ok(**{name: ""}))
    assert name.upper() in str(info.value)


def test_the_error_names_the_setting_and_never_its_value():
    with pytest.raises(StartupError) as info:
        check_settings(meta_ok(meta_app_secret="", meta_token="supersecretvalue"))
    assert "supersecretvalue" not in str(info.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"parent_number": "+911234567890"},
        {"kid_number": "abc"},
        {"kid_number": "1000000001"},
        {"parent_lang": "hi"},
        {"channel": "sms"},
    ],
)
def test_bad_numbers_language_or_channel_are_refused(changes):
    with pytest.raises(StartupError):
        check_settings(good(**changes))


def test_the_telegram_token_in_a_url_is_blanked_in_every_log_line(caplog):
    token = "123456789:AAH-secret_token_value_abc"
    redact = Redact([token, "app-secret-xyz"])
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        "f",
        1,
        'HTTP Request: POST https://api.telegram.org/bot%s/sendMessage "200"',
        (token,),
        None,
    )
    redact.filter(record)
    assert token not in record.getMessage() and "bot123456789" not in record.getMessage()
    other = logging.LogRecord(
        "x", logging.INFO, "f", 1, "key app-secret-xyz and Bearer abc.def", (), None
    )
    redact.filter(other)
    assert "app-secret-xyz" not in other.getMessage() and "abc.def" not in other.getMessage()


def test_http_libraries_are_set_to_warning_so_urls_are_not_logged():
    configure_logging(["secret-value-1"])
    for name in ("httpx", "httpx2", "httpcore", "anthropic", "uvicorn.access"):
        assert logging.getLogger(name).level == logging.WARNING


def test_no_bot_code_logs_message_text_or_numbers():
    """A crude guard: log calls in the bot never name text, number or token variables."""
    banned = ("text", "number", "token", "body", "incoming", "inc.", "message.text")
    for path in BOT_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "log"
                and node.func.attr in {"debug", "info", "warning", "error", "exception", "critical"}
            ):
                for arg in node.args[1:]:
                    shown = ast.unparse(arg)
                    assert not any(word == shown or shown.startswith(word) for word in banned), (
                        path.name,
                        node.lineno,
                        shown,
                    )
                    assert node.func.attr != "exception"
