#!/usr/bin/env python3
"""
Polymarket Liquidity-Reward Farmer

Farms Polymarket liquidity rewards on the markets a human approved with
`reward_screener.py approve`. It runs as a daemon: every few seconds it reads
the favorite's order book, works out the orders it wants resting, compares
them with the orders it has, and cancels or places the difference. By default
it is a dry run that prints what it would do; `--live` signs and posts.

Above a 0.90 midpoint Polymarket only pays for two-sided liquidity. A maker's
score is min(Q_one, Q_two), where Q_one sums favorite bids and longshot asks
and Q_two sums favorite asks and longshot bids, each order weighted by
S(v, s) = ((v - s) / v)^2 at distance s from the midpoint inside the max
spread v. Score is sampled once a minute and the pool is paid daily. So the
farmer rests on both sides, in four slots per market:

  FAV_BID   favorite BUY, Q_one, uses cash. Fills are wanted: below our own
            probability p_true a fill is +EV, and the bid never prices above
            p_true minus a margin.
  LONG_ASK  longshot SELL, Q_one, covered by longshot shares. Sells off
            longshot inventory from an unwanted fill, at the mirror of the
            favorite bid so it scores the same.
  FAV_ASK   favorite SELL, Q_two, covered by favorite shares. Exits favorite
            inventory above its average cost and frees the capital again.
  LONG_BID  longshot BUY, Q_two, naked. It exists only to make Q_one count.
            On a FLOOR market it sits at 0.1c-0.2c and almost never fills; on
            a BUDGET market it must sit higher to score, and the market's
            naked_budget_usd caps its cost plus any longshot shares held.

Q_two comes from the covered FAV_ASK first and only the remainder from the
naked LONG_BID. When the budget leaves Q_two short, the favorite bid shrinks
to match: score on one side beyond the other earns nothing and only ties up
capital.

The farmer never buys at market. Every order is post-only, so any fill is a
maker fill that captures spread, and every order is GTD, expiring at the
market's end date minus the expiry buffer (7 days), when the market retires.

Each market has a lifecycle, advanced every cycle by `step_state`:

  QUOTING      orders rest as planned.
  COOLDOWN     the favorite midpoint fell fast; quotes come down for a while,
               because a falling favorite is how a bid gets filled right
               before the news arrives.
  BELOW_RANGE  the midpoint fell under --min-fav, where the strategy does not
               apply; it resumes a little above the threshold.
  PAUSED       a human paused the market (markets.paused). It leaves the
               moment the flag clears and goes through the checks above.
  RETIRED      rewards removed, market closed or not accepting orders, no
               longer approved, or the expiry buffer reached. Terminal, and
               checked before the pause.

Every state but QUOTING wants no orders, so the same reconcile step that
maintains quotes also cancels them. Separately, a longshot fill (longshot
shares went up) pauses the naked LONG_BID for --naked-cooldown seconds, and
with it the favorite bid it was balancing; markets.naked_paused does the same
for as long as a human leaves it set.

farm.db is the control surface (see farm_db.py). Every cycle the farmer
re-reads the approved markets with their pause flags and capital, the
settings that override its command-line config, and any stop command, so a
change in the console takes effect within one interval. It publishes what it
is doing to farm_state, farmer_status, farm_events, and (one sample per market
per minute) farm_mids, in dry run and live alike; only live mode writes to
markets (retirement, baselines).

The farmer never sells shares that were in the wallet before it managed the
market. The first time it sees an approved market it records the holdings as
a baseline (in the database when live), lowers the baseline whenever the
holdings drop below it, and farms only the shares above it. One
approximation remains: the data API reports one average price per token,
blended across baseline and farm shares, and the farmer uses it as the farm
inventory's cost for the exit floor and the capital and budget caps.

A place the CLOB rejects (a post-only cross, an unfunded bid) backs off that
market's slot for 30 seconds, doubling on each further rejection up to 15
minutes, and resets on the next accepted place.

Reconciling keeps an existing order when it is still close enough to target
(same price grid, most of the target score, size within -20%/+25%) instead of
replacing it on every wiggle of the midpoint. Replacing an order loses its
queue position and costs two API calls, so this hysteresis matters.

Planning, reconciling, and the lifecycle are pure functions; the daemon around
them only fetches, executes, and logs.

Usage:
  python reward_farmer.py                      # dry run on farm.db, forever
  python reward_farmer.py --db scratch.db --once
  python reward_farmer.py --live               # sign and post real orders
"""

import argparse
import json
import math
import os
import signal
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import NamedTuple, Optional

import httpx
from dotenv import load_dotenv

import farm_db
from farm_db import EventKind, stamp
from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OpenOrderParams,
    OrderArgsV2,
    OrderMarketCancelParams,
    OrderType,
    PartialCreateOrderOptions,
    PostOrdersV2Args,
)

from reward_screener import (
    CLOB_HOST,
    Skip,
    _chunks,
    _float,
    _make_output_safe,
    adjust_levels,
    adjusted_mid,
    fav_bid_price,
    fetch_levels,
    fetch_total_rates,
    floor_to_tick,
    lowest_scoring_bid,
    parse_time,
    round_size_down,
    round_size_up,
    score,
    tick_decimals,
)

DATA_API_HOST = os.getenv("POLYMARKET_DATA_API_HOST", "https://data-api.polymarket.com")

BUY, SELL = "BUY", "SELL"

# A BELOW_RANGE market resumes this far above --min-fav, so a midpoint
# hovering at the threshold does not flap the quotes on and off.
RESUME_MARGIN = 0.005

# An order whose size left this band around the target gets replaced.
KEEP_SIZE_RANGE = (0.8, 1.25)

POSITIONS_S = 60
MARKET_INFO_S = 300
MARKET_INFO_RETRY_S = 60
TOTAL_RATES_S = 300
REWARDS_S = 600
MID_SAMPLE_S = 60
HEARTBEAT_S = 5

# A rejected place waits this long before the slot tries again, doubling on
# each further rejection up to the cap.
BACKOFF_START_S = 30
BACKOFF_MAX_S = 900

# Most orders the CLOB accepts in one batch POST.
POST_BATCH_LIMIT = 15
POSITIONS_PAGE = 500


def log(message):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def cents(price, tick):
    return f"{price * 100:.{max(1, tick_decimals(tick) - 2)}f}c"


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

class Role(Enum):
    FAV = "favorite"
    LONG = "longshot"


class QSide(Enum):
    ONE = "Q_one"
    TWO = "Q_two"


class Slot(Enum):
    """The four orders a market can rest: (token role, side, score side)."""

    FAV_BID = (Role.FAV, BUY, QSide.ONE)
    LONG_ASK = (Role.LONG, SELL, QSide.ONE)
    FAV_ASK = (Role.FAV, SELL, QSide.TWO)
    LONG_BID = (Role.LONG, BUY, QSide.TWO)

    @property
    def role(self):
        return self.value[0]

    @property
    def side(self):
        return self.value[1]

    @property
    def q_side(self):
        return self.value[2]


SLOT_BY_ROLE_SIDE = {(slot.role, slot.side): slot for slot in Slot}


class FarmMarket(NamedTuple):
    condition_id: str
    slug: str
    question: str
    end_date: datetime
    neg_risk: bool
    tick: float
    fav_token_id: str
    fav_outcome: str
    rate_per_day: float
    v: float
    min_size: float
    long_token_id: str
    p_true: float
    naked_budget_usd: float
    expires_at: datetime
    capital: float
    paused: bool = False
    naked_paused: bool = False

    def token(self, role):
        return self.fav_token_id if role is Role.FAV else self.long_token_id

    def role_of(self, token_id):
        return {self.fav_token_id: Role.FAV, self.long_token_id: Role.LONG}.get(token_id)

    @property
    def label(self):
        return self.slug or self.question[:50]


class Position(NamedTuple):
    fav_shares: float = 0.0
    fav_avg: float = 0.0
    long_shares: float = 0.0
    long_avg: float = 0.0


class Intent(NamedTuple):
    slot: Slot
    token_id: str
    side: str
    price: float
    size: float
    score: float


class LiveOrder(NamedTuple):
    id: str
    slot: Slot
    token_id: str
    side: str
    price: float
    size_remaining: float


class Target(NamedTuple):
    """The orders wanted, and the midpoint they were scored against.

    mid is the size-adjusted midpoint after our own quotes join the book, or
    None when the book is one-sided and nothing can be planned.
    """

    mid: Optional[float]
    intents: list


class Cancel(NamedTuple):
    order_ids: tuple


class Place(NamedTuple):
    intent: Intent


class Baseline(NamedTuple):
    """Shares held before the farmer managed the market; it never sells them."""

    fav: float
    long: float


class Backoff(NamedTuple):
    until: datetime
    delay_s: float
    attempts: int = 1


class Phase(Enum):
    QUOTING = "QUOTING"
    COOLDOWN = "COOLDOWN"
    BELOW_RANGE = "BELOW_RANGE"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"


class State(NamedTuple):
    phase: Phase
    until: Optional[datetime] = None
    reason: str = ""

    def __str__(self):
        detail = self.reason
        if self.until is not None:
            detail += f" until {self.until.astimezone():%H:%M:%S}"
        return f"{self.phase.value} ({detail.strip()})" if detail else self.phase.value


class Observation(NamedTuple):
    """What step_state and naked_allowed see of a market in one cycle.

    paused and naked_paused are the human's flags on the market row;
    naked_cooldown_until is set by the farmer after a longshot fill.
    """

    mid: Optional[float]
    recent_mids: tuple
    rewards_active: bool
    accepting_orders: bool
    closed: bool
    approved: bool
    expires_at: datetime
    paused: bool = False
    naked_paused: bool = False
    naked_cooldown_until: Optional[datetime] = None


class MarketInfo(NamedTuple):
    """The CLOB's market record: tokens, reward terms, and trading status."""

    token_ids: tuple
    native_rate: float
    v: float
    min_size: float
    tick: float
    neg_risk: bool
    accepting_orders: bool
    closed: bool


class FarmConfig(NamedTuple):
    capital: float
    bid_offset: float
    ask_offset: float
    min_capture: float
    ev_margin: float
    min_fav: float
    min_low_score: float
    keep_ratio: float
    drop_threshold: float
    drop_window_s: float
    cooldown_s: float
    naked_cooldown_s: float
    expiry_buffer_days: float


# ---------------------------------------------------------------------------
# Planning (pure)
# ---------------------------------------------------------------------------

def ceil_to_tick(price, tick):
    return round(math.ceil(round(price / tick, 6)) * tick, tick_decimals(tick))


def on_tick(price, tick):
    return abs(price / tick - round(price / tick)) < 1e-6


def order_score(slot, price, mid, v):
    """Per-share score of an order in `slot` at `price`, given the favorite mid."""
    token_mid = mid if slot.role is Role.FAV else 1 - mid
    distance = token_mid - price if slot.side == BUY else price - token_mid
    return score(v, distance)


def ev_cap(fm, cfg):
    """Highest favorite bid that is still +EV by the margin we demand."""
    return floor_to_tick(fm.p_true - cfg.ev_margin, fm.tick)


def exit_floor(position, fm, cfg):
    """Lowest price a FAV_ASK may sell at: average cost plus the capture."""
    return ceil_to_tick(position.fav_avg + cfg.min_capture, fm.tick)


def naked_room(fm, position):
    """USD of longshot bid the budget still allows after longshot shares held."""
    return max(0.0, fm.naked_budget_usd - position.long_shares * position.long_avg)


def mirror(price, tick):
    """The longshot price equivalent to a favorite price, and vice versa."""
    return round(1 - price, tick_decimals(tick))


def plan_fav_bid(fm, levels, book_mid, position, cfg):
    """(price, size) of the favorite bid before balancing, or None."""
    price = fav_bid_price(book_mid, levels, fm.tick, cfg.bid_offset, fm.min_size)
    if isinstance(price, Skip):
        return None
    price = min(price, ev_cap(fm, cfg))
    if price < fm.tick:
        return None
    room = fm.capital - position.fav_shares * position.fav_avg
    return price, round_size_down(max(0.0, room) / price)


def plan_long_ask_price(fm, levels, book_mid, fav_bid, cfg):
    """Longshot ask at the mirror of the favorite bid, or of where it would sit."""
    if fav_bid is not None:
        return mirror(fav_bid, fm.tick)
    fav_equivalent = floor_to_tick(book_mid - max(cfg.bid_offset, fm.tick), fm.tick)
    if levels.asks:
        fav_equivalent = min(fav_equivalent, floor_to_tick(levels.asks[0][0] - fm.tick, fm.tick))
    return mirror(fav_equivalent, fm.tick)


def plan_fav_ask_price(fm, levels, book_mid, position, cfg):
    """Favorite ask above both the midpoint and our cost, or None if it would cross."""
    tick = fm.tick
    price = max(
        ceil_to_tick(book_mid + max(cfg.ask_offset, tick), tick),
        exit_floor(position, fm, cfg),
    )
    best_bid = levels.bids[0][0] if levels.bids else None
    if best_bid is not None:
        price = max(price, ceil_to_tick(best_bid + tick, tick))
    price = min(price, round(1 - tick, tick_decimals(tick)))
    if best_bid is not None and price <= best_bid + 1e-9:
        return None
    return price


def target_orders(fm, levels, position, cfg, naked_allowed):
    """The orders this market should rest, scored against the mid they create.

    `levels` is the favorite book without our own resting orders. Sizes are
    balanced so Q_two covers Q_one: covered FAV_ASK first, then the naked
    LONG_BID up to the budget, then the favorite bid shrinks to whatever
    Q_two reached. A bid that ends below the reward minimum size is dropped,
    because an order that does not score is risk without reward; the two
    asks are exits and stay regardless.
    """
    book_mid = adjusted_mid(levels.bids, levels.asks, fm.min_size)
    if book_mid is None:
        return Target(None, [])
    tick, v = fm.tick, fm.v

    fav_bid = plan_fav_bid(fm, levels, book_mid, position, cfg)
    long_ask = fav_ask = None
    if position.long_shares >= fm.min_size:
        long_ask = plan_long_ask_price(
            fm, levels, book_mid, fav_bid and fav_bid[0], cfg
        )
    if position.fav_shares >= fm.min_size:
        fav_ask = plan_fav_ask_price(fm, levels, book_mid, position, cfg)

    our_bids = [(fav_bid[0], fav_bid[1])] if fav_bid and fav_bid[1] > 0 else []
    if long_ask is not None:
        our_bids.append((mirror(long_ask, tick), position.long_shares))
    our_asks = [(fav_ask, position.fav_shares)] if fav_ask is not None else []
    quoted = adjust_levels(levels, bids=our_bids, asks=our_asks)
    mid = adjusted_mid(quoted.bids, quoted.asks, fm.min_size)

    intents = {}

    def add(slot, price, size, slot_score):
        intents[slot] = Intent(
            slot, fm.token(slot.role), slot.side, price, round_size_down(size), slot_score
        )

    if long_ask is not None:
        add(Slot.LONG_ASK, long_ask, position.long_shares,
            order_score(Slot.LONG_ASK, long_ask, mid, v))
    if fav_ask is not None:
        add(Slot.FAV_ASK, fav_ask, position.fav_shares,
            order_score(Slot.FAV_ASK, fav_ask, mid, v))

    fav_score = order_score(Slot.FAV_BID, fav_bid[0], mid, v) if fav_bid else 0.0
    fav_size = fav_bid[1] if fav_score > 0 else 0.0

    def q(slot):
        intent = intents.get(slot)
        return intent.score * intent.size if intent else 0.0

    q_one_exits, q_two_covered = q(Slot.LONG_ASK), q(Slot.FAV_ASK)

    low = lowest_scoring_bid(levels, mid, v, tick, cfg.min_low_score)

    def long_bid_size(q_one):
        if not naked_allowed or low is None or q_one <= q_two_covered:
            return 0.0
        price, low_score = low
        size = min(
            round_size_up((q_one - q_two_covered) / low_score),
            round_size_down(naked_room(fm, position) / price),
        )
        return size if size >= fm.min_size else 0.0

    long_size = long_bid_size(q_one_exits + fav_score * fav_size)
    q_two = q_two_covered + (low[1] * long_size if long_size else 0.0)
    if fav_score > 0 and q_one_exits + fav_score * fav_size > q_two:
        fav_size = round_size_down(max(0.0, q_two - q_one_exits) / fav_score)
    if fav_size < fm.min_size:
        fav_size = 0.0
        long_size = min(long_size, long_bid_size(q_one_exits))

    if fav_size > 0:
        add(Slot.FAV_BID, fav_bid[0], fav_size, fav_score)
    if long_size > 0:
        add(Slot.LONG_BID, low[0], long_size, low[1])
    return Target(mid, [intents[slot] for slot in Slot if slot in intents])


def keep_failure(order, intent, mid, fm, position, cfg):
    """Why a resting order is not close enough to its target to leave alone, or None."""
    low, high = KEEP_SIZE_RANGE
    if (order.token_id, order.side) != (intent.token_id, intent.side):
        return "wrong token or side"
    if not on_tick(order.price, fm.tick):
        return "off the tick grid"
    if order_score(order.slot, order.price, mid, fm.v) < cfg.keep_ratio * intent.score - 1e-12:
        return "score below keep ratio"
    if not low * intent.size - 1e-9 <= order.size_remaining <= high * intent.size + 1e-9:
        return "size out of range"
    if order.slot is Slot.FAV_BID and order.price > ev_cap(fm, cfg) + 1e-9:
        return "above the EV cap"
    if order.slot is Slot.FAV_ASK and order.price < exit_floor(position, fm, cfg) - 1e-9:
        return "below the exit floor"
    if order.slot is Slot.LONG_BID and order.price * order.size_remaining > naked_room(fm, position) + 1e-9:
        return "over the naked budget"
    return None


def reconcile(intents, live_orders, mid, fm, position, cfg):
    """Cancels and places that turn the resting orders into the targets.

    Per slot, one resting order with no keep_failure survives; every other
    order in the slot is cancelled, and the target is placed if nothing
    survived.
    All cancels come first, so freed balance is available to the places.
    """
    targets = {intent.slot: intent for intent in intents}
    cancels, places = [], []
    for slot in Slot:
        orders = [o for o in live_orders if o.slot is slot]
        intent = targets.get(slot)
        kept = None
        if intent is not None:
            kept = next(
                (o for o in orders
                 if keep_failure(o, intent, mid, fm, position, cfg) is None),
                None,
            )
            if kept is None:
                places.append(Place(intent))
        cancels.extend(o.id for o in orders if o is not kept)
    return ([Cancel(tuple(cancels))] if cancels else []) + places


# ---------------------------------------------------------------------------
# Lifecycle (pure)
# ---------------------------------------------------------------------------

RETIRE_CHECKS = (
    ("rewards removed", lambda obs, now: not obs.rewards_active),
    ("market closed", lambda obs, now: obs.closed),
    ("not accepting orders", lambda obs, now: not obs.accepting_orders),
    ("no longer approved", lambda obs, now: not obs.approved),
    ("expiry buffer reached", lambda obs, now: now >= obs.expires_at),
)


def mid_dropped(recent_mids, mid, now, cfg):
    """Whether the mid fell by drop_threshold from any point in the window."""
    window = [m for t, m in recent_mids if (now - t).total_seconds() <= cfg.drop_window_s]
    return bool(window) and max(window) - mid >= cfg.drop_threshold - 1e-9


def step_state(state, obs, now, cfg):
    if state.phase is Phase.RETIRED:
        return state
    for reason, applies in RETIRE_CHECKS:
        if applies(obs, now):
            return State(Phase.RETIRED, reason=reason)
    if obs.paused:
        return State(Phase.PAUSED, reason="paused by operator")
    if state.phase is Phase.PAUSED:
        state = State(Phase.QUOTING)
    if obs.mid is None:
        return state
    if mid_dropped(obs.recent_mids, obs.mid, now, cfg):
        return State(Phase.COOLDOWN, now + timedelta(seconds=cfg.cooldown_s), "mid drop")
    if state.phase is Phase.COOLDOWN and now < state.until:
        return state
    if obs.mid < cfg.min_fav:
        return State(Phase.BELOW_RANGE)
    if state.phase is Phase.BELOW_RANGE and obs.mid < cfg.min_fav + RESUME_MARGIN:
        return state
    return State(Phase.QUOTING)


def naked_allowed(obs, now):
    """Whether the naked LONG_BID may rest: not paused by a human or a recent fill."""
    if obs.naked_paused:
        return False
    return obs.naked_cooldown_until is None or now >= obs.naked_cooldown_until


def fill_events(previous, current):
    """[(slot that filled, shares)] implied by a change in position."""
    events = []
    for slot, before, after in (
        (Slot.FAV_BID, previous.fav_shares, current.fav_shares),
        (Slot.LONG_BID, previous.long_shares, current.long_shares),
    ):
        delta = round(after - before, 2)
        if delta > 0:
            events.append((slot, delta))
        elif delta < 0:
            sell = Slot.FAV_ASK if slot is Slot.FAV_BID else Slot.LONG_ASK
            events.append((sell, -delta))
    return events


def ratchet_baseline(baseline, held):
    """Record the holdings on first sight, then only ever lower them.

    A manual sale below the baseline lowers it, so those shares do not stay
    reserved after they are gone.
    """
    if baseline is None:
        return Baseline(held.fav_shares, held.long_shares)
    return Baseline(min(baseline.fav, held.fav_shares), min(baseline.long, held.long_shares))


def farm_position(held, baseline):
    """The farm's own inventory: holdings above the baseline.

    The averages stay the wallet's, which blend baseline and farm shares;
    the data API has no per-lot cost.
    """
    return held._replace(
        fav_shares=round(max(0.0, held.fav_shares - baseline.fav), 2),
        long_shares=round(max(0.0, held.long_shares - baseline.long), 2),
    )


def next_backoff(previous, now):
    if previous is None:
        return Backoff(now + timedelta(seconds=BACKOFF_START_S), BACKOFF_START_S)
    delay = min(previous.delay_s * 2, BACKOFF_MAX_S)
    return Backoff(now + timedelta(seconds=delay), delay, previous.attempts + 1)


def without_backed_off(actions, backoffs, now):
    """Drop places into slots still waiting out a rejection."""
    def waiting(action):
        backoff = isinstance(action, Place) and backoffs.get(action.intent.slot)
        return bool(backoff) and now < backoff.until

    return [a for a in actions if not waiting(a)]


def in_fav_book(order, fm):
    """(is a favorite bid, favorite price) where an order shows in the favorite book.

    A longshot order shows at the mirrored price on the opposite side, so a
    LONG_ASK is a favorite bid and a LONG_BID a favorite ask.
    """
    price = order.price if order.slot.role is Role.FAV else mirror(order.price, fm.tick)
    return order.slot.q_side is QSide.ONE, price


def strip_own(levels, orders, fm):
    """The favorite book without our resting orders."""
    bids, asks = [], []
    for o in orders:
        is_bid, price = in_fav_book(o, fm)
        (bids if is_bid else asks).append((price, -o.size_remaining))
    return adjust_levels(levels, bids=bids, asks=asks)


# ---------------------------------------------------------------------------
# Observability (pure): what farm_state shows of a market
# ---------------------------------------------------------------------------

def q_totals(intents):
    q = {side: 0.0 for side in QSide}
    for intent in intents:
        q[intent.slot.q_side] += intent.score * intent.size
    return q[QSide.ONE], q[QSide.TWO]


def slot_views(state, orders, target, actions, backoffs, fm, position, cfg):
    """Per slot: the resting order, the target, and what this cycle decided."""
    intents = {i.slot: i for i in target.intents}
    cancelled = {i for a in actions if isinstance(a, Cancel) for i in a.order_ids}
    placing = {a.intent.slot for a in actions if isinstance(a, Place)}
    views = {}
    for slot in Slot:
        resting = [o for o in orders if o.slot is slot]
        kept = next((o for o in resting if o.id not in cancelled), None)
        shown = kept or (resting[0] if resting else None)
        intent = intents.get(slot)
        if intent is None:
            why = "no target" if state.phase is Phase.QUOTING else str(state)
            decision, reason = ("cancel" if resting else "none"), why
        elif kept is not None:
            decision, reason = "hold", ""
        elif slot in placing and resting:
            decision = "replace"
            reason = keep_failure(resting[0], intent, target.mid, fm, position, cfg)
        elif slot in placing:
            decision, reason = "place", ""
        else:
            decision = "backoff"
            reason = f"rejected; retry after {stamp(backoffs[slot].until)}"
        views[slot.name] = {
            "order": shown and {"id": shown.id, "price": shown.price,
                                "size": shown.size_remaining},
            "target": intent and {"price": intent.price, "size": intent.size,
                                  "score": intent.score},
            "live_score": (order_score(slot, shown.price, target.mid, fm.v)
                           if shown and target.mid is not None else None),
            "decision": decision,
            "reason": reason,
        }
    return views


def book_view(levels, orders, mid, fm):
    """Favorite-book levels inside the reward band, with our resting size at each.

    `size` is the CLOB's level size, which includes our orders in live mode
    and never includes the simulated ones of a dry run.
    """
    if mid is None:
        return {"bids": [], "asks": []}
    ours = {}
    for o in orders:
        key = in_fav_book(o, fm)
        ours[key] = ours.get(key, 0.0) + o.size_remaining

    def side(is_bid, entries):
        sizes = dict(entries)
        prices = set(sizes) | {p for b, p in ours if b == is_bid}
        inside = [p for p in prices if abs(p - mid) < fm.v]
        return [{"price": p, "size": sizes.get(p, 0.0), "ours": ours.get((is_bid, p), 0.0)}
                for p in sorted(inside, reverse=is_bid)]

    return {"bids": side(True, levels.bids), "asks": side(False, levels.asks)}


def position_view(held, baseline, farm):
    return {
        "wallet": {"fav": held.fav_shares, "long": held.long_shares},
        "baseline": {"fav": baseline.fav, "long": baseline.long},
        "farm": {"fav": farm.fav_shares, "long": farm.long_shares},
        "avg_cost": {"fav": held.fav_avg, "long": held.long_avg},
    }


def resting_price(view):
    """The price a slot rests at once this cycle's actions land, or None."""
    if view["decision"] == "hold":
        return view["order"]["price"]
    if view["decision"] in ("place", "replace"):
        return view["target"]["price"]
    return None


def mid_sample(state):
    """The farm_mids row of a market's farm_state snapshot."""
    return farm_db.MidSample(
        state.condition_id, state.updated_at, state.mid, state.book_mid,
        resting_price(state.slots[Slot.FAV_BID.name]),
        resting_price(state.slots[Slot.LONG_BID.name]),
    )


def naked_used(position, orders):
    """USD of the naked leg: longshot shares held at cost plus resting longshot bids."""
    resting = sum(o.price * o.size_remaining for o in orders if o.slot is Slot.LONG_BID)
    return position.long_shares * position.long_avg + resting


# ---------------------------------------------------------------------------
# External data -> domain types
# ---------------------------------------------------------------------------

def parse_market_info(entry):
    rewards = entry.get("rewards") or {}
    return MarketInfo(
        token_ids=tuple(
            str(t["token_id"]) for t in entry.get("tokens") or [] if t.get("token_id")
        ),
        native_rate=sum(
            _float(r.get("rewards_daily_rate")) or 0 for r in rewards.get("rates") or []
        ),
        v=(_float(rewards.get("max_spread")) or 0) / 100,
        min_size=_float(rewards.get("min_size")) or 0,
        tick=_float(entry.get("minimum_tick_size")) or 0.01,
        neg_risk=bool(entry.get("neg_risk")),
        accepting_orders=bool(entry.get("accepting_orders")),
        closed=bool(entry.get("closed")),
    )


def rewards_active(info, condition_id, total_rates):
    """Native rates show on the market; sponsored-only pools only in the total.

    total_rates is None until the first fetch of the slow total listing
    finishes, and a market is not retired on a rate nobody has read yet.
    """
    if info.v <= 0:
        return False
    if info.native_rate > 0 or total_rates is None:
        return True
    return total_rates.get(condition_id, 0) > 0


def farm_market(row, info, cfg):
    """An approved DB row plus the CLOB record as a FarmMarket, or Skip."""
    market = json.loads(row["metrics"])["market"]
    fav = market["fav_token_id"]
    others = [t for t in info.token_ids if t != fav]
    if fav not in info.token_ids or len(others) != 1:
        return Skip("favorite token not in the market's two tokens")
    if row["p_true"] is None:
        return Skip("no p_true")
    end_date = parse_time(row["end_date"]) or parse_time(market["end_date"])
    return FarmMarket(
        condition_id=row["condition_id"],
        slug=row["slug"] or market["slug"],
        question=row["question"] or market["question"],
        end_date=end_date,
        neg_risk=info.neg_risk,
        tick=info.tick,
        fav_token_id=fav,
        fav_outcome=market["fav_outcome"],
        rate_per_day=market["rate_per_day"],
        v=info.v,
        min_size=info.min_size,
        long_token_id=others[0],
        p_true=row["p_true"],
        naked_budget_usd=row["naked_budget_usd"] or 0.0,
        expires_at=end_date - timedelta(days=cfg.expiry_buffer_days),
        capital=row["capital_usd"] if row["capital_usd"] is not None else cfg.capital,
        paused=bool(row["paused"]),
        naked_paused=bool(row["naked_paused"]),
    )


def position_for(fm, holdings):
    fav = holdings.get(fm.fav_token_id, (0.0, 0.0))
    long = holdings.get(fm.long_token_id, (0.0, 0.0))
    return Position(fav[0], fav[1], long[0], long[1])


def parse_open_order(entry, fm):
    """A CLOB open order on a managed market as a LiveOrder, or None."""
    token_id = str(entry.get("asset_id") or "")
    side = str(entry.get("side") or "").upper()
    role = fm.role_of(token_id)
    if role is None or side not in (BUY, SELL):
        return None
    price = _float(entry.get("price"))
    remaining = (_float(entry.get("original_size")) or 0) - (_float(entry.get("size_matched")) or 0)
    if price is None or not entry.get("id"):
        return None
    return LiveOrder(
        id=entry["id"],
        slot=SLOT_BY_ROLE_SIDE[(role, side)],
        token_id=token_id,
        side=side,
        price=price,
        size_remaining=round(remaining, 2),
    )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_market_info(http, condition_id, timeout=20):
    resp = http.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=timeout)
    resp.raise_for_status()
    return parse_market_info(resp.json())


def fetch_holdings(http, user, timeout=30):
    """{token_id: (shares, avg_price)} for every position the user holds."""
    holdings, offset = {}, 0
    while True:
        resp = http.get(
            f"{DATA_API_HOST}/positions",
            params={"user": user, "limit": POSITIONS_PAGE, "offset": offset,
                    "sizeThreshold": 0},
            timeout=timeout,
        )
        resp.raise_for_status()
        page = resp.json() or []
        for p in page:
            size, avg = _float(p.get("size")), _float(p.get("avgPrice"))
            if p.get("asset") and size is not None:
                holdings[str(p["asset"])] = (size, avg or 0.0)
        if len(page) < POSITIONS_PAGE:
            return holdings
        offset += POSITIONS_PAGE


def parse_reward_percentages(resp):
    """{condition_id: percent of the pool we earn} from /rewards/user/percentages.

    The endpoint is undocumented in the SDK; it is read as an object mapping
    condition ids to numbers, and anything else is a shape mismatch.
    """
    if not isinstance(resp, dict):
        raise ValueError(f"expected an object, got {type(resp).__name__}")
    parsed = {str(k): _float(v) for k, v in resp.items()}
    return {k: v for k, v in parsed.items() if v is not None}


def parse_earnings(entries):
    """({condition_id: USD earned}, entries skipped) from /rewards/user.

    Each entry is read as {condition_id, earnings, asset_rate}, earnings in
    the reward asset and asset_rate its USD price (1 when absent).
    """
    if not isinstance(entries, list):
        raise ValueError(f"expected a list, got {type(entries).__name__}")
    totals, skipped = {}, 0
    for e in entries:
        earned = _float(e.get("earnings")) if isinstance(e, dict) else None
        if earned is None or not e.get("condition_id"):
            skipped += 1
            continue
        rate = _float(e.get("asset_rate"))
        cid = str(e["condition_id"])
        totals[cid] = totals.get(cid, 0.0) + earned * (1.0 if rate is None else rate)
    return totals, skipped


def row_baseline(row):
    if row["baseline_fav"] is None or row["baseline_long"] is None:
        return None
    return Baseline(row["baseline_fav"], row["baseline_long"])


class TotalRates:
    """The total daily rate of every reward market, sponsored pools included.

    The listing that has them pages slowly (most of a minute), so a thread
    refreshes it while the main loop keeps quoting. `rates` is None until the
    first fetch lands and is replaced whole, never mutated.
    """

    def __init__(self, stop):
        self.rates = None
        self._stop = stop

    def run(self):
        while not self._stop.is_set():
            try:
                with httpx.Client() as http:
                    rates = fetch_total_rates(http)
                if rates:
                    self.rates = rates
            except Exception as e:
                log(f"Warning: total reward rates fetch failed ({e})")
            self._stop.wait(TOTAL_RATES_S)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class Managed:
    """One approved market's runtime: its plan inputs, lifecycle, and last view."""

    def __init__(self, fm, info, baseline):
        self.fm = fm
        self.info = info
        self.baseline = baseline
        self.backoffs = {}
        self.approved = True
        self.state = State(Phase.QUOTING)
        self.recent_mids = deque()
        self.naked_paused_until = None
        self.held = None
        self.position = None
        self.orders = []
        self.sim_orders = []
        self.snapshot = None
        self.mid_sampled_at = None


class Farmer:
    def __init__(self, args, cfg, client=None):
        self.args = args
        self.base_cfg = self.cfg = cfg
        self.client = client
        self.live = client is not None
        self.http = httpx.Client()
        self.db = farm_db.open_db(args.db)
        self.markets = {}
        self.infos = {}
        self.skips = {}
        self.setting_errors = {}
        self.events = []
        self.mids = []
        self.scoring = {}
        self.positions_at = self.rewards_at = 0.0
        self.started_at = self.last_cycle_at = datetime.now(timezone.utc)
        self.sim_ids = 0
        self.stop = threading.Event()
        self.stop_reason = None
        self.total_rates = TotalRates(self.stop)
        self.user = os.getenv("POLYMARKET_FUNDER_ADDRESS")

    def active(self):
        return [m for m in self.markets.values() if m.state.phase is not Phase.RETIRED]

    def emit(self, kind, line, condition_id=None, slot=None, price=None, size=None,
             score=None, data=None):
        """Log a change and queue it for farm_events."""
        log(line)
        self.events.append(farm_db.Event(
            datetime.now(timezone.utc), kind, line.strip(), condition_id,
            slot.name if slot else None, price, size, score, data,
        ))

    def flush(self, stopped_at=None):
        """Publish the queued events and the status; farm_state too unless stopping."""
        states = None
        if stopped_at is None:
            states = [m.snapshot for m in self.markets.values() if m.snapshot]
        status = farm_db.FarmerStatus(
            mode="live" if self.live else "dry",
            pid=os.getpid(),
            started_at=self.started_at,
            last_cycle_at=self.last_cycle_at,
            interval_s=self.args.interval,
            heartbeat=self.live and self.args.heartbeat,
            managed=len(self.active()),
            config=self.cfg._asdict(),
            stopped_at=stopped_at,
        )
        farm_db.publish(self.db, status, states, self.events, self.mids)
        self.events, self.mids = [], []

    def start(self):
        now = datetime.now(timezone.utc)
        pruned = farm_db.prune_events(self.db, now)
        mids_pruned = farm_db.prune_mids(self.db, now)
        expired = farm_db.expire_commands(self.db, now)
        mode = "LIVE" if self.live else "DRY RUN (no orders are sent)"
        self.emit(EventKind.START,
                  f"Reward farmer: {mode}; db {self.args.db}, every {self.args.interval:g}s",
                  data={"pid": os.getpid(), "events_pruned": pruned,
                        "mids_pruned": mids_pruned, "commands_expired": expired})
        self.read_controls(now)
        self.refresh_positions(now)
        self.positions_at = time.monotonic() + POSITIONS_S
        if not self.markets:
            log(f"No approved markets in {self.args.db}; waiting for approvals")
        if self.live:
            self.check_collateral()
        self.flush()

    def shutdown(self, reason, cancel):
        """Stop the way Ctrl+C does: live orders come down unless the run just ended."""
        if self.live and cancel:
            self.cancel_managed()
        self.stop.set()
        self.emit(EventKind.STOP, f"Stopped: {reason}", data={"reason": reason})
        try:
            self.flush(stopped_at=datetime.now(timezone.utc))
        except Exception as e:
            log(f"Warning: writing the stop to {self.args.db} failed ({e})")
        self.db.close()
        self.http.close()

    def read_controls(self, now):
        """Commands, settings, and approved markets, re-read from the DB every cycle."""
        for command in farm_db.pending_commands(self.db):
            if command.kind == farm_db.CommandKind.STOP.value:
                self.stop_reason = self.stop_reason or f"stop command {command.id}"
                result = "stopping"
            else:
                result = f"unknown command {command.kind!r}"
            farm_db.consume_command(self.db, command.id, result, now)
            log(f"Command {command.id} {command.kind}: {result}")
        self.apply_settings(*farm_db.load_settings(self.db))
        self.reload(now)

    def apply_settings(self, values, errors):
        for key, error in sorted(errors.items() - self.setting_errors.items()):
            self.emit(EventKind.ALERT, f"Ignoring setting {error}", data={"key": key})
        self.setting_errors = errors
        cfg = self.base_cfg._replace(**values)
        if cfg == self.cfg:
            return
        diff = {f: [old, new] for f, old, new in zip(cfg._fields, self.cfg, cfg) if old != new}
        self.emit(EventKind.SETTINGS, "Settings: " + ", ".join(
            f"{f} {old:g} -> {new:g}" for f, (old, new) in diff.items()
        ), data=diff)
        self.cfg = cfg

    def market_info(self, condition_id, label, now):
        """The CLOB market record, refetched every MARKET_INFO_S; None until one lands."""
        info, due = self.infos.get(condition_id, (None, now))
        if now >= due:
            try:
                info = fetch_market_info(self.http, condition_id)
                due = now + timedelta(seconds=MARKET_INFO_S)
            except Exception as e:
                log(f"Warning: market data for {label} failed ({e})")
                due = now + timedelta(seconds=MARKET_INFO_RETRY_S)
            self.infos[condition_id] = (info, due)
        return info

    def reload(self, now):
        rows = farm_db.load_approved(self.db)
        for cid, managed in self.markets.items():
            managed.approved = cid in rows
        for cid, row in rows.items():
            managed = self.markets.get(cid)
            if managed is not None and managed.state.phase is Phase.RETIRED:
                continue
            label = row["slug"] or cid
            info = self.market_info(cid, label, now)
            if info is None:
                continue
            fm = farm_market(row, info, self.cfg)
            if isinstance(fm, Skip):
                if self.skips.get(cid) != fm.reason:
                    self.skips[cid] = fm.reason
                    self.emit(EventKind.ALERT, f"Skipping {label}: {fm.reason}", cid)
                continue
            self.skips.pop(cid, None)
            if managed is None:
                self.markets[cid] = Managed(fm, info, row_baseline(row))
                self.emit(EventKind.PHASE,
                          f"Managing {fm.label}: {fm.fav_outcome} p_true {fm.p_true:.3f}, "
                          f"budget ${fm.naked_budget_usd:.2f}, capital ${fm.capital:.2f}, "
                          f"v {fm.v * 100:.1f}c, min {fm.min_size:g}, tick {fm.tick:g}, "
                          f"expires {fm.expires_at:%Y-%m-%d}",
                          cid, data={"from": None, "to": Phase.QUOTING.value})
            else:
                managed.fm, managed.info = fm, info

    def refresh_positions(self, now):
        try:
            holdings = fetch_holdings(self.http, self.user)
        except Exception as e:
            log(f"Warning: positions fetch failed ({e}); keeping the last ones")
            return
        for managed in self.active():
            fm, previous = managed.fm, managed.position
            held = managed.held = position_for(fm, holdings)
            baseline = ratchet_baseline(managed.baseline, held)
            if baseline != managed.baseline:
                managed.baseline = baseline
                if self.live:
                    farm_db.save_baseline(self.db, fm.condition_id, baseline.fav, baseline.long)
            current = managed.position = farm_position(held, baseline)
            if previous is None:
                if held != Position() or baseline != Baseline(0.0, 0.0):
                    log(f"{fm.label}: holding fav {held.fav_shares:g} @ "
                        f"{cents(held.fav_avg, fm.tick)}, long {held.long_shares:g} @ "
                        f"{cents(held.long_avg, fm.tick)}; baseline fav {baseline.fav:g}, "
                        f"long {baseline.long:g} (never sold); farming fav "
                        f"{current.fav_shares:g}, long {current.long_shares:g}")
                continue
            for slot, shares in fill_events(previous, current):
                self.record_fill(managed, slot, shares)
                if slot is Slot.LONG_BID:
                    managed.naked_paused_until = now + timedelta(
                        seconds=self.cfg.naked_cooldown_s
                    )
                    self.emit(EventKind.ALERT,
                              f"ALERT {fm.label}: naked longshot fill; LONG_BID paused "
                              f"until {managed.naked_paused_until.astimezone():%H:%M:%S}",
                              fm.condition_id, slot)

    def record_fill(self, managed, slot, shares):
        """A fill inferred from a position change, priced at the slot's resting order.

        The data API gives no fill price, so the price is that of the order
        resting in the slot at the last cycle, if there was one.
        """
        fm = managed.fm
        resting = next((o for o in managed.orders if o.slot is slot), None)
        price = resting.price if resting else None
        at = f" @ {cents(price, fm.tick)}" if resting else ""
        self.emit(EventKind.FILL, f"FILL {fm.label} {slot.name} {shares:g} shares{at}",
                  fm.condition_id, slot, price, shares,
                  data={"price_source": "resting order at the last cycle" if resting
                        else "unknown: no order rested in the slot",
                        "order_id": resting.id if resting else None})

    def refresh_rewards(self, now):
        """Live only, best effort: our scoring share per market and today's earnings."""
        try:
            self.scoring = parse_reward_percentages(self.client.get_reward_percentages())
        except Exception as e:
            log(f"Warning: reward percentages unavailable ({e})")
        date = now.strftime("%Y-%m-%d")
        try:
            earnings, skipped = parse_earnings(self.client.get_earnings_for_user_for_day(date))
            if skipped:
                log(f"Warning: {skipped} earnings entries had an unexpected shape")
            farm_db.store_rewards(self.db, date, earnings, now)
        except Exception as e:
            log(f"Warning: earnings for {date} unavailable ({e})")

    def open_orders(self, managed):
        if not self.live:
            return list(managed.sim_orders)
        entries = self.client.get_open_orders(
            OpenOrderParams(market=managed.fm.condition_id)
        )
        orders = [parse_open_order(e, managed.fm) for e in entries]
        return [o for o in orders if o is not None]

    def cycle(self):
        now = self.last_cycle_at = datetime.now(timezone.utc)
        self.read_controls(now)
        if self.stop_reason is not None:
            return
        unpositioned = any(m.position is None for m in self.active())
        if unpositioned or time.monotonic() >= self.positions_at:
            self.refresh_positions(now)
            self.positions_at = time.monotonic() + POSITIONS_S
        if self.live and time.monotonic() >= self.rewards_at:
            self.refresh_rewards(now)
            self.rewards_at = time.monotonic() + REWARDS_S

        active = self.active()
        books = fetch_levels(self.http, [m.fm.fav_token_id for m in active])
        for managed in active:
            try:
                self.step_market(managed, books.get(managed.fm.fav_token_id), now)
            except Exception as e:
                log(f"Error on {managed.fm.label}: {e}")
        self.flush()

    def step_market(self, managed, levels, now):
        fm = managed.fm
        if levels is None:
            log(f"{fm.label}: no book this cycle")
            return
        if managed.position is None:
            log(f"{fm.label}: no position yet; not quoting")
            return
        if levels.tick:
            fm = managed.fm = fm._replace(tick=levels.tick)

        orders = managed.orders = self.open_orders(managed)
        book = strip_own(levels, orders, fm) if self.live else levels
        book_mid = adjusted_mid(book.bids, book.asks, fm.min_size)
        obs = Observation(
            mid=book_mid,
            recent_mids=tuple(managed.recent_mids),
            rewards_active=rewards_active(managed.info, fm.condition_id,
                                          self.total_rates.rates),
            accepting_orders=managed.info.accepting_orders,
            closed=managed.info.closed,
            approved=managed.approved,
            expires_at=fm.expires_at,
            paused=fm.paused,
            naked_paused=fm.naked_paused,
            naked_cooldown_until=managed.naked_paused_until,
        )
        previous, managed.state = managed.state, step_state(managed.state, obs, now, self.cfg)
        if book_mid is not None:
            managed.recent_mids.append((now, book_mid))
        while managed.recent_mids and (
            now - managed.recent_mids[0][0]
        ).total_seconds() > self.cfg.drop_window_s:
            managed.recent_mids.popleft()
        if managed.state != previous:
            state = managed.state
            self.emit(EventKind.PHASE, f"{fm.label}: {previous} -> {state}", fm.condition_id,
                      data={"from": previous.phase.value, "to": state.phase.value,
                            "reason": state.reason, "until": stamp(state.until)})

        target = Target(book_mid, [])
        if managed.state.phase is Phase.QUOTING:
            target = target_orders(
                fm, book, managed.position, self.cfg, naked_allowed(obs, now)
            )
        actions = without_backed_off(
            reconcile(target.intents, orders, target.mid, fm, managed.position, self.cfg),
            managed.backoffs,
            now,
        )
        self.report(managed, target, orders, actions)
        for slot, placed in self.execute(managed, actions).items():
            if placed:
                managed.backoffs.pop(slot, None)
                continue
            backoff = managed.backoffs[slot] = next_backoff(managed.backoffs.get(slot), now)
            self.emit(EventKind.BACKOFF,
                      f"  {slot.name} on {fm.label}: backing off {backoff.delay_s:g}s",
                      fm.condition_id, slot,
                      data={"until": stamp(backoff.until), "attempts": backoff.attempts})
        managed.snapshot = self.market_state(managed, levels, book_mid, target, orders,
                                             actions, now)
        if (managed.mid_sampled_at is None
                or (now - managed.mid_sampled_at).total_seconds() >= MID_SAMPLE_S):
            managed.mid_sampled_at = now
            self.mids.append(mid_sample(managed.snapshot))
        if managed.state.phase is Phase.RETIRED:
            self.retire(managed)

    def market_state(self, managed, levels, book_mid, target, orders, actions, now):
        fm, state = managed.fm, managed.state
        q1, q2 = q_totals(target.intents)
        return farm_db.FarmState(
            condition_id=fm.condition_id,
            updated_at=now,
            phase=state.phase.value,
            phase_reason=state.reason,
            phase_until=state.until,
            book_mid=book_mid,
            mid=target.mid,
            q1=q1,
            q2=q2,
            capital_usd=fm.capital,
            naked_used_usd=naked_used(managed.position, orders),
            position=position_view(managed.held, managed.baseline, managed.position),
            slots=slot_views(state, orders, target, actions, managed.backoffs, fm,
                             managed.position, self.cfg),
            book=book_view(levels, orders, target.mid or book_mid, fm),
            naked_paused_until=managed.naked_paused_until,
            backoff={slot.name: {"until": stamp(b.until), "attempts": b.attempts}
                     for slot, b in managed.backoffs.items()},
            scoring_pct=self.scoring.get(fm.condition_id) if self.live else None,
        )

    def report(self, managed, target, orders, actions):
        fm = managed.fm
        q1, q2 = q_totals(target.intents)
        cancelled = {i for a in actions if isinstance(a, Cancel) for i in a.order_ids}
        placed = sum(isinstance(a, Place) for a in actions)
        held = len(orders) - len(cancelled)
        mid = cents(target.mid, fm.tick) if target.mid is not None else "--"
        log(f"{fm.label}: {managed.state.phase.value} mid {mid} "
            f"Q1 {q1:.1f} Q2 {q2:.1f} | hold {held} cancel {len(cancelled)} place {placed}")
        slot_order = list(Slot)
        for o in sorted(orders, key=lambda o: slot_order.index(o.slot)):
            if o.id not in cancelled:
                continue
            s = order_score(o.slot, o.price, target.mid, fm.v) if target.mid else 0.0
            self.emit(EventKind.CANCEL,
                      f"  CANCEL {o.slot.name:8} {cents(o.price, fm.tick):>7} x "
                      f"{o.size_remaining:>9.2f}  score {s:.3f}  {fm.label}",
                      fm.condition_id, o.slot, o.price, o.size_remaining, s,
                      data={"order_id": o.id})
        for a in actions:
            if isinstance(a, Place):
                i = a.intent
                self.emit(EventKind.PLACE,
                          f"  PLACE  {i.slot.name:8} {cents(i.price, fm.tick):>7} x "
                          f"{i.size:>9.2f}  score {i.score:.3f}  {fm.label}",
                          fm.condition_id, i.slot, i.price, i.size, i.score)

    def execute(self, managed, actions):
        """Carry out the actions. Returns {slot: whether its place was accepted}."""
        placed = {a.intent.slot: False for a in actions if isinstance(a, Place)}
        if not self.live:
            cancelled = {i for a in actions if isinstance(a, Cancel) for i in a.order_ids}
            kept = [o for o in managed.sim_orders if o.id not in cancelled]
            for a in actions:
                if isinstance(a, Place):
                    self.sim_ids += 1
                    i = a.intent
                    kept.append(LiveOrder(f"sim-{self.sim_ids}", i.slot, i.token_id,
                                          i.side, i.price, i.size))
            managed.sim_orders = kept
            return dict.fromkeys(placed, True)

        fm = managed.fm
        for a in actions:
            if isinstance(a, Cancel):
                try:
                    resp = self.client.cancel_orders(list(a.order_ids))
                    failed = (resp or {}).get("not_canceled") or {}
                    for order_id, why in dict(failed).items():
                        self.emit(EventKind.ALERT, f"  cancel {order_id} failed: {why}",
                                  fm.condition_id, data={"order_id": order_id})
                except Exception as e:
                    self.emit(EventKind.ALERT, f"  cancel failed on {fm.label}: {e}",
                              fm.condition_id)

        signed = []
        options = PartialCreateOrderOptions(tick_size=f"{fm.tick:g}", neg_risk=fm.neg_risk)
        expiration = int(fm.expires_at.timestamp())
        for a in actions:
            if isinstance(a, Place):
                i = a.intent
                try:
                    order = self.client.create_order(
                        OrderArgsV2(token_id=i.token_id, price=i.price, size=i.size,
                                    side=i.side, expiration=expiration),
                        options,
                    )
                    signed.append((i, order))
                except Exception as e:
                    self.emit(EventKind.ALERT, f"  sign {i.slot.name} failed on {fm.label}: {e}",
                              fm.condition_id, i.slot)
        for chunk in _chunks(signed, POST_BATCH_LIMIT):
            try:
                responses = self.client.post_orders(
                    [PostOrdersV2Args(order=o, orderType=OrderType.GTD) for _, o in chunk],
                    post_only=True,
                )
            except Exception as e:
                self.emit(EventKind.ALERT,
                          f"  post of {len(chunk)} orders failed on {fm.label}: {e}",
                          fm.condition_id)
                continue
            for (i, _), resp in zip(chunk, responses if isinstance(responses, list) else []):
                if not isinstance(resp, dict) or not resp.get("success") or resp.get("errorMsg"):
                    self.emit(EventKind.REJECT,
                              f"  {i.slot.name} {cents(i.price, fm.tick)} rejected on "
                              f"{fm.label}: {resp}",
                              fm.condition_id, i.slot, i.price, i.size, i.score,
                              data={"response": resp if isinstance(resp, dict) else str(resp)})
                else:
                    placed[i.slot] = True
        return placed

    def retire(self, managed):
        fm, reason = managed.fm, managed.state.reason
        managed.sim_orders = []
        if self.live:
            farm_db.mark_retired(self.db, fm.condition_id, reason)
            line = f"{fm.label}: retired ({reason}); orders cancelled"
        else:
            line = f"{fm.label}: retired ({reason}); dry run leaves markets unchanged"
        self.emit(EventKind.RETIRE, line, fm.condition_id, data={"reason": reason})

    def check_collateral(self):
        planned = sum(
            max(0.0, m.fm.capital - m.position.fav_shares * m.position.fav_avg)
            for m in self.active() if m.position is not None
        )
        try:
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            balance = int(resp["balance"]) / 1e6
        except Exception as e:
            log(f"Warning: collateral balance check failed ({e})")
            return
        log(f"Collateral ${balance:,.2f}; favorite bids plan up to ${planned:,.2f}")
        if balance < planned:
            self.emit(EventKind.ALERT,
                      "Warning: collateral is short of the planned favorite bids; "
                      "the CLOB will reject the bids it cannot fund",
                      data={"balance_usd": balance, "planned_usd": planned})

    def cancel_managed(self):
        for cid, managed in self.markets.items():
            try:
                self.client.cancel_market_orders(OrderMarketCancelParams(market=cid))
                self.emit(EventKind.CANCEL, f"Cancelled all orders on {managed.fm.label}", cid)
            except Exception as e:
                self.emit(EventKind.ALERT,
                          f"Warning: cancelling orders on {managed.fm.label} failed ({e})", cid)


def heartbeat(client, stop):
    """Keep the CLOB's dead-man switch fed; the server chains heartbeat ids."""
    heartbeat_id = ""
    while not stop.is_set():
        try:
            resp = client.post_heartbeat(heartbeat_id)
            heartbeat_id = (resp or {}).get("heartbeat_id") or heartbeat_id
        except Exception as e:
            log(f"Warning: heartbeat failed ({e})")
        stop.wait(HEARTBEAT_S)


def run(args):
    cfg = config_from(args)
    client = None
    if args.live:
        from config import create_client, load_config
        client = create_client(load_config())
    farmer = Farmer(args, cfg, client)
    if not farmer.user:
        log("Error: POLYMARKET_FUNDER_ADDRESS is not set; positions cannot be read")
        raise SystemExit(1)

    def on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)

    threading.Thread(target=farmer.total_rates.run, daemon=True).start()
    if args.live and args.heartbeat:
        threading.Thread(target=heartbeat, args=(client, farmer.stop), daemon=True).start()
        log("Heartbeat on: if it stops for 10s Polymarket cancels this key's open orders")

    # A stop command or Ctrl+C cancels live orders; so does a crash. Only a
    # finished --once run leaves them resting.
    reason, cancel = "--once finished", False
    try:
        farmer.start()
        while True:
            started = time.monotonic()
            try:
                farmer.cycle()
            except Exception as e:
                log(f"Cycle failed: {e}")
            if farmer.stop_reason is not None:
                reason, cancel = farmer.stop_reason, True
                log("Stopping")
                break
            if args.once:
                break
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        reason, cancel = "interrupted", True
        log("Stopping")
    except BaseException as e:
        reason, cancel = f"crashed: {e!r}", True
        raise
    finally:
        farmer.shutdown(reason, cancel)
        if args.live:
            from key_loader import cleanup_key_source
            cleanup_key_source()


def config_from(args):
    return FarmConfig(
        capital=args.capital,
        bid_offset=args.bid_offset,
        ask_offset=args.ask_offset,
        min_capture=args.min_capture,
        ev_margin=args.ev_margin,
        min_fav=args.min_fav,
        min_low_score=args.min_low_score,
        keep_ratio=args.keep_ratio,
        drop_threshold=args.drop_threshold,
        drop_window_s=args.drop_window,
        cooldown_s=args.cooldown,
        naked_cooldown_s=args.naked_cooldown,
        expiry_buffer_days=args.expiry_buffer_days,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Farm Polymarket liquidity rewards on the markets approved in the "
            "screener's SQLite file. Dry run unless --live."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python reward_farmer.py                          # dry run, forever
  python reward_farmer.py --db scratch.db --once   # one dry-run cycle
  python reward_farmer.py --live --capital 300     # real orders
        """,
    )
    parser.add_argument("--db", default="farm.db", help="SQLite file (default: farm.db)")
    parser.add_argument("--live", action="store_true",
                        help="Sign and post real orders (default: dry run)")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument(
        "--heartbeat", action="store_true",
        help=(
            "Live only: post a heartbeat every 5s. WARNING: if heartbeats then "
            "stop for 10s (crash, network loss, sleep), Polymarket cancels ALL "
            "open orders owned by these API credentials, which may include "
            "orders placed by your other tools; whether the scope is the API "
            "key or the whole account is undocumented. Off by default."
        ),
    )
    for flag, kind, default, help_text in (
        ("--interval", float, 5.0, "Seconds between cycles"),
        ("--capital", float, 500.0, "USD per market behind the favorite bid, "
                                    "favorite inventory at cost included"),
        ("--bid-offset", float, 0.01, "Favorite bid distance below the midpoint"),
        ("--ask-offset", float, 0.01, "Favorite ask distance above the midpoint"),
        ("--min-capture", float, 0.005, "Favorite ask minimum above average cost"),
        ("--ev-margin", float, 0.01, "Favorite bid maximum is p_true minus this"),
        ("--min-fav", float, 0.90, "Stop quoting below this favorite midpoint"),
        ("--min-low-score", float, 0.10, "Minimum per-share score of the longshot bid"),
        ("--keep-ratio", float, 0.8, "Keep an order scoring at least this share "
                                     "of its target"),
        ("--drop-threshold", float, 0.03, "Midpoint fall that triggers a cooldown"),
        ("--drop-window", float, 60.0, "Seconds over which the fall is measured"),
        ("--cooldown", float, 600.0, "Seconds without quotes after a midpoint drop"),
        ("--naked-cooldown", float, 3600.0, "Seconds without a longshot bid after "
                                            "a longshot fill"),
        ("--expiry-buffer-days", float, 7.0, "Retire this many days before the end date"),
    ):
        parser.add_argument(flag, type=kind, default=default,
                            help=f"{help_text} (default: {default:g})")
    return parser


def main():
    _make_output_safe()
    load_dotenv(Path(__file__).parent / ".env")
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
