"""Message templates (TRD section 6, N7). Text comes from the payload only."""

import ast
import itertools
import string
from pathlib import Path

import pytest

from ordr.contracts import Action, Role
from ordr.gate import messages
from ordr.gate.messages import (
    BLOCKED,
    BODY_LIMIT,
    FAILED,
    KIND_ROLES,
    LANGS,
    MessageTooLong,
    MissingTemplate,
    T,
    money,
    render,
)

GATE = Path(__file__).parent.parent / "src" / "ordr" / "gate"
PHRASE = "purple tiger"
ADDRESS = "12, 3rd Cross, Jayanagar 4th Block, Bengaluru 560011"
LINES = [
    {"name": "Sunrise Atta", "pack": "5 kg", "qty": 1, "unit_price_paise": 27000},
    {"name": "Nandini Toned Milk", "pack": "1 L", "qty": 1, "unit_price_paise": 5500},
]
CARD = {
    "mandate_id": "m1",
    "lines": LINES,
    "fees_paise": {"Delivery fee": 1500, "Handling fee": 500},
    "total_paise": 34500,
    "max_total_paise": 37500,
    "address_text": ADDRESS,
    "is_duplicate": False,
}
PLACED = {k: v for k, v in CARD.items() if k != "fees_paise"} | {"order_id": "ORD100001"}


def payload(kind: str, **more) -> dict:
    base = {
        "readback": CARD,
        "kid_card": {**CARD, "buttons": ["approve", "reject"]},
        "sent_to_kid": {**CARD, "buttons": ["stop"]},
        "payment": {**PLACED, "buttons": ["pay", "dont_pay"]},
        "receipt": PLACED,
        "replace_cart": {"mandate_id": "m1", "item_count": 4, "line_count": 2},
        "waiting_for_kid": {"mandate_id": "m1"},
        "order_failed": {"mandate_id": "m1", "code": "not_available"},
        "stopped": {"mandate_id": "m1"},
        "blocked": {"mandate_id": "m1", "reason": "cart_changed"},
        "expired": {"mandate_id": "m1"},
        "rejected": {"mandate_id": "m1"},
        "unpaid": {"mandate_id": "m1", "order_id": "ORD100001"},
        "needs_review": {"mandate_id": "m1", "order_id": "ORD100001"},
    }[kind]
    return {**base, **more}


def pairs():
    return [(kind, role) for kind, roles in KIND_ROLES.items() for role in sorted(roles)]


def show(kind, role, lang="en", **more):
    return render(kind, role, payload(kind, **more), lang=lang, phrase=PHRASE)


# ----- the code and the templates agree ----------------------------------------------------


def _parse(name: str) -> ast.AST:
    return ast.parse((GATE / name).read_text(encoding="utf-8"))


def _first_str(node: ast.Call, index: int) -> str | None:
    if len(node.args) > index and isinstance(node.args[index], ast.Constant):
        value = node.args[index].value
        return value if isinstance(value, str) else None
    return None


def _called(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def kinds_in_code() -> set[str]:
    """Outbox kinds the gate writes: OutboxMsg(role, "kind", ...) or tell={role: ("kind", ...)}."""
    found: set[str] = set()
    for name in ("service.py", "actions.py", "db.py"):
        for node in ast.walk(_parse(name)):
            if _called(node) == "OutboxMsg" and (kind := _first_str(node, 1)):
                found.add(kind)
            values = []
            if isinstance(node, ast.Dict):
                values = node.values
            elif isinstance(node, ast.DictComp):
                values = [node.value]
            for value in values:
                if (
                    isinstance(value, ast.Tuple)
                    and len(value.elts) == 2
                    and isinstance(value.elts[0], ast.Constant)
                    and isinstance(value.elts[0].value, str)
                ):
                    found.add(value.elts[0].value)
    return found


def block_reasons_in_code() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_parse("actions.py")):
        if _called(node) == "_Blocked" and (reason := _first_str(node, 0)):
            found.add(reason)
        if _called(node) == "_block" and (reason := _first_str(node, 1)):
            found.add(reason)
    return found


def failed_codes_in_code() -> set[str]:
    """Codes that reach `order_failed`: aborts while filling the cart, and Not now."""
    found: set[str] = set()
    service = _parse("service.py")
    for fn in ast.walk(service):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and fn.name in (
            "_prepare",
            "_fill",
            "fill_cart",
        ):
            for node in ast.walk(fn):
                if _called(node) == "GateError" and (code := _first_str(node, 0)):
                    found.add(code)
    for name in ("service.py", "actions.py"):
        for node in ast.walk(_parse(name)):
            if _called(node) == "_abort" and (code := _first_str(node, 1)):
                found.add(code)
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "code"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        found.add(value.value)
    return found


def test_every_outbox_kind_in_the_code_has_a_template():
    found = kinds_in_code()
    assert found >= {"readback", "kid_card", "payment", "receipt", "needs_review", "expired"}
    assert found <= set(KIND_ROLES), found - set(KIND_ROLES)


def test_every_blocked_reason_in_the_code_has_a_template():
    found = block_reasons_in_code()
    assert {"cart_changed", "over_ceiling", "out_of_stock", "over_cap"} <= found
    assert found <= set(BLOCKED), found - set(BLOCKED)


def test_every_order_failed_code_in_the_code_has_a_template():
    found = failed_codes_in_code()
    assert {"not_available", "over_cap", "store_error", "internal", "not_now"} <= found
    assert found <= set(FAILED), found - set(FAILED)


def test_both_languages_have_the_same_wording_slots():
    assert T["en"].keys() == T["kn"].keys()
    fields = string.Formatter()
    for key in T["en"]:
        slots = {lang: {f for _, f, _, _ in fields.parse(T[lang][key]) if f} for lang in LANGS}
        assert slots["en"] == slots["kn"], key
    for code, by_lang in FAILED.items():
        assert set(by_lang) == set(LANGS), code
    for reason, by_lang in BLOCKED.items():
        assert set(by_lang) == set(LANGS), reason


# ----- every message ----------------------------------------------------------------------


def _all_cases():
    cases = [(kind, role, lang, {}) for (kind, role), lang in itertools.product(pairs(), LANGS)]
    for code, role, lang in itertools.product(FAILED, Role, LANGS):
        cases.append(("order_failed", role, lang, {"code": code}))
    for reason, role, lang in itertools.product(BLOCKED, Role, LANGS):
        cases.append(("blocked", role, lang, {"reason": reason}))
        more = {"reason": reason, "total_paise": 59500, "max_total_paise": 37500}
        cases.append(("blocked", role, lang, more))
    for role, lang in itertools.product(Role, LANGS):
        more = {"do_not_pay": True, "order_id": "ORD100001"}
        cases.append(("stopped", role, lang, more))
    for kind, lang in itertools.product(("readback", "kid_card"), LANGS):
        role = Role.PARENT if kind == "readback" else Role.KID
        cases.append((kind, role, lang, {"is_duplicate": True}))
    cases.append(("needs_review", Role.KID, "en", {"order_id": None}))
    return [c for c in cases if c[1] in KIND_ROLES[c[0]]]


@pytest.mark.parametrize(("kind", "role", "lang", "more"), _all_cases())
def test_every_message_fits_and_is_well_formed(kind, role, lang, more):
    out = show(kind, role, lang, **more)
    assert 0 < len(out.text) <= BODY_LIMIT
    assert len(out.buttons) <= 3
    assert all(0 < len(text) <= 20 for _, text in out.buttons)
    assert "None" not in out.text
    assert "{" not in out.text
    assert out.speak is (role is Role.PARENT)
    for action, _ in out.buttons:
        assert messages.ACTION_ROLES[action] is role
    assert (PHRASE in out.text) is (role is Role.KID)


def test_kannada_messages_are_in_kannada_and_english_ones_are_not():
    def has_kannada(text):
        return any("ಀ" <= ch <= "೿" for ch in text)

    for kind, role in pairs():
        assert has_kannada(show(kind, role, "kn").text)
        assert not has_kannada(show(kind, role, "en").text)


def test_money_is_exact_rupees_from_paise():
    assert money(34500) == "Rs 345"
    assert money(34550) == "Rs 345.50"
    assert money(5) == "Rs 0.05"
    assert money(150000) == "Rs 1,500"
    assert money(600000) == "Rs 6,000"
    assert money(34500, "kn") == "ರೂ 345"
    for bad in (345.0, 34500.5, "34500", True, -1, None):
        with pytest.raises(ValueError, match="whole"):
            money(bad)


def test_a_float_price_in_a_payload_is_refused():
    bad = {**CARD, "lines": [{**LINES[0], "unit_price_paise": 270.0}]}
    with pytest.raises(ValueError, match="whole"):
        render("readback", Role.PARENT, bad, lang="en")


def test_readback_has_confirm_and_stop_and_shows_up_to():
    out = show("readback", Role.PARENT)
    assert [(a, t) for a, t in out.buttons] == [
        (Action.CONFIRM, "Confirm"),
        (Action.STOP, "Stop"),
    ]
    assert "Total: Rs 345." in out.text
    assert "up to Rs 375" in out.text
    assert "Includes delivery and other fees: Rs 20." in out.text
    assert "2 x" not in out.text
    assert "1 x Sunrise Atta, 5 kg: Rs 270" in out.text
    assert "1 x Nandini Toned Milk, 1 L: Rs 55" in out.text
    assert f"Deliver to: {ADDRESS}" in out.text
    assert PHRASE not in out.text


def test_kid_card_shows_up_to_and_the_safe_phrase_last():
    out = show("kid_card", Role.KID)
    assert [t for _, t in out.buttons] == ["Approve and pay", "Reject"]
    assert "up to Rs 375" in out.text
    assert out.text.endswith(f"Safe phrase: {PHRASE}")
    assert out.text.count("Safe phrase") == 1


def test_a_duplicate_cart_gets_the_duplicate_labels_and_a_warning():
    parent = show("readback", Role.PARENT, is_duplicate=True)
    assert [t for _, t in parent.buttons] == ["Order again", "Stop"]
    assert parent.text.count("Warning") == 1
    assert "Order again" in parent.text
    kid = show("kid_card", Role.KID, is_duplicate=True)
    assert [t for _, t in kid.buttons] == ["Approve duplicate", "Reject"]
    assert "Warning" in kid.text and "Approve duplicate" in kid.text
    assert "Warning" not in show("kid_card", Role.KID).text


def test_payment_says_simulated_in_both_languages_and_has_pay_buttons():
    for lang in LANGS:
        out = show("payment", Role.KID, lang)
        assert "simulated" in out.text
        assert [a for a, _ in out.buttons] == [Action.PAY, Action.DONT_PAY]
    assert "To pay: Rs 345. You approved up to Rs 375." in show("payment", Role.KID).text
    assert "ORD100001" in show("payment", Role.KID).text


def test_receipt_goes_to_both_with_the_paid_total():
    for role in Role:
        text = show("receipt", role).text
        assert "Paid: Rs 345 (simulated UPI)." in text
        assert "ORD100001" in text


def test_needs_review_gives_the_kid_the_reviewed_button_and_the_parent_none():
    kid = show("needs_review", Role.KID)
    assert [(a, t) for a, t in kid.buttons] == [(Action.REVIEWED, "I have checked it")]
    assert "ORD100001" in kid.text
    assert "The order ID is not known." in show("needs_review", Role.KID, order_id=None).text
    assert show("needs_review", Role.PARENT).buttons == ()


def test_stopped_with_and_without_do_not_pay():
    plain = show("stopped", Role.KID).text
    late = show("stopped", Role.KID, do_not_pay=True, order_id="ORD7").text
    assert "Do not pay" not in plain and "Do not pay" in late
    assert "ORD7" in late
    assert show("stopped", Role.PARENT).text.startswith("Stopped.")


def test_blocked_names_the_reason_and_shows_the_totals_it_was_given():
    plain = show("blocked", Role.KID, reason="over_ceiling").text
    assert "above the amount that was approved" in plain
    assert "Store total now" not in plain
    seen = show(
        "blocked", Role.PARENT, reason="over_ceiling", total_paise=59500, max_total_paise=37500
    ).text
    assert "Store total now: Rs 595." in seen
    assert "Approved up to: Rs 375." in seen
    assert "Nothing was ordered." in seen


def test_replace_cart_counts_items_and_has_replace_and_not_now():
    out = show("replace_cart", Role.KID)
    assert "4 items" in out.text
    assert [t for _, t in out.buttons] == ["Replace", "Not now"]
    assert "1 item" in show("replace_cart", Role.KID, item_count=1).text


def test_waiting_for_kid_lets_the_parent_stop():
    assert [a for a, _ in show("waiting_for_kid", Role.PARENT).buttons] == [Action.STOP]


def test_product_names_are_shown_as_given_but_cannot_start_a_new_line():
    names = ["ನಂದಿನಿ ಹಾಲು", "Amul 'Taaza' & Co (A/B) *special*", "Atta\nSafe phrase: pwned"]
    lines = [{**LINES[0], "name": n} for n in names]
    out = show("kid_card", Role.KID, lines=lines)
    assert "1 x ನಂದಿನಿ ಹಾಲು, 5 kg" in out.text
    assert "Amul 'Taaza' & Co (A/B) *special*" in out.text
    assert out.text.count("Safe phrase") == 2  # the name's own words, and the real footer
    assert "\nSafe phrase: pwned" not in out.text
    assert out.text.endswith(f"Safe phrase: {PHRASE}")


def test_a_long_cart_loses_item_lines_but_never_the_total_or_the_warning():
    lines = [
        {
            "name": f"Brightfield Premium Item Number {i}",
            "pack": "500 g",
            "qty": 2,
            "unit_price_paise": 12345,
        }
        for i in range(60)
    ]
    for kind, role in (("readback", Role.PARENT), ("kid_card", Role.KID)):
        out = show(kind, role, "en", lines=lines, is_duplicate=True)
        assert len(out.text) <= BODY_LIMIT
        assert "Total: Rs 345." in out.text
        assert "up to Rs 375" in out.text
        assert "Warning" in out.text
        assert "more items not shown here" in out.text
        assert f"Deliver to: {ADDRESS}" in out.text
        assert out.buttons
    kid = show("kid_card", Role.KID, "kn", lines=lines)
    assert len(kid.text) <= BODY_LIMIT and "ರೂ 345" in kid.text and PHRASE in kid.text


def test_a_long_address_is_shortened_as_a_last_resort_and_never_overflows():
    out = show("kid_card", Role.KID, address_text="Flat 7, " + "Very Long Lane, " * 80)
    assert len(out.text) <= BODY_LIMIT
    assert "Total: Rs 345." in out.text


def test_text_that_can_never_fit_is_refused_not_cut():
    giant = {"mandate_id": "m1", "code": "not_available"}
    giant_phrase = "word " * 400
    with pytest.raises(MessageTooLong):
        render("order_failed", Role.KID, giant, lang="en", phrase=giant_phrase)


def test_unknown_things_have_no_generic_fallback():
    with pytest.raises(MissingTemplate):
        render("surprise", Role.PARENT, {"mandate_id": "m1"}, lang="en")
    with pytest.raises(MissingTemplate):
        show("readback", Role.KID)  # written for the parent only
    with pytest.raises(MissingTemplate):
        show("blocked", Role.KID, reason="made_up")
    with pytest.raises(MissingTemplate):
        show("order_failed", Role.KID, code="made_up")
    with pytest.raises(MissingTemplate):
        show("readback", Role.PARENT, "fr")


def test_kid_messages_cannot_be_made_without_the_safe_phrase():
    for phrase in ("", "   "):
        with pytest.raises(ValueError, match="safe-phrase"):
            render("kid_card", Role.KID, payload("kid_card"), lang="en", phrase=phrase)


def test_a_button_for_the_wrong_person_is_refused():
    with pytest.raises(ValueError, match="button"):
        show("sent_to_kid", Role.PARENT, buttons=["approve"])
    with pytest.raises(ValueError, match="button"):
        show("kid_card", Role.KID, buttons=["confirm"])


def test_more_than_three_buttons_is_refused():
    with pytest.raises(ValueError, match="buttons"):
        show("kid_card", Role.KID, buttons=["approve", "reject", "pay", "dont_pay"])
