#!/usr/bin/env python3
"""
Polymarket Liquidity-Reward Screener

Ranks the markets paying liquidity rewards for a farming strategy, so a human
can approve the few worth farming. It is strictly read-only: it never places,
cancels, or signs an order and needs no credentials. Its output is a SQLite
table that a separate farmer daemon reads; the human approval in between is
the point, because the farmer will hold real inventory on whatever it is told.

The strategy being screened for: on a market whose favorite outcome trades at
90c or more, rest a favorite BID just under the midpoint and a longshot BID
far below the longshot midpoint (a fill there is not wanted). A favorite fill
is not free either: the share ties up capital until the farmer's ask sells
it, which takes weeks, or until the market resolves first. So the favorite
bid sits at the price where its rewards net of what its fills cost are
highest (fill_cost.py has the model and the measurements).
Polymarket only scores the two-sided minimum once the midpoint is above 0.90,
so the longshot bid exists purely to make the favorite bid count. On a 0.001
tick a longshot bid at 0.1c almost never fills and costs a thousandth of a
dollar a share, but it only scores if it sits inside the reward band. That
splits markets into two modes:

  FLOOR   the longshot bid rests at 0.1c. Where a share there scores at
          least FLOOR_MIN_SCORE it is sized to balance the favorite bid
          whatever that costs: the naked budget does not limit it.
  BUDGET  the longshot bid must sit higher to score or to fit the budget, at
          most at the cheapest price scoring min_low_score; the naked risk on
          that leg is budgeted per market at approval time, and the favorite
          bid shrinks to what the budget balances.

Reward scoring follows Polymarket's published formula. An order at distance
s from the adjusted midpoint scores S(v, s) = ((v - s) / v)^2 inside the max
spread v, times its size. Q_one sums favorite bids and longshot asks, Q_two
favorite asks and longshot bids. Above a 0.90 midpoint a maker earns
min(Q_one, Q_two); between 0.10 and 0.90, max(min, max / 3). The daily pool
is split in proportion to that score.

A scan is three stages:

  1. List every reward market from /sampling-markets, with the daily rate
     from /rewards/markets/current (which includes sponsored rewards, and
     leaves out markets paid only by sponsors) fetched concurrently, and drop anything that cannot qualify without a book:
     closed, not accepting orders, too close to expiry, favorite too cheap.
  2. Fetch the favorite token's order book for each survivor (the two outcome
     books of a market mirror each other exactly, so one is enough) and the
     Gamma market record for volume, holding rewards, and a precise end date.
  3. Plan our two quotes against each book at every favorite bid price in
     the band, estimate the score already resting there, and value each plan
     as our share of the pool less the cost of its expected fills, with the
     book midpoint standing in for p_true. Fills come from the last scan's
     cross-market prior first; the top markets then read their fill
     history, a new prior is fit from those, and every market is valued
     again on it.
     Markets rank by expected net return over the farm horizon, capped so a
     two-year market does not dwarf everything.

Scoring and planning are pure functions of (market, book, config) so the
farmer can reuse them verbatim against its own live books.

Usage:
  python reward_screener.py                          # scan with defaults
  python reward_screener.py scan --mode floor --top 20
  python reward_screener.py scan --exclude sports,politics
  python reward_screener.py list --status approved
  python reward_screener.py approve some-market-slug --p-true 0.97
  python reward_screener.py reject some-market-slug --reason resolution_risk --note "vague rules"
"""

import argparse
import base64
import json
import math
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import NamedTuple, Optional

import httpx

import exit_value
import farm_db
import fill_cost

CLOB_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
GAMMA_HOST = os.getenv("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
DATA_API_HOST = os.getenv("POLYMARKET_DATA_API_HOST", "https://data-api.polymarket.com")

# Cursor values the CLOB uses to start and end a paginated listing.
FIRST_CURSOR = "MA=="
LAST_CURSOR = "LTE="

# /rewards/markets/current serves 500 rows a page, sorted by condition id,
# from a cursor that is the row offset in base64. A market leaving between two
# page reads moves every later row up one, so pages read end to end skip a
# market still listed. Pages read 400 rows apart overlap by 100 instead, and a
# read whose neighbouring pages stop overlapping is read again.
REWARDS_PAGE = 500
REWARDS_STRIDE = 400
REWARDS_THREADS = 8
REWARDS_ATTEMPTS = 3

# Most tokens /books accepts in one request; 501 is refused as too large.
BOOKS_REQUEST_LIMIT = 500
# Most condition ids Gamma /markets resolves in one request.
GAMMA_REQUEST_LIMIT = 100
# The data API answers about one read in thirty with a 429 and Retry-After: 1,
# even at one read every two seconds.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_WAIT_MAX_S = 5.0
# /trades pages; past 2,000 rows of a market it returns nothing.
TRADES_PAGE = 1000
TRADES_PAGES = 2
# /prices-history rejects an interval over about 15 days.
MIDS_CHUNK_S = 14 * 86400
# Mids are read from this long before a history starts, so a quiet stretch
# with no mid point does not push the start later.
MIDS_LEAD_S = 3600
HISTORY_THREADS = 4
# The fill prior is fit on this many markets drawn at random from those it
# values, not on the top ones: those are the busiest, and most of the rest are
# quiet.
PRIOR_SAMPLE = 60
FILLS_COST_TOO_MUCH = "fills cost more than the rewards"

# The longshot floor, 0.1c. A longshot bid at or below it is the FLOOR mode,
# and the naked budget does not limit it where it scores (FLOOR_MIN_SCORE): a
# fill there is rare and costs a thousandth of a dollar a share. A market on
# a coarser tick has no floor.
FLOOR_PRICE = 0.001
# The least a floor share may score for its bid to be sized without the
# budget. The covering size is need / score, and at the band edge the score
# runs to zero (the edge itself scores a float-noise 1e-30). At 0.002 the
# floor sits 4.5% of the band inside its edge, two to four ticks on the
# bands in use, and the bid costs at most half a dollar for each unit of
# score it covers: 0.5 x its score x its size for the favorite bid it
# balances.
FLOOR_MIN_SCORE = 0.002
# The naked budget a scan prices each longshot bid within, and the one a new
# review and the console's quotable check start with.
DEFAULT_NAKED_BUDGET = 10.0
# Above this midpoint only two-sided liquidity scores.
TWO_SIDED_ABOVE = 0.90

# The favorite's sampling price is a last-trade-ish hint, not the book; the
# prefilter only drops markets that are clearly too cheap, and the real
# midpoint check happens on the book.
PREFILTER_SLACK = 0.03

# Categories a scan can leave out, each the listing tags (lowercase) that put
# a market in it. Some NFL, tennis and UFC markets lack the Sports tag, hence
# the extra sports tags. Polymarket tags every esports market Sports too, so
# excluding sports excludes esports with it.
CATEGORIES = {
    "sports": frozenset({"sports", "sport", "football", "nfl", "tennis", "combat sports",
                         "ufc"}),
    "esports": frozenset({"esports"}),
    "crypto": frozenset({"crypto"}),
    "politics": frozenset({"politics"}),
    "geopolitics": frozenset({"geopolitics"}),
    "economy": frozenset({"economy"}),
    "finance": frozenset({"finance"}),
    "tech": frozenset({"tech"}),
    "culture": frozenset({"culture"}),
    "weather": frozenset({"weather"}),
}

COMMANDS = ("scan", "list", "approve", "reject")

SCAN_HELP = {
    "min_fav": "Minimum favorite midpoint",
    "expiry_buffer_days": "Stop farming this many days before end",
    "min_window_days": "Minimum farmable days left",
    "horizon_cap_days": "Cap on days counted toward rank",
    "capital": "USD behind the favorite bid",
    "max_lead": "Most the favorite bid may sit above the best bid already in the book. "
                "0 joins it",
    "min_low_score": "The longshot bid never sits above the cheapest price scoring this per "
                     "share. Above the 0.1c floor it takes the cheapest price that covers "
                     "the favorite bid within the naked budget, or else the price where the "
                     "budget buys the most score",
    "naked_budget": "USD a longshot bid above the 0.1c floor may cost. Each market's "
                    "longshot bid is priced with it, as the farmer would with this budget "
                    "approved",
    "min_rate": "Minimum daily reward pool in USD",
    "fragile_drop": "Skip a market whose midpoint falls this much once its best bid "
                      "leaves",
    "hurdle_rate": "Yearly return capital must beat; a filled share costs its price "
                   "minus what it sells for, discounted at this rate over the weeks "
                   "the sale takes",
    "tape_markets": "How many of the top markets read their fill history; the "
                    "cross-market fill prior is fit from a random sample of every "
                    "market it values, and a market not read is valued on that "
                    "prior alone",
}


def _make_output_safe():
    """Replace unencodable characters instead of crashing on them.

    Market questions carry degree signs, accents, and the occasional emoji.
    A stock Windows console is cp1252, where printing one of those raises
    UnicodeEncodeError - which would otherwise kill the scan mid-table.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def human_volume(value):
    if value is None:
        return "--"
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return f"{value:.0f}"


def tally(counter):
    return ", ".join(f"{reason} {n:,}" for reason, n in counter.most_common())


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

class Mode(Enum):
    FLOOR = "floor"
    BUDGET = "budget"


class RewardMarket(NamedTuple):
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
    tags: tuple = ()
    event_slug: Optional[str] = None
    event_title: Optional[str] = None


class Levels(NamedTuple):
    """One token's book: bids high to low, asks low to high, (price, size)."""

    bids: list
    asks: list
    tick: Optional[float] = None


class LongshotBid(NamedTuple):
    price: float
    size: float
    score: float


class QuotePlan(NamedTuple):
    mid: float
    fav_bid_price: float
    fav_bid_size: float
    fav_bid_score: float
    low_price: float
    low_size: float
    low_score: float
    mode: Mode


class Assessment(NamedTuple):
    market: RewardMarket
    book_mid: float
    mid: float
    plan: QuotePlan
    comp_q: float
    my_q: float
    share: float
    est_daily_usd: float
    capital_usd: float
    daily_yield: float
    apr: float
    days_left: float
    rank_score: float
    depth_ahead: float
    depth_ahead_ratio: float
    floor_score: float
    single_sided_ok: bool
    volume24hr: Optional[float] = None
    holding_rewards: Optional[bool] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    # The fill cost of the favorite bid. est_daily_usd, daily_yield and apr
    # stay the gross reward; rank_score is the net yield. fill_source is
    # fill_cost.fill_source's; history is the market's fill history as
    # fill_cost.history_to_json stores it, when it was read.
    fills_per_day: Optional[float] = None
    fill_cost_usd: Optional[float] = None
    net_daily_usd: Optional[float] = None
    net_apr: Optional[float] = None
    loss_per_share: Optional[float] = None
    fill_source: Optional[str] = None
    history: Optional[dict] = None


class Outlook(NamedTuple):
    """What valuing a quote needs beyond the book, worked out by the caller:
    the daily pool, the years to resolution, the fill curve and the hurdle
    rate a filled share is discounted at."""

    rate_per_day: float
    years_left: float
    fills: fill_cost.FillCurve
    hurdle_rate: float


class QuoteValue(NamedTuple):
    """One configuration of our quotes, valued per day. price is the favorite
    bid's, or None for a configuration without one: no fills, and the reward
    the other legs earn alone. loss_per_share is what a filled share loses,
    negative when the share is worth more than its price."""

    price: Optional[float]
    size: float
    capital_usd: float
    reward_per_day: float
    fills_per_day: float
    loss_per_share: float

    @property
    def fill_cost_per_day(self):
        """What the fills cost a day, never less than nothing: a bid rests
        for its reward, so a fill worth more than its price is not income."""
        return self.fills_per_day * max(self.loss_per_share, 0.0)

    @property
    def net_per_day(self):
        return self.reward_per_day - self.fill_cost_per_day

    @property
    def net_apr(self):
        return self.net_per_day * 365 / self.capital_usd if self.capital_usd > 0 else None


class Skip(NamedTuple):
    reason: str


class ScreenConfig(NamedTuple):
    """The scan parameters; the defaults are the CLI's and the console's."""

    min_fav: float = 0.90
    expiry_buffer_days: float = 7.0
    min_window_days: float = 30.0
    horizon_cap_days: float = 90.0
    capital: float = 500.0
    max_lead: float = 0.005
    min_low_score: float = 0.10
    naked_budget: float = DEFAULT_NAKED_BUDGET
    min_rate: float = 0.0
    fragile_drop: float = 0.03
    hurdle_rate: float = 0.40
    # How many markets read their fill history; the key predates histories.
    tape_markets: float = 100.0
    # Keys of CATEGORIES whose markets the prefilter skips.
    excluded_categories: tuple = ("sports", "esports", "crypto")


class ScanResult(NamedTuple):
    assessments: list
    reward_ids: set
    funnel: farm_db.ScanFunnel
    # The fill prior this scan fit, or None when too few histories were read
    # and the stored one stands; the exit prior likewise from the tapes.
    prior: Optional[fill_cost.Prior] = None
    exit_prior: Optional[exit_value.ExitPrior] = None


class GammaInfo(NamedTuple):
    volume24hr: Optional[float]
    holding_rewards: Optional[bool]
    end_date: Optional[datetime]
    neg_risk: Optional[bool]
    slug: Optional[str]
    event_closed: Optional[bool] = None
    resolution_statuses: tuple = ()
    event_slug: Optional[str] = None
    event_title: Optional[str] = None


# ---------------------------------------------------------------------------
# Scoring and planning (pure)
# ---------------------------------------------------------------------------

def tick_decimals(tick):
    return max(0, round(-math.log10(tick)))


def floor_to_tick(price, tick):
    # round() first so 0.94 / 0.01 = 93.99999999999999 floors to 94, not 93.
    return round(math.floor(round(price / tick, 6)) * tick, tick_decimals(tick))


def round_size_up(size):
    """Order sizes carry two decimals; round up so a target score is met."""
    return math.ceil(round(size * 100, 6)) / 100


def round_size_down(size):
    return math.floor(round(size * 100, 6)) / 100


def score(v, s):
    """Polymarket's order score S(v, s) per share, s = distance from the mid."""
    if -1e-9 < s < 0:
        # Float noise on an order sitting exactly at the mid.
        s = 0.0
    if 0 <= s < v:
        return ((v - s) / v) ** 2
    return 0.0


def q_min(q_one, q_two, mid):
    """The score a maker actually earns from its two sides."""
    if TWO_SIDED_ABOVE >= mid >= 1 - TWO_SIDED_ABOVE:
        return max(min(q_one, q_two), max(q_one, q_two) / 3)
    return min(q_one, q_two)


def best_price(side, min_size):
    """Price of the first level on one side of the book with at least min_size,
    or None. Levels under the reward minimum are dust that cannot score."""
    return next((p for p, s in side if s >= min_size), None)


def adjusted_mid(bids, asks, min_size):
    """Midpoint of the best bid and ask among levels of at least min_size.

    This approximates Polymarket's size-cutoff-adjusted midpoint, which skips
    orders too small to earn rewards so dust cannot drag the band. Here the
    cutoff applies to whole price levels, which aggregate many orders, so a
    level of small orders that sum past min_size still counts. Returns None
    when either side has no qualifying level.
    """
    bid = best_price(bids, min_size)
    ask = best_price(asks, min_size)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def fragile_mid(levels, min_size, min_fav, fragile_drop):
    """Whether pulling the best qualifying bid level leaves no bid, drops the
    midpoint under min_fav, or drops it by fragile_drop. In a thin book one
    maker's bid can hold the midpoint up. When that maker leaves, the farmer
    stops quoting below min_fav, and above it our bid follows the next bid down
    and the reward score collapses."""
    best = next(((p, s) for p, s in levels.bids if s >= min_size), None)
    mid = adjusted_mid(levels.bids, levels.asks, min_size)
    pulled = adjusted_mid([lvl for lvl in levels.bids if lvl != best], levels.asks, min_size)
    return pulled is None or pulled < min_fav or mid - pulled >= fragile_drop - 1e-9


def adjust_levels(levels, bids=(), asks=()):
    """Add (price, size) entries to a book's levels; a negative size removes.

    Used to put our planned quotes into a book to find the midpoint they
    create, and to take our resting orders out of a live book so we never
    price off our own quotes. A level left with no size disappears.
    """
    def merge(side, extra, descending):
        sizes = dict(side)
        for price, size in extra:
            sizes[price] = sizes.get(price, 0.0) + size
        return sorted(
            ((p, s) for p, s in sizes.items() if s > 1e-9), reverse=descending
        )

    return levels._replace(
        bids=merge(levels.bids, bids, descending=True),
        asks=merge(levels.asks, asks, descending=False),
    )


def created_mid(price, best_bid, best_ask):
    """The reward midpoint once a bid of at least min_size rests at price.

    best_bid and best_ask are the best levels of at least min_size. A bid at
    or under the best bid leaves the midpoint where it was; above it, the bid
    becomes the best bid.
    """
    return (max(price, best_bid) + best_ask) / 2


def at_floor(price):
    """Whether a longshot bid at price is a floor bid."""
    return price <= FLOOR_PRICE + 1e-9


def floor_free(long_shares, min_size):
    """Whether the naked budget leaves a market's floor bid alone: while the
    market holds no longshot shares that rest an exit. A floor bid that
    filled leaves such shares. Their exit adds to Q_one, and a floor bid
    sized to balance that would grow with every fill. So the budget, less
    the shares held at cost, then limits the floor like any other price."""
    return long_shares < min_size


def sized_without_budget(price, share_score, floor_is_free=True):
    """Whether the naked budget leaves a longshot bid at price alone: a floor
    bid whose share scores at least FLOOR_MIN_SCORE, while the floor is free
    of the budget (floor_free)."""
    return floor_is_free and at_floor(price) and share_score >= FLOOR_MIN_SCORE - 1e-9


def budget_cost(price, size, share_score, floor_is_free=True):
    """USD a longshot bid takes of the naked budget: its cost, or nothing
    where it is sized without the budget (sized_without_budget)."""
    return 0.0 if sized_without_budget(price, share_score, floor_is_free) else price * size


def longshot_bid(levels, mid, v, tick, min_size, need, room, min_score, floor_is_free=True):
    """The naked longshot bid adding `need` to Q_two. Above the floor it costs
    at most `room` USD. LongshotBid, or None when need is not positive or
    nothing fits.

    The bid never prices above the ceiling, the cheapest price scoring at
    least min_score. Up to the ceiling it takes the cheapest price whose
    covering size, round_size_up(need / score) and at least min_size, it may
    rest. The cheaper the bid, the deeper it shows on the favorite's ask side
    and the more No buyers fill ahead of it, so a 0.1c bid scoring 0.02 beats
    a 1.6c bid scoring 0.10.

    A floor bid whose share scores at least FLOOR_MIN_SCORE may rest any size
    (sized_without_budget): room does not limit it. So there the floor is the
    bid whatever the need, the price cannot jump as the need grows, and the
    caller never trims the favorite bid for it. FLOOR_MIN_SCORE bounds the
    arithmetic, not the dollars: the covering size is need / score, and at
    the band edge the score runs to zero.

    Every other size must cost at most room: a bid above the floor, a floor
    bid scoring under FLOOR_MIN_SCORE, and any bid while floor_is_free is
    false (floor_free: the market holds longshot shares that rest an exit).
    When no price up to the ceiling covers within room, the bid goes to the
    price where the size room buys scores the most, ties to the cheaper, and
    the caller trims the favorite bid to the Q_two that reaches; None when
    room buys less than min_size there. Score per dollar is U-shaped in
    price: 0.1c buys ten times the shares of 1c, so near the band edge the
    floor with the whole room often beats the ceiling (Pfizer, $10 at a
    0.9395 midpoint with v 0.065: 71.6 at 0.1c, 64.4 at the 1.7c ceiling,
    49.8 at 1c). Sitting at the ceiling there made the plan jump between the
    floor and the ceiling as the need crossed what the floor covers,
    replacing the bid every few cycles.

    With no ceiling under the crossing limit there is no bid, even if a
    lower-scoring price could cover.

    Near the band edge a small move of the midpoint changes the score, and so
    the covering size, a lot. The farmer keeps a resting bid while it still
    covers most of the score it balances (see keep_failure), so the swing
    does not replace it.

    `levels` is the favorite book and `mid` the favorite midpoint. The
    longshot ask is the mirror of the raw favorite best bid; a longshot bid
    at or above it would cross.
    """
    if need <= 0:
        return None
    low_mid = 1 - mid
    low_limit = low_mid if not levels.bids else min(low_mid, 1 - levels.bids[0][0])
    decimals = tick_decimals(tick)
    scored, n = [], 1
    while (price := round(n * tick, decimals)) < low_limit - 1e-9:
        scored.append((price, score(v, low_mid - price)))
        if scored[-1][1] >= min_score:
            break
        n += 1
    if not scored or scored[-1][1] < min_score:
        return None
    for price, s in scored:
        if s > 0:
            size = max(min_size, round_size_up(need / s))
            if sized_without_budget(price, s, floor_is_free) or size * price <= room + 1e-9:
                return LongshotBid(price, size, s)
    price, size, s = max(
        ((p, round_size_down(room / p), s) for p, s in scored if s > 0),
        key=lambda bid: (bid[2] * bid[1], -bid[0]),
    )
    return LongshotBid(price, size, s) if size >= min_size else None


def competing_score(levels, mid, v, min_size):
    """Score already resting in the book, as one maker's Q_min.

    The favorite book carries both outcomes: its bids are favorite bids plus
    longshot asks (Q_one), its asks favorite asks plus longshot bids (Q_two).
    Each level aggregates every maker's orders at that price, so taking Q_min
    of the book-wide sums treats all of them as one maker. In the two-sided
    regime that is an upper bound on what they really earn (a sum of per-maker
    minimums never exceeds the minimum of the sums), so our share comes out
    conservative. Between 0.10 and 0.90 the max/3 term breaks that bound and
    the figure is only an estimate. A level that includes our own resting
    orders counts them as competition too.
    """
    q_one = sum(score(v, mid - p) * s for p, s in levels.bids if s >= min_size)
    q_two = sum(score(v, p - mid) * s for p, s in levels.asks if s >= min_size)
    return q_min(q_one, q_two, mid)


def fav_ceiling(levels, tick, min_size, max_lead):
    """The highest favorite bid allowed, or None: at most max_lead above the
    best bid of at least min_size, and a tick under the lowest ask of any
    size, because it is post-only. None on a one-sided book or under a tick."""
    best_bid = best_price(levels.bids, min_size)
    if best_bid is None or best_price(levels.asks, min_size) is None:
        return None
    price = min(floor_to_tick(best_bid + max_lead, tick),
                floor_to_tick(levels.asks[0][0] - tick, tick))
    return price if price >= tick - 1e-9 else None


def fav_bid_candidates(levels, v, tick, min_size, max_lead, cap=None):
    """Favorite bid prices worth valuing, highest first.

    From fav_ceiling, or cap when lower, down the tick grid while the bid
    still scores against the midpoint it creates. Its score never falls as
    its price rises: under the best bid the midpoint is fixed, and above it
    the bid sits (best_ask - p) / 2 from the midpoint it creates. So the walk
    ends at the first price that scores nothing.
    """
    price = fav_ceiling(levels, tick, min_size, max_lead)
    if price is None:
        return []
    if cap is not None:
        price = min(price, cap)
    best_bid, best_ask = best_price(levels.bids, min_size), best_price(levels.asks, min_size)
    decimals = tick_decimals(tick)
    prices = []
    # The band edge itself scores a float-noise 1e-30, not a real share.
    while (price >= tick - 1e-9
           and score(v, created_mid(price, best_bid, best_ask) - price) > 1e-9):
        prices.append(price)
        price = round(price - tick, decimals)
    return prices


def value_quote(levels, mid, v, min_size, my_q, bid, capital_usd, p_true, outlook, held=0.0,
                sale_price=None):
    """What a configuration of our quotes earns per day, net of what its fills cost.

    levels is the favorite book without our orders, mid the midpoint once our
    quotes join it, my_q our Q_min there, bid the favorite bid's (price,
    size), or None, and held the favorite shares already in inventory: none
    for a scan or a preview. The reward is our share of the pool against
    competing_score. The whole bid fills each time a sweep reaches past its
    distance below mid: a sweep would find our bid in the book, so mid is the
    one our quotes create. A fill loses its price less fill_cost.share_value:
    the share sells at sale_price, the ask the farmer would rest for it,
    credited never over p_true, or at p_true without one, at the rate that
    ask sells the inventory the fill leaves, fill_cost.sales_per_day of held
    plus the bid.
    """
    comp_q = competing_score(levels, mid, v, min_size)
    reward = outlook.rate_per_day * my_q / (my_q + comp_q) if my_q > 0 else 0.0
    if bid is None:
        return QuoteValue(None, 0.0, capital_usd, reward, 0.0, 0.0)
    price, size = bid
    return QuoteValue(
        price=price,
        size=size,
        capital_usd=capital_usd,
        reward_per_day=reward,
        fills_per_day=size * outlook.fills.per_day(mid - price),
        loss_per_share=price - fill_cost.share_value(
            p_true, p_true if sale_price is None else min(sale_price, p_true),
            fill_cost.sales_per_day(held + size), outlook.years_left, outlook.hurdle_rate),
    )


def plan_at(market, levels, config, fav_price):
    """Size our favorite bid at fav_price and price our longshot bid. QuotePlan or Skip.

    The longshot bid is longshot_bid with config.naked_budget, covering the
    favorite bid's score, as the farmer prices it with no longshot shares
    held: at the floor whatever that costs, above it within the budget. When
    the budget leaves it short, the favorite bid shrinks to match, as the
    farmer's does: score beyond the smaller side earns nothing.

    A favorite bid above the best bid drags the midpoint up toward itself, so
    both legs are scored against the midpoint that includes it (QuotePlan.mid),
    not the book's own.
    """
    tick, v = market.tick, market.v
    fav_size = max(market.min_size, round_size_down(config.capital / fav_price))
    quoted = adjust_levels(levels, bids=[(fav_price, fav_size)])
    mid = adjusted_mid(quoted.bids, quoted.asks, market.min_size)
    fav_score = score(v, mid - fav_price)
    if fav_score <= 0:
        return Skip("favorite bid outside band")
    low = longshot_bid(levels, mid, v, tick, market.min_size, fav_score * fav_size,
                       config.naked_budget, config.min_low_score)
    if low is not None:
        fav_size = min(fav_size, round_size_down(low.score * low.size / fav_score))
    if low is None or fav_size < market.min_size:
        return Skip("no scoring longshot bid within the naked budget")

    return QuotePlan(
        mid=mid,
        fav_bid_price=fav_price,
        fav_bid_size=fav_size,
        fav_bid_score=fav_score,
        low_price=low.price,
        low_size=low.size,
        low_score=low.score,
        mode=Mode.FLOOR if at_floor(low.price) else Mode.BUDGET,
    )


def plan_quotes(market, levels, config, outlook, p_true):
    """The plan whose favorite bid earns the most net of its fill cost.
    (QuotePlan, QuoteValue) or Skip.

    Every price from fav_bid_candidates is planned with plan_at and valued
    with value_quote on the plan as balanced, so a bid the budget trims is
    valued at the size it rests at. A tie goes to the lower price.
    """
    best, reason = None, "favorite bid outside band"
    for price in fav_bid_candidates(levels, market.v, market.tick, market.min_size,
                                    config.max_lead):
        plan = plan_at(market, levels, config, price)
        if isinstance(plan, Skip):
            reason = plan.reason
            continue
        my_q = q_min(plan.fav_bid_score * plan.fav_bid_size, plan.low_score * plan.low_size,
                     plan.mid)
        capital = plan.fav_bid_size * plan.fav_bid_price + plan.low_size * plan.low_price
        value = value_quote(levels, plan.mid, market.v, market.min_size, my_q,
                            (price, plan.fav_bid_size), capital, p_true, outlook)
        if best is None or value.net_per_day >= best[1].net_per_day - 1e-12:
            best = plan, value
    if best is None:
        return Skip(reason)
    if best[1].net_per_day <= 0:
        return Skip(FILLS_COST_TOO_MUCH)
    return best


def days_until(end_date, buffer_days, now):
    return (end_date - timedelta(days=buffer_days) - now).total_seconds() / 86400


def assess(market, levels, config, now, prior, history=None):
    """Plan quotes on one market's favorite book and value them. Returns Assessment or Skip.

    Fills come from fill_cost.fill_curve on the market's history and the
    fill prior, read at config.min_fav; with no history, from the prior
    alone. There is no p_true at scan time, so a fill is costed against the
    book midpoint.
    """
    days_left = days_until(market.end_date, config.expiry_buffer_days, now)
    if days_left < config.min_window_days:
        return Skip("window too short")
    book_mid = adjusted_mid(levels.bids, levels.asks, market.min_size)
    if book_mid is None:
        return Skip("one-sided book")
    if book_mid < config.min_fav:
        return Skip("favorite too cheap")
    if fragile_mid(levels, market.min_size, config.min_fav, config.fragile_drop):
        return Skip("fragile midpoint")

    outlook = Outlook(market.rate_per_day, fill_cost.years_until(market.end_date, now),
                      fill_cost.fill_curve(history, prior, config.min_fav), config.hurdle_rate)
    planned = plan_quotes(market, levels, config, outlook, book_mid)
    if isinstance(planned, Skip):
        return planned
    plan, value = planned
    mid = plan.mid

    q_one = plan.fav_bid_score * plan.fav_bid_size
    q_two = plan.low_score * plan.low_size
    my_q = q_min(q_one, q_two, mid)
    comp_q = competing_score(levels, mid, market.v, market.min_size)
    capital_usd = value.capital_usd
    daily_yield = value.reward_per_day / capital_usd

    # Longshot bids priced above ours fill first; they are the favorite asks
    # below the mirror of our price.
    depth_ahead = sum(s for p, s in levels.asks if p < 1 - plan.low_price - 1e-9)

    return Assessment(
        market=market,
        book_mid=book_mid,
        mid=mid,
        plan=plan,
        comp_q=comp_q,
        my_q=my_q,
        share=my_q / (my_q + comp_q),
        est_daily_usd=value.reward_per_day,
        capital_usd=capital_usd,
        daily_yield=daily_yield,
        apr=daily_yield * 365,
        days_left=days_left,
        rank_score=value.net_per_day / capital_usd * min(days_left, config.horizon_cap_days),
        depth_ahead=depth_ahead,
        depth_ahead_ratio=depth_ahead / plan.low_size,
        floor_score=score(market.v, (1 - mid) - market.tick),
        single_sided_ok=mid <= TWO_SIDED_ABOVE,
        best_bid=best_price(levels.bids, market.min_size),
        best_ask=best_price(levels.asks, market.min_size),
        fills_per_day=value.fills_per_day,
        fill_cost_usd=value.fill_cost_per_day,
        net_daily_usd=value.net_per_day,
        net_apr=value.net_apr,
        loss_per_share=value.loss_per_share,
        fill_source=fill_cost.fill_source(history, prior, config.min_fav),
        history=fill_cost.history_to_json(history) if history else None,
    )


# ---------------------------------------------------------------------------
# External data -> domain types
# ---------------------------------------------------------------------------

def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def in_excluded_category(tags, excluded):
    """Whether a listing's tags put it in any of the excluded CATEGORIES."""
    lowered = {str(t).lower() for t in tags or ()}
    return any(lowered & CATEGORIES[c] for c in excluded)


def screen_listing(item, total_rates, config, now):
    """Turn one /sampling-markets item into a RewardMarket, or say why not.

    Only checks that need no book happen here; the favorite-price check is
    deliberately loose because the listing price is not the midpoint.
    """
    if not item.get("active") or item.get("closed"):
        return Skip("inactive")
    if not item.get("accepting_orders") or not item.get("enable_order_book"):
        return Skip("not accepting orders")
    if in_excluded_category(item.get("tags"), config.excluded_categories):
        return Skip("excluded category")

    rewards = item.get("rewards") or {}
    v = (_float(rewards.get("max_spread")) or 0) / 100
    if v <= 0:
        return Skip("no max spread")
    rate = total_rates.get(item.get("condition_id"))
    if rate is None:
        rate = sum(
            _float(r.get("rewards_daily_rate")) or 0
            for r in rewards.get("rates") or []
        )
    if rate <= 0 or rate < config.min_rate:
        return Skip("rate too low")

    end_date = parse_time(item.get("end_date_iso"))
    if end_date is None:
        return Skip("no end date")
    if days_until(end_date, config.expiry_buffer_days, now) < config.min_window_days:
        return Skip("window too short")

    tokens = [t for t in item.get("tokens") or [] if t.get("token_id")]
    if len(tokens) != 2:
        return Skip("not binary")
    fav = max(tokens, key=lambda t: _float(t.get("price")) or 0)
    if (_float(fav.get("price")) or 0) < config.min_fav - PREFILTER_SLACK:
        return Skip("favorite too cheap")

    return RewardMarket(
        condition_id=item["condition_id"],
        slug=item.get("market_slug") or "",
        question=item.get("question") or "",
        end_date=end_date,
        neg_risk=bool(item.get("neg_risk")),
        tick=_float(item.get("minimum_tick_size")) or 0.01,
        fav_token_id=str(fav["token_id"]),
        fav_outcome=fav.get("outcome") or "",
        rate_per_day=rate,
        v=v,
        min_size=_float(rewards.get("min_size")) or 0,
        tags=tuple(str(t) for t in item.get("tags") or ()),
    )


def parse_levels(entry):
    def side(raw, descending):
        levels = []
        for level in raw or []:
            price, size = _float(level.get("price")), _float(level.get("size"))
            if price is not None and size is not None and size > 0:
                levels.append((price, size))
        return sorted(levels, reverse=descending)

    return Levels(
        bids=side(entry.get("bids"), descending=True),
        asks=side(entry.get("asks"), descending=False),
        tick=_float(entry.get("tick_size")),
    )


def _string_list(value):
    """Gamma sends some list fields JSON-encoded as a string ("[]")."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def parse_gamma(entry):
    holding = entry.get("holdingRewardsEnabled")
    neg_risk = entry.get("negRisk")
    events = [e for e in entry.get("events") or [] if isinstance(e, dict)]
    event = events[0] if events else {}
    return GammaInfo(
        volume24hr=_float(entry.get("volume24hr")),
        holding_rewards=holding if isinstance(holding, bool) else None,
        end_date=parse_time(entry.get("endDate")),
        neg_risk=neg_risk if isinstance(neg_risk, bool) else None,
        slug=entry.get("slug") or None,
        event_closed=any(bool(e.get("closed")) for e in events) if events else None,
        resolution_statuses=_string_list(entry.get("umaResolutionStatuses")),
        event_slug=event.get("slug") or None,
        event_title=event.get("title") or None,
    )


def resolution_signal(info):
    """Why a still-open market is already effectively decided, or None.

    Polymarket closes a market only once its on-chain resolution settles, so
    for a while a decided market stays active and accepting orders. Two
    Gamma fields give it away earlier: the parent event is closed once the
    outcome is known (an award announced, a game over), and a UMA status
    appears once someone proposes a resolution. Farming either is quoting
    into a known outcome.
    """
    if info is None:
        return None
    if info.event_closed:
        return "event closed"
    if info.resolution_statuses:
        return "resolution proposed"
    return None


def apply_gamma(market, info):
    """Gamma's end date carries the real resolution time; the CLOB rounds it.
    The listing has no event, so Gamma's is the market's."""
    if info is None:
        return market
    return market._replace(
        end_date=info.end_date or market.end_date,
        neg_risk=market.neg_risk if info.neg_risk is None else info.neg_risk,
        slug=info.slug or market.slug,
        event_slug=info.event_slug or market.event_slug,
        event_title=info.event_title or market.event_title,
    )


class TradeRow(NamedTuple):
    """One /trades row. The first six fields identify it in both the taker
    listing and the full one."""

    tx: str
    wallet: str
    asset: str
    side: str
    size: float
    price: float
    at: float


def parse_trade(row):
    """A /trades row as a TradeRow, or None when it lacks a transaction, an
    asset, a BUY or SELL side, a positive size, a price inside (0, 1) or a
    timestamp."""
    if not isinstance(row, dict):
        return None
    side = str(row.get("side") or "").upper()
    tx, asset = str(row.get("transactionHash") or ""), str(row.get("asset") or "")
    size, price = _float(row.get("size")), _float(row.get("price"))
    at = _float(row.get("timestamp"))
    if (not tx or not asset or side not in ("BUY", "SELL") or size is None or not size > 0
            or price is None or not 0 < price < 1 or at is None or not at > 0):
        return None
    return TradeRow(tx, str(row.get("proxyWallet") or ""), asset, side, size, price, at)


def parse_hits(taker_rows, all_rows, fav_token_id):
    """[fill_cost.Hit] from a market's /trades rows: taker_rows from
    takerOnly=true, all_rows from takerOnly=false, which also lists a row for
    every maker a taker trade filled.

    A taker hits favorite bids by selling the favorite, or by buying the other
    outcome at 1 - price, matched against favorite bids by minting a pair.
    Maker rows are all_rows less taker_rows, as a multiset on their first six
    fields; a maker's favorite bid filled when it bought the favorite, or sold
    the other outcome at 1 - price. A hit's deepest price is the lowest of its
    own and of its transaction's maker fills. Malformed rows are dropped.
    """
    taker = [t for t in map(parse_trade, taker_rows) if t]
    unmatched = Counter(t[:6] for t in taker)
    deepest = {}
    for t in filter(None, map(parse_trade, all_rows)):
        if unmatched[t[:6]]:
            unmatched[t[:6]] -= 1
        elif (t.side == "BUY") == (t.asset == fav_token_id):
            deepest[t.tx] = min(deepest.get(t.tx, 1.0), _fav_price(t, fav_token_id))
    return [fill_cost.Hit(t.at, min(_fav_price(t, fav_token_id), deepest.get(t.tx, 1.0)))
            for t in taker if (t.side == "SELL") == (t.asset == fav_token_id)]


def parse_buys(taker_rows, all_rows, fav_token_id):
    """[exit_value.Sweep] from a market's /trades rows, oldest first, read as
    parse_hits reads them: one sweep per transaction whose taker bought the
    favorite, or sold the other outcome at 1 - price.

    A taker row prints the volume-weighted price of its fills, so a sweep's
    pieces are its transaction's maker fills that sold the favorite, or bought
    the other outcome at 1 - price, shares summed per price. A transaction
    listed without such makers is pieced from its taker rows instead.
    Malformed rows are dropped.
    """
    taker = [t for t in map(parse_trade, taker_rows) if t]
    unmatched = Counter(t[:6] for t in taker)
    makers = {}
    for t in filter(None, map(parse_trade, all_rows)):
        if unmatched[t[:6]]:
            unmatched[t[:6]] -= 1
        elif (t.side == "SELL") == (t.asset == fav_token_id):
            makers.setdefault(t.tx, []).append(t)
    buys = {}
    for t in taker:
        if (t.side == "BUY") == (t.asset == fav_token_id):
            buys.setdefault(t.tx, []).append(t)
    return sorted((exit_value.Sweep(min(t.at for t in rows),
                                    _pieces(makers.get(tx) or rows, fav_token_id))
                   for tx, rows in buys.items()), key=lambda s: s.at)


def _pieces(trades, fav_token_id):
    """The trades' shares summed per favorite-equivalent price, highest first."""
    shares = Counter()
    for t in trades:
        # 1 - price carries float error; rounding lets equal prices merge.
        shares[round(_fav_price(t, fav_token_id), 9)] += t.size
    return tuple(exit_value.Piece(price, size) for price, size in sorted(shares.items(),
                                                                         reverse=True))


def _fav_price(trade, fav_token_id):
    return trade.price if trade.asset == fav_token_id else 1 - trade.price


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_paginated(http, path, timeout=30):
    items, cursor = [], FIRST_CURSOR
    while cursor and cursor != LAST_CURSOR:
        resp = http.get(
            f"{CLOB_HOST}{path}", params={"next_cursor": cursor}, timeout=timeout
        )
        resp.raise_for_status()
        page = resp.json()
        items.extend(page.get("data") or [])
        cursor = page.get("next_cursor")
    return items


def offset_cursor(offset):
    return base64.b64encode(str(offset).encode()).decode()


def fetch_rewards_page(http, offset, timeout):
    """The rows of the /rewards/markets/current page at this row offset; raises
    when its cursor is no longer the row offset the fetch builds on."""
    page = get_with_retry(http, f"{CLOB_HOST}/rewards/markets/current",
                          {"next_cursor": offset_cursor(offset)}, timeout).json()
    rows = page.get("data") or []
    allowed = {LAST_CURSOR}
    if len(rows) == REWARDS_PAGE:
        allowed.add(offset_cursor(offset + REWARDS_PAGE))
    if page.get("next_cursor") not in allowed:
        raise ValueError(f"/rewards/markets/current cursor {page.get('next_cursor')!r} "
                         f"after offset {offset} is not a row offset")
    return rows


def read_rewards_pages(pool, http, timeout):
    """The pages REWARDS_STRIDE rows apart, read in parallel batches, through
    the first short one."""
    pages, offset = [], 0
    while True:
        batch = [offset + i * REWARDS_STRIDE for i in range(REWARDS_THREADS)]
        for rows in pool.map(lambda o: fetch_rewards_page(http, o, timeout), batch):
            pages.append(rows)
            if len(rows) < REWARDS_PAGE:
                return pages
        offset += REWARDS_THREADS * REWARDS_STRIDE


def stitch_rewards_pages(pages):
    """The rows of read_rewards_pages, each market once, or None when two
    neighbouring pages do not overlap and rows between them may be missing.

    Each page covers a contiguous run of condition ids, so overlapping runs
    hold every market listed throughout both reads. Raises when a page is not
    sorted by condition id, which that relies on.
    """
    for rows in pages:
        ids = [r["condition_id"] for r in rows]
        if any(a >= b for a, b in zip(ids, ids[1:])):
            raise ValueError("/rewards/markets/current is not sorted by condition id")
    for before, after in zip(pages, pages[1:]):
        if not after or after[0]["condition_id"] > before[-1]["condition_id"]:
            return None
    return list({r["condition_id"]: r for rows in pages for r in rows}.values())


def fetch_rewards_listing(http, timeout=30):
    """Every /rewards/markets/current row, each market once. Raises when the
    listing shifts past the overlap on every one of REWARDS_ATTEMPTS reads."""
    with ThreadPoolExecutor(REWARDS_THREADS) as pool:
        for _ in range(REWARDS_ATTEMPTS):
            rows = stitch_rewards_pages(read_rewards_pages(pool, http, timeout))
            if rows is not None:
                return rows
    raise RuntimeError(f"/rewards/markets/current shifted past the page overlap "
                       f"on {REWARDS_ATTEMPTS} reads")


def fetch_total_rates(http):
    """{condition_id: total_daily_rate}, sponsored rewards included, of the
    markets with native rewards. A market paid only by sponsors is left out:
    those draw bots into mispriced books, and some listed ones pay nothing."""
    rates = {}
    for item in fetch_rewards_listing(http):
        rate = _float(item.get("total_daily_rate"))
        if item.get("condition_id") and item.get("rewards_config") and rate is not None:
            rates[item["condition_id"]] = rate
    return rates


def _chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def fetch_levels(http, token_ids, timeout=20):
    """{token_id: Levels} for every token, in concurrent /books chunks.

    A failed chunk is reported and its markets drop out of this scan.
    """
    def fetch(chunk):
        try:
            resp = http.post(
                f"{CLOB_HOST}/books",
                json=[{"token_id": t} for t in chunk],
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json() or []
        except Exception as e:
            print(f"  Warning: /books chunk of {len(chunk)} failed ({e})")
            return []

    chunks = _chunks(list(dict.fromkeys(token_ids)), BOOKS_REQUEST_LIMIT)
    books = {}
    if not chunks:
        return books
    with ThreadPoolExecutor(min(len(chunks), 8)) as pool:
        for entries in pool.map(fetch, chunks):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("asset_id"):
                    books[str(entry["asset_id"])] = parse_levels(entry)
    return books


def fetch_gamma(http, condition_ids, timeout=20):
    """{condition_id: GammaInfo}; a failed chunk just leaves its markets bare."""
    def fetch(chunk):
        params = [("condition_ids", c) for c in chunk]
        params.append(("limit", str(len(chunk))))
        try:
            resp = http.get(f"{GAMMA_HOST}/markets", params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json() or []
        except Exception as e:
            print(f"  Warning: Gamma chunk of {len(chunk)} failed ({e})")
            return []

    chunks = _chunks(list(condition_ids), GAMMA_REQUEST_LIMIT)
    info = {}
    if not chunks:
        return info
    with ThreadPoolExecutor(min(len(chunks), 8)) as pool:
        for entries in pool.map(fetch, chunks):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("conditionId"):
                    info[entry["conditionId"]] = parse_gamma(entry)
    return info


def get_with_retry(http, url, params, timeout):
    """GET, waiting out a 429 up to RATE_LIMIT_RETRIES times; raises on a failed read."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        resp = http.get(url, params=params, timeout=timeout)
        if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            break
        time.sleep(min(_float(resp.headers.get("retry-after")) or 1.0, RATE_LIMIT_WAIT_MAX_S))
    resp.raise_for_status()
    return resp


def fetch_trades(http, condition_id, taker_only, cutoff, timeout=20):
    """(rows, complete_from) of the market's /trades listing, newest first;
    raises on a failed read.

    Pages stop at a short page, at one reaching past cutoff, or at
    TRADES_PAGES, the most the API serves. complete_from is cutoff unless the
    row cap ended the listing first, then the oldest row's unix time.
    """
    rows, times = [], []
    for page in range(TRADES_PAGES):
        got = get_with_retry(
            http, f"{DATA_API_HOST}/trades",
            {"market": condition_id, "takerOnly": "true" if taker_only else "false",
             "limit": TRADES_PAGE, "offset": page * TRADES_PAGE},
            timeout,
        ).json()
        got = got if isinstance(got, list) else []
        rows.extend(got)
        times.extend(t for t in (_float(r.get("timestamp")) for r in got if isinstance(r, dict))
                     if t is not None)
        if len(got) < TRADES_PAGE or min(times, default=cutoff) < cutoff:
            return rows, cutoff
    return rows, max(cutoff, min(times, default=cutoff))


def fetch_mids(http, token_id, start, end, timeout=20):
    """The token's minute mids [(unix t, price)] from start to end, sorted,
    read in MIDS_CHUNK_S chunks; raises on a failed read."""
    mids = set()
    for lo in range(int(start), int(end), MIDS_CHUNK_S):
        data = get_with_retry(
            http, f"{CLOB_HOST}/prices-history",
            {"market": token_id, "startTs": lo, "endTs": min(int(end), lo + MIDS_CHUNK_S),
             "fidelity": 1},
            timeout,
        ).json()
        for point in (data.get("history") if isinstance(data, dict) else None) or []:
            if isinstance(point, dict):
                t, p = _float(point.get("t")), _float(point.get("p"))
                if t is not None and p is not None:
                    mids.add((t, p))
    return sorted(mids)


class Read(NamedTuple):
    """One market's trades read both ways: the fill history of a favorite bid
    and the buy tape a favorite ask sells into."""

    history: fill_cost.History
    tape: Optional[exit_value.BuyTape]


def fetch_read(http, condition_id, fav_token_id, now, floor, timeout=20):
    """One market's Read; raises on a failed read.

    All rows come first because they reach the row cap soonest, then taker
    rows only back to where all rows are complete, then mids only from where
    both are. The tape holds the buys from that same start.
    """
    cutoff = now.timestamp() - fill_cost.HISTORY_DAYS * fill_cost.DAY_S
    every, every_from = fetch_trades(http, condition_id, False, cutoff, timeout)
    taker, taker_from = fetch_trades(http, condition_id, True, every_from, timeout)
    start = max(every_from, taker_from)
    mids = fetch_mids(http, fav_token_id, start - fill_cost.PRE_SWEEP_S - MIDS_LEAD_S,
                      now.timestamp(), timeout)
    hits = parse_hits(taker, every, fav_token_id)
    buys = [s for s in parse_buys(taker, every, fav_token_id) if s.at >= start]
    return Read(fill_cost.make_history(hits, mids, start, now, floor),
                exit_value.BuyTape(buys, now, start))


def fetch_reads(http, markets, now, floor):
    """{condition_id: Read} over HISTORY_THREADS threads; a failed market is left out."""
    def fetch(market):
        try:
            return market.condition_id, fetch_read(http, market.condition_id,
                                                   market.fav_token_id, now, floor)
        except Exception as e:
            print(f"  Warning: fill history of {market.slug or market.condition_id} failed ({e})")
            return market.condition_id, None

    if not markets:
        return {}
    with ThreadPoolExecutor(min(len(markets), HISTORY_THREADS)) as pool:
        return {cid: read for cid, read in pool.map(fetch, markets) if read is not None}


def load_prior(conn):
    """The fill prior the last scan stored, or fill_cost.DEFAULT_PRIOR when none
    is stored or it does not parse."""
    stored = farm_db.load_scan_json(conn, farm_db.SCAN_FILL_PRIOR_KEY)
    return fill_cost.prior_from_json(stored) or fill_cost.DEFAULT_PRIOR


def load_exit_prior(conn):
    """The exit prior the last scan stored, or exit_value.DEFAULT_EXIT_PRIOR
    when none is stored or it does not parse."""
    stored = farm_db.load_scan_json(conn, farm_db.SCAN_EXIT_PRIOR_KEY)
    return exit_value.exit_prior_from_json(stored) or exit_value.DEFAULT_EXIT_PRIOR


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def run_scan(config, now, prior, rejected=frozenset(), on_stage=lambda stage: None):
    """Fetch, screen, and assess, naming each stage to on_stage as it starts.

    prior is the stored fill prior. Every market is first assessed on it,
    only to rank them. The top tape_markets of that ranking read their fill
    history, and so do PRIOR_SAMPLE markets drawn at random, seeded by now,
    from every market whose fills the first pass valued, those it dropped as
    costing too much included, so a stored prior that ran high cannot narrow
    its own sample. The top passes over the condition ids in rejected, giving
    their slots to the next markets down the ranking, but a rejected market is
    assessed like any other and may still be sampled. A new prior is fit from
    the sample's histories alone, so it matches the markets it values rather
    than the busy top; too few leave prior standing. An exit prior is fit
    from the same sample's tapes, and too few leave the stored one standing.
    Every market is then assessed again on that prior, each one read on its
    own history too, and only this second pass's skips count. A market that
    read no history is valued on the prior alone.
    """
    scan_started = time.monotonic()
    with httpx.Client() as http:
        on_stage("listing reward markets")
        started = time.monotonic()
        with ThreadPoolExecutor(2) as pool:
            listing_job = pool.submit(fetch_paginated, http, "/sampling-markets")
            rates_job = pool.submit(fetch_total_rates, http)
            listing = listing_job.result()
            try:
                total_rates = rates_job.result()
            except Exception as e:
                print(f"  Warning: /rewards/markets/current failed ({e}); "
                      "using sampling rates")
                total_rates = {}
        print(f"Reward markets: {len(listing):,} listed, {len(total_rates):,} "
              f"total rates ({time.monotonic() - started:.1f}s)")

        survivors, skipped = [], Counter()
        for item in listing:
            result = screen_listing(item, total_rates, config, now)
            if isinstance(result, Skip):
                skipped[result.reason] += 1
            else:
                survivors.append(result)
        print(f"Prefilter: {len(survivors):,} kept; skipped {tally(skipped)}")
        prefilter_skips = dict(skipped.most_common())

        on_stage(f"fetching books and Gamma records for {len(survivors):,} markets")
        started = time.monotonic()
        with ThreadPoolExecutor(2) as pool:
            books_job = pool.submit(
                fetch_levels, http, [m.fav_token_id for m in survivors]
            )
            gamma_job = pool.submit(
                fetch_gamma, http, [m.condition_id for m in survivors]
            )
            books, gamma = books_job.result(), gamma_job.result()
        print(f"Books: {len(books):,} fetched, Gamma: {len(gamma):,} enriched "
              f"({time.monotonic() - started:.1f}s)")

    on_stage("assessing")
    candidates, skipped = [], Counter()
    for market in survivors:
        levels = books.get(market.fav_token_id)
        if levels is None:
            skipped["no book"] += 1
            continue
        info = gamma.get(market.condition_id)
        stale = resolution_signal(info)
        if stale:
            skipped[stale] += 1
            continue
        market = apply_gamma(market, info)
        if levels.tick:
            market = market._replace(tick=levels.tick)
        candidates.append((market, levels, info))
    ranked, _, valued = assess_all(candidates, config, now, prior, {})

    top, passed_over = [], 0
    for a in ranked:
        if len(top) == int(config.tape_markets):
            break
        if a.market.condition_id in rejected:
            passed_over += 1
        else:
            top.append(a.market)
    sample = random.Random(now.timestamp()).sample(valued, min(PRIOR_SAMPLE, len(valued)))
    top_ids = {m.condition_id for m in top}
    reads = top + [m for m in sample if m.condition_id not in top_ids]
    on_stage(f"reading fill histories for {len(reads):,} markets")
    started = time.monotonic()
    with httpx.Client() as http:
        fetched = fetch_reads(http, reads, now, config.min_fav)
    histories = {cid: read.history for cid, read in fetched.items()}
    fitted = fill_cost.fit_prior(
        [histories[m.condition_id] for m in sample if m.condition_id in histories],
        now, config.min_fav)
    sampled_tapes = [(fetched[m.condition_id].tape, m.end_date) for m in sample
                     if m.condition_id in fetched and fetched[m.condition_id].tape is not None]
    fitted_exit = exit_value.fit_exit_prior(sampled_tapes, exit_value.EXIT_PARAMS, now)
    print(f"Histories: {len(histories):,} of {len(reads):,} read, {len(top):,} top "
          f"({passed_over:,} rejected passed over) and "
          f"{len(reads) - len(top):,} more of {len(sample):,} sampled "
          f"({time.monotonic() - started:.1f}s); "
          + (f"fill prior fit from {fitted.markets:,} sampled" if fitted
             else "too few sampled to fit a fill prior, the stored one stands")
          + (f"; exit prior fit from {fitted_exit.markets:,} sampled tapes" if fitted_exit
             else "; too few sampled tapes to fit an exit prior, the stored one stands"))

    assessments, assess_skips, _ = assess_all(candidates, config, now, fitted or prior,
                                              histories)
    skipped.update(assess_skips)
    print(f"Assessed: {len(assessments):,} kept; skipped {tally(skipped)}")
    funnel = farm_db.ScanFunnel(
        at=now, duration_s=round(time.monotonic() - scan_started, 1), listed=len(listing),
        prefiltered=len(survivors), assessed=len(assessments),
        prefilter_skips=prefilter_skips, assess_skips=dict(skipped.most_common()),
    )
    return ScanResult(assessments, {item.get("condition_id") for item in listing}, funnel,
                      fitted, fitted_exit)


def assess_all(candidates, config, now, prior, histories):
    """(assessments by rank_score, best first, a Counter of why the rest were
    skipped, and the markets whose fills were valued: those kept and those
    whose fills cost too much) of (market, levels, Gamma info) candidates,
    each valued on prior and on its own history when histories holds one."""
    assessments, skipped, valued = [], Counter(), []
    for market, levels, info in candidates:
        result = assess(market, levels, config, now, prior, histories.get(market.condition_id))
        if isinstance(result, Skip):
            skipped[result.reason] += 1
            if result.reason == FILLS_COST_TOO_MUCH:
                valued.append(market)
            continue
        valued.append(market)
        if info is not None:
            result = result._replace(
                volume24hr=info.volume24hr, holding_rewards=info.holding_rewards
            )
        assessments.append(result)
    assessments.sort(key=lambda a: a.rank_score, reverse=True)
    return assessments, skipped, valued


def assessment_record(a):
    record = a._asdict()
    record["market"] = {
        **a.market._asdict(), "end_date": a.market.end_date.isoformat()
    }
    record["plan"] = {**a.plan._asdict(), "mode": a.plan.mode.value}
    return record


def scan_row(a):
    m = a.market
    return farm_db.ScanRow(
        condition_id=m.condition_id,
        slug=m.slug,
        question=m.question,
        end_date=m.end_date.isoformat(),
        rank_score=a.rank_score,
        metrics=json.dumps(assessment_record(a)),
    )


def store(conn, result, now):
    """Keep a scan in farm.db: its rows, the reward set, its funnel and the
    fill and exit priors it fit."""
    farm_db.store_scan(conn, [scan_row(a) for a in result.assessments], result.reward_ids,
                       now, result.funnel,
                       prior=result.prior and fill_cost.prior_to_json(result.prior),
                       exit_prior=(result.exit_prior
                                   and exit_value.exit_prior_to_json(result.exit_prior)))


FILL_SOURCE_MARK = {"history": "h", "prior": "p", "default": "d"}


def print_table(assessments):
    header = (
        f"{'#':>3} {'mode':6} {'fav':>14} {'pool$':>6} {'v':>4} {'min':>4} "
        f"{'est$/d':>7} {'fill/d':>6} {'net$/d':>7} {'cap$':>6} {'APR%':>6} {'net%':>6} "
        f"{'days':>5} {'ahead':>6} "
        f"{'vol24h':>7} {'H':1} {'question':40} slug"
    )
    print(header)
    print("-" * len(header))
    for rank, a in enumerate(assessments, 1):
        m = a.market
        fav = f"{m.fav_outcome[:7]} {a.book_mid * 100:.1f}"
        holding = {True: "H", False: "-", None: "?"}[a.holding_rewards]
        print(
            f"{rank:>3} {a.plan.mode.value:6} {fav:>14} {m.rate_per_day:>6.0f} "
            f"{m.v * 100:>4.1f} {m.min_size:>4.0f} {a.est_daily_usd:>7.2f} "
            f"{a.fills_per_day:>5.1f}{FILL_SOURCE_MARK[a.fill_source]} "
            f"{a.net_daily_usd:>7.2f} {a.capital_usd:>6.0f} {a.apr * 100:>6.0f} "
            f"{a.net_apr * 100:>6.0f} {a.days_left:>5.0f} "
            f"{a.depth_ahead_ratio:>6.1f} {human_volume(a.volume24hr):>7} "
            f"{holding:1} {m.question[:40]:40} {m.slug}"
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def config_from(args):
    return ScreenConfig(
        min_fav=args.min_fav,
        expiry_buffer_days=args.expiry_buffer_days,
        min_window_days=args.min_window_days,
        horizon_cap_days=args.horizon_cap_days,
        capital=args.capital,
        max_lead=args.max_lead,
        min_low_score=args.min_low_score,
        naked_budget=args.naked_budget,
        min_rate=args.min_rate,
        fragile_drop=args.fragile_drop,
        hurdle_rate=args.hurdle_rate,
        tape_markets=args.tape_markets,
        excluded_categories=args.exclude,
    )


def category_list(text):
    """CATEGORIES keys from comma-separated text; blank or "none" is none."""
    names = tuple(n.strip().lower() for n in text.split(",") if n.strip())
    if names == ("none",):
        return ()
    unknown = [n for n in names if n not in CATEGORIES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown categor{'y' if len(unknown) == 1 else 'ies'} {', '.join(unknown)}; "
            f"choose from {', '.join(CATEGORIES)}")
    return names


def cmd_scan(args):
    now = datetime.now(timezone.utc)
    conn = farm_db.open_db(args.db)
    result = run_scan(config_from(args), now, load_prior(conn),
                      farm_db.condition_ids(conn, "rejected"))
    assessments = result.assessments
    store(conn, result, now)
    conn.close()
    print(f"Stored {len(assessments):,} assessments in {args.db}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([assessment_record(a) for a in assessments], f, indent=1)
        print(f"Wrote {args.json}")

    shown = [
        a for a in assessments
        if args.mode == "any" or a.plan.mode.value == args.mode
    ][:args.top]
    print()
    print_table(shown)


def cmd_list(args):
    conn = farm_db.open_db(args.db)
    latest = farm_db.latest_scan(conn)
    rows = farm_db.list_markets(conn, args.status, args.top)
    conn.close()

    if not rows:
        print(f"No {args.status} markets in {args.db}")
        return
    header = (
        f"{'status':9} {'mode':6} {'mid':>5} {'est$/d':>7} {'cap$':>6} "
        f"{'APR%':>6} {'days':>5} {'p_true':>6} {'budget':>6} {'live':4} slug"
    )
    print(f"Latest scan: {latest}  (live: R = in reward set, * = in latest scan)")
    print(header)
    print("-" * len(header))
    for row in rows:
        m = json.loads(row["metrics"])
        live = ("R" if row["rewards_active"] else "-") + (
            "*" if row["last_scanned"] == latest else " "
        )
        p_true = f"{row['p_true']:.3f}" if row["p_true"] is not None else "--"
        budget = (
            f"{row['naked_budget_usd']:.0f}"
            if row["naked_budget_usd"] is not None else "--"
        )
        print(
            f"{row['status']:9} {m['plan']['mode']:6} {m.get('book_mid', m['mid']) * 100:>5.1f} "
            f"{m['est_daily_usd']:>7.2f} {m['capital_usd']:>6.0f} "
            f"{m['apr'] * 100:>6.0f} {m['days_left']:>5.0f} {p_true:>6} "
            f"{budget:>6} {live:4} {row['slug']}"
        )
        if row["note"]:
            print(f"{'':9} note: {row['note']}")


def cmd_approve(args):
    if not 0 < args.p_true < 1:
        print("Error: --p-true must be strictly between 0 and 1")
        sys.exit(2)
    if args.capital is not None and not args.capital > 0:
        print("Error: --capital must be positive")
        sys.exit(2)
    conn = farm_db.open_db(args.db)
    row = farm_db.find_market(conn, args.market)
    if row is None:
        print(f"Error: no market {args.market!r} in {args.db}; run a scan first")
        sys.exit(1)

    plan = json.loads(row["metrics"])["plan"]
    if plan["fav_bid_price"] >= args.p_true:
        print(
            f"Warning: planned favorite bid {plan['fav_bid_price'] * 100:.1f}c is "
            f"at or above p_true {args.p_true * 100:.1f}c; a fill would be -EV"
        )
    low_capital = budget_cost(plan["low_price"], plan["low_size"], plan.get("low_score", 0.0))
    if args.budget < low_capital - 1e-9:
        print(
            f"Warning: planned longshot leg ties up ${low_capital:.2f}, above the "
            f"${args.budget:.2f} naked budget"
        )

    farm_db.approve_market(conn, row["condition_id"], args.p_true, args.budget, args.capital,
                           now=datetime.now(timezone.utc))
    capital = farm_db.find_market(conn, row["condition_id"])["capital_usd"]
    conn.close()
    capital = f"${capital:.2f}" if capital is not None else "the farmer's --capital"
    print(f"Approved {row['slug']} (p_true {args.p_true}, budget ${args.budget:.2f}, "
          f"capital {capital})")


def cmd_reject(args):
    conn = farm_db.open_db(args.db)
    row = farm_db.find_market(conn, args.market)
    if row is None:
        print(f"Error: no market {args.market!r} in {args.db}; run a scan first")
        sys.exit(1)
    farm_db.reject_market(conn, row["condition_id"], args.note,
                          args.reason and farm_db.RejectReason(args.reason),
                          datetime.now(timezone.utc))
    conn.close()
    print(f"Rejected {row['slug']}")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Rank Polymarket liquidity-reward markets for a favorite-bid "
            "farming strategy and keep them in SQLite for approval. Read-only: "
            "places no orders and needs no credentials."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python reward_screener.py                          # same as 'scan'
  python reward_screener.py scan --mode floor --top 20 --json scan.json
  python reward_screener.py list --status approved
  python reward_screener.py approve some-market-slug --p-true 0.97 --budget 25
  python reward_screener.py approve some-market-slug --p-true 0.97 --capital 200
  python reward_screener.py reject some-market-slug --reason resolution_risk --note "vague rules"
        """,
    )
    db = argparse.ArgumentParser(add_help=False)
    db.add_argument("--db", default="farm.db", help="SQLite file (default: farm.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", parents=[db], help="Screen and rank reward markets")
    for field, help_text in SCAN_HELP.items():
        default = ScreenConfig._field_defaults[field]
        scan.add_argument(f"--{field.replace('_', '-')}", type=float, default=default,
                          help=f"{help_text} (default: {default})")
    excluded = ",".join(ScreenConfig._field_defaults["excluded_categories"])
    scan.add_argument("--exclude", type=category_list, default=excluded,
                      metavar="CATEGORIES",
                      help=f"Comma-separated categories to skip, or 'none' to keep all; from "
                           f"{', '.join(CATEGORIES)} (default: {excluded})")
    scan.add_argument("--mode", choices=("any", "floor", "budget"), default="any",
                      help="Show only this mode in the table (all are stored)")
    scan.add_argument("--top", type=int, default=40, help="Rows to show (default: 40)")
    scan.add_argument("--json", metavar="PATH", help="Also dump every assessment here")

    lst = sub.add_parser("list", parents=[db], help="Show stored markets")
    lst.add_argument("--status", choices=farm_db.STATUSES[:3] + ("all",), default="candidate")
    lst.add_argument("--top", type=int, default=40)

    approve = sub.add_parser("approve", parents=[db], help="Approve a market to farm")
    approve.add_argument("market", help="Slug or condition id")
    approve.add_argument("--p-true", type=float, required=True,
                         help="Your probability the favorite resolves YES")
    approve.add_argument("--budget", type=float, default=25.0,
                         help="Naked USD risk allowed on the longshot leg above the 0.1c "
                              "floor (default: 25)")
    approve.add_argument("--capital", type=float,
                         help="USD behind this market's favorite bid (default: keep the "
                              "current value, else the farmer's --capital)")

    reject = sub.add_parser("reject", parents=[db], help="Reject a market")
    reject.add_argument("market", help="Slug or condition id")
    reject.add_argument("--reason", choices=[r.value for r in farm_db.RejectReason],
                        help="Why (optional)")
    reject.add_argument("--note", help="More on why")
    return parser


def main():
    _make_output_safe()
    argv = sys.argv[1:]
    if not argv or argv[0] not in COMMANDS + ("-h", "--help"):
        argv = ["scan", *argv]
    args = build_parser().parse_args(argv)
    {
        "scan": cmd_scan,
        "list": cmd_list,
        "approve": cmd_approve,
        "reject": cmd_reject,
    }[args.command](args)


if __name__ == "__main__":
    main()
