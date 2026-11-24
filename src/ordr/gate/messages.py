"""Every message people get from Ordr, written by code from the outbox payload (TRD section 6).

The model never writes any of this (CLAUDE.md rule 3). Text comes from the payload only: lines,
totals and address were read from the store, and the rest is a fixed template in English or
Kannada. Kannada wording needs a check by a Kannada speaker on the team.

Rules kept here:
- Money is whole paise, shown as rupees by integer arithmetic. A float is refused.
- Product names are shown as the store gives them, except that whitespace is collapsed (a name
  must not be able to start a new line). Only a receipt cuts a name over 100 characters.
- The safe-phrase is added to kid messages here, at render time. It is never in the payload.
- The body is at most 1024 characters. A read-back, sent-to-kid, kid card or payment message
  never hides or shortens an item line or the address: if it does not fit, MessageTooLong is
  raised, and `check_fits` makes the gate refuse such a cart before it is prepared. Only a
  receipt may drop item lines (with a "more items" line) to fit.
- At most 3 buttons, at most 20 characters each. Duplicate carts get DUPLICATE_LABELS.
- An unknown kind, reason or code raises MissingTemplate. There is no generic fallback.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ordr.contracts import ACTION_ROLES, DUPLICATE_LABELS, LABELS, Action, Role

LANGS = ("kn", "en")
BODY_LIMIT = 1024
MAX_BUTTONS = 3
MAX_LABEL = 20
MAX_NAME = 100  # receipts only
MAX_SAFE_PHRASE = 40  # characters, checked at start
MAX_ORDER_ID = 40  # characters, checked when checkout answers


class MissingTemplate(KeyError):
    """No template for this kind, role, reason, code or language."""


class MessageTooLong(ValueError):
    """The fixed parts alone do not fit in 1024 characters."""


@dataclass(frozen=True)
class Rendered:
    text: str
    buttons: tuple[tuple[Action, str], ...]  # (action, label)
    speak: bool


# ----- fixed wording -----------------------------------------------------------------------

T: dict[str, dict[str, str]] = {
    "en": {
        "deliver": "Deliver to: {address}",
        "more": "{n} more items not shown here.",
        "fees": "Includes delivery and other fees: {fees}.",
        "order_id": "Order ID: {order_id}",
        "phrase": "Safe phrase: {phrase}",
        "one_item": "{n} item",
        "many_items": "{n} items",
        "readback_head": "Please check your order:",
        "total_parent": "Total: {total}. It can go up to {max} if fees change.",
        "dup_parent": "Warning: this same order was placed recently. Tap {confirm} only if you "
        "want it again.",
        "readback_close": "If it is right, tap {confirm}. Your family will then be asked to "
        "approve. To cancel, tap {stop}.",
        "sent_head": "Sent to your family for approval:",
        "sent_close": "Nothing is ordered until they approve. To cancel before that, tap {stop}.",
        "waiting": "Your family's store cart has other things in it. I asked them if I can replace "
        "it. Nothing is ordered yet. To cancel, tap {stop}.",
        "replace": "Your family wants to order, but your store cart already has {items}. Replace "
        "it with their order? {replace} empties your cart first. {not_now} keeps it as it is.",
        "card_head": "Order from your family. Please check it:",
        "total_kid": "Total: {total}. If you approve, up to {max} can be charged, in case fees "
        "change.",
        "dup_kid": "Warning: this matches an order placed recently. Tap {approve} only if you "
        "want two.",
        "card_close": "{approve} places the order at the store. You then pay by UPI. {reject} "
        "cancels it.",
        "rejected_parent": "Your family rejected this order. Nothing was ordered.",
        "rejected_kid": "You rejected this order. Nothing was ordered.",
        "stopped_parent": "Stopped. Nothing was ordered.",
        "stopped_parent_dnp": "Stopped. Your family was told not to pay.",
        "stopped_kid": "Your family stopped this order. Nothing was ordered.",
        "stopped_kid_dnp": "Your family stopped this order after it was placed at the store. "
        "Do not pay.",
        "blocked_head": "Order blocked.",
        "blocked_now": "Store total now: {total}.",
        "blocked_approved": "Approved up to: {max}.",
        "blocked_close": "Nothing was ordered. No money was taken.",
        "blocked_close_money": "No money was taken.",
        "expired_parent": "This order timed out, so nothing was ordered. Please ask again if you "
        "still need it.",
        "expired_kid": "This order timed out or was reset, so it can no longer be approved. "
        "Nothing was ordered.",
        "pay_head": "Your order is placed at the store. Pay to complete it.",
        "pay_total": "To pay: {total}. You approved up to {max}.",
        "pay_sim": "This is a simulated UPI payment. No real money moves.",
        "pay_close": "Tap {pay} and enter your UPI PIN. Tap {dont} to leave it unpaid.",
        "receipt_head": "Paid. Your order is confirmed.",
        "receipt_total": "Paid: {total} (simulated UPI).",
        "unpaid": "The order was not paid, and the store has closed it. No payment was made "
        "(simulated UPI).",
        "review_parent": "I am not sure what happened with this order. It may have been placed at "
        "the store. Your family has been asked to check. Please do not order again until they "
        "have.",
        "review_kid": "Ordr is not sure what happened with this order. It may have been placed "
        "at the store. Check the store app or ask customer care. No new orders until you tap "
        "{reviewed}.",
        "review_no_id": "The order ID is not known.",
    },
    "kn": {
        "deliver": "ತಲುಪಿಸುವ ವಿಳಾಸ: {address}",
        "more": "ಇನ್ನೂ {n} ವಸ್ತುಗಳನ್ನು ಇಲ್ಲಿ ತೋರಿಸಿಲ್ಲ.",
        "fees": "ಇದರಲ್ಲಿ ಡೆಲಿವರಿ ಮತ್ತು ಇತರ ಶುಲ್ಕ {fees} ಸೇರಿದೆ.",
        "order_id": "ಆರ್ಡರ್ ಐಡಿ: {order_id}",
        "phrase": "ಸುರಕ್ಷಿತ ಪದ: {phrase}",
        "one_item": "{n} ವಸ್ತು",
        "many_items": "{n} ವಸ್ತುಗಳು",
        "readback_head": "ದಯವಿಟ್ಟು ನಿಮ್ಮ ಆರ್ಡರ್ ಪರಿಶೀಲಿಸಿ:",
        "total_parent": "ಒಟ್ಟು: {total}. ಶುಲ್ಕ ಬದಲಾದರೆ {max} ವರೆಗೆ ಆಗಬಹುದು.",
        "dup_parent": "ಎಚ್ಚರಿಕೆ: ಇದೇ ಆರ್ಡರ್ ಇತ್ತೀಚೆಗೆ ಮಾಡಲಾಗಿದೆ. ಮತ್ತೆ ಬೇಕಿದ್ದರೆ ಮಾತ್ರ {confirm} ಒತ್ತಿ.",
        "readback_close": "ಸರಿಯಿದ್ದರೆ {confirm} ಒತ್ತಿ. ನಂತರ ನಿಮ್ಮ ಮನೆಯವರ ಒಪ್ಪಿಗೆ ಕೇಳಲಾಗುತ್ತದೆ. ಬೇಡವಾದರೆ {stop} ಒತ್ತಿ.",
        "sent_head": "ಒಪ್ಪಿಗೆಗಾಗಿ ನಿಮ್ಮ ಮನೆಯವರಿಗೆ ಕಳುಹಿಸಲಾಗಿದೆ:",
        "sent_close": "ಅವರು ಒಪ್ಪುವವರೆಗೆ ಏನೂ ಆರ್ಡರ್ ಆಗುವುದಿಲ್ಲ. ಅದಕ್ಕೂ ಮೊದಲು ರದ್ದುಮಾಡಲು {stop} ಒತ್ತಿ.",
        "waiting": "ನಿಮ್ಮ ಮನೆಯವರ ಅಂಗಡಿ ಕಾರ್ಟ್‌ನಲ್ಲಿ ಬೇರೆ ವಸ್ತುಗಳಿವೆ. ಅದನ್ನು ಬದಲಾಯಿಸಲು ಅವರ ಒಪ್ಪಿಗೆ "
        "ಕೇಳಿದ್ದೇನೆ. ಇನ್ನೂ ಏನೂ ಆರ್ಡರ್ ಆಗಿಲ್ಲ. ರದ್ದುಮಾಡಲು {stop} ಒತ್ತಿ.",
        "replace": "ನಿಮ್ಮ ಪೋಷಕರು ಆರ್ಡರ್ ಮಾಡಲು ಬಯಸಿದ್ದಾರೆ, ಆದರೆ ನಿಮ್ಮ ಅಂಗಡಿ ಕಾರ್ಟ್‌ನಲ್ಲಿ ಈಗಾಗಲೇ "
        "{items} ಇವೆ. ಅದನ್ನು ಅವರ ಆರ್ಡರ್‌ನಿಂದ ಬದಲಾಯಿಸಬೇಕೇ? {replace} ಒತ್ತಿದರೆ ನಿಮ್ಮ ಕಾರ್ಟ್ "
        "ಮೊದಲು ಖಾಲಿಯಾಗುತ್ತದೆ. ಹಾಗೆಯೇ ಇರಿಸಲು {not_now} ಒತ್ತಿ.",
        "card_head": "ನಿಮ್ಮ ಪೋಷಕರ ಆರ್ಡರ್. ದಯವಿಟ್ಟು ಪರಿಶೀಲಿಸಿ:",
        "total_kid": "ಒಟ್ಟು: {total}. ಒಪ್ಪಿದರೆ, ಶುಲ್ಕ ಬದಲಾದಾಗ {max} ವರೆಗೆ ಪಾವತಿಸಬೇಕಾಗಬಹುದು.",
        "dup_kid": "ಎಚ್ಚರಿಕೆ: ಇದು ಇತ್ತೀಚೆಗೆ ಮಾಡಿದ ಆರ್ಡರ್‌ನಂತೆಯೇ ಇದೆ. ಎರಡು ಬೇಕಿದ್ದರೆ ಮಾತ್ರ {approve} ಒತ್ತಿ.",
        "card_close": "{approve} ಒತ್ತಿದರೆ ಅಂಗಡಿಯಲ್ಲಿ ಆರ್ಡರ್ ಆಗುತ್ತದೆ. ನಂತರ UPI ಮೂಲಕ ಪಾವತಿಸಿ. "
        "ರದ್ದುಮಾಡಲು {reject} ಒತ್ತಿ.",
        "rejected_parent": "ನಿಮ್ಮ ಮನೆಯವರು ಈ ಆರ್ಡರ್ ತಿರಸ್ಕರಿಸಿದ್ದಾರೆ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        "rejected_kid": "ನೀವು ಈ ಆರ್ಡರ್ ತಿರಸ್ಕರಿಸಿದ್ದೀರಿ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        "stopped_parent": "ನಿಲ್ಲಿಸಲಾಗಿದೆ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        "stopped_parent_dnp": "ನಿಲ್ಲಿಸಲಾಗಿದೆ. ಪಾವತಿಸಬೇಡಿ ಎಂದು ನಿಮ್ಮ ಮನೆಯವರಿಗೆ ಹೇಳಲಾಗಿದೆ.",
        "stopped_kid": "ನಿಮ್ಮ ಪೋಷಕರು ಈ ಆರ್ಡರ್ ನಿಲ್ಲಿಸಿದ್ದಾರೆ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        "stopped_kid_dnp": "ಅಂಗಡಿಯಲ್ಲಿ ಆರ್ಡರ್ ಆದ ನಂತರ ನಿಮ್ಮ ಪೋಷಕರು ಅದನ್ನು ನಿಲ್ಲಿಸಿದ್ದಾರೆ. ಪಾವತಿಸಬೇಡಿ.",
        "blocked_head": "ಆರ್ಡರ್ ತಡೆಹಿಡಿಯಲಾಗಿದೆ.",
        "blocked_now": "ಅಂಗಡಿಯಲ್ಲಿ ಈಗಿನ ಒಟ್ಟು: {total}.",
        "blocked_approved": "ಒಪ್ಪಿದ ಗರಿಷ್ಠ ಮೊತ್ತ: {max}.",
        "blocked_close": "ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ಹಣ ತೆಗೆದುಕೊಂಡಿಲ್ಲ.",
        "blocked_close_money": "ಹಣ ತೆಗೆದುಕೊಂಡಿಲ್ಲ.",
        "expired_parent": "ಈ ಆರ್ಡರ್‌ನ ಸಮಯ ಮುಗಿದಿದೆ, ಆದ್ದರಿಂದ ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ಇನ್ನೂ ಬೇಕಿದ್ದರೆ ಮತ್ತೆ ಕೇಳಿ.",
        "expired_kid": "ಈ ಆರ್ಡರ್‌ನ ಸಮಯ ಮುಗಿದಿದೆ ಅಥವಾ ಮರುಹೊಂದಿಸಲಾಗಿದೆ, ಆದ್ದರಿಂದ ಇನ್ನು ಒಪ್ಪಲು ಸಾಧ್ಯವಿಲ್ಲ. "
        "ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        "pay_head": "ನಿಮ್ಮ ಆರ್ಡರ್ ಅಂಗಡಿಯಲ್ಲಿ ಆಗಿದೆ. ಪೂರ್ಣಗೊಳಿಸಲು ಪಾವತಿಸಿ.",
        "pay_total": "ಪಾವತಿಸಬೇಕಾದ ಮೊತ್ತ: {total}. ನೀವು ಒಪ್ಪಿದ ಗರಿಷ್ಠ: {max}.",
        "pay_sim": "ಇದು ಅನುಕರಣೆ (simulated) UPI ಪಾವತಿ. ನಿಜವಾದ ಹಣ ಹೋಗುವುದಿಲ್ಲ.",
        "pay_close": "{pay} ಒತ್ತಿ UPI ಪಿನ್ ಹಾಕಿ. ಪಾವತಿಸಬಾರದೆಂದರೆ {dont} ಒತ್ತಿ.",
        "receipt_head": "ಪಾವತಿ ಆಗಿದೆ. ಆರ್ಡರ್ ದೃಢಪಟ್ಟಿದೆ.",
        "receipt_total": "ಪಾವತಿಸಿದ್ದು: {total} (ಅನುಕರಣೆ UPI).",
        "unpaid": "ಆರ್ಡರ್‌ಗೆ ಪಾವತಿ ಆಗಿಲ್ಲ, ಮತ್ತು ಅಂಗಡಿ ಅದನ್ನು ಮುಚ್ಚಿದೆ. ಯಾವುದೇ ಪಾವತಿ ಆಗಿಲ್ಲ (ಅನುಕರಣೆ UPI).",
        "review_parent": "ಈ ಆರ್ಡರ್‌ಗೆ ಏನಾಯಿತು ಎಂದು ನನಗೆ ಖಚಿತವಿಲ್ಲ. ಅದು ಅಂಗಡಿಯಲ್ಲಿ ಆಗಿರಬಹುದು. ಪರಿಶೀಲಿಸಲು "
        "ನಿಮ್ಮ ಮನೆಯವರಿಗೆ ಹೇಳಿದ್ದೇನೆ. ಅವರು ಪರಿಶೀಲಿಸುವವರೆಗೆ ಮತ್ತೆ ಆರ್ಡರ್ ಮಾಡಬೇಡಿ.",
        "review_kid": "ಈ ಆರ್ಡರ್‌ಗೆ ಏನಾಯಿತು ಎಂದು Ordr ಗೆ ಖಚಿತವಿಲ್ಲ. ಅದು ಅಂಗಡಿಯಲ್ಲಿ ಆಗಿರಬಹುದು. ಅಂಗಡಿ "
        "ಆ್ಯಪ್ ನೋಡಿ ಅಥವಾ ಗ್ರಾಹಕ ಸೇವೆಯನ್ನು ಕೇಳಿ. {reviewed} ಒತ್ತುವವರೆಗೆ ಹೊಸ ಆರ್ಡರ್ ಇಲ್ಲ.",
        "review_no_id": "ಆರ್ಡರ್ ಐಡಿ ತಿಳಿದಿಲ್ಲ.",
    },
}

# order_failed: code -> {lang: (parent text, kid text)}
FAILED: dict[str, dict[str, tuple[str, str]]] = {
    "not_available": {
        "en": (
            "Some of these items are not available right now, so I did not order anything. "
            "Please ask again, or ask for something else.",
            "Your family's order could not be prepared because an item is not available. "
            "Nothing was ordered.",
        ),
        "kn": (
            "ಈ ಕೆಲವು ವಸ್ತುಗಳು ಈಗ ಲಭ್ಯವಿಲ್ಲ, ಆದ್ದರಿಂದ ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಕೇಳಿ, ಅಥವಾ ಬೇರೆ ವಸ್ತು ಕೇಳಿ.",
            "ನಿಮ್ಮ ಪೋಷಕರ ಆರ್ಡರ್ ಸಿದ್ಧಪಡಿಸಲು ಆಗಲಿಲ್ಲ, ಏಕೆಂದರೆ ಒಂದು ವಸ್ತು ಲಭ್ಯವಿಲ್ಲ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        ),
    },
    "over_cap": {
        "en": (
            "This order is over the spending limit set for your family, so I did not order it.",
            "This order is over the spending limit set up for your family. Nothing was ordered.",
        ),
        "kn": (
            "ಈ ಆರ್ಡರ್ ನಿಮ್ಮ ಕುಟುಂಬಕ್ಕೆ ಹಾಕಿರುವ ಖರ್ಚಿನ ಮಿತಿಗಿಂತ ಹೆಚ್ಚಾಗಿದೆ, ಆದ್ದರಿಂದ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
            "ನಿಮ್ಮ ಪೋಷಕರ ಆರ್ಡರ್ ಕುಟುಂಬಕ್ಕೆ ಹಾಕಿರುವ ಖರ್ಚಿನ ಮಿತಿಗಿಂತ ಹೆಚ್ಚಾಗಿದೆ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        ),
    },
    "store_error": {
        "en": (
            "The store did not answer properly, so nothing was ordered. Please try again in a "
            "few minutes.",
            "The store did not answer properly while preparing your family's order. "
            "Nothing was ordered.",
        ),
        "kn": (
            "ಅಂಗಡಿಯಿಂದ ಸರಿಯಾದ ಉತ್ತರ ಬರಲಿಲ್ಲ, ಆದ್ದರಿಂದ ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ಕೆಲವು ನಿಮಿಷಗಳ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
            "ನಿಮ್ಮ ಪೋಷಕರ ಆರ್ಡರ್ ಸಿದ್ಧಪಡಿಸುವಾಗ ಅಂಗಡಿಯಿಂದ ಸರಿಯಾದ ಉತ್ತರ ಬರಲಿಲ್ಲ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        ),
    },
    "internal": {
        "en": (
            "Something went wrong on our side. Nothing was ordered. Please try again.",
            "Something went wrong on our side while preparing your family's order. "
            "Nothing was ordered.",
        ),
        "kn": (
            "ನಮ್ಮ ಕಡೆ ಏನೋ ತಪ್ಪಾಗಿದೆ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
            "ನಿಮ್ಮ ಪೋಷಕರ ಆರ್ಡರ್ ಸಿದ್ಧಪಡಿಸುವಾಗ ನಮ್ಮ ಕಡೆ ಏನೋ ತಪ್ಪಾಗಿದೆ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        ),
    },
    "order_too_large": {
        "en": (
            "This order has too many items to show in one message, so I did not order it. "
            "Please order fewer items at a time.",
            "Your family's order has too many items to show on one card, so nothing was ordered.",
        ),
        "kn": (
            "ಈ ಆರ್ಡರ್‌ನಲ್ಲಿ ತುಂಬಾ ವಸ್ತುಗಳಿವೆ, ಒಂದು ಸಂದೇಶದಲ್ಲಿ ತೋರಿಸಲು ಆಗುವುದಿಲ್ಲ, ಆದ್ದರಿಂದ ಆರ್ಡರ್ "
            "ಮಾಡಿಲ್ಲ. ದಯವಿಟ್ಟು ಒಮ್ಮೆಗೆ ಕಡಿಮೆ ವಸ್ತುಗಳನ್ನು ಕೇಳಿ.",
            "ನಿಮ್ಮ ಪೋಷಕರ ಆರ್ಡರ್‌ನಲ್ಲಿ ತುಂಬಾ ವಸ್ತುಗಳಿವೆ, ಒಂದು ಕಾರ್ಡ್‌ನಲ್ಲಿ ತೋರಿಸಲು ಆಗುವುದಿಲ್ಲ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        ),
    },
    "not_found": {
        "en": (
            "This order was stopped or changed while I was preparing it. Nothing was ordered.",
            "This order was stopped or changed while it was being prepared. Nothing was ordered.",
        ),
        "kn": (
            "ನಾನು ಸಿದ್ಧಪಡಿಸುತ್ತಿದ್ದಾಗ ಈ ಆರ್ಡರ್ ನಿಲ್ಲಿಸಲಾಯಿತು ಅಥವಾ ಬದಲಾಯಿತು. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
            "ಸಿದ್ಧಪಡಿಸುತ್ತಿದ್ದಾಗ ಈ ಆರ್ಡರ್ ನಿಲ್ಲಿಸಲಾಯಿತು ಅಥವಾ ಬದಲಾಯಿತು. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        ),
    },
    "not_now": {
        "en": (
            "Your family wants to keep their store cart as it is for now. Nothing was ordered.",
            "You kept your store cart as it is. Nothing was ordered.",
        ),
        "kn": (
            "ನಿಮ್ಮ ಮನೆಯವರು ತಮ್ಮ ಅಂಗಡಿ ಕಾರ್ಟ್ ಈಗ ಹಾಗೆಯೇ ಇರಲಿ ಎಂದಿದ್ದಾರೆ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
            "ನೀವು ನಿಮ್ಮ ಅಂಗಡಿ ಕಾರ್ಟ್ ಹಾಗೆಯೇ ಇರಿಸಿದ್ದೀರಿ. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
        ),
    },
}

# blocked: reason -> {lang: text}. The reasons are the ones `actions.py` can give.
BLOCKED: dict[str, dict[str, str]] = {
    "not_approved": {
        "en": "The approval was no longer valid.",
        "kn": "ಒಪ್ಪಿಗೆ ಇನ್ನು ಮಾನ್ಯವಾಗಿರಲಿಲ್ಲ.",
    },
    "bad_signature": {
        "en": "The approval record did not check out.",
        "kn": "ಒಪ್ಪಿಗೆಯ ದಾಖಲೆ ಸರಿಯಾಗಿ ಹೊಂದಲಿಲ್ಲ.",
    },
    "cart_changed": {
        "en": "The store cart is not the one on the card, so nothing was ordered.",
        "kn": "ಅಂಗಡಿ ಕಾರ್ಟ್ ಕಾರ್ಡ್‌ನಲ್ಲಿ ಇದ್ದದ್ದಲ್ಲ, ಆದ್ದರಿಂದ ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
    },
    "over_ceiling": {
        "en": "The price is now above the amount that was approved.",
        "kn": "ಬೆಲೆ ಈಗ ಒಪ್ಪಿದ ಮೊತ್ತಕ್ಕಿಂತ ಹೆಚ್ಚಾಗಿದೆ.",
    },
    "out_of_stock": {
        "en": "An item is out of stock.",
        "kn": "ಒಂದು ವಸ್ತು ಸ್ಟಾಕ್‌ನಲ್ಲಿ ಇಲ್ಲ.",
    },
    "not_serviceable": {
        "en": "The store cannot deliver this order now.",
        "kn": "ಅಂಗಡಿ ಈಗ ಈ ಆರ್ಡರ್ ತಲುಪಿಸಲು ಸಾಧ್ಯವಿಲ್ಲ.",
    },
    "over_cap": {
        "en": "This order is over the spending limit set for the family.",
        "kn": "ಈ ಆರ್ಡರ್ ಕುಟುಂಬಕ್ಕೆ ಹಾಕಿರುವ ಖರ್ಚಿನ ಮಿತಿಗಿಂತ ಹೆಚ್ಚಾಗಿದೆ.",
    },
    "payment_not_allowed": {
        "en": "This payment method is not allowed.",
        "kn": "ಈ ಪಾವತಿ ವಿಧಾನಕ್ಕೆ ಅನುಮತಿ ಇಲ್ಲ.",
    },
    "store_error": {
        "en": "The store did not answer properly.",
        "kn": "ಅಂಗಡಿಯಿಂದ ಸರಿಯಾದ ಉತ್ತರ ಬರಲಿಲ್ಲ.",
    },
    "internal": {
        "en": "Something went wrong on our side.",
        "kn": "ನಮ್ಮ ಕಡೆ ಏನೋ ತಪ್ಪಾಗಿದೆ.",
    },
}

# Reason texts that already say "nothing was ordered", so the closing line must not repeat it.
SAYS_NOTHING_ORDERED = frozenset({"cart_changed"})

BOTH = frozenset({Role.PARENT, Role.KID})
# Who each kind is written for. Rendering any other pair is refused.
KIND_ROLES: dict[str, frozenset[Role]] = {
    "readback": frozenset({Role.PARENT}),
    "waiting_for_kid": frozenset({Role.PARENT}),
    "replace_cart": frozenset({Role.KID}),
    "order_failed": BOTH,
    "kid_card": frozenset({Role.KID}),
    "sent_to_kid": frozenset({Role.PARENT}),
    "rejected": frozenset({Role.PARENT}),
    "stopped": BOTH,
    "blocked": BOTH,
    "expired": BOTH,
    "payment": frozenset({Role.KID}),
    "receipt": BOTH,
    "unpaid": BOTH,
    "needs_review": BOTH,
}


# ----- small helpers -----------------------------------------------------------------------


def _t(lang: str, key: str, **values: object) -> str:
    try:
        return T[lang][key].format(**values)
    except KeyError as exc:
        raise MissingTemplate(f"{lang}/{key}") from exc


def _whole(value: Any, what: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{what} must be a whole number")
    return value


def money(paise: Any, lang: str = "en") -> str:
    """Whole paise as rupees, by integer arithmetic. 34500 is "Rs 345", 34550 is "Rs 345.50"."""
    value = _whole(paise, "money")
    rupees, rest = divmod(value, 100)
    prefix = "ರೂ " if lang == "kn" else "Rs "
    return f"{prefix}{rupees:,}" + (f".{rest:02d}" if rest else "")


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be text")
    return value


def _clean(value: Any, what: str, limit: int | None = None) -> str:
    """One line, whitespace collapsed. Words and characters are kept as given."""
    text = " ".join(_text(value, what).split())
    if limit is not None and len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _line(raw: Mapping[str, Any], lang: str, shorten: bool) -> str:
    qty = _whole(raw["qty"], "qty", minimum=1)
    unit = _whole(raw["unit_price_paise"], "unit price")
    name = _clean(raw["name"], "name", MAX_NAME if shorten else None)
    pack = _clean(raw["pack"], "pack", MAX_NAME if shorten else None)
    return f"{qty} x {name}, {pack}: {money(qty * unit, lang)}"


def _lines(payload: Mapping[str, Any], lang: str, shorten: bool) -> list[str]:
    lines = payload["lines"]
    if not isinstance(lines, Sequence) or isinstance(lines, str) or not lines:
        raise ValueError("lines must be a list")
    return [_line(raw, lang, shorten) for raw in lines]


def label(action: Action, duplicate: bool = False) -> str:
    return (DUPLICATE_LABELS if duplicate else {}).get(action) or LABELS[action]


@dataclass
class _Draft:
    """A message in parts. Only a receipt (`droppable`) may lose item lines to fit."""

    head: str
    items: list[str]
    mid: list[str]
    address: str | None
    end: list[str]
    droppable: bool = False


def _fees(payload: Mapping[str, Any], lang: str) -> list[str]:
    fees = payload.get("fees_paise")
    if not isinstance(fees, Mapping):
        return []
    total = sum(_whole(v, "fee") for v in fees.values())
    return [_t(lang, "fees", fees=money(total, lang))] if total else []


def _assemble(draft: _Draft, lang: str, footer: str | None) -> str:
    def build(shown: int, address: str | None) -> str:
        first = [draft.head, *draft.items[:shown]]
        if shown < len(draft.items):
            first.append(_t(lang, "more", n=len(draft.items) - shown))
        second = list(draft.mid)
        if address is not None:
            second.append(_t(lang, "deliver", address=address))
        sections = [first, second, draft.end, [footer] if footer else []]
        return "\n\n".join("\n".join(part) for part in sections if part)

    if not draft.droppable:  # never hide an item line or shorten the address: refuse instead
        text = build(len(draft.items), draft.address)
        if len(text) > BODY_LIMIT:
            raise MessageTooLong(draft.head[:30])
        return text
    for shown in range(len(draft.items), -1, -1):
        text = build(shown, draft.address)
        if len(text) <= BODY_LIMIT:
            return text
    if draft.address is not None:  # last resort: a very long address
        for width in (300, 150, 80, 40):
            text = build(0, _clean(draft.address, "address", width))
            if len(text) <= BODY_LIMIT:
                return text
    raise MessageTooLong(draft.head[:30])


def _card(
    payload: Mapping[str, Any], lang: str, head: str, total_line: str, droppable: bool = False
) -> _Draft:
    """Lines, total, fees, address: the part every order message shares."""
    return _Draft(
        head=head,
        items=_lines(payload, lang, droppable),
        mid=[total_line, *_fees(payload, lang)],
        address=_clean(payload["address_text"], "address"),
        end=[],
        droppable=droppable,
    )


def _buttons(payload: Mapping[str, Any], role: Role, default: Sequence[Action]) -> list[Action]:
    raw = payload.get("buttons")
    actions = [Action(value) for value in raw] if raw is not None else list(default)
    for action in actions:
        if ACTION_ROLES[action] is not role:
            raise ValueError(f"{action} is not a {role} button")
    return actions


# ----- one builder per kind ----------------------------------------------------------------
# Each takes (payload, role, lang, duplicate) and returns (draft, default buttons). The default
# is used only when the payload has no "buttons" list.


def _readback(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    total = money(p["total_paise"], lang)
    ceiling = money(p["max_total_paise"], lang)
    draft = _card(
        p, lang, _t(lang, "readback_head"), _t(lang, "total_parent", total=total, max=ceiling)
    )
    labels = {"confirm": label(Action.CONFIRM, dup), "stop": label(Action.STOP)}
    if dup:
        draft.end.append(_t(lang, "dup_parent", **labels))
    draft.end.append(_t(lang, "readback_close", **labels))
    return draft, [Action.CONFIRM, Action.STOP]


def _sent_to_kid(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    total = money(p["total_paise"], lang)
    ceiling = money(p["max_total_paise"], lang)
    draft = _card(
        p, lang, _t(lang, "sent_head"), _t(lang, "total_parent", total=total, max=ceiling)
    )
    draft.end.append(_t(lang, "sent_close", stop=label(Action.STOP)))
    return draft, [Action.STOP]


def _kid_card(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    total = money(p["total_paise"], lang)
    ceiling = money(p["max_total_paise"], lang)
    draft = _card(p, lang, _t(lang, "card_head"), _t(lang, "total_kid", total=total, max=ceiling))
    labels = {"approve": label(Action.APPROVE, dup), "reject": label(Action.REJECT)}
    if dup:
        draft.end.append(_t(lang, "dup_kid", **labels))
    draft.end.append(_t(lang, "card_close", **labels))
    return draft, [Action.APPROVE, Action.REJECT]


def _payment(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    total = money(p["total_paise"], lang)
    ceiling = money(p["max_total_paise"], lang)
    draft = _card(p, lang, _t(lang, "pay_head"), _t(lang, "pay_total", total=total, max=ceiling))
    order_id = _clean(p["order_id"], "order id")
    if len(order_id) > MAX_ORDER_ID:
        raise ValueError("order id is too long")
    draft.mid.insert(0, _t(lang, "order_id", order_id=order_id))
    draft.end.append(_t(lang, "pay_sim"))
    draft.end.append(_t(lang, "pay_close", pay=label(Action.PAY), dont=label(Action.DONT_PAY)))
    return draft, [Action.PAY, Action.DONT_PAY]


def _receipt(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    total = _t(lang, "receipt_total", total=money(p["total_paise"], lang))
    draft = _card(p, lang, _t(lang, "receipt_head"), total, droppable=True)
    draft.mid.insert(0, _t(lang, "order_id", order_id=_clean(p["order_id"], "order id")))
    return draft, []


def _plain(text: str, *extra: str) -> _Draft:
    return _Draft(head=text, items=[], mid=[], address=None, end=list(extra))


def _replace_cart(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    n = _whole(p["item_count"], "item_count", minimum=1)
    items = _t(lang, "one_item" if n == 1 else "many_items", n=n)
    text = _t(
        lang,
        "replace",
        items=items,
        replace=label(Action.REPLACE_CART),
        not_now=label(Action.NOT_NOW),
    )
    return _plain(text), [Action.REPLACE_CART, Action.NOT_NOW]


def _waiting(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    return _plain(_t(lang, "waiting", stop=label(Action.STOP))), [Action.STOP]


def _order_failed(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    try:
        parent, kid = FAILED[p["code"]][lang]
    except KeyError as exc:
        raise MissingTemplate(f"order_failed/{p.get('code')}/{lang}") from exc
    return _plain(parent if role is Role.PARENT else kid), []


def _rejected(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    return _plain(_t(lang, "rejected_parent")), []


def _stopped(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    dnp = p.get("do_not_pay") is True
    key = f"stopped_{role.value}" + ("_dnp" if dnp else "")
    draft = _plain(_t(lang, key))
    order_id = p.get("order_id")
    if dnp and order_id:
        draft.end.append(_t(lang, "order_id", order_id=_clean(order_id, "order id")))
    return draft, []


def _blocked(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    try:
        reason = BLOCKED[p["reason"]][lang]
    except KeyError as exc:
        raise MissingTemplate(f"blocked/{p.get('reason')}/{lang}") from exc
    lines = [reason]
    if "total_paise" in p:
        lines.append(_t(lang, "blocked_now", total=money(p["total_paise"], lang)))
    if "max_total_paise" in p:
        lines.append(_t(lang, "blocked_approved", max=money(p["max_total_paise"], lang)))
    close = "blocked_close_money" if p["reason"] in SAYS_NOTHING_ORDERED else "blocked_close"
    draft = _plain(_t(lang, "blocked_head"), "\n".join(lines), _t(lang, close))
    return draft, []


def _expired(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    return _plain(_t(lang, f"expired_{role.value}")), []


def _unpaid(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    draft = _plain(_t(lang, "unpaid"))
    if p.get("order_id"):
        draft.end.append(_t(lang, "order_id", order_id=_clean(p["order_id"], "order id")))
    return draft, []


def _needs_review(p: Mapping[str, Any], role: Role, lang: str, dup: bool) -> tuple[_Draft, list]:
    if role is Role.PARENT:
        return _plain(_t(lang, "review_parent")), []
    text = _t(lang, "review_kid", reviewed=label(Action.REVIEWED))
    known = p.get("order_id")
    id_line = (
        _t(lang, "order_id", order_id=_clean(known, "order id"))
        if known
        else _t(lang, "review_no_id")
    )
    return _plain(text, id_line), [Action.REVIEWED]


BUILDERS = {
    "readback": _readback,
    "waiting_for_kid": _waiting,
    "replace_cart": _replace_cart,
    "order_failed": _order_failed,
    "kid_card": _kid_card,
    "sent_to_kid": _sent_to_kid,
    "rejected": _rejected,
    "stopped": _stopped,
    "blocked": _blocked,
    "expired": _expired,
    "payment": _payment,
    "receipt": _receipt,
    "unpaid": _unpaid,
    "needs_review": _needs_review,
}
assert set(BUILDERS) == set(KIND_ROLES)


# ----- entry -------------------------------------------------------------------------------


def render(
    kind: str,
    role: Role,
    payload: Mapping[str, Any],
    *,
    lang: str,
    phrase: str = "",
) -> Rendered:
    """Text, buttons and the speak flag for one outbox row. Raises on anything unexpected.

    `phrase` is added to kid messages only, and is required for them.
    """
    if lang not in LANGS:
        raise MissingTemplate(f"language {lang}")
    if kind not in BUILDERS or role not in KIND_ROLES[kind]:
        raise MissingTemplate(f"{kind}/{role}")
    footer = None
    if role is Role.KID:
        phrase = " ".join(phrase.split())
        if not phrase:
            raise ValueError("kid messages need the safe-phrase")
        footer = _t(lang, "phrase", phrase=phrase)
    duplicate = payload.get("is_duplicate") is True
    draft, default = BUILDERS[kind](payload, role, lang, duplicate)
    actions = _buttons(payload, role, default)
    buttons = tuple((action, label(action, duplicate)) for action in actions)
    if len(buttons) > MAX_BUTTONS or any(len(text) > MAX_LABEL for _, text in buttons):
        raise ValueError("too many buttons or a label is too long")
    return Rendered(_assemble(draft, lang, footer), buttons, speak=role is Role.PARENT)


def check_fits(
    lines: Sequence[Mapping[str, Any]],
    fees_paise: Mapping[str, int],
    total_paise: int,
    max_total_paise: int,
    address_text: str,
) -> None:
    """Raise MessageTooLong unless every message that shows this cart fits, worst case.

    Worst case: duplicate warning on, longest safe-phrase and longest order ID allowed, and the
    paid total at the ceiling. Read-back and sent-to-kid in both languages, kid card and payment
    in English. Call before the cart is accepted, so no card is ever shortened.
    """
    card = {
        "mandate_id": "0" * 32,
        "lines": list(lines),
        "fees_paise": dict(fees_paise),
        "total_paise": total_paise,
        "max_total_paise": max_total_paise,
        "address_text": address_text,
        "is_duplicate": True,
    }
    phrase = "w" * MAX_SAFE_PHRASE
    for lang in LANGS:
        render("readback", Role.PARENT, card, lang=lang)
        render("sent_to_kid", Role.PARENT, {**card, "buttons": ["stop"]}, lang=lang)
    kid = {**card, "buttons": ["approve", "reject"]}
    render("kid_card", Role.KID, kid, lang="en", phrase=phrase)
    placed = {k: v for k, v in card.items() if k != "fees_paise"} | {
        "total_paise": max_total_paise,
        "order_id": "9" * MAX_ORDER_ID,
        "buttons": ["pay", "dont_pay"],
    }
    render("payment", Role.KID, placed, lang="en", phrase=phrase)
