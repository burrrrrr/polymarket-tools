"""
View models of the farm console: pure functions from farm.db reads to what a
page shows. Every derived number, alert, and class name is decided here, so
the templates only lay values out and the JSON API serves the same objects.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Callable, Iterable, NamedTuple, Optional
from urllib.parse import urlencode

from farm_db import EventKind, FarmerStatus, resting_order
from reward_screener import floor_to_tick
from reward_screener import score as share_score

SLOTS = ("FAV_BID", "LONG_BID", "FAV_ASK", "LONG_ASK")
PHASES = ("QUOTING", "COOLDOWN", "BELOW_RANGE", "PAUSED", "RETIRED")
WAITING = "WAITING"

# A farmer whose last cycle is older than this many intervals is not running.
STALE_CYCLES = 3
LOW_SCORE = 0.10
BUDGET_BOUND = 0.99
DEFAULT_EXPIRY_BUFFER_DAYS = 7.0
FEED_EVENTS = 12
LOG_PAGE = 50

PHASE_TONE = {"QUOTING": "t-q", "COOLDOWN": "t-c", "BELOW_RANGE": "t-b",
              "PAUSED": "t-n", "RETIRED": "t-r", WAITING: "t-n"}
SLOT_TONE = {"FAV_BID": "t-f", "FAV_ASK": "t-f", "LONG_BID": "t-b", "LONG_ASK": "t-b"}
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
class Chrome:
    """The layout around every page: the farmer status and the nav counts."""

    liveness: Liveness
    candidates: int
    quoting: int


def chrome(snap, now):
    return Chrome(liveness(snap.status, now), snap.counts.get("candidate", 0),
                  sum(s.phase == "QUOTING" for s in snap.states.values()))


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
    naked_used_usd: float
    naked_pct: float
    position: Optional[dict]
    paused: bool
    naked_paused: bool
    status: str


def card(market, state, status, now):
    """One market's live card; a market without farm_state is WAITING."""
    metrics = market.metrics
    m = metrics.get("market", {})
    cutoff = expiry_cutoff(market, status)
    budget = market.naked_budget_usd or 0.0
    if state is None:
        return Card(
            condition_id=market.condition_id, name=market_name(market),
            mode=metrics.get("plan", {}).get("mode"), fav_outcome=m.get("fav_outcome"),
            p_true=market.p_true, budget_usd=budget, capital_usd=market.capital_usd,
            cutoff=cutoff, cutoff_in_s=cutoff and (cutoff - now).total_seconds(),
            phase=WAITING, phase_tone=PHASE_TONE[WAITING],
            phase_reason="waiting for farmer", phase_until=None, mid=None,
            book_mid=None, band=None, fav_bid=None, long_bid=None, slots=[], q1=0.0,
            q2=0.0, q_balance_pct=0.0, naked_used_usd=0.0, naked_pct=0.0, position=None,
            paused=market.paused, naked_paused=market.naked_paused, status=market.status,
        )
    fav_bid, long_bid = resting(state, "FAV_BID"), resting(state, "LONG_BID")
    mid = state.mid if state.mid is not None else state.book_mid
    high = max(state.q1, state.q2)
    return Card(
        condition_id=market.condition_id, name=market_name(market),
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
        naked_used_usd=state.naked_used_usd,
        naked_pct=round(min(100.0, state.naked_used_usd / budget * 100), 1) if budget else 0.0,
        position=state.position, paused=market.paused, naked_paused=market.naked_paused,
        status=market.status,
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

@dataclass(frozen=True)
class Totals:
    quoting: int
    approved: int
    fav_bid_usd: float
    naked_usd: float
    naked_budget_usd: float
    inventory_usd: float
    est_daily_usd: float
    earned_today_usd: Optional[float]


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
        naked_budget_usd=sum(snap.markets[c].naked_budget_usd or 0.0
                             for c in quoting if c in snap.markets),
        inventory_usd=sum(inventory(s) for s in snap.states.values()),
        est_daily_usd=sum(snap.markets[c].metrics.get("est_daily_usd", 0.0)
                          for c in quoting if c in snap.markets),
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
    for cid, s in snap.states.items():
        budget = (snap.markets[cid].naked_budget_usd if cid in snap.markets else None) or 0.0
        if budget > 0 and s.naked_used_usd >= BUDGET_BOUND * budget:
            yield cid, (f"{_name(snap, cid)}: the naked longshot leg uses "
                        f"{usd(s.naked_used_usd)} of its {usd(budget)} budget, so the "
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
    AlertRule("Cooldown", "t-c", "View market", _in_phase("COOLDOWN", "mid drop")),
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
    slug: Optional[str]
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
        card=c, slug=market.slug, status=market.status, v=m.get("v"),
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
    est_daily_usd: Optional[float]
    scoring_pct: Optional[float]
    today_usd: float
    week_usd: float


@dataclass(frozen=True)
class RewardsTab:
    mode: Optional[str]
    today_usd: Optional[float]
    week_usd: Optional[float]
    est_daily_usd: float
    quoting: int
    realised_ratio: Optional[float]
    rows: list
    daily: list


def rewards_tab(snap, now):
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=6)).strftime("%Y-%m-%d")
    by_market = {}
    for date, cid, amount in snap.rewards:
        today_usd, week_usd = by_market.get(cid, (0.0, 0.0))
        by_market[cid] = (today_usd + (amount if date == today else 0.0),
                          week_usd + (amount if date >= week_start else 0.0))
    cids = list(dict.fromkeys([*snap.states, *by_market]))
    rows = [RewardRow(
        cid, _name(snap, cid),
        snap.markets[cid].metrics.get("est_daily_usd") if cid in snap.markets else None,
        snap.states[cid].scoring_pct if cid in snap.states else None,
        *by_market.get(cid, (0.0, 0.0)),
    ) for cid in cids]
    t = totals(snap, now)
    week = sum(r.week_usd for r in rows) if snap.rewards else None
    ratio = week / 7 / t.est_daily_usd if week is not None and t.est_daily_usd else None
    return RewardsTab(liveness(snap.status, now).mode, t.earned_today_usd, week,
                      t.est_daily_usd, t.quoting, ratio, rows, snap.rewards)


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
    hint: str
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
        rows.append(SettingRow(key, field.label, field.hint, value, source,
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
    apr: Optional[float]
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
        capital_usd=m.get("capital_usd"), apr=m.get("apr"),
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
class PlannedOrder:
    slot: str
    tone: str
    order: str
    size: float
    score: float
    cost: float
    why: str


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


def planned_orders(metrics):
    plan, m = metrics.get("plan"), metrics.get("market", {})
    if not plan:
        return []
    fav = m.get("fav_outcome") or "favorite"
    long = OTHER_OUTCOME.get(fav, "longshot")
    book_mid = metrics.get("book_mid", plan["mid"])
    if abs(plan["mid"] - book_mid) > 1e-9:
        fav_why = ("Becomes the best bid, priced two offsets under the best ask so it "
                   "sits one offset from the midpoint it creates.")
    else:
        fav_why = f"One offset under the {cents(book_mid)} midpoint."
    long_cost = plan["low_price"] * plan["low_size"]
    if plan.get("mode") == "floor":
        long_why = (f"Floor bid: the cheapest tick that still scores, sized so its score "
                    f"matches the favorite bid's. It ties up {usd(long_cost)}.")
    else:
        long_why = (f"The lowest longshot price that still scores, sized so its score "
                    f"matches the favorite bid's. Its {usd(long_cost)} is naked risk, "
                    f"capped by the naked budget.")
    return [
        PlannedOrder("FAV_BID", SLOT_TONE["FAV_BID"],
                     f"BUY {fav} {cents(plan['fav_bid_price'])}", plan["fav_bid_size"],
                     plan["fav_bid_score"], plan["fav_bid_price"] * plan["fav_bid_size"],
                     fav_why),
        PlannedOrder("LONG_BID", SLOT_TONE["LONG_BID"], f"BUY {long} {cents(plan['low_price'])}",
                     plan["low_size"], plan["low_score"], long_cost, long_why),
    ]


def review_strip(metrics):
    """The band around the planned midpoint with the scanned book's best bid and
    ask and our planned bids."""
    plan, m = metrics.get("plan"), metrics.get("market", {})
    if not plan:
        return None
    book = [("book", price, f"{label} {cents(price)}")
            for price, label in ((metrics.get("best_bid"), "best bid"),
                                 (metrics.get("best_ask"), "best ask"))
            if price is not None]
    strip = band_strip(plan["mid"], m.get("v"), plan["fav_bid_price"], plan["low_price"], book)
    if strip is None:
        return None
    names = {"fav_bid": "our bid", "mid": "mid", "long_bid": "longshot bid (mirror)"}
    return BandStrip(strip.lo, strip.hi, sorted(
        (k if k.label else replace(k, label=f"{names[k.kind]} {cents(k.price)}")
         for k in strip.marks), key=lambda k: k.price))


def review_stats(metrics):
    def pct(value):
        return "--" if value is None else f"{value * 100:,.0f}%"

    ahead = metrics.get("depth_ahead_ratio")
    return [
        ("Est share", pct(metrics.get("share"))),
        ("Est $/day", usd(metrics.get("est_daily_usd"))),
        ("Capital", usd(metrics.get("capital_usd"))),
        ("APR", pct(metrics.get("apr"))),
        ("Depth ahead", "--" if ahead is None else f"{ahead:.1f}x"),
        ("Holding rewards", {True: "yes", False: "no"}.get(metrics.get("holding_rewards"),
                                                           "unknown")),
        ("Vol 24h", human_volume(metrics.get("volume24hr"))),
    ]


@dataclass(frozen=True)
class EvCheck:
    tone: str
    lines: list


def _float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def ev_check(metrics, p_true_text, budget_text, ev_margin):
    """What the farmer will do with this p_true and budget, by its own rule: the
    favorite bid never prices above floor_to_tick(p_true - ev_margin)."""
    plan, m = metrics.get("plan"), metrics.get("market", {})
    p_true = _float(p_true_text)
    if p_true is None or not 0 < p_true < 1 or not plan:
        return EvCheck("c-warn", ["Enter a probability strictly between 0 and 1."])
    tick = m.get("tick") or 0.001
    bid, cap = plan["fav_bid_price"], floor_to_tick(p_true - ev_margin, tick)
    margin = f"p_true minus {cents(ev_margin)}"
    if bid <= cap + 1e-9:
        tone, lines = "c-good", [f"Planned bid {cents(bid)} is at or under your cap of "
                                 f"{cents(cap)} ({margin}). A fill is +EV by your estimate."]
    else:
        capped = share_score(m.get("v") or 0.0, plan["mid"] - cap) if cap >= tick else 0.0
        then = (f"The farmer bids {cents(cap)} instead, which scores {capped:.3f} per share "
                f"instead of {plan['fav_bid_score']:.3f}." if capped > 0 else
                f"{cents(cap)} is outside the reward band, so the farmer places no favorite "
                f"bid and the market earns nothing.")
        tone, lines = "c-warn", [f"Planned bid {cents(bid)} is above your cap of "
                                 f"{cents(cap)} ({margin}). {then}"]
    budget = _float(budget_text)
    long_cost = plan["low_price"] * plan["low_size"]
    if plan.get("mode") == "budget" and budget is not None and budget < long_cost:
        tone = "c-warn"
        lines.append(f"BUDGET market: the planned longshot bid ties up {usd(long_cost)}, "
                     f"above your {usd(budget)} naked budget. The farmer shrinks it to the "
                     f"budget and trims the favorite bid to match.")
    return EvCheck(tone, lines)
