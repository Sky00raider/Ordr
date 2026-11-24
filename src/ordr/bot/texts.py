"""Fixed replies the bot writes itself (never the model). Kannada wording needs a native check.

The parent gets `PARENT_LANG`. The kid always gets English.
"""

TRY_AGAIN = {
    "en": "Sorry, something went wrong. Nothing was ordered. Please try again.",
    "kn": "ಕ್ಷಮಿಸಿ, ಏನೋ ತಪ್ಪಾಯಿತು. ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
}
NOT_UNDERSTOOD = {
    "en": "I did not understand. Please tell me the items again.",
    "kn": "ನನಗೆ ಅರ್ಥವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ವಸ್ತುಗಳನ್ನು ಮತ್ತೆ ಹೇಳಿ.",
}
VOICE_SOON = {
    "en": "Voice notes are coming next. For now, please type your order.",
    "kn": "ಧ್ವನಿ ಸಂದೇಶ ಮುಂದಿನ ಹಂತದಲ್ಲಿ ಬರುತ್ತದೆ. ಈಗ ದಯವಿಟ್ಟು ಆರ್ಡರ್ ಟೈಪ್ ಮಾಡಿ.",
}
KID_TEXT = (
    "I only act on the buttons on your cards. Send the word log to see what happened. "
    "Typing yes or no does not approve anything."
)
BUTTON_STALE = "This button is no longer active."
PRESS_FAILED = "Something went wrong, and nothing was ordered. Please try again."
GATE_DOWN = "I cannot reach Ordr right now. Nothing was ordered. Please try again in a minute."

# What prepare_order's error codes mean for the parent. One entry per code in TRD section 6.
PREPARE_FAILED: dict[str, dict[str, str]] = {
    "item_not_from_search": {
        "en": "I could not match one of the items to the store, so I did not order anything. "
        "Please tell me the items again.",
        "kn": "ಒಂದು ವಸ್ತುವನ್ನು ಅಂಗಡಿಯಲ್ಲಿ ಹೊಂದಿಸಲು ಆಗಲಿಲ್ಲ, ಆದ್ದರಿಂದ ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. "
        "ದಯವಿಟ್ಟು ವಸ್ತುಗಳನ್ನು ಮತ್ತೆ ಹೇಳಿ.",
    },
    "order_already_open": {
        "en": "You already have an order waiting. Finish it, or tap Stop on it, before a new one.",
        "kn": "ನಿಮ್ಮ ಒಂದು ಆರ್ಡರ್ ಈಗಾಗಲೇ ಕಾಯುತ್ತಿದೆ. ಹೊಸದನ್ನು ಮಾಡುವ ಮೊದಲು ಅದನ್ನು ಮುಗಿಸಿ, ಅಥವಾ Stop ಒತ್ತಿ.",
    },
    "over_cap": {
        "en": "This order is over the spending limit set for your family, so I did not order it.",
        "kn": "ಈ ಆರ್ಡರ್ ನಿಮ್ಮ ಕುಟುಂಬಕ್ಕೆ ಹಾಕಿರುವ ಖರ್ಚಿನ ಮಿತಿಗಿಂತ ಹೆಚ್ಚಾಗಿದೆ, ಆದ್ದರಿಂದ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ.",
    },
    "needs_review": {
        "en": "An earlier order has to be checked by your family first, so I cannot start a new "
        "one.",
        "kn": "ಹಿಂದಿನ ಆರ್ಡರ್ ಅನ್ನು ಮೊದಲು ನಿಮ್ಮ ಮನೆಯವರು ಪರಿಶೀಲಿಸಬೇಕು, ಆದ್ದರಿಂದ ಹೊಸದನ್ನು ಮಾಡಲು ಆಗುವುದಿಲ್ಲ.",
    },
    "not_found": {
        "en": "I could not find those items. Please tell me the items again.",
        "kn": "ಆ ವಸ್ತುಗಳು ಸಿಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ವಸ್ತುಗಳನ್ನು ಮತ್ತೆ ಹೇಳಿ.",
    },
    "store_error": {
        "en": "The store did not answer properly, so nothing was ordered. Please try again in a "
        "few minutes.",
        "kn": "ಅಂಗಡಿಯಿಂದ ಸರಿಯಾದ ಉತ್ತರ ಬರಲಿಲ್ಲ, ಆದ್ದರಿಂದ ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ಕೆಲವು ನಿಮಿಷಗಳ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
    },
    "not_available": {
        "en": "Some of these items are not available right now, so I did not order anything. "
        "Please ask again, or ask for something else.",
        "kn": "ಈ ಕೆಲವು ವಸ್ತುಗಳು ಈಗ ಲಭ್ಯವಿಲ್ಲ, ಆದ್ದರಿಂದ ಏನನ್ನೂ ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಕೇಳಿ, ಅಥವಾ ಬೇರೆ ವಸ್ತು ಕೇಳಿ.",
    },
    "order_too_large": {
        "en": "This order has too many items to show in one message, so I did not order it. "
        "Please order fewer items at a time.",
        "kn": "ಈ ಆರ್ಡರ್‌ನಲ್ಲಿ ತುಂಬಾ ವಸ್ತುಗಳಿವೆ, ಒಂದು ಸಂದೇಶದಲ್ಲಿ ತೋರಿಸಲು ಆಗುವುದಿಲ್ಲ, ಆದ್ದರಿಂದ "
        "ಆರ್ಡರ್ ಮಾಡಿಲ್ಲ. ದಯವಿಟ್ಟು ಒಮ್ಮೆಗೆ ಕಡಿಮೆ ವಸ್ತುಗಳನ್ನು ಹೇಳಿ.",
    },
}


def pick(table: dict[str, str], lang: str) -> str:
    return table.get(lang) or table["en"]


def prepare_failed(code: str | None, lang: str) -> str:
    """Fixed text for a prepare_order error code. An unknown code gets the generic retry text."""
    table = PREPARE_FAILED.get(code or "")
    return pick(table if table is not None else TRY_AGAIN, lang)
