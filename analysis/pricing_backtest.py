"""
Backtest how the farmer prices its favorite bid by walking it through each
market's recorded books, with the position, the fill watch and the fill mid
carried as the farmer carries them.

  fetch  read each market with book samples: its taker sells and buys of the
         favorite from LEAD_DAYS before its first sample, and its favorite's
         minute mids to now
  sizes  print fill_cost.SWEEP_SIZES as measured on the cache
  run    walk each run of each market's book samples under each arm

A run is a stretch of book samples no more than RUN_GAP_S apart: the farmer
was quoting the market throughout, and every walk starts it flat. At each
decision the walk plans with the production reward_farmer.target_orders on
the walked position, ceiling and fill mid, and rests the plan's favorite bid
and favorite ask. Between decisions, in time order:

  sells  each burst of taker sells fills the bid min(shares left, shares sold
         strictly under it) at its price. The bid is then gone until the next
         decision, in every arm: a farmer that saw it leave the book waits
         for positions before it bids again.
  buys   each burst of taker buys fills the ask min(shares left, shares bought
         strictly over it), with no queue ahead of it: optimistic for every
         arm alike.

Decisions are taken at a run's first sample, every DECISION_S after that, and
at the first sample after a fill or a sale. The walk's today arm is today's
rule on a walked position, not a replay of what the farmer did.

An arm is a Rules record, and it switches a rule off with the data the
farmer already takes: the fill curve's sweep sizes, the market's bid
multiple, the config's tie band and the ceiling it feeds the market. No
flag reaches the farmer.

A run's ledger is the plan's reward for the time each plan rested whole, less
what the bids bought, plus what the asks sold, less the carry of the
inventory at cost at the hurdle rate, plus what is held at the end marked at
the mid H after the run's end, less its carry to then. The held variant
carries that inventory for its expected hold (expected_hold_s) instead of H.
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
from typing import NamedTuple, Optional

import httpx

from analysis import queue_fit
from analysis.exit_backtest import Mids, _fmt_interval, _utc, changes
from farm import farm_books, fill_cost, reward_screener
from farm.exit_value import Piece, Sweep
from farm.fill_cost import DAY_S, DEFAULT_PRIOR, PRE_SWEEP_S, SWEEP_SIZES, WHOLE_BID, YEAR_DAYS, Hit
from farm.fill_watch import (BUY, NO_FILLS, SELL, Gone, inventory_ceiling, watch_listing,
                             watch_positions)
from farm.reward_farmer import (FarmConfig, FarmMarket, Position, QSide, Role, Slot,
                                approved_terms, build_parser, capital_room, config_from,
                                held_cost, held_fill_mid, market_outlook, target_orders,
                                with_book_mid)
from farm.reward_screener import Zones, adjust_levels
from polymarket.books import Levels

CACHE_FILE = "markets.jsonl"
LEAD_DAYS = 60.0
DECISION_S = 1800.0
# Samples further apart than this are two runs of quoting: the farmer was
# not quoting the market in between, so no order is held across the gap.
RUN_GAP_S = 2 * farm_books.BOOK_SAMPLE_S + 60
HORIZONS = (("24h", DAY_S), ("72h", 3 * DAY_S))
PRIMARY = "24h"
# The operator values capital at about 100% a year.
HURDLE_RATE = 1.0
BOOTSTRAP_REPS = 2000
YEAR_S = 365 * DAY_S
EPS = 1e-9


class Rules(NamedTuple):
    """Which of the new pricing rules an arm plays, as the data the farmer
    takes: sweep_sizes (WHOLE_BID fills the whole bid), bid_multiple (None
    is the market's), tie_band (None is the config's) and the inventory
    ceiling."""

    name: str
    sweep_sizes: tuple = SWEEP_SIZES
    bid_multiple: Optional[float] = None
    tie_band: Optional[float] = None
    inventory_ceiling: bool = True

    def terms(self, fm, cfg, outlook):
        """(fm, cfg, outlook) this arm plans with."""
        if self.bid_multiple is not None:
            fm = fm._replace(bid_multiple=self.bid_multiple)
        if self.tie_band is not None:
            cfg = cfg._replace(tie_band=self.tie_band)
        return fm, cfg, outlook._replace(
            fills=outlook.fills._replace(sweep_sizes=self.sweep_sizes))

    def ceiling(self, watch, held, cost, tick, now):
        """The farmer's bid_ceiling, if this arm plays it."""
        return inventory_ceiling(watch, held, cost, tick, now) if self.inventory_ceiling else None


def scaled(sizes, factor):
    return tuple(x * factor for x in sizes)


TODAY = Rules("today", WHOLE_BID, 1.0, 0.0, False)
COMBINED = Rules("combined")
ABLATIONS = (
    COMBINED._replace(name="-sweep fill", sweep_sizes=WHOLE_BID),
    COMBINED._replace(name="-bid multiple", bid_multiple=1.0),
    COMBINED._replace(name="-tie band", tie_band=0.0),
    COMBINED._replace(name="-inventory ceiling", inventory_ceiling=False),
)
SENSITIVITY = (
    COMBINED._replace(name="sweep x0.5", sweep_sizes=scaled(SWEEP_SIZES, 0.5)),
    COMBINED._replace(name="sweep x2", sweep_sizes=scaled(SWEEP_SIZES, 2.0)),
)
ARMS = (TODAY, COMBINED) + ABLATIONS + SENSITIVITY


class Market(NamedTuple):
    """One market as the walk reads it: its farm terms (zones set per
    decision), its taker sells of the favorite from start, as fill_cost Hits
    and chained into bursts, its taker buys chained into bursts, and its
    favorite's minute mids. hit_mids is the first mid and the mid each hit's
    sweep would find: all of the mids that make_history's events and days
    read."""

    condition_id: str
    fm: FarmMarket
    sole_bid: bool
    start: float
    sells: list
    hits: list
    bursts: list
    buys: list
    mids: list
    mid: Mids
    hit_mids: list


class Burst(NamedTuple):
    """A burst of taker trades (queue_fit.bursts): its first trade's time,
    pieces per price, all its shares, the mid PRE_SWEEP_S before it (None
    before the first mid) and its last trade's time."""

    at: float
    pieces: tuple
    shares: float
    pre_mid: Optional[float]
    last: float


class Resting(NamedTuple):
    """One of the walk's orders: its price and the shares it has left."""

    price: float
    shares: float


class Run(NamedTuple):
    """One arm over one run. reward, bought, sold and carry in USD; held the
    favorite shares at the run's end, cost their cost; marks the mid each
    HORIZONS after the run's end, None past the last mid; hold_s the
    expected hold of what is held. decisions and bids count the decisions
    and those resting a favorite bid, bid_usd and room_usd sum those bids
    and the capital left beside them; peak is the most inventory at cost
    over the capital; fills counts the bursts that bought."""

    reward: float
    bought: float
    bought_shares: float
    sold: float
    sold_shares: float
    carry: float
    held: float
    cost: float
    marks: tuple
    hold_s: float
    decisions: int
    bids: int
    bid_usd: float
    room_usd: float
    peak: float
    fills: int

    def net(self, horizon, hurdle_rate, held=False):
        """The run's net at a HORIZONS index, None without a mark there:
        what is held is marked at the mid then, less its carry to then or,
        held, over its expected hold."""
        mark = self.marks[horizon]
        if mark is None:
            return None
        hold = self.hold_s if held else HORIZONS[horizon][1]
        return (self.reward - self.bought + self.sold - self.carry + self.held * mark
                - self.cost * math.log1p(hurdle_rate) * hold / YEAR_S)


def market_bursts(sweeps, mids):
    """The sweeps chained as queue_fit chains them, each Burst with the mid
    before it and its last trade's time."""
    chained = queue_fit.bursts(sweeps)
    starts = [b.at for b in chained[1:]] + [math.inf]
    times = [s.at for s in sweeps]
    out = []
    for burst, following in zip(chained, starts):
        last = times[bisect.bisect_left(times, following) - 1]
        out.append(Burst(burst.at, burst.pieces, sum(p.size for p in burst.pieces),
                         mids.at(burst.at - PRE_SWEEP_S), last))
    return out


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


def runs(samples):
    """The samples split into runs of quoting: a gap over RUN_GAP_S ends one."""
    found, run = [], []
    for s in samples:
        if run and s.at.timestamp() - run[-1].at.timestamp() > RUN_GAP_S:
            found.append(run)
            run = []
        run.append(s)
    return found + [run] if run else found


def expected_hold_s(Q, years_left):
    """How long a fill of Q shares is held, on average: the farmer's ask
    sells it at fill_cost.sales_per_day, cut short at resolution."""
    rate = fill_cost.sales_per_day(Q)
    return -math.expm1(-rate * years_left * YEAR_DAYS) / rate * DAY_S


class Plans:
    """What a decision at each of a run's samples plans from, worked out once
    for every arm: (FarmMarket with this cycle's tick, zones and book mid,
    the book without our orders, the Outlook), or None on a one-sided book."""

    def __init__(self, market, cfg):
        self.market, self.cfg, self._seen = market, cfg, {}

    def at(self, sample):
        key = sample.at
        if key not in self._seen:
            self._seen[key] = self._plan_inputs(sample)
        return self._seen[key]

    def _plan_inputs(self, sample):
        market, cfg = self.market, self.cfg
        t = sample.at.timestamp()
        tick = tick_of(sample, market.fm.tick)
        levels = others_book(sample, tick)
        fm = with_book_mid(market.fm._replace(tick=tick, zones=Zones(
            fill_cost.years_until(market.fm.end_date, sample.at), sample.mid)), levels)
        if fm.book_mid is None:
            return None
        history = history_as_of(market, t, cfg.min_fav)
        return fm, levels, market_outlook(fm, None, history, DEFAULT_PRIOR, cfg, sample.at)


def _fill(position, shares, price):
    """The position after buying shares at price."""
    total = position.fav_shares + shares
    return position._replace(fav_shares=total, fav_avg=(
        position.fav_shares * position.fav_avg + shares * price) / total)


def walk_run(market, run, cfg, rules, plans, checks=None, trace=None):
    """One arm through one run of book samples: its Run. checks, when given,
    counts the harness checks of the today arm (_check_current); trace, a
    list, gets each decision's (time, favorite bid Resting or None)."""
    start, end = run[0].at.timestamp(), run[-1].at.timestamp() + farm_books.BOOK_SAMPLE_S
    events = sorted(
        [(b.at, 1, b) for b in _between(market.bursts, start, end)]
        + [(b.at, 2, b) for b in _between(market.buys, start, end)],
        key=lambda e: (e[0], e[1]))
    position, watch, fill_mid = Position(), NO_FILLS, None
    bid = ask = None
    rate = reward = bought = bought_shares = sold = sold_shares = carry = 0.0
    decisions = bids = fills = 0
    bid_usd = room_usd = peak = 0.0
    last, decided, due = start, None, True
    e = 0
    samples = run + [None]
    for sample, following in zip(run, samples[1:]):
        t = sample.at.timestamp()
        if due or t >= decided + DECISION_S:
            due, decided = False, t
            inputs = plans.at(sample)
            bid = ask = None
            rate = 0.0
            if inputs is not None:
                fm, levels, outlook = inputs
                arm_fm, arm_cfg, arm_outlook = rules.terms(fm, cfg, outlook)
                fill_mid = held_fill_mid(fill_mid, fm.book_mid, position, fm)
                arm_fm = arm_fm._replace(bid_ceiling=rules.ceiling(
                    watch, *held_cost(position, None), fm.tick, sample.at))
                plan = target_orders(arm_fm, levels, position, arm_cfg, True, arm_outlook,
                                     bid_without_history=True, fill_mid=fill_mid)
                by = {i.slot: i for i in plan.intents}
                if Slot.FAV_BID in by:
                    bid = Resting(by[Slot.FAV_BID].price, by[Slot.FAV_BID].size)
                    bids += 1
                    bid_usd += bid.price * bid.shares
                    room_usd += capital_room(arm_fm, position)
                if Slot.FAV_ASK in by:
                    ask = Resting(by[Slot.FAV_ASK].price, by[Slot.FAV_ASK].size)
                best = plan.fav.best
                rate = best.reward_per_day if best else plan.fav.baseline_per_day
                if trace is not None:
                    trace.append((t, bid))
                if checks is not None and position.fav_shares == 0:
                    _check_current(checks, bid and bid.price, sample, fm.tick, market.sole_bid)
            decisions += 1
        until = following.at.timestamp() if following else end
        while e < len(events) and events[e][0] < until:
            at, kind, item = events[e]
            e += 1
            dt = at - last
            reward += rate * dt / DAY_S
            carry += position.fav_shares * position.fav_avg * math.log1p(cfg.hurdle_rate) \
                * dt / YEAR_S
            last = at
            when = _utc(at)
            if kind == 1 and bid is not None:
                shares = min(bid.shares, sum(p.size for p in item.pieces
                                             if p.price < bid.price - EPS))
                if shares > 0:
                    position = _fill(position, shares, bid.price)
                    bought, bought_shares, fills = (bought + shares * bid.price,
                                                    bought_shares + shares, fills + 1)
                    watch = watch_listing(watch, [Gone(BUY, "bid", bid.price, shares, when)], when)
                    watch = watch_positions(watch, shares, 0.0, None, None, when).watch
                    fill_mid = max(fill_mid or 0.0, item.pre_mid or 0.0) or None
                    peak = max(peak, position.fav_shares * position.fav_avg / market.fm.capital)
                    bid, rate, due = None, 0.0, True
            elif kind == 2 and ask is not None and position.fav_shares > 0:
                shares = min(ask.shares, position.fav_shares,
                             sum(p.size for p in item.pieces if p.price > ask.price + EPS))
                if shares > 0:
                    left = round(position.fav_shares - shares, 6)
                    position = position._replace(fav_shares=left,
                                                 fav_avg=position.fav_avg if left > 0 else 0.0)
                    sold, sold_shares = sold + shares * ask.price, sold_shares + shares
                    watch = watch_listing(watch, [Gone(SELL, "ask", ask.price, shares, when)],
                                          when)
                    watch = watch_positions(watch, 0.0, shares, None, None, when).watch
                    ask = ask._replace(shares=ask.shares - shares) if ask.shares > shares else None
                    due = True
        dt = until - last
        reward += rate * dt / DAY_S
        carry += position.fav_shares * position.fav_avg * math.log1p(cfg.hurdle_rate) \
            * dt / YEAR_S
        last = until
    last_mid = market.mids[-1][0] if market.mids else -math.inf
    marks = tuple(market.mid.at(end + seconds) if end + seconds <= last_mid else None
                  for _, seconds in HORIZONS)
    held = position.fav_shares
    years = fill_cost.years_until(market.fm.end_date, _utc(end))
    return Run(reward, bought, bought_shares, sold, sold_shares, carry, held,
               held * position.fav_avg, marks, expected_hold_s(held, years) if held > 0 else 0.0,
               decisions, bids, bid_usd, room_usd, peak, fills)


def _between(bursts, start, end):
    lo = bisect.bisect_left(bursts, start, key=lambda b: b.at)
    hi = bisect.bisect_left(bursts, end, key=lambda b: b.at)
    return bursts[lo:hi]


def _check_current(checks, price, sample, tick, sole_bid):
    """At a flat decision of the today arm: the bid it rests beside the live
    FAV_BID of the sample, when there was one."""
    live = [p for name, p, _ in sample.ours if name == "FAV_BID"]
    if not live:
        return
    key = "sole" if sole_bid else "plain"
    checks[f"live {key}"] += 1
    if price is None:
        checks[f"live {key} no current bid"] += 1
        return
    gap = price - max(live)
    checks[f"live {key} both"] += 1
    checks[f"live {key} gap sum"] += gap
    checks[f"live {key} within tick"] += abs(gap) <= tick + EPS


def walk_market(market, samples, cfg, arms=ARMS):
    """({arm name: [Run]} over the market's runs, the harness checks)."""
    checks = Counter()
    out = {rules.name: [] for rules in arms}
    for run in runs(samples):
        if run[0].at.timestamp() < market.start:
            checks["runs before trades"] += 1
            continue
        plans = Plans(market, cfg)
        checks["runs"] += 1
        for rules in arms:
            out[rules.name].append(walk_run(market, run, cfg, rules, plans,
                                            checks if rules is TODAY else None))
    return out, checks


def farm_terms(db_path, hurdle_rate=HURDLE_RATE):
    """(FarmConfig, {condition_id: farm row dict}): the farmer's status
    config over the parser's defaults, since a status row from before a
    setting existed lacks it, at hurdle_rate rather than the live one."""
    conn = queue_fit._read_only(db_path)
    conn.row_factory = sqlite3.Row
    try:
        stored = json.loads(conn.execute("SELECT config FROM farmer_status").fetchone()["config"])
        rows = {r["condition_id"]: {"bid_multiple": None, **dict(r)}
                for r in conn.execute("SELECT * FROM markets")}
    finally:
        conn.close()
    cfg = config_from(build_parser().parse_args([]))
    cfg = cfg._replace(**{k: v for k, v in stored.items() if k in FarmConfig._fields})
    return cfg._replace(hurdle_rate=hurdle_rate), rows


def farm_market_of(row, cfg):
    """The FarmMarket of a farm.db row, its terms from the scan's market
    record and approved_terms; None without a market record, or without
    p_true: a market with none has no samples from before the farmer could
    follow one."""
    market = (json.loads(row["metrics"]) or {}).get("market") if row["metrics"] else None
    if not market or row["p_true"] is None:
        return None
    end_date = datetime.fromisoformat(row["end_date"] or market["end_date"])
    capital, bid_multiple = approved_terms(row, cfg)
    return FarmMarket(
        condition_id=row["condition_id"], slug=row["slug"] or market.get("slug", ""),
        question=row["question"] or "", end_date=end_date, neg_risk=False,
        tick=float(market["tick"]), fav_token_id=market["fav_token_id"],
        fav_outcome=market.get("fav_outcome", ""), rate_per_day=market["rate_per_day"],
        v=market["v"], min_size=market["min_size"], long_token_id="",
        p_true=row["p_true"], naked_budget_usd=row["naked_budget_usd"] or 0.0,
        expires_at=end_date, capital=capital, zones=Zones(0.0), bid_multiple=bid_multiple)


def _sweeps(rows):
    return sorted((Sweep(at, tuple(Piece(p, s) for p, s in pieces)) for at, pieces in rows),
                  key=lambda s: s.at)


def market_of(record, row, cfg):
    fm = farm_market_of(row, cfg)
    if fm is None:
        return None
    sells, buys = _sweeps(record["sells"]), _sweeps(record["buys"])
    hits = [Hit(s.at, min(p.price for p in s.pieces)) for s in sells]
    mids = sorted((t, p) for t, p in record["mids"])
    mid = Mids(mids)
    return Market(record["condition_id"], fm, bool(row["sole_bid"]), record["start"], sells,
                  hits, market_bursts(sells, mid) if sells else [],
                  market_bursts(buys, mid) if buys else [], mids, mid, hit_mids(hits, mids))


def load_cache(cache_dir, limit=None, only=None):
    records = []
    with (Path(cache_dir) / CACHE_FILE).open() as lines:
        for line in lines:
            r = json.loads(line)
            if only is None or r["condition_id"] in only:
                records.append(r)
    records.sort(key=lambda r: r["condition_id"])
    return records[:limit]


_shared = None


def _share(books_path, cfg, arms):
    global _shared
    _shared = books_path, cfg, arms


def _job(args):
    record, row = args
    books_path, cfg, arms = _shared
    started = time.perf_counter()
    market = market_of(record, row, cfg)
    conn = queue_fit._read_only(books_path)
    try:
        samples = farm_books.read_books(conn, market.condition_id)
    finally:
        conn.close()
    walked, checks = walk_market(market, samples, cfg, arms)
    checks["seconds"] += time.perf_counter() - started
    return market.condition_id, (walked, checks)


def run(records, rows, books_path, cfg, arms=ARMS, workers=1):
    """({condition_id: ({arm: [Run]}, checks)}, cached markets left out)."""
    kept = [(r, rows[r["condition_id"]]) for r in records
            if r["condition_id"] in rows and r["mids"]
            and farm_market_of(rows[r["condition_id"]], cfg) is not None]
    if workers > 1:
        with Pool(workers, initializer=_share, initargs=(books_path, cfg, arms)) as pool:
            done = pool.map(_job, kept, chunksize=1)
    else:
        _share(books_path, cfg, arms)
        done = [_job(job) for job in kept]
    return dict(done), len(records) - len(kept)


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


def interval(values, level):
    """The central `level` interval of the values, None without values."""
    if not values:
        return None
    values, tail = sorted(values), (1 - level) / 2
    return values[int(tail * len(values))], values[max(int((1 - tail) * len(values)) - 1, 0)]


def resampled(diffs, reps=BOOTSTRAP_REPS, seed=0):
    """{condition_id: difference} summed, with 95% and 90% intervals from
    resampling the markets, and the differences."""
    ids = sorted(diffs)
    rng = random.Random(seed)
    drawn = [sum(diffs[cid] for cid in rng.choices(ids, k=len(ids)))
             for _ in range(reps if ids else 0)]
    return {"difference": sum(diffs.values()), "interval": interval(drawn, 0.95),
            "interval90": interval(drawn, 0.90), "markets": len(ids), "by_market": diffs}


def market_net(walked, arm, horizon, hurdle_rate, held=False):
    """An arm's net over a market's runs with a mark at the horizon."""
    nets = [r.net(horizon, hurdle_rate, held) for r in walked[arm]]
    return sum(n for n in nets if n is not None)


def compare(by_market, arm, base, horizon, hurdle_rate, held=False, reps=BOOTSTRAP_REPS):
    """arm less base in net over the markets, resampled by market."""
    return resampled({cid: market_net(w, arm, horizon, hurdle_rate, held)
                      - market_net(w, base, horizon, hurdle_rate, held)
                      for cid, (w, _) in by_market.items()}, reps)


def arm_totals(by_market, arm, horizon, hurdle_rate):
    """An arm's Run fields summed over the runs marked at the horizon, with
    its net, held net, peak inventory and effective multiple."""
    picked = [r for w, _ in by_market.values() for r in w[arm]
              if r.marks[horizon] is not None]
    sums = {f: sum(getattr(r, f) for r in picked)
            for f in ("reward", "bought", "bought_shares", "sold", "sold_shares", "carry",
                      "held", "cost", "decisions", "bids", "bid_usd", "room_usd", "fills")}
    peaks = [max((r.peak for r in w[arm]), default=0.0) for w, _ in by_market.values()]
    return {**sums, "runs": len(picked),
            "net": sum(r.net(horizon, hurdle_rate) for r in picked),
            "net_held": sum(r.net(horizon, hurdle_rate, True) for r in picked),
            "peak_max": max(peaks, default=0.0),
            "markets_over_capital": sum(p > 1 + EPS for p in peaks),
            "effective_multiple": sums["bid_usd"] / sums["room_usd"] if sums["room_usd"] else None}


def report(by_market, skipped, cfg, live, arms=ARMS, reps=BOOTSTRAP_REPS):
    checks = Counter()
    for _, c in by_market.values():
        checks.update(c)
    names = [a.name for a in arms]
    out = {"config": cfg._asdict(), "decision_s": DECISION_S, "arms": names,
           "skipped_markets": skipped, "markets": len(by_market), "checks": dict(checks),
           "live_fills": dict(zip(("events", "shares", "markets"), live)), "horizons": {}}
    h = cfg.hurdle_rate
    for i, (horizon, _) in enumerate(HORIZONS):
        block = {"arms": {a: arm_totals(by_market, a, i, h) for a in names}}
        if TODAY.name in names and COMBINED.name in names:
            block["verdict"] = {held: compare(by_market, COMBINED.name, TODAY.name, i, h,
                                              held == "held", reps)
                                for held in ("net", "held")}
            block["rule worth"] = {a: {held: compare(by_market, COMBINED.name, a, i, h,
                                                     held == "held", reps)
                                       for held in ("net", "held")}
                                   for a in names if a not in (TODAY.name, COMBINED.name)}
        out["horizons"][horizon] = block
    return out


def _num(value, spec):
    return "none" if value is None else format(value, spec)


def _usd(x):
    return f"{x:+,.2f}"


def _difference(d):
    def side(iv):
        return iv is not None and (iv[0] > 0 or iv[1] < 0)
    return (f"{_usd(d['difference']):>10} 95% {_fmt_interval(d['interval'], '+,.2f'):<22}"
            f"90% {_fmt_interval(d['interval90'], '+,.2f'):<22}"
            f"{'excludes 0' if side(d['interval']) else 'includes 0'}")


def render(r):
    c = r["checks"]
    runs_n = c.get("runs", 0)
    lines = [f"favorite bid pricing walk: {r['markets']} markets, {runs_n} runs "
             f"({c.get('runs before trades', 0)} before a market's trades are complete left "
             f"out; {r['skipped_markets']} cached markets without terms or mids); decisions every "
             f"{r['decision_s']:g} s and after each fill or sale; hurdle "
             f"{r['config']['hurdle_rate']:g}",
             f"walk time {c.get('seconds', 0.0):.0f} s, "
             f"{c.get('seconds', 0.0) / r['markets'] if r['markets'] else 0:.1f} s a market"]
    for horizon, h in r["horizons"].items():
        lines += ["", f"H = {horizon}: runs whose end is {horizon} before the last mid",
                  f"{'arm':<20}{'runs':>6}{'bids':>8}{'reward $':>11}{'bought $':>12}"
                  f"{'sold $':>11}{'carry $':>9}{'held sh':>9}{'net $':>11}{'held $':>11}"
                  f"{'peak':>6}{'>1x':>5}{'eff x':>7}"]
        for name, a in h["arms"].items():
            lines.append(
                f"{name:<20}{a['runs']:>6}{a['bids']:>8}{a['reward']:>11,.2f}"
                f"{a['bought']:>12,.0f}{a['sold']:>11,.0f}{a['carry']:>9,.2f}"
                f"{a['held']:>9,.0f}{a['net']:>11,.2f}{a['net_held']:>11,.2f}"
                f"{a['peak_max']:>6.2f}{a['markets_over_capital']:>5}"
                f"{_num(a['effective_multiple'], '.2f'):>7}")
        if "verdict" in h:
            lines += ["", "combined less today (the verdict), resampled by market:"]
            lines += [f"  {kind:<20}{_difference(d)}" for kind, d in h["verdict"].items()]
            lines += ["", "combined less each arm (a rule's worth):"]
            for name, d in h["rule worth"].items():
                lines += [f"  {name:<20}{_difference(d['net'])}",
                          f"  {'  held':<20}{_difference(d['held'])}"]
    lines += ["", "harness checks:"]
    for key in ("plain", "sole"):
        n, both = c.get(f"live {key}", 0), c.get(f"live {key} both", 0)
        if n:
            near = c.get(f"live {key} within tick", 0)
            lines.append(
                f"  {'sole-bid' if key == 'sole' else 'other'} markets: {n} flat today-arm "
                f"decisions beside a live FAV_BID; today bids at {both}, within a tick at {near} "
                f"({near / n:.1%}), no bid at {c.get(f'live {key} no current bid', 0)}; mean today "
                f"less live {100 * c.get(f'live {key} gap sum', 0) / both if both else 0:+.3f}c "
                f"(the live farmer ran its own hurdle, not this one)")
    lf = r["live_fills"]
    today = r["horizons"][PRIMARY]["arms"].get(TODAY.name)
    if today:
        lines.append(f"  today arm bought {today['bought_shares']:,.0f} shares in "
                     f"{today['fills']} fills; the farmer logged {lf['events']} FAV_BID fills of "
                     f"{lf['shares']:,.0f} shares on {lf['markets']} markets over the sampled days")
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
    cfg, rows = farm_terms(args.db, args.hurdle_rate)
    arms = tuple(a for a in ARMS if not args.arms or a.name in args.arms)
    records = load_cache(args.cache, args.limit, set(args.markets) if args.markets else None)
    by_market, skipped = run(records, rows, args.books, cfg, arms, args.workers)
    live = live_fills(args.db, *sampled_span(args.books), by_market)
    result = report(by_market, skipped, cfg, live, arms, args.reps)
    result["seconds"] = time.perf_counter() - started
    text = render(result)
    print(text, end="")
    if args.report:
        Path(args.report).write_text(text)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))
        print(f"wrote {args.json}")


def fetch_market(http, listing, now):
    """One market's cache record: its taker sells and buys over both /trades
    listings from LEAD_DAYS before its first sample, or from where the
    listings are complete, and its mids from a minute mid before that to
    now, kept where they change and at the last point read."""
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
    buys = [b for b in reward_screener.parse_buys(taker, every, listing.fav_token_id)
            if b.at >= start]
    read = reward_screener.fetch_mids(http, listing.fav_token_id,
                                      start - PRE_SWEEP_S - reward_screener.MIDS_LEAD_S, end)
    mids = changes(read)
    if read and mids[-1][0] != read[-1][0]:
        mids.append(list(read[-1]))
    return {"condition_id": cid, "fav_token_id": listing.fav_token_id, "tick": listing.tick,
            "read_at": now.isoformat(), "start": start,
            "sells": [[s.at, [list(p) for p in s.pieces]] for s in sells],
            "buys": [[b.at, [list(p) for p in b.pieces]] for b in buys], "mids": mids}


def fetch(found, cache_dir, now, http):
    """Cache every market not cached yet, one JSON line each; a record from
    before buys were cached is dropped and fetched again, and a failed
    market is reported and left for the next run."""
    path = Path(cache_dir) / CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = [line for line in path.read_text().splitlines()
            if "buys" in json.loads(line)] if path.exists() else []
    path.write_text("".join(line + "\n" for line in kept))
    done = {json.loads(line)["condition_id"] for line in kept}
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


SIZE_POINTS = 40
SIZE_CHECKS = (100.0, 300.0, 600.0, 1000.0, 2000.0, 4000.0)


def sweep_sizes(record, floor):
    """The shares each burst of a cached market sold strictly under a bid
    EXIT_DISTANCE below the mid before it, for the bursts that reached there
    from a mid at or above floor: the sweeps fill_cost counts at that
    distance, sized as they would fill a bid there."""
    sells = sorted((Sweep(at, tuple(Piece(p, s) for p, s in pieces))
                    for at, pieces in record["sells"]), key=lambda s: s.at)
    if not sells or not record["mids"]:
        return []
    found = []
    for b in market_bursts(sells, Mids(record["mids"])):
        if b.pre_mid is None or b.pre_mid < floor:
            continue
        under = b.pre_mid - fill_cost.EXIT_DISTANCE - EPS
        below = sum(p.size for p in b.pieces if p.price < under)
        if below > 0:
            found.append(below)
    return found


def quantile_table(sizes, points=SIZE_POINTS):
    """The means of `points` equal-probability bins of the sizes, smallest
    first, or the sizes themselves when there are fewer. Bin means rather than
    midpoints, so the table's mean is the sizes' mean: the tail of a
    heavy-tailed sweep is what fills a large bid."""
    xs = sorted(sizes)
    if len(xs) < points:
        return tuple(round(x, 1) for x in xs)
    edges = [i * len(xs) // points for i in range(points + 1)]
    return tuple(round(sum(xs[a:b]) / (b - a), 1) for a, b in zip(edges, edges[1:]))


def sizes_text(records, floor, now):
    """fill_cost.SWEEP_SIZES as source text with its provenance, and
    E[min(Q, X)] at SIZE_CHECKS beside the FILL events' fractions."""
    per = {r["condition_id"]: sweep_sizes(r, floor) for r in records}
    sizes = [x for found in per.values() for x in found]
    if not sizes:
        return "no sweeps in the cache\n"
    table = quantile_table(sizes)
    rows = [", ".join(f"{x:g}" for x in table[i:i + 10]) for i in range(0, len(table), 10)]
    xs = sorted(sizes)
    lines = [f"# The means of {SIZE_POINTS} equal-probability bins of the shares the sell",
             f"# bursts sold more than EXIT_DISTANCE under the mid before them, from a mid",
             f"# of at least {floor:g}: {len(sizes):,} bursts on "
             f"{sum(1 for found in per.values() if found)} of {len(records)} markets of",
             f"# the cache, measured {now:%Y-%m-%d} by",
             "#   python -m analysis.pricing_backtest sizes --cache work\\fill-pricing\\cache",
             f"# median {xs[len(xs) // 2]:,.0f}, mean {sum(xs) / len(xs):,.0f}, "
             f"p90 {xs[int(0.9 * (len(xs) - 1))]:,.0f}, p99 {xs[int(0.99 * (len(xs) - 1))]:,.0f} shares.",
             "SWEEP_SIZES = (", *(f"    {row}," for row in rows), ")", "",
             "E[min(Q, X)] over the table: " + ", ".join(
                 f"Q {q:,.0f}: {fill_cost.sweep_fill(q, table):,.0f}" for q in SIZE_CHECKS)]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(prog="python -m analysis.pricing_backtest",
                                     description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sizes", help="print fill_cost.SWEEP_SIZES measured on the cache")
    p.add_argument("--cache", required=True)
    p.add_argument("--floor", type=float, default=DEFAULT_PRIOR.floor,
                   help="only sweeps from a mid at or above this")
    p = sub.add_parser("fetch", help="cache the sells and mids of the markets with book samples")
    p.add_argument("--db", required=True, help="farm.db, for each market's favorite and tick")
    p.add_argument("--books", required=True, help="farm_books.db")
    p.add_argument("--cache", required=True)
    p = sub.add_parser("run", help="walk the arms through the book samples")
    p.add_argument("--db", required=True, help="farm.db, for the terms and the config")
    p.add_argument("--books", required=True, help="farm_books.db")
    p.add_argument("--cache", required=True)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--limit", type=int, help="only the first N cached markets")
    p.add_argument("--markets", nargs="*", help="only these condition ids")
    p.add_argument("--reps", type=int, default=BOOTSTRAP_REPS, help="bootstrap replicates")
    p.add_argument("--hurdle-rate", type=float, default=HURDLE_RATE,
                   help="yearly return capital must beat, in place of the farmer's "
                        f"(default: {HURDLE_RATE:g})")
    p.add_argument("--arms", nargs="*", choices=[a.name for a in ARMS],
                   help="only these arms (default: all)")
    p.add_argument("--json", help="also write the result to this file")
    p.add_argument("--report", help="also write the text report to this file")
    args = parser.parse_args()
    if args.command == "sizes":
        print(sizes_text(load_cache(args.cache), args.floor, datetime.now(timezone.utc)), end="")
    elif args.command == "fetch":
        found, missing = queue_fit.listings(args.db, args.books)
        if missing:
            print(f"{len(missing)} markets with samples have no favorite in {args.db}", flush=True)
        with httpx.Client() as http:
            fetch(found, args.cache, datetime.now(timezone.utc), http)
    else:
        run_backtest(args)


if __name__ == "__main__":
    main()
