"""
Backtest the favorite exit model against the buys that came after.

  fetch      read each sampled market's trades and mids once into --cache
  search     replay the parameter grid and every arm offline, choose the
             parameters, refit the prior and write the report
  calibrate  print the hillclimb metric at fixed parameters: the model's
             predicted over realised sales by cell, the same forecast at
             the floor's and the anchor's asks, and its gain guards

A case is a market at a past time t holding a hypothetical inventory bought
a cent under the mid, as a swept favorite bid buys it. Each arm prices one
favorite ask for it from what was known at t: the production ExitModel on
the tape as of t, with a prior fit as of t on the markets of the other
folds, beside fixed-rule and alternative arms. The ask then rests for a
horizon against the buys that came after. Queue ahead at the ask's price
is not modelled, for every arm alike: by default every buy at or over the
ask fills it, and the strictly-above fill rule bounds the queue's cost.
"""

import argparse
import bisect
import json
import math
import random
import sqlite3
import statistics
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path
from typing import NamedTuple, Optional

import httpx

from farm import exit_value, reward_screener
from farm.exit_value import (
    DAY_S,
    PRIOR_MIN_MARKETS,
    SIZE_BASE,
    SIZE_GRID,
    YEAR_DAYS,
    BuyTape,
    ExitCurve,
    ExitModel,
    ExitParams,
    ExitPolicy,
    ExitPrior,
    Holding,
    TapeView,
    fit_exit_prior,
    sales_rate,
)

# The live farmer's settings: min_capture, hurdle_rate, ask_offset, expiry buffer.
POLICY = ExitPolicy(min_capture=0.005, hurdle_rate=0.40)
ASK_OFFSET = 0.01
EXPIRY_BUFFER_DAYS = 7.0
# A swept favorite bid fills this far under the mid.
COST_UNDER_MID = 0.01
FAV_RANGE = (0.90, 0.995)
EVERY_DAYS = 2.0
WARMUP_DAYS = 7.0
SIZES = (100.0, 1000.0)
HORIZONS = (14.0, 30.0)
FOLDS = 5
GRID = {
    "half_life_days": (3.0, 5.0, 7.0, 10.0, 14.0, 21.0, 30.0, 60.0),
    "arrival_days": (0.0, 1.0, 3.0, 10.0, 30.0),
    "mark_buys": (0.0, 1.0, 3.0, 10.0, 30.0),
}
BOOTSTRAP_REPS = 2000
# The backtest reads trades this far back, past the farmer's 60 days, so quiet
# markets carry enough tape to replay.
FETCH_DAYS = 365.0
CACHE_FILE = "markets.jsonl"
# The distance arm prices a buy against the mid this long before it.
PRE_SWEEP_S = 60.0
# Ask price minus the mid it rested against, negated: a buy that paid more
# over its mid takes asks further over theirs.
DISTANCE_GRID = (-0.05, -0.03, -0.02, -0.015, -0.01, -0.0075, -0.005, -0.0025,
                 0.0, 0.0025, 0.005, 0.01, 0.02, 0.05)
# The prototype: every buy on tape, no decay, no prior.
PROTOTYPE = ExitParams(half_life_days=1e6, arrival_days=0.0, mark_buys=0.0)


class Mids:
    """A token's mid as a step function of unix time."""

    def __init__(self, points):
        points = sorted(points)
        self.times = [t for t, _ in points]
        self.prices = [p for _, p in points]

    def at(self, t):
        """The last mid at or before t, None before the first."""
        i = bisect.bisect_right(self.times, t)
        return self.prices[i - 1] if i else None


class Market(NamedTuple):
    condition_id: str
    end_date: datetime
    tick: float
    tape: BuyTape
    mids: Mids

    @property
    def expires_at(self):
        return self.end_date - timedelta(days=EXPIRY_BUFFER_DAYS)


class Case(NamedTuple):
    """Market at unix time t holding size shares bought at cost; lowest is the
    lowest ask the farmer may rest, over cost by min_capture."""

    market: Market
    t: float
    size: float
    mid: float
    cost: float
    lowest: float
    fold: int


class Outcome(NamedTuple):
    """An ask rested over a horizon: proceeds of its fills and the unsold
    remainder at the mid, discounted at the hurdle, per share."""

    price: float
    pv_per_share: float
    sold_fraction: float


class Pick(NamedTuple):
    """An arm's ask, and the share of the inventory a year it expects to sell
    there when it is a model."""

    price: float
    rate: Optional[float] = None


def _ceil(price, tick):
    return round(math.ceil(round(price / tick, 6)) * tick, 10)


def _floor(price, tick):
    return round(math.floor(round(price / tick, 6)) * tick, 10)


def folds(markets):
    """{condition_id: fold}, dealt round the markets in id order."""
    return {cid: i % FOLDS for i, cid in enumerate(sorted(m.condition_id for m in markets))}


def cases(markets, horizon_days, sizes=SIZES, every_days=EVERY_DAYS):
    """Every every_days of each market, on one clock for all, from a week after
    its first buy to a horizon before its tape or its selling ends, where its
    mid is a favorite's in FAV_RANGE; one case per size."""
    fold, step, found = folds(markets), every_days * DAY_S, []
    for m in markets:
        if not m.tape.sweeps:
            continue
        first = m.tape.sweeps[0].at + WARMUP_DAYS * DAY_S
        last = min(m.tape.read_at, m.expires_at).timestamp() - horizon_days * DAY_S
        for k in range(math.ceil(first / step), math.floor(last / step) + 1):
            t = k * step
            mid = m.mids.at(t)
            if mid is None or not FAV_RANGE[0] <= mid <= FAV_RANGE[1]:
                continue
            cost = _floor(mid - COST_UNDER_MID, m.tick)
            lowest = max(_ceil(cost + POLICY.min_capture, m.tick), _ceil(cost + m.tick, m.tick))
            found.extend(Case(m, t, size, mid, cost, lowest, fold[m.condition_id])
                         for size in sizes)
    return found


def with_prior(found, markets):
    """The cases whose other folds hold enough markets with tape at t to fit a
    prior; the same for every half-life."""
    fold = folds(markets)
    starts = [(fold[m.condition_id], m.tape.first_at) for m in markets]
    exposed = lambda f, t: sum(1 for g, first in starts if g != f and first < t)
    return [c for c in found if exposed(c.fold, c.t) >= PRIOR_MIN_MARKETS]


# Which of a later buy's pieces fill a resting ask: at-or-above assumes the ask
# is first in its queue, strictly-above counts only the shares a buy took past
# it, which it would have taken from the ask whatever the queue.
FILLS = {
    "at-or-above": lambda piece, ask: piece >= ask - 1e-9,
    "strictly-above": lambda piece, ask: piece > ask + 1e-9,
}
DEFAULT_FILL = "at-or-above"


def simulate(price, case, horizon_days, fill=DEFAULT_FILL):
    """The ask at price rested horizon_days from case.t for case.size: each
    later buy sells the shares it took that FILLS[fill] says take the ask, at
    price, until none are left; the rest are marked at the mid the horizon
    ends at."""
    m, t = case.market, case.t
    end, delta = t + horizon_days * DAY_S, math.log1p(POLICY.hurdle_rate)
    sweeps, fills = m.tape.sweeps, FILLS[fill]
    left, pv = case.size, 0.0
    for s in sweeps[bisect.bisect_right(sweeps, t, key=lambda s: s.at):]:
        if s.at > end or left <= 0:
            break
        took = sum(p.size for p in s.pieces if fills(p.price, price))
        if took:
            sold = min(left, took)
            pv += sold * price * math.exp(-delta * (s.at - t) / DAY_S / YEAR_DAYS)
            left -= sold
    pv += left * m.mids.at(end) * math.exp(-delta * horizon_days / YEAR_DAYS)
    return Outcome(price, pv / case.size, 1 - left / case.size)


class DistancePrior(NamedTuple):
    """ExitPrior's shape over DISTANCE_GRID instead of yields."""

    arrival_per_day: float
    marks: tuple

    def mark(self, key, size):
        i = bisect.bisect_right(DISTANCE_GRID, key)
        if i == 0:
            return 0.0
        x = math.log(size)
        row = lambda j: exit_value._lerp(exit_value._LOG_SIZES, self.marks[j], x)
        if i == len(DISTANCE_GRID):
            return row(i - 1)
        lo = row(i - 1)
        return lo + ((key - DISTANCE_GRID[i - 1]) / (DISTANCE_GRID[i] - DISTANCE_GRID[i - 1])
                     * (row(i) - lo))


class DistanceView(TapeView):
    """A TapeView whose pieces are keyed by the mid before their buy minus
    their price, not by yield: the estimator of the farmer's old mid offset."""

    def __init__(self, tape, mids, half_life_days):
        super().__init__(tape, math.inf, half_life_days)
        pieces = sorted((_key(mids, s.at, p.price), i, p.size)
                        for i, s in enumerate(tape.sweeps) for p in s.pieces)
        self.yields = [k for k, _, _ in pieces]
        self._pieces = [(i, size) for _, i, size in pieces]


def _key(mids, at, price):
    mid = mids.at(at - PRE_SWEEP_S)
    return (price if mid is None else mid) - price


def fit_distance_prior(views):
    """fit_exit_prior's rules over distance views; None under PRIOR_MIN_MARKETS."""
    usable = [v for v in views if v.D > 0]
    if len(usable) < PRIOR_MIN_MARKETS:
        return None
    seen = [v for v in usable if v.W >= 1]
    marks = tuple(
        tuple(sum(v.cum(size)[bisect.bisect_right(v.yields, k)] / v.W for v in seen) / len(seen)
              if seen else 0.0 for size in SIZE_GRID)
        for k in DISTANCE_GRID)
    return DistancePrior(statistics.median(v.W / v.D for v in usable), marks)


class DistanceModel(ExitModel):
    """The ExitModel with every ask priced by its distance from the mid at t."""

    def __init__(self, view, prior, params, case):
        m = case.market
        super().__init__(None, prior, params, POLICY, case.mid, m.end_date, m.expires_at,
                         _utc(case.t), m.tick, Holding(case.size, case.cost))
        self.view, self.mid = view, case.mid

    def curve(self, size):
        bucket = round(math.log(size) / math.log(SIZE_BASE))
        curve = self._curves.get(bucket)
        if curve is None:
            n = SIZE_BASE ** bucket
            rate, years, mid = sales_rate(self.view, self.prior, self.params, n), self.years, self.mid
            curve = self._curves[bucket] = ExitCurve(
                n, lambda r: rate(mid - math.exp(-r * years)), self.p_true, self.years,
                self.sell_years, self.delta, self.tick)
        return curve


def _utc(t):
    return datetime.fromtimestamp(t, timezone.utc)


class Replay:
    """What was known at a case's time for one half-life: each market's tape as
    of then, and the priors fit on the other folds. Holds one time at once, so
    replay cases in time order."""

    def __init__(self, markets, half_life_days):
        self.markets = markets
        self.half_life_days = half_life_days
        self._fold = folds(markets)
        self._t = None

    def _at(self, t):
        if self._t != t:
            self._t, self._tapes, self._priors = t, {}, {}
            self._views, self._distance_priors = {}, {}
        return self

    def tape(self, market, t):
        tapes = self._at(t)._tapes
        if market.condition_id not in tapes:
            tapes[market.condition_id] = market.tape.as_of(t)
        return tapes[market.condition_id]

    def _training(self, case):
        return [m for m in self.markets if self._fold[m.condition_id] != case.fold]

    def prior(self, case):
        priors = self._at(case.t)._priors
        if case.fold not in priors:
            priors[case.fold] = fit_exit_prior(
                [(self.tape(m, case.t), m.end_date) for m in self._training(case)],
                ExitParams(self.half_life_days, 0.0, 0.0), _utc(case.t))
        return priors[case.fold]

    def distance_view(self, market, t):
        views = self._at(t)._views
        if market.condition_id not in views:
            views[market.condition_id] = DistanceView(self.tape(market, t), market.mids,
                                                      self.half_life_days)
        return views[market.condition_id]

    def distance_prior(self, case):
        priors = self._at(case.t)._distance_priors
        if case.fold not in priors:
            priors[case.fold] = fit_distance_prior(
                [self.distance_view(m, case.t) for m in self._training(case)])
        return priors[case.fold]


def _zero_prior():
    return ExitPrior(0.0, tuple((0.0,) * len(SIZE_GRID) for _ in exit_value.YIELD_GRID), 0, None)


def _chosen(model, case):
    best = model.choose(case.lowest).best
    return Pick(best.price, best.sold_per_year / best.size)


def _exit_model(case, tape, prior, params):
    m = case.market
    return ExitModel(tape, prior, params, POLICY, case.mid, m.end_date, m.expires_at,
                     _utc(case.t), m.tick, Holding(case.size, case.cost))


def _model(case, replay, params):
    tape = replay.tape(case.market, case.t)
    return _chosen(_exit_model(case, tape, replay.prior(case), params), case)


def _prototype(case, replay, params):
    tape = replay.tape(case.market, case.t)
    return _chosen(_exit_model(case, tape, _zero_prior(), PROTOTYPE), case)


def _distance(case, replay, params):
    view = replay.distance_view(case.market, case.t)
    return _chosen(DistanceModel(view, replay.distance_prior(case), params, case), case)


def _fixed(price_of):
    return lambda case, replay, params: Pick(min(price_of(case), 1 - case.market.tick))


# Each arm prices an ask for a case from the replay as of its time.
ARMS = {
    "floor": _fixed(lambda c: c.lowest),
    "anchor": _fixed(lambda c: max(c.lowest, _ceil(c.mid + ASK_OFFSET, c.market.tick))),
    "lowest+2c": _fixed(lambda c: _floor(c.lowest + 0.02, c.market.tick)),
    "prototype": _prototype,
    "distance": _distance,
    "model": _model,
}
PARAMETRIC = ("model", "distance")
ALL_SIZES = "all"


def picks(markets, found, half_life_days, points, arm):
    """{params: [Pick per case]} of one arm at grid points sharing a half-life."""
    replay = Replay(markets, half_life_days)
    out = {p: [None] * len(found) for p in points}
    for i in sorted(range(len(found)), key=lambda i: found[i].t):
        for p in points:
            out[p][i] = ARMS[arm](found[i], replay, p)
    return out


class Book:
    """Outcomes of asks on the cases over one horizon under one fill rule,
    each simulated once."""

    def __init__(self, found, horizon_days, fill=DEFAULT_FILL):
        self.cases = found
        self.horizon_days = horizon_days
        self.fill = fill
        self._memo = {}

    def outcome(self, i, price):
        key = i, price
        if key not in self._memo:
            self._memo[key] = simulate(price, self.cases[i], self.horizon_days, self.fill)
        return self._memo[key]

    def outcomes(self, picked, among):
        return {i: self.outcome(i, picked[i].price) for i in among}

    def hindsight(self, among):
        """Each case's best fixed ask over the grid an ExitCurve searches."""
        best = {}
        for i in among:
            c = self.cases[i]
            stride = max(c.market.tick, exit_value.MIN_STRIDE)
            prices = [c.lowest] + [round(k * stride, 10) for k in range(
                math.floor(round(c.lowest / stride, 6)) + 1,
                math.floor(round((1 - c.market.tick) / stride, 6)) + 1)]
            best[i] = max((self.outcome(i, p) for p in prices), key=lambda o: o.pv_per_share)
        return best


class Score(NamedTuple):
    """Mean gain in cents a share over a baseline, its market-clustered
    standard error, and how many cases and markets it averages."""

    mean: float
    se: float
    cases: int
    markets: int


def _clusters(gains, found):
    by = {}
    for i, g in gains.items():
        s = by.setdefault(found[i].market.condition_id, [0.0, 0])
        s[0] += g
        s[1] += 1
    return list(by.values())


def score(gains, found):
    clusters = _clusters(gains, found)
    n = sum(k for _, k in clusters)
    mean = sum(s for s, _ in clusters) / n
    g = len(clusters)
    var = sum((s - k * mean) ** 2 for s, k in clusters) / n ** 2 * g / max(g - 1, 1)
    return Score(mean, math.sqrt(var), n, g)


def interval(gains, found, reps=BOOTSTRAP_REPS, seed=0):
    """The 95% bootstrap interval of the mean gain, markets resampled."""
    clusters = _clusters(gains, found)
    rng = random.Random(seed)
    means = []
    for _ in range(reps):
        draw = rng.choices(clusters, k=len(clusters))
        means.append(sum(s for s, _ in draw) / sum(k for _, k in draw))
    means.sort()
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def gains(a, b):
    """Cents a share that a earns over b, per case."""
    return {i: 100 * (a[i].pv_per_share - b[i].pv_per_share) for i in a}


_shared = None


def _share(markets, found, yield_grid):
    global _shared
    set_yield_grid(yield_grid)
    _shared = markets, found


def _picks_job(half_life_days, points, arm):
    return picks(*_shared, half_life_days, points, arm)


def grid_picks(markets, found, grid, arm, workers=1, yield_grid=None):
    """{params: [Pick per case]} of an arm over the grid, a half-life a job."""
    jobs = [(h, [ExitParams(h, d, m) for d in grid["arrival_days"] for m in grid["mark_buys"]],
             arm) for h in grid["half_life_days"]]
    if workers > 1:
        with Pool(min(workers, len(jobs)), initializer=_share,
                  initargs=(markets, found, yield_grid)) as pool:
            results = pool.starmap(_picks_job, jobs)
    else:
        results = [picks(markets, found, *job) for job in jobs]
    return {p: v for r in results for p, v in r.items()}


def search(markets, found, grid, horizon_days, arm="model", workers=1, yield_grid=None):
    """(params, {params: Score over the floor ask}): the argmax of the mean
    gain, then the simplest point within one standard error of it, the
    strongest prior first, then the longest half-life."""
    every = grid_picks(markets, found, grid, arm, workers, yield_grid)
    book, among = Book(found, horizon_days), range(len(found))
    floor = book.outcomes([ARMS["floor"](c, None, None) for c in found], among)
    surface = {p: score(gains(book.outcomes(v, among), floor), found) for p, v in every.items()}
    top = surface[max(surface, key=lambda p: surface[p].mean)]
    near = [p for p in surface if surface[p].mean >= top.mean - top.se]
    return max(near, key=lambda p: (p.arrival_days, p.mark_buys, p.half_life_days)), surface


def fit(markets, params, at):
    """The prior fit from every market's tape as of unix time at."""
    return fit_exit_prior([(m.tape.as_of(at), m.end_date) for m in markets], params, _utc(at))


def set_yield_grid(grid):
    """Price the prior on another yield grid, for the run of this process."""
    if grid:
        exit_value.YIELD_GRID = tuple(grid)
        exit_value._YIELDS = (0.0,) + tuple(grid)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def sample(conn, limit=250):
    """The markets to replay: every approved one, then the last scan's best
    ranked, up to limit. Each a dict of the scan's market record."""
    last = conn.execute("SELECT max(last_scanned) FROM markets").fetchone()[0]
    rows = conn.execute(
        "SELECT condition_id, metrics FROM markets WHERE metrics IS NOT NULL "
        "AND (status = 'approved' OR last_scanned = ?) "
        "ORDER BY status = 'approved' DESC, rank_score DESC", (last,)).fetchall()
    chosen = []
    for cid, metrics in rows:
        market = json.loads(metrics).get("market")
        if market and market.get("fav_token_id") and market.get("end_date"):
            chosen.append(market)
        if len(chosen) == limit:
            break
    return chosen


def fetch_market(http, market, now):
    """One market's cache record: its buy tape over both /trades listings and
    its minute mids from where the tape is complete."""
    cid, fav = market["condition_id"], market["fav_token_id"]
    cutoff = now.timestamp() - FETCH_DAYS * DAY_S
    every, every_from = reward_screener.fetch_trades(http, cid, False, cutoff)
    taker, taker_from = reward_screener.fetch_trades(http, cid, True, every_from)
    oldest = min((t.at for t in map(reward_screener.parse_trade, every + taker) if t),
                 default=now.timestamp())
    # A listing read to its end reaches the market's first trade, not the cutoff.
    start = max(every_from, taker_from, oldest)
    buys = [s for s in reward_screener.parse_buys(taker, every, fav) if s.at >= start]
    mids = reward_screener.fetch_mids(http, fav, start - reward_screener.MIDS_LEAD_S,
                                      now.timestamp())
    tape = BuyTape(buys, now, start)
    return {"condition_id": cid, "slug": market.get("slug"), "end_date": market["end_date"],
            "tick": market["tick"], "fav_token_id": fav,
            "tape": exit_value.tape_to_json(tape, len(tape.sweeps)),
            "mids": changes(mids)}


def changes(mids):
    """The (t, price) points at which the mid changed: the last mid at or
    before any time is the same, in a fraction of the points."""
    kept = []
    for t, p in mids:
        if not kept or p != kept[-1][1]:
            kept.append([t, p])
    return kept


def fetch(markets, cache_dir, now, http):
    """Cache every market not cached yet, one JSON line each; a failed
    market is reported and left for the next run."""
    path = Path(cache_dir) / CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(line)["condition_id"] for line in path.open()} if path.exists() else set()
    todo = [m for m in markets if m["condition_id"] not in done]
    print(f"{len(done)} cached, {len(todo)} to fetch")
    for i, market in enumerate(todo, 1):
        try:
            record = fetch_market(http, market, now)
        except Exception as e:
            print(f"  {market.get('slug') or market['condition_id']}: failed ({e})")
            continue
        with path.open("a") as out:
            out.write(json.dumps(record) + "\n")
        print(f"  {i}/{len(todo)} {market.get('slug')}: "
              f"{len(record['tape']['sweeps'])} sweeps, {len(record['mids'])} mids")
        time.sleep(0.5)


def load(cache_dir):
    """The cached markets, with a tape and mids."""
    markets = []
    with (Path(cache_dir) / CACHE_FILE).open() as lines:
        for line in lines:
            r = json.loads(line)
            tape = exit_value.tape_from_json(r["tape"])
            if tape is not None and r["mids"]:
                markets.append(Market(r["condition_id"], datetime.fromisoformat(r["end_date"]),
                                      float(r["tick"]), tape, Mids(r["mids"])))
    return markets


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def calibration(picked, outcomes, among, horizon_days, bins=10):
    """[(n, mean predicted, mean realised)] of the sold fraction by decile of
    the predicted, 1 - exp(-rate * horizon)."""
    rows = sorted((1 - math.exp(-picked[i].rate * horizon_days / YEAR_DAYS),
                   outcomes[i].sold_fraction) for i in among)
    out = []
    for b in range(bins):
        part = rows[len(rows) * b // bins:len(rows) * (b + 1) // bins]
        if part:
            out.append((len(part), statistics.fmean(p for p, _ in part),
                        statistics.fmean(r for _, r in part)))
    return out


# Cases split by the time from t to the market's end date.
PARTS = {
    "all": lambda c: True,
    "under 60d": lambda c: c.market.end_date.timestamp() - c.t < 60 * DAY_S,
    "60d+": lambda c: c.market.end_date.timestamp() - c.t >= 60 * DAY_S,
}


def slices(found, valid):
    """{(size, part): case indices} over each size and all sizes, each part."""
    sizes = sorted({found[i].size for i in valid}) + [ALL_SIZES]
    found_in = {(size, part): [i for i in valid if size in (ALL_SIZES, found[i].size)
                               and inside(found[i])]
                for size in sizes for part, inside in PARTS.items()}
    return {key: among for key, among in found_in.items() if among}


def evaluate(markets, found, chosen, horizons=HORIZONS):
    """Every arm at its chosen parameters on the cases, per horizon, size and
    time to resolution, and over all of them, the primary score:
    {(horizon, size, part): {"cases", "markets", "arms": {arm: stats},
    "versus": ..., "calibration": {arm: rows}}}."""
    picked = {arm: picks(markets, found, chosen[arm].half_life_days if arm in chosen else 0.0,
                         [chosen.get(arm)], arm)[chosen.get(arm)] for arm in ARMS}
    report = {}
    for horizon in horizons:
        book = Book(found, horizon)
        limit = lambda c: min(c.market.tape.read_at, c.market.expires_at).timestamp()
        valid = [i for i, c in enumerate(found) if c.t <= limit(c) - horizon * DAY_S]
        for (size, part), among in slices(found, valid).items():
            out = {arm: book.outcomes(picked[arm], among) for arm in ARMS}
            out["hindsight"] = book.hindsight(among)
            floor = out["floor"]
            arms = {}
            for arm, o in out.items():
                g = gains(o, floor)
                arms[arm] = {
                    "gain": score(g, found), "interval": interval(g, found),
                    "regret": statistics.fmean(100 * (out["hindsight"][i].pv_per_share
                                                      - o[i].pv_per_share) for i in among),
                    "never_filled": statistics.fmean(
                        o[i].sold_fraction == 0 and floor[i].sold_fraction > 0 for i in among),
                    "premium": statistics.fmean(100 * (o[i].price - found[i].lowest)
                                                for i in among),
                    "sold": statistics.fmean(o[i].sold_fraction for i in among)}
            versus = {}
            for other in ("anchor", "distance", "prototype", "lowest+2c"):
                g = gains(out["model"], out[other])
                versus[other] = {"gain": score(g, found), "interval": interval(g, found)}
            report[horizon, size, part] = {
                "cases": len(among), "markets": len({found[i].market.condition_id
                                                     for i in among}),
                "arms": arms, "versus": versus,
                "calibration": {arm: calibration(picked[arm], out[arm], among, horizon)
                                for arm in ("model", "distance", "prototype")}}
    return report


# ---------------------------------------------------------------------------
# Calibration: the hillclimb metric at fixed parameters
# ---------------------------------------------------------------------------

# A cell is an inventory and a time to resolution; its label names both.
CELLS = tuple((size, part) for size in SIZES for part in ("under 60d", "60d+"))
CALIBRATION_ARMS = ("floor", "anchor", "model")


def cell_label(size, part):
    return f"{size:g}/{part}"


def predicted_sold(pick, horizon_days):
    """The share of the inventory a model's pick expects to sell over the horizon."""
    return 1 - math.exp(-pick.rate * horizon_days / YEAR_DAYS)


class Totals(NamedTuple):
    """Sums over some cases: how many, the model's predicted and realised sold
    fraction, each arm's realised sold fraction and gain in cents a share
    over the floor ask, the model's gain over the anchor, and the model's
    predicted sold fraction at the floor's and the anchor's asks."""

    n: float
    predicted: float
    realised: float
    sold_floor: float
    sold_anchor: float
    sold_model: float
    gain_anchor: float
    gain_model: float
    model_over_anchor: float
    predicted_floor: float
    predicted_anchor: float

    @staticmethod
    def of(rows):
        return Totals(*map(sum, zip(*rows)))

    def stats(self):
        """The means, and the ratio of predicted to realised, None when nothing
        was realised: the model's at its own ask, and at each arm's ask."""
        mean = lambda x: x / self.n
        ratio = lambda predicted, realised: predicted / realised if realised > 0 else None
        return {"predicted": mean(self.predicted), "realised": mean(self.realised),
                "ratio": ratio(self.predicted, self.realised),
                "sold floor": mean(self.sold_floor), "sold anchor": mean(self.sold_anchor),
                "sold model": mean(self.sold_model), "gain floor": 0.0,
                "gain anchor": mean(self.gain_anchor), "gain model": mean(self.gain_model),
                "model over anchor": mean(self.model_over_anchor),
                "predicted floor": mean(self.predicted_floor),
                "ratio floor": ratio(self.predicted_floor, self.sold_floor),
                "predicted anchor": mean(self.predicted_anchor),
                "ratio anchor": ratio(self.predicted_anchor, self.sold_anchor),
                "predicted model": mean(self.predicted),
                "ratio model": ratio(self.predicted, self.realised)}


def _totals(i, picked, out, horizon_days):
    pv = lambda arm: out[arm][i].pv_per_share
    sold = lambda arm: out[arm][i].sold_fraction
    predicted = lambda arm: predicted_sold(picked[arm][i], horizon_days)
    return Totals(1.0, predicted("model"), sold("model"),
                  sold("floor"), sold("anchor"), sold("model"),
                  100 * (pv("anchor") - pv("floor")), 100 * (pv("model") - pv("floor")),
                  100 * (pv("model") - pv("anchor")), predicted("floor"), predicted("anchor"))


def forecast_picks(markets, found, params):
    """{arm: [Pick per case]} of the calibration arms at params: the model's
    own choice, and each fixed arm's ask carrying the model's sales rate
    there, read off the same curve."""
    replay = Replay(markets, params.half_life_days)
    out = {arm: [None] * len(found) for arm in CALIBRATION_ARMS}
    for i in sorted(range(len(found)), key=lambda i: found[i].t):
        case = found[i]
        model = _exit_model(case, replay.tape(case.market, case.t), replay.prior(case), params)
        out["model"][i] = _chosen(model, case)
        curve = model.curve(case.size)
        for arm in ("floor", "anchor"):
            v = curve.value_at(ARMS[arm](case, replay, params).price)
            out[arm][i] = Pick(v.price, v.sold_per_year / v.size)
    return out


def _by_market(found, among, row):
    """{condition_id: Totals} over the cases among, row(i) their Totals."""
    by = {}
    for i in among:
        by.setdefault(found[i].market.condition_id, []).append(row(i))
    return {cid: Totals.of(rows) for cid, rows in by.items()}


def _percentile_interval(values):
    """The 2.5th and 97.5th percentiles, None without values."""
    if not values:
        return None
    values = sorted(values)
    return values[int(0.025 * len(values))], values[max(int(0.975 * len(values)) - 1, 0)]


def _metric(stats_by_cell):
    """(M, cell): the largest |ln ratio| over the cells with a ratio."""
    found = [(abs(math.log(s["ratio"])), label) for label, s in stats_by_cell.items()
             if s["ratio"]]
    return max(found) if found else (None, None)


def calibrate(markets, found, params, horizon_days, fill, reps=BOOTSTRAP_REPS, seed=0):
    """The hillclimb metric of the model at params on the cases: per cell the
    mean predicted and realised sold fraction of its asks and their ratio; M,
    the largest |ln ratio| over the cells; the guards, the pooled gain in
    cents a share of the model over the floor ask and over the anchor; and
    per cell and arm the model's predicted sold fraction at that arm's ask
    over what the ask realised. Each carries a 95% bootstrap interval from
    one resample of the markets per replicate, so the cells, M, the guards
    and the arms vary together."""
    picked = forecast_picks(markets, found, params)
    book, among = Book(found, horizon_days, fill), range(len(found))
    out = {arm: book.outcomes(picked[arm], among) for arm in CALIBRATION_ARMS}
    row = lambda i: _totals(i, picked, out, horizon_days)
    pooled = _by_market(found, among, row)
    by_cell = {cell_label(size, part): _by_market(found, idx, row)
               for (size, part), idx in slices(found, among).items() if (size, part) in CELLS}
    point = {label: Totals.of(by.values()).stats() for label, by in by_cell.items()}
    point_pooled = Totals.of(pooled.values()).stats()

    rng, ids = random.Random(seed), sorted(pooled)
    reps_cells = {label: {key: [] for key in point[label]} for label in by_cell}
    reps_pooled = {key: [] for key in point_pooled}
    reps_m = []
    for _ in range(reps):
        draw = rng.choices(ids, k=len(ids))
        stats = {}
        for label, by in by_cell.items():
            rows = [by[cid] for cid in draw if cid in by]
            if rows:
                stats[label] = Totals.of(rows).stats()
                for key, value in stats[label].items():
                    if value is not None:
                        reps_cells[label][key].append(value)
        for key, value in Totals.of([pooled[cid] for cid in draw]).stats().items():
            reps_pooled[key].append(value)
        m, _ = _metric(stats)
        if m is not None:
            reps_m.append(m)

    cells = {}
    for label, by in by_cell.items():
        s, r = point[label], reps_cells[label]
        cells[label] = {
            "cases": int(sum(t.n for t in by.values())), "markets": len(by),
            "predicted": s["predicted"], "realised": s["realised"], "ratio": s["ratio"],
            "ratio_interval": _percentile_interval(r["ratio"]),
            "arms": {arm: {"sold": s[f"sold {arm}"], "gain": s[f"gain {arm}"],
                           "gain_interval": _percentile_interval(r[f"gain {arm}"]),
                           "predicted": s[f"predicted {arm}"], "ratio": s[f"ratio {arm}"],
                           "ratio_interval": _percentile_interval(r[f"ratio {arm}"])}
                     for arm in CALIBRATION_ARMS},
            "model over anchor": {"mean": s["model over anchor"],
                                  "interval": _percentile_interval(r["model over anchor"])}}
    m, cell = _metric(point)
    guard = lambda key, g: {"mean": point_pooled[key], "se": score(g, found).se,
                            "interval": _percentile_interval(reps_pooled[key])}
    return {
        "params": params._asdict(), "fill": fill, "horizon_days": horizon_days,
        "markets": len(markets), "cases": len(found), "markets_with_cases": len(pooled),
        "cells": cells,
        "M": {"value": m, "cell": cell, "interval": _percentile_interval(reps_m)},
        "guards": {"model over floor": guard("gain model", gains(out["model"], out["floor"])),
                   "anchor over floor": guard("gain anchor", gains(out["anchor"], out["floor"])),
                   "model over anchor": guard("model over anchor",
                                              gains(out["model"], out["anchor"]))},
        "bootstrap": {"reps": reps, "seed": seed},
    }


def _fmt_interval(interval, spec):
    return "[none]" if interval is None else f"[{interval[0]:{spec}}, {interval[1]:{spec}}]"


def render_calibration(r):
    p = r["params"]
    lines = [f"calibrate at half_life_days={p['half_life_days']:g} arrival_days="
             f"{p['arrival_days']:g} mark_buys={p['mark_buys']:g}, fill {r['fill']}, "
             f"horizon {r['horizon_days']:g}d: {r['markets']} markets, {r['cases']} cases on "
             f"{r['markets_with_cases']} of them", "",
             f"{'cell':<16}{'cases':>6}{'mkts':>6}{'predicted':>11}{'realised':>10}"
             f"{'ratio':>7}  [95%]"]
    for label, c in r["cells"].items():
        ratio = "none" if c["ratio"] is None else f"{c['ratio']:.3f}"
        lines.append(f"{label:<16}{c['cases']:>6}{c['markets']:>6}{c['predicted']:>11.3f}"
                     f"{c['realised']:>10.3f}{ratio:>7}  {_fmt_interval(c['ratio_interval'], '.3f')}")
    m = r["M"]
    value = "none" if m["value"] is None else f"{m['value']:.3f}"
    lines += ["", f"M = max |ln ratio| = {value} {_fmt_interval(m['interval'], '.3f')}"
              f" at {m['cell']}", "", "guards, pooled, cents a share:"]
    for key, g in r["guards"].items():
        lines.append(f"  {key:<18}{g['mean']:+.3f} {_fmt_interval(g['interval'], '+.3f')} "
                     f"se {g['se']:.3f}")
    lines += ["", f"{'cell':<16}{'arm':<8}{'sold':>7}{'gain over floor':>17}  [95%]"]
    for label, c in r["cells"].items():
        for arm, a in c["arms"].items():
            lines.append(f"{label:<16}{arm:<8}{a['sold']:>7.3f}{a['gain']:>+17.3f}  "
                         f"{_fmt_interval(a['gain_interval'], '+.3f')}")
    lines += ["", "the model's forecast at each arm's ask:",
              f"{'cell':<16}{'arm':<8}{'predicted':>11}{'realised':>10}{'ratio':>7}  [95%]"]
    for label, c in r["cells"].items():
        for arm, a in c["arms"].items():
            ratio = "none" if a["ratio"] is None else f"{a['ratio']:.3f}"
            lines.append(f"{label:<16}{arm:<8}{a['predicted']:>11.3f}{a['sold']:>10.3f}"
                         f"{ratio:>7}  {_fmt_interval(a['ratio_interval'], '.3f')}")
    if "seconds" in r:
        lines += ["", f"took {r['seconds']:.0f} s"]
    return "\n".join(lines) + "\n"


def run_calibrate(args):
    started = time.perf_counter()
    markets = load(args.cache)
    found = with_prior(cases(markets, HORIZONS[0]), markets)
    result = calibrate(markets, found, ExitParams(*args.params), HORIZONS[0], args.fill)
    result["seconds"] = time.perf_counter() - started
    result["cache"] = str(args.cache)
    print(render_calibration(result), end="")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=1))
        print(f"wrote {args.json}")


def _floats(s):
    return [float(x) for x in s.split(",")]


def main():
    parser = argparse.ArgumentParser(prog="python -m analysis.exit_backtest",
                                     description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="cache the sampled markets' trades and mids")
    p.add_argument("--db", required=True, help="a copy of farm.db to sample from")
    p.add_argument("--cache", required=True)
    p.add_argument("--limit", type=int, default=250)
    p = sub.add_parser("search", help="choose the parameters and write the report")
    p.add_argument("--cache", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--yield-grid", type=_floats,
                   help="price the prior on these yields instead of YIELD_GRID")
    p.add_argument("--params", type=_floats,
                   help="half_life_days,arrival_days,mark_buys for both estimators: "
                        "skip the search and report at these")
    p = sub.add_parser("calibrate", help="print the hillclimb metric at fixed parameters")
    p.add_argument("--cache", required=True)
    p.add_argument("--params", required=True, type=_floats,
                   help="half_life_days,arrival_days,mark_buys")
    p.add_argument("--fill", choices=tuple(FILLS), default=DEFAULT_FILL,
                   help="which later buys fill an ask (default at-or-above)")
    p.add_argument("--json", help="also write the result to this file")
    args = parser.parse_args()
    if args.command == "fetch":
        with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as conn:
            markets = sample(conn, args.limit)
        with httpx.Client() as http:
            fetch(markets, args.cache, datetime.now(timezone.utc), http)
    elif args.command == "calibrate":
        run_calibrate(args)
    else:
        run_search(args)


def run_search(args):
    set_yield_grid(args.yield_grid)
    markets = load(args.cache)
    found = with_prior(cases(markets, HORIZONS[0]), markets)
    print(f"{len(markets)} markets, {len(found)} cases")
    chosen, surfaces = {}, {}
    for arm in PARAMETRIC:
        if args.params:
            chosen[arm] = ExitParams(*args.params)
            continue
        started = time.perf_counter()
        chosen[arm], surfaces[arm] = search(markets, found, GRID, HORIZONS[0], arm,
                                            args.workers, args.yield_grid)
        print(f"{arm}: {chosen[arm]} in {time.perf_counter() - started:.0f}s")
    report = evaluate(markets, found, chosen)
    at = min(m.tape.read_at for m in markets).timestamp()
    prior = fit(markets, chosen["model"], at)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(_jsonable(
        {"chosen": {a: p._asdict() for a, p in chosen.items()},
         "surfaces": {a: [[*p, *s] for p, s in surf.items()] for a, surf in surfaces.items()},
         "report": {f"{h:g}d/{s}/{part}": r for (h, s, part), r in report.items()},
         "prior": exit_value.exit_prior_to_json(prior) if prior else None,
         "markets": len(markets), "cases": len(found)}), indent=1))
    (out / "tables.md").write_text(render(chosen, surfaces, report, prior))
    print(f"wrote {out / 'result.json'} and {out / 'tables.md'}")


def _ci(stats):
    low, high = stats["interval"]
    return f"{stats['gain'].mean:+.3f} [{low:+.3f}, {high:+.3f}]"


def render(chosen, surfaces, report, prior):
    """The run as Markdown tables: gains in cents a share."""
    lines = []
    for arm, surface in surfaces.items():
        lines += [f"## {arm} surface: mean gain over the floor ask (se), horizon "
                  f"{HORIZONS[0]:g}d, chosen {tuple(chosen[arm])}", ""]
        for h in GRID["half_life_days"]:
            lines += [f"half-life {h:g}d", "", "| s_d \\ s_m | "
                      + " | ".join(f"{m:g}" for m in GRID["mark_buys"]) + " |",
                      "|---" * (len(GRID["mark_buys"]) + 1) + "|"]
            for d in GRID["arrival_days"]:
                cells = (surface[ExitParams(h, d, m)] for m in GRID["mark_buys"])
                lines.append(f"| {d:g} | " + " | ".join(f"{s.mean:+.3f} ({s.se:.3f})"
                                                        for s in cells) + " |")
            lines.append("")
    for (horizon, size, part), r in report.items():
        shares = size if size == ALL_SIZES else f"{size:g}"
        lines += [f"## Horizon {horizon:g}d, {shares} shares, {part} to resolution: "
                  f"{r['cases']} cases, {r['markets']} markets", "",
                  "| arm | gain over floor [95%] | regret | never filled, floor did | "
                  "premium over lowest | sold |", "|---|---|---|---|---|---|"]
        for arm, s in r["arms"].items():
            lines.append(f"| {arm} | {_ci(s)} | {s['regret']:.3f} | {s['never_filled']:.3f} "
                         f"| {s['premium']:.2f} | {s['sold']:.3f} |")
        lines += ["", "| model minus | gain [95%] |", "|---|---|"]
        lines += [f"| {other} | {_ci(s)} |" for other, s in r["versus"].items()]
        for arm, rows in r["calibration"].items():
            n = sum(k for k, _, _ in rows)
            mean = lambda j: sum(row[0] * row[j] for row in rows) / n
            lines += ["", f"{arm} calibration: predicted sold {mean(1):.3f}, realised "
                      f"{mean(2):.3f}; by decile: cases, predicted, realised", ""]
            lines += [f"    {b + 1:2} {n:5} {p:.3f} {q:.3f}" for b, (n, p, q) in enumerate(rows)]
        lines.append("")
    if prior:
        lines += [f"## Prior at {tuple(chosen['model'])}", "",
                  f"arrival_per_day {prior.arrival_per_day:.4g}, markets {prior.markets}", ""]
        lines += [f"    {r:<5g} " + " ".join(f"{m:8.4g}" for m in row)
                  for r, row in zip(exit_value.YIELD_GRID, prior.marks)]
    return "\n".join(lines) + "\n"


def _jsonable(x):
    if isinstance(x, Score):
        return x._asdict()
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


if __name__ == "__main__":
    main()
