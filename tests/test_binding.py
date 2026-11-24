"""What is bound: canonical JSON, cart hash, approval HMAC (PRD section 6, N4)."""

import hashlib
import unicodedata
from dataclasses import replace

import pytest

from ordr.contracts import Cart, CartLine
from ordr.gate.binding import (
    Approval,
    WeakServerKey,
    canonical_json,
    cart_hash,
    sign_approval,
    verify_approval,
)

KEY = "k" * 32
MILK = CartLine("sku_milk", "spin_milk", 2, "Demo milk", "500 ml", 2800)
RICE = CartLine("sku_rice", "spin_rice", 1, "Demo rice", "1 kg", 9000)
CART = Cart(
    lines=(MILK, RICE),
    total_paise=34500,
    address_id="addr_home",
    address_text="12 Demo Street",
    fees_paise={"delivery": 1500},
    all_in_stock=True,
    serviceable=True,
)
APPROVAL = Approval(
    mandate_id="m1",
    cart_hash="a" * 64,
    max_total_paise=37500,
    approver_id="kid1",
    nonce="n1",
    expires_at="2026-12-01T10:00:00.000000Z",
)


def h(cart: Cart = CART, store: str = "demo", **kw) -> str:
    return cart_hash(cart, store=store, **kw)


# ----- canonical JSON


def test_canonical_json_has_sorted_keys_no_spaces_and_utf8():
    assert canonical_json({"b": [1, "ಹಾಲು"], "a": "x"}) == '{"a":"x","b":[1,"ಹಾಲು"]}'.encode()


def test_canonical_json_normalises_to_nfc():
    nfc, nfd = (unicodedata.normalize(form, "café") for form in ("NFC", "NFD"))
    assert nfc != nfd
    assert canonical_json({nfd: nfd}) == canonical_json({nfc: nfc})


@pytest.mark.parametrize("bad", [1.0, True, None, {1: "a"}, {"é": 1, "é": 2}, b"x"])
def test_canonical_json_refuses_anything_ambiguous(bad):
    with pytest.raises(ValueError):
        canonical_json({"v": bad})


# ----- cart hash


def test_cart_hash_is_exactly_what_the_prd_says():
    expected = (
        '{"address_id":"addr_home","address_text":"12 Demo Street","currency":"INR",'
        '"items":[["sku_milk","spin_milk",2],["sku_rice","spin_rice",1]],"store":"demo"}'
    )
    assert h() == hashlib.sha256(expected.encode()).hexdigest()


def test_line_order_does_not_change_the_hash():
    assert h(replace(CART, lines=(RICE, MILK))) == h()


def test_repeated_items_are_merged():
    one = replace(MILK, qty=1)
    assert h(replace(CART, lines=(one, RICE, one))) == h()


def test_unicode_form_of_the_address_does_not_change_the_hash():
    nfc, nfd = (unicodedata.normalize(form, "12 Café Road") for form in ("NFC", "NFD"))
    assert nfc != nfd
    assert h(replace(CART, address_text=nfc)) == h(replace(CART, address_text=nfd))


@pytest.mark.parametrize(
    "changed",
    [
        replace(CART, lines=(replace(MILK, qty=3), RICE)),
        replace(CART, lines=(replace(MILK, sku_id="sku_curd"), RICE)),
        replace(CART, lines=(replace(MILK, spin_id="spin_milk_1l"), RICE)),
        replace(CART, lines=(MILK,)),
        replace(CART, lines=()),
        replace(CART, lines=(MILK, RICE, replace(RICE, sku_id="sku_extra"))),
        replace(CART, address_id="addr_office"),
        replace(CART, address_text="12 Demo Street, Flat 4"),
    ],
)
def test_changing_what_or_where_changes_the_hash(changed):
    assert h(changed) != h()


def test_changing_store_or_currency_changes_the_hash():
    assert len({h(), h(store="other"), h(currency="USD")}) == 3


@pytest.mark.parametrize(
    "changed",
    [
        replace(CART, lines=(replace(MILK, unit_price_paise=9999), RICE)),
        replace(CART, total_paise=36300),
        replace(CART, fees_paise={"delivery": 1500, "rain": 1800}),
        replace(CART, lines=(replace(MILK, name="Renamed", pack="half litre"), RICE)),
    ],
)
def test_changing_a_price_fee_or_name_does_not_change_the_hash(changed):
    assert h(changed) == h()


@pytest.mark.parametrize("qty", [0, -1, 1.0, True, "2"])
def test_quantity_must_be_a_whole_number_above_zero(qty):
    with pytest.raises(ValueError):
        h(replace(CART, lines=(replace(MILK, qty=qty),)))


# ----- approval


def test_signature_verifies_for_the_same_approval():
    signature = sign_approval(KEY, APPROVAL)
    assert len(signature) == 64
    assert verify_approval(KEY, replace(APPROVAL), signature)


@pytest.mark.parametrize(
    "change",
    [
        {"mandate_id": "m2"},
        {"cart_hash": "b" * 64},
        {"max_total_paise": 37501},
        {"approver_id": "kid2"},
        {"nonce": "n2"},
        {"expires_at": "2026-12-01T10:00:00.000001Z"},
    ],
)
def test_signature_fails_if_any_bound_field_differs(change):
    assert not verify_approval(KEY, replace(APPROVAL, **change), sign_approval(KEY, APPROVAL))


def test_text_cannot_be_moved_from_one_field_to_the_next():
    moved = replace(APPROVAL, approver_id="kid1n", nonce="1")
    assert not verify_approval(KEY, moved, sign_approval(KEY, APPROVAL))


def test_signature_fails_with_another_key():
    assert not verify_approval("j" * 32, APPROVAL, sign_approval(KEY, APPROVAL))


@pytest.mark.parametrize("bad", [None, "", "0" * 64, "é", "\ud800", b"x", 1], ids=ascii)
def test_missing_or_malformed_signature_fails(bad):
    assert not verify_approval(KEY, APPROVAL, bad)


@pytest.mark.parametrize(
    "bad",
    [{"nonce": ""}, {"approver_id": None}, {"max_total_paise": None}, {"max_total_paise": 1.0}],
)
def test_incomplete_approval_is_never_signed_or_verified(bad):
    broken = replace(APPROVAL, **bad)
    with pytest.raises(ValueError):
        sign_approval(KEY, broken)
    assert not verify_approval(KEY, broken, sign_approval(KEY, APPROVAL))


@pytest.mark.parametrize("key", ["", " " * 40, "k" * 31, "k" * 31 + "\n"])
def test_empty_or_short_key_is_refused(key):
    with pytest.raises(WeakServerKey):
        sign_approval(key, APPROVAL)
    with pytest.raises(WeakServerKey):
        verify_approval(key, APPROVAL, "0" * 64)


def test_verify_uses_the_constant_time_compare(monkeypatch):
    calls = []

    def spy(a, b):
        calls.append((a, b))
        return False

    monkeypatch.setattr("ordr.gate.binding.hmac.compare_digest", spy)
    assert not verify_approval(KEY, APPROVAL, sign_approval(KEY, APPROVAL))
    assert len(calls) == 1
