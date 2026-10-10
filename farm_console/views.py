"""
View models of the farm console: pure functions from farm.db and tools.db reads
to what a page shows. Every derived number, alert, and class name is decided
here, so the templates only lay values out and the JSON API serves the same
objects.
"""

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Iterable, NamedTuple, Optional
from urllib.parse import quote, urlencode

import fill_cost
import reward_farmer
from farm_db import EventKind, FarmerStatus, resting_order
from reward_screener import (
    Skip,
    adjusted_mid,
    created_mid,
    floor_to_tick,
    planned_budget_cost,
)
from reward_screener import score as share_score
from tools import db as tools_db
from tools.db import JobState

from .forms import P_TRUE_ERROR

SLOTS = ("FAV_BID", "LONG_BID", "FAV_ASK", "LONG_ASK")
PHASES = ("QUOTING", "COOLDOWN", "BELOW_RANGE", "PAUSED", "UNREWARDED", "RETIRED")
WAITING = "WAITING"

# A farmer whose last cycle is older than this many intervals is not running.
STALE_CYCLES = 3
LOW_SCORE = 0.10
BUDGET_BOUND = 0.99
DEFAULT_EXPIRY_BUFFER_DAYS = 7.0
FEED_EVENTS = 12
LOG_PAGE = 50

PHASE_TONE = {"QUOTING": "t-q", "COOLDOWN": "t-c", "BELOW_RANGE": "t-b",
              "PAUSED": "t-n", "UNREWARDED": "t-n", "RETIRED": "t-r", WAITING: "t-n"}
SLOT_TONE = {"FAV_BID": "t-f", "FAV_ASK": "t-f", "LONG_BID": "t-b", "LONG_ASK": "t-b"}
JOB_TONE = {JobState.QUEUED: "t-n", JobState.RUNNING: "t-c", JobState.DONE: "t-q",
            JobState.FAILED: "t-r"}
KIND_TEXT_TONE = {
    EventKind.PHASE: "c-warn", EventKind.BACKOFF: "c-warn", EventKind.RETIRE: "c-warn",
    EventKind.ALERT: "c-bad", EventKind.REJECT: "c-bad",
    EventKind.START: "c-mut", EventKind.STOP: "c-mut", EventKind.SETTINGS: "c-mut",
}
SLOT_TEXT_TONE = {"FAV_BID": "c-fav", "FAV_ASK": "c-fav", "LONG_BID": "c-long",
                  "LONG_ASK": "c-long"}
FILL_EFFECT = {
    "FAV_BID": "farm inventory up; a FAV_ASK can exit it",
    "LONG_BID": "naked fill; LONG_BID paused for the naked cooldown",
    "FAV_ASK": "farm inventory sold",
    "LONG_ASK": "longshot inventory sold",
}


class Snapshot(NamedTuple):
    """What every page reads of farm.db at one moment.

    markets holds the approved markets plus every market with a farm_state
    row; fills are the FILL events of the last 24 hours; rewards are
    (date, condition_id, usd) rows of the last 30 days.
    """

    status: Optional[FarmerStatus]
    states: dict
    markets: dict
    fills: list
    rewards: list
    counts: dict


# ---------------------------------------------------------------------------
# Formatting (used as template filters)
# ---------------------------------------------------------------------------

def cents(price):
    return "--" if price is None else f"{price * 100:.1f}c"


def usd(value):
    return "--" if value is None else f"${value:,.2f}"


def shares(value):
    return "--" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".")


def score(value):
    return "--" if value is None else f"{value:.3f}"


def clock(dt):
    return "--" if dt is None else f"{dt.astimezone():%H:%M:%S}"


def stamp_local(dt):
    return "--" if dt is None else f"{dt.astimezone():%Y-%m-%d %H:%M:%S}"


def span(seconds):
    """A duration as its two largest units: 82d 4h, 3m 12s, 45s."""
    seconds = int(max(0, seconds))
    parts = [(seconds // 86400, "d"), (seconds % 86400 // 3600, "h"),
             (seconds % 3600 // 60, "m"), (seconds % 60, "s")]
    while len(parts) > 1 and parts[0][0] == 0:
        parts.pop(0)
    return " ".join(f"{n}{unit}" for n, unit in parts[:2])


def market_name(market, condition_id=""):
    if market is None:
        return condition_id[:12]
    return market.question or market.slug or market.condition_id[:12]


def review_url(condition_id, mode=None, skipped=0):
    """A market's Review page, keeping the queue's mode filter."""
    query = urlencode({k: v for k, v in (("mode", mode), ("skipped", skipped)) if v})
    return f"/review/{condition_id}" + (f"?{query}" if query else "")


def polymarket_url(slug):
    """polymarket.com redirects /market/<slug> to the market inside its event."""
    return f"https://polymarket.com/market/{quote(slug)}" if slug else None


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Liveness:
    running: bool
    mode: Optional[str]
    interval_s: Optional[float]
    last_cycle_at: Optional[datetime]
    stopped_at: Optional[datetime]
    message: str


def liveness(status, now):
    """Whether the farmer is running, and the banner text when it is not."""
    if status is None:
        return Liveness(False, None, None, None, None,
                        "The farmer has never run against this database.")
    mode = "LIVE" if status.mode == "live" else "DRY RUN"
    base = dict(mode=mode, interval_s=status.interval_s,
                last_cycle_at=status.last_cycle_at, stopped_at=status.stopped_at)
    if status.stopped_at is not None:
        ago = span((now - status.stopped_at).total_seconds())
        return Liveness(False, message=f"The farmer is not running. It stopped {ago} ago.",
                        **base)
    age = (now - status.last_cycle_at).total_seconds()
    if age > STALE_CYCLES * status.interval_s:
        return Liveness(False, message=(
            f"The farmer is not running. Its last cycle was {span(age)} ago, "
            f"and it cycles every {status.interval_s:g}s."), **base)
    return Liveness(True, message="", **base)


@dataclass(frozen=True)
class WorkerLiveness:
    """Whether the order tools' worker is running, the sidebar's label for it,
    and the banner text when it is not. configured is False for a console
    started with no tools.db."""

    configured: bool
    running: bool
    keyed: Optional[bool]
    last_beat_at: Optional[datetime]
    label: str
    message: str


NO_WORKER = WorkerLiveness(False, False, None, None, "not configured",
                           "The worker database is not configured. Start the console with "
                           "--tools-db to use the order tools.")


def worker_liveness(status, now):
    """The WorkerLiveness of a tools.db worker_status row, or of none."""
    if status is None:
        return WorkerLiveness(True, False, None, None, "never ran",
                              "The worker has never run against this database.")
    label = "keyed" if status.keyed else "no key"
    if tools_db.is_running(status, now):
        return WorkerLiveness(True, True, status.keyed, status.last_beat_at, label, "")
    if status.stopped_at is not None:
        message = f"It stopped {span((now - status.stopped_at).total_seconds())} ago."
    else:
        ago = span((now - status.last_beat_at).total_seconds())
        message = f"Its last heartbeat was {ago} ago."
    return WorkerLiveness(True, False, status.keyed, status.last_beat_at, f"{label} · stopped",
                          f"The worker is not running. {message}")


@dataclass(frozen=True)
class Chrome:
    """The layout around every page: the farmer's and the worker's status and
    the nav counts."""

    liveness: Liveness
    candidates: int
    quoting: int
    worker: WorkerLiveness = NO_WORKER


def chrome(snap, now, worker=NO_WORKER):
    return Chrome(liveness(snap.status, now), snap.counts.get("candidate", 0),
                  sum(s.phase == "QUOTING" for s in snap.states.values()), worker)


@dataclass(frozen=True)
class Order:
    price: float
    size: float

    @property
    def notional(self):
        return self.price * self.size


def _order(view_part):
    return Order(view_part["price"], view_part["size"]) if view_part else None


def resting(state, slot):
    """The order a slot rests once the farmer's last cycle landed, or None."""
    found = resting_order(state.slots.get(slot))
    return Order(*found) if found else None


def free_bid(state):
    """The longshot bid a market rests free of its naked budget, or None. The
    farmer says each cycle whether the budget leaves the bid alone
    (reward_farmer.free_of_budget, published on the LONG_BID slot): its price
    gives a favorite buyer under 5% a year and has passed its test for ten
    minutes. A row from a farmer that did not say counts the bid against the
    budget."""
    bid = resting(state, "LONG_BID")
    return bid if bid and (state.slots.get("LONG_BID") or {}).get("budget_free") else None


def budget_counted(state):
    """USD of its naked budget a market's resting longshot bid takes: its
    cost, or nothing where it is free of the budget (free_bid)."""
    bid = resting(state, "LONG_BID")
    return bid.notional if bid and free_bid(state) is None else 0.0


def budget_used(state):
    """USD a market has used of its naked budget: longshot shares held, at
    cost, plus what its resting longshot bid takes of it (budget_counted).
    farm_state's naked_used_usd is everything at risk, a free bid included."""
    position = state.position or {}
    held = (position.get("farm", {}).get("long", 0.0)
            * position.get("avg_cost", {}).get("long", 0.0))
    return held + budget_counted(state)


def years_left(market, now):
    """Years to a market's end date, which set the return a longshot bid
    offers; none without an end date."""
    return fill_cost.years_until(market.end_date, now) if market.end_date else 0.0


@dataclass(frozen=True)
class EventView:
    id: int
    at: datetime
    kind: str
    slot: Optional[str]
    condition_id: Optional[str]
    text: str
    detail: str
    tone: str


def event_view(e):
    """text is the farmer's line; detail drops the kind it starts with, if any."""
    tone = KIND_TEXT_TONE.get(e.kind) or SLOT_TEXT_TONE.get(e.slot, "c-mut")
    prefix = f"{e.kind.value} "
    detail = e.message[len(prefix):].lstrip() if e.message.startswith(prefix) else e.message
    return EventView(e.id, e.at, e.kind.value, e.slot, e.condition_id, e.message, detail,
                     tone)


def expiry_cutoff(market, status):
    """When the farmer retires the market: its end date minus the expiry buffer."""
    if market.end_date is None:
        return None
    buffer = status.config.get("expiry_buffer_days") if status else None
    days = DEFAULT_EXPIRY_BUFFER_DAYS if buffer is None else buffer
    return market.end_date - timedelta(days=days)


# ---------------------------------------------------------------------------
# Band strip
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Mark:
    kind: str
    price: float
    left: float
    label: str = ""


@dataclass(frozen=True)
class BandStrip:
    """The reward band on the favorite-price axis, with our quotes marked.

    The axis runs from mid - v to min(mid + v, 1). The longshot bid shows at
    its favorite-price mirror, 1 - price. left is a percentage of the width,
    clamped to the strip.
    """

    lo: float
    hi: float
    marks: list


def band_strip(mid, v, fav_bid, long_bid, book=()):
    """book: extra (kind, price, label) marks, drawn first."""
    if mid is None or not v:
        return None
    lo, hi = mid - v, min(mid + v, 1.0)

    def left(price):
        return round(max(0.0, min(100.0, (price - lo) / (hi - lo) * 100)), 2)

    marks = [Mark(kind, price, left(price), label) for kind, price, label in book]
    if fav_bid is not None:
        marks.append(Mark("fav_bid", fav_bid, left(fav_bid)))
    marks.append(Mark("mid", mid, left(mid)))
    if long_bid is not None:
        mirror = round(1 - long_bid, 6)
        marks.append(Mark("long_bid", mirror, left(mirror)))
    return BandStrip(round(lo, 6), round(hi, 6), marks)


# ---------------------------------------------------------------------------
# Market cards
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SlotRow:
    slot: str
    tone: str
    order: Optional[Order]
    target: Optional[Order]
    target_score: Optional[float]
    live_score: Optional[float]
    decision: str
    reason: str
    backoff_until: Optional[str]
    backoff_attempts: Optional[int]


def slot_rows(state):
    rows = []
    for slot in SLOTS:
        view = state.slots.get(slot) or {}
        target = view.get("target")
        backoff = state.backoff.get(slot) or {}
        rows.append(SlotRow(
            slot=slot, tone=SLOT_TONE[slot], order=_order(view.get("order")),
            target=_order(target), target_score=target and target["score"],
            live_score=view.get("live_score"), decision=view.get("decision", "none"),
            reason=view.get("reason", ""), backoff_until=backoff.get("until"),
            backoff_attempts=backoff.get("attempts"),
        ))
    return rows


@dataclass(frozen=True)
class Card:
    condition_id: str
    name: str
    slug: Optional[str]
    mode: Optional[str]
    fav_outcome: Optional[str]
    p_true: Optional[float]
    budget_usd: float
    capital_usd: Optional[float]
    cutoff: Optional[datetime]
    cutoff_in_s: Optional[float]
    phase: str
    phase_tone: str
    phase_reason: str
    phase_until: Optional[datetime]
    mid: Optional[float]
    book_mid: Optional[float]
    band: Optional[BandStrip]
    fav_bid: Optional[Order]
    long_bid: Optional[Order]
    slots: list
    q1: float
    q2: float
    q_balance_pct: float
    # naked_used_usd is everything at risk on the naked leg. budget_used_usd
    # is the part the naked budget limits, which naked_pct is a share of, and
    # free_bid_usd the resting longshot bid it does not. long_return is the
    # annual return a fill of the resting longshot bid would give the
    # favorite buyer, as the farmer published it.
    naked_used_usd: float
    budget_used_usd: float
    free_bid_usd: float
    naked_pct: float
    position: Optional[dict]
    paused: bool
    naked_paused: bool
    status: str
    # The farmer's favorite bid economics from its FAV_BID slot, the reason it
    # has none, and the fill history behind them; absent from farm_state rows
    # that predate fill-cost pricing, and history from rows that predate it.
    economics: Optional[dict] = None
    fav_reason: str = ""
    history: Optional[fill_cost.History] = None
    long_return: Optional[float] = None


def card(market, state, status, now):
    """One market's live card; a market without farm_state is WAITING."""
    metrics = market.metrics
    m = metrics.get("market", {})
    cutoff = expiry_cutoff(market, status)
    budget = market.naked_budget_usd or 0.0
    if state is None:
        return Card(
            condition_id=market.condition_id, name=market_name(market), slug=market.slug,
            mode=metrics.get("plan", {}).get("mode"), fav_outcome=m.get("fav_outcome"),
            p_true=market.p_true, budget_usd=budget, capital_usd=market.capital_usd,
            cutoff=cutoff, cutoff_in_s=cutoff and (cutoff - now).total_seconds(),
            phase=WAITING, phase_tone=PHASE_TONE[WAITING],
            phase_reason="waiting for farmer", phase_until=None, mid=None,
            book_mid=None, band=None, fav_bid=None, long_bid=None, slots=[], q1=0.0,
            q2=0.0, q_balance_pct=0.0, naked_used_usd=0.0, budget_used_usd=0.0,
            free_bid_usd=0.0, naked_pct=0.0, position=None,
            paused=market.paused, naked_paused=market.naked_paused, status=market.status,
        )
    fav_bid, long_bid = resting(state, "FAV_BID"), resting(state, "LONG_BID")
    fav_view = state.slots.get("FAV_BID") or {}
    mid = state.mid if state.mid is not None else state.book_mid
    high = max(state.q1, state.q2)
    used, free = budget_used(state), free_bid(state)
    return Card(
        condition_id=market.condition_id, name=market_name(market), slug=market.slug,
        mode=metrics.get("plan", {}).get("mode"), fav_outcome=m.get("fav_outcome"),
        p_true=market.p_true, budget_usd=budget, capital_usd=state.capital_usd,
        cutoff=cutoff, cutoff_in_s=cutoff and (cutoff - now).total_seconds(),
        phase=state.phase, phase_tone=PHASE_TONE.get(state.phase, "t-n"),
        phase_reason=state.phase_reason, phase_until=state.phase_until,
        mid=state.mid, book_mid=state.book_mid,
        band=band_strip(mid, m.get("v"), fav_bid and fav_bid.price,
                        long_bid and long_bid.price),
        fav_bid=fav_bid, long_bid=long_bid, slots=slot_rows(state),
        q1=state.q1, q2=state.q2,
        q_balance_pct=round(min(state.q1, state.q2) / high * 100, 1) if high else 0.0,
        naked_used_usd=state.naked_used_usd, budget_used_usd=used,
        free_bid_usd=free.notional if free else 0.0,
        naked_pct=round(min(100.0, used / budget * 100), 1) if budget else 0.0,
        position=state.position, paused=market.paused, naked_paused=market.naked_paused,
        status=market.status, economics=fav_view.get("economics"),
        fav_reason=fav_view.get("fav_reason") or "",
        history=fill_cost.history_from_json(fav_view.get("history")),
        long_return=(state.slots.get("LONG_BID") or {}).get("fill_return"),
    )


def tracked(snap, now):
    """Cards of the managed markets, then the approved ones still waiting."""
    managed = [card(snap.markets[cid], s, snap.status, now)
               for cid, s in snap.states.items() if cid in snap.markets]
    waiting = [card(m, None, snap.status, now) for cid, m in snap.markets.items()
               if cid not in snap.states and m.status == "approved"]
    return managed + waiting


@dataclass(frozen=True)
class MarketsPage:
    phase: Optional[str]
    filters: list
    cards: list


def markets_page(snap, phase, now):
    cards = tracked(snap, now)
    shown = [c for c in cards if phase is None or c.phase == phase]
    return MarketsPage(phase, [None, *PHASES, WAITING], shown)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def live_daily(state):
    """The reward $/day the farmer modeled this cycle against the live pool and
    book, 0.0 quoting without a favorite bid, or None when not quoting: a
    paused market's economics are only a preview."""
    if state.phase != "QUOTING":
        return None
    economics = (state.slots.get("FAV_BID") or {}).get("economics")
    return economics["reward_per_day"] if economics else 0.0


@dataclass(frozen=True)
class Totals:
    quoting: int
    approved: int
    fav_bid_usd: float
    # naked_usd is every resting longshot bid, and naked_counted_usd the part
    # of it the naked budgets limit.
    naked_usd: float
    naked_counted_usd: float
    naked_budget_usd: float
    inventory_usd: float
    est_daily_usd: float
    live_daily_usd: float
    earned_today_usd: Optional[float]

    @property
    def live_hourly_usd(self):
        return self.live_daily_usd / 24


def totals(snap, now):
    quoting = [cid for cid, s in snap.states.items() if s.phase == "QUOTING"]
    today = now.strftime("%Y-%m-%d")
    earned = [usd for date, _, usd in snap.rewards if date == today]

    def notional(slot):
        orders = (resting(s, slot) for s in snap.states.values())
        return sum(o.notional for o in orders if o)

    def inventory(state):
        p = state.position or {}
        farm, avg = p.get("farm", {}), p.get("avg_cost", {})
        return sum(farm.get(k, 0.0) * avg.get(k, 0.0) for k in ("fav", "long"))

    return Totals(
        quoting=len(quoting),
        approved=sum(m.status == "approved" for m in snap.markets.values()),
        fav_bid_usd=notional("FAV_BID"),
        naked_usd=notional("LONG_BID"),
        naked_counted_usd=sum(budget_counted(s) for s in snap.states.values()),
        naked_budget_usd=sum(snap.markets[c].naked_budget_usd or 0.0
                             for c in quoting if c in snap.markets),
        inventory_usd=sum(inventory(s) for s in snap.states.values()),
        est_daily_usd=sum(snap.markets[c].metrics.get("est_daily_usd", 0.0)
                          for c in quoting if c in snap.markets),
        live_daily_usd=sum(live_daily(s) or 0.0 for s in snap.states.values()),
        earned_today_usd=sum(earned) if earned else None,
    )


@dataclass(frozen=True)
class Alert:
    kind: str
    tone: str
    text: str
    condition_id: Optional[str]
    href: str
    action: str


class AlertRule(NamedTuple):
    """A kind of alert: find yields (condition_id or None, text) per instance;
    href is where its action is, with {cid} for the market."""

    kind: str
    tone: str
    action: str
    find: Callable[[Snapshot, datetime], Iterable[tuple]]
    href: str = "/markets/{cid}"


def _name(snap, cid):
    return market_name(snap.markets.get(cid), cid)


def _in_phase(phase, default_reason):
    def find(snap, now):
        for cid, s in snap.states.items():
            if s.phase == phase:
                until = f" until {clock(s.phase_until)}" if s.phase_until else ""
                yield cid, (f"{_name(snap, cid)}: {s.phase_reason or default_reason}"
                            f"{until}. All its orders are down.")
    return find


def _budget_bound(snap, now):
    """A budget is bound when it is used up and no longshot bid rests free of
    it: that bid balances the favorite bid whatever the budget."""
    for cid, s in snap.states.items():
        market = snap.markets.get(cid)
        budget = (market.naked_budget_usd if market else None) or 0.0
        used = budget_used(s)
        if budget > 0 and used >= BUDGET_BOUND * budget and free_bid(s) is None:
            yield cid, (f"{_name(snap, cid)}: the naked longshot leg uses "
                        f"{usd(used)} of its {usd(budget)} budget, so the "
                        f"favorite bid is trimmed to match.")


def _low_score(snap, now):
    for cid, s in snap.states.items():
        target = (s.slots.get("FAV_BID") or {}).get("target")
        if target and target["score"] < LOW_SCORE:
            yield cid, (f"{_name(snap, cid)}: the favorite bid at {cents(target['price'])} "
                        f"scores {target['score']:.3f} per share, under {LOW_SCORE:g}.")


def _backoff(snap, now):
    for cid, s in snap.states.items():
        for slot, b in sorted(s.backoff.items()):
            until = datetime.fromisoformat(b["until"]) if b.get("until") else None
            yield cid, (f"{_name(snap, cid)}: {slot} was rejected {b.get('attempts', 1)} "
                        f"time(s) and waits until {clock(until)}.")


def _naked_fill(snap, now):
    for e in snap.fills:
        if e.slot == "LONG_BID" and now - e.at <= timedelta(hours=24):
            yield e.condition_id, (f"{_name(snap, e.condition_id)}: longshot bid filled "
                                   f"{shares(e.size)} shares at {clock(e.at)}.")


def _not_running(snap, now):
    live = liveness(snap.status, now)
    if not live.running:
        yield None, live.message


ALERT_RULES = (
    AlertRule("Not running", "t-r", "Farmer status", _not_running, "/settings"),
    AlertRule("Naked fill", "t-r", "Naked leg controls", _naked_fill),
    AlertRule("Cooldown", "t-c", "View market", _in_phase("COOLDOWN", "ask drop")),
    AlertRule("Below range", "t-b", "View market",
              _in_phase("BELOW_RANGE", "the mid is under min_fav")),
    AlertRule("Paused", "t-n", "Resume", _in_phase("PAUSED", "paused")),
    AlertRule("Budget bound", "t-b", "Edit budget", _budget_bound, "/review/{cid}"),
    AlertRule("Low score", "t-n", "Edit p_true", _low_score, "/review/{cid}"),
    AlertRule("Backoff", "t-r", "View market", _backoff),
)


def alerts(snap, now):
    return [
        Alert(rule.kind, rule.tone, text, cid, rule.href.format(cid=cid), rule.action)
        for rule in ALERT_RULES
        for cid, text in rule.find(snap, now)
    ]


@dataclass(frozen=True)
class Overview:
    totals: Totals
    mode: Optional[str]
    alerts: list
    markets: list
    events: list


def overview(snap, events, now):
    return Overview(
        totals=totals(snap, now),
        mode=liveness(snap.status, now).mode,
        alerts=alerts(snap, now),
        markets=tracked(snap, now),
        events=[event_view(e) for e in events[:FEED_EVENTS]],
    )


# ---------------------------------------------------------------------------
# Market detail
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BookRow:
    side: str
    price: float
    size: Optional[float]
    ours: float
    score: Optional[float]


def book_rows(book, mid, v):
    """Asks high to low, the midpoint, then bids high to low, scored at the mid."""
    def scored(side, level):
        s = abs(level["price"] - mid) if mid is not None else None
        sc = ((v - s) / v) ** 2 if v and s is not None and s < v else None
        return BookRow(side, level["price"], level["size"], level.get("ours", 0.0), sc)

    asks = sorted(book.get("asks", []), key=lambda lv: -lv["price"])
    bids = sorted(book.get("bids", []), key=lambda lv: -lv["price"])
    rows = [scored("ask", lv) for lv in asks]
    if mid is not None:
        rows.append(BookRow("mid", mid, None, 0.0, None))
    return rows + [scored("bid", lv) for lv in bids]


@dataclass(frozen=True)
class TimelineItem:
    at: Optional[datetime]
    text: str
    upcoming: bool = False


def timeline(market, events, cutoff, now):
    """Approval, then the market's PHASE and RETIRE events and the farmer's
    START events since the market's first event, then the expiry cutoff."""
    own = [e for e in events if e.condition_id == market.condition_id
           and e.kind in (EventKind.PHASE, EventKind.RETIRE)]
    first = min((e.at for e in own), default=None)
    starts = [e for e in events if e.kind is EventKind.START
              and first is not None and e.at >= first]
    capital = usd(market.capital_usd) if market.capital_usd is not None else "farmer default"
    items = [TimelineItem(market.first_seen, (
        f"First seen by the screener. Status {market.status}"
        + (f", p_true {market.p_true:.3f}, naked budget {usd(market.naked_budget_usd)}, "
           f"capital {capital}" if market.p_true is not None else "")
        + (f". Note: {market.note}" if market.note else "") + "."))]
    items += [TimelineItem(e.at, e.message) for e in sorted(own + starts, key=lambda e: e.id)]
    if cutoff is not None:
        items.append(TimelineItem(cutoff, "Expiry cutoff: orders expire (GTD) and the "
                                          "market retires.", upcoming=cutoff > now))
    return items


@dataclass(frozen=True)
class FillRow:
    id: int
    at: datetime
    condition_id: Optional[str]
    name: str
    slot: Optional[str]
    tone: str
    price: Optional[float]
    size: Optional[float]
    edge: Optional[float]
    effect: str


def fill_row(e, markets):
    market = markets.get(e.condition_id)
    edge = None
    if e.slot == "FAV_BID" and e.price is not None and market and market.p_true is not None:
        edge = market.p_true - e.price
    return FillRow(e.id, e.at, e.condition_id, market_name(market, e.condition_id or ""),
                   e.slot, SLOT_TONE.get(e.slot, "t-n"), e.price, e.size, edge,
                   FILL_EFFECT.get(e.slot, ""))


@dataclass(frozen=True)
class MidPoint:
    at: datetime
    mid: Optional[float]
    book_mid: Optional[float]
    fav_bid: Optional[float]
    long_bid: Optional[float]


@dataclass(frozen=True)
class Marker:
    at: datetime
    kind: str
    text: str


@dataclass(frozen=True)
class MarketDetail:
    card: Card
    status: str
    v: Optional[float]
    min_size: Optional[float]
    tick: Optional[float]
    phase_since: Optional[datetime]
    fav_bid_cap_usd: Optional[float]
    book: list
    timeline: list
    fills: list
    events: list
    mids: list
    markers: list


def market_detail(market, state, status, events, mids, now):
    """events: this market's events and the farmer's START events, newest first."""
    c = card(market, state, status, now)
    m = market.metrics.get("market", {})
    own = [e for e in events if e.condition_id == market.condition_id]
    phase_since = next((e.at for e in own if e.kind is EventKind.PHASE), None)
    markets = {market.condition_id: market}
    return MarketDetail(
        card=c, status=market.status, v=m.get("v"),
        min_size=m.get("min_size"), tick=m.get("tick"), phase_since=phase_since,
        fav_bid_cap_usd=c.capital_usd,
        book=book_rows(state.book, c.mid if c.mid is not None else c.book_mid, m.get("v"))
        if state else [],
        timeline=timeline(market, events, c.cutoff, now),
        fills=[fill_row(e, markets) for e in own if e.kind is EventKind.FILL],
        events=[event_view(e) for e in own],
        mids=[MidPoint(m.at, m.mid, m.book_mid, m.fav_bid, m.long_bid) for m in mids],
        markers=[Marker(e.at, e.kind.value, e.message) for e in own
                 if e.kind in (EventKind.FILL, EventKind.PHASE)],
    )


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------

TABS = ("rewards", "fills", "inventory", "log")


@dataclass(frozen=True)
class RewardRow:
    condition_id: str
    name: str
    rate_daily_usd: Optional[float]
    est_daily_usd: Optional[float]
    scoring_pct: Optional[float]
    today_usd: float
    week_usd: float
    total_usd: float

    @property
    def rate_hourly_usd(self):
        return None if self.rate_daily_usd is None else self.rate_daily_usd / 24


@dataclass(frozen=True)
class RewardsTab:
    mode: Optional[str]
    today_usd: Optional[float]
    week_usd: Optional[float]
    total_usd: Optional[float]
    est_daily_usd: float
    live_daily_usd: float
    quoting: int
    realised_ratio: Optional[float]
    rows: list
    daily: list

    @property
    def live_hourly_usd(self):
        return self.live_daily_usd / 24


def rewards_tab(snap, earned_totals, now):
    """earned_totals is {condition_id: usd} over every recorded day; the
    snapshot's rewards reach back 30 days only."""
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    by_market = {}
    for date, cid, amount in snap.rewards:
        today_usd, week_usd = by_market.get(cid, (0.0, 0.0))
        by_market[cid] = (today_usd + (amount if date == today else 0.0),
                          week_usd + (amount if date >= week_start else 0.0))
    cids = dict.fromkeys([*snap.states, *by_market, *earned_totals])
    rows = sorted((RewardRow(
        cid, _name(snap, cid),
        live_daily(snap.states[cid]) if cid in snap.states else None,
        snap.markets[cid].metrics.get("est_daily_usd") if cid in snap.markets else None,
        snap.states[cid].scoring_pct if cid in snap.states else None,
        *by_market.get(cid, (0.0, 0.0)),
        earned_totals.get(cid, 0.0),
    ) for cid in cids), key=lambda r: (r.rate_daily_usd is None, -(r.rate_daily_usd or 0.0),
                                       -r.total_usd))
    t = totals(snap, now)
    week = sum(r.week_usd for r in rows) if snap.rewards else None
    ratio = week / 7 / t.est_daily_usd if week is not None and t.est_daily_usd else None
    return RewardsTab(liveness(snap.status, now).mode, t.earned_today_usd, week,
                      sum(earned_totals.values()) if earned_totals else None,
                      t.est_daily_usd, t.live_daily_usd, t.quoting, ratio, rows, snap.rewards)


@dataclass(frozen=True)
class InventoryRow:
    condition_id: str
    name: str
    position: dict
    exits: dict


def inventory_tab(snap):
    """Managed markets' holdings, with the FAV_ASK and LONG_ASK exits resting."""
    return [InventoryRow(cid, _name(snap, cid), s.position or {},
                         {slot: o for slot in ("FAV_ASK", "LONG_ASK")
                          if (o := resting(s, slot))})
            for cid, s in snap.states.items()]


@dataclass(frozen=True)
class LogPage:
    kind: Optional[str]
    condition_id: Optional[str]
    kinds: list
    markets: list
    events: list
    older_than: Optional[int]


def log_page(snap, events, kind, condition_id):
    """events: one page plus one more row, newest first, to tell whether older exist."""
    page = events[:LOG_PAGE]
    return LogPage(
        kind, condition_id, [k.value for k in EventKind],
        sorted(((cid, market_name(m)) for cid, m in snap.markets.items()),
               key=lambda pair: pair[1]),
        [event_view(e) for e in page],
        page[-1].id if len(events) > LOG_PAGE else None,
    )


def fills_tab(snap, fill_events):
    return [fill_row(e, snap.markets) for e in fill_events]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SettingRow:
    key: str
    label: str
    value: float
    source: str
    text: str
    error: Optional[str]


def setting_value(key, status, overrides, defaults):
    """(value, source): the override, else what the farmer last ran with, else
    the farmer's built-in default."""
    if key in overrides:
        return overrides[key], "override"
    if status is not None and key in status.config:
        return status.config[key], "farmer"
    return defaults[key], "default"


def number_text(value):
    return f"{value:g}"


def setting_rows(fields, group, status, overrides, bad_rows, defaults, submitted, errors):
    """One group's rows; submitted and errors are a failed save's, shown back as typed."""
    rows = []
    for key, field in fields.items():
        if field.group != group:
            continue
        value, source = setting_value(key, status, overrides, defaults)
        stored_error = f"Stored row ignored: {bad_rows[key]}" if key in bad_rows else None
        rows.append(SettingRow(key, field.label, value, source,
                               submitted.get(key, number_text(value)),
                               errors.get(key) or stored_error))
    return rows


# ---------------------------------------------------------------------------
# Discover
# ---------------------------------------------------------------------------

DISCOVER_PAGE = 50
MODES = ("floor", "budget")
STATUS_FILTERS = ("candidate", "approved", "rejected", "retired", "all")
STATUS_TONE = {"candidate": "t-n", "approved": "t-q", "rejected": "t-r", "retired": "t-c"}


@dataclass(frozen=True)
class DiscoverFilters:
    mode: Optional[str] = None
    q: str = ""
    status: str = "candidate"
    min_days: Optional[float] = None
    page: int = 1

    def query(self, **changes):
        """This filter set as a query string, with changes, back on page 1 unless given."""
        f = {**asdict(self), "page": 1, **changes}
        return "?" + urlencode([(k, number_text(v) if isinstance(v, float) else v)
                                for k, v in f.items()
                                if v not in (None, "") and not (k == "page" and v == 1)])


@dataclass(frozen=True)
class DiscoverRow:
    rank: int
    condition_id: str
    name: str
    slug: Optional[str]
    mode: Optional[str]
    mode_tone: str
    fav: str
    pool: Optional[float]
    v: Optional[float]
    est_daily_usd: Optional[float]
    capital_usd: Optional[float]
    net_apr: Optional[float]
    days: Optional[float]
    ahead: Optional[float]
    volume: str
    status: str
    status_tone: str


def mode_tone(mode):
    return "t-b" if (mode or "").lower() == "budget" else "t-f"


def human_volume(value):
    if value is None:
        return "--"
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return f"{value:.0f}"


def discover_row(rank, market, status, now):
    m = market.metrics
    info, plan = m.get("market", {}), m.get("plan", {})
    cutoff = expiry_cutoff(market, status)
    return DiscoverRow(
        rank=rank, condition_id=market.condition_id, name=market_name(market),
        slug=market.slug, mode=plan.get("mode"), mode_tone=mode_tone(plan.get("mode")),
        fav=f"{info.get('fav_outcome', '?')} {cents(m.get('book_mid', m.get('mid')))}",
        pool=info.get("rate_per_day"), v=info.get("v"), est_daily_usd=m.get("est_daily_usd"),
        capital_usd=m.get("capital_usd"),
        # A row scanned before fill-cost pricing has only the gross APR.
        net_apr=m.get("net_apr", m.get("apr")),
        days=(cutoff - now).total_seconds() / 86400 if cutoff else None,
        ahead=m.get("depth_ahead_ratio"), volume=human_volume(m.get("volume24hr")),
        status=market.status, status_tone=STATUS_TONE.get(market.status, "t-n"),
    )


@dataclass(frozen=True)
class DiscoverPage:
    filters: DiscoverFilters
    rows: list
    total: int
    pages: int


def discover_page(filters, markets, total, status, now):
    start = (filters.page - 1) * DISCOVER_PAGE
    return DiscoverPage(filters, [discover_row(start + i + 1, m, status, now)
                                  for i, m in enumerate(markets)],
                        total, max(1, -(-total // DISCOVER_PAGE)))


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

OTHER_OUTCOME = {"Yes": "No", "No": "Yes"}


@dataclass(frozen=True)
class Queue:
    position: Optional[int]
    total: int
    prev: Optional[str]
    next: Optional[str]


def queue(ids, condition_id):
    """Where a market sits among the undecided candidates (ids, best first)."""
    if condition_id not in ids:
        return Queue(None, len(ids), None, ids[0] if ids else None)
    i = ids.index(condition_id)
    return Queue(i + 1, len(ids), ids[i - 1] if i > 0 else None,
                 ids[i + 1] if i + 1 < len(ids) else None)


def after_decision(ids, condition_id):
    """The candidate to review once this one is decided: the next, else the previous."""
    q = queue(ids, condition_id)
    return q.next or q.prev


# ---------------------------------------------------------------------------
# Review: the farmer's orders against the live book
# ---------------------------------------------------------------------------

LADDER_LEVELS = 12


@dataclass(frozen=True)
class PreviewOrder:
    """One order the farmer would place. price is in its own outcome's terms;
    book_price is where it rests in the favorite book (a longshot bid shows
    there as an ask at its mirror)."""

    slot: str
    tone: str
    outcome: str
    price: float
    size: float
    score: float
    cost: float
    book_price: float


@dataclass(frozen=True)
class LadderRow:
    price: float
    size: float
    total: float
    depth_pct: float
    score: Optional[float]
    ours: Optional[PreviewOrder]


@dataclass(frozen=True)
class LiveReview:
    """The favorite book now, and what the farmer would place against it.

    asks run highest price first and bids best first, so the two meet at the
    midpoint. total is the size resting from the inside out to that level, and
    outside the shares on each side beyond the levels shown.
    """

    read_at: datetime
    fav_outcome: str
    long_outcome: str
    book_mid: Optional[float]
    mid: Optional[float]
    note: str
    orders: list
    strip: Optional[BandStrip]
    asks: list
    bids: list
    outside_asks: float
    outside_bids: float
    # The favorite bid's economics, where its fills came from (as
    # fill_cost.fill_source names it) with the fill prior behind them, and
    # the daily pool it was valued against.
    value: Optional[reward_farmer.QuoteValue] = None
    fill_source: str = "default"
    prior: Optional[fill_cost.Prior] = None
    pool_per_day: Optional[float] = None

    @property
    def share(self):
        """Our share of the pool: value_quote pays the pool times our share of the score."""
        if self.value is None or not self.pool_per_day:
            return None
        return self.value.reward_per_day / self.pool_per_day


def rewards_state(info, condition_id, total_rates):
    """reward_farmer.rewards_active, or None while the answer waits on
    total_rates, which is None until the slow listing is first read."""
    active = reward_farmer.rewards_active(info, condition_id, total_rates)
    # Before the listing is read the rule assumes active. That answer is real
    # only when an empty listing would give the same one.
    if total_rates is None and active != reward_farmer.rewards_active(info, condition_id, {}):
        return None
    return active


def live_review(row, info, levels, total_rates, cfg, now, managed, prior):
    """row: the market's farm.db row carrying the terms to preview; levels: the
    favorite token's live book; total_rates: as reward_farmer.rewards_active
    takes it; prior: the stored fill prior. Planning is the farmer's own, from
    a zero farm position, since a new market's holdings become its baseline.
    A market the farmer already manages gets the book without a plan.

    A row with no p_true is valued at the book midpoint, as the scan values
    it. Fills come from the history the scan stored shrunk toward prior, or
    prior alone without one; the console never reads a history itself, and
    the farmer would not bid until it has read one."""
    metrics = json.loads(row["metrics"])
    fav = metrics["market"].get("fav_outcome") or "favorite"
    long = OTHER_OUTCOME.get(fav, "longshot")
    book_mid = adjusted_mid(levels.bids, levels.asks, info.min_size)
    target, note, pool = reward_farmer.Target(book_mid, []), "", None
    history = fill_cost.history_from_json(metrics.get("history"))
    if row["p_true"] is None:
        row = {**dict(row), "p_true": book_mid}
    fm = reward_farmer.farm_market(row, info, cfg, now)
    if not isinstance(fm, Skip):
        # A preview has no cycles behind it. It shows the plan the farmer
        # settles on once the midpoint has held for FREE_DWELL_S.
        fm = fm._replace(zones=fm.zones._replace(steady_mid=book_mid))
    if managed:
        note = "The farmer manages this market. Its live orders are on the market page."
    elif isinstance(fm, Skip):
        note = f"The farmer cannot manage this market: {fm.reason}."
    else:
        fm = fm._replace(tick=levels.tick or fm.tick)
        obs = reward_farmer.Observation(
            mid=book_mid, ask=reward_farmer.drop_ask(levels.asks, info.min_size), recent_asks=(),
            rewards_active=reward_farmer.rewards_active(info, fm.condition_id, total_rates),
            accepting_orders=info.accepting_orders, closed=info.closed, approved=True,
            expires_at=fm.expires_at)
        state = reward_farmer.step_state(reward_farmer.State(reward_farmer.Phase.QUOTING),
                                         obs, now, cfg)
        if state.phase is reward_farmer.Phase.QUOTING:
            outlook = reward_farmer.market_outlook(fm, total_rates, history, prior, cfg, now)
            pool = outlook.rate_per_day
            target = reward_farmer.target_orders(fm, levels, reward_farmer.Position(), cfg, True,
                                                 outlook, bid_without_history=True)
            if not target.intents:
                note = f"The farmer would place nothing: {target.fav.reason}."
        else:
            why = state.reason or (f"the midpoint {cents(book_mid)} is under min_fav "
                                   f"{cents(cfg.min_fav)}")
            note = f"The farmer would not quote: {why}."

    orders = []
    for i in target.intents:
        is_fav = i.slot.role is reward_farmer.Role.FAV
        orders.append(PreviewOrder(
            i.slot.name, SLOT_TONE[i.slot.name], fav if is_fav else long, i.price, i.size,
            i.score, i.price * i.size, i.price if is_fav else round(1 - i.price, 6)))

    mid = target.mid if target.mid is not None else book_mid
    ours = {(o.book_price, o.slot == "FAV_BID"): o for o in orders}

    def side(entries, is_bid):
        sizes = dict(entries)
        prices = sorted(set(sizes) | {p for p, bid in ours if bid == is_bid}, reverse=is_bid)
        rows, total = [], 0.0
        for n, price in enumerate(prices):
            total += sizes.get(price, 0.0)
            mine = ours.get((price, is_bid))
            if n < LADDER_LEVELS or mine:
                in_band = mid is not None and abs(price - mid) < info.v
                rows.append((price, sizes.get(price, 0.0), total,
                             share_score(info.v, abs(price - mid)) if in_band else None, mine))
        return rows, total - sum(r[1] for r in rows)

    ask_rows, outside_asks = side(levels.asks, False)
    bid_rows, outside_bids = side(levels.bids, True)
    deepest = max([r[2] for r in ask_rows + bid_rows] or [0.0]) or 1.0

    def ladder(rows):
        return [LadderRow(price, size, total, round(total / deepest * 100, 1), score, mine)
                for price, size, total, score, mine in rows]

    by_slot = {o.slot: o.price for o in orders}
    book = [("book", price, f"{label} {cents(price)}")
            for price, label in ((levels.bids[0][0] if levels.bids else None, "best bid"),
                                 (levels.asks[0][0] if levels.asks else None, "best ask"))
            if price is not None]
    strip = band_strip(mid, info.v, by_slot.get("FAV_BID"), by_slot.get("LONG_BID"), book)
    if strip is not None:
        names = {"fav_bid": "our bid", "mid": "mid", "long_bid": "longshot bid (mirror)"}
        strip = BandStrip(strip.lo, strip.hi, sorted(
            (k if k.label else replace(k, label=f"{names[k.kind]} {cents(k.price)}")
             for k in strip.marks), key=lambda k: k.price))
    return LiveReview(now, fav, long, book_mid, mid, note, orders, strip,
                      list(reversed(ladder(ask_rows))), ladder(bid_rows),
                      outside_asks, outside_bids, target.fav.best,
                      fill_cost.fill_source(history, prior, cfg.min_fav), prior, pool)


@dataclass(frozen=True)
class EvCheck:
    tone: str
    lines: list


def _float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def p_true_start(metrics):
    """The whole percent an empty p_true steps to first: the scanned favorite
    mid, rounded, or None without one."""
    mid = metrics.get("book_mid")
    return None if mid is None else min(max(round(mid * 100), 1), 99)


def ev_check(metrics, p_true, budget_text, ev_margin, years_left):
    """What the farmer will do with this p_true (a probability, None while the
    typed percent is invalid) and budget, by its own rule: the favorite bid
    never prices above floor_to_tick(p_true - ev_margin). The budget is held
    against what the planned longshot leg takes of it with years_left to the
    end date (planned_budget_cost)."""
    plan, m = metrics.get("plan"), metrics.get("market", {})
    if p_true is None or not plan:
        return EvCheck("c-warn", [P_TRUE_ERROR])
    tick = m.get("tick") or 0.001
    bid, cap = plan["fav_bid_price"], floor_to_tick(p_true - ev_margin, tick)
    margin = f"p_true − {cents(ev_margin)}"
    if bid <= cap + 1e-9:
        tone, lines = "c-good", [f"Bid {cents(bid)} is within your {cents(cap)} cap ({margin}): "
                                 f"a fill is +EV."]
    else:
        best_bid, best_ask = metrics.get("best_bid"), metrics.get("best_ask")
        # A scan row from before the book's best levels were stored has only the
        # midpoint the planned bid created.
        mid = (plan["mid"] if best_bid is None or best_ask is None
               else created_mid(cap, best_bid, best_ask))
        capped = share_score(m.get("v") or 0.0, mid - cap) if cap >= tick else 0.0
        then = (f"scores {capped:.3f}, not {plan['fav_bid_score']:.3f}." if capped > 0 else
                "outside the band, so no favorite bid and no reward.")
        tone, lines = "c-warn", [f"Bid capped at {cents(cap)} ({margin}): {then}"]
    budget = _float(budget_text)
    long_cost = planned_budget_cost(metrics, years_left)
    if budget is not None and budget < long_cost - 1e-9:
        tone = "c-warn"
        lines.append(f"Longshot {usd(long_cost)} is over your {usd(budget)} budget: "
                     f"re-priced to fit.")
    return EvCheck(tone, lines)
