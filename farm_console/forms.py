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
    """A form input; its explanation is the glossary entry under the same key.
    Settings take their bounds from farm_db.SETTINGS."""

    label: str
    group: str = ""
    bound: Optional[Bound] = None


SCAN_FIELDS = {
    "min_fav": Field("Min favorite mid", bound=Bound(0, 1, low_open=True)),
    "expiry_buffer_days": Field("Expiry buffer (days)", bound=Bound(0)),
    "min_window_days": Field("Min window (days)", bound=Bound(0)),
    "horizon_cap_days": Field("Horizon cap (days)", bound=Bound(0, low_open=True)),
    "capital": Field("Capital per market ($)", bound=Bound(0, low_open=True)),
    "max_lead": Field("Max lead", bound=Bound(0, 0.5)),
    "min_low_score": Field("Min longshot score", bound=Bound(0, 1, low_open=True)),
    "naked_budget": Field("Naked budget ($)", bound=Bound(0)),
    "min_rate": Field("Min pool ($/day)", bound=Bound(0)),
    "fragile_drop": Field("Fragile mid drop", bound=Bound(0, 1)),
    "hurdle_rate": Field("Hurdle rate", bound=Bound(0, 10)),
    "depth_haircut": Field("Depth haircut", bound=Bound(0, 1)),
    "fill_lookback_days": Field("Fill lookback (days)", bound=Bound(1, 365)),
    "sweep_prior_weight": Field("Sweep prior weight", bound=Bound(0, 100)),
    "tape_markets": Field("Markets with a tape", bound=Bound(0, 2000)),
}

QUOTING, SAFETY = "Quoting", "Safety"
SETTING_GROUPS = (QUOTING, SAFETY)

SETTING_FIELDS = {
    "capital": Field("Capital per market ($)", QUOTING),
    "max_lead": Field("Max lead", QUOTING),
    "ask_offset": Field("Ask offset", QUOTING),
    "min_capture": Field("Min capture", QUOTING),
    "ev_margin": Field("EV margin", QUOTING),
    "min_low_score": Field("Min longshot score", QUOTING),
    "keep_ratio": Field("Keep ratio", QUOTING),
    "hurdle_rate": Field("Hurdle rate", QUOTING),
    "depth_haircut": Field("Depth haircut", QUOTING),
    "fill_lookback_days": Field("Fill lookback (days)", QUOTING),
    "sweep_prior_weight": Field("Sweep prior weight", QUOTING),
    "drop_threshold": Field("Ask drop threshold", SAFETY),
    "drop_window_s": Field("Drop window (s)", SAFETY),
    "cooldown_s": Field("Cooldown (s)", SAFETY),
    "naked_cooldown_s": Field("Naked cooldown (s)", SAFETY),
    "min_fav": Field("Min favorite mid", SAFETY),
    "expiry_buffer_days": Field("Expiry buffer (days)", SAFETY),
}


def _number(text):
    """A finite float from user text, or None."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


P_TRUE_ERROR = "Enter a whole percent from 1 to 99."


def parse_p_true(text):
    """The probability of a p_true typed as a whole percent from 1 to 99, or None."""
    percent = _number(text)
    if percent is None or percent != int(percent) or not 0 < percent < 100:
        return None
    return percent / 100


class Decision(NamedTuple):
    p_true: float
    naked_budget: float
    capital: Optional[float]
    start_paused: bool


def parse_decision(form):
    """Approval terms: p_true as a whole percent, a budget of 0 or more, and an
    optional positive capital (blank means the farmer's capital), and whether to
    start paused."""
    errors = {}
    p_true = parse_p_true(form.get("p_true"))
    if p_true is None:
        errors["p_true"] = P_TRUE_ERROR
    budget = _number(form.get("naked_budget"))
    if budget is None or budget < 0:
        errors["naked_budget"] = "Enter a budget in dollars, 0 or more."
    capital = None
    if (form.get("capital") or "").strip():
        capital = _number(form.get("capital"))
        if capital is None or not capital > 0:
            errors["capital"] = "Enter a positive amount, or leave blank for the farmer's."
    start_paused = form.get("start_paused") == "1"
    return (None if errors else Decision(p_true, budget, capital, start_paused)), errors


def preview_terms(form):
    """(p_true, naked budget, capital) from a decision form still being typed:
    each is None when blank or invalid, so a preview can plan around it."""
    decision, errors = parse_decision(form)
    if decision:
        return decision.p_true, decision.naked_budget, decision.capital
    return (parse_p_true(form.get("p_true")),
            None if "naked_budget" in errors else _number(form.get("naked_budget")),
            None if "capital" in errors else _number(form.get("capital")))


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
