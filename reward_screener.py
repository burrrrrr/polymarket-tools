#!/usr/bin/env python3
"""
Polymarket Liquidity-Reward Screener

Ranks the markets paying liquidity rewards for a farming strategy, so a human
can approve the few worth farming. It is strictly read-only: it never places,
cancels, or signs an order and needs no credentials. Its output is a SQLite
table that a separate farmer daemon reads; the human approval in between is
the point, because the farmer will hold real inventory on whatever it is told.

The strategy being screened for: on a market whose favorite outcome trades at
90c or more, rest a favorite BID just under the midpoint (a fill there is
wanted - it is +EV whenever our own probability beats the price) and a
longshot BID far below the longshot midpoint (a fill there is not wanted).
Polymarket only scores the two-sided minimum once the midpoint is above 0.90,
so the longshot bid exists purely to make the favorite bid count. On a 0.001
tick a longshot bid at 0.1c-0.2c almost never fills and costs nearly nothing,
but it only scores if it sits inside the reward band. That splits markets
into two modes:

  FLOOR   the longshot bid scores at <= 0.2c, and the size that balances the
          favorite bid there fits the naked budget; its risk is noise.
  BUDGET  the longshot bid must sit higher to score or to fit the budget, at
          most at the cheapest price scoring min_low_score; the naked risk on
          that leg is budgeted per market at approval time.

Reward scoring follows Polymarket's published formula. An order at distance
s from the adjusted midpoint scores S(v, s) = ((v - s) / v)^2 inside the max
spread v, times its size. Q_one sums favorite bids and longshot asks, Q_two
favorite asks and longshot bids. Above a 0.90 midpoint a maker earns
min(Q_one, Q_two); between 0.10 and 0.90, max(min, max / 3). The daily pool
is split in proportion to that score.

A scan is three stages:

  1. List every reward market from /sampling-markets, with the daily rate
     from /rewards/markets/current (which includes sponsored rewards) fetched
     concurrently, and drop anything that cannot qualify without a book:
     closed, not accepting orders, too close to expiry, favorite too cheap.
  2. Fetch the favorite token's order book for each survivor (the two outcome
     books of a market mirror each other exactly, so one is enough) and the
     Gamma market record for volume, holding rewards, and a precise end date.
  3. Plan our two quotes against each book, estimate the score already
     resting there, and turn our share of the pool into a yield on the
     capital the quotes tie up. Markets rank by expected reward return over
     the farm horizon, capped so a two-year market does not dwarf everything.

Scoring and planning are pure functions of (market, book, config) so the
farmer can reuse them verbatim against its own live books.

Usage:
  python reward_screener.py                          # scan with defaults
  python reward_screener.py scan --mode floor --top 20
  python reward_screener.py list --status approved
  python reward_screener.py approve some-market-slug --p-true 0.97
  python reward_screener.py reject some-market-slug --note "resolution risk"
"""

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import NamedTuple, Optional

import httpx

import farm_db

CLOB_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
GAMMA_HOST = os.getenv("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")

# Cursor values the CLOB uses to start and end a paginated listing.
FIRST_CURSOR = "MA=="
LAST_CURSOR = "LTE="

# Most tokens /books accepts in one request; 501 is refused as too large.
BOOKS_REQUEST_LIMIT = 500
# Most condition ids Gamma /markets resolves in one request.
GAMMA_REQUEST_LIMIT = 100

# A longshot bid at or below this price is the FLOOR mode.
FLOOR_PRICE = 0.002
# The naked budget a scan prices each longshot bid within, and the one a new
# review and the console's quotable check start with.
DEFAULT_NAKED_BUDGET = 10.0
# Above this midpoint only two-sided liquidity scores.
TWO_SIDED_ABOVE = 0.90

# The favorite's sampling price is a last-trade-ish hint, not the book; the
# prefilter only drops markets that are clearly too cheap, and the real
# midpoint check happens on the book.
PREFILTER_SLACK = 0.03

COMMANDS = ("scan", "list", "approve", "reject")

SCAN_HELP = {
    "min_fav": "Minimum favorite midpoint",
    "expiry_buffer_days": "Stop farming this many days before end",
    "min_window_days": "Minimum farmable days left",
    "horizon_cap_days": "Cap on days counted toward rank",
    "capital": "USD behind the favorite bid",
    "fav_target_score": "Per-share score the favorite bid aims for. It sits at the deepest "
                        "price that still reaches it",
    "max_lead": "Most the favorite bid may sit above the best bid already in the book. "
                "0 joins it",
    "min_low_score": "The longshot bid never sits above the cheapest price scoring this per "
                     "share, and sits there when no cheaper price covers the favorite bid "
                     "within the naked budget",
    "naked_budget": "USD the longshot bid may cost. Each market's longshot bid is priced "
                    "within it, as the farmer would with this budget approved",
    "min_rate": "Minimum daily reward pool in USD",
    "fragile_drop": "Skip a market whose midpoint falls this much once its best bid "
                      "leaves",
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


class Skip(NamedTuple):
    reason: str


class ScreenConfig(NamedTuple):
    """The scan parameters; the defaults are the CLI's and the console's."""

    min_fav: float = 0.90
    expiry_buffer_days: float = 7.0
    min_window_days: float = 30.0
    horizon_cap_days: float = 90.0
    capital: float = 500.0
    fav_target_score: float = 0.45
    max_lead: float = 0.005
    min_low_score: float = 0.10
    naked_budget: float = DEFAULT_NAKED_BUDGET
    min_rate: float = 0.0
    fragile_drop: float = 0.03


class ScanResult(NamedTuple):
    assessments: list
    reward_ids: set
    funnel: farm_db.ScanFunnel


class GammaInfo(NamedTuple):
    volume24hr: Optional[float]
    holding_rewards: Optional[bool]
    end_date: Optional[datetime]
    neg_risk: Optional[bool]
    slug: Optional[str]
    event_closed: Optional[bool] = None
    resolution_statuses: tuple = ()


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


def fav_bid_price(levels, v, tick, min_size, target_score, max_lead):
    """Deepest favorite bid reaching target_score against the midpoint it creates. Price or Skip.

    The bid may sit at most max_lead above the best bid of at least min_size,
    and one tick under the lowest ask of any size, because it is post-only.
    Its score never falls as its price rises: under the best bid the midpoint
    is fixed, and above it the bid sits (best_ask - p) / 2 from the midpoint
    it creates. So the scan walks down from that ceiling and stops at the
    first miss. A ceiling that misses the target is still the answer, since
    the target is a depth preference and a low score still earns a share.
    """
    best_bid = best_price(levels.bids, min_size)
    best_ask = best_price(levels.asks, min_size)
    if best_bid is None or best_ask is None:
        return Skip("one-sided book")
    price = min(floor_to_tick(best_bid + max_lead, tick),
                floor_to_tick(levels.asks[0][0] - tick, tick))
    if price < tick:
        return Skip("favorite bid below one tick")
    decimals = tick_decimals(tick)
    while price - tick >= tick - 1e-9:
        lower = round(price - tick, decimals)
        if score(v, created_mid(lower, best_bid, best_ask) - lower) < target_score:
            break
        price = lower
    return price


def longshot_bid(levels, mid, v, tick, min_size, need, room, min_score):
    """The naked longshot bid adding `need` to Q_two for at most `room` USD.
    LongshotBid, or None when need is not positive or nothing fits.

    The bid never prices above the ceiling, the cheapest price scoring at
    least min_score. Up to the ceiling it takes the cheapest price whose
    covering size, round_size_up(need / score) and at least min_size, costs at
    most room. The cheaper the bid, the deeper it shows on the favorite's ask
    side and the more No buyers fill ahead of it, so a 0.1c bid scoring 0.02
    beats a 1.6c bid scoring 0.10. When no price up to the ceiling covers, the
    bid sits at the ceiling with the size room buys, and the caller trims the
    favorite bid to the Q_two that reaches; None when room buys less than
    min_size there. With no ceiling under the crossing limit there is no bid,
    even if a lower-scoring price could cover.

    Near the band edge a small move of the midpoint changes the score, and so
    the covering size, a lot, and KEEP_SIZE_RANGE replaces the bid more
    often. That is accepted: at the floor, losing queue position only makes a
    fill less likely.

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
            if size * price <= room + 1e-9:
                return LongshotBid(price, size, s)
    price, s = scored[-1]
    size = round_size_down(room / price)
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


def plan_quotes(market, levels, config):
    """Price and size our favorite bid and longshot bid. Returns QuotePlan or Skip.

    The longshot bid is longshot_bid within config.naked_budget, covering the
    favorite bid's score, as the farmer prices it. When the budget leaves it
    short, the favorite bid shrinks to match, as the farmer's does: score
    beyond the smaller side earns nothing.

    A favorite bid above the best bid drags the midpoint up toward itself, so
    both legs are scored against the midpoint that includes it (QuotePlan.mid),
    not the book's own.
    """
    tick, v = market.tick, market.v
    fav_price = fav_bid_price(levels, v, tick, market.min_size, config.fav_target_score,
                              config.max_lead)
    if isinstance(fav_price, Skip):
        return fav_price
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
        mode=Mode.FLOOR if low.price <= FLOOR_PRICE + 1e-9 else Mode.BUDGET,
    )


def days_until(end_date, buffer_days, now):
    return (end_date - timedelta(days=buffer_days) - now).total_seconds() / 86400


def assess(market, levels, config, now):
    """Plan quotes on one market's favorite book and value them. Returns Assessment or Skip."""
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

    plan = plan_quotes(market, levels, config)
    if isinstance(plan, Skip):
        return plan
    mid = plan.mid

    q_one = plan.fav_bid_score * plan.fav_bid_size
    q_two = plan.low_score * plan.low_size
    my_q = q_min(q_one, q_two, mid)
    comp_q = competing_score(levels, mid, market.v, market.min_size)
    share = my_q / (my_q + comp_q)
    est_daily_usd = market.rate_per_day * share
    capital_usd = (
        plan.fav_bid_size * plan.fav_bid_price + plan.low_size * plan.low_price
    )
    daily_yield = est_daily_usd / capital_usd

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
        share=share,
        est_daily_usd=est_daily_usd,
        capital_usd=capital_usd,
        daily_yield=daily_yield,
        apr=daily_yield * 365,
        days_left=days_left,
        rank_score=daily_yield * min(days_left, config.horizon_cap_days),
        depth_ahead=depth_ahead,
        depth_ahead_ratio=depth_ahead / plan.low_size,
        floor_score=score(market.v, (1 - mid) - market.tick),
        single_sided_ok=mid <= TWO_SIDED_ABOVE,
        best_bid=best_price(levels.bids, market.min_size),
        best_ask=best_price(levels.asks, market.min_size),
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


def screen_listing(item, total_rates, config, now):
    """Turn one /sampling-markets item into a RewardMarket, or say why not.

    Only checks that need no book happen here; the favorite-price check is
    deliberately loose because the listing price is not the midpoint.
    """
    if not item.get("active") or item.get("closed"):
        return Skip("inactive")
    if not item.get("accepting_orders") or not item.get("enable_order_book"):
        return Skip("not accepting orders")

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
    return GammaInfo(
        volume24hr=_float(entry.get("volume24hr")),
        holding_rewards=holding if isinstance(holding, bool) else None,
        end_date=parse_time(entry.get("endDate")),
        neg_risk=neg_risk if isinstance(neg_risk, bool) else None,
        slug=entry.get("slug") or None,
        event_closed=any(bool(e.get("closed")) for e in events) if events else None,
        resolution_statuses=_string_list(entry.get("umaResolutionStatuses")),
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
    """Gamma's end date carries the real resolution time; the CLOB rounds it."""
    if info is None:
        return market
    return market._replace(
        end_date=info.end_date or market.end_date,
        neg_risk=market.neg_risk if info.neg_risk is None else info.neg_risk,
        slug=info.slug or market.slug,
    )


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


def fetch_total_rates(http):
    """{condition_id: total_daily_rate}, sponsored rewards included."""
    rates = {}
    for item in fetch_paginated(http, "/rewards/markets/current"):
        rate = _float(item.get("total_daily_rate"))
        if item.get("condition_id") and rate is not None:
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


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def run_scan(config, now, on_stage=lambda stage: None):
    """Fetch, screen, and assess, naming each stage to on_stage as it starts."""
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
    assessments, skipped = [], Counter()
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
        result = assess(market, levels, config, now)
        if isinstance(result, Skip):
            skipped[result.reason] += 1
            continue
        if info is not None:
            result = result._replace(
                volume24hr=info.volume24hr, holding_rewards=info.holding_rewards
            )
        assessments.append(result)
    assessments.sort(key=lambda a: a.rank_score, reverse=True)
    print(f"Assessed: {len(assessments):,} kept; skipped {tally(skipped)}")
    funnel = farm_db.ScanFunnel(
        at=now, duration_s=round(time.monotonic() - scan_started, 1), listed=len(listing),
        prefiltered=len(survivors), assessed=len(assessments),
        prefilter_skips=prefilter_skips, assess_skips=dict(skipped.most_common()),
    )
    return ScanResult(assessments, {item.get("condition_id") for item in listing}, funnel)


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
    """Keep a scan in farm.db: its rows, the reward set, and its funnel."""
    farm_db.store_scan(conn, [scan_row(a) for a in result.assessments], result.reward_ids,
                       now, result.funnel)


def print_table(assessments):
    header = (
        f"{'#':>3} {'mode':6} {'fav':>14} {'pool$':>6} {'v':>4} {'min':>4} "
        f"{'est$/d':>7} {'cap$':>6} {'APR%':>6} {'days':>5} {'ahead':>6} "
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
            f"{a.capital_usd:>6.0f} {a.apr * 100:>6.0f} {a.days_left:>5.0f} "
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
        fav_target_score=args.fav_target_score,
        max_lead=args.max_lead,
        min_low_score=args.min_low_score,
        naked_budget=args.naked_budget,
        min_rate=args.min_rate,
        fragile_drop=args.fragile_drop,
    )


def cmd_scan(args):
    now = datetime.now(timezone.utc)
    result = run_scan(config_from(args), now)
    assessments = result.assessments

    conn = farm_db.open_db(args.db)
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
    low_capital = plan["low_size"] * plan["low_price"]
    if args.budget < low_capital - 1e-9:
        print(
            f"Warning: planned longshot leg ties up ${low_capital:.2f}, above the "
            f"${args.budget:.2f} naked budget"
        )

    farm_db.approve_market(conn, row["condition_id"], args.p_true, args.budget, args.capital)
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
    farm_db.reject_market(conn, row["condition_id"], args.note)
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
  python reward_screener.py reject some-market-slug --note "resolution risk"
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
                         help="Naked USD risk allowed on the longshot leg (default: 25)")
    approve.add_argument("--capital", type=float,
                         help="USD behind this market's favorite bid (default: keep the "
                              "current value, else the farmer's --capital)")

    reject = sub.add_parser("reject", parents=[db], help="Reject a market")
    reject.add_argument("market", help="Slug or condition id")
    reject.add_argument("--note", help="Why")
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
