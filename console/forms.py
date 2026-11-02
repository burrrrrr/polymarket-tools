"""
Parsing of the console's form posts, the one place user input is checked.
Each parser returns the typed value and a {field: message} of errors; a form
with any error writes nothing.
"""

import math
from typing import NamedTuple, Optional

from farm import farm_db
from farm.farm_db import Bound
from farm.reward_screener import CATEGORIES, ScreenConfig

NOTE_MAX = 500
CANCEL_ALL_WORD = "CANCEL"
RESTORE_WORD = "PLACE"
ARM_WORD = "ARM"


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
    "tape_markets": Field("Markets read for fills", bound=Bound(0, 2000)),
}

QUOTING, SAFETY = "Quoting", "Safety"
SETTING_GROUPS = (QUOTING, SAFETY)

SETTING_FIELDS = {
    "capital": Field("Capital per market ($)", QUOTING),
    "bid_multiple": Field("Bid multiple of capital", QUOTING),
    "tail_days": Field("Tail cap (days of reward)", QUOTING),
    "max_lead": Field("Max lead", QUOTING),
    "ask_offset": Field("Ask offset", QUOTING),
    "min_capture": Field("Min capture", QUOTING),
    "ev_margin": Field("EV margin", QUOTING),
    "min_low_score": Field("Min longshot score", QUOTING),
    "keep_ratio": Field("Keep ratio", QUOTING),
    "hurdle_rate": Field("Hurdle rate", QUOTING),
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


P_TRUE_ERROR = "Enter a whole percent from 1 to 99, or tick Follow the market."
FOLLOW_AND_P_TRUE_ERROR = "Clear p_true or untick Follow the market."
FOLLOW_SOLE_ERROR = "A sole bid needs your p_true. Untick Follow the market or Allow sole bid."
BID_MULTIPLE = farm_db.SETTINGS["bid_multiple"]
BID_MULTIPLE_ERROR = (f"Enter a multiple from {BID_MULTIPLE.low:g} to {BID_MULTIPLE.high:g}, "
                      f"or leave blank for the farmer's.")


def parse_p_true(text, follow_market):
    """The favorite's p_true chosen on the form: a whole percent from 1 to 99,
    as a probability, or None when Follow the market is ticked with p_true
    left blank. ValueError with the message to show for anything else."""
    if follow_market:
        if (text or "").strip():
            raise ValueError(FOLLOW_AND_P_TRUE_ERROR)
        return None
    percent = _number(text)
    if percent is None or percent != int(percent) or not 0 < percent < 100:
        raise ValueError(P_TRUE_ERROR)
    return percent / 100


class Decision(NamedTuple):
    # None follows the market.
    p_true: Optional[float]
    naked_budget: float
    capital: Optional[float]
    start_paused: bool
    sole_bid: bool
    # None is the farmer's setting.
    bid_multiple: Optional[float]


def parse_decision(form):
    """Approval terms: p_true as a whole percent, or Follow the market ticked
    instead, a budget of 0 or more, an optional positive capital and an
    optional bid multiple within farm_db's bound (blank means the farmer's),
    whether to start paused, and whether a sole bid is allowed, which needs a
    p_true."""
    errors = {}
    follow_market = form.get("follow_market") == "1"
    sole_bid = form.get("sole_bid") == "1"
    try:
        p_true = parse_p_true(form.get("p_true"), follow_market)
    except ValueError as e:
        p_true, errors["p_true"] = None, str(e)
    if follow_market and sole_bid:
        errors["follow_market"] = FOLLOW_SOLE_ERROR
    budget = _number(form.get("naked_budget"))
    if budget is None or budget < 0:
        errors["naked_budget"] = "Enter a budget in dollars, 0 or more."
    capital = None
    if (form.get("capital") or "").strip():
        capital = _number(form.get("capital"))
        if capital is None or not capital > 0:
            errors["capital"] = "Enter a positive amount, or leave blank for the farmer's."
    bid_multiple = None
    if (form.get("bid_multiple") or "").strip():
        bid_multiple = _number(form.get("bid_multiple"))
        if bid_multiple is None or not BID_MULTIPLE.admits(bid_multiple):
            errors["bid_multiple"] = BID_MULTIPLE_ERROR
    start_paused = form.get("start_paused") == "1"
    return (None if errors else Decision(p_true, budget, capital, start_paused, sole_bid,
                                         bid_multiple)), errors


def preview_terms(form):
    """(p_true, naked budget, capital, bid multiple) from a decision form still
    being typed: each is None when blank or invalid, so a preview can plan
    around it."""
    decision, errors = parse_decision(form)
    if decision:
        return decision.p_true, decision.naked_budget, decision.capital, decision.bid_multiple
    try:
        p_true = parse_p_true(form.get("p_true"), form.get("follow_market") == "1")
    except ValueError:
        p_true = None
    return (p_true,
            None if "naked_budget" in errors else _number(form.get("naked_budget")),
            None if "capital" in errors else _number(form.get("capital")),
            None if "bid_multiple" in errors else _number(form.get("bid_multiple")))


class Rejection(NamedTuple):
    reason: Optional[farm_db.RejectReason]
    note: Optional[str]
    mute_event: bool


def parse_rejection(form):
    """A reject: an optional RejectReason, an optional note, and whether to mute
    the event too."""
    errors, reason = {}, None
    if form.get("reason"):
        try:
            reason = farm_db.RejectReason(form["reason"])
        except ValueError:
            errors["reason"] = "Pick a reason from the list, or none."
    note = (form.get("note") or "").strip()
    if len(note) > NOTE_MAX:
        errors["note"] = f"Keep the note under {NOTE_MAX} characters."
    return ((None if errors else Rejection(reason, note or None, form.get("mute_event") == "1")),
            errors)


def exclude_field(category):
    """The scan form's checkbox name for excluding one CATEGORIES key."""
    return f"exclude_{category}"


def parse_scan(form):
    """A ScreenConfig from the scan parameters form."""
    values, errors = {}, {}
    for name, field in SCAN_FIELDS.items():
        value = _number(form.get(name))
        if value is None or not field.bound.admits(value):
            errors[name] = f"Out of range for {field.label.lower()}."
        else:
            values[name] = value
    excluded = tuple(c for c in CATEGORIES if form.get(exclude_field(c)) == "1")
    return (None if errors else ScreenConfig(**values, excluded_categories=excluded)), errors


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


def parse_typed_word(form, word):
    """The errors of a form whose confirm field must hold this word, typed out."""
    if (form.get("confirm") or "").strip() != word:
        return {"confirm": f"Type {word} to confirm."}
    return {}


SNAPSHOT_NOTE_MAX = 200


def parse_snapshot_note(form):
    """(the note of a snapshot, trimmed and possibly empty; errors by field)."""
    note = (form.get("note") or "").strip()
    if len(note) > SNAPSHOT_NOTE_MAX:
        return None, {"note": f"A note is at most {SNAPSHOT_NOTE_MAX} characters."}
    return note, {}


TRIAGE_BATCH_DEFAULT = 20
TRIAGE_BATCH = Bound(1, 100)


def _count(text, bound):
    """A whole number within bound from user text, or None."""
    value = _number(text)
    if value is None or value != int(value) or not bound.admits(value):
        return None
    return int(value)


def parse_triage_batch(form):
    """How many queued candidates to send to Claude; blank means the default."""
    text = (form.get("n") or "").strip()
    n = _count(text, TRIAGE_BATCH) if text else TRIAGE_BATCH_DEFAULT
    if n is None:
        return None, {"n": f"Enter a whole number from {TRIAGE_BATCH.low:.0f} to "
                           f"{TRIAGE_BATCH.high:.0f}."}
    return n, {}


def parse_triage_auto(form):
    """farm_db's triage.auto_top_n: how many new candidates a scan sends to Claude."""
    n = _count(form.get("auto_top_n"), farm_db.TRIAGE_AUTO)
    if n is None:
        return None, {"auto_top_n": f"Enter a whole number from 0 (off) to "
                                    f"{farm_db.TRIAGE_AUTO.high:.0f}."}
    return n, {}
