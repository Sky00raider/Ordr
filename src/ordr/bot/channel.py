"""Small pieces shared by the channel adapters (Telegram and Meta)."""

from ordr.contracts import Action, Button

MAX_BUTTONS = 3  # WhatsApp reply buttons. Telegram would allow more; we keep one rule
MAX_LABEL = 20
MAX_BUTTON_BODY = 1024  # WhatsApp button message body
TELEGRAM_DATA_BYTES = 64  # callback_data limit


class ChannelError(Exception):
    """A send failed. `permanent` means retrying the same message cannot help.

    The message never holds a URL (Telegram's holds the bot token), a number or message text.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def encode_button(button: Button) -> str:
    """The id a button carries back to us: `action:mandate_id`."""
    return f"{button.action.value}:{button.mandate_id}"


def decode_button(data: str) -> tuple[Action, str] | None:
    """Action and mandate id from a button id, or None if it is not one of ours."""
    action_text, sep, mandate_id = data.partition(":")
    if not sep or not mandate_id or len(mandate_id) > 100:
        return None
    try:
        return Action(action_text), mandate_id
    except ValueError:
        return None


def check_buttons(buttons: list[Button], *, id_bytes: int) -> None:
    """Refuse buttons the channel cannot show as given. Nothing is cut short."""
    if not 1 <= len(buttons) <= MAX_BUTTONS:
        raise ChannelError("a message needs 1 to 3 buttons", permanent=True)
    labels = [button.label for button in buttons]
    if len(set(labels)) != len(labels):
        raise ChannelError("button labels must differ", permanent=True)
    for button in buttons:
        if not 1 <= len(button.label) <= MAX_LABEL:
            raise ChannelError("a button label is empty or over 20 characters", permanent=True)
        if len(encode_button(button).encode("utf-8")) > id_bytes:
            raise ChannelError("a button id is too long", permanent=True)
