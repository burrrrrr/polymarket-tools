"""
How well the fill model predicts the farmer's own favorite-bid fills, and the
fit of the shares a seller who hits our bid takes. The gate for any change to
the fill model: fill_cost.SWEEP_SIZES, the prior rule, FillCurve.

  fit     print fill_cost.SWEEP_SIZES from the farmer's own FILL events
          before --until: a censored lognormal of the shares each fill
          took, a fill that took CENSOR_FRACTION or more of the resting bid
          and no more counting as "at least that" (farm.db alone)
  report  score predicted against realized FAV_BID fills over the bids the
          farmer rested before --until (farm_books.db), with each History
          rebuilt as of then from the pricing_backtest cache: per model and
          window (all, before --split, from it), by predicted events and by
          fill source, per fill unit, the tail share, the top markets, the
          tail cap's effect on current markets, and the gate verdict

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
from statistics import NormalDist
from typing import NamedTuple, Optional

from analysis import pricing_backtest as pb
from analysis import queue_fit
from farm import farm_books, farm_db, fill_cost, reward_screener
from farm.reward_farmer import FarmMarket, Position, market_outlook, target_orders, with_book_mid
from farm.reward_screener import Zones

CENSOR_FRACTION = 0.95
SIZE_POINTS = 40
# Fewer fills than this leave a two-parameter fit to noise.
MIN_FIT_FILLS = 20
KM_CHECKS = (50.0, 100.0, 300.0, 1000.0)
SIZE_CHECKS = (100.0, 300.0, 1000.0, 3000.0)
SIGMA_RANGE = (0.3, 4.0)
MU_RANGE = (0.0, math.log(1e5))
FIT_TOL = 1e-4
PRICE_EPS = 1e-9
SHARE_EPS = 1e-6
NORMAL = NormalDist()

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
BIG_FILLS = (500.0, 1000.0)
TAIL_DAYS = (10.0, 30.0, 60.0)
HURDLES = (0.3, 1.0)
CURRENT_S = 3600.0
TOP_MARKETS = 15
TOP_TRIMMED = 10
REPS = 2000
LEVEL = 0.90
# The test window's realized/predicted must lie in this band and its interval
# reach into it: a model off by half or double from the split fails.
GATE_BAND = (0.67, 1.5)

# Sweep sizes from the public tape, kept as a model to score: the means of 40
# equal-probability bins of the shares each taker sell burst sold more than
# EXIT_DISTANCE under the mid before it, from a mid of at least 0.90.
BURST_SIZES = (
    0.3, 1.2, 2.7, 4.9, 6.3, 8.8, 10.4, 12.6, 15.4, 18.7,
    20, 20, 20, 21, 24.1, 28.4, 31.7, 38.1, 41.3, 47.1,
    50, 53, 60.5, 69.7, 82.9, 95.9, 103.1, 119.5, 141.8, 169,
    198.5, 239.6, 299.6, 374.6, 482.9, 637.9, 881.4, 1310.3, 2318.3, 10613.4,
)


class Unit(Enum):
    """What one fill event is. FILL: one positions read that showed a fill,
    the event FILL rows log. HOUR: the fills of a market within
    fill_cost.DECLUSTER_S of the first, as a History declusters sweeps."""

    FILL = "fill"
    HOUR = "hour"


# The unit fill_cost.SWEEP_SIZES is fit in, which unit_check holds to the
# unit the FillCurve's rates are calibrated in.
SWEEP_UNIT = Unit.FILL


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
        return (self.resting is not None and not self.over_bid
                and self.shares >= CENSOR_FRACTION * self.resting)


class SizeFit(NamedTuple):
    """The censored lognormal of the shares our fill events took, in unit."""

    mu: float
    sigma: float
    n: int
    censored: int
    unit: Unit

    def table(self, points=SIZE_POINTS):
        return size_table(self, points)


# --- reading -----------------------------------------------------------------

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


# --- the size fit ------------------------------------------------------------

def in_unit(fills, unit):
    """The fills as events of unit, oldest first. An HOUR event sums the
    shares of a market's fills from its first to DECLUSTER_S after it, and
    rests on the first one's bid."""
    fills = sorted(fills, key=lambda f: f.at)
    if unit is Unit.FILL:
        return fills
    open_, out = {}, []
    for f in fills:
        i = open_.get(f.condition_id)
        if i is not None and f.at - out[i].at <= fill_cost.DECLUSTER_S:
            out[i] = out[i]._replace(shares=out[i].shares + f.shares)
        else:
            open_[f.condition_id] = len(out)
            out.append(f)
    return out


def _golden(f, lo, hi):
    """The x in [lo, hi] that maximises a unimodal f, to FIT_TOL."""
    g = (math.sqrt(5) - 1) / 2
    c, d = hi - g * (hi - lo), lo + g * (hi - lo)
    fc, fd = f(c), f(d)
    while hi - lo > FIT_TOL:
        if fc >= fd:
            hi, d, fd = d, c, fc
            c = hi - g * (hi - lo)
            fc = f(c)
        else:
            lo, c, fc = c, d, fd
            d = lo + g * (hi - lo)
            fd = f(d)
    return (lo + hi) / 2


def fit_sizes(fills, unit=SWEEP_UNIT):
    """SizeFit of the fills with a resting bid, as events of unit: the
    (mu, sigma) maximising the log density of each uncensored event's
    log-shares plus the log survival at each censored one's. The censored
    normal's likelihood is unimodal in each parameter, so a golden-section
    search over sigma wraps one over mu. ValueError on fewer than
    MIN_FIT_FILLS events or no uncensored one."""
    events = [f for f in in_unit(fills, unit) if f.resting is not None and f.shares > 0]
    exact = [math.log(f.shares) for f in events if not f.censored]
    at_least = [math.log(f.shares) for f in events if f.censored]
    if len(events) < MIN_FIT_FILLS or not exact:
        raise ValueError(f"{len(events)} fill events, {len(exact)} uncensored: too few to fit")

    def loglik(mu, sigma):
        density = sum(-0.5 * ((x - mu) / sigma) ** 2 for x in exact) - len(exact) * math.log(sigma)
        survival = sum(math.log(max(1e-300, 0.5 * math.erfc((x - mu) / sigma / math.sqrt(2))))
                       for x in at_least)
        return density + survival

    def best_mu(sigma):
        return _golden(lambda mu: loglik(mu, sigma), *MU_RANGE)

    sigma = _golden(lambda s: loglik(best_mu(s), s), *SIGMA_RANGE)
    return SizeFit(best_mu(sigma), sigma, len(events), len(at_least), unit)


def size_table(fit, points=SIZE_POINTS):
    """The exact lognormal means of `points` equal-probability bins, smallest
    first, rounded to 0.1: exp(mu + sigma^2 / 2) * (Phi(b - sigma) -
    Phi(a - sigma)) * points between the bin's standard-normal quantiles a
    and b. The table's mean is the distribution's, so its tail fills a large
    bid as the fit says."""
    mean = math.exp(fit.mu + fit.sigma ** 2 / 2)
    edges = [-math.inf] + [NORMAL.inv_cdf(i / points) for i in range(1, points)] + [math.inf]

    def below(z):
        return 0.0 if z == -math.inf else 1.0 if z == math.inf else NORMAL.cdf(z - fit.sigma)

    return tuple(round(mean * (below(b) - below(a)) * points, 1) for a, b in zip(edges, edges[1:]))


def kaplan_meier(fills):
    """[(x, S(x))] at each uncensored size of the fills with a resting bid,
    the product-limit estimate of the share of sellers wanting more than x.
    The mass left at the end lies past the last uncensored size."""
    events = sorted(((f.shares, f.censored) for f in fills if f.resting is not None),
                    key=lambda e: e)
    at_risk, s, out, i = len(events), 1.0, [], 0
    while i < len(events):
        x = events[i][0]
        tied = [c for size, c in events[i:] if size == x]
        died = sum(not c for c in tied)
        if died:
            s *= 1 - died / at_risk
            out.append((x, s))
        at_risk -= len(tied)
        i += len(tied)
    return out


def _survival(curve, x):
    """S(x) of a kaplan_meier curve."""
    s = 1.0
    for size, after in curve:
        if size > x:
            break
        s = after
    return s


def fit_text(fit, fills, until):
    """fill_cost.SWEEP_SIZES as source text with its provenance, the fills
    before until (a datetime), then the Kaplan-Meier survival beside the
    fit's at KM_CHECKS and E[min(Q, X)] at SIZE_CHECKS beside the table it
    replaces."""
    table = fit.table()
    stamp = until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    markets = len({f.condition_id for f in fills if f.resting is not None})
    rows = [", ".join(f"{x:g}" for x in table[i:i + 10]) for i in range(0, len(table), 10)]
    lines = [
        f"# Shares one FILL event takes of a bid it reaches, {SIZE_POINTS} bin means of a censored",
        f"# lognormal fit to {fit.n} fills on {markets} markets, from",
        f"#   python -m analysis.fill_calibration fit --db data\\farm.db --until {stamp}",
        "SWEEP_SIZES = (", *(f"    {row}," for row in rows), ")",
    ]
    curve = kaplan_meier(in_unit(fills, fit.unit))
    lognormal = NormalDist(fit.mu, fit.sigma)
    lines += ["", "S(x), the share of sellers wanting more than x: Kaplan-Meier / fit"]
    lines += [f"  {x:>7,.0f}: {_survival(curve, x):.3f} / {1 - lognormal.cdf(math.log(x)):.3f}"
              for x in KM_CHECKS]
    lines += ["E[min(Q, X)]: this table / fill_cost.SWEEP_SIZES now"]
    lines += [f"  Q {q:>5,.0f}: {fill_cost.sweep_fill(q, table):7,.0f} / "
              f"{fill_cost.sweep_fill(q):7,.0f}" for q in SIZE_CHECKS]
    return "\n".join(lines) + "\n"


# --- the bids the farmer rested ------------------------------------------------

class Rested(NamedTuple):
    """A stretch of farm_books samples with our FAV_BID resting unchanged:
    from at, for days, distance = the sample's mid (without our orders) less
    the bid's price, shares = the bid's size, the History a farmer read then
    (None before the cache's trades are complete), and deep: more than
    DEPTH_FREE_SHARES bid ahead of it, which value_quote charges no fills."""

    at: float
    days: float
    distance: float
    shares: float
    history: Optional[fill_cost.History]
    deep: bool


class Market(NamedTuple):
    """A market the farmer rested a favorite bid on: its stretches oldest
    first, the fills inside one (covered), and how many fell outside."""

    condition_id: str
    slug: str
    rested: tuple
    fills: tuple
    uncovered: int


def _day(at):
    return int(at // fill_cost.DAY_S)


def rested_of(samples, market, floor):
    """[(at, days, distance, shares, history, deep)] of each sample with our
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
                    ahead > reward_screener.DEPTH_FREE_SHARES))
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
    and depth within a UTC day summed: a window split at a date splits them
    exactly."""
    out = []
    for at, seconds, distance, shares, history, deep in raw:
        last = out[-1] if out else None
        if (last and (last.distance, last.shares, last.history, last.deep)
                == (distance, shares, history, deep) and _day(last.at) == _day(at)):
            out[-1] = last._replace(days=last.days + seconds / fill_cost.DAY_S)
        else:
            out.append(Rested(at, seconds / fill_cost.DAY_S, distance, shares, history, deep))
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


# --- scoring -------------------------------------------------------------------

class Model(NamedTuple):
    """A fill model as data: the prior fill_curve shrinks toward, the size
    table sweep_fill draws from, and the unit its events are counted in.
    Scored by production fill_curve at the farmer's floor."""

    name: str
    prior: fill_cost.Prior
    sizes: tuple
    unit: Unit


class Window(Enum):
    ALL = "all"
    TRAIN = "train"
    TEST = "test"


class Tally(NamedTuple):
    events: float
    shares: float


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


class Verdict(NamedTuple):
    passes: bool
    reasons: tuple


def in_window(at, window, split):
    return window is Window.ALL or (at < split) == (window is Window.TRAIN)


def predicted(market, model, window, split, floor):
    """Tally of the model over the market's stretches in the window: events
    = the curve's rate at the bid's distance times the days, shares = that
    times sweep_fill of the bid's size, the curve fill_curve(history,
    model.prior, floor). A deep stretch predicts nothing, as value_quote
    charges it."""
    curves, fills = {}, {}
    events = shares = 0.0
    for r in market.rested:
        if r.deep or not in_window(r.at, window, split):
            continue
        if r.history not in curves:
            curves[r.history] = fill_cost.fill_curve(r.history, model.prior, floor)
        if r.shares not in fills:
            fills[r.shares] = fill_cost.sweep_fill(r.shares, model.sizes)
        expected = curves[r.history].per_day(r.distance) * r.days
        events += expected
        shares += expected * fills[r.shares]
    return Tally(events, shares)


def realized(market, window, split, unit):
    """Tally of the market's covered fills in the window: its events in unit,
    the shares they took."""
    events = [f for f in in_unit(market.fills, unit) if in_window(f.at, window, split)]
    return Tally(len(events), sum(f.shares for f in events))


def _ratio(pairs, field, rng_seed, reps):
    def of(sample):
        p = sum(getattr(pred, field) for pred, _ in sample)
        return sum(getattr(real, field) for _, real in sample) / p if p > 0 else None

    value = of(pairs)
    rng = random.Random(rng_seed)
    draws = [of(rng.choices(pairs, k=len(pairs))) for _ in range(reps)] if pairs else []
    return Ratio(value, pb.interval([d for d in draws if d is not None], LEVEL))


def score(markets, model, window, split, floor, reps=REPS, seed=0):
    """Score of the model over the markets in the window: the totals and both
    ratios, each interval from the same seeded resamples of markets."""
    pairs = [(predicted(m, model, window, split, floor), realized(m, window, split, model.unit))
             for m in markets]
    total = lambda i: Tally(sum(p[i].events for p in pairs), sum(p[i].shares for p in pairs))
    return Score(model.name, window, total(0), total(1), _ratio(pairs, "events", seed, reps),
                 _ratio(pairs, "shares", seed, reps))


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
        r = realized(m, window, split, model.unit).events
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
        for r in m.rested:
            if in_window(r.at, window, split):
                row = rows[_source(r, model.prior, floor)]
                tally = predicted(m._replace(rested=(r,)), model, window, split, floor)
                row["predicted_events"] += tally.events
                row["predicted_shares"] += tally.shares
        for f in in_unit(m.fills, model.unit):
            if in_window(f.at, window, split):
                row = rows[_source(stretch_of(m, f.at), model.prior, floor)]
                row["realized_events"] += 1
                row["realized_shares"] += f.shares
    return [rows[s] for s in SOURCES]


def unit_check(markets, prior, fills, split, floor, reps=REPS):
    """Per unit, the rate's TEST events realized/predicted with fills counted
    in that unit, and the TEST shares of a table fit before split in that
    unit; the unit whose events the rate predicts best (ratio nearest 1),
    and whether that is SWEEP_UNIT, the unit fill_cost.SWEEP_SIZES is fit
    in. A table fit in one unit and paired with a rate calibrated in the
    other would miss the shares by the events per hour."""
    rows, train = [], [f for f in fills if f.at < split]
    for unit in Unit:
        events = score(markets, Model(unit.value, prior, fill_cost.SWEEP_SIZES, unit),
                       Window.TEST, split, floor, reps)
        try:
            fit = fit_sizes(train, unit)
            shares = score(markets, Model(unit.value, prior, fit.table(), unit), Window.TEST,
                           split, floor, reps).shares
        except ValueError as e:
            fit, shares = None, None
            note = str(e)
        rows.append({"unit": unit.value, "events": _ratio_json(events.events),
                     "shares": _ratio_json(shares) if shares else None,
                     "fit": _fit_json(fit) if fit else note})
    distance = {row["unit"]: abs(math.log(row["events"][0])) if row["events"][0] else math.inf
                for row in rows}
    calibrated = min(distance, key=distance.get)
    return {"rows": rows, "calibrated": calibrated, "passes": calibrated == SWEEP_UNIT.value}


def _test_miss(r):
    lo, hi = GATE_BAND
    band = f"[{lo:.2f}, {hi:.2f}]"
    if r.interval[1] < lo or r.interval[0] > hi:
        return f"misses the band {band}"
    if not lo <= r.value <= hi:
        return f"is outside the band {band}"
    return None


def gate(candidate_all, candidate_test, unit_ok):
    """Verdict on a fill model: it passes when, for events and shares, the
    ALL-window interval includes 1, and the TEST-window ratio lies in
    GATE_BAND and its interval reaches into it, so a model off by half or
    double from the split fails; and the size table is fit in the unit the
    rate is calibrated in."""
    reasons = []
    for window, score_, miss_of in (
            ("all", candidate_all,
             lambda r: None if r.interval[0] <= 1 <= r.interval[1] else "excludes 1"),
            ("test", candidate_test, _test_miss)):
        for name in ("events", "shares"):
            r = getattr(score_, name)
            if r.interval is None:
                reasons.append(f"{window} {name}: nothing predicted")
            elif (miss := miss_of(r)) is not None:
                reasons.append(f"{window} {name} realized/predicted {r.value:.2f} "
                               f"[{r.interval[0]:.2f}, {r.interval[1]:.2f}] {miss}")
    if not unit_ok:
        reasons.append(f"the rate is calibrated in another unit than {SWEEP_UNIT.value}, the "
                       f"unit fill_cost.SWEEP_SIZES is fit in")
    return Verdict(not reasons, tuple(reasons))


def tail_share(markets, fit):
    """Realized fill events of at least each BIG_FILLS size: how many and
    their share of the filled shares; and the fit's P(X >= the size)."""
    fills = [f for m in markets for f in in_unit(m.fills, SWEEP_UNIT)]
    total = sum(f.shares for f in fills)
    return [{"size": size, "fills": sum(f.shares >= size for f in fills),
             "share": sum(f.shares for f in fills if f.shares >= size) / total if total else 0.0,
             "fit_p": 1 - NORMAL.cdf((math.log(size) - fit.mu) / fit.sigma) if fit else None}
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
    top = sorted(markets, key=lambda m: realized(m, Window.ALL, split, SWEEP_UNIT).shares,
                 reverse=True)[:n]
    return [{"slug": m.slug, "realized": list(realized(m, Window.ALL, split, SWEEP_UNIT)),
             "predicted": {model.name: predicted(m, model, Window.ALL, split, floor).shares
                           for model in models}}
            for m in top]


# --- the tail cap on current markets -------------------------------------------

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


def tail_effect(current, cfg, prior, hurdles, tail_days_list):
    """Per hurdle rate and tail_days (0, no cap, first): the production
    target_orders on each current market from no position, and what the
    cap does: markets bidding, resting $, planned reward and its share of
    the uncapped reward, the bids it changes or removes, and the tail $; and
    per hurdle the TOP_TRIMMED markets losing the most reward at
    cfg.tail_days."""
    rows, trimmed = [], []
    for hurdle in hurdles:
        at_hurdle = cfg._replace(hurdle_rate=hurdle)
        outlooks = [market_outlook(c.fm, None, c.history, prior, at_hurdle, c.at, any_floor=True)
                    for c in current]
        best = {}
        for tail_days in (0.0,) + tuple(t for t in tail_days_list if t > 0):
            capped = at_hurdle._replace(tail_days=tail_days)
            best[tail_days] = [target_orders(c.fm, c.levels, Position(), capped, True, o).fav.best
                               for c, o in zip(current, outlooks)]
        free = best[0.0]
        free_reward = sum(b.reward_per_day for b in free if b)
        for tail_days, chosen in best.items():
            reward = sum(b.reward_per_day for b in chosen if b)
            rows.append({
                "hurdle_rate": hurdle, "tail_days": tail_days,
                "bidding": sum(b is not None for b in chosen),
                "resting_usd": sum(b.price * b.size for b in chosen if b),
                "reward_per_day": reward,
                "kept": reward / free_reward if free_reward > 0 else None,
                "binds": sum(1 for a, b in zip(free, chosen)
                             if a and (b is None or (b.price, b.size) != (a.price, a.size))),
                "tail_usd": sum(b.tail_usd for b in chosen if b),
            })
        at_cfg = best.get(cfg.tail_days)
        if at_cfg is not None:
            lost = sorted(((a.reward_per_day - (b.reward_per_day if b else 0.0), c, a, b)
                           for c, a, b in zip(current, free, at_cfg) if a),
                          key=lambda x: -x[0])[:TOP_TRIMMED]
            trimmed += [{"hurdle_rate": hurdle, "slug": c.slug, "lost_per_day": d,
                         "free": [a.price, a.size], "capped": [b.price, b.size] if b else None}
                        for d, c, a, b in lost if d > 0]
    return {"markets": len(current), "rows": rows, "trimmed": trimmed}


# --- the report ----------------------------------------------------------------

def models_for(prior, fits):
    """The report's models: DEFAULT_PRIOR with BURST_SIZES, BURST_SIZES on
    the stored prior, production, the size table refit on the window's
    fills, and the whole bid. fits maps a window to the SizeFit refit for
    it, if any."""
    base = [Model("default prior", fill_cost.DEFAULT_PRIOR, BURST_SIZES, SWEEP_UNIT),
            Model("burst table", prior, BURST_SIZES, SWEEP_UNIT),
            Model("production", prior, fill_cost.SWEEP_SIZES, SWEEP_UNIT),
            Model("whole bid", prior, fill_cost.WHOLE_BID, SWEEP_UNIT)]
    return {w: base + ([Model("refit", prior, fits[w].table(), SWEEP_UNIT)] if fits.get(w) else [])
            for w in Window}


def _ratio_json(r):
    return [r.value, list(r.interval) if r.interval else None]


def _score_json(s):
    return {"model": s.model, "window": s.window.value, "predicted": list(s.predicted),
            "realized": list(s.realized), "events": _ratio_json(s.events),
            "shares": _ratio_json(s.shares)}


def _fit_json(fit):
    return {"mu": fit.mu, "sigma": fit.sigma, "n": fit.n, "censored": fit.censored,
            "unit": fit.unit.value}


def _fit_or_reason(fills):
    try:
        return fit_sizes(fills), None
    except ValueError as e:
        return None, str(e)


def report(markets, skipped, fills, current, cfg, prior, split, until, tail_days_list,
           hurdles, reps=REPS):
    """The report as a JSON-safe dict, its sections in render's order."""
    floor = cfg.min_fav
    train = [f for f in fills if f.at < split]
    fit_all, all_reason = _fit_or_reason(fills)
    fit_train, train_reason = _fit_or_reason(train)
    fits = {Window.ALL: fit_all, Window.TRAIN: fit_train, Window.TEST: fit_train}
    by_window = models_for(prior, fits)
    scores = [score(markets, model, w, split, floor, reps) for w in Window for model in by_window[w]]
    production = by_window[Window.ALL][2]
    find = {(s.model, s.window): s for s in scores}
    units = unit_check(markets, prior, fills, split, floor, reps)
    held_out = find.get(("refit", Window.TEST))
    verdict = gate(find[("production", Window.ALL)],
                   held_out or find[("production", Window.TEST)], units["passes"])
    if held_out is None:
        verdict = Verdict(False, verdict.reasons + (f"no held-out size fit: {train_reason}",))
    rested = [r for m in markets for r in m.rested]
    tables = {"production": fill_cost.SWEEP_SIZES, "burst table": BURST_SIZES}
    tables.update({f"fit {w}": f.table() for w, f in (("all", fit_all), ("train", fit_train)) if f})
    km = kaplan_meier(in_unit(fills, SWEEP_UNIT))
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
            "deep_days": sum(r.days for r in rested if r.deep),
            "deep_fills": sum(stretch_of(m, f.at).deep for m in markets for f in m.fills),
            "until": _stamp(until),
            "prior": {"at": prior.at.isoformat(timespec="minutes") if prior.at else None,
                      "markets": prior.markets, "floor": prior.floor},
        },
        "size_fit": {
            "all": _fit_json(fit_all) if fit_all else all_reason,
            "train": _fit_json(fit_train) if fit_train else train_reason,
            "km": [[x, _survival(km, x),
                    1 - NORMAL.cdf((math.log(x) - fit_all.mu) / fit_all.sigma) if fit_all else None]
                   for x in KM_CHECKS],
            "expected_min": {name: [fill_cost.sweep_fill(q, t) for q in SIZE_CHECKS]
                             for name, t in tables.items()},
        },
        "scores": [_score_json(s) for s in scores],
        "buckets": by_bucket(markets, production, Window.ALL, split, floor),
        "buckets_test": by_bucket(markets, production, Window.TEST, split, floor),
        "sources": by_source(markets, production, Window.ALL, split, floor),
        "units": units,
        "tail_share": tail_share(markets, fit_all),
        "stopped_after_big": stopped_after_big(markets),
        "top_markets": top_markets(markets, by_window[Window.ALL], split, floor),
        "tail_effect": tail_effect(current, cfg, prior, hurdles, tail_days_list),
        "gate": {"passes": verdict.passes, "reasons": list(verdict.reasons),
                 "candidate": ["production", "all"] if held_out is None
                 else [["production", "all"], ["refit", "test"]]},
    }


def _stamp(at):
    return None if at is None else datetime.fromtimestamp(at, timezone.utc).isoformat(
        timespec="minutes")


def _num(x, spec):
    return "-" if x is None else format(x, spec)


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
             f"  {d['deep_days']:,.1f} market-days rested behind more than "
             f"{reward_screener.DEPTH_FREE_SHARES:,} shares, which every model charges no "
             f"fills, as value_quote does; {d['deep_fills']} fills landed there",
             f"  floor {d['floor']:g}; data before {d['until']}; train before {d['split']}, "
             f"test from it",
             f"  rates shrink toward the stored prior, fit {d['prior']['at']} on "
             f"{d['prior']['markets']} markets at floor {d['prior']['floor']:g} over "
             f"{fill_cost.HISTORY_DAYS:g} days that overlap the test window; only the size "
             f"table is refit before the split",
             "", "Size fit (censored lognormal of the shares one fill event took)"]
    for name in ("all", "train"):
        f = fit[name]
        lines.append(f"  {name:<6}" + (f"n {f['n']} ({f['censored']} censored) mu {f['mu']:.2f} "
                                       f"sigma {f['sigma']:.2f}, median "
                                       f"{math.exp(f['mu']):,.0f} shares"
                                       if isinstance(f, dict) else f))
    lines.append("  S(x) Kaplan-Meier / fit: " + ", ".join(
        f"{x:,.0f}: {km:.3f} / {_num(p, '.3f')}" for x, km, p in fit["km"]))
    lines.append("  E[min(Q, X)] at Q " + ", ".join(f"{q:,.0f}" for q in SIZE_CHECKS))
    lines += [f"    {name:<14}" + "".join(f"{x:>8,.0f}" for x in values)
              for name, values in fit["expected_min"].items()]
    lines += ["", "Scores (realized / predicted, 90% intervals from resampling markets; "
                  f"events counted per {SWEEP_UNIT.value} event)",
              f"  {'model':<14} {'window':<6} {'events':<13} {'r/p [90%]':<20} "
              f"{'shares':<17} r/p [90%]"]
    for s in r["scores"]:
        lines.append(f"  {s['model']:<14} {s['window']:<6} {s['realized'][0]:>4.0f}/"
                     f"{s['predicted'][0]:<8.1f} {_ratio_text(s['events']):<20} "
                     f"{s['realized'][1]:>8,.0f}/{s['predicted'][1]:<8,.0f} "
                     f"{_ratio_text(s['shares'])}")
    for key, title in (("buckets", "all"), ("buckets_test", "test")):
        lines += ["", f"By predicted events per market, production, {title}"]
        lines += [f"  {b['bucket']:<8} {b['markets']:>4} markets  predicted {b['predicted']:7.1f}  "
                  f"realized {b['realized']:>4}  r/p {_num(b['ratio'], '.2f')}" for b in r[key]]
    lines += ["", "By fill source, production, all"]
    lines += [f"  {s['source']:<28} events {s['realized_events']:>4}/{s['predicted_events']:<7.1f}"
              f" shares {s['realized_shares']:>8,.0f}/{s['predicted_shares']:<8,.0f}"
              for s in r["sources"]]
    u = r["units"]
    lines += ["", "Unit check (test window: the rate's events, and a table fit before the "
                  "split in the unit)"]
    lines += [f"  {row['unit']:<5} events {_ratio_text(row['events']):<20} shares "
              + (_ratio_text(row["shares"]) if row["shares"] else row["fit"]) for row in u["rows"]]
    lines.append(f"  the rate is calibrated per {u['calibrated']} event; fill_cost.SWEEP_SIZES is "
                 f"fit per {SWEEP_UNIT.value} event: {'ok' if u['passes'] else 'MISMATCH'}")
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
              f"  {'hurdle':>6} {'tail days':>9} {'bidding':>7} {'resting $':>10} "
              f"{'reward $/d':>10} {'kept':>5} {'binds':>5} {'tail $':>8}"]
    lines += [f"  {row['hurdle_rate']:>6g} {'none' if row['tail_days'] == 0 else format(row['tail_days'], 'g'):>9} "
              f"{row['bidding']:>7} {row['resting_usd']:>10,.0f} {row['reward_per_day']:>10,.2f} "
              f"{_num(row['kept'], '.0%'):>5} {row['binds']:>5} {row['tail_usd']:>8,.0f}"
              for row in te["rows"]]
    if te["trimmed"]:
        lines.append("  most reward lost at the farmer's tail_days (price x size, uncapped -> "
                     "capped):")
        lines += [f"    {t['hurdle_rate']:>4g} {t['slug'][:44]:<44} -${t['lost_per_day']:.2f}/day  "
                  f"{t['free'][0]:.3f} x {t['free'][1]:,.0f} -> "
                  + (f"{t['capped'][0]:.3f} x {t['capped'][1]:,.0f}" if t["capped"] else "no bid")
                  for t in te["trimmed"]]
    g = r["gate"]
    lines += ["", "Gate",
              f"  production over all, and the table refit before the split over the test: "
              f"for events and shares, the 90% interval must include 1 (all), and the ratio "
              f"lie in [{GATE_BAND[0]:.2f}, {GATE_BAND[1]:.2f}] with its interval reaching it "
              f"(test)",
              f"  {'PASS' if g['passes'] else 'FAIL'}"]
    lines += [f"  - {reason}" for reason in g["reasons"]]
    return "\n".join(lines) + "\n"


# --- CLI -----------------------------------------------------------------------

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
                    args.hurdle_rates, args.reps)
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
    p = sub.add_parser("fit", help="print fill_cost.SWEEP_SIZES fit to the farmer's own fills")
    p.add_argument("--db", required=True, help="farm.db")
    p.add_argument("--until", help=UNTIL_HELP)
    p = sub.add_parser("report", help="score the fill model against the farmer's own fills")
    p.add_argument("--db", required=True, help="farm.db")
    p.add_argument("--until", help=UNTIL_HELP)
    p.add_argument("--books", required=True, help="farm_books.db")
    p.add_argument("--cache", required=True, help="the pricing_backtest fetch cache")
    p.add_argument("--split", required=True, help="UTC date the test window starts")
    p.add_argument("--tail-days", type=float, nargs="*", default=list(TAIL_DAYS),
                   help=f"tail caps to try on current markets (default: "
                        f"{' '.join(f'{t:g}' for t in TAIL_DAYS)})")
    p.add_argument("--hurdle-rates", type=float, nargs="*", default=list(HURDLES),
                   help=f"hurdle rates to try them at (default: "
                        f"{' '.join(f'{h:g}' for h in HURDLES)})")
    p.add_argument("--reps", type=int, default=REPS, help="bootstrap replicates")
    p.add_argument("--report", help="also write the text report to this file")
    p.add_argument("--json", help="also write the result to this file")
    args = parser.parse_args(argv)
    if args.command == "fit":
        until = _until(args.until)
        fills = [f for found in read_fills(args.db, until).values() for f in found]
        print(fit_text(fit_sizes(fills), fills, datetime.fromtimestamp(until, timezone.utc)),
              end="")
    else:
        run_report(args)


if __name__ == "__main__":
    main()
