#!/usr/bin/env python3
"""
Polymarket Liquidity-Reward Farmer

Farms Polymarket liquidity rewards on the markets a human approved with
`python -m farm.reward_screener approve`. It runs as a daemon: every few seconds it reads
the favorite's order book, works out the orders it wants resting, compares
them with the orders it has, and cancels or places the difference. By default
it is a dry run that prints what it would do; `--live` signs and posts.

Above a 0.90 midpoint Polymarket only pays for two-sided liquidity. A maker's
score is min(Q_one, Q_two), where Q_one sums favorite bids and longshot asks
and Q_two sums favorite asks and longshot bids, each order weighted by
S(v, s) = ((v - s) / v)^2 at distance s from the midpoint inside the max
spread v. Score is sampled once a minute and the pool is paid daily. So the
farmer rests on both sides, in four slots per market:

  FAV_BID   favorite BUY, Q_one, uses cash. A fill holds capital until
            FAV_ASK sells it, which takes weeks and longer the more shares
            are held, or until the market resolves first. So it costs the
            price less, discounted at the hurdle rate over that hold, what
            the share sells for: the ask the fill would leave resting, never
            credited over p_true, and p_true for what is unsold at
            resolution (farm/fill_cost.py has the measurements). The bid rests at
            the price in the band, and the size at or under the market's
            capital, whose rewards net of expected fills (from the market's
            fill history, shrunk toward the last scan's cross-market prior)
            are highest, never above p_true minus a margin, and only when
            that net beats the orders without it. Until the market's history
            is read it does not rest at all.
  LONG_ASK  longshot SELL, Q_one, covered by longshot shares. Sells off
            longshot inventory from an unwanted fill, at the mirror of the
            favorite bid so it scores the same.
  FAV_ASK   favorite SELL, Q_two, covered by favorite shares. Exits favorite
            inventory above its average cost and frees the capital again.
  LONG_BID  longshot BUY, Q_two, naked. It exists only to make Q_one count.
            A fill of it at price q sells the favorite at 1 - q, so its
            zones go by the annual return that gives a favorite buyer who
            holds to the end date (reward_screener.fill_return). Under
            FREE_RETURN almost no buyer takes it, and the bid is sized to
            cover the rest of Q_one whatever that costs: the market's
            naked_budget_usd does not limit it, so long as a share costs at
            most FREE_MAX_COST for each unit of its score. It takes the
            cheapest such price, 0.1c on most markets, unless a cheaper
            price covers within the budget. From FREE_RETURN up it takes
            the cheapest price whose covering size fits the budget, less
            longshot shares held, and never one above the cheapest price
            scoring --min-low-score. Over MAX_RETURN it does
            not rest at all: such a bid is one convergence fills. Once a
            longshot bid has filled, the budget less the shares held limits
            every price until LONG_ASK has sold them. And a price is free
            of the budget only once it has passed its test on every cycle
            for FREE_DWELL_S: a midpoint that flickers across the test would
            flip the plan, and the favorite bid's size with it, as often as
            it crossed.

Q_two comes from the covered FAV_ASK first and only the remainder from the
naked LONG_BID. When the budget leaves the LONG_BID short of Q_one, the
favorite bid shrinks to match: score on one side beyond the other earns
nothing and only ties up capital.

The farmer never buys at market. Every order is post-only, so any fill is a
maker fill that captures spread, and every order is GTD, expiring at the
market's end date minus the expiry buffer (7 days), when the market retires.

Each market has a lifecycle, advanced every cycle by `step_state`:

  QUOTING      orders rest as planned.
  COOLDOWN     the favorite's ask, read where its asks add up to three reward
               minimums, fell fast; quotes come down for a while, because
               sellers repricing the favorite down is how a bid gets
               filled right before the news arrives. A bid leaving never
               cools down: it looks the same cancelled or sold into, and the
               plan just follows the bids that remain.
  BELOW_RANGE  the midpoint fell under --min-fav, where the strategy does not
               apply; it resumes a little above the threshold.
  PAUSED       a human paused the market (markets.paused). It leaves the
               moment the flag clears and goes through the checks above.
               When resuming would quote, the orders it would place are
               published to farm_state as a preview and never sent.
  UNREWARDED   the total reward listing no longer pays the market; its own
               market record keeps showing the old rate after rewards end.
               Checked before the pause. When rewards return it goes through
               the pause and the checks above.
  RETIRED      market closed or not accepting orders, no longer approved, or
               the expiry buffer reached. Terminal, and checked before the
               rewards and the pause. A market a human approves again
               after the farmer saw it unapproved starts over from QUOTING
               with fresh runtime state, through the same checks.

Every state but QUOTING wants no orders, so the same reconcile step that
maintains quotes also cancels them. Separately, a longshot fill (longshot
shares went up) pauses the naked LONG_BID for --naked-cooldown seconds, and
with it the favorite bid it was balancing; markets.naked_paused does the same
for as long as a human leaves it set.

farm.db is the control surface (see farm/farm_db.py). Every cycle the farmer
re-reads the approved markets with their pause flags and capital, the
settings that override its command-line config, and any stop command, so a
change in the console takes effect within one interval. It publishes what it
is doing to farm_state, farmer_status, farm_events, and (one sample per market
per minute) farm_mids, in dry run and live alike; only live mode writes to
markets (retirement, baselines). Each quoting market's favorite book goes to
farm_books.db beside farm.db every five minutes, on a thread of its own (see
farm/farm_books.py).

Stopping cancels the live orders on the managed markets, whether by a stop
command, Ctrl+C, SIGTERM, or a crash; only a finished --once run leaves them
resting. A stop command asking to keep orders, or Ctrl+C and SIGTERM under
--keep-orders, leaves them resting too. Until the next start they are
unmanaged: no cooldowns, retirement, or fill handling. The next start adopts
the ones still close to target and replaces the rest; a market no longer
approved by then is not managed, and its orders rest until they expire.
--heartbeat cannot keep orders: Polymarket cancels them 10s after the
heartbeats stop.

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
replacing it on every wiggle of the midpoint. A favorite bid is held to most
of the best bid's net instead of the score, valued as it rests at its own
price and size, and the orders that balance it are planned around the bid
that stays rather than the target. A longshot bid is kept while it covers
keep_ratio of the score it balances, up to twice it where the naked budget
limits it above the lowest tick,
at a price no higher than target or within two ticks of the min-score price,
and not once its price has come to offer more than MAX_RETURN a year. A
favorite ask more than two ticks under its target is replaced although it
scores more, so it follows a target that rises. Replacing an order loses its queue
position, costs two API calls, and scores nothing for the ~30 seconds before
Polymarket starts scoring the new order, so this hysteresis matters.

Planning, reconciling, and the lifecycle are pure functions; the daemon around
them only fetches, executes, and logs.

Usage:
  python -m farm.reward_farmer                      # dry run, forever
  python -m farm.reward_farmer --db scratch.db --once
  python -m farm.reward_farmer --live               # sign and post real orders
  python -m farm.reward_farmer --live --keep-orders # Ctrl+C leaves orders resting
  python -m farm.reward_farmer --live --key-stdin   # the wallet key is piped in

A live run first checks where it connects from, as polymarket.region
describes, and does not start from a place whose new orders Polymarket
rejects. It then reads the wallet key from the environment or the Bitwarden
vault, as polymarket.keys describes, and asks for the vault's master password
when the vault is locked. --key-stdin reads the key itself from one line of
standard input instead, for a machine with no vault.

--db defaults to farm.db in the data directory (see data_dir.py): the
directory POLYMARKET_DATA_DIR names, or the repo directory.
"""

import argparse
import getpass
import json
import math
import os
import signal
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import NamedTuple, Optional

import httpx
from dotenv import load_dotenv

import data_dir
from farm import farm_books, farm_db, fill_cost
from farm.exit_value import TAPE_ROWS_FARMER, tape_from_json, tape_to_json
from farm.farm_db import EventKind, stamp
from py_clob_client_v2.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgsV2,
    OrderMarketCancelParams,
    OrderType,
    PartialCreateOrderOptions,
    PostOrdersV2Args,
)

from farm.reward_screener import (
    Outlook,
    QuoteValue,
    Read,
    Skip,
    Zones,
    _make_output_safe,
    adjust_levels,
    adjusted_mid,
    fav_bid_candidates,
    fav_ceiling,
    fetch_gamma,
    fetch_levels,
    fetch_read,
    fetch_total_rates,
    fill_return,
    floor_to_tick,
    free_zone_open,
    load_prior,
    longshot_bid,
    over_max_return,
    q_min,
    resolution_signal,
    round_size_down,
    score,
    sized_without_budget,
    tick_decimals,
    value_quote,
)
from polymarket import region
from polymarket.books import Levels
from polymarket.gamma import GAMMA_REQUEST_LIMIT, request_markets
from polymarket.hosts import DATA_API_HOST
from polymarket.http import RATE_LIMIT_RETRIES, chunks, get_with_retry
from polymarket.parse import as_float, parse_time

BUY, SELL = "BUY", "SELL"

# A BELOW_RANGE market resumes this far above --min-fav, so a midpoint
# hovering at the threshold does not flap the quotes on and off.
RESUME_MARGIN = 0.005

# The fractions of the capital left that a favorite bid is planned at.
SIZE_LADDER = (1.0, 0.75, 0.5, 0.25, 0.125)

# An order whose size left this band around the target gets replaced.
KEEP_SIZE_RANGE = (0.8, 1.25)

# A longshot bid above its market's lowest tick covering more than this multiple
# of the score it balances gets replaced, so a kept bid cannot ratchet up to
# the whole naked budget where a fill costs real money. At the lowest tick it
# stays: at 0.1c the extra cover costs cents, and on a 1c tick one tick of
# mid moves the cover by more than this multiple. A bid free of the budget
# stays too: it cost at most FREE_MAX_COST a unit of the score it was sized
# for, and a smaller one in its place would save part of that for 30 seconds
# unscored. Near the band edge, where the dearer free prices sit, a tick of
# mid moves the cover by a third to a half, so such a bid would be replaced
# often.
LONG_COVER_MAX = 2.0

# How long a price must have passed its test for the free zone, on every
# cycle, before the naked budget leaves a bid there alone (steady_mid). One
# cycle that fails puts the price back under the budget at once. Live, a
# 107-share bid coming and going moved what one market's 0.1c share scored
# between 0.0008 and 0.0053 every few cycles, and its favorite bid flipped
# between all and a quarter of its capital 17 times in seven minutes.
FREE_DWELL_S = 600

# farm_mids samples further apart than this, or a last one older than this at
# start, do not vouch for the midpoint in between. They are due every
# MID_SAMPLE_S.
FREE_SEED_GAP_S = 180

# A longshot bid priced over target stays while it is within this many ticks
# of the cheapest price scoring min_low_score, which moves a tick with each
# tick of mid: the cheapest covering price swings with a two-tick wobble.
LONG_PRICE_SLACK_TICKS = 2

# A favorite ask priced under target stays while it is within this many
# ticks of it: the target moves a tick with each tick of mid, and a cheaper
# ask out-scores it, so score alone never lifts an ask to a dearer target.
ASK_PRICE_SLACK_TICKS = 2

# The CLOB lists some orders one size step (0.01 share) under the size placed,
# so a cover check exact to the share would replace them every cycle.
SIZE_STEP = 0.01

# A resting favorite bid stays while its net trails the best bid's by no
# more than this, in $/day, or (1 - keep_ratio) of the best net if larger.
NET_KEEP_SLACK_USD = 0.02

# An ask drop is measured where the asks add up to this many reward minimums.
DROP_DEPTH = 3

POSITIONS_S = 60
MARKET_INFO_S = 300
MARKET_INFO_RETRY_S = 60
MARKET_INFO_TICK_S = 5
HISTORY_S = 3600
HISTORY_RETRY_S = 120
HISTORY_TICK_S = 15
RESOLUTION_S = 60
TOTAL_RATES_S = 300
REWARDS_S = 600
MID_SAMPLE_S = 60
# farm_mids read back this far for the mid before a fill.
FILL_MID_LOOKBACK_S = 600
HEARTBEAT_S = 5
# Stopping waits this long for the book recorder to write its last batch.
BOOKS_JOIN_S = 10

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


def signed_usd(value):
    return f"-${-value:.2f}" if value < 0 else f"${value:.2f}"


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
    # What decides where the longshot bid may rest (reward_screener.Zones): an
    # observation, not a term. years_left is read with the terms each cycle.
    # steady_mid is the farmer's own, set each cycle from its watch of the
    # midpoint (Managed.mid_watch), and None without that evidence.
    zones: Zones
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


NOTHING_RESTING = MappingProxyType({})


class FavChoice(NamedTuple):
    """How the favorite bid was priced. best is the QuoteValue of the bid
    chosen, or None with the reason there is none. resting maps the id of
    each favorite bid resting to the QuoteValue of the orders planned around
    it as it rests, at its own price and size; a bid no plan can rest, off
    the candidate prices or with nothing to balance it, has no entry.
    baseline_per_day is what the plan with no favorite bid earns."""

    best: Optional[QuoteValue] = None
    resting: MappingProxyType = NOTHING_RESTING
    baseline_per_day: float = 0.0
    reason: str = ""


class Target(NamedTuple):
    """The orders wanted, and the midpoint they were scored against.

    mid is the size-adjusted midpoint after our own quotes join the book, or
    None when the book is one-sided and nothing can be planned.
    """

    mid: Optional[float]
    intents: list
    fav: FavChoice = FavChoice()


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
    UNREWARDED = "UNREWARDED"
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

    mid is the adjusted midpoint and ask is the price at which the asks add
    up to DROP_DEPTH reward minimums, both in the book without our orders;
    recent_asks holds (time, ask) from earlier cycles. paused and naked_paused
    are the human's flags on the market row; naked_cooldown_until is set by
    the farmer after a longshot fill.
    """

    mid: Optional[float]
    ask: Optional[float]
    recent_asks: tuple
    rewards_active: bool
    accepting_orders: bool
    closed: bool
    approved: bool
    expires_at: datetime
    paused: bool = False
    naked_paused: bool = False
    naked_cooldown_until: Optional[datetime] = None
    resolution: Optional[str] = None


class MarketInfo(NamedTuple):
    """Gamma's market record: tokens, reward terms, and trading status."""

    token_ids: tuple
    v: float
    min_size: float
    tick: float
    neg_risk: bool
    accepting_orders: bool
    closed: bool


class FarmConfig(NamedTuple):
    capital: float
    max_lead: float
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
    hurdle_rate: float


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


def capital_room(fm, position):
    """USD of favorite bid the capital still allows after favorite shares held, at cost."""
    return max(0.0, fm.capital - position.fav_shares * position.fav_avg)


def naked_room(fm, position):
    """USD of longshot bid the budget still allows after longshot shares held,
    at cost. It does not limit a bid sized without the budget
    (free_of_budget)."""
    return max(0.0, fm.naked_budget_usd - position.long_shares * position.long_avg)


class MidWatch(NamedTuple):
    """A market's midpoint, in the book without our orders, over the dwell:
    since when every cycle has had one, and the (time, midpoint) of each
    cycle of the last FREE_DWELL_S."""

    since: datetime
    mids: tuple


def watch_mid(watch, book_mid, at):
    """The watch after a cycle at `at` whose book without our orders has the
    midpoint book_mid. A one-sided book has none and ends the watch."""
    if book_mid is None:
        return None
    if watch is None:
        return MidWatch(at, ((at, book_mid),))
    kept = tuple((t, mid) for t, mid in watch.mids if (at - t).total_seconds() <= FREE_DWELL_S)
    return watch._replace(mids=kept + ((at, book_mid),))


def steady_mid(watch, now):
    """The lowest midpoint of the last FREE_DWELL_S, or None unless the
    midpoint has been watched that long. A price is free of the naked budget
    only if it passes its test there too (sized_without_budget). A longshot
    share scores least against the lowest favorite midpoint, so the test has
    then passed on every cycle of the dwell, and one cycle that fails it
    counts at once and for the whole dwell after.

    The midpoint is the book's without our orders, the one the lifecycle
    watches. A favorite bid of ours only lifts the midpoint a plan scores
    against, so a share scores no less in the plan than here. A favorite ask
    of ours under the book's can lower it, and the plan's own test then
    holds the price to the budget."""
    if watch is None or (now - watch.since).total_seconds() < FREE_DWELL_S:
        return None
    return min((mid for t, mid in watch.mids if (now - t).total_seconds() <= FREE_DWELL_S),
               default=None)


def seeded_watch(samples, now):
    """The watch at start, from the market's farm_mids samples: (time, book
    midpoint), oldest first. A gap over FREE_SEED_GAP_S between samples
    starts the watch again, and a last sample older than that is no evidence
    at all, so a market the farmer has not watched starts with its free zone
    closed."""
    watch = last = None
    for at, book_mid in samples:
        if last is not None and (at - last).total_seconds() > FREE_SEED_GAP_S:
            watch = None
        watch, last = watch_mid(watch, book_mid, at), at
    if last is None or (now - last).total_seconds() > FREE_SEED_GAP_S:
        return None
    return watch


def long_zones(fm, position):
    """The zones this market's longshot bid is planned and kept by: fm.zones,
    with the free zone closed while longshot shares rest an exit
    (free_zone_open)."""
    if free_zone_open(position.long_shares, fm.min_size):
        return fm.zones
    return fm.zones._replace(steady_mid=None)


def free_of_budget(price, mid, fm, position):
    """Whether the naked budget leaves a longshot bid of this market at price
    alone this cycle, scored against the favorite midpoint mid
    (sized_without_budget). The one test the plan, the keep rule and the
    console's figures share."""
    return sized_without_budget(price, order_score(Slot.LONG_BID, price, mid, fm.v), fm.v,
                                long_zones(fm, position))


def mirror(price, tick):
    """The longshot price equivalent to a favorite price, and vice versa."""
    return round(1 - price, tick_decimals(tick))


def plan_long_ask_price(fm, levels, fav_price, cfg):
    """Longshot ask at the mirror of the favorite bid, or of the highest one allowed
    when there is none; None when the book leaves no favorite bid to mirror. With no
    favorite bid the exit takes the most fill-prone price: its fills are wanted."""
    if fav_price is None:
        fav_price = fav_ceiling(levels, fm.tick, fm.min_size, cfg.max_lead)
    return None if fav_price is None else mirror(fav_price, fm.tick)


def plan_fav_ask_price(fm, levels, book_mid, position, cfg, fill_mid=None):
    """Favorite ask above both the midpoint and our cost, or None if it would cross.

    The midpoint is the one before the fill the inventory came from when that
    is higher. fill_cost.SOLD is the share of a fill this ask sold, so a
    change to the rule here calls for a new measurement there.
    """
    tick = fm.tick
    anchor = max(book_mid, fill_mid or 0.0)
    price = max(
        ceil_to_tick(anchor + max(cfg.ask_offset, tick), tick),
        exit_floor(position, fm, cfg),
    )
    best_bid = levels.bids[0][0] if levels.bids else None
    if best_bid is not None:
        price = max(price, ceil_to_tick(best_bid + tick, tick))
    price = min(price, round(1 - tick, tick_decimals(tick)))
    if best_bid is not None and price <= best_bid + 1e-9:
        return None
    return price


def quotes_at(fm, levels, position, cfg, naked_allowed, fav_price, fav_size=None,
              fill_mid=None):
    """The orders this market would rest with its favorite bid at fav_price, or
    with no favorite bid when fav_price is None, scored against the mid they create.
    The favorite ask prices off fill_mid per plan_fav_ask_price.

    `levels` is the favorite book without our own resting orders. The bid
    starts at fav_size, or at all of capital_room without one. Sizes are
    balanced so Q_two covers Q_one: covered FAV_ASK first, then the naked
    LONG_BID, at the cheapest free price whatever the rest costs, or else at
    the cheapest price that covers the rest within the budget, no higher
    than the min_low_score price (longshot_bid), then the favorite bid
    shrinks to whatever Q_two reached. While longshot shares rest an exit the
    budget limits every price (long_zones), so a filled bid cannot grow the
    next one. No LONG_BID rests at a price offering over MAX_RETURN a year
    (fm.zones): when the prices under it cannot cover, the favorite bid
    shrinks or goes the same way.
    The LONG_BID is re-derived when the favorite bid is dropped, because the
    cheapest covering price depends on what it must cover. A bid that ends
    below the reward minimum size is dropped, because an order that does not
    score is risk without reward; the two asks are exits and stay regardless.
    """
    book_mid = adjusted_mid(levels.bids, levels.asks, fm.min_size)
    if book_mid is None:
        return Target(None, [])
    tick, v = fm.tick, fm.v

    fav_bid = None
    if fav_price is not None and fav_size is None:
        fav_size = round_size_down(capital_room(fm, position) / fav_price)
    if fav_price is not None:
        fav_bid = fav_price, fav_size
    long_ask = fav_ask = None
    if position.long_shares >= fm.min_size:
        long_ask = plan_long_ask_price(fm, levels, fav_price, cfg)
    if position.fav_shares >= fm.min_size:
        fav_ask = plan_fav_ask_price(fm, levels, book_mid, position, cfg, fill_mid)

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

    def long_bid(q_one):
        if not naked_allowed:
            return None
        return longshot_bid(levels, mid, v, tick, fm.min_size, q_one - q_two_covered,
                            naked_room(fm, position), cfg.min_low_score,
                            long_zones(fm, position))

    low = long_bid(q_one_exits + fav_score * fav_size)
    q_two = q_two_covered + (low.score * low.size if low else 0.0)
    if fav_score > 0 and q_one_exits + fav_score * fav_size > q_two:
        fav_size = round_size_down(max(0.0, q_two - q_one_exits) / fav_score)
    if fav_size < fm.min_size:
        fav_size = 0.0
        low = long_bid(q_one_exits)

    if fav_size > 0:
        add(Slot.FAV_BID, fav_bid[0], fav_size, fav_score)
    if low:
        add(Slot.LONG_BID, low.price, low.size, low.score)
    return Target(mid, [intents[slot] for slot in Slot if slot in intents])


def live_rate(fm, total_rates):
    """The market's daily pool: the live total, sponsored rewards included, else
    the rate of the scan it was approved from."""
    rate = total_rates.get(fm.condition_id) if total_rates else None
    return fm.rate_per_day if rate is None else rate


def market_outlook(fm, total_rates, history, prior, cfg, now):
    """The Outlook a favorite bid on this market is valued with: fills from its
    history shrunk toward prior, or prior alone without a history."""
    return Outlook(live_rate(fm, total_rates), fill_cost.years_until(fm.end_date, now),
                   fill_cost.fill_curve(history, prior, cfg.min_fav), cfg.hurdle_rate)


def fill_exit_price(fm, levels, position, cfg, bid, fill_mid=None):
    """The favorite ask the farmer would rest once `bid` fills: on the
    inventory with the bid's shares added at its price, anchored at the mid
    before the fill, the book's now, or fill_mid when higher, as
    plan_fav_ask_price takes them. None if that ask would cross."""
    book_mid = adjusted_mid(levels.bids, levels.asks, fm.min_size)
    shares = position.fav_shares + bid.size
    filled = position._replace(fav_shares=shares, fav_avg=(
        position.fav_shares * position.fav_avg + bid.size * bid.price) / shares)
    return plan_fav_ask_price(fm, levels, book_mid, filled, cfg, fill_mid)


def value_of(target, fm, levels, position, cfg, outlook, fill_mid=None):
    """value_quote of a planned set of orders, its favorite bid included, a
    fill adding to the favorite shares the position holds and sold at its
    fill_exit_price."""
    by = {i.slot: i for i in target.intents}
    bid = by.get(Slot.FAV_BID)
    capital = sum(i.price * i.size for slot, i in by.items() if slot.side == BUY)
    sale_price = bid and fill_exit_price(fm, levels, position, cfg, bid, fill_mid)
    return value_quote(levels, target.mid, fm.v, fm.min_size,
                       q_min(*q_totals(target.intents), target.mid),
                       bid and (bid.price, bid.size), capital, fm.p_true, outlook,
                       position.fav_shares, sale_price)


def target_orders(fm, levels, position, cfg, naked_allowed, outlook, bid_without_history=False,
                  fill_mid=None, resting=()):
    """The orders this market should rest, with the favorite bid at the price
    and size that earn the most net of what its fills cost.

    Every price from fav_bid_candidates, capped at ev_cap, is planned with
    quotes_at at each SIZE_LADDER fraction of capital_room that buys the
    reward minimum, and valued with value_quote on the plan as balanced. The
    capital is the most the bid may be: reward is a share of the pool, so
    each further share earns less, while a fill costs more a share the more
    inventory it leaves. The baseline is the plan with no favorite bid:
    nothing, or the exits alone. The best bid is quoted only when its net
    beats the baseline's reward, and a tie goes to the lower price, then to
    the smaller size. Fills not drawn from the market's own history quote the
    baseline, so real money is never bid on the prior alone, unless
    bid_without_history (a preview of a market the farmer has not read yet).

    Not every size is planned. A smaller bid at a price earns no more reward
    than a larger one there, and a fill never pays, so once a size's reward
    is under the best net found, no smaller size at its price can be chosen.
    Every price is valued at its full size first, which sets that bar high
    before any smaller size is tried. A price where the full size rests no
    bid rests no smaller one: the naked budget trims every size it cannot
    balance to the same bid.

    resting is the orders the market rests now. Each favorite bid among them
    at a candidate price is planned and valued the same way at its own price
    and size (FavChoice.resting), which is what keep_failure judges it by.
    """
    base = quotes_at(fm, levels, position, cfg, naked_allowed, None, fill_mid=fill_mid)
    if base.mid is None:
        return base._replace(fav=FavChoice(reason="one-sided book"))
    baseline = value_of(base, fm, levels, position, cfg, outlook, fill_mid).net_per_day

    def no_bid(reason):
        return base._replace(fav=FavChoice(baseline_per_day=baseline, reason=reason))

    if not outlook.fills.from_history and not bid_without_history:
        return no_bid("no fill history yet")

    def valued(price, size):
        """(plan, its QuoteValue) with the favorite bid at price, starting at
        size, or None when the plan rests no favorite bid."""
        plan = quotes_at(fm, levels, position, cfg, naked_allowed, price, size, fill_mid)
        if any(i.slot is Slot.FAV_BID for i in plan.intents):
            return plan, value_of(plan, fm, levels, position, cfg, outlook, fill_mid)
        return None

    prices = fav_bid_candidates(levels, fm.v, fm.tick, fm.min_size, cfg.max_lead,
                                ev_cap(fm, cfg))
    room = capital_room(fm, position)
    full = [valued(price, round_size_down(room / price)) for price in prices]
    bar = max((value.net_per_day for _, value in filter(None, full)), default=None)
    best = None
    for price, choice in zip(prices, full):
        smaller = (round_size_down(fraction * room / price) for fraction in SIZE_LADDER[1:])
        while choice:
            if best is None or choice[1].net_per_day >= best[1].net_per_day - 1e-12:
                best = choice
            bar = max(bar, choice[1].net_per_day)
            size = next(smaller, 0.0)
            if size < fm.min_size or choice[1].reward_per_day < bar - 1e-12:
                break
            choice = valued(price, size)
    if best is None:
        return no_bid("no scoring favorite bid")
    plan, value = best
    if value.net_per_day <= baseline:
        return no_bid(f"no favorite bid pays: best net {signed_usd(value.net_per_day)}/day at "
                      f"{cents(value.price, fm.tick)}")
    rested = {}
    for order in resting:
        price = round(order.price, tick_decimals(fm.tick))
        if order.slot is Slot.FAV_BID and price in prices:
            around = valued(price, order.size_remaining)
            if around:
                rested[order.id] = around[1]
    return plan._replace(fav=FavChoice(value, MappingProxyType(rested), baseline))


def keep_failure(order, intent, target, fm, position, cfg):
    """Why a resting order is not close enough to its target to leave alone, or None.

    A favorite bid is judged by the net of the orders planned around it as
    it rests (target.fav.resting) rather than by its score or the target's
    size: the net-best price moves a tick with every wiggle of the midpoint,
    while the net of the bid resting barely changes. It stays while that net
    is within the keep margin of the best net and above the baseline, the
    plan around it still rests at least 80% of it (a budget-trimmed bid's
    size moves a third between neighboring prices), and it is no more than
    25% over the capital left.
    A longshot bid is judged by the score it covers rather than its size: near
    the band edge a half-tick of mid moves its per-share score, and so the
    covering size, by a third. Score is min(Q_one, Q_two), so a shortfall
    loses that fraction of the score while over-coverage at 0.1c costs
    almost nothing. But a replacement scores nothing for about 30 seconds
    (Polymarket starts scoring an order ~30s after it is placed, measured
    live), and above a 0.90 mid that zeroes the whole market, so the bid is
    replaced only once it covers less than keep_ratio of the need. A bid
    priced over target stays within LONG_PRICE_SLACK_TICKS of the cheapest
    price scoring min_low_score, the fill-risk bound the plan prices under.
    The naked budget does not hold a bid while its price is free of it
    (free_of_budget). That bid stays whatever it costs or covers. Once the
    price is no longer free, because the midpoint dipped under its test,
    longshot shares rest an exit, or the nearing end date took the price out
    of the free zone, a bid over the budget is replaced by the plan's, once,
    and FREE_DWELL_S keeps the plan there.
    A longshot bid whose price has come to offer the favorite buyer more
    than MAX_RETURN a year is replaced whatever it covers. Returns only rise
    as the end date nears, so a resting bid leaves each zone once, and the
    target that replaces it is priced by the same zones in the same cycle.
    A favorite ask is judged by score too, but one more than
    ASK_PRICE_SLACK_TICKS under target is replaced although it scores more.
    """
    low, high = KEEP_SIZE_RANGE
    size = intent.size
    if (order.token_id, order.side) != (intent.token_id, intent.side):
        return "wrong token or side"
    if not on_tick(order.price, fm.tick):
        return "off the tick grid"
    if order.slot is Slot.FAV_BID:
        fav = target.fav
        value = fav.resting.get(order.id)
        if value is None:
            return "outside the bid candidates"
        size = value.size
        slack = max((1 - cfg.keep_ratio) * abs(fav.best.net_per_day), NET_KEEP_SLACK_USD)
        if (value.net_per_day < fav.best.net_per_day - slack
                or value.net_per_day <= fav.baseline_per_day):
            return "net below keep ratio"
    elif order.slot is Slot.LONG_BID:
        if over_max_return(order.price, fm.zones.years_left):
            return "over the return limit"
        need = intent.score * intent.size
        per_share = order_score(Slot.LONG_BID, order.price, target.mid, fm.v)
        covers = per_share * order.size_remaining
        under = round(order.price - (LONG_PRICE_SLACK_TICKS + 1) * fm.tick, tick_decimals(fm.tick))
        if order.price > intent.price + 1e-9 and under >= fm.tick - 1e-9 and (
                order_score(Slot.LONG_BID, under, target.mid, fm.v) >= cfg.min_low_score):
            return "above the target price"
        if covers + per_share * SIZE_STEP < cfg.keep_ratio * need - 1e-9:
            return "short of the cover needed"
        if (order.price > fm.tick + 1e-9 and covers > LONG_COVER_MAX * need + 1e-9
                and not free_of_budget(order.price, target.mid, fm, position)):
            return "over twice the cover needed"
    elif order.slot is Slot.FAV_ASK and order.price < (
            intent.price - ASK_PRICE_SLACK_TICKS * fm.tick - 1e-9):
        return "below the target price"
    elif order_score(order.slot, order.price, target.mid, fm.v) < (
            cfg.keep_ratio * intent.score - 1e-12):
        return "score below keep ratio"
    if order.slot is not Slot.LONG_BID and not (
            low * size - 1e-9 <= order.size_remaining <= high * size + 1e-9):
        return "size out of range"
    if order.slot is Slot.FAV_BID and order.price > ev_cap(fm, cfg) + 1e-9:
        return "above the EV cap"
    if order.slot is Slot.FAV_BID and order.price * order.size_remaining > (
            high * capital_room(fm, position) + 1e-9):
        return "over the capital"
    if order.slot is Slot.FAV_ASK and order.price < exit_floor(position, fm, cfg) - 1e-9:
        return "below the exit floor"
    if (order.slot is Slot.LONG_BID
            and order.price * order.size_remaining > naked_room(fm, position) + 1e-9
            and not free_of_budget(order.price, target.mid, fm, position)):
        return "over the naked budget"
    return None


def around_kept_bid(target, orders, fm, levels, position, cfg, naked_allowed, fill_mid=None):
    """The target re-planned around the favorite bid reconcile will keep.

    The orders that balance a favorite bid must balance the one resting, not
    a target the hysteresis never places: the net-best price wobbles a tick
    between cycles, and a LONG_BID sized for it swings and gets replaced
    while the favorite bid it covers never moves. Unchanged when no favorite
    bid rests within keep, or the kept one already is the target, or the
    kept one cannot be balanced. target.fav stays the net-best choice.
    """
    intent = next((i for i in target.intents if i.slot is Slot.FAV_BID), None)
    if intent is None:
        return target
    kept = next((o for o in orders if o.slot is Slot.FAV_BID
                 and keep_failure(o, intent, target, fm, position, cfg) is None), None)
    if kept is None or (abs(kept.price - intent.price) < fm.tick / 2
                        and abs(kept.size_remaining - intent.size) < 0.005):
        return target
    around = quotes_at(fm, levels, position, cfg, naked_allowed, kept.price,
                       kept.size_remaining, fill_mid)
    if not any(i.slot is Slot.FAV_BID for i in around.intents):
        return target
    return around._replace(fav=target.fav)


def reconcile(target, live_orders, fm, position, cfg):
    """Cancels and places that turn the resting orders into the targets.

    Per slot, one resting order with no keep_failure survives; every other
    order in the slot is cancelled, and the target is placed if nothing
    survived.
    All cancels come first, so freed balance is available to the places.
    """
    targets = {intent.slot: intent for intent in target.intents}
    cancels, places = [], []
    for slot in Slot:
        orders = [o for o in live_orders if o.slot is slot]
        intent = targets.get(slot)
        kept = None
        if intent is not None:
            kept = next(
                (o for o in orders
                 if keep_failure(o, intent, target, fm, position, cfg) is None),
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
    ("market closed", lambda obs, now: obs.closed),
    ("not accepting orders", lambda obs, now: not obs.accepting_orders),
    ("no longer approved", lambda obs, now: not obs.approved),
    ("expiry buffer reached", lambda obs, now: now >= obs.expires_at),
    ("event closed", lambda obs, now: obs.resolution == "event closed"),
    ("resolution proposed", lambda obs, now: obs.resolution == "resolution proposed"),
)


def drop_ask(asks, min_size):
    """Price at which the best-first asks add up to DROP_DEPTH * min_size, or None.

    Measured at depth rather than at the first min_size level, so a single
    reward-minimum lot coming and going on a thin book cannot move it.
    """
    need = DROP_DEPTH * min_size - 1e-9
    depth = 0.0
    for price, size in asks:
        depth += size
        if depth >= need:
            return price
    return None


def ask_dropped(recent_asks, ask, now, cfg):
    """Whether the drop ask fell by drop_threshold from any point in the window.

    Only the ask counts. A bid leaving looks the same whether it was cancelled
    or sold into, and the farmer re-plans against the bids that remain, while
    a falling offer is sellers repricing the favorite down.
    """
    window = [a for t, a in recent_asks if (now - t).total_seconds() <= cfg.drop_window_s]
    return bool(window) and max(window) - ask >= cfg.drop_threshold - 1e-9


def step_state(state, obs, now, cfg):
    if state.phase is Phase.RETIRED:
        return state
    for reason, applies in RETIRE_CHECKS:
        if applies(obs, now):
            return State(Phase.RETIRED, reason=reason)
    if not obs.rewards_active:
        return State(Phase.UNREWARDED, reason="rewards removed")
    if obs.paused:
        return State(Phase.PAUSED, reason="paused by operator")
    if state.phase in (Phase.PAUSED, Phase.UNREWARDED):
        state = State(Phase.QUOTING)
    if obs.mid is None:
        return state
    if ask_dropped(obs.recent_asks, obs.ask, now, cfg):
        return State(Phase.COOLDOWN, now + timedelta(seconds=cfg.cooldown_s), "ask drop")
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


def pre_fill_mid(samples, seen_at):
    """The book mid of the last (time, book_mid) sample a positions read or
    more before seen_at, the read that saw a fill: the fill came after the
    read before it. None without one."""
    cutoff = seen_at - timedelta(seconds=POSITIONS_S)
    before = [mid for at, mid in samples if mid is not None and at <= cutoff]
    return before[-1] if before else None


def held_fill_mid(fill_mid, book_mid, position, fm):
    """The fill mid while it still prices the exit: None once the farm holds
    less than a reward minimum of the favorite, or the mid is back at it."""
    if fill_mid is None or position.fav_shares < fm.min_size:
        return None
    if book_mid is not None and book_mid >= fill_mid - 1e-9:
        return None
    return fill_mid


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
        fav_shares=round_size_down(max(0.0, held.fav_shares - baseline.fav)),
        long_shares=round_size_down(max(0.0, held.long_shares - baseline.long)),
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


def economics_view(fav):
    """The chosen favorite bid's economics per day, or None without one."""
    if fav.best is None:
        return None
    return {**fav.best._asdict(), "fill_cost_per_day": fav.best.fill_cost_per_day,
            "net_per_day": fav.best.net_per_day, "net_apr": fav.best.net_apr,
            "baseline_per_day": fav.baseline_per_day}


def slot_views(state, orders, target, preview, actions, backoffs, fm, position, cfg, history):
    """Per slot: the resting order, the target, and what this cycle decided.

    preview is what a paused market would place on resume; its intents show
    as the target with decision "preview" and are never acted on. FAV_BID
    also carries the economics of the bid chosen (or previewed) and the fill
    history behind them. LONG_BID carries, for the bid it rests once the
    cycle's actions land, the annual return a fill would give the favorite
    buyer and whether the naked budget leaves it alone (free_of_budget).
    """
    intents = {i.slot: i for i in target.intents}
    previews = {i.slot: i for i in preview.intents} if preview else {}
    cancelled = {i for a in actions if isinstance(a, Cancel) for i in a.order_ids}
    placing = {a.intent.slot for a in actions if isinstance(a, Place)}
    views = {}
    for slot in Slot:
        resting = [o for o in orders if o.slot is slot]
        kept = next((o for o in resting if o.id not in cancelled), None)
        shown = kept or (resting[0] if resting else None)
        intent = intents.get(slot)
        if intent is None and slot in previews and not resting:
            decision, reason = "preview", "would place on resume"
        elif intent is None:
            why = "no target" if state.phase is Phase.QUOTING else str(state)
            if slot is Slot.FAV_BID and state.phase is Phase.QUOTING and target.fav.reason:
                why = target.fav.reason
            decision, reason = ("cancel" if resting else "none"), why
        elif kept is not None:
            decision, reason = "hold", ""
        elif slot in placing and resting:
            decision = "replace"
            reason = keep_failure(resting[0], intent, target, fm, position, cfg)
        elif slot in placing:
            decision, reason = "place", ""
        else:
            decision = "backoff"
            reason = f"rejected; retry after {stamp(backoffs[slot].until)}"
        shown_intent = intent or previews.get(slot)
        views[slot.name] = {
            "order": shown and {"id": shown.id, "price": shown.price,
                                "size": shown.size_remaining},
            "target": shown_intent and {"price": shown_intent.price, "size": shown_intent.size,
                                        "score": shown_intent.score},
            "live_score": (order_score(slot, shown.price, target.mid, fm.v)
                           if shown and target.mid is not None else None),
            "decision": decision,
            "reason": reason,
        }
    fav = (preview or target).fav
    views[Slot.FAV_BID.name].update(
        economics=economics_view(fav), fav_reason=fav.reason,
        history=fill_cost.history_to_json(history) if history else None)
    low = views[Slot.LONG_BID.name]
    rests = farm_db.resting_order(low)
    offers = fill_return(rests[0], fm.zones.years_left) if rests else None
    low.update(
        fill_return=offers if offers is not None and math.isfinite(offers) else None,
        budget_free=bool(rests) and free_of_budget(rests[0], target.mid, fm, position))
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


def mid_sample(state):
    """The farm_mids row of a market's farm_state snapshot."""
    def price(slot):
        order = farm_db.resting_order(state.slots[slot.name])
        return order and order[0]

    return farm_db.MidSample(state.condition_id, state.updated_at, state.mid,
                             state.book_mid, price(Slot.FAV_BID), price(Slot.LONG_BID))


def naked_used(position, orders):
    """USD of the naked leg: longshot shares held at cost plus resting longshot bids."""
    resting = sum(o.price * o.size_remaining for o in orders if o.slot is Slot.LONG_BID)
    return position.long_shares * position.long_avg + resting


# ---------------------------------------------------------------------------
# External data -> domain types
# ---------------------------------------------------------------------------

def parse_market_info(entry):
    return MarketInfo(
        token_ids=tuple(str(t) for t in json.loads(entry.get("clobTokenIds") or "[]") if t),
        v=(as_float(entry.get("rewardsMaxSpread")) or 0) / 100,
        min_size=as_float(entry.get("rewardsMinSize")) or 0,
        tick=as_float(entry.get("orderPriceMinTickSize")) or 0.01,
        neg_risk=bool(entry.get("negRisk")),
        accepting_orders=bool(entry.get("acceptingOrders")),
        closed=bool(entry.get("closed")),
    )


def rewards_active(info, condition_id, total_rates):
    """Whether the total listing, which leaves out markets paid only by
    sponsors, still pays the market.

    The market record keeps its native rates after its rewards end, so only
    the listing decides. total_rates is None until the first fetch of the slow
    listing finishes, and a market is not pulled on a rate nobody has read yet.
    """
    if info.v <= 0:
        return False
    return total_rates is None or total_rates.get(condition_id, 0) > 0


def farm_market(row, info, cfg, now):
    """An approved DB row plus its market record as a FarmMarket, or Skip,
    with the time its end date leaves at now."""
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
        zones=Zones(fill_cost.years_until(end_date, now)),
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
    price = as_float(entry.get("price"))
    remaining = ((as_float(entry.get("original_size")) or 0)
                 - (as_float(entry.get("size_matched")) or 0))
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

def fetch_market_infos(http, condition_ids, timeout=20):
    """{condition_id: MarketInfo} for every market Gamma returns.

    Gamma leaves closed markets out unless asked for closed markets alone, so
    each chunk is read twice, open and closed.
    """
    infos = {}
    for chunk in chunks(list(condition_ids), GAMMA_REQUEST_LIMIT):
        for closed in (False, True):
            for entry in request_markets(http, chunk, closed, timeout):
                if isinstance(entry, dict) and entry.get("conditionId"):
                    infos[str(entry["conditionId"])] = parse_market_info(entry)
    return infos


def fetch_holdings(http, user, timeout=30):
    """{token_id: (shares, avg_price)} for every position the user holds."""
    holdings, offset = {}, 0
    while True:
        page = get_with_retry(
            http, f"{DATA_API_HOST}/positions",
            {"user": user, "limit": POSITIONS_PAGE, "offset": offset, "sizeThreshold": 0},
            timeout,
        ).json() or []
        for p in page:
            size, avg = as_float(p.get("size")), as_float(p.get("avgPrice"))
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
    parsed = {str(k): as_float(v) for k, v in resp.items()}
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
        earned = as_float(e.get("earnings")) if isinstance(e, dict) else None
        if earned is None or not e.get("condition_id"):
            skipped += 1
            continue
        rate = as_float(e.get("asset_rate"))
        cid = str(e["condition_id"])
        totals[cid] = totals.get(cid, 0.0) + earned * (1.0 if rate is None else rate)
    return totals, skipped


def row_baseline(row):
    if row["baseline_fav"] is None or row["baseline_long"] is None:
        return None
    return Baseline(row["baseline_fav"], row["baseline_long"])


class TotalRates:
    """The total daily rate of every market with native rewards, sponsored pools
    included.

    The listing that has them takes about ten seconds to read, so a thread
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


class Want(NamedTuple):
    """A market whose read the farmer wants, and the floor to read it at."""

    fav_token_id: str
    floor: float
    label: str


class Histories:
    """Each active market's fill history and buy tape, read on a thread of
    its own: a read takes up to nine requests, and the main loop must not wait
    on them.

    wanted ({condition_id: Want}) is written by the main loop alone, read
    ({condition_id: Read}) by the reader alone once Farmer.start has seeded
    it; each is replaced whole, never mutated. A market is read once wanted,
    even if seeded, again every HISTORY_S, and at once when what is wanted of
    it changes. A failed read keeps the last one and retries after
    HISTORY_RETRY_S.
    """

    def __init__(self, stop, fetch):
        self.wanted = {}
        self.read = {}
        self._due = {}
        self._stop = stop
        self._fetch = fetch

    def refresh(self, http, now):
        """Read every wanted market that is due, one at a time, publishing
        read after each, and forget the markets no longer wanted."""
        wanted = self.wanted
        self.read = {cid: h for cid, h in self.read.items() if cid in wanted}
        self._due = {cid: due for cid, due in self._due.items() if cid in wanted}
        for cid, want in wanted.items():
            if self._stop.is_set():
                return
            due, read_for = self._due.get(cid, (now, want))
            if now < due and read_for == want:
                continue
            try:
                read = self._fetch(http, cid, want.fav_token_id, now, want.floor)
            except Exception as e:
                log(f"Warning: fill history of {want.label} failed ({e})")
                self._due[cid] = now + timedelta(seconds=HISTORY_RETRY_S), want
                continue
            self.read = {**self.read, cid: read}
            self._due[cid] = now + timedelta(seconds=HISTORY_S), want

    def run(self):
        with httpx.Client() as http:
            while not self._stop.is_set():
                self.refresh(http, datetime.now(timezone.utc))
                self._stop.wait(HISTORY_TICK_S)


class MarketInfos:
    """Each wanted market's record, read on a thread of its own: the records
    come in batched Gamma requests, and the main loop must not wait on them.

    wanted (a frozenset of condition ids) is written by the main loop alone,
    read ({condition_id: MarketInfo}) by the reader alone; each is replaced
    whole, never mutated. Every wanted market is read in one fetch every
    MARKET_INFO_S, and at once when a market joins wanted. A failed fetch
    keeps the last records and retries after MARKET_INFO_RETRY_S; a market a
    fetch leaves out keeps its last record.
    """

    def __init__(self, stop, fetch):
        self.wanted = frozenset()
        self.read = {}
        self._due = None
        self._read_for = frozenset()
        self._stop = stop
        self._fetch = fetch

    def refresh(self, http, now):
        """Read every wanted market if due or one joined, and forget the
        markets no longer wanted."""
        wanted = self.wanted
        self.read = {cid: info for cid, info in self.read.items() if cid in wanted}
        self._read_for &= wanted
        fresh = self._due is not None and now < self._due
        if not wanted or (fresh and wanted <= self._read_for):
            return
        self._read_for = wanted
        try:
            infos = self._fetch(http, wanted)
        except Exception as e:
            log(f"Warning: market data fetch failed ({e})")
            self._due = now + timedelta(seconds=MARKET_INFO_RETRY_S)
            return
        missing = wanted - infos.keys()
        if missing:
            log(f"Warning: Gamma left out {len(missing)} wanted markets "
                f"({', '.join(sorted(missing))}); keeping their last data")
        self.read = {**self.read, **{cid: infos[cid] for cid in wanted - missing}}
        self._due = now + timedelta(seconds=MARKET_INFO_S)

    def run(self):
        with httpx.Client() as http:
            while not self._stop.is_set():
                self.refresh(http, datetime.now(timezone.utc))
                self._stop.wait(MARKET_INFO_TICK_S)


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
        self.recent_asks = deque()
        # The midpoint over the dwell (watch_mid), None while unwatched.
        self.mid_watch = None
        self.naked_paused_until = None
        self.fill_mid = None
        self.held = None
        self.position = None
        self.orders = []
        self.sim_orders = []
        self.snapshot = None
        self.mid_sampled_at = None
        self.book_sampled_at = None
        self.last_summary = None


class Plan(NamedTuple):
    """One market's cycle, planned and reported, waiting to be executed."""
    managed: Managed
    levels: Levels
    book_mid: Optional[float]
    target: Target
    preview: Optional[Target]
    orders: list
    actions: list


class Farmer:
    def __init__(self, args, cfg, client=None):
        self.args = args
        self.base_cfg = self.cfg = cfg
        self.client = client
        self.live = client is not None
        self.http = httpx.Client()
        self.db = farm_db.open_db(args.db)
        self.markets = {}
        self.skips = {}
        self.setting_errors = {}
        self.events = []
        self.mids = []
        self.book_samples = []
        self.scoring = {}
        self.positions_at = self.rewards_at = self.resolutions_at = 0.0
        self.resolutions = {}
        self.started_at = self.last_cycle_at = datetime.now(timezone.utc)
        self.sim_ids = 0
        self.stop = threading.Event()
        self.stop_reason = None
        self.keep_orders = False
        self.total_rates = TotalRates(self.stop)
        self.infos = MarketInfos(self.stop, fetch_market_infos)
        self.histories = Histories(self.stop, fetch_read)
        self.books = farm_books.BookRecorder(farm_books.books_path(args.db), self.stop, log)
        self.saved_tapes = {}
        self.prior = fill_cost.DEFAULT_PRIOR
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
        """Publish the queued events and the status; farm_state and the buy
        tapes read since the last flush too unless stopping."""
        states = None
        if stopped_at is None:
            states = [m.snapshot for m in self.markets.values() if m.snapshot]
            self.save_tapes()
            self.books.put(self.book_samples)
            self.book_samples = []
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

    def save_tapes(self):
        """Save each tape the reader replaced since the last save; a tape
        changes only when a read lands, hourly, so most flushes save none."""
        reads = self.histories.read
        for cid, read in reads.items():
            if read.tape is not None and read.tape is not self.saved_tapes.get(cid):
                farm_db.save_buys(self.db, cid, read.tape.read_at,
                                  tape_to_json(read.tape, TAPE_ROWS_FARMER))
        self.saved_tapes = {cid: read.tape for cid, read in reads.items()}

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
        # The first records are read here, before the reader thread starts, so
        # the first cycle (all of a --once run) manages the markets.
        self.infos.refresh(self.http, now)
        self.reload(now)
        self.seed_histories(now)
        self.seed_watches(now)
        self.refresh_positions(now)
        self.positions_at = time.monotonic() + POSITIONS_S
        if not self.markets:
            log(f"No approved markets in {self.args.db}; waiting for approvals")
        if self.live:
            self.check_collateral()
        self.flush()

    def seed_watches(self, now):
        """Take each market's watch of its midpoint from its farm_mids samples
        of the last run, so a restart keeps the bids that run sized without
        the budget where the midpoint held, and puts back under the budget
        the ones where it did not."""
        since = now - timedelta(seconds=FREE_DWELL_S + FREE_SEED_GAP_S)
        for managed in self.markets.values():
            samples = farm_db.read_mids(self.db, managed.fm.condition_id, since)
            managed.mid_watch = seeded_watch([(s.at, s.book_mid) for s in samples], now)

    def seed_histories(self, now):
        """Take each wanted market's history from its farm_state row, and its
        buy tape from fav_buys, while each is fresh, so the first cycle keeps
        the favorite bids the last run left resting instead of cancelling them
        until the reader gets round to it. A history seeds without a tape."""

        def fresh(read):
            return read is not None and now - read.read_at < timedelta(seconds=HISTORY_S)

        stored, tapes = farm_db.read_farm_states(self.db), farm_db.read_buys(self.db)
        histories = {cid: fill_cost.history_from_json(
                         stored[cid].slots.get(Slot.FAV_BID.name, {}).get("history"))
                     for cid in self.histories.wanted if cid in stored}
        reads = {}
        for cid, history in histories.items():
            if fresh(history):
                tape = tape_from_json(tapes.get(cid))
                reads[cid] = Read(history, tape if fresh(tape) else None)
        self.histories.read = reads
        self.saved_tapes = {cid: read.tape for cid, read in reads.items()}

    def history(self, fm):
        """The market's last fill history read, or None."""
        read = self.histories.read.get(fm.condition_id)
        return read and read.history

    def shutdown(self, reason, cancel):
        """Stop the threads and publish the stop, cancelling the live orders if asked."""
        if self.live and cancel:
            self.cancel_managed()
        self.stop.set()
        kept = self.live and not cancel and not self.args.heartbeat
        self.emit(EventKind.STOP, f"Stopped: {reason}" + ("; orders left resting" if kept else ""),
                  data={"reason": reason, "orders_kept": kept})
        try:
            self.flush(stopped_at=datetime.now(timezone.utc))
        except Exception as e:
            log(f"Warning: writing the stop to {self.args.db} failed ({e})")
        self.db.close()
        self.http.close()
        self.books.join(BOOKS_JOIN_S)

    def read_controls(self, now):
        """Commands, settings, the fill prior, and approved markets, re-read
        from the DB every cycle."""
        for command in farm_db.pending_commands(self.db):
            if command.kind == farm_db.CommandKind.STOP.value:
                if self.stop_reason is None:
                    self.stop_reason = f"stop command {command.id}"
                    self.keep_orders = bool((command.payload or {}).get("keep_orders"))
                    if self.keep_orders and self.live and self.args.heartbeat:
                        self.emit(EventKind.ALERT,
                                  "ALERT: keeping orders, but the heartbeat stops with the "
                                  "farmer and Polymarket cancels them within about 10s")
                result = "stopping, keeping orders" if self.keep_orders else "stopping"
            else:
                result = f"unknown command {command.kind!r}"
            farm_db.consume_command(self.db, command.id, result, now)
            log(f"Command {command.id} {command.kind}: {result}")
        self.apply_settings(*farm_db.load_settings(self.db))
        self.prior = load_prior(self.db)
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

    def reload(self, now):
        rows = farm_db.load_approved(self.db)
        added = []
        for cid, managed in list(self.markets.items()):
            was_approved, managed.approved = managed.approved, cid in rows
            if managed.state.phase is Phase.RETIRED and managed.approved and not was_approved:
                # Re-approved after retiring: start over as if newly approved.
                del self.markets[cid]
        self.infos.wanted = frozenset(
            cid for cid in rows
            if cid not in self.markets or self.markets[cid].state.phase is not Phase.RETIRED
        )
        for cid, row in rows.items():
            managed = self.markets.get(cid)
            if managed is not None and managed.state.phase is Phase.RETIRED:
                continue
            label = row["slug"] or cid
            info = self.infos.read.get(cid)
            if info is None:
                continue
            fm = farm_market(row, info, self.cfg, now)
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
                added.append(fm)
            else:
                managed.fm, managed.info = fm, info
        if self.live and added:
            self.prewarm(added)
        # Quoting or not, so a market that starts quoting has its history already.
        self.histories.wanted = {m.fm.condition_id: Want(m.fm.fav_token_id, self.cfg.min_fav,
                                                         m.fm.label)
                                 for m in self.active()}

    def prewarm(self, fms):
        """Read each market's CLOB info, which the client caches per token.

        Best effort: without it the first sign of each token after a
        restart waits on the client's own tick size lookup.
        """
        def fetch(fm):
            try:
                self.client.get_clob_market_info(fm.condition_id)
            except Exception as e:
                log(f"Warning: market info for {fm.label} unavailable ({e})")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(fetch, fms))

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
                if current.fav_shares >= fm.min_size:
                    managed.fill_mid = self.last_fill_mid(fm)
                if held != Position() or baseline != Baseline(0.0, 0.0):
                    log(f"{fm.label}: holding fav {held.fav_shares:g} @ "
                        f"{cents(held.fav_avg, fm.tick)}, long {held.long_shares:g} @ "
                        f"{cents(held.long_avg, fm.tick)}; baseline fav {baseline.fav:g}, "
                        f"long {baseline.long:g} (never sold); farming fav "
                        f"{current.fav_shares:g}, long {current.long_shares:g}")
                continue
            for slot, shares in fill_events(previous, current):
                if slot is Slot.FAV_BID:
                    mids = [m for m in (managed.fill_mid, self.fill_mid_seen(fm, now)) if m]
                    managed.fill_mid = max(mids, default=None)
                self.record_fill(managed, slot, shares)
                if slot is Slot.LONG_BID:
                    managed.naked_paused_until = now + timedelta(
                        seconds=self.cfg.naked_cooldown_s
                    )
                    self.emit(EventKind.ALERT,
                              f"ALERT {fm.label}: naked longshot fill; LONG_BID paused "
                              f"until {managed.naked_paused_until.astimezone():%H:%M:%S}",
                              fm.condition_id, slot)

    def fill_mid_seen(self, fm, seen_at):
        """The pre_fill_mid of a fill seen at seen_at, from farm_mids."""
        since = seen_at - timedelta(seconds=FILL_MID_LOOKBACK_S)
        samples = farm_db.read_mids(self.db, fm.condition_id, since)
        return pre_fill_mid([(s.at, s.book_mid) for s in samples], seen_at)

    def last_fill_mid(self, fm):
        """The fill mid of inventory held when the farmer starts: the higher of
        the mid before the last favorite fill on record and the fill mid that
        fill recorded, which weighs the earlier fills of the same inventory."""
        fills = farm_db.read_events(self.db, condition_id=fm.condition_id,
                                    kinds=(EventKind.FILL,), limit=100)
        last = next((e for e in fills if e.slot == Slot.FAV_BID.name), None)
        if last is None:
            return None
        mids = [m for m in ((last.data or {}).get("fill_mid"), self.fill_mid_seen(fm, last.at))
                if m]
        return max(mids, default=None)

    def record_fill(self, managed, slot, shares):
        """A fill inferred from a position change, priced at the slot's resting order.

        The data API gives no fill price, so the price is that of the order
        resting in the slot at the last cycle, if there was one.
        """
        fm = managed.fm
        resting = next((o for o in managed.orders if o.slot is slot), None)
        price = resting.price if resting else None
        at = f" @ {cents(price, fm.tick)}" if resting else ""
        data = {"price_source": "resting order at the last cycle" if resting
                else "unknown: no order rested in the slot",
                "order_id": resting.id if resting else None}
        if slot is Slot.FAV_BID:
            data["fill_mid"] = managed.fill_mid
        self.emit(EventKind.FILL, f"FILL {fm.label} {slot.name} {shares:g} shares{at}",
                  fm.condition_id, slot, price, shares, data=data)

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

    def refresh_resolutions(self):
        """Gamma's view of whether each managed market is already decided.

        One batched read a minute. A failed read keeps the last answer, so a
        Gamma outage neither retires markets nor forgets a known signal.
        """
        ids = [m.fm.condition_id for m in self.active()]
        if not ids:
            return
        info = fetch_gamma(self.http, ids)
        if not info:
            log("Warning: Gamma resolution check returned nothing; keeping the last answer")
            return
        for cid in ids:
            if cid in info:
                self.resolutions[cid] = resolution_signal(info[cid])

    def market_of_token(self):
        return {token_id: cid for cid, m in self.markets.items()
                for token_id in (m.fm.fav_token_id, m.fm.long_token_id)}

    def open_orders(self):
        """{condition_id: [LiveOrder]} for every managed market; one listing live."""
        if not self.live:
            return {cid: list(m.sim_orders) for cid, m in self.markets.items()}
        orders = {cid: [] for cid in self.markets}
        market_of = self.market_of_token()
        for entry in self.client.get_open_orders():
            cid = market_of.get(str(entry.get("asset_id") or ""))
            order = parse_open_order(entry, self.markets[cid].fm) if cid else None
            if order is not None:
                orders[cid].append(order)
        return orders

    def cycle(self):
        now = self.last_cycle_at = datetime.now(timezone.utc)
        self.read_controls(now)
        if self.stop_reason is not None:
            return
        unpositioned = any(m.position is None for m in self.active())
        if unpositioned or time.monotonic() >= self.positions_at:
            self.refresh_positions(now)
            self.positions_at = time.monotonic() + POSITIONS_S
        if time.monotonic() >= self.resolutions_at:
            self.refresh_resolutions()
            self.resolutions_at = time.monotonic() + RESOLUTION_S
        if self.live and time.monotonic() >= self.rewards_at:
            self.refresh_rewards(now)
            self.rewards_at = time.monotonic() + REWARDS_S

        try:
            orders = self.open_orders()
        except Exception as e:
            # Planning on no orders would duplicate the ones actually resting.
            log(f"Warning: open orders unavailable ({e}); planning no market")
            self.flush()
            return
        active = self.active()
        books = fetch_levels(self.http, [m.fm.fav_token_id for m in active])
        plans = []
        for managed in active:
            try:
                plan = self.step_market(managed, books.get(managed.fm.fav_token_id),
                                        orders[managed.fm.condition_id], now)
            except Exception as e:
                log(f"Error on {managed.fm.label}: {e}")
                continue
            if plan is not None:
                plans.append(plan)
        try:
            placed = self.execute(plans)
        except Exception as e:
            log(f"Error executing the cycle's orders: {e}")
            placed = {}
        for plan in plans:
            try:
                self.settle(plan, placed, now)
            except Exception as e:
                log(f"Error on {plan.managed.fm.label}: {e}")
        self.flush()

    def step_market(self, managed, levels, orders, now):
        """Advance the lifecycle and plan the orders; None when there is no plan."""
        fm = managed.fm
        if levels is None:
            log(f"{fm.label}: no book this cycle")
            return None
        if managed.position is None:
            log(f"{fm.label}: no position yet; not quoting")
            return None
        if levels.tick:
            fm = managed.fm = fm._replace(tick=levels.tick)

        managed.orders = orders
        book = strip_own(levels, orders, fm) if self.live else levels
        book_mid = adjusted_mid(book.bids, book.asks, fm.min_size)
        book_ask = drop_ask(book.asks, fm.min_size)
        managed.mid_watch = watch_mid(managed.mid_watch, book_mid, now)
        fm = managed.fm = fm._replace(
            zones=fm.zones._replace(steady_mid=steady_mid(managed.mid_watch, now)))
        obs = Observation(
            mid=book_mid,
            ask=book_ask,
            recent_asks=tuple(managed.recent_asks),
            rewards_active=rewards_active(managed.info, fm.condition_id,
                                          self.total_rates.rates),
            accepting_orders=managed.info.accepting_orders,
            closed=managed.info.closed,
            approved=managed.approved,
            expires_at=fm.expires_at,
            paused=fm.paused,
            naked_paused=fm.naked_paused,
            naked_cooldown_until=managed.naked_paused_until,
            resolution=self.resolutions.get(fm.condition_id),
        )
        previous, managed.state = managed.state, step_state(managed.state, obs, now, self.cfg)
        if book_ask is not None:
            managed.recent_asks.append((now, book_ask))
        while managed.recent_asks and (
            now - managed.recent_asks[0][0]
        ).total_seconds() > self.cfg.drop_window_s:
            managed.recent_asks.popleft()
        if managed.state != previous:
            state = managed.state
            self.emit(EventKind.PHASE, f"{fm.label}: {previous} -> {state}", fm.condition_id,
                      data={"from": previous.phase.value, "to": state.phase.value,
                            "reason": state.reason, "until": stamp(state.until)})

        target, preview = Target(book_mid, []), None
        previewing = (managed.state.phase is Phase.PAUSED
                      and step_state(managed.state, obs._replace(paused=False), now,
                                     self.cfg).phase is Phase.QUOTING)
        managed.fill_mid = held_fill_mid(managed.fill_mid, book_mid, managed.position, fm)
        if managed.state.phase is Phase.QUOTING or previewing:
            planned = target_orders(
                fm, book, managed.position, self.cfg, naked_allowed(obs, now),
                market_outlook(fm, self.total_rates.rates, self.history(fm), self.prior,
                               self.cfg, now),
                fill_mid=managed.fill_mid, resting=orders,
            )
            if previewing:
                preview = planned
            else:
                target = around_kept_bid(planned, orders, fm, book, managed.position,
                                         self.cfg, naked_allowed(obs, now), managed.fill_mid)
        actions = without_backed_off(
            reconcile(target, orders, fm, managed.position, self.cfg),
            managed.backoffs,
            now,
        )
        self.report(managed, target, orders, actions)
        return Plan(managed, levels, book_mid, target, preview, orders, actions)

    def settle(self, plan, placed, now):
        """Back off rejected slots, publish the market's view, and retire it if due."""
        managed = plan.managed
        fm = managed.fm
        for (condition_id, slot), accepted in placed.items():
            if condition_id != fm.condition_id:
                continue
            if accepted:
                managed.backoffs.pop(slot, None)
                continue
            backoff = managed.backoffs[slot] = next_backoff(managed.backoffs.get(slot), now)
            self.emit(EventKind.BACKOFF,
                      f"  {slot.name} on {fm.label}: backing off {backoff.delay_s:g}s",
                      fm.condition_id, slot,
                      data={"until": stamp(backoff.until), "attempts": backoff.attempts})
        managed.snapshot = self.market_state(plan, now)
        if (managed.mid_sampled_at is None
                or (now - managed.mid_sampled_at).total_seconds() >= MID_SAMPLE_S):
            managed.mid_sampled_at = now
            self.mids.append(mid_sample(managed.snapshot))
        if managed.state.phase is Phase.QUOTING and plan.book_mid is not None and (
                managed.book_sampled_at is None
                or (now - managed.book_sampled_at).total_seconds() >= farm_books.BOOK_SAMPLE_S):
            managed.book_sampled_at = now
            self.book_samples.append(farm_books.sample(
                fm.condition_id, now, plan.book_mid, plan.levels.bids, plan.levels.asks,
                [(o.slot.name, o.price, o.size_remaining) for o in plan.orders]))
        if managed.state.phase is Phase.RETIRED:
            self.retire(managed)

    def market_state(self, plan, now):
        managed, levels, book_mid, target, preview, orders, actions = plan
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
            slots=slot_views(state, orders, target, preview, actions, managed.backoffs, fm,
                             managed.position, self.cfg, self.history(fm)),
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
        best = target.fav.best
        fav = (f"bid {cents(best.price, fm.tick)} net {signed_usd(best.net_per_day)}/day"
               if best else target.fav.reason or "no bid")
        summary = (f"{fm.label}: {managed.state.phase.value} mid {mid} "
                   f"Q1 {q1:.1f} Q2 {q2:.1f} | {fav} | hold {held} cancel {len(cancelled)} "
                   f"place {placed}")
        if summary != managed.last_summary:
            managed.last_summary = summary
            log(summary)
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

    def execute(self, plans):
        """Carry out every plan's actions, all cancels before any place.

        Returns {(condition_id, slot): whether its place was accepted}.
        """
        placed = {(p.managed.fm.condition_id, a.intent.slot): False
                  for p in plans for a in p.actions if isinstance(a, Place)}
        if not self.live:
            for p in plans:
                self.simulate(p.managed, p.actions)
            return dict.fromkeys(placed, True)
        self.cancel_planned(plans)
        for fm, i in self.post_planned(plans):
            placed[(fm.condition_id, i.slot)] = True
        return placed

    def simulate(self, managed, actions):
        """Dry run: carry the actions out on the market's simulated orders."""
        cancelled = {i for a in actions if isinstance(a, Cancel) for i in a.order_ids}
        kept = [o for o in managed.sim_orders if o.id not in cancelled]
        for a in actions:
            if isinstance(a, Place):
                self.sim_ids += 1
                i = a.intent
                kept.append(LiveOrder(f"sim-{self.sim_ids}", i.slot, i.token_id,
                                      i.side, i.price, i.size))
        managed.sim_orders = kept

    def cancel_planned(self, plans):
        """Every plan's cancels in one call."""
        fm_of = {order_id: p.managed.fm for p in plans for a in p.actions
                 if isinstance(a, Cancel) for order_id in a.order_ids}
        if not fm_of:
            return
        try:
            resp = self.client.cancel_orders(list(fm_of))
        except Exception as e:
            for fm in {fm.condition_id: fm for fm in fm_of.values()}.values():
                self.emit(EventKind.ALERT, f"  cancel failed on {fm.label}: {e}",
                          fm.condition_id)
            return
        failed = (resp or {}).get("not_canceled") or {}
        for order_id, why in dict(failed).items():
            fm = fm_of.get(order_id)
            self.emit(EventKind.ALERT, f"  cancel {order_id} failed: {why}",
                      fm.condition_id if fm else None, data={"order_id": order_id})

    def post_planned(self, plans):
        """Sign every plan's places and post them in shared batches.

        Returns the (fm, intent) of each place the CLOB accepted.
        """
        signed = []
        for p in plans:
            fm = p.managed.fm
            self.refresh_tick(fm, {a.intent.token_id for a in p.actions if isinstance(a, Place)})
            options = PartialCreateOrderOptions(tick_size=f"{fm.tick:g}", neg_risk=fm.neg_risk)
            expiration = int(fm.expires_at.timestamp())
            for a in p.actions:
                if isinstance(a, Place):
                    i = a.intent if a.intent.side == BUY else self.sellable(fm, a.intent)
                    try:
                        order = self.client.create_order(
                            OrderArgsV2(token_id=i.token_id, price=i.price, size=i.size,
                                        side=i.side, expiration=expiration),
                            options,
                        )
                        signed.append((fm, i, order))
                    except Exception as e:
                        self.emit(EventKind.ALERT,
                                  f"  sign {i.slot.name} failed on {fm.label}: {e}",
                                  fm.condition_id, i.slot)
        accepted = []
        for chunk in chunks(signed, POST_BATCH_LIMIT):
            try:
                responses = self.client.post_orders(
                    [PostOrdersV2Args(order=o, orderType=OrderType.GTD) for _, _, o in chunk],
                    post_only=True,
                )
            except Exception as e:
                by_market = {}
                for fm, _, _ in chunk:
                    by_market.setdefault(fm.condition_id, []).append(fm)
                for fms in by_market.values():
                    self.emit(EventKind.ALERT,
                              f"  post of {len(fms)} orders failed on {fms[0].label}: {e}",
                              fms[0].condition_id)
                continue
            for (fm, i, _), resp in zip(chunk, responses if isinstance(responses, list) else []):
                if not isinstance(resp, dict) or not resp.get("success") or resp.get("errorMsg"):
                    self.emit(EventKind.REJECT,
                              f"  {i.slot.name} {cents(i.price, fm.tick)} rejected on "
                              f"{fm.label}: {resp}",
                              fm.condition_id, i.slot, i.price, i.size, i.score,
                              data={"response": resp if isinstance(resp, dict) else str(resp)})
                else:
                    accepted.append((fm, i))
        return accepted

    def sellable(self, fm, intent):
        """The ask sized to the shares the CLOB holds of its token: the data
        API books each fill to the cent, so its position can overstate them.
        As planned when the balance cannot be read."""
        try:
            resp = self.client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=intent.token_id))
            held = round_size_down(int(resp["balance"]) / 1e6)
        except Exception as e:
            log(f"Warning: {intent.slot.name} balance on {fm.label} unavailable ({e}); "
                f"placing as planned")
            return intent
        return intent._replace(size=min(intent.size, held))

    def refresh_tick(self, fm, token_ids):
        """Re-read the market's CLOB info when the client's cached tick for a
        token about to be signed is coarser than the book's.

        The client caches a token's tick for the life of the process and
        refuses to sign finer, and Polymarket narrows a market's tick past
        0.96. Best effort: on failure the signing fails and alerts.
        """
        try:
            if any(float(self.client.get_tick_size(t)) > fm.tick for t in token_ids):
                self.client.get_clob_market_info(fm.condition_id)
        except Exception as e:
            log(f"Warning: tick size of {fm.label} unavailable ({e})")

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
        planned = sum(capital_room(m.fm, m.position)
                      for m in self.active() if m.position is not None)
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
        """Cancel every open order on a managed market: one listing, one cancel.

        Orders left resting while the farmer is down are what stopping
        guards against, so any failure falls back to a cancel per market.
        """
        market_of = self.market_of_token()
        try:
            entries = [e for e in self.client.get_open_orders()
                       if str(e.get("asset_id") or "") in market_of]
            resp = self.client.cancel_orders([e["id"] for e in entries]) if entries else None
            failed = dict((resp or {}).get("not_canceled") or {})
        except Exception as e:
            log(f"Warning: bulk cancel failed ({e}); cancelling market by market")
            self.cancel_by_market()
            return
        sent = {}
        for e in entries:
            sent.setdefault(market_of[str(e["asset_id"])], []).append(e["id"])
        for cid, ids in sent.items():
            label = self.markets[cid].fm.label
            n = sum(order_id not in failed for order_id in ids)
            self.emit(EventKind.CANCEL, f"Cancelled {n} orders on {label}", cid)
            for order_id in ids:
                if order_id in failed:
                    self.emit(EventKind.ALERT,
                              f"Warning: cancelling {order_id} on {label} failed "
                              f"({failed[order_id]})", cid, data={"order_id": order_id})

    def cancel_by_market(self):
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


def run(args, client=None):
    """Farm until stopped. client is the authenticated CLOB client of a live run."""
    farmer = Farmer(args, config_from(args), client)
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

    reason, cancel = "--once finished", False
    try:
        farmer.start()
        # Only now: start() reads the first records and seeds the histories
        # itself, and only one thread may write each.
        threading.Thread(target=farmer.infos.run, daemon=True).start()
        threading.Thread(target=farmer.histories.run, daemon=True).start()
        farmer.books.start()
        while True:
            started = time.monotonic()
            try:
                farmer.cycle()
            except Exception as e:
                log(f"Cycle failed: {e}")
            if farmer.stop_reason is not None:
                reason, cancel = farmer.stop_reason, not farmer.keep_orders
                log("Stopping")
                break
            if args.once:
                break
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        reason, cancel = "interrupted", not args.keep_orders
        log("Stopping")
    except BaseException as e:
        reason, cancel = f"crashed: {e!r}", True
        raise
    finally:
        farmer.shutdown(reason, cancel)


def config_from(args):
    return FarmConfig(
        capital=args.capital,
        max_lead=args.max_lead,
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
        hurdle_rate=args.hurdle_rate,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m farm.reward_farmer",
        description=(
            "Farm Polymarket liquidity rewards on the markets approved in the "
            "screener's SQLite file. Dry run unless --live."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m farm.reward_farmer                          # dry run, forever
  python -m farm.reward_farmer --db scratch.db --once   # one dry-run cycle
  python -m farm.reward_farmer --live --capital 300     # real orders
        """,
    )
    parser.add_argument("--db", help=f"SQLite file (default: farm.db {data_dir.LOCATION_HELP})")
    parser.add_argument("--live", action="store_true",
                        help="Sign and post real orders (default: dry run)")
    parser.add_argument("--key-stdin", action="store_true",
                        help="Live only: read the wallet key from one line of standard input, "
                             "not from the environment or Bitwarden")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    stopping = parser.add_mutually_exclusive_group()
    stopping.add_argument(
        "--keep-orders", action="store_true",
        help=(
            "Live only: Ctrl+C and SIGTERM leave the managed orders resting instead "
            "of cancelling them. They rest unmanaged until the farmer starts again: "
            "no cooldowns, retirement, or fill handling, and they expire only at "
            "their GTD date. A crash still cancels them. Not with --heartbeat, which "
            "cancels them 10s after the farmer stops."
        ),
    )
    stopping.add_argument(
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
        ("--capital", float, 500.0, "Most USD a market's favorite bid may be, favorite "
                                    "inventory at cost included. The farmer rests the "
                                    "size at or under it that nets the most"),
        ("--max-lead", float, 0.005, "Most the favorite bid may sit above the best bid "
                                     "already in the book. 0 joins it"),
        ("--ask-offset", float, 0.01, "Favorite ask distance above the midpoint"),
        ("--min-capture", float, 0.005, "Favorite ask minimum above average cost"),
        ("--ev-margin", float, 0.02, "Favorite bid maximum is p_true minus this"),
        ("--min-fav", float, 0.90, "Stop quoting below this favorite midpoint"),
        ("--min-low-score", float, 0.10, "The longshot bid never sits above the cheapest "
                                         "price scoring this per share"),
        ("--keep-ratio", float, 0.8, "Keep an order scoring at least this share of its "
                                     "target, a favorite bid earning this share of the "
                                     "best net"),
        ("--drop-threshold", float, 0.03, "Fall in the favorite's best ask that triggers "
                                          "a cooldown"),
        ("--drop-window", float, 60.0, "Seconds over which the ask's fall is measured"),
        ("--cooldown", float, 600.0, "Seconds without quotes after an ask drop"),
        ("--naked-cooldown", float, 3600.0, "Seconds without a longshot bid after "
                                            "a longshot fill"),
        ("--expiry-buffer-days", float, 7.0, "Retire this many days before the end date"),
        ("--hurdle-rate", float, 0.40, "Yearly return capital must beat; a filled share "
                                       "costs its price minus what it sells for, "
                                       "discounted at this rate over the weeks the sale "
                                       "takes"),
    ):
        parser.add_argument(flag, type=kind, default=default,
                            help=f"{help_text} (default: {default:g})")
    return parser


def connect(args):
    """An authenticated client for a live run, or None after saying why there is none."""
    from polymarket import client as polymarket_client
    from polymarket import keys

    with httpx.Client() as http:
        verdict = region.check(http)
    log(f"Region: {verdict.message}.")
    if not verdict.allowed:
        log("Not started: a live farmer would place orders from here. "
            "Start it without --live for a dry run.")
        return None
    try:
        return polymarket_client.connect(read_key(args.key_stdin))
    except (keys.KeyUnavailable, polymarket_client.ConfigError) as e:
        log(f"Error: {e}")
        return None


def read_key(from_stdin):
    from polymarket import keys

    if not from_stdin:
        return keys.load_private_key(lambda: getpass.getpass("Bitwarden master password: "))
    key = sys.stdin.readline().strip()
    if not key:
        raise keys.KeyUnavailable("no key arrived on standard input")
    return key


def main(argv=None):
    load_dotenv(data_dir.REPO / ".env")
    _make_output_safe()
    args = build_parser().parse_args(argv)
    try:
        args.db = data_dir.farm_db_path(args.db)
    except data_dir.NoDataDir as e:
        log(f"Not started: {e}.")
        return 1
    client = None
    if args.live:
        client = connect(args)
        if client is None:
            return 1
    run(args, client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
