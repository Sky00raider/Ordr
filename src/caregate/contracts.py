"""Shared types. Everyone codes against this file.

Changing it affects all four people, so agree first. Spec: PRD sections 5 and 6.
Money is always whole paise (int).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class State(StrEnum):
    PREPARED = "PREPARED"
    PARENT_CONFIRMED = "PARENT_CONFIRMED"
    HANDOFF_WARNED = "HANDOFF_WARNED"
    APPROVED = "APPROVED"
    COMMITTING = "COMMITTING"
    PLACED_PENDING_PAYMENT = "PLACED_PENDING_PAYMENT"
    # Ends
    PAID = "PAID"
    UNPAID = "UNPAID"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    ABORTED = "ABORTED"
    BLOCKED = "BLOCKED"
    HANDED_OFF = "HANDED_OFF"
    NEEDS_REVIEW = "NEEDS_REVIEW"


# Anything not listed is refused. Each move is one atomic database update.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.PREPARED: frozenset({State.PARENT_CONFIRMED, State.ABORTED, State.EXPIRED}),
    State.PARENT_CONFIRMED: frozenset(
        {
            State.APPROVED,
            State.HANDOFF_WARNED,
            State.HANDED_OFF,
            State.REJECTED,
            State.ABORTED,
            State.EXPIRED,
        }
    ),
    State.HANDOFF_WARNED: frozenset(
        {State.HANDED_OFF, State.PARENT_CONFIRMED, State.ABORTED, State.EXPIRED}
    ),
    State.APPROVED: frozenset({State.COMMITTING, State.BLOCKED, State.ABORTED, State.EXPIRED}),
    State.COMMITTING: frozenset({State.PLACED_PENDING_PAYMENT, State.NEEDS_REVIEW}),
    State.PLACED_PENDING_PAYMENT: frozenset({State.PAID, State.UNPAID, State.NEEDS_REVIEW}),
}


class Role(StrEnum):
    PARENT = "parent"
    KID = "kid"


class Action(StrEnum):
    """Button presses. Handled by the webhook, never by the model."""

    CONFIRM = "confirm"
    STOP = "stop"
    APPROVE = "approve"
    OPEN_IN_STORE = "open_in_store"
    REJECT = "reject"
    PAY = "pay"
    DONT_PAY = "dont_pay"
    REPLACE_CART = "replace_cart"
    NOT_NOW = "not_now"
    FILL_CART = "fill_cart"
    CANCEL_FILL = "cancel_fill"
    REVIEWED = "reviewed"


ACTION_ROLES: dict[Action, Role] = {
    action: Role.PARENT if action in (Action.CONFIRM, Action.STOP) else Role.KID
    for action in Action
}

# WhatsApp limits: 3 buttons, 20 characters each (verify on the chosen provider).
LABELS: dict[Action, str] = {
    Action.CONFIRM: "Confirm",
    Action.STOP: "Stop",
    Action.APPROVE: "Approve and pay",
    Action.OPEN_IN_STORE: "Open in store",
    Action.REJECT: "Reject",
    Action.PAY: "Pay",
    Action.DONT_PAY: "Don't pay",
    Action.REPLACE_CART: "Replace",
    Action.NOT_NOW: "Not now",
    Action.FILL_CART: "Fill cart",
    Action.CANCEL_FILL: "Cancel",
    Action.REVIEWED: "I have checked it",
}
DUPLICATE_LABELS: dict[Action, str] = {
    Action.CONFIRM: "Order again",
    Action.APPROVE: "Approve duplicate",
}


@dataclass(frozen=True)
class OrderItem:
    """What the assistant may pass to prepare_order. No price, no total, no address."""

    sku_id: str
    spin_id: str
    qty: int
    heard_as: str


@dataclass(frozen=True)
class CartLine:
    sku_id: str
    spin_id: str
    qty: int
    name: str
    pack: str
    unit_price_paise: int


@dataclass(frozen=True)
class Cart:
    """Read back from the store, never from the assistant."""

    lines: tuple[CartLine, ...]
    total_paise: int
    address_id: str
    address_text: str
    fees_paise: dict[str, int] = field(default_factory=dict)
    all_in_stock: bool = False
    serviceable: bool = False


@dataclass(frozen=True)
class Button:
    action: Action
    mandate_id: str
    label: str


@dataclass(frozen=True)
class Incoming:
    """One verified message from the channel. `number` comes from the provider."""

    message_id: str
    number: str
    text: str | None = None
    audio: bytes | None = None
    audio_type: str | None = None
    action: Action | None = None
    mandate_id: str | None = None


class Channel(Protocol):
    """WhatsApp (Meta or Twilio) or Telegram. Keep provider code behind this."""

    async def send_text(self, number: str, text: str) -> None: ...

    async def send_voice(self, number: str, audio: bytes, audio_type: str) -> None: ...

    async def send_buttons(self, number: str, text: str, buttons: list[Button]) -> None: ...

    def verify(self, headers: dict[str, str], body: bytes) -> bool: ...

    async def parse(self, body: bytes) -> list[Incoming]: ...
