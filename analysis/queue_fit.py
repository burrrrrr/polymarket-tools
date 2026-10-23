"""
Fit how much of the favorite bid book ahead of a new bid a sell sweep must
pass before it fills that bid.

  fetch  read the taker sells of each market with book samples into --cache
  fit    set each sell beside the last book sampled before it and fit alpha

A sell sweep sells X shares into the favorite bids from the top down. A new
bid at b joins behind the A shares the book shows at b and above, so the
model fills it with max(0, X - alpha A) shares, at most its own size Q.
alpha discounts the depth that cancels or refills between the book read and
the sweep. What the sweep sold strictly under b went past everything resting
at b and above, so a bid at b would have filled with that, at most Q: that
is the observed fill the predicted one is set beside.

A row is one sweep at one candidate price b, the prices a depth-aware bid
would choose from in the sample: each bid level and one tick above it, no
higher than one tick over the best bid and under the best ask. A sweep is
either one transaction ("tx") or a burst of them chained on
fill_cost.SWEEP_GAP_S and MAX_SWEEP_SPAN_S ("burst"); the two are fit apart.
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
from pathlib import Path
from typing import NamedTuple

import httpx

from analysis.exit_backtest import _fmt_interval, _percentile_interval
from farm import farm_books, reward_screener
from farm.exit_value import Piece, Sweep
from farm.fill_cost import MAX_SWEEP_SPAN_S, SWEEP_GAP_S

CACHE_FILE = "sweeps.jsonl"
# Trades are read from this long before a market's first book sample.
LEAD_S = 3600.0
# A sample is set beside the sweeps of the next 300 s, one BOOK_SAMPLE_S,
# with a minute to spare for a late cycle.
MAX_AGE_S = 360.0
EPS = 1e-9
QS = (200.0, 1000.0)
FIT_Q = 1000.0
ALPHA_MAX = 5.0
GRID_POINTS = 50
GOLDEN_STEPS = 40
BISECT_STEPS = 60
BOOTSTRAP_REPS = 200
UNITS = ("tx", "burst")
# Upper edges of the depth buckets; a depth of exactly 0 has a bucket of its own.
DEPTH_EDGES = (0.0, 100.0, 500.0, 2000.0, 10000.0)
AGE_EDGES = (120.0, 240.0, MAX_AGE_S)


class Market(NamedTuple):
    """One market's taker sells of the favorite, oldest first and none
    before start, the unix time its trades are complete from, as read at
    read_at."""

    condition_id: str
    fav_token_id: str
    tick: float
    read_at: datetime
    start: float
    sells: list


class Row(NamedTuple):
    """One sweep of a market beside the book sampled age_s before it, at a
    candidate bid price b: A shares rested at b and above, the sweep sold X
    shares in all and `below` of them under b."""

    market: str
    unit: str
    sweep: int
    b: float
    A: float
    below: float
    X: float
    age_s: float


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def bursts(sweeps):
    """The sweeps, oldest first, chained as fill_cost chains hits: each
    within SWEEP_GAP_S of the last, the burst spanning at most
    MAX_SWEEP_SPAN_S. A burst is timed at its first sweep, its pieces merged
    per price."""
    chained, last = [], None
    for sweep in sorted(sweeps, key=lambda s: s.at):
        if chained and sweep.at - last <= SWEEP_GAP_S \
                and sweep.at - chained[-1][0] <= MAX_SWEEP_SPAN_S:
            chained[-1][1].extend(sweep.pieces)
        else:
            chained.append((sweep.at, list(sweep.pieces)))
        last = sweep.at
    return [Sweep(at, _merged(pieces)) for at, pieces in chained]


def _merged(pieces):
    shares = Counter()
    for p in pieces:
        shares[p.price] += p.size
    return tuple(Piece(price, size) for price, size in sorted(shares.items(), reverse=True))


def ladder(sample, tick):
    """[(b, A)] of a BookSample, highest b first: each bid level within
    BOOK_RECORD_DEPTH of the mid and one tick above it, none over the best
    bid and a tick or at the best ask, with the shares bid at b and above."""
    bids = [(p, s) for p, s in sample.bids if p >= sample.mid - farm_books.BOOK_RECORD_DEPTH - EPS]
    if not bids:
        return []
    top = max(p for p, _ in bids) + tick + EPS
    ask = min((p for p, _ in sample.asks), default=math.inf) - EPS
    prices = {round(x, 9) for p, _ in bids for x in (p, p + tick)}
    return [(b, sum(s for p, s in sample.bids if p >= b - EPS))
            for b in sorted(prices, reverse=True) if b <= top and b < ask]


def market_rows(market, samples):
    """The Rows of a market's sells in both units beside its BookSamples,
    sorted by time: each sweep against the last sample at or before it, and
    none when that is over MAX_AGE_S old."""
    times = [s.at.timestamp() for s in samples]
    ladders = {}
    rows = []
    for unit, sweeps in (("tx", market.sells), ("burst", bursts(market.sells))):
        for i, sweep in enumerate(sweeps):
            k = bisect.bisect_right(times, sweep.at) - 1
            if k < 0 or sweep.at - times[k] > MAX_AGE_S:
                continue
            if k not in ladders:
                ladders[k] = ladder(samples[k], market.tick)
            x = sum(p.size for p in sweep.pieces)
            for b, depth in ladders[k]:
                below = sum(p.size for p in sweep.pieces if p.price < b - EPS)
                rows.append(Row(market.condition_id, unit, i, b, depth, below, x,
                                sweep.at - times[k]))
    return rows


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------

def predicted(row, q, alpha):
    return min(q, max(0.0, row.X - alpha * row.A))


def observed(row, q):
    return min(q, row.below)


def _segments(row, q):
    """[(from alpha, p0, p1)]: the predicted fill is p0 + p1 alpha from each
    alpha on, up to the next."""
    if row.A <= 0:
        return [(-math.inf, min(q, row.X), 0.0)]
    return [(-math.inf, q, 0.0), ((row.X - q) / row.A, row.X, -row.A), (row.X / row.A, 0.0, 0.0)]


def _squared_error(y, p0, p1):
    return (y - p0) ** 2, -2.0 * (y - p0) * p1, p1 * p1


def _prediction(y, p0, p1):
    return p0, p1, 0.0


class Curve(NamedTuple):
    """A sum over rows of a function of alpha that is a quadratic between
    breaks: c0 + c1 alpha + c2 alpha^2 with coefs[k] from breaks[k - 1] on,
    coefs[0] below breaks[0]."""

    breaks: list
    coefs: list

    @staticmethod
    def of(rows, q, term):
        """The sum over rows of term(observed, p0, p1) on each predicted
        segment, from alpha 0 to ALPHA_MAX."""
        base, steps = [0.0, 0.0, 0.0], []
        for r in rows:
            y, prev = observed(r, q), (0.0, 0.0, 0.0)
            for start, p0, p1 in _segments(r, q):
                c = term(y, p0, p1)
                d = (c[0] - prev[0], c[1] - prev[1], c[2] - prev[2])
                if start <= 0:
                    base = [base[0] + d[0], base[1] + d[1], base[2] + d[2]]
                elif start <= ALPHA_MAX:
                    steps.append((start, d))
                prev = c
        steps.sort(key=lambda step: step[0])
        coefs, c = [tuple(base)], base
        for _, d in steps:
            c = [c[0] + d[0], c[1] + d[1], c[2] + d[2]]
            coefs.append(tuple(c))
        return Curve([at for at, _ in steps], coefs)

    def at(self, alpha):
        c0, c1, c2 = self.coefs[bisect.bisect_right(self.breaks, alpha)]
        return c0 + alpha * (c1 + alpha * c2)


def minimize(f):
    """The alpha in [0, ALPHA_MAX] minimizing f: the best of a grid of
    GRID_POINTS steps, refined by golden section within a step of it."""
    step = ALPHA_MAX / GRID_POINTS
    best = min((k * step for k in range(GRID_POINTS + 1)), key=f)
    lo, hi = max(0.0, best - step), min(ALPHA_MAX, best + step)
    ratio = (math.sqrt(5) - 1) / 2
    a, b = hi - ratio * (hi - lo), lo + ratio * (hi - lo)
    fa, fb = f(a), f(b)
    for _ in range(GOLDEN_STEPS):
        if fa <= fb:
            hi, b, fb = b, a, fa
            a = hi - ratio * (hi - lo)
            fa = f(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + ratio * (hi - lo)
            fb = f(b)
    return min(best, (lo + hi) / 2, key=f)


def fit(curves, weights):
    """The alpha minimizing the weighted sum of the markets' squared-error
    Curves."""
    picked = [(c, w) for c, w in zip(curves, weights) if w]
    return minimize(lambda alpha: sum(w * c.at(alpha) for c, w in picked))


def matching_alpha(curve, total):
    """The alpha in [0, ALPHA_MAX] at which a predicted-fill Curve, which
    never rises, sums to total: 0 when it is at or under total there,
    ALPHA_MAX when it is at or over total there, and None when alpha does
    not move it."""
    lo, hi = 0.0, ALPHA_MAX
    if curve.at(lo) == curve.at(hi):
        return None
    if curve.at(lo) <= total:
        return lo
    if curve.at(hi) >= total:
        return hi
    for _ in range(BISECT_STEPS):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if curve.at(mid) > total else (lo, mid)
    return (lo + hi) / 2


def depth_bucket(depth):
    if depth <= 0:
        return "0"
    for low, high in zip(DEPTH_EDGES, DEPTH_EDGES[1:]):
        if depth <= high:
            return f"({low:g},{high:g}]"
    return f">{DEPTH_EDGES[-1]:g}"


def age_bucket(age_s):
    low = 0.0
    for high in AGE_EDGES:
        if age_s <= high:
            return f"{low:g}-{high:g}s"
        low = high
    return f">{AGE_EDGES[-1]:g}s"


DEPTH_BUCKETS = ["0"] + [depth_bucket(high) for high in DEPTH_EDGES[1:]] + \
    [depth_bucket(math.inf)]
AGE_BUCKETS = [age_bucket(high) for high in AGE_EDGES]


def _ratio(pred, obs):
    return pred / obs if obs > 0 else None


def _cell(rows, alpha):
    """Totals of some rows: per Q the observed fill and the ratio of the
    predicted to it at alpha, 0 and 1, and the alpha matching the totals;
    and the shares of rows the model and the sweeps reach below b."""
    share = lambda reached: sum(map(reached, rows)) / len(rows) if rows else None
    out = {"rows": len(rows), "reach_predicted": share(lambda r: r.X - alpha * r.A > 0),
           "reach_observed": share(lambda r: r.below > 0)}
    for q in QS:
        obs = sum(observed(r, q) for r in rows)
        out[f"q{q:g}"] = {
            "observed": obs,
            **{f"ratio_{key}": _ratio(sum(predicted(r, q, a) for r in rows), obs)
               for key, a in (("fit", alpha), ("0", 0.0), ("1", 1.0))},
            "matching_alpha": matching_alpha(Curve.of(rows, q, _prediction), obs) if rows else None}
    return out


def fit_unit(rows, reps=BOOTSTRAP_REPS, seed=0):
    """One unit's rows: alpha fit on FIT_Q with a 95% interval from a
    bootstrap over markets, and the calibration overall, by depth and by
    sample age."""
    by_market = {}
    for r in rows:
        by_market.setdefault(r.market, []).append(r)
    ids = sorted(by_market)
    curves = [Curve.of(by_market[cid], FIT_Q, _squared_error) for cid in ids]
    alpha = fit(curves, [1] * len(ids))
    rng, drawn = random.Random(seed), []
    for _ in range(reps if ids else 0):
        count = Counter(rng.choices(range(len(ids)), k=len(ids)))
        drawn.append(fit(curves, [count[i] for i in range(len(ids))]))
    groups = lambda key, buckets: {b: _cell([r for r in rows if key(r) == b], alpha)
                                   for b in buckets}
    return {"rows": len(rows), "sweeps": len({(r.market, r.sweep) for r in rows}),
            "markets": len(ids), "alpha": alpha, "alpha_interval": _percentile_interval(drawn),
            "all": _cell(rows, alpha),
            "by_depth": groups(lambda r: depth_bucket(r.A), DEPTH_BUCKETS),
            "by_age": groups(lambda r: age_bucket(r.age_s), AGE_BUCKETS)}


def report(rows, reps=BOOTSTRAP_REPS, seed=0):
    return {"fit_q": FIT_Q, "max_age_s": MAX_AGE_S, "bootstrap": {"reps": reps, "seed": seed},
            "units": {unit: fit_unit([r for r in rows if r.unit == unit], reps, seed)
                      for unit in UNITS}}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _num(value, spec):
    return "none" if value is None else f"{value:{spec}}"


def _alpha(alpha):
    if alpha is None:
        return "none"
    if alpha in (0.0, ALPHA_MAX):
        return f"{'<=' if alpha == 0 else '>='}{alpha:g}"
    return f"{alpha:.3f}"


def _table(title, cells):
    lines = [f"{'':<23}" + "".join(f"  {'Q=' + format(q, 'g'):<33}" for q in QS)]
    head = f"{title:<14}{'rows':>9}"
    for _ in QS:
        head += f"  {'@fit':>8}{'@0':>8}{'@1':>8}{'match a':>9}"
    lines.append(head + f"{'reach mdl':>11}{'reach obs':>10}")
    for label, c in cells.items():
        line = f"{label:<14}{c['rows']:>9}"
        for q in QS:
            qc = c[f"q{q:g}"]
            line += (f"  {_num(qc['ratio_fit'], '.3f'):>8}{_num(qc['ratio_0'], '.3f'):>8}"
                     f"{_num(qc['ratio_1'], '.3f'):>8}{_alpha(qc['matching_alpha']):>9}")
        lines.append(line + f"{_num(c['reach_predicted'], '.3f'):>11}"
                     f"{_num(c['reach_observed'], '.3f'):>10}")
    return lines


def render(r):
    """The report as text: per unit the fit, then predicted over observed
    fill at the fitted alpha, at 0 and at 1, overall, by depth A and by
    sample age, with the alpha matching each bucket's totals and the shares
    of rows reached below b."""
    lines = [f"alpha fit on Q={r['fit_q']:g} by least squares; samples at most "
             f"{r['max_age_s']:g} s old; 95% intervals from {r['bootstrap']['reps']} "
             f"market resamples"]
    for unit, u in r["units"].items():
        lines += ["", f"unit {unit}: {u['rows']} rows, {u['sweeps']} sweeps, "
                      f"{u['markets']} markets",
                  f"alpha {u['alpha']:.3f} {_fmt_interval(u['alpha_interval'], '.3f')}", ""]
        lines += _table("all", {"all": u["all"]})
        lines += [""] + _table("depth A", u["by_depth"])
        lines += [""] + _table("sample age", u["by_age"])
    lines += ["", "ratios are predicted over observed fill; match a sets them to 1; reach is "
              "the share of rows with any fill below b"]
    if "seconds" in r:
        lines.append(f"took {r['seconds']:.0f} s")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Fetching and loading
# ---------------------------------------------------------------------------

class Listing(NamedTuple):
    """A market with book samples, as fetch reads it: its favorite, tick and
    first sample's unix time."""

    condition_id: str
    fav_token_id: str
    tick: float
    first_sample: float


def _read_only(path):
    return sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)


def listings(db_path, books_path):
    """(the Listings of every market with book samples, the condition ids
    farm.db holds no favorite for)."""
    books, farm = _read_only(books_path), _read_only(db_path)
    try:
        firsts = books.execute(
            "SELECT condition_id, MIN(at) FROM books JOIN markets ON markets.id = books.market "
            "GROUP BY condition_id ORDER BY condition_id").fetchall()
        found, missing = [], []
        for cid, first in firsts:
            row = farm.execute("SELECT metrics FROM markets WHERE condition_id = ?",
                               (cid,)).fetchone()
            market = (json.loads(row[0]) or {}).get("market", {}) if row and row[0] else {}
            if market.get("fav_token_id") and market.get("tick"):
                found.append(Listing(cid, market["fav_token_id"], float(market["tick"]),
                                     datetime.fromisoformat(first).timestamp()))
            else:
                missing.append(cid)
        return found, missing
    finally:
        books.close()
        farm.close()


def fetch_market(http, listing, now):
    """One market's cache record: its sells over both /trades listings from
    LEAD_S before its first sample, or from where the listings are complete."""
    cid, end = listing.condition_id, now.timestamp()
    every, every_from = reward_screener.fetch_trades(http, cid, False,
                                                     listing.first_sample - LEAD_S)
    taker, taker_from = reward_screener.fetch_trades(http, cid, True, every_from)
    oldest = min((t.at for t in map(reward_screener.parse_trade, every + taker) if t),
                 default=end)
    # A listing read to its end reaches the market's first trade, not the cutoff.
    start = max(every_from, taker_from, oldest)
    sells = [s for s in reward_screener.parse_sells(taker, every, listing.fav_token_id)
             if s.at >= start]
    return {"condition_id": cid, "fav_token_id": listing.fav_token_id, "tick": listing.tick,
            "read_at": now.isoformat(), "start": start,
            "sells": [[s.at, [list(p) for p in s.pieces]] for s in sells]}


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
        print(f"  {i}/{len(todo)} {listing.condition_id[:12]}: {len(record['sells'])} sells "
              f"from {datetime.fromtimestamp(record['start'], timezone.utc):%m-%d %H:%M}",
              flush=True)
        time.sleep(0.5)
    print(f"done: {len(todo) - failed} fetched, {failed} failed", flush=True)


def load(cache_dir):
    markets = []
    with (Path(cache_dir) / CACHE_FILE).open() as lines:
        for line in lines:
            r = json.loads(line)
            sells = [Sweep(at, tuple(Piece(price, size) for price, size in pieces))
                     for at, pieces in r["sells"]]
            markets.append(Market(r["condition_id"], r["fav_token_id"], float(r["tick"]),
                                  datetime.fromisoformat(r["read_at"]), r["start"],
                                  sorted(sells, key=lambda s: s.at)))
    return markets


def all_rows(markets, books_path):
    """The Rows of the cached markets beside their samples in farm_books.db."""
    conn, rows = _read_only(books_path), []
    try:
        for m in markets:
            if m.sells:
                rows += market_rows(m, farm_books.read_books(conn, m.condition_id))
    finally:
        conn.close()
    return rows


def run_fit(args):
    started = time.perf_counter()
    rows = all_rows(load(args.cache), args.books)
    print(f"{len(rows)} rows in {time.perf_counter() - started:.0f} s", flush=True)
    result = report(rows, args.reps)
    result["seconds"] = time.perf_counter() - started
    result["cache"] = str(args.cache)
    text = render(result)
    print(text, end="")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, indent=1))
        print(f"wrote {args.json}")


def main():
    parser = argparse.ArgumentParser(prog="python -m analysis.queue_fit",
                                     description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", help="cache the sells of the markets with book samples")
    p.add_argument("--db", required=True, help="farm.db, for each market's favorite and tick")
    p.add_argument("--books", required=True, help="farm_books.db")
    p.add_argument("--cache", required=True)
    p = sub.add_parser("fit", help="fit alpha on the cached sells and the book samples")
    p.add_argument("--books", required=True, help="farm_books.db")
    p.add_argument("--cache", required=True)
    p.add_argument("--json", help="also write the result to this file")
    p.add_argument("--report", help="also write the text report to this file")
    p.add_argument("--reps", type=int, default=BOOTSTRAP_REPS, help="bootstrap replicates")
    args = parser.parse_args()
    if args.command == "fetch":
        found, missing = listings(args.db, args.books)
        if missing:
            print(f"{len(missing)} markets with samples have no favorite in {args.db}: "
                  f"{', '.join(cid[:12] for cid in missing)}", flush=True)
        with httpx.Client() as http:
            fetch(found, args.cache, datetime.now(timezone.utc), http)
    else:
        run_fit(args)


if __name__ == "__main__":
    main()
