"""What an approval is bound to: canonical JSON, cart hash, HMAC (PRD section 6, N4).

The hash covers what is bought and where it goes. The ceiling covers how much. Prices, fees and
product names stay out of the hash.
"""

import hashlib
import hmac
import json
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from ordr.contracts import Cart

CURRENCY = "INR"
MIN_KEY_BYTES = 32


class WeakServerKey(Exception):
    """SERVER_KEY is empty or shorter than 32 bytes. Nothing is signed or verified with it."""


# ----- canonical JSON ----------------------------------------------------------------------


def _normalise(value: Any) -> Any:
    """Text to NFC. Only text, whole numbers, lists and objects are allowed."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, list | tuple):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON keys must be text")
            key = unicodedata.normalize("NFC", key)
            if key in out:
                raise ValueError(f"two keys are the same after normalisation: {key!r}")
            out[key] = _normalise(item)
        return out
    raise ValueError(f"not allowed in canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8 with NFC. The same value always gives the same bytes."""
    return json.dumps(
        _normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# ----- cart hash ---------------------------------------------------------------------------


def cart_hash(cart: Cart, *, store: str, currency: str = CURRENCY) -> str:
    """SHA-256 of store, items, address and currency. Repeated items are merged."""
    merged: dict[tuple[str, str], int] = {}
    for line in cart.lines:
        if not isinstance(line.qty, int) or isinstance(line.qty, bool) or line.qty <= 0:
            raise ValueError("quantity must be a whole number above zero")
        key = (_normalise(line.sku_id), _normalise(line.spin_id))
        merged[key] = merged.get(key, 0) + line.qty
    bound = {
        "store": store,
        "items": [[sku_id, spin_id, qty] for (sku_id, spin_id), qty in sorted(merged.items())],
        "address_id": cart.address_id,
        "address_text": cart.address_text,
        "currency": currency,
    }
    return hashlib.sha256(canonical_json(bound)).hexdigest()


# ----- approval ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    """Everything the kid's approval is bound to. `expires_at` is the text saved in the row."""

    mandate_id: str
    cart_hash: str
    max_total_paise: int
    approver_id: str
    nonce: str
    expires_at: str


def _key_bytes(server_key: str) -> bytes:
    key = server_key.strip().encode("utf-8")
    if len(key) < MIN_KEY_BYTES:
        raise WeakServerKey(f"SERVER_KEY must be at least {MIN_KEY_BYTES} bytes")
    return key


def check_server_key(server_key: str) -> None:
    """Call at start. Raises WeakServerKey."""
    _key_bytes(server_key)


def _message(approval: Approval) -> bytes:
    fields = asdict(approval)
    ceiling = fields["max_total_paise"]
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling <= 0:
        raise ValueError("max_total_paise must be whole paise above zero")
    for name, value in fields.items():
        if name != "max_total_paise" and (not isinstance(value, str) or not value):
            raise ValueError(f"{name} must be non-empty text")
    return canonical_json(fields)


def sign_approval(server_key: str, approval: Approval) -> str:
    """HMAC-SHA256 as hex. Raises on a weak key or an incomplete approval."""
    return hmac.new(_key_bytes(server_key), _message(approval), hashlib.sha256).hexdigest()


def verify_approval(server_key: str, approval: Approval, signature: str | None) -> bool:
    """True only for a signature made over exactly this approval. A weak key still raises.

    Expiry is bound here, not compared with the clock. The final checks do that.
    """
    key = _key_bytes(server_key)
    try:
        message = _message(approval)
    except ValueError:
        return False
    if not isinstance(signature, str):
        return False
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(
        expected.encode("ascii"), signature.encode("utf-8", errors="replace")
    )
