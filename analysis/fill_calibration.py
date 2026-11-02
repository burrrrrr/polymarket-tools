"""
How well the fill model predicts the farmer's own favorite-bid fills, and the
fit of the shares a seller who hits our bid takes. The gate for any change to
the fill model: fill_cost.SWEEPS, the prior rule, FillCurve.

  fit     print fill_cost.SWEEPS from the farmer's own FILL events and
          the bids it rested before --until (farm_books.db), each History
          rebuilt as of then from the pricing_backtest cache: what a seller
          brings, lognormal, the share of the bids ahead that holds and the
          sellers a counted sweep brings, fit jointly, a fill that took
          CENSOR_FRACTION or more of the resting bid and no more counting as
          "at least that"
  report  score predicted against realized FAV_BID fills over the same
          bids: per model and window (all, before --split, from it), by
          predicted events, by fill source and by shares ahead, the tail
          share, the top markets, the tail cap's effect on current markets,
          and the gate verdict

The resting bid a FILL hit is rebuilt from the FAV_BID PLACE and FILL rows:
by the order id a PLACE records, or, for a PLACE from before PLACE rows
carried one, by the last PLACE at the fill's price.

Our fills are trades in the public tape, so each History already counts
them. They score models here and enter no production count. Both databases
are opened read-only.
"""

import argparse
import bisect
import json
import math
import random
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import NamedTuple, Optional

from analysis import pricing_backtest as pb
from analysis import queue_fit
from farm import farm_books, farm_db, fill_cost, reward_screener
from farm.reward_farmer import FarmMarket, Position, market_outlook, target_orders, with_book_mid
from farm.reward_screener import Zones

CENSOR_FRACTION = 0.95
# Fewer fills than this leave a three-parameter fit to noise.
MIN_FIT_FILLS = 20
SIZE_CHECKS = (100.0, 300.0, 1000.0, 3000.0)
AHEAD_CHECKS = (0.0, 500.0, 2000.0, 10_000.0, 20_000.0)
SIGMA_RANGE = (0.3, 4.0)
MU_RANGE = (0.0, math.log(1e5))
HOLD_RANGE = (0.0, 1.0)
FIT_START = (4.5, 1.7, 0.3)
FIT_STEP = (0.5, 0.3, 0.2)
FIT_TOL = 1e-6
FIT_ITERATIONS = 2000
FIT_PENALTY = 1e3
FIT_REPS = 200
# Shares ahead merge stretches at this many significant digits.
AHEAD_DIGITS = 2
PRICE_EPS = 1e-9
SHARE_EPS = 1e-6

BOOK_SAMPLE_S = farm_books.BOOK_SAMPLE_S
# A farm_books stretch runs to the next sample, at most this long: a longer
# gap is the farmer stopped, not a bid resting.
MAX_GAP_S = 900.0
# A FILL counts against a stretch from this long before its start to
# COVER_AFTER_S after its end: positions lag the book by a poll or more.
COVER_BEFORE_S = 60.0
COVER_AFTER_S = 600.0
# The History a farmer read is rebuilt this often. It reads hourly, but the
# rate barely moves in 6 h and the rebuild is most of the run time.
HISTORY_EVERY_S = 6 * 3600.0
BUCKETS = (0.1, 0.5, 2.0)
BUCKETS_ORDER = ("<0.1", "0.1-0.5", "0.5-2", "2+")
AHEAD_BUCKETS = (500.0, 2000.0, 10_000.0, 20_000.0)
AHEAD_ORDER = ("0-500", "500-2k", "2k-10k", "10k-20k", "20k+")
BIG_FILLS = (500.0, 1000.0)
TAIL_DAYS = (10.0, 30.0, 60.0)
TAIL_QUANTILES = (0.95, 0.99)
HURDLES = (0.3, 1.0)
CURRENT_S = 3600.0
TOP_MARKETS = 15
TOP_TRIMMED = 10
REPS = 2000
LEVEL = 0.90
# The test window's realized/predicted must lie in this band and its interval
# reach into it: a model off by half or double from the split fails.
GATE_BAND = (0.67, 1.5)


class Table(NamedTuple):
    """One seller a sweep, from a table of equal-probability sizes, filling
    min(Q, size): fill_cost.Sweeps' fill, events and survival with nothing
    ahead holding."""

    sizes: tuple

    def held(self, ahead):
        return 0.0

    def survival(self, x):
        return sum(size > x for size in self.sizes) / len(self.sizes)

    def fill(self, shares, ahead=0.0):
        return sum(min(shares, x) for x in self.sizes) / len(self.sizes)

    def events(self, ahead=0.0):
        return 1.0


# 40 equal-probability bins of the public tape's taker sell bursts past EXIT_DISTANCE, mid >= 0.90.
BURST = Table((
    0.3, 1.2, 2.7, 4.9, 6.3, 8.8, 10.4, 12.6, 15.4, 18.7,
    20, 20, 20, 21, 24.1, 28.4, 31.7, 38.1, 41.3, 47.1,
    50, 53, 60.5, 69.7, 82.9, 95.9, 103.1, 119.5, 141.8, 169,
    198.5, 239.6, 299.6, 374.6, 482.9, 637.9, 881.4, 1310.3, 2318.3, 10613.4,
))


class OwnFill(NamedTuple):
    """One of our FAV_BID fill events and the bid it hit: resting is the
    shares that bid had left (own_fills), None when no PLACE matches."""

    condition_id: str
    at: float
    shares: float
    resting: Optional[float]

    @property
    def over_bid(self):
        """The fill took more than the bid it was joined to had left, so it
        hit another order too and says nothing about that bid's size."""
        return self.resting is not None and self.shares > self.resting + SHARE_EPS

    @property
    def censored(self):
        """The fill took (about) the whole bid and no more: the seller wanted
        at least this much."""
        return self.whole and not self.over_bid

    @property
    def whole(self):
        """The fill took CENSOR_FRACTION of the bid it was joined to or more."""
        return self.resting is not None and self.shares >= CENSOR_FRACTION * self.resting



def _unix(at):
    return datetime.fromisoformat(at).timestamp()


def read_events(conn):
    """[(unix at, condition_id, kind, price, size, data dict)] of the FAV_BID
    PLACE and FILL rows of farm.db in id order."""
    return [(_unix(at), cid, kind, price, size, json.loads(data) if data else {})
            for at, cid, kind, price, size, data in conn.execute(
                "SELECT at, condition_id, kind, price, size, data FROM farm_events "
                "WHERE slot = 'FAV_BID' AND kind IN ('PLACE', 'FILL') ORDER BY id")]


def own_fills(events):
    """{condition_id: [OwnFill]} from read_events rows, oldest first. Per
    market, a PLACE sets the resting bid; a FILL takes its shares from the
    bid it hit, so the next fill on that order sees what is left. The bid a
    FILL hit is the PLACE with its order id, or else the market's last PLACE
    when the fill is at its price or has none (placed and filled within a
    cycle, "unknown: no order rested in the slot"). A CANCEL does not end the
    bid: the farmer cancels and places in one cycle, and a fill priced from
    the last cycle's order must still find it."""
    by_id, last, out = {}, {}, {}
    for at, cid, kind, price, size, data in events:
        if kind == "PLACE":
            bid = last[cid] = [price, size]
            if data.get("order_id"):
                by_id[data["order_id"]] = bid
            continue
        bid = by_id.get(data.get("order_id"))
        if bid is None and cid in last and (
                price is None or abs(last[cid][0] - price) < PRICE_EPS):
            bid = last[cid]
        resting = bid[1] if bid and bid[1] > 0 else None
        out.setdefault(cid, []).append(OwnFill(cid, at, size, resting))
        if bid:
            bid[1] = max(0.0, bid[1] - size)
    return out



class Rested(NamedTuple):
    """A stretch of farm_books samples with our FAV_BID resting unchanged:
    from at, for days, distance = the sample's mid (without our orders) less
    the bid's price, shares = the bid's size, the History a farmer read then
    (None before the cache's trades are complete), and ahead = the others'
    shares bid above it (reward_screener.shares_ahead) to AHEAD_DIGITS
    significant digits."""

    at: float
    days: float
    distance: float
    shares: float
    history: Optional[fill_cost.History]
    ahead: float


class Market(NamedTuple):
    """A market the farmer rested a favorite bid on: its stretches oldest
    first, the fills inside one (covered) oldest first, and how many fell
    outside."""

    condition_id: str
    slug: str
    rested: tuple
    fills: tuple
    uncovered: int


def _day(at):
    return int(at // fill_cost.DAY_S)


def _rounded(shares):
    """shares to AHEAD_DIGITS significant digits, so stretches merge."""
    return round(shares, AHEAD_DIGITS - 1 - math.floor(math.log10(shares))) if shares > 0 else 0.0


def rested_of(samples, market, floor):
    """[(at, days, distance, shares, history, ahead)] of each sample with our
    FAV_BID, to the next sample and at most MAX_GAP_S, the History rebuilt
    from the cached market every HISTORY_EVERY_S."""
    out, history, read_at = [], None, -math.inf
    for i, s in enumerate(samples):
        bid = next(((p, z) for name, p, z in s.ours if name == "FAV_BID"), None)
        t = s.at.timestamp()
        after = samples[i + 1].at.timestamp() - t if i + 1 < len(samples) else BOOK_SAMPLE_S
        if bid is None or after <= 0:
            continue
        if t - read_at >= HISTORY_EVERY_S and t >= market.start:
            read = pb.history_as_of(market, t, floor)
            history, read_at = (read if read.days > 0 else None), t
        ahead = reward_screener.shares_ahead(pb.others_book(s, pb.tick_of(s, market.fm.tick)),
                                             bid[0])
        out.append((t, min(after, MAX_GAP_S), round(s.mid - bid[0], 6), bid[1], history,
                    _rounded(ahead)))
    return out


def covered(raw, fills):
    """(the fills inside a raw stretch, from COVER_BEFORE_S before its start
    to COVER_AFTER_S after its end, the count of the others)."""
    starts = [at for at, *_ in raw]
    inside = []
    for f in fills:
        i = bisect.bisect_right(starts, f.at + COVER_BEFORE_S) - 1
        if i >= 0 and f.at <= raw[i][0] + raw[i][1] + COVER_AFTER_S:
            inside.append(f)
    return tuple(inside), len(fills) - len(inside)


def merged(raw):
    """The raw stretches as Rested, runs of the same distance, size, History
    and shares ahead within a UTC day summed: a window split at a date splits
    them exactly."""
    out = []
    for at, seconds, distance, shares, history, ahead in raw:
        last = out[-1] if out else None
        if (last and (last.distance, last.shares, last.history, last.ahead)
                == (distance, shares, history, ahead) and _day(last.at) == _day(at)):
            out[-1] = last._replace(days=last.days + seconds / fill_cost.DAY_S)
        else:
            out.append(Rested(at, seconds / fill_cost.DAY_S, distance, shares, history, ahead))
    return tuple(out)


def load_markets(books_path, cache_dir, cfg, rows, fills, until):
    """([Market] of the markets farm_books saw our FAV_BID rest on before
    until (unix), with a farm.db row and a cache record; how many such
    markets were left out)."""
    records = {r["condition_id"]: r for r in pb.load_cache(cache_dir)}
    conn = queue_fit._read_only(books_path)
    try:
        ids = [cid for (cid,) in conn.execute("SELECT condition_id FROM markets")]
        out, skipped = [], 0
        for cid in ids:
            samples = [s for s in farm_books.read_books(conn, cid) if s.at.timestamp() < until]
            if not any(name == "FAV_BID" for s in samples for name, _, _ in s.ours):
                continue
            row, record = rows.get(cid), records.get(cid)
            market = (pb.market_of(record, _with_p_true(row), cfg)
                      if row and record and record["mids"] else None)
            if market is None:
                skipped += 1
                continue
            raw = rested_of(samples, market, cfg.min_fav)
            inside, outside = covered(raw, fills.get(cid, []))
            out.append(Market(cid, row["slug"] or cid, merged(raw), inside, outside))
    finally:
        conn.close()
    return out, skipped


def _with_p_true(row):
    """A farm row with a p_true, so a market following the book still has
    terms: the walk's helpers want one, and no worth is read here."""
    return {**row, "p_true": 0.5 if row["p_true"] is None else row["p_true"]}



class Model(NamedTuple):
    """A fill model as data: the prior fill_curve shrinks toward, and what a
    sweep fills (fill_cost.Sweeps or a Table). Scored by production
    fill_curve at the farmer's floor, a fill event being one FILL row."""

    name: str
    prior: fill_cost.Prior
    sweeps: object


class Window(Enum):
    ALL = "all"
    TRAIN = "train"
    TEST = "test"


class Tally(NamedTuple):
    """Fill events, the shares they took, and the events that took the
    whole bid (OwnFill.whole)."""

    events: float
    shares: float
    whole: float = 0.0


class Ratio(NamedTuple):
    """realized / predicted, and its 90% interval from resampling markets."""

    value: Optional[float]
    interval: Optional[tuple]


class Score(NamedTuple):
    model: str
    window: Window
    predicted: Tally
    realized: Tally
    events: Ratio
    shares: Ratio
    whole: Optional[Ratio] = None


class Verdict(NamedTuple):
    passes: bool
    reasons: tuple


def in_window(at, window, split):
    return window is Window.ALL or (at < split) == (window is Window.TRAIN)


def predicted(market, model, window, split, floor):
    """Tally of the model over the market's stretches in the window, the
    sweeps reaching the bid's distance being the curve's rate there times the
    days, the curve fill_curve(history, model.prior, floor): events = those
    sweeps times the fills each brings past the shares ahead (events),
    shares = those sweeps times what they fill of the bid, whole = those
    sweeps times their sellers who bring CENSOR_FRACTION of the bid past the
    shares that hold (Sweeps.held)."""
    return _summed(t for _, t in stretch_tallies(market, model, window, split, floor))


def stretch_tallies(market, model, window, split, floor):
    """[(Rested, the Tally predicted over it)] of the market's stretches in
    the window, as predicted sums them."""
    curves, fills, out = {}, {}, []
    sweeps = model.sweeps
    for r in market.rested:
        if not in_window(r.at, window, split):
            continue
        if r.history not in curves:
            curves[r.history] = fill_cost.fill_curve(r.history, model.prior, floor)
        key = (r.shares, r.ahead)
        if key not in fills:
            whole = sweeps.events(0.0) * sweeps.survival(
                CENSOR_FRACTION * r.shares + sweeps.held(r.ahead))
            fills[key] = Tally(sweeps.events(r.ahead), sweeps.fill(r.shares, r.ahead), whole)
        swept = curves[r.history].per_day(r.distance) * r.days
        out.append((r, Tally(*(swept * x for x in fills[key]))))
    return out


def _summed(tallies):
    events = shares = whole = 0.0
    for t in tallies:
        events, shares, whole = events + t.events, shares + t.shares, whole + t.whole
    return Tally(events, shares, whole)


def realized(market, window, split):
    """Tally of the market's covered fills in the window."""
    events = [f for f in market.fills if in_window(f.at, window, split)]
    return Tally(len(events), sum(f.shares for f in events), sum(f.whole for f in events))


def _ratio(pairs, field, rng_seed, reps):
    def of(sample):
        p = sum(getattr(pred, field) for pred, _ in sample)
        return sum(getattr(real, field) for _, real in sample) / p if p > 0 else None

    value = of(pairs)
    rng = random.Random(rng_seed)
    draws = [of(rng.choices(pairs, k=len(pairs))) for _ in range(reps)] if pairs else []
    return Ratio(value, pb.interval([d for d in draws if d is not None], LEVEL))


def score(markets, model, window, split, floor, reps=REPS, seed=0):
    """Score of the model over the markets in the window: the totals and the
    ratios, each interval from the same seeded resamples of markets."""
    pairs = [(predicted(m, model, window, split, floor), realized(m, window, split))
             for m in markets]
    return Score(model.name, window, _summed(p for p, _ in pairs), _summed(r for _, r in pairs),
                 *(_ratio(pairs, field, seed, reps) for field in Tally._fields))


def _bucket(events):
    labels = ["<0.1", "0.1-0.5", "0.5-2", "2+"]
    return labels[bisect.bisect_right(BUCKETS, events)]


def by_bucket(markets, model, window, split, floor):
    """Per bucket of the markets' own predicted events (BUCKETS), the
    markets, predicted and realized events and their ratio; markets with
    neither left out."""
    rows = {}
    for m in markets:
        p = predicted(m, model, window, split, floor).events
        r = realized(m, window, split).events
        if p == 0 and r == 0:
            continue
        row = rows.setdefault(_bucket(p), {"bucket": _bucket(p), "markets": 0,
                                           "predicted": 0.0, "realized": 0})
        row["markets"] += 1
        row["predicted"] += p
        row["realized"] += r
    ordered = sorted(rows.values(), key=lambda row: BUCKETS_ORDER.index(row["bucket"]))
    for row in ordered:
        row["ratio"] = row["realized"] / row["predicted"] if row["predicted"] > 0 else None
    return ordered


def _source(r, prior, floor):
    """Where a stretch's rate came from: the prior alone, DEFAULT_PRIOR, or
    a history that weighs under or over half against the prior at the bid's
    distance."""
    source = fill_cost.fill_source(r.history, prior, floor)
    if source != "history":
        return source
    i = max(0, bisect.bisect_right(fill_cost.DISTANCES, r.distance) - 1)
    weight = r.history.days / (r.history.days + prior.strengths[i])
    return "history, weight under 0.5" if weight < 0.5 else "history, weight 0.5 or more"


SOURCES = ("history, weight 0.5 or more", "history, weight under 0.5", "prior", "default")


def stretch_of(market, at):
    """The market's Rested a covered fill at `at` landed in."""
    i = bisect.bisect_right([r.at for r in market.rested], at + COVER_BEFORE_S) - 1
    return market.rested[max(0, i)]


def by_source(markets, model, window, split, floor):
    """Per source of the rate (SOURCES), predicted and realized events and
    shares, a fill counting toward the stretch it landed in."""
    rows = {s: {"source": s, "predicted_events": 0.0, "predicted_shares": 0.0,
                "realized_events": 0, "realized_shares": 0.0} for s in SOURCES}
    for m in markets:
        if not m.rested:
            continue
        for r, tally in stretch_tallies(m, model, window, split, floor):
            row = rows[_source(r, model.prior, floor)]
            row["predicted_events"] += tally.events
            row["predicted_shares"] += tally.shares
        for f in m.fills:
            if in_window(f.at, window, split):
                row = rows[_source(stretch_of(m, f.at), model.prior, floor)]
                row["realized_events"] += 1
                row["realized_shares"] += f.shares
    return [rows[s] for s in SOURCES]


def _ahead_bucket(ahead):
    return AHEAD_ORDER[bisect.bisect_right(AHEAD_BUCKETS, ahead)]


def by_ahead(markets, model, window, split, floor):
    """Per bucket of the shares ahead of the bid (AHEAD_BUCKETS), the
    market-days rested and the realized and predicted Tally, a fill
    counting toward the stretch it landed in."""
    rows = {b: {"bucket": b, "days": 0.0, "predicted": Tally(0.0, 0.0, 0.0),
                "realized": Tally(0, 0.0, 0)} for b in AHEAD_ORDER}
    for m in markets:
        if not m.rested:
            continue
        for r, tally in stretch_tallies(m, model, window, split, floor):
            row = rows[_ahead_bucket(r.ahead)]
            row["days"] += r.days
            row["predicted"] = _summed((row["predicted"], tally))
        for f in m.fills:
            if in_window(f.at, window, split):
                row = rows[_ahead_bucket(stretch_of(m, f.at).ahead)]
                row["realized"] = _summed((row["realized"], Tally(1, f.shares, f.whole)))
    return [{**row, "predicted": list(row["predicted"]), "realized": list(row["realized"])}
            for row in rows.values()]


def _miss(r):
    lo, hi = GATE_BAND
    band = f"[{lo:.2f}, {hi:.2f}]"
    if r.interval[1] < lo or r.interval[0] > hi:
        return f"misses the band {band}"
    if not lo <= r.value <= hi:
        return f"is outside the band {band}"
    return None


GATED = (("events", "events"), ("shares", "shares"), ("whole", "whole-bid fills"))


def gate(scores):
    """Verdict on a fill model from the sweeps refit before the split, scored
    over the test window: it passes when its events, shares and whole-bid
    fills each lie in GATE_BAND with the interval reaching into it, so a
    model off by half or double from the split fails. No ALL-window score
    counts: production's sellers is fit to make its events match there, and
    its sweeps are fit to every fill."""
    held_out = next((s for s in scores if (s.model, s.window) == ("refit", Window.TEST)), None)
    if held_out is None:
        return Verdict(False, ("no held-out size fit",))
    reasons = []
    for field, name in GATED:
        r = getattr(held_out, field)
        if r is None or r.interval is None:
            reasons.append(f"test {name}: nothing predicted")
        elif (miss := _miss(r)) is not None:
            reasons.append(f"test {name} realized/predicted {r.value:.2f} "
                           f"[{r.interval[0]:.2f}, {r.interval[1]:.2f}] {miss}")
    return Verdict(not reasons, tuple(reasons))


def tail_share(markets, fit):
    """Realized fill events of at least each BIG_FILLS size: how many and
    their share of the filled shares; and the fit's P(X >= the size)."""
    fills = [f for m in markets for f in m.fills]
    total = sum(f.shares for f in fills)
    return [{"size": size, "fills": sum(f.shares >= size for f in fills),
             "share": sum(f.shares for f in fills if f.shares >= size) / total if total else 0.0,
             "fit_p": fit.sweeps().survival(size) if fit else None}
            for size in BIG_FILLS]


def stopped_after_big(markets, size=BIG_FILLS[0]):
    """[(slug, largest fill, days rested after it)] of the markets with a fill
    of at least size, the fewest days first: a time split under-represents
    the markets that stop resting after one."""
    rows = []
    for m in markets:
        big = max(m.fills, key=lambda f: f.shares, default=None)
        if big and big.shares >= size:
            rows.append({"slug": m.slug, "shares": big.shares,
                         "days_after": sum(r.days for r in m.rested if r.at > big.at)})
    return sorted(rows, key=lambda row: row["days_after"])


def top_markets(markets, models, split, floor, n=TOP_MARKETS):
    """The n markets by realized shares, with each model's predicted shares."""
    top = sorted(markets, key=lambda m: realized(m, Window.ALL, split).shares,
                 reverse=True)[:n]
    return [{"slug": m.slug, "realized": list(realized(m, Window.ALL, split)),
             "predicted": {model.name: predicted(m, model, Window.ALL, split, floor).shares
                           for model in models}}
            for m in top]



class SweepFit(NamedTuple):
    """fill_cost.Sweeps fit to n fill events, `censored` of them taking the
    whole bid: what a seller reaching our bid brings, X
    lognormal(mu, sigma), the share of the shares bid ahead that holds, and
    the sellers a sweep the rate counts brings."""

    mu: float
    sigma: float
    hold: float
    sellers: float
    n: int
    censored: int

    def sweeps(self, quantile=fill_cost.TAIL_QUANTILE):
        return fill_cost.Sweeps(self.mu, self.sigma, self.hold, self.sellers, quantile)


class FitTerms(NamedTuple):
    """One market's part of the fit. swept: {shares ahead: the sweeps the
    rate expects to reach the bid's distance} over its stretches. exact and
    at_least: (shares, shares ahead) of the fill events that took part of
    the bid, or about the whole of it; reached: the shares ahead of a fill
    with no resting bid, which says only that a sweep got past them."""

    swept: dict
    exact: tuple
    at_least: tuple
    reached: tuple


def fit_terms(market, prior, floor, window, split):
    """FitTerms of the market's stretches and fill events in the window, the
    rate fill_curve(history, prior, floor) and each fill's shares ahead its
    stretch's (stretch_of)."""
    swept, curves = {}, {}
    for r in market.rested:
        if in_window(r.at, window, split):
            if r.history not in curves:
                curves[r.history] = fill_cost.fill_curve(r.history, prior, floor)
            swept[r.ahead] = swept.get(r.ahead, 0.0) + curves[r.history].per_day(r.distance) * r.days
    exact, at_least, reached = [], [], []
    for f in market.fills:
        if in_window(f.at, window, split) and f.shares > 0:
            ahead = stretch_of(market, f.at).ahead
            if f.resting is None:
                reached.append(ahead)
            else:
                (at_least if f.censored else exact).append((f.shares, ahead))
    return FitTerms(swept, tuple(exact), tuple(at_least), tuple(reached))


def _pooled(terms):
    """The markets' FitTerms as one."""
    swept = {}
    for t in terms:
        for ahead, expected in t.swept.items():
            swept[ahead] = swept.get(ahead, 0.0) + expected
    return FitTerms(swept, *(tuple(x for t in terms for x in getattr(t, name))
                             for name in ("exact", "at_least", "reached")))


def _loglik(t, mu, sigma, hold):
    """The log likelihood of where and how much the fills of FitTerms t took,
    given how many there were: per fill event the density of a sweep
    bringing its shares plus the shares ahead that hold (Sweeps.held), the
    production depth credit included, or more for one that
    took the whole bid, or getting past them for one of no known bid; less
    the events times the log of the sweeps expected to fill anything. That
    is the Poisson likelihood of the fills with the rate's scale profiled
    out, so a rate off by a constant moves no parameter but the sellers.
    Without the second term nothing says how many sellers the bids ahead
    stopped."""
    sweeps = fill_cost.Sweeps(mu, sigma, hold)
    total = 0.0
    for shares, ahead in t.exact:
        y = shares + sweeps.held(ahead)
        total -= math.log(sigma * y) + 0.5 * ((math.log(y) - mu) / sigma) ** 2
    for shares, ahead in t.at_least:
        total += math.log(max(1e-300, sweeps.survival(shares + sweeps.held(ahead))))
    for ahead in t.reached:
        total += math.log(max(1e-300, sweeps.events(ahead)))
    return total - _events(t) * math.log(max(1e-300, _reaching(t, sweeps)))


def _events(t):
    return len(t.exact) + len(t.at_least) + len(t.reached)


def _reaching(t, sweeps):
    """The fills sweeps predicts over FitTerms t."""
    return sum(expected * sweeps.events(ahead) for ahead, expected in t.swept.items())


def _maximise(f, start, step, bounds):
    """The point within bounds where f is greatest, by Nelder-Mead on f of the
    point clamped into bounds less a penalty for the distance outside, run
    twice so a collapsed simplex starts again from the best point."""
    def clamp(x):
        return [min(hi, max(lo, v)) for v, (lo, hi) in zip(x, bounds)]

    def g(x):
        c = clamp(x)
        return f(*c) - FIT_PENALTY * sum(abs(a - b) for a, b in zip(x, c))

    best = list(start)
    for _ in range(2):
        simplex = [best] + [[v + (s if i == j else 0.0) for j, (v, s) in enumerate(zip(best, step))]
                            for i in range(len(best))]
        scored = [(g(x), x) for x in simplex]
        for _ in range(FIT_ITERATIONS):
            scored.sort(key=lambda p: -p[0])
            if scored[0][0] - scored[-1][0] < FIT_TOL:
                break
            worst = scored[-1][1]
            centre = [sum(x[j] for _, x in scored[:-1]) / (len(scored) - 1)
                      for j in range(len(worst))]

            def towards(t):
                return [c + t * (w - c) for c, w in zip(centre, worst)]

            reflected = towards(-1.0)
            fr = g(reflected)
            if fr > scored[0][0]:
                expanded = towards(-2.0)
                fe = g(expanded)
                scored[-1] = (fe, expanded) if fe > fr else (fr, reflected)
            elif fr > scored[-2][0]:
                scored[-1] = (fr, reflected)
            else:
                inner = towards(0.5)
                fi = g(inner)
                if fi > scored[-1][0]:
                    scored[-1] = (fi, inner)
                else:
                    top = scored[0][1]
                    scored = [scored[0]] + [(g(x), x) for x in (
                        [t + 0.5 * (v - t) for t, v in zip(top, p)] for _, p in scored[1:])]
        best = max(scored, key=lambda p: p[0])[1]
    return clamp(best)


def fit_sweeps(terms):
    """SweepFit maximising _loglik over the markets' FitTerms, its sellers
    the profiled rate scale: as many fills as the rate predicts. ValueError on
    fewer than MIN_FIT_FILLS fill events with a resting bid, no partial one,
    or no sweep expected over the stretches to scale the sellers by."""
    t = _pooled(terms)
    sized = len(t.exact) + len(t.at_least)
    if sized < MIN_FIT_FILLS or not t.exact:
        raise ValueError(f"{sized} fill events with a resting bid, {len(t.exact)} partial: "
                         f"too few to fit")
    if not any(expected > 0 for expected in t.swept.values()):
        raise ValueError(f"{_events(t)} fill events but no sweep expected over the rested "
                         f"stretches: no rate to fit the sellers against")
    mu, sigma, hold = _maximise(lambda mu, sigma, hold: _loglik(t, mu, sigma, hold),
                                FIT_START, FIT_STEP, (MU_RANGE, SIGMA_RANGE, HOLD_RANGE))
    sellers = _events(t) / _reaching(t, fill_cost.Sweeps(mu, sigma, hold))
    return SweepFit(mu, sigma, hold, sellers, _events(t), len(t.at_least))


def fit_intervals(terms, reps=FIT_REPS, seed=0):
    """{mu, sigma, hold, sellers: its LEVEL interval} over fits to seeded resamples of
    the markets' FitTerms, None for a resample too small to fit."""
    rng, draws = random.Random(seed), []
    for _ in range(reps):
        try:
            draws.append(fit_sweeps(rng.choices(terms, k=len(terms))))
        except ValueError:
            continue
    return {name: pb.interval([getattr(d, name) for d in draws], LEVEL) if draws else None
            for name in ("mu", "sigma", "hold", "sellers")}


def fit_text(fit, markets, until, prior):
    """fill_cost.SWEEPS and SWEEPS_PRIOR_AT as source text with their
    provenance, the markets' data before until (a datetime) and the stored
    prior the rates shrank toward, then E[min(Q, X)] at SIZE_CHECKS beside
    the SWEEPS it replaces, and what the hold leaves of a 1,000-share bid's
    fill and of the tail's sweep behind AHEAD_CHECKS."""
    sweeps = fit.sweeps()
    stamp = until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    filled = sum(1 for m in markets if m.fills)
    prior_at = prior.at.isoformat() if prior.at else None
    lines = [
        "# What a seller brings to a bid, a censored lognormal, the share of the shares",
        "# bid ahead of the bid that holds, and the sellers (FILL events) a counted",
        f"# sweep brings, fit jointly to {fit.n} fills on {filled} of the {len(markets)} markets",
        f"# that rested a bid, shares ahead counted up to {fill_cost.DEPTH_CREDIT_MAX:,.0f}, from",
        "#   python -m analysis.fill_calibration fit --db data\\farm.db --books data\\farm_books.db",
        f"#       --cache work\\fill-pricing\\cache --until {stamp}",
        f"# against the scan prior fit {prior_at} on {prior.markets} markets: sellers",
        "# scales that prior's rates, so a new prior needs a refit.",
        f"SWEEPS = Sweeps(mu={fit.mu:.4f}, sigma={fit.sigma:.4f}, hold={fit.hold:.4f}, "
        f"sellers={fit.sellers:.4f})",
        "SWEEPS_PRIOR_AT = " + (f'datetime.fromisoformat("{prior_at}")' if prior_at else "None"),
        "",
        "What a counted sweep fills of a bid of Q, nothing ahead: this fit / fill_cost.SWEEPS now",
    ]
    lines += [f"  Q {q:>5,.0f}: {sweeps.fill(q):7,.0f} / {fill_cost.SWEEPS.fill(q):7,.0f}"
              for q in SIZE_CHECKS]
    lines.append(f"Behind A shares, a counted sweep's fill of a 1,000-share bid, its fills, "
                 f"the tail's shares (dump {sweeps.dump:,.0f} at {sweeps.quantile:g})")
    lines += [f"  A {a:>6,.0f}: {sweeps.fill(1000.0, a):7,.1f}  {sweeps.events(a):.3f}  "
              f"{sweeps.tail(math.inf, a):7,.0f}" for a in AHEAD_CHECKS]
    return "\n".join(lines) + "\n"



class Current(NamedTuple):
    """A market's latest book sample as the farmer plans from it: its terms
    with the sample's tick, zones and book mid, the book without our orders,
    the History farm_state holds, and when."""

    slug: str
    fm: FarmMarket
    levels: object
    history: Optional[fill_cost.History]
    at: datetime


def current_markets(db_path, books_path, cfg, rows, until):
    """[Current] of every market sampled within CURRENT_S of the newest
    sample before until (unix): the ones the farmer quoted then. Each
    History is the one farm_state holds now."""
    farm = queue_fit._read_only(db_path)
    try:
        slots = {cid: json.loads(s or "{}") for cid, s in
                 farm.execute("SELECT condition_id, slots FROM farm_state")}
    finally:
        farm.close()
    books = queue_fit._read_only(books_path)
    try:
        newest = books.execute("SELECT MAX(at) FROM books WHERE at < ?", (farm_db.stamp(
            datetime.fromtimestamp(until, timezone.utc)),)).fetchone()[0]
        if newest is None:
            return []
        since = datetime.fromisoformat(newest) - timedelta(seconds=CURRENT_S)
        latest = {s.condition_id: s for s in farm_books.read_books(books, since=since)
                  if s.at.timestamp() < until}
    finally:
        books.close()
    out = []
    for cid, s in sorted(latest.items()):
        row = rows.get(cid)
        fm = pb.farm_market_of(_with_p_true(row), cfg) if row else None
        if fm is None:
            continue
        tick = pb.tick_of(s, fm.tick)
        levels = pb.others_book(s, tick)
        fm = with_book_mid(fm._replace(p_true=row["p_true"], tick=tick, zones=Zones(
            fill_cost.years_until(fm.end_date, s.at), s.mid)), levels)
        stored = ((slots.get(cid) or {}).get("FAV_BID") or {}).get("history")
        out.append(Current(fm.slug, fm, levels,
                           fill_cost.history_from_json(stored) if stored else None, s.at))
    return out


def tail_effect(current, cfg, prior, hurdles, tail_days_list, quantiles):
    """Per hurdle rate, tail quantile (Sweeps.quantile) and tail_days (no cap
    first): the production target_orders on each current market from no
    position, and what the cap does: markets bidding, resting $, planned
    reward and its share of the uncapped reward, the bids it changes or
    removes, and the tail $; and per hurdle the TOP_TRIMMED markets losing
    the most reward at cfg.tail_days and fill_cost.TAIL_QUANTILE."""
    rows, trimmed = [], []
    for hurdle in hurdles:
        at_hurdle = cfg._replace(hurdle_rate=hurdle)
        outlooks = [market_outlook(c.fm, None, c.history, prior, at_hurdle, c.at, any_floor=True)
                    for c in current]

        def planned(tail_days, quantile):
            capped = at_hurdle._replace(tail_days=tail_days)
            return [target_orders(c.fm, c.levels, Position(), capped, True, o._replace(
                fills=o.fills._replace(sweeps=o.fills.sweeps._replace(quantile=quantile))))
                .fav.best for c, o in zip(current, outlooks)]

        free = planned(0.0, fill_cost.TAIL_QUANTILE)
        best = {(None, 0.0): free}
        for quantile in quantiles:
            for tail_days in (t for t in tail_days_list if t > 0):
                best[(quantile, tail_days)] = planned(tail_days, quantile)
        free_reward = sum(b.reward_per_day for b in free if b)
        for (quantile, tail_days), chosen in best.items():
            reward = sum(b.reward_per_day for b in chosen if b)
            rows.append({
                "hurdle_rate": hurdle, "quantile": quantile, "tail_days": tail_days,
                "bidding": sum(b is not None for b in chosen),
                "resting_usd": sum(b.price * b.size for b in chosen if b),
                "reward_per_day": reward,
                "kept": reward / free_reward if free_reward > 0 else None,
                "binds": sum(1 for a, b in zip(free, chosen)
                             if a and (b is None or (b.price, b.size) != (a.price, a.size))),
                "tail_usd": sum(b.tail_usd for b in chosen if b),
            })
        at_cfg = best.get((fill_cost.TAIL_QUANTILE, cfg.tail_days))
        if at_cfg is not None:
            lost = sorted(((a.reward_per_day - (b.reward_per_day if b else 0.0), c, a, b)
                           for c, a, b in zip(current, free, at_cfg) if a),
                          key=lambda x: -x[0])[:TOP_TRIMMED]
            trimmed += [{"hurdle_rate": hurdle, "slug": c.slug, "lost_per_day": d,
                         "free": [a.price, a.size], "capped": [b.price, b.size] if b else None}
                        for d, c, a, b in lost if d > 0]
    return {"markets": len(current), "rows": rows, "trimmed": trimmed}



def models_for(prior, fits):
    """The report's models: DEFAULT_PRIOR with BURST, BURST on
    the stored prior, production, the sweeps refit on the window's fills,
    and the whole bid. fits maps a window to the SweepFit refit for it, if
    any."""
    base = [Model("default prior", fill_cost.DEFAULT_PRIOR, BURST),
            Model("burst table", prior, BURST),
            Model("production", prior, fill_cost.SWEEPS),
            Model("whole bid", prior, fill_cost.WHOLE_BID)]
    return {w: base + ([Model("refit", prior, fits[w].sweeps())] if fits.get(w) else [])
            for w in Window}


def _ratio_json(r):
    return [r.value, list(r.interval) if r.interval else None]


def _score_json(s):
    return {"model": s.model, "window": s.window.value, "predicted": list(s.predicted),
            "realized": list(s.realized), "events": _ratio_json(s.events),
            "shares": _ratio_json(s.shares), "whole": _ratio_json(s.whole)}


def _fit_json(fit):
    return {"mu": fit.mu, "sigma": fit.sigma, "hold": fit.hold, "sellers": fit.sellers,
            "n": fit.n, "censored": fit.censored}


def _fit_or_reason(terms):
    try:
        return fit_sweeps(terms), None
    except ValueError as e:
        return None, str(e)


def report(markets, skipped, fills, current, cfg, prior, split, until, tail_days_list,
           hurdles, quantiles=TAIL_QUANTILES, reps=REPS):
    """The report as a JSON-safe dict, its sections in render's order."""
    floor = cfg.min_fav
    terms = {w: [fit_terms(m, prior, floor, w, split) for m in markets]
             for w in (Window.ALL, Window.TRAIN)}
    fit_all, all_reason = _fit_or_reason(terms[Window.ALL])
    fit_train, train_reason = _fit_or_reason(terms[Window.TRAIN])
    fits = {Window.ALL: fit_all, Window.TRAIN: fit_train, Window.TEST: fit_train}
    by_window = models_for(prior, fits)
    scores = [score(markets, model, w, split, floor, reps) for w in Window for model in by_window[w]]
    production = by_window[Window.ALL][2]
    verdict = gate(scores)
    in_sample = next(s for s in scores if (s.model, s.window) == ("production", Window.ALL))
    rested = [r for m in markets for r in m.rested]
    tables = {"production": fill_cost.SWEEPS, "burst table": BURST}
    tables.update({f"fit {w}": f.sweeps() for w, f in (("all", fit_all), ("train", fit_train)) if f})
    return {
        "data": {
            "first": _stamp(min((r.at for r in rested), default=None)),
            "last": _stamp(max((r.at + r.days * fill_cost.DAY_S for r in rested), default=None)),
            "split": _stamp(split), "floor": floor, "markets": len(markets),
            "skipped_markets": skipped, "rested_days": sum(r.days for r in rested),
            "covered_fills": sum(len(m.fills) for m in markets),
            "uncovered_fills": sum(m.uncovered for m in markets),
            "fills": len(fills), "resolved": sum(f.resting is not None for f in fills),
            "over_bid": sum(f.over_bid for f in fills),
            "whole": sum(f.whole for m in markets for f in m.fills),
            "until": _stamp(until),
            "prior": {"at": prior.at.isoformat(timespec="minutes") if prior.at else None,
                      "markets": prior.markets, "floor": prior.floor},
            "warnings": [] if prior.at == fill_cost.SWEEPS_PRIOR_AT else [
                f"the stored prior was fit {_iso(prior.at)}, fill_cost.SWEEPS against the one "
                f"fit {_iso(fill_cost.SWEEPS_PRIOR_AT)}: sellers scales that prior's rates, so "
                f"production's predictions here are off by their ratio until a refit"],
        },
        "size_fit": {
            "all": _fit_json(fit_all) if fit_all else all_reason,
            "train": _fit_json(fit_train) if fit_train else train_reason,
            "intervals": fit_intervals(terms[Window.ALL], reps=min(reps, FIT_REPS))
            if fit_all else None,
            "dump": {f"{q:g}": fit_all.sweeps(q).dump for q in quantiles} if fit_all else None,
            "expected_min": {name: [t.fill(q) for q in SIZE_CHECKS]
                             for name, t in tables.items()},
        },
        "scores": [_score_json(s) for s in scores],
        "buckets": by_bucket(markets, production, Window.ALL, split, floor),
        "buckets_test": by_bucket(markets, production, Window.TEST, split, floor),
        "sources": by_source(markets, production, Window.ALL, split, floor),
        "ahead": {w.value: by_ahead(markets, production, w, split, floor)
                  for w in (Window.ALL, Window.TEST)},
        "tail_share": tail_share(markets, fit_all),
        "stopped_after_big": stopped_after_big(markets),
        "top_markets": top_markets(markets, by_window[Window.ALL], split, floor),
        "tail_effect": tail_effect(current, cfg, prior, hurdles, tail_days_list, quantiles),
        "gate": {"passes": verdict.passes, "reasons": list(verdict.reasons),
                 "candidate": ["refit", "test"],
                 "in_sample": {field: _ratio_json(getattr(in_sample, field))
                               for field, _ in GATED}},
    }


def _iso(at):
    return at.isoformat() if at else None


def _stamp(at):
    return None if at is None else datetime.fromtimestamp(at, timezone.utc).isoformat(
        timespec="minutes")


def _num(x, spec):
    return "-" if x is None else format(x, spec)


def _over(bucket, i):
    """realized / predicted of a by_ahead row's Tally field i, None on nothing
    predicted."""
    p = bucket["predicted"][i]
    return bucket["realized"][i] / p if p > 0 else None


def _ratio_text(r):
    value, interval = r
    if value is None:
        return "-"
    return f"{value:.2f}" + (f" [{interval[0]:.2f}, {interval[1]:.2f}]" if interval else "")


def render(r):
    """The report as text, sections in the order of the dict."""
    d, fit = r["data"], r["size_fit"]
    lines = ["Data",
             f"  {d['markets']} markets rested a favorite bid from {d['first']} to {d['last']}, "
             f"{d['rested_days']:,.1f} market-days; {d['skipped_markets']} more had no farm row "
             f"or cache record",
             f"  {d['fills']} FAV_BID fills, {d['resolved']} with their resting bid; "
             f"{d['covered_fills']} inside a rested stretch, {d['uncovered_fills']} outside, "
             f"{d['fills'] - d['covered_fills'] - d['uncovered_fills']} on markets not scored",
             f"  {d['over_bid']} fills took more than the bid they were joined to: "
             f"counted as exact sizes, not censored",
             f"  {d['whole']} of the fills inside a stretch took {CENSOR_FRACTION:.0%} of the "
             f"bid or more, the whole-bid fills",
             f"  floor {d['floor']:g}; data before {d['until']}; train before {d['split']}, "
             f"test from it",
             f"  rates shrink toward the stored prior, fit {d['prior']['at']} on "
             f"{d['prior']['markets']} markets at floor {d['prior']['floor']:g} over "
             f"{fill_cost.HISTORY_DAYS:g} days that overlap the test window; only the "
             f"sweeps are refit before the split",
             *(f"  warning: {w}" for w in d["warnings"]),
             "", "Sweep fit (what a seller brings, lognormal, the share of the shares ahead "
                 "that holds, and the sellers a counted sweep brings, fit jointly)"]
    for name in ("all", "train"):
        f = fit[name]
        lines.append(f"  {name:<6}" + (f"n {f['n']} ({f['censored']} censored) mu {f['mu']:.2f} "
                                       f"sigma {f['sigma']:.2f} hold {f['hold']:.3f} sellers "
                                       f"{f['sellers']:.2f}, median {math.exp(f['mu']):,.0f} "
                                       f"shares" if isinstance(f, dict) else f))
    if fit["intervals"]:
        lines.append(f"  all, {LEVEL:.0%} intervals from fits to resampled markets: " + ", ".join(
            f"{name} [{lo:.3f}, {hi:.3f}]" for name, (lo, hi) in fit["intervals"].items()))
    if fit["dump"]:
        lines.append("  the tail's seller: " + ", ".join(
            f"{shares:,.0f} shares at quantile {q}" for q, shares in fit["dump"].items()))
    lines.append("  what a counted sweep fills of a bid of Q, nothing ahead, at Q "
                 + ", ".join(f"{q:,.0f}" for q in SIZE_CHECKS))
    lines += [f"    {name:<14}" + "".join(f"{x:>8,.0f}" for x in values)
              for name, values in fit["expected_min"].items()]
    lines += ["", "Scores (realized / predicted, 90% intervals from resampling markets; "
                  "an event is one FILL row)",
              f"  {'model':<14} {'window':<6} {'events':<13} {'r/p [90%]':<20} "
              f"{'shares':<17} {'r/p [90%]':<20} {'whole bid':<11} r/p [90%]"]
    for s in r["scores"]:
        lines.append(f"  {s['model']:<14} {s['window']:<6} {s['realized'][0]:>4.0f}/"
                     f"{s['predicted'][0]:<8.1f} {_ratio_text(s['events']):<20} "
                     f"{s['realized'][1]:>8,.0f}/{s['predicted'][1]:<8,.0f} "
                     f"{_ratio_text(s['shares']):<20} {s['realized'][2]:>3.0f}/"
                     f"{s['predicted'][2]:<7.1f} {_ratio_text(s['whole'])}")
    for key, title in (("buckets", "all"), ("buckets_test", "test")):
        lines += ["", f"By predicted events per market, production, {title}"]
        lines += [f"  {b['bucket']:<8} {b['markets']:>4} markets  predicted {b['predicted']:7.1f}  "
                  f"realized {b['realized']:>4}  r/p {_num(b['ratio'], '.2f')}" for b in r[key]]
    lines += ["", "By fill source, production, all"]
    lines += [f"  {s['source']:<28} events {s['realized_events']:>4}/{s['predicted_events']:<7.1f}"
              f" shares {s['realized_shares']:>8,.0f}/{s['predicted_shares']:<8,.0f}"
              for s in r["sources"]]
    for window, rows in r["ahead"].items():
        lines += ["", f"By shares ahead of the bid, production, {window} (realized/predicted)",
                  f"  {'ahead':<8} {'days':>7} {'events':>14} {'r/p':>5} {'shares':>17} "
                  f"{'r/p':>5} {'whole bid':>12} {'r/p':>5}"]
        lines += [f"  {b['bucket']:<8} {b['days']:>7,.0f} "
                  f"{b['realized'][0]:>5.0f}/{b['predicted'][0]:<8.1f} "
                  f"{_num(_over(b, 0), '.2f'):>5} "
                  f"{b['realized'][1]:>7,.0f}/{b['predicted'][1]:<9,.0f} "
                  f"{_num(_over(b, 1), '.2f'):>5} "
                  f"{b['realized'][2]:>4.0f}/{b['predicted'][2]:<7.1f} "
                  f"{_num(_over(b, 2), '.2f'):>5}" for b in rows]
    lines += ["", "Tail share (realized fill events)"]
    lines += [f"  >= {t['size']:,.0f} shares: {t['fills']} fills, {t['share']:.0%} of filled "
              f"shares; fit P(X >= {t['size']:,.0f}) {_num(t['fit_p'], '.3f')}"
              for t in r["tail_share"]]
    if r["stopped_after_big"]:
        lines.append(f"  markets with a fill of {BIG_FILLS[0]:,.0f}+ shares, days rested after it:")
        lines += [f"    {s['slug'][:50]:<50} {s['shares']:>7,.0f}  {s['days_after']:5.1f}"
                  for s in r["stopped_after_big"]]
    models = list(r["top_markets"][0]["predicted"]) if r["top_markets"] else []
    lines += ["", f"Top markets by realized shares (all; predicted shares per model)",
              f"  {'market':<44} {'fills':>5} {'shares':>7}" + "".join(f" {m[:12]:>12}"
                                                                    for m in models)]
    lines += [f"  {t['slug'][:44]:<44} {t['realized'][0]:>5.0f} {t['realized'][1]:>7,.0f}"
              + "".join(f" {t['predicted'][m]:>12,.0f}" for m in models)
              for t in r["top_markets"]]
    te = r["tail_effect"]
    lines += ["", f"Tail cap on current markets ({te['markets']} markets, from no position)",
              f"  {'hurdle':>6} {'quantile':>8} {'tail days':>9} {'bidding':>7} "
              f"{'resting $':>10} {'reward $/d':>10} {'kept':>5} {'binds':>5} {'tail $':>8}"]
    lines += [f"  {row['hurdle_rate']:>6g} {_num(row['quantile'], 'g'):>8} "
              f"{'none' if row['tail_days'] == 0 else format(row['tail_days'], 'g'):>9} "
              f"{row['bidding']:>7} {row['resting_usd']:>10,.0f} {row['reward_per_day']:>10,.2f} "
              f"{_num(row['kept'], '.0%'):>5} {row['binds']:>5} {row['tail_usd']:>8,.0f}"
              for row in te["rows"]]
    if te["trimmed"]:
        lines.append(f"  most reward lost at the farmer's tail_days and quantile "
                     f"{fill_cost.TAIL_QUANTILE:g} (price x size, uncapped -> capped):")
        lines += [f"    {t['hurdle_rate']:>4g} {t['slug'][:44]:<44} -${t['lost_per_day']:.2f}/day  "
                  f"{t['free'][0]:.3f} x {t['free'][1]:,.0f} -> "
                  + (f"{t['capped'][0]:.3f} x {t['capped'][1]:,.0f}" if t["capped"] else "no bid")
                  for t in te["trimmed"]]
    g = r["gate"]
    lines += ["", "Gate",
              f"  the sweeps refit before the split, over the test window: events, shares and "
              f"whole-bid fills realized/predicted must each lie in [{GATE_BAND[0]:.2f}, "
              f"{GATE_BAND[1]:.2f}] with the {LEVEL:.0%} interval reaching it",
              f"  {'PASS' if g['passes'] else 'FAIL'}"]
    lines += [f"  - {reason}" for reason in g["reasons"]]
    lines.append("  in sample, not gated (sellers is fit to match production's events over all, "
                 "its sweeps to every fill): production all " + ", ".join(
                     f"{name} {_ratio_text(g['in_sample'][field])}" for field, name in GATED))
    return "\n".join(lines) + "\n"



def read_fills(db_path, until):
    """own_fills of farm.db's events before until (unix)."""
    conn = queue_fit._read_only(db_path)
    try:
        return own_fills([e for e in read_events(conn) if e[0] < until])
    finally:
        conn.close()


def _until(text):
    """--until as unix seconds: an ISO time, UTC unless it says otherwise, or
    now."""
    if text is None:
        return datetime.now(timezone.utc).timestamp()
    at = datetime.fromisoformat(text)
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).timestamp()


def stored_prior(db_path):
    conn = queue_fit._read_only(db_path)
    try:
        return reward_screener.load_prior(conn)
    finally:
        conn.close()


def run_fit(args):
    until = _until(args.until)
    cfg, rows = pb.farm_terms(args.db)
    markets, _ = load_markets(args.books, args.cache, cfg, rows, read_fills(args.db, until), until)
    prior = stored_prior(args.db)
    fit = fit_sweeps([fit_terms(m, prior, cfg.min_fav, Window.ALL, until) for m in markets])
    print(fit_text(fit, markets, datetime.fromtimestamp(until, timezone.utc), prior), end="")


def run_report(args):
    split = datetime.fromisoformat(args.split).replace(tzinfo=timezone.utc).timestamp()
    until = _until(args.until)
    cfg, rows = pb.farm_terms(args.db)
    by_market = read_fills(args.db, until)
    markets, skipped = load_markets(args.books, args.cache, cfg, rows, by_market, until)
    prior = stored_prior(args.db)
    current = current_markets(args.db, args.books, cfg, rows, until)
    fills = [f for found in by_market.values() for f in found]
    result = report(markets, skipped, fills, current, cfg, prior, split, until, args.tail_days,
                    args.hurdle_rates, args.tail_quantiles, args.reps)
    text = render(result)
    print(text, end="")
    if args.report:
        Path(args.report).write_text(text)
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))


UNTIL_HELP = "read only the data before this UTC time (ISO; default: now)"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m analysis.fill_calibration",
                                     description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fit = sub.add_parser("fit", help="print fill_cost.SWEEPS fit to the farmer's own fills")
    p = sub.add_parser("report", help="score the fill model against the farmer's own fills")
    for q in (fit, p):
        q.add_argument("--db", required=True, help="farm.db")
        q.add_argument("--until", help=UNTIL_HELP)
        q.add_argument("--books", required=True, help="farm_books.db")
        q.add_argument("--cache", required=True, help="the pricing_backtest fetch cache")
    p.add_argument("--split", required=True, help="UTC date the test window starts")
    p.add_argument("--tail-days", type=float, nargs="*", default=list(TAIL_DAYS),
                   help=f"tail caps to try on current markets (default: "
                        f"{' '.join(f'{t:g}' for t in TAIL_DAYS)})")
    p.add_argument("--tail-quantiles", type=float, nargs="*", default=list(TAIL_QUANTILES),
                   help=f"tail quantiles to try them at (default: "
                        f"{' '.join(f'{q:g}' for q in TAIL_QUANTILES)})")
    p.add_argument("--hurdle-rates", type=float, nargs="*", default=list(HURDLES),
                   help=f"hurdle rates to try them at (default: "
                        f"{' '.join(f'{h:g}' for h in HURDLES)})")
    p.add_argument("--reps", type=int, default=REPS, help="bootstrap replicates")
    p.add_argument("--report", help="also write the text report to this file")
    p.add_argument("--json", help="also write the result to this file")
    args = parser.parse_args(argv)
    if args.command == "fit":
        run_fit(args)
    else:
        run_report(args)


if __name__ == "__main__":
    main()
