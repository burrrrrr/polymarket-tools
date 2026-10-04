"""
Parsing of the console's form posts, the one place user input is checked.
Each parser returns the typed value and a {field: message} of errors; a form
with any error writes nothing.
"""

import math
from typing import NamedTuple, Optional

import farm_db
from farm_db import Bound
from reward_screener import ScreenConfig

NOTE_MAX = 500
CANCEL_ALL_WORD = "CANCEL"


class Field(NamedTuple):
    """A form input; settings take their bounds from farm_db.SETTINGS."""

    label: str
    hint: str
    group: str = ""
    bound: Optional[Bound] = None


SCAN_FIELDS = {
    "min_fav": Field("Min favorite mid", "Skip markets whose favorite trades below this.",
                     bound=Bound(0, 1, low_open=True)),
    "expiry_buffer_days": Field("Expiry buffer (days)", "Farming stops this long before the end.",
                                bound=Bound(0)),
    "min_window_days": Field("Min window (days)", "Farmable days left, at least.", bound=Bound(0)),
    "horizon_cap_days": Field("Horizon cap (days)", "Most days counted toward rank.",
                              bound=Bound(0, low_open=True)),
    "capital": Field("Capital per market ($)", "Behind the planned favorite bid.",
                     bound=Bound(0, low_open=True)),
    "bid_offset": Field("Bid offset", "Under the midpoint, in price (0.01 = 1c).",
                        bound=Bound(0, 0.5)),
    "min_low_score": Field("Min longshot score", "Per-share score the longshot bid needs.",
                           bound=Bound(0, 1, low_open=True)),
    "min_rate": Field("Min pool ($/day)", "Skip smaller daily reward pools.", bound=Bound(0)),
}

QUOTING, SAFETY = "Quoting", "Safety"
SETTING_GROUPS = (QUOTING, SAFETY)

SETTING_FIELDS = {
    "capital": Field("Capital per market ($)",
                     "Caps the favorite bid plus farm inventory at cost.", QUOTING),
    "bid_offset": Field("Bid offset", "Under the midpoint the bid creates, in price.",
                        QUOTING),
    "ask_offset": Field("Ask offset", "Favorite exit above the midpoint, in price.",
                        QUOTING),
    "min_capture": Field("Min capture", "The exit never sells below average cost plus this.",
                         QUOTING),
    "ev_margin": Field("EV margin", "The favorite bid never prices above p_true minus this.",
                       QUOTING),
    "min_low_score": Field("Min longshot score",
                           "The cheapest longshot bid that still scores this per share.",
                           QUOTING),
    "keep_ratio": Field("Keep ratio", "Hold an order while it scores this share of target.",
                        QUOTING),
    "drop_threshold": Field("Mid drop threshold",
                            "A fall this size inside the window starts a cooldown.",
                            SAFETY),
    "drop_window_s": Field("Drop window (s)", "How far back the fall is measured.",
                           SAFETY),
    "cooldown_s": Field("Cooldown (s)", "Orders stay pulled this long after a drop.",
                        SAFETY),
    "naked_cooldown_s": Field("Naked cooldown (s)",
                              "No longshot bid this long after a longshot-bid fill.",
                              SAFETY),
    "min_fav": Field("Min favorite mid", "Below this the market goes BELOW_RANGE.",
                     SAFETY),
    "expiry_buffer_days": Field("Expiry buffer (days)",
                                "GTD expiry and retirement before the end date.", SAFETY),
}


def _number(text):
    """A finite float from user text, or None."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class Decision(NamedTuple):
    p_true: float
    naked_budget: float
    capital: Optional[float]


def parse_decision(form):
    """Approval terms: p_true in (0, 1), a budget of 0 or more, and an optional
    positive capital (blank means the farmer's capital)."""
    errors = {}
    p_true = _number(form.get("p_true"))
    if p_true is None or not 0 < p_true < 1:
        errors["p_true"] = "Enter a probability strictly between 0 and 1."
    budget = _number(form.get("naked_budget"))
    if budget is None or budget < 0:
        errors["naked_budget"] = "Enter a budget in dollars, 0 or more."
    capital = None
    if (form.get("capital") or "").strip():
        capital = _number(form.get("capital"))
        if capital is None or not capital > 0:
            errors["capital"] = "Enter a positive amount, or leave blank for the farmer's."
    return (None if errors else Decision(p_true, budget, capital)), errors


def parse_note(form):
    note = (form.get("note") or "").strip()
    if len(note) > NOTE_MAX:
        return None, {"note": f"Keep the note under {NOTE_MAX} characters."}
    return note or None, {}


def parse_scan(form):
    """A ScreenConfig from the scan parameters form."""
    values, errors = {}, {}
    for name, field in SCAN_FIELDS.items():
        value = _number(form.get(name))
        if value is None or not field.bound.admits(value):
            errors[name] = f"Out of range for {field.label.lower()}."
        else:
            values[name] = value
    return (None if errors else ScreenConfig(**values)), errors


def parse_settings(form, group):
    """{key: value} of one settings group, each checked by farm_db.parse_setting."""
    values, errors = {}, {}
    for key, field in SETTING_FIELDS.items():
        if field.group != group:
            continue
        try:
            values[key] = farm_db.parse_setting(key, (form.get(key) or "").strip())
        except ValueError as e:
            errors[key] = str(e)
    return (None if errors else values), errors


def parse_cancel_all(form):
    if (form.get("confirm") or "").strip() != CANCEL_ALL_WORD:
        return {"confirm": f"Type {CANCEL_ALL_WORD} to confirm."}
    return {}
