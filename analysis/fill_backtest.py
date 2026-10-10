"""
Backtest the favorite bid's fill forecast against the sweeps that came after.

  fetch      read the hits on favorite bids of each market in the exit
             backtest's cache once into --cache, with its mids up to now
  calibrate  print the fill curve's forecast over the realised events by
             distance and split, and where the mid stood after each fill

A case is a market at a past time t whose mid is a favorite's. The farmer
charges a bid resting d under the mid FillCurve.per_day(d) events a day, each
filling the whole bid. Here that curve is built from what was known at t,
the market's History as of t shrunk toward a Prior fit as of t on the
markets of the other folds, and its forecast over HORIZON_DAYS is set beside
the events of the days after t. fill_cost.make_history counts both sides, so
a realised event is a History event; no estimator is changed.

make_history's window is half-open, [start, now). The history as of t is
handed the hits at or before t and counts the sweeps before t; the realised
window is handed the hits after t and counts the sweeps up to t +
HORIZON_DAYS. A hit exactly at t starts a sweep in neither.
"""

import argparse
import bisect
import json
import math
import random
import statistics
import time
from datetime import datetime, timedelta, timezone
from multiprocessing import Pool
from pathlib import Path
from typing import NamedTuple

import httpx

from analysis.exit_backtest import (
    BOOTSTRAP_REPS,
    DAY_S,
    EVERY_DAYS,
    EXPIRY_BUFFER_DAYS,
    FAV_RANGE,
    FETCH_DAYS,
    WARMUP_DAYS,
    Mids,
    _fmt_interval,
    _percentile_interval,
    _utc,
    changes,
    folds,
)
from farm import fill_cost, reward_screener
from farm.fill_cost import DISTANCES, HISTORY_DAYS, PRE_SWEEP_S, History, Hit

# The farmer's min_fav: a sweep counts while the mid it found is at or above it.
FLOOR = 0.90
HORIZON_DAYS = 14.0
# make_history counts no further back than HISTORY_DAYS before its window ends.
assert HORIZON_DAYS <= HISTORY_DAYS
FORECAST_DISTANCES = DISTANCES[:DISTANCES.index(0.05) + 1]
I_1C = DISTANCES.index(0.01)
POST_FILL_DISTANCES = (0.01, 0.02, 0.03)
POST_FILL_HORIZONS = (("1h", 3600.0), ("1d", DAY_S), ("7d", 7 * DAY_S), ("14d", 14 * DAY_S))
CACHE_FILE = "markets.jsonl"


class Market(NamedTuple):
    """One market's hits on favorite bids, sorted by time and none before
    start, the unix time the trades behind them are complete from, as read at
    read_at. mids are its favorite's sorted (unix t, price) points, the list
    make_history takes, and mid the same points as a step function."""

    condition_id: str
    slug: str
    end_date: datetime
    tick: float
    hits: list
    start: float
    read_at: datetime
    mids: list
    mid: Mids

    @staticmethod
    def of(condition_id, slug, end_date, tick, hits, start, read_at, mids):
        mids = sorted((t, price) for t, price in mids)
        hits = sorted(Hit(*hit) for hit in hits)
        return Market(condition_id, slug, end_date, tick, [h for h in hits if h.at >= start],
                      start, read_at, mids, Mids(mids))

    @property
    def expires_at(self):
        return self.end_date - timedelta(days=EXPIRY_BUFFER_DAYS)


class Case(NamedTuple):
    """Market at unix time t, its mid then a favorite's; fold is the market's."""

    market: Market
    t: float
    fold: int
    mid: float


class Observed(NamedTuple):
    """One case's forecast and what followed: the market's History as of the
    case's time, where fill_curve took its rates from, and per
    FORECAST_DISTANCES the events the curve expects over HORIZON_DAYS and the
    events History counts in them."""

    case: Case
    history: History
    source: str
    forecast: tuple
    realised: tuple


def cases(markets):
    """Every EVERY_DAYS of each market, on one clock for all, from a week
    after its trades are complete to a horizon before its read or its expiry,
    where its mid is a favorite's in FAV_RANGE."""
    fold, step, found = folds(markets), EVERY_DAYS * DAY_S, []
    for m in markets:
        first = m.start + WARMUP_DAYS * DAY_S
        last = min(m.read_at, m.expires_at).timestamp() - HORIZON_DAYS * DAY_S
        for k in range(math.ceil(first / step), math.floor(last / step) + 1):
            t = k * step
            mid = m.mid.at(t)
            if mid is not None and FAV_RANGE[0] <= mid <= FAV_RANGE[1]:
                found.append(Case(m, t, fold[m.condition_id], mid))
    return found


def _split(market, t):
    """The market's hits at or before t, and those after."""
    i = bisect.bisect_right(market.hits, t, key=lambda hit: hit.at)
    return market.hits[:i], market.hits[i:]


def history_as_of(market, t):
    """The market's History as a read at unix time t would have made it."""
    known, _ = _split(market, t)
    return fill_cost.make_history(known, market.mids, market.start, _utc(t), FLOOR)


def realised(market, t):
    """Per FORECAST_DISTANCES, the events History counts in the HORIZON_DAYS
    after t."""
    _, later = _split(market, t)
    after = fill_cost.make_history(later, market.mids, t, _utc(t + HORIZON_DAYS * DAY_S), FLOOR)
    return after.events[:len(FORECAST_DISTANCES)]


class Replay:
    """What was known at a case's time: every market's History as of then,
    and per fold the Prior fit on the histories of the other folds. Holds one
    time at once, so replay cases in time order."""

    def __init__(self, markets):
        self.markets = markets
        self._fold = folds(markets)
        self._t = None

    def _at(self, t):
        if self._t != t:
            self._t, self._priors = t, {}
            self._histories = {m.condition_id: history_as_of(m, t) for m in self.markets}
        return self

    def history(self, case):
        return self._at(case.t)._histories[case.market.condition_id]

    def prior(self, case):
        """The Prior of the case's fold, None when under PRIOR_MIN_MARKETS of
        the other folds' markets had any history yet."""
        priors = self._at(case.t)._priors
        if case.fold not in priors:
            priors[case.fold] = fill_cost.fit_prior(
                [h for cid, h in self._histories.items() if self._fold[cid] != case.fold],
                _utc(case.t), FLOOR)
        return priors[case.fold]


def _seen(replay, case):
    """The case's Observed less the case, None without a prior."""
    prior = replay.prior(case)
    if prior is None:
        return None
    history = replay.history(case)
    curve = fill_cost.fill_curve(history, prior, FLOOR)
    return (history, fill_cost.fill_source(history, prior, FLOOR),
            tuple(curve.per_day(d) * HORIZON_DAYS for d in FORECAST_DISTANCES),
            realised(case.market, case.t))


_shared = None


def _share(markets, found):
    global _shared
    _shared = markets, found


def _seen_job(indices):
    markets, found = _shared
    replay = Replay(markets)
    return [_seen(replay, found[i]) for i in indices]


def observe(markets, found, workers=1):
    """(the Observed of the cases with a prior, in the order given; how many
    cases were dropped for lack of one). With workers, a case time a job."""
    by_time = {}
    for i in sorted(range(len(found)), key=lambda i: found[i].t):
        by_time.setdefault(found[i].t, []).append(i)
    jobs = list(by_time.values())
    if workers > 1 and len(jobs) > 1:
        # A worker returns no case: each would pickle its whole market back.
        with Pool(min(workers, len(jobs)), initializer=_share,
                  initargs=(markets, found)) as pool:
            results = pool.map(_seen_job, jobs, chunksize=1)
    else:
        replay = Replay(markets)
        results = [[_seen(replay, found[i]) for i in job] for job in jobs]
    seen = {i: row for job, rows in zip(jobs, results) for i, row in zip(job, rows)}
    observed = [Observed(case, *seen[i]) for i, case in enumerate(found) if seen[i] is not None]
    return observed, len(found) - len(observed)


# Cases split by what was known at t: whether the market's history holds an
# event past 1c, how many days it covers, and the time to the end date.
SPLITS = {
    "all": lambda o: True,
    "quiet as of t (no event past 1c)": lambda o: o.history.events[I_1C] == 0,
    "active as of t (an event past 1c)": lambda o: o.history.events[I_1C] >= 1,
    "history under 20d": lambda o: o.history.days < 20.0,
    "history 20d+": lambda o: o.history.days >= 20.0,
    "under 60d to resolution":
        lambda o: o.case.market.end_date.timestamp() - o.case.t < 60 * DAY_S,
    "60d+ to resolution":
        lambda o: o.case.market.end_date.timestamp() - o.case.t >= 60 * DAY_S,
}


class Sums(NamedTuple):
    """Sums over some cases, each but n per FORECAST_DISTANCES: how many
    cases, the forecast and the realised events, the forecast chance of at
    least one event, 1 - exp(-forecast), and the cases that saw one."""

    n: float
    forecast: tuple
    realised: tuple
    p_forecast: tuple
    p_realised: tuple

    @staticmethod
    def one(observed):
        return Sums(1.0, observed.forecast, observed.realised,
                    tuple(-math.expm1(-f) for f in observed.forecast),
                    tuple(float(k >= 1) for k in observed.realised))

    @staticmethod
    def of(rows):
        n, *columns = zip(*rows)
        return Sums(sum(n), *(tuple(map(sum, zip(*column))) for column in columns))

    def ratios(self):
        """Forecast over realised events, None where nothing was realised."""
        return tuple(f / r if r > 0 else None for f, r in zip(self.forecast, self.realised))


def calibrate(observed, reps=BOOTSTRAP_REPS, seed=0):
    """The forecast against what followed: per split in SPLITS and distance
    in FORECAST_DISTANCES, the cases and markets, the mean forecast and
    realised events a case and the ratio of their sums, None when nothing
    was realised, and the forecast chance of at least one event beside the
    share of cases that saw one. Each ratio carries a 95% bootstrap interval
    from one resample of the markets per replicate, shared by every split and
    distance; a market without a case in a split adds nothing there. A split
    without cases has no rows."""
    by_split = {}
    for split, inside in SPLITS.items():
        rows = {}
        for o in observed:
            if inside(o):
                rows.setdefault(o.case.market.condition_id, []).append(Sums.one(o))
        by_split[split] = {cid: Sums.of(r) for cid, r in rows.items()}

    rng, ids = random.Random(seed), sorted({o.case.market.condition_id for o in observed})
    drawn = {split: [[] for _ in FORECAST_DISTANCES] for split in SPLITS}
    for _ in range(reps if ids else 0):
        draw = rng.choices(ids, k=len(ids))
        for split, by in by_split.items():
            rows = [by[cid] for cid in draw if cid in by]
            if rows:
                for ratios, ratio in zip(drawn[split], Sums.of(rows).ratios()):
                    if ratio is not None:
                        ratios.append(ratio)

    splits = {}
    for split, by in by_split.items():
        splits[split] = rows = []
        if by:
            total = Sums.of(by.values())
            ratios = total.ratios()
            for j, distance in enumerate(FORECAST_DISTANCES):
                rows.append({
                    "distance": distance, "cases": int(total.n), "markets": len(by),
                    "forecast": total.forecast[j] / total.n,
                    "realised": total.realised[j] / total.n,
                    "ratio": ratios[j],
                    "ratio_interval": _percentile_interval(drawn[split][j]),
                    "p_forecast": total.p_forecast[j] / total.n,
                    "p_realised": total.p_realised[j] / total.n})
    return {"floor": FLOOR, "horizon_days": HORIZON_DAYS, "cases": len(observed),
            "markets_with_cases": len(ids), "splits": splits,
            "bootstrap": {"reps": reps, "seed": seed}}


def events(hits, mids, start, end, floor):
    """{distance: [(at, mid, reach)]} over DISTANCES: the sweeps make_history
    counts as events from start to end, each with its first hit's time, the
    mid it found and how far under it it reached. make_history's rules to the
    letter, less its HISTORY_DAYS lookback: the whole span is counted."""
    start = max(start, mids[0][0] + PRE_SWEEP_S if mids else end)
    times = [t for t, _ in mids]
    reached = []
    for at, deep in fill_cost._sweeps(hits):
        if start <= at < end:
            mid = mids[bisect.bisect_right(times, at - PRE_SWEEP_S) - 1][1]
            if mid >= floor:
                reached.append((at, mid, mid - deep))
    return {d: fill_cost._declustered(reached, d) for d in DISTANCES}


def _quantile(values, q):
    """The q-quantile of sorted values, linear between neighbours."""
    x = q * (len(values) - 1)
    low = math.floor(x)
    high = min(low + 1, len(values) - 1)
    return values[low] + (x - low) * (values[high] - values[low])


def post_fill(markets, reps=BOOTSTRAP_REPS, seed=0):
    """Where the mid stood after a favorite bid filled, in cents over the
    bid. Per distance in POST_FILL_DISTANCES and horizon in
    POST_FILL_HORIZONS, over every event past that distance in each market's
    whole span: the mid that long after the sweep less the bid it filled,
    the mid the sweep found less the distance. An event whose horizon ends
    after the market's last mid is left out of that horizon. Each row holds
    the events and markets, the mean with a 95% bootstrap interval from one
    resample of the markets per replicate, and the pooled quartiles; None
    without events."""
    cells = {(d, label): {} for d in POST_FILL_DISTANCES for label, _ in POST_FILL_HORIZONS}
    for m in markets:
        swept = events(m.hits, m.mids, m.start, m.read_at.timestamp(), FLOOR)
        for d in POST_FILL_DISTANCES:
            for at, mid, _ in swept[d]:
                for label, later in POST_FILL_HORIZONS:
                    if at + later <= m.mids[-1][0]:
                        cells[d, label].setdefault(m.condition_id, []).append(
                            100 * (m.mid.at(at + later) - (mid - d)))

    rng, ids = random.Random(seed), sorted({cid for by in cells.values() for cid in by})
    draws = [rng.choices(ids, k=len(ids)) for _ in range(reps if ids else 0)]
    rows = []
    for (d, label), by in cells.items():
        cents = sorted(c for market_cents in by.values() for c in market_cents)
        sums = {cid: (sum(market_cents), len(market_cents)) for cid, market_cents in by.items()}
        means = []
        for draw in draws:
            picked = [sums[cid] for cid in draw if cid in sums]
            if picked:
                means.append(sum(s for s, _ in picked) / sum(n for _, n in picked))
        quartile = lambda q: _quantile(cents, q) if cents else None
        rows.append({"distance": d, "later": label, "events": len(cents), "markets": len(by),
                     "mean": statistics.fmean(cents) if cents else None,
                     "interval": _percentile_interval(means),
                     "q25": quartile(0.25), "median": quartile(0.5), "q75": quartile(0.75)})
    return rows


def report(markets, workers=1, reps=BOOTSTRAP_REPS, seed=0):
    """calibrate over every case of the markets and post_fill over their
    events, with the markets read and the cases dropped for lack of a prior."""
    observed, dropped = observe(markets, cases(markets), workers)
    return {"markets": len(markets), "dropped": dropped, **calibrate(observed, reps, seed),
            "post_fill": post_fill(markets, reps, seed)}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def exit_markets(exit_cache_dir, limit=None):
    """The exit backtest's cache records, each a dict, the first limit of them."""
    with (Path(exit_cache_dir) / CACHE_FILE).open() as lines:
        return [json.loads(line) for line in lines][:limit]


def fetch_market(http, market, now):
    """One market's cache record from its exit cache record: its hits over
    both /trades listings from where they are complete, and its mids.

    The exit cache holds the mids from its first point to its tape's read, so
    only what it lacks is read: the minutes since that read, and the lead
    before the first hit when its first point comes too late to price one.
    The mids are kept where they change, and at the last point read, which
    says how far they are known."""
    cid, fav = market["condition_id"], market["fav_token_id"]
    end = now.timestamp()
    cutoff = end - FETCH_DAYS * DAY_S
    every, every_from = reward_screener.fetch_trades(http, cid, False, cutoff)
    taker, taker_from = reward_screener.fetch_trades(http, cid, True, every_from)
    oldest = min((t.at for t in map(reward_screener.parse_trade, every + taker) if t),
                 default=end)
    # A listing read to its end reaches the market's first trade, not the cutoff.
    start = max(every_from, taker_from, oldest)
    hits = sorted(h for h in reward_screener.parse_hits(taker, every, fav) if h.at >= start)

    cached = [(t, price) for t, price in market["mids"]]
    lead_from = start - PRE_SWEEP_S - reward_screener.MIDS_LEAD_S
    if not cached:
        spans = [(lead_from, end)]
    else:
        # A minute of overlap, so a mid in the cache's last minute is not lost.
        spans = [(datetime.fromisoformat(market["tape"]["read_at"]).timestamp() - 60, end)]
        if cached[0][0] > start - PRE_SWEEP_S:
            spans.append((lead_from, cached[0][0]))
    by_time = {t: price for span in spans
               for t, price in reward_screener.fetch_mids(http, fav, *span)}
    by_time.update(cached)
    read = sorted(by_time.items())
    mids = changes(read)
    if read and mids[-1][0] != read[-1][0]:
        mids.append(list(read[-1]))
    return {"condition_id": cid, "slug": market.get("slug"), "end_date": market["end_date"],
            "tick": market["tick"], "fav_token_id": fav, "read_at": now.isoformat(),
            "start": start, "hits": [list(h) for h in hits], "mids": mids}


def fetch(markets, cache_dir, now, http):
    """Cache every market not cached yet, one JSON line each; a failed
    market is reported and left for the next run."""
    path = Path(cache_dir) / CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    done = ({json.loads(line)["condition_id"] for line in path.read_text().splitlines()}
            if path.exists() else set())
    todo = [m for m in markets if m["condition_id"] not in done]
    print(f"{len(done)} cached, {len(todo)} to fetch", flush=True)
    for i, market in enumerate(todo, 1):
        try:
            record = fetch_market(http, market, now)
        except Exception as e:
            print(f"  {market.get('slug') or market['condition_id']}: failed ({e})", flush=True)
            continue
        with path.open("a") as out:
            out.write(json.dumps(record) + "\n")
        print(f"  {i}/{len(todo)} {market.get('slug')}: {len(record['hits'])} hits, "
              f"{len(record['mids'])} mids", flush=True)
        time.sleep(0.5)


def load(cache_dir):
    """The cached markets that have mids."""
    markets = []
    with (Path(cache_dir) / CACHE_FILE).open() as lines:
        for line in lines:
            r = json.loads(line)
            if r["mids"]:
                markets.append(Market.of(
                    r["condition_id"], r["slug"], datetime.fromisoformat(r["end_date"]),
                    float(r["tick"]), r["hits"], r["start"],
                    datetime.fromisoformat(r["read_at"]), r["mids"]))
    return markets


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _cents(distance):
    return f"{100 * distance:g}c"


def render(r):
    """The report as text tables: events a case over the horizon by split and
    distance, then cents over the bid after a fill."""
    lines = [f"fill forecast over {r['horizon_days']:g}d at floor {r['floor']:.2f}: "
             f"{r['markets']} markets, {r['cases']} cases kept on "
             f"{r['markets_with_cases']} of them, {r['dropped']} dropped for lack of a prior"]
    for split, rows in r["splits"].items():
        lines += ["", split,
                  f"{'distance':>8}{'cases':>7}{'mkts':>6}{'forecast':>10}{'realised':>10}"
                  f"{'ratio':>8}  {'[95%]':<18}{'P>=1 fcst':>10}{'P>=1 real':>10}"]
        if not rows:
            lines.append("  no cases")
        for c in rows:
            ratio = "none" if c["ratio"] is None else f"{c['ratio']:.3f}"
            lines.append(
                f"{_cents(c['distance']):>8}{c['cases']:>7}{c['markets']:>6}"
                f"{c['forecast']:>10.4f}{c['realised']:>10.4f}{ratio:>8}  "
                f"{_fmt_interval(c['ratio_interval'], '.3f'):<18}"
                f"{c['p_forecast']:>10.4f}{c['p_realised']:>10.4f}")
    lines += ["", "the mid after a fill, cents over the bid:",
              f"{'distance':>8}{'later':>7}{'events':>8}{'mkts':>6}{'mean':>9}  {'[95%]':<20}"
              f"{'q25':>8}{'median':>8}{'q75':>8}"]
    for c in r["post_fill"]:
        cents = [f"{c[key]:+.3f}" if c["events"] else "none"
                 for key in ("mean", "q25", "median", "q75")]
        lines.append(f"{_cents(c['distance']):>8}{c['later']:>7}{c['events']:>8}"
                     f"{c['markets']:>6}{cents[0]:>9}  "
                     f"{_fmt_interval(c['interval'], '+.3f'):<20}"
                     f"{cents[1]:>8}{cents[2]:>8}{cents[3]:>8}")
    if "seconds" in r:
        lines += ["", f"took {r['seconds']:.0f} s"]
    return "\n".join(lines) + "\n"


def run_calibrate(args):
    started = time.perf_counter()
    result = report(load(args.cache), args.workers)
    result["seconds"] = time.perf_counter() - started
    result["cache"] = str(args.cache)
    print(render(result), end="")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=1))
        print(f"wrote {args.json}")


def main():
    parser = argparse.ArgumentParser(prog="python -m analysis.fill_backtest",
                                     description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="cache the hits and mids of the exit cache's markets")
    p.add_argument("--exit-cache", required=True,
                   help="the exit backtest's cache, for the market list and its mids")
    p.add_argument("--cache", required=True)
    p.add_argument("--limit", type=int, help="only the first N markets")
    p = sub.add_parser("calibrate", help="print the forecast against what followed")
    p.add_argument("--cache", required=True)
    p.add_argument("--json", help="also write the result to this file")
    p.add_argument("--workers", type=int, default=1,
                   help="processes to replay the case times over (default 1)")
    args = parser.parse_args()
    if args.command == "fetch":
        with httpx.Client() as http:
            fetch(exit_markets(args.exit_cache, args.limit), args.cache,
                  datetime.now(timezone.utc), http)
    else:
        run_calibrate(args)


if __name__ == "__main__":
    main()
