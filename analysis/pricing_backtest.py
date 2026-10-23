"""
Backtest how the farmer prices its favorite bid against pricing that counts
what a filled share is bought for and how deep the book is.

  fetch  read each market with book samples: its taker sells from LEAD_DAYS
         before its first sample, and its favorite's minute mids to now
  run    replay every DECISION_S of each market's book samples under each
         arm and score what the chosen bid earned

At each decision the favorite bid is planned with reward_farmer.quotes_at
over target_orders' candidates (fav_bid_candidates capped at ev_cap, times
SIZE_LADDER fractions of the capital), from a flat Position, so no ask rests
and the ask rule is the same, absent, in every arm. value_of values every
candidate once: one reward, and today's fill rate and loss. An Arm is only
the function that turns that into a net a day:

  current  QuoteValue.net_per_day, today's rule: fills at the distance
           curve, charged max(loss, 0)
  priced   the reward plus the same fills valued signed at
           e(b) = (m - b) - phi - carry(b)
  depth    as priced, with the fills a day F(b, Q) of the bursts before the
           decision walking the book ahead of b, shrunk toward the other
           folds' pooled rate by STRENGTH_DAYS
  margin 0       today's rule on the live p_true with no EV margin
  follow market  today's rule on the market as the farmer runs one with no
           p_true: the FarmMarket with p_true None and the decision's book
           mid read by reward_farmer.with_book_mid, under the live config

The report also splits follow market less current by where the market's
p_true sat against the book mid at each decision (view_group), so the
operator can pick the markets to leave to the book.

Every arm's choice is scored by score(): the plan's reward over the
interval, and each burst of the interval filling min(Q left, shares sold
strictly under b), marked at the minute mid H after it, less carry. A filled
share is held for weeks, not H, so the report also nets each arm with the
carry of the expected hold (expected_hold_s) in place of H's.
"""

import argparse
import bisect
import json
import math
import random
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import httpx

from analysis import queue_fit
from analysis.exit_backtest import Mids, _fmt_interval, _percentile_interval, _utc, changes, folds
from farm import farm_books, fill_cost, reward_screener
from farm.exit_value import Piece, Sweep
from farm.fill_cost import DAY_S, DEFAULT_PRIOR, PRE_SWEEP_S, YEAR_DAYS, Hit
from farm.reward_farmer import (
    SIZE_LADDER,
    FarmConfig,
    FarmMarket,
    Position,
    QSide,
    Role,
    Slot,
    capital_room,
    ev_cap,
    quotes_at,
    target_orders,
    value_of,
    with_book_mid,
)
from farm.reward_screener import Outlook, Zones, adjust_levels, fav_bid_candidates, round_size_down
from polymarket.books import Levels

CACHE_FILE = "markets.jsonl"
LEAD_DAYS = 60.0
DECISION_S = 1800.0
# Samples further apart than this are two runs of quoting: the farmer was
# not quoting the market in between, so no order is held across the gap.
RUN_GAP_S = 2 * farm_books.BOOK_SAMPLE_S + 60
HORIZONS = (("24h", DAY_S), ("72h", 3 * DAY_S))
HORIZON_S = dict(HORIZONS)
PRIMARY = "24h"
STRENGTH_DAYS = 10.0
SENSITIVITY_STRENGTHS = (3.0, 30.0)
BOOTSTRAP_REPS = 2000
# Every this many decisions also runs target_orders itself, to show the
# current arm picks what it picks.
TARGET_CHECK_EVERY = 10
TOP_MARKETS = 10
FOLLOW = "follow market"
# Where a market's p_true sits against the book mid at a decision.
VIEW_BAND = 0.005
GROUPS = ("view over mid", "view at mid", "view under mid")
MICHIGAN = "0xeee75e18b53425c67035084fb8be5414331614ab4e5ec59574bc73e11817be93"
EPS = 1e-9


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

class Market(NamedTuple):
    """One market as the replay reads it: its farm terms (zones set per
    decision), its taker sells of the favorite from start, as fill_cost
    Hits and chained into bursts, and its favorite's minute mids.
    hit_mids is the first mid and the mid each hit's sweep would find: all
    of the mids that make_history's events and days read."""

    condition_id: str
    fm: FarmMarket
    sole_bid: bool
    start: float
    sells: list
    hits: list
    bursts: list
    mids: list
    mid: Mids
    hit_mids: list


class Burst(NamedTuple):
    """A burst of sells (queue_fit.bursts): its first sell's time, pieces
    per price, all its shares, the mid PRE_SWEEP_S before it (None before
    the first mid) and its last sell's time."""

    at: float
    pieces: tuple
    shares: float
    pre_mid: Optional[float]
    last: float


class Decision(NamedTuple):
    """A market's book sample at t, the order chosen then held to end."""

    t: float
    end: float
    sample: farm_books.BookSample


class Candidate(NamedTuple):
    """One planned favorite bid at b of Q shares, the mid m its plan
    creates, and value_of's QuoteValue of the plan: the reward, and today's
    fills a day and loss a share."""

    plan: object
    b: float
    Q: float
    m: float
    value: reward_screener.QuoteValue


class Known(NamedTuple):
    """What an arm may know at a decision beyond the candidates: per horizon
    the fall phi of the other folds, the hurdle rate, and the fills a day
    of a bid at b of Q shares from the depth model at a strength."""

    phi: dict
    hurdle_rate: float
    depth_fills: Callable
    years_left: float = 0.0


class Arm(NamedTuple):
    """How an arm values a candidate: its fill model, fills(c, known) a
    day, and its net(c, known, fills) a day; horizon is the one its fills
    are valued at, None for today's rule, which values at none."""

    name: str
    horizon: Optional[str]
    fills: Callable
    net: Callable
    terms: str = "live"


class Choice(NamedTuple):
    """An arm's pick at a decision: the bid (b, Q, m) or none, the reward a
    day of the plan picked, and the fills a day the arm forecast for it."""

    b: Optional[float]
    Q: float
    m: Optional[float]
    reward_per_day: float
    fills_per_day: float = 0.0


class Scored(NamedTuple):
    """One choice over its interval at one horizon: the reward earned, the
    shares filled and their mark H later less b and carry, and the fills
    with no mid H later, left out of fill_pnl. hold_cost is the carry the
    marked fills would add held for the expected hold rather than H."""

    reward_usd: float
    fill_shares: float
    fill_pnl: float
    dropped_fills: int
    hold_cost: float = 0.0

    @property
    def net(self):
        return self.reward_usd + self.fill_pnl


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

def carry(b, hurdle_rate, horizon_s):
    """What holding a share bought at b costs over the horizon at the hurdle rate."""
    return b * math.log1p(hurdle_rate) * horizon_s / DAY_S / 365


def edge(c, known, horizon):
    """e(b): what a filled share earns by the horizon, signed."""
    return (c.m - c.b) - known.phi[horizon] - carry(c.b, known.hurdle_rate,
                                                     HORIZON_S[horizon])


def edge_held(c, known, horizon):
    """e(b) with the carry of the expected hold, the cost the held scoring
    charges, instead of the horizon's."""
    return (c.m - c.b) - known.phi[horizon] - carry(
        c.b, known.hurdle_rate, expected_hold_s(c.Q, known.years_left))


def distance_fills(c, known):
    """Today's fills: the whole bid each time a sweep passes its distance."""
    return c.value.fills_per_day


def depth_model(strength):
    return lambda c, known: known.depth_fills(c.b, c.Q, strength)


def today_net(c, known, fills):
    """Today's net: fills charged max(loss, 0)."""
    return c.value.net_per_day


def today_net_with(c, known, fills):
    """Today's valuation, loss charged max(loss, 0), on the arm's fills."""
    return c.value.reward_per_day - fills * max(c.value.loss_per_share, 0.0)


def signed_net(horizon):
    return lambda c, known, fills: c.value.reward_per_day + fills * edge(c, known, horizon)


def signed_net_held(horizon):
    return lambda c, known, fills: c.value.reward_per_day + fills * edge_held(c, known, horizon)


def arms():
    """The arms per horizon: today's, and priced and depth at it; at the
    primary horizon also depth at the other strengths."""
    table = {}
    for horizon, _ in HORIZONS:
        table[horizon] = [
            Arm("current", None, distance_fills, today_net),
            Arm("priced", horizon, distance_fills, signed_net(horizon)),
            Arm("depth", horizon, depth_model(STRENGTH_DAYS), signed_net(horizon)),
            Arm("current+depth", horizon, depth_model(STRENGTH_DAYS), today_net_with),
            Arm("priced held", horizon, distance_fills, signed_net_held(horizon)),
            Arm("depth held", horizon, depth_model(STRENGTH_DAYS), signed_net_held(horizon)),
            Arm("margin 0", horizon, distance_fills, today_net, "margin 0"),
            Arm(FOLLOW, horizon, distance_fills, today_net, FOLLOW),
            Arm("follow+depth", horizon, depth_model(STRENGTH_DAYS), today_net_with, FOLLOW)]
        if horizon == PRIMARY:
            table[horizon] += [Arm(f"depth s={s:g}", horizon, depth_model(s), signed_net(horizon))
                               for s in SENSITIVITY_STRENGTHS]
    return table


ARMS = arms()


def terms(fm, cfg, levels):
    """{terms name: (FarmMarket, FarmConfig)} the arms plan under at a
    decision on levels, the book without our orders: the live ones, the
    live p_true with no EV margin, and the market followed, as the farmer
    runs a market with no p_true, under the live config."""
    return {"live": (fm, cfg), "margin 0": (fm, cfg._replace(ev_margin=0.0)),
            FOLLOW: (with_book_mid(fm._replace(p_true=None), levels), cfg)}


def view_group(p_true, mid):
    """The GROUPS entry of a p_true against the book mid: over it by more
    than VIEW_BAND, within it, or under it by more."""
    if p_true - mid > VIEW_BAND + EPS:
        return GROUPS[0]
    return GROUPS[2] if mid - p_true > VIEW_BAND + EPS else GROUPS[1]


def choose(candidates, base_reward, arm, known):
    """The arm's pick, by target_orders' rules: the best net, ties to the
    lower price then the smaller size (candidates come highest price and
    largest size first), quoted only when it beats the plan without a bid."""
    best = None
    for c in candidates:
        fills = arm.fills(c, known)
        net = arm.net(c, known, fills)
        if best is None or net >= best[1] - 1e-12:
            best = c, net, fills
    if best is None or best[1] <= base_reward:
        return Choice(None, 0.0, None, base_reward)
    c, _, fills = best
    return Choice(c.b, c.Q, c.m, c.value.reward_per_day, fills)


# ---------------------------------------------------------------------------
# Depth fills
# ---------------------------------------------------------------------------

class Swept(NamedTuple):
    """Burst sizes sorted with their running sums, over days: what fills a
    bid behind A shares, G(A, Q) = sum min(Q, max(0, X - A)), a day."""

    sizes: list
    sums: list
    days: float

    @staticmethod
    def of(sizes, days):
        sizes = sorted(sizes)
        sums, total = [0.0], 0.0
        for x in sizes:
            total += x
            sums.append(total)
        return Swept(sizes, sums, days)

    def filled(self, A, Q):
        """sum over the bursts of min(Q, max(0, X - A))."""
        lo = bisect.bisect_right(self.sizes, A)
        hi = bisect.bisect_right(self.sizes, A + Q)
        partial = (self.sums[hi] - self.sums[lo]) - A * (hi - lo)
        return partial + Q * (len(self.sizes) - hi)

    def per_day(self, A, Q):
        return self.filled(A, Q) / self.days if self.days > 0 else 0.0


def depth_fills(own, prior, levels):
    """F(b, Q, s) at a decision: the market's own bursts and the other
    folds' pooled rate, with A(b) the shares the others bid at b and
    above, since a new bid joins behind everyone at its price."""
    bids = levels.bids

    def fills(b, Q, strength):
        A = sum(s for p, s in bids if p >= b - EPS)
        return (own.filled(A, Q) + strength * prior.per_day(A, Q)) / (own.days + strength)

    return fills


# ---------------------------------------------------------------------------
# Market history as of a decision
# ---------------------------------------------------------------------------

def market_bursts(sells, mids):
    """The sells chained as queue_fit chains them, each Burst with the mid
    before it and its last sell's time."""
    chained = queue_fit.bursts(sells)
    starts = [b.at for b in chained[1:]] + [math.inf]
    times = [s.at for s in sells]
    out = []
    for burst, following in zip(chained, starts):
        last = times[bisect.bisect_left(times, following) - 1]
        out.append(Burst(burst.at, burst.pieces, sum(p.size for p in burst.pieces),
                         mids.at(burst.at - PRE_SWEEP_S), last))
    return out


def bursts_before(market, t):
    """The bursts made of the sells before t only: a burst still running at
    t is cut at t."""
    i = bisect.bisect_left(market.bursts, t, key=lambda b: b.at)
    known = market.bursts[:i]
    if known and known[-1].last >= t:
        cut = [s for s in market.sells if known[-1].at <= s.at < t]
        (b,) = queue_fit.bursts(cut)
        known[-1] = known[-1]._replace(pieces=b.pieces,
                                       shares=sum(p.size for p in b.pieces),
                                       last=max(s.at for s in cut))
    return known


def history_as_of(market, t, floor):
    """fill_cost.make_history as a read at t would make it, from the hits
    and mids before t."""
    hits = market.hits[:bisect.bisect_left(market.hits, t, key=lambda h: h.at)]
    mids = market.hit_mids[:bisect.bisect_left(market.hit_mids, t, key=lambda m: m[0])]
    return fill_cost.make_history(hits, mids, market.start, _utc(t), floor)


def hit_mids(hits, mids):
    """The first mid and, per hit, the last mid PRE_SWEEP_S or more before
    it: every mid make_history reads for its events and days, so it counts
    the same from these as from all of them, in far less time. Only its
    exits, which no valuation reads, would differ."""
    times = [t for t, _ in mids]
    kept = {0} if mids else set()
    for h in hits:
        i = bisect.bisect_right(times, h.at - PRE_SWEEP_S) - 1
        if i >= 0:
            kept.add(i)
    return [mids[i] for i in sorted(kept)]


def own_swept(market, t, history, floor):
    """The market's bursts in its history window before t, the mid before
    each at or above floor, over the history's days."""
    start = t - history.days * DAY_S
    return Swept.of([b.shares for b in bursts_before(market, t)
                     if b.at >= start and b.pre_mid is not None and b.pre_mid >= floor],
                    history.days)


# ---------------------------------------------------------------------------
# Folds: phi and the depth prior from the other folds
# ---------------------------------------------------------------------------

class FoldStats(NamedTuple):
    """One market's whole span, for the other folds' fits: its bursts with
    the mid before at or above floor, the days its span covers, and per
    horizon the shares and share-weighted falls of the mid by then."""

    sizes: list
    days: float
    fall: dict


def fold_stats(market, floor):
    span_from = max(market.start, market.mids[0][0] + PRE_SWEEP_S) if market.mids else math.inf
    last = market.mids[-1][0] if market.mids else -math.inf
    used = [b for b in market.bursts
            if span_from <= b.at < last and b.pre_mid is not None and b.pre_mid >= floor]
    fall = {}
    for horizon, seconds in HORIZONS:
        later = [b for b in used if b.at + seconds <= last]
        fall[horizon] = (sum(b.shares for b in later),
                         sum(b.shares * (b.pre_mid - market.mid.at(b.at + seconds))
                             for b in later))
    return FoldStats([b.shares for b in used], max(0.0, (last - span_from) / DAY_S), fall)


class FoldFit(NamedTuple):
    """Fit on the markets outside a fold: phi per horizon, and the pooled
    bursts a day for the depth prior."""

    phi: dict
    prior: Swept
    markets: int


def fit_folds(stats, fold_of):
    """{fold: FoldFit} from {condition_id: FoldStats}."""
    fits = {}
    for fold in sorted(set(fold_of.values())):
        others = [s for cid, s in stats.items() if fold_of[cid] != fold]
        phi = {}
        for horizon, _ in HORIZONS:
            shares = sum(s.fall[horizon][0] for s in others)
            phi[horizon] = sum(s.fall[horizon][1] for s in others) / shares if shares else 0.0
        fits[fold] = FoldFit(phi, Swept.of([x for s in others for x in s.sizes],
                                           sum(s.days for s in others)), len(others))
    return fits


# ---------------------------------------------------------------------------
# One decision
# ---------------------------------------------------------------------------

def tick_of(sample, tick):
    """The market's tick, or 0.001 when the book quotes off its grid: the
    tick shrinks near the ends of the price range."""
    for p, _ in sample.bids + sample.asks:
        if abs(p / tick - round(p / tick)) > 1e-6:
            return 0.001
    return tick


def others_book(sample, tick):
    """The sample's favorite book less our own orders, each where it shows
    there: a longshot order at the mirrored price on the other side."""
    bids, asks = [], []
    for name, price, size in sample.ours:
        slot = Slot[name]
        fav_price = price if slot.role is Role.FAV else round(1 - price, 6)
        (bids if slot.q_side is QSide.ONE else asks).append((fav_price, -size))
    return adjust_levels(Levels(list(sample.bids), list(sample.asks), tick), bids, asks)


def candidates(fm, levels, cfg, outlook):
    """(the plan with no bid and its reward, the Candidates): every
    candidate price at every SIZE_LADDER fraction of the capital that buys
    the reward minimum, highest price and largest size first, whose plan
    rests a favorite bid, each valued once by value_of."""
    flat = Position()
    base = quotes_at(fm, levels, flat, cfg, True, None)
    if base.mid is None:
        return None, []
    base_reward = value_of(base, fm, levels, flat, cfg, outlook).net_per_day
    room = capital_room(fm, flat)
    found = []
    for price in fav_bid_candidates(levels, fm.v, fm.tick, fm.min_size, cfg.max_lead,
                                    ev_cap(fm, cfg)):
        for fraction in SIZE_LADDER:
            size = round_size_down(fraction * room / price)
            if size < fm.min_size:
                break
            plan = quotes_at(fm, levels, flat, cfg, True, price, size)
            bid = next((i for i in plan.intents if i.slot is Slot.FAV_BID), None)
            if bid is None:
                break
            found.append(Candidate(plan, bid.price, bid.size, plan.mid,
                                   value_of(plan, fm, levels, flat, cfg, outlook)))
    return base_reward, found


def expected_hold_s(Q, years_left):
    """How long a fill of Q shares is held, on average: the farmer's ask
    sells it at fill_cost.sales_per_day, cut short at resolution."""
    rate = fill_cost.sales_per_day(Q)
    return -math.expm1(-rate * years_left * YEAR_DAYS) / rate * DAY_S


def score(choice, bursts, start, end, last_mid, mid, hurdle_rate, horizon_s, hold_s=None):
    """The choice held over [start, end): the plan's reward for that long,
    and each burst starting in it filling min(Q left, shares sold strictly
    under b), marked at the mid horizon_s after the burst less b and carry;
    a fill with no mid that late is counted and left out. hold_s, when
    given, prices the hold_cost of the marked fills."""
    reward = choice.reward_per_day * (end - start) / DAY_S
    if choice.b is None:
        return Scored(reward, 0.0, 0.0, 0)
    left, shares, pnl, dropped, marked = choice.Q, 0.0, 0.0, 0, 0.0
    cost = choice.b + carry(choice.b, hurdle_rate, horizon_s)
    i = bisect.bisect_left(bursts, start, key=lambda b: b.at)
    while i < len(bursts) and bursts[i].at < end and left > EPS:
        burst = bursts[i]
        filled = min(left, sum(p.size for p in burst.pieces if p.price < choice.b - EPS))
        if filled > 0:
            left -= filled
            shares += filled
            if burst.at + horizon_s <= last_mid:
                pnl += filled * (mid.at(burst.at + horizon_s) - cost)
                marked += filled
            else:
                dropped += 1
        i += 1
    extra = 0.0 if hold_s is None else marked * (carry(choice.b, hurdle_rate, hold_s)
                                                 - carry(choice.b, hurdle_rate, horizon_s))
    return Scored(reward, shares, pnl, dropped, extra)


def decisions(samples):
    """The samples a decision is taken at: the first of each run of
    quoting, then the first DECISION_S or more after the last decision;
    each held until the next decision or the run's end."""
    runs, run = [], []
    for s in samples:
        if run and s.at.timestamp() - run[-1].at.timestamp() > RUN_GAP_S:
            runs.append(run)
            run = []
        run.append(s)
    if run:
        runs.append(run)
    found = []
    for run in runs:
        picked = []
        for s in run:
            if not picked or s.at.timestamp() >= picked[-1].at.timestamp() + DECISION_S:
                picked.append(s)
        ends = [s.at.timestamp() for s in picked[1:]] + \
            [run[-1].at.timestamp() + farm_books.BOOK_SAMPLE_S]
        found += [Decision(s.at.timestamp(), end, s) for s, end in zip(picked, ends)]
    return found


# ---------------------------------------------------------------------------
# One market
# ---------------------------------------------------------------------------

class Totals(NamedTuple):
    """Sums over one market's decisions for one arm at one horizon, with
    the fill shares the arm forecast for its picks over their intervals."""

    decisions: int = 0
    bids: int = 0
    reward: float = 0.0
    fill_shares: float = 0.0
    fill_pnl: float = 0.0
    dropped_fills: int = 0
    distance: float = 0.0
    size: float = 0.0
    forecast_fills: float = 0.0
    hold_cost: float = 0.0

    def add(self, choice, s, days):
        bid = choice.b is not None
        return Totals(self.decisions + 1, self.bids + bid, self.reward + s.reward_usd,
                      self.fill_shares + s.fill_shares, self.fill_pnl + s.fill_pnl,
                      self.dropped_fills + s.dropped_fills,
                      self.distance + (choice.m - choice.b if bid else 0.0),
                      self.size + choice.Q, self.forecast_fills + choice.fills_per_day * days,
                      self.hold_cost + s.hold_cost)


def replay_market(market, samples, cfg, fit, check_every=TARGET_CHECK_EVERY):
    """One market's decisions under every arm: {horizon: {arm: Totals}}
    over the decisions whose interval ends a horizon before the last mid;
    the checks: the current and follow market arms beside target_orders,
    the current arm beside the live favorite bid and its fill shares over
    every decision, and the follow market mid beside the sample's; and
    {horizon: {group: {arm: Totals}}} of the current and follow market arms
    by the decision's view_group."""
    floor = cfg.min_fav
    last_mid = market.mids[-1][0] if market.mids else -math.inf
    totals = {h: {arm.name: Totals() for arm in ARMS[h]} for h, _ in HORIZONS}
    grouped = {h: {g: {"current": Totals(), FOLLOW: Totals()} for g in GROUPS}
               for h, _ in HORIZONS}
    checks = Counter()
    for k, d in enumerate(decisions(samples)):
        if d.t < market.start:
            checks["before trades"] += 1
            continue
        tick = tick_of(d.sample, market.fm.tick)
        fm = market.fm._replace(tick=tick, zones=Zones(
            fill_cost.years_until(market.fm.end_date, _utc(d.t)), d.sample.mid))
        levels = others_book(d.sample, tick)
        history = history_as_of(market, d.t, floor)
        outlook = Outlook(fm.rate_per_day, fm.zones.years_left,
                          fill_cost.fill_curve(history, DEFAULT_PRIOR, floor), cfg.hurdle_rate)
        base_reward, found = candidates(fm, levels, cfg, outlook)
        if base_reward is None:
            checks["one-sided"] += 1
            continue
        checks["decisions"] += 1
        by_terms = terms(fm, cfg, levels)
        found_by = {name: found if name == "live" else candidates(t_fm, levels, t_cfg, outlook)[1]
                    for name, (t_fm, t_cfg) in by_terms.items()}
        follow_fm = by_terms[FOLLOW][0]
        group = view_group(fm.p_true, follow_fm.book_mid)
        gap = abs(follow_fm.book_mid - d.sample.mid)
        checks["follow mid differs"] += gap > EPS
        checks["follow mid gap sum"] += gap
        known = Known(fit.phi, cfg.hurdle_rate,
                      depth_fills(own_swept(market, d.t, history, floor), fit.prior, levels),
                      fm.zones.years_left)
        current = choose(found, base_reward, ARMS[PRIMARY][0], known)
        _check_current(checks, current, d.sample, tick, market.sole_bid)
        if check_every and k % check_every == 0:
            _check_target(checks, "target", current, fm, levels, cfg, outlook)
            follow = choose(found_by[FOLLOW], base_reward, ARMS[PRIMARY][0], known)
            _check_target(checks, "follow target", follow, follow_fm, levels, cfg, outlook)
        checks["current fill shares"] += score(current, market.bursts, d.t, d.end, math.inf,
                                               market.mid, cfg.hurdle_rate, 0.0).fill_shares
        for horizon, seconds in HORIZONS:
            if d.end + seconds > last_mid:
                continue
            for arm in ARMS[horizon]:
                choice = current if arm.horizon is None else choose(
                    found_by[arm.terms], base_reward, arm, known)
                s = score(choice, market.bursts, d.t, d.end, last_mid, market.mid,
                          cfg.hurdle_rate, seconds,
                          expected_hold_s(choice.Q, fm.zones.years_left) if choice.Q else None)
                days = (d.end - d.t) / DAY_S
                totals[horizon][arm.name] = totals[horizon][arm.name].add(choice, s, days)
                split = grouped[horizon][group]
                if arm.name in split:
                    split[arm.name] = split[arm.name].add(choice, s, days)
    return totals, checks, grouped


def _check_current(checks, current, sample, tick, sole_bid):
    live = [price for name, price, _ in sample.ours if name == "FAV_BID"]
    if not live:
        return
    key = "sole" if sole_bid else "plain"
    checks[f"live {key}"] += 1
    if current.b is None:
        checks[f"live {key} no current bid"] += 1
        return
    gap = current.b - max(live)
    checks[f"live {key} both"] += 1
    checks[f"live {key} gap sum"] += gap
    checks[f"live {key} within tick"] += abs(gap) <= tick + EPS


def _check_target(checks, key, choice, fm, levels, cfg, outlook):
    planned = target_orders(fm, levels, Position(), cfg, True, outlook, bid_without_history=True)
    bid = next(((i.price, i.size) for i in planned.intents if i.slot is Slot.FAV_BID), None)
    mine = None if choice.b is None else (choice.b, choice.Q)
    checks[f"{key} checked"] += 1
    checks[f"{key} agrees"] += bid == mine


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def farm_terms(db_path):
    """(FarmConfig of the farmer's status, {condition_id: farm row dict})."""
    conn = queue_fit._read_only(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cfg = FarmConfig(**json.loads(conn.execute(
            "SELECT config FROM farmer_status").fetchone()["config"]))
        rows = {r["condition_id"]: dict(r) for r in conn.execute(
            "SELECT condition_id, slug, question, p_true, capital_usd, naked_budget_usd, "
            "end_date, metrics, sole_bid FROM markets")}
        return cfg, rows
    finally:
        conn.close()


def farm_market_of(row, cfg):
    """The FarmMarket of a farm.db row, its terms from the scan's market
    record; None without a market record, or without p_true: the current
    arm is the operator's view, and a market with none has no samples from
    before the farmer could follow one."""
    market = (json.loads(row["metrics"]) or {}).get("market") if row["metrics"] else None
    if not market or row["p_true"] is None:
        return None
    end_date = datetime.fromisoformat(row["end_date"] or market["end_date"])
    return FarmMarket(
        condition_id=row["condition_id"], slug=row["slug"] or market.get("slug", ""),
        question=row["question"] or "", end_date=end_date, neg_risk=False,
        tick=float(market["tick"]), fav_token_id=market["fav_token_id"],
        fav_outcome=market.get("fav_outcome", ""), rate_per_day=market["rate_per_day"],
        v=market["v"], min_size=market["min_size"], long_token_id="",
        p_true=row["p_true"], naked_budget_usd=row["naked_budget_usd"] or 0.0,
        expires_at=end_date, capital=(row["capital_usd"] if row["capital_usd"] is not None
                                      else cfg.capital),
        zones=Zones(0.0))


def market_of(record, row, cfg):
    fm = farm_market_of(row, cfg)
    if fm is None:
        return None
    sells = sorted((Sweep(at, tuple(Piece(p, s) for p, s in pieces))
                    for at, pieces in record["sells"]), key=lambda s: s.at)
    hits = [Hit(s.at, min(p.price for p in s.pieces)) for s in sells]
    mids = sorted((t, p) for t, p in record["mids"])
    mid = Mids(mids)
    return Market(record["condition_id"], fm, bool(row["sole_bid"]), record["start"], sells,
                  hits, market_bursts(sells, mid) if sells else [], mids, mid,
                  hit_mids(hits, mids))


def load_cache(cache_dir, limit=None, only=None):
    records = []
    with (Path(cache_dir) / CACHE_FILE).open() as lines:
        for line in lines:
            r = json.loads(line)
            if only is None or r["condition_id"] in only:
                records.append(r)
    records.sort(key=lambda r: r["condition_id"])
    return records[:limit]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

_shared = None


def _share(books_path, cfg, fits, fold_of):
    global _shared
    _shared = books_path, cfg, fits, fold_of


def _job(args):
    record, row = args
    books_path, cfg, fits, fold_of = _shared
    market = market_of(record, row, cfg)
    conn = queue_fit._read_only(books_path)
    try:
        samples = farm_books.read_books(conn, market.condition_id)
    finally:
        conn.close()
    cid = market.condition_id
    return cid, replay_market(market, samples, cfg, fits[fold_of[cid]])


def run(records, rows, books_path, cfg, workers=1):
    """{condition_id: (totals, checks)} and the FoldFits."""
    kept = [(r, rows[r["condition_id"]]) for r in records
            if r["condition_id"] in rows and r["mids"]
            and farm_market_of(rows[r["condition_id"]], cfg) is not None]
    markets = {r["condition_id"]: market_of(r, row, cfg) for r, row in kept}
    fold_of = folds(list(markets.values()))
    fits = fit_folds({cid: fold_stats(m, cfg.min_fav) for cid, m in markets.items()}, fold_of)
    del markets
    if workers > 1:
        with Pool(workers, initializer=_share,
                  initargs=(books_path, cfg, fits, fold_of)) as pool:
            done = pool.map(_job, kept, chunksize=1)
    else:
        _share(books_path, cfg, fits, fold_of)
        done = [_job(job) for job in kept]
    return dict(done), fits, len(records) - len(kept)


def live_fills(db_path, start, end, markets):
    """(FAV_BID FILL events, their shares, the markets) the farmer logged
    on the markets from start to end."""
    conn = queue_fit._read_only(db_path)
    try:
        rows = [(c, s) for c, s in conn.execute(
            "SELECT condition_id, size FROM farm_events WHERE kind = 'FILL' AND slot = 'FAV_BID' "
            "AND at >= ? AND at <= ?", (start, end)) if c in markets]
    finally:
        conn.close()
    return len(rows), sum(s or 0.0 for _, s in rows), len({c for c, _ in rows})


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _sum(totals):
    return Totals(*(sum(column) for column in zip(*totals))) if totals else Totals()


def net(t):
    return t.reward + t.fill_pnl


def net_held(t):
    return t.reward + t.fill_pnl - t.hold_cost


def compare(by_market, horizon, arm, metric=net, reps=BOOTSTRAP_REPS, seed=0):
    """arm less current in a metric over the markets, with a 95% interval
    from resampling the markets, and per market the difference."""
    diffs = {}
    for cid, (totals, *_) in by_market.items():
        t = totals[horizon]
        if t["current"].decisions:
            diffs[cid] = metric(t[arm]) - metric(t["current"])
    return resampled(diffs, reps, seed)


def compare_groups(by_market, horizon, metric=net, reps=BOOTSTRAP_REPS, seed=0):
    """Per view_group: follow market less current in a metric over the
    decisions in it, resampled over the markets with any, beside the
    decisions and both arms' sums."""
    out = {}
    for g in GROUPS:
        per = {cid: grouped[horizon][g] for cid, (_, _, grouped) in by_market.items()
               if grouped[horizon][g]["current"].decisions}
        diffs = resampled({cid: metric(t[FOLLOW]) - metric(t["current"])
                           for cid, t in per.items()}, reps, seed)
        del diffs["by_market"]
        out[g] = {**diffs, "decisions": sum(t["current"].decisions for t in per.values()),
                  **{arm: metric(_sum([t[arm] for t in per.values()]))
                     for arm in ("current", FOLLOW)}}
    return out


def resampled(diffs, reps=BOOTSTRAP_REPS, seed=0):
    """{condition_id: difference} summed, with a 95% interval from
    resampling the markets, and the differences."""
    ids = sorted(diffs)
    rng = random.Random(seed)
    drawn = [sum(diffs[cid] for cid in rng.choices(ids, k=len(ids)))
             for _ in range(reps if ids else 0)]
    return {"difference": sum(diffs.values()), "interval": _percentile_interval(drawn),
            "markets": len(ids), "by_market": diffs}


def report(by_market, fits, skipped, cfg, live, reps=BOOTSTRAP_REPS):
    checks = Counter()
    for _, c, _ in by_market.values():
        checks.update(c)
    out = {"config": cfg._asdict(), "decision_s": DECISION_S, "strength_days": STRENGTH_DAYS,
           "prior": "fill_cost.DEFAULT_PRIOR", "skipped_markets": skipped,
           "markets": len(by_market), "checks": dict(checks),
           "live_fills": dict(zip(("events", "shares", "markets"), live)),
           "phi": {str(f): fit.phi for f, fit in fits.items()},
           "prior_days": {str(f): fit.prior.days for f, fit in fits.items()},
           "horizons": {}}
    for horizon, _ in HORIZONS:
        arms_out = {}
        for arm in ARMS[horizon]:
            per = [totals[horizon][arm.name] for totals, *_ in by_market.values()]
            total = _sum(per)
            arms_out[arm.name] = {
                **total._asdict(), "net": net(total), "net_held": net_held(total),
                "markets": sum(1 for t in per if t.decisions),
                "mean_distance_c": 100 * total.distance / total.bids if total.bids else None,
                "mean_size": total.size / total.bids if total.bids else None}
        diffs = {arm.name: compare(by_market, horizon, arm.name, net, reps)
                 for arm in ARMS[horizon][1:]}
        held = {arm.name: compare(by_market, horizon, arm.name, net_held, reps)
                for arm in ARMS[horizon][1:]}
        groups = {"net": compare_groups(by_market, horizon, net, reps),
                  "held": compare_groups(by_market, horizon, net_held, reps)}
        out["horizons"][horizon] = {"arms": arms_out, "differences": diffs, "held": held,
                                    "groups": groups}
    return out


def _num(value, spec):
    return "none" if value is None else format(value, spec)


def _usd(x):
    return f"{x:+,.2f}"


def _difference(d):
    excludes = d["interval"] is not None and (d["interval"][0] > 0 or d["interval"][1] < 0)
    return (f"{_usd(d['difference']):>10} {_fmt_interval(d['interval'], '+,.2f'):<22}"
            f"{'excludes 0' if excludes else 'includes 0'}")


def render(r, slugs=None):
    slugs = slugs or {}
    c = r["checks"]
    lines = [f"favorite bid pricing backtest: {r['markets']} markets, {c.get('decisions', 0)} "
             f"decisions every {r['decision_s']:g} s ({c.get('one-sided', 0)} one-sided books "
             f"and {c.get('before trades', 0)} before a market's trades are complete left out; "
             f"{r['skipped_markets']} cached markets without terms or mids)",
             "bid side only: every decision starts flat, so no FAV_ASK or LONG_ASK rests and "
             "the ask rule is the same, absent, in every arm",
             f"current fills from {r['prior']} shrunk by each market's history as of t; "
             f"depth strength {r['strength_days']:g} days; phi per fold (cents): " +
             ", ".join(f"{h} " + "/".join(f"{100 * fit[h]:.3f}" for fit in r["phi"].values())
                       for h, _ in HORIZONS)]
    for horizon, h in r["horizons"].items():
        lines += ["", f"H = {horizon}: decisions whose interval ends {horizon} before the last mid",
                  f"{'arm':<14}{'decisions':>10}{'bids':>8}{'mkts':>6}{'reward $':>11}"
                  f"{'fcst sh':>11}{'fill sh':>11}{'fill pnl $':>12}{'net $':>11}"
                  f"{'held $':>11}{'dist c':>8}{'size':>8}{'dropped':>8}"]
        for name, a in h["arms"].items():
            lines.append(
                f"{name:<14}{a['decisions']:>10}{a['bids']:>8}{a['markets']:>6}"
                f"{a['reward']:>11,.2f}{a['forecast_fills']:>11,.0f}{a['fill_shares']:>11,.0f}"
                f"{a['fill_pnl']:>12,.2f}"
                f"{a['net']:>11,.2f}{a['net_held']:>11,.2f}"
                f"{_num(a['mean_distance_c'], '.2f'):>8}{_num(a['mean_size'], ',.0f'):>8}"
                f"{a['dropped_fills']:>8}")
        for title, key in (("net", "differences"),
                           ("held: net less the carry of the expected hold over H", "held")):
            lines += ["", f"{title}, less current, 95% interval from resampling markets:"]
            lines += [f"  {name:<14}{_difference(d)}" for name, d in h[key].items()]
        lines += ["", f"{FOLLOW} less current by where the market's p_true sat against the "
                      f"book mid at each decision, over {100 * VIEW_BAND:g}c, within, or under:",
                  f"  {'group':<16}{'decisions':>10}{'current $':>11}{'follow $':>11}"
                  f"  {'net less current':<46}held less current"]
        for g in GROUPS:
            d, held = h["groups"]["net"][g], h["groups"]["held"][g]
            lines.append(f"  {g:<16}{d['decisions']:>10}{d['current']:>11,.2f}"
                         f"{d[FOLLOW]:>11,.2f}  {_difference(d):<46}{_difference(held)}")
        for name in ("priced", "depth", FOLLOW):
            d = h["differences"][name]["by_market"]
            top = sorted(d, key=lambda cid: -abs(d[cid]))[:TOP_MARKETS]
            lines += ["", f"  {name} less current by market (top {TOP_MARKETS} by size):"]
            lines += [f"    {_usd(d[cid]):>10}  {slugs.get(cid, cid[:12])}" for cid in top]
            if MICHIGAN in d:
                lines.append(f"    {_usd(d[MICHIGAN]):>10}  Michigan governor: "
                             f"{slugs.get(MICHIGAN, MICHIGAN[:12])}")
    lines += ["", "harness checks:"]
    for key in ("plain", "sole"):
        n, both = c.get(f"live {key}", 0), c.get(f"live {key} both", 0)
        if n:
            near = c.get(f"live {key} within tick", 0)
            lines.append(
                f"  {'sole-bid' if key == 'sole' else 'other'} markets: {n} decisions with a "
                f"live FAV_BID; current bids at {both}, within a tick at {near} ({near / n:.1%}),"
                f" no bid at {c.get(f'live {key} no current bid', 0)}; mean current less live "
                f"{100 * c.get(f'live {key} gap sum', 0) / both if both else 0:+.3f}c")
    for key, name in (("target", "current"), ("follow target", FOLLOW)):
        lines.append(f"  target_orders itself beside the {name} arm at "
                     f"{c.get(f'{key} checked', 0)} decisions: the same bid at "
                     f"{c.get(f'{key} agrees', 0)}")
    differs, n = c.get("follow mid differs", 0), c.get("decisions", 0)
    lines.append(f"  {FOLLOW} mid, read from the sample's levels less ours, differs from the "
                 f"sample's recorded mid at {differs} of {n} decisions, by "
                 f"{100 * c.get('follow mid gap sum', 0) / n if n else 0:.4f}c on average")
    lf = r["live_fills"]
    lines.append(f"  current arm fills {c.get('current fill shares', 0):,.0f} shares over every "
                 f"decision; the farmer logged {lf['events']} FAV_BID fills of "
                 f"{lf['shares']:,.0f} shares on {lf['markets']} markets over the sampled days")
    lines.append("  reward and fills: candidates() values each plan once with value_of and "
                 "score() scores every arm's choice; an arm is only its fill model and net")
    if "seconds" in r:
        lines += ["", f"took {r['seconds']:.0f} s"]
    return "\n".join(lines) + "\n"


def sampled_span(books_path):
    conn = queue_fit._read_only(books_path)
    try:
        return conn.execute("SELECT MIN(at), MAX(at) FROM books").fetchone()
    finally:
        conn.close()


def run_backtest(args):
    started = time.perf_counter()
    cfg, rows = farm_terms(args.db)
    records = load_cache(args.cache, args.limit, set(args.markets) if args.markets else None)
    by_market, fits, skipped = run(records, rows, args.books, cfg, args.workers)
    live = live_fills(args.db, *sampled_span(args.books), by_market)
    result = report(by_market, fits, skipped, cfg, live, args.reps)
    result["seconds"] = time.perf_counter() - started
    text = render(result, {cid: rows[cid]["slug"] for cid in by_market})
    print(text, end="")
    if args.report:
        Path(args.report).write_text(text)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))
        print(f"wrote {args.json}")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_market(http, listing, now):
    """One market's cache record: its sells over both /trades listings from
    LEAD_DAYS before its first sample, or from where the listings are
    complete, and its mids from a minute mid before that to now, kept where
    they change and at the last point read."""
    cid, end = listing.condition_id, now.timestamp()
    every, every_from = reward_screener.fetch_trades(http, cid, False,
                                                     listing.first_sample - LEAD_DAYS * DAY_S)
    taker, taker_from = reward_screener.fetch_trades(http, cid, True, every_from)
    oldest = min((t.at for t in map(reward_screener.parse_trade, every + taker) if t),
                 default=end)
    # A listing read to its end reaches the market's first trade, not the cutoff.
    start = max(every_from, taker_from, oldest)
    sells = [s for s in reward_screener.parse_sells(taker, every, listing.fav_token_id)
             if s.at >= start]
    read = reward_screener.fetch_mids(http, listing.fav_token_id,
                                      start - PRE_SWEEP_S - reward_screener.MIDS_LEAD_S, end)
    mids = changes(read)
    if read and mids[-1][0] != read[-1][0]:
        mids.append(list(read[-1]))
    return {"condition_id": cid, "fav_token_id": listing.fav_token_id, "tick": listing.tick,
            "read_at": now.isoformat(), "start": start,
            "sells": [[s.at, [list(p) for p in s.pieces]] for s in sells], "mids": mids}


def fetch(found, cache_dir, now, http):
    """Cache every market not cached yet, one JSON line each; a failed
    market is reported and left for the next run."""
    path = Path(cache_dir) / CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    done = ({json.loads(line)["condition_id"] for line in path.read_text().splitlines()}
            if path.exists() else set())
    todo = [m for m in found if m.condition_id not in done]
    print(f"{len(done)} cached, {len(todo)} to fetch", flush=True)
    failed = 0
    for i, listing in enumerate(todo, 1):
        try:
            record = fetch_market(http, listing, now)
        except Exception as e:
            failed += 1
            print(f"  {listing.condition_id}: failed ({e})", flush=True)
            continue
        with path.open("a") as out:
            out.write(json.dumps(record) + "\n")
        print(f"  {i}/{len(todo)} {listing.condition_id[:12]}: {len(record['sells'])} sells, "
              f"{len(record['mids'])} mids from "
              f"{datetime.fromtimestamp(record['start'], timezone.utc):%m-%d %H:%M}", flush=True)
        time.sleep(0.5)
    print(f"done: {len(todo) - failed} fetched, {failed} failed", flush=True)


def main():
    parser = argparse.ArgumentParser(prog="python -m analysis.pricing_backtest",
                                     description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="cache the sells and mids of the markets with book samples")
    p.add_argument("--db", required=True, help="farm.db, for each market's favorite and tick")
    p.add_argument("--books", required=True, help="farm_books.db")
    p.add_argument("--cache", required=True)
    p = sub.add_parser("run", help="replay the arms over the book samples")
    p.add_argument("--db", required=True, help="farm.db, for the terms and the config")
    p.add_argument("--books", required=True, help="farm_books.db")
    p.add_argument("--cache", required=True)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--limit", type=int, help="only the first N cached markets")
    p.add_argument("--markets", nargs="*", help="only these condition ids")
    p.add_argument("--reps", type=int, default=BOOTSTRAP_REPS, help="bootstrap replicates")
    p.add_argument("--json", help="also write the result to this file")
    p.add_argument("--report", help="also write the text report to this file")
    args = parser.parse_args()
    if args.command == "fetch":
        found, missing = queue_fit.listings(args.db, args.books)
        if missing:
            print(f"{len(missing)} markets with samples have no favorite in {args.db}", flush=True)
        with httpx.Client() as http:
            fetch(found, args.cache, datetime.now(timezone.utc), http)
    else:
        run_backtest(args)


if __name__ == "__main__":
    main()
