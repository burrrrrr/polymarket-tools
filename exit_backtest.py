"""
Backtest the favorite exit model against the buys that came after.

  fetch    read each sampled market's trades and mids once into --cache
  search   replay the parameter grid and every arm offline, choose the
           parameters, refit the prior and write the report

A case is a market at a past time t holding a hypothetical inventory bought
a cent under the mid, as a swept favorite bid buys it. Each arm prices one
favorite ask for it from what was known at t: the production ExitModel on
the tape as of t, with a prior fit as of t on the markets of the other
folds, beside fixed-rule and alternative arms. The ask then rests for a
horizon against the buys that came after. Queue ahead at the ask's price
is not modelled, for every arm alike.
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

import exit_value
import reward_screener
from exit_value import (
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


def simulate(price, case, horizon_days):
    """The ask at price rested horizon_days from case.t for case.size: each
    later buy sells the shares it took at or over price, at price, until none
    are left; the rest are marked at the mid the horizon ends at."""
    m, t = case.market, case.t
    end, delta = t + horizon_days * DAY_S, math.log1p(POLICY.hurdle_rate)
    sweeps = m.tape.sweeps
    left, pv = case.size, 0.0
    for s in sweeps[bisect.bisect_right(sweeps, t, key=lambda s: s.at):]:
        if s.at > end or left <= 0:
            break
        took = sum(p.size for p in s.pieces if p.price >= price - 1e-9)
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
    """Outcomes of asks on the cases over one horizon, each simulated once."""

    def __init__(self, found, horizon_days):
        self.cases = found
        self.horizon_days = horizon_days
        self._memo = {}

    def outcome(self, i, price):
        key = i, price
        if key not in self._memo:
            self._memo[key] = simulate(price, self.cases[i], self.horizon_days)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="cache the sampled markets' trades and mids")
    p.add_argument("--db", required=True, help="a copy of farm.db to sample from")
    p.add_argument("--cache", required=True)
    p.add_argument("--limit", type=int, default=250)
    p = sub.add_parser("search", help="choose the parameters and write the report")
    p.add_argument("--cache", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--yield-grid", type=lambda s: [float(x) for x in s.split(",")],
                   help="price the prior on these yields instead of YIELD_GRID")
    p.add_argument("--params", type=lambda s: [float(x) for x in s.split(",")],
                   help="half_life_days,arrival_days,mark_buys for both estimators: "
                        "skip the search and report at these")
    args = parser.parse_args()
    if args.command == "fetch":
        with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as conn:
            markets = sample(conn, args.limit)
        with httpx.Client() as http:
            fetch(markets, args.cache, datetime.now(timezone.utc), http)
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
