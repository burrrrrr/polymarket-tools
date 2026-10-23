"""
What a favorite exit is worth, from the taker buys a resting favorite ask
sells into.

A taker that buys the favorite, or sells the longshot, sweeps the favorite
asks from the cheapest up. Its own /trades row prints a volume-weighted
average price, so the sweep is kept as the ladder of maker fills it took,
each a Piece at its own favorite-equivalent price. A market's BuyTape holds
its sweeps as read at read_at, complete from first_at: a stretch of tape
with no sweep is a stretch in which nobody bought.

Raw sweeps are kept, not a digest, so a tape can be replayed as of any past
time and valued again under another end date or recency weight.

A share offered at a sells at the rate the buys say buyers take a's yield,
ln(1 / a) a year over the years left, and one still unsold when the farmer
stops selling is worth p_true at resolution. Both are discounted at the
hurdle rate, so an ExitCurve prices every ask a by the discounted proceeds
V(a) of the inventory resting there.

The rate comes from the buys in yields: a buyer who paid p with y years to
go accepted ln(1 / p) / y, and would take an ask offering more. A sweep
fills at most the inventory resting, and weighs less the older it is. Few
markets see enough buys to be rated alone, so a market's rate of buys and
the shares each takes are shrunk toward an ExitPrior that a scan pools
across markets.

An ExitModel holds all of it for one market and one holding: it chooses
the ask to rest and values what a bid fill adds.

Everything here is pure and imports nothing else from the project; the
screener parses the sweeps and the farmer keeps the tapes in farm.db.
"""

import bisect
import math
import statistics
from datetime import datetime, timezone
from typing import NamedTuple, Optional, Sequence

YEAR_DAYS = 365.25
DAY_S = 86400.0
EXIT_JSON_VERSION = 1
# Sweeps the farmer keeps per market in farm.db, the newest.
TAPE_ROWS_FARMER = 500
# The prior's grid: the yields a year below which it marks the shares a sweep
# took, and the inventories it caps them at.
YIELD_GRID = (0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.55, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
SIZE_GRID = (25.0, 100.0, 300.0, 1000.0, 3000.0, 10000.0)
_YIELDS = (0.0,) + YIELD_GRID
_LOG_SIZES = tuple(math.log(size) for size in SIZE_GRID)
PRIOR_MIN_MARKETS = 20
# Inventory is valued on a 5% log grid of sizes, so bids of nearby sizes share
# one curve.
SIZE_BASE = 1.05
_LOG_SIZE_BASE = math.log(SIZE_BASE)
# The ask chosen is the lowest within this many dollars, over the whole
# holding, of the best: nearest the mid, likeliest to print.
EXIT_TIE_USD = 0.25
# Asks are searched in steps of at least this, so a 0.0001 tick book is not
# 10,000 candidates.
MIN_STRIDE = 0.001


class Piece(NamedTuple):
    """Shares that traded at one favorite-equivalent price inside a sweep."""

    price: float
    size: float


class Sweep(NamedTuple):
    """One taker buy or sell of the favorite: its unix time and its pieces,
    highest price first. A BuyTape holds buys."""

    at: float
    pieces: tuple


class BuyTape:
    """A market's sweeps, oldest first, as read at read_at and complete from
    first_at (unix). Every sweep has at least one piece, each priced inside
    (0, 1) with a positive size, and none is older than first_at. Immutable.
    """

    def __init__(self, sweeps: Sequence[Sweep], read_at: datetime, first_at: float):
        self.sweeps = tuple(sorted(
            (Sweep(s.at, tuple(sorted(s.pieces, key=lambda p: -p.price))) for s in sweeps),
            key=lambda s: s.at))
        self.read_at = read_at
        self.first_at = first_at
        self._views = {}
        if not math.isfinite(first_at):
            raise ValueError(f"first_at {first_at}")
        for s in self.sweeps:
            if not (s.pieces and math.isfinite(s.at) and s.at >= first_at
                    and all(0 < p.price < 1 and 0 < p.size < math.inf for p in s.pieces)):
                raise ValueError(f"not a buy of the favorite: {s}")

    def as_of(self, t):
        """The tape as a read at unix time t would have found it: the sweeps up
        to t, complete from first_at, or from t when that is earlier."""
        return BuyTape([s for s in self.sweeps if s.at <= t],
                       datetime.fromtimestamp(t, timezone.utc), min(self.first_at, t))

    def view(self, end_ts, half_life_days):
        """The TapeView to the end date end_ts (unix) at this half-life, built once."""
        key = end_ts, half_life_days
        view = self._views.get(key)
        if view is None:
            view = self._views[key] = TapeView(self, end_ts, half_life_days)
        return view


class TapeView:
    """A tape's pieces priced in yields to one end date, its sweeps weighted
    for one half-life.

    yields holds each piece's ln(1 / price) over the years from its sweep to
    the end, ascending. A sweep weighs 2 ** (-age / half_life) by its age at
    the read, not at now, so a stored tape values the same all hour. W sums
    the weights, the effective sweeps; D integrates the weight over the span
    the tape is complete for, the effective days of exposure.
    """

    def __init__(self, tape, end_ts, half_life_days):
        read_ts = tape.read_at.timestamp()
        decay = math.log(2) / (half_life_days * DAY_S)
        self._weights = [math.exp(-decay * max(0.0, read_ts - s.at)) for s in tape.sweeps]
        pieces = sorted((_yield(p.price, end_ts - s.at), i, p.size)
                        for i, s in enumerate(tape.sweeps) for p in s.pieces)
        self.yields = [r for r, _, _ in pieces]
        self._pieces = [(i, size) for _, i, size in pieces]
        self.W = sum(self._weights)
        self.D = -math.expm1(-decay * max(0.0, read_ts - tape.first_at)) / (decay * DAY_S)
        self._cums = {}

    def cum(self, size):
        """cum[j]: the weighted shares the sweeps took in the first j pieces by
        yield, each sweep's capped at size, since a buy fills at most the
        inventory resting. Built once per size."""
        cum = self._cums.get(size)
        if cum is None:
            took = [0.0] * len(self._weights)
            total, cum = 0.0, [0.0]
            for i, shares in self._pieces:
                before = took[i]
                took[i] = before + shares
                total += self._weights[i] * (min(size, took[i]) - min(size, before))
                cum.append(total)
            self._cums[size] = cum
        return cum


def _yield(price, seconds_left):
    """The return a year of buying at price with seconds_left to resolution; a
    buy at or past the end accepted any."""
    if seconds_left <= 0:
        return math.inf
    return math.log(1 / price) / (seconds_left / DAY_S / YEAR_DAYS)


class ExitParams(NamedTuple):
    """The estimator's free parameters: a sweep's weight halves every
    half_life_days, and the prior counts as arrival_days of exposure toward
    the rate of buys and as mark_buys sweeps toward the shares each takes."""

    half_life_days: float
    arrival_days: float
    mark_buys: float


# A market's own buys of the last days, with no weight on the prior: the
# setting whose forecasts match realised sales in analysis/exit_backtest.py calibrate
# on markets 60+ days from resolution. The prior, pooled by yield, reads far
# markets' cheap buys as demand that is not there. Unproven under 60 days.
EXIT_PARAMS = ExitParams(half_life_days=5.0, arrival_days=0.0, mark_buys=0.0)


class ExitPrior(NamedTuple):
    """The cross-market prior: arrival_per_day effective sweeps a day, and
    marks[j][k] the shares a sweep took at yields up to YIELD_GRID[j], capped
    at SIZE_GRID[k], each a mean over markets. markets 0 and at None mark
    DEFAULT_EXIT_PRIOR."""

    arrival_per_day: float
    marks: tuple
    markets: int
    at: Optional[datetime]

    def mark(self, r, size):
        """The shares a sweep takes at yields up to r, capped at size: bilinear
        in r and ln size, 0 at r = 0 and flat past the grid."""
        i = bisect.bisect_right(_YIELDS, r)
        if i == 0:
            return 0.0
        x = math.log(size)
        row = lambda j: _lerp(_LOG_SIZES, self.marks[j - 1], x) if j else 0.0
        if i == len(_YIELDS):
            return row(i - 1)
        lo = row(i - 1)
        return lo + (r - _YIELDS[i - 1]) / (_YIELDS[i] - _YIELDS[i - 1]) * (row(i) - lo)


def _lerp(xs, ys, x):
    """ys over xs at x: linear between grid points, flat outside them."""
    i = bisect.bisect_right(xs, x)
    if i == 0:
        return ys[0]
    if i == len(xs):
        return ys[-1]
    return ys[i - 1] + (x - xs[i - 1]) / (xs[i] - xs[i - 1]) * (ys[i] - ys[i - 1])


def sales_rate(view, prior, params, size):
    """The shares a year of `size` resting that take an ask offering yield r,
    as a function of r: YEAR_DAYS * nu * g(r).

    nu, the sweeps a day, is the view's W over its D, shrunk toward the
    prior's arrival rate by arrival_days of exposure. g(r), the shares a sweep
    takes at yields up to r capped at size, is the view's over its W, shrunk
    toward the prior's mark by mark_buys sweeps. No view is the prior alone.
    """
    if view is None:
        nu = prior.arrival_per_day
        return lambda r: YEAR_DAYS * nu * prior.mark(r, size)
    nu = _shrunk(view.W, view.D, prior.arrival_per_day, params.arrival_days)
    yields, cum, weight, strength = view.yields, view.cum(size), view.W, params.mark_buys
    return lambda r: YEAR_DAYS * nu * _shrunk(cum[bisect.bisect_right(yields, r)], weight,
                                              prior.mark(r, size), strength)


def _shrunk(seen, exposure, prior_rate, strength):
    total = exposure + strength
    return (seen + strength * prior_rate) / total if total > 0 else prior_rate


def fit_exit_prior(markets, params, at):
    """The prior fit from the (BuyTape, end_date) of the markets a scan read,
    or None when fewer than PRIOR_MIN_MARKETS have any exposure (the caller
    keeps the prior it had).

    The arrival rate is the median over those markets of W / D, so a few busy
    markets do not set it for all. Each mark is the mean, over the markets
    with at least one effective sweep, of the capped shares their sweeps took
    at yields up to the grid's, over their W.
    """
    views = [tape.view(end_date.timestamp(), params.half_life_days) for tape, end_date in markets]
    usable = [v for v in views if v.D > 0]
    if len(usable) < PRIOR_MIN_MARKETS:
        return None
    seen = [v for v in usable if v.W >= 1]
    marks = tuple(
        tuple(sum(v.cum(size)[bisect.bisect_right(v.yields, r)] / v.W for v in seen) / len(seen)
              if seen else 0.0 for size in SIZE_GRID)
        for r in YIELD_GRID)
    return ExitPrior(statistics.median(v.W / v.D for v in usable), marks, len(usable), at)


# fit_exit_prior at EXIT_PARAMS over the tapes of the 237 approved markets,
# read as the farmer reads them on 2026-10-09; used until a scan stores a
# prior of its own. Provisional until analysis/exit_backtest.py refits it.
DEFAULT_EXIT_PRIOR = ExitPrior(
    arrival_per_day=0.6158,
    marks=(
        (1.249, 2.19, 2.753, 3.125, 3.145, 3.145),
        (4.132, 8.66, 12.08, 15.21, 18.18, 21.73),
        (8.192, 18.24, 27.05, 35.94, 42.15, 49.06),
        (10.73, 24.29, 36.81, 49.85, 58.66, 68.04),
        (13.39, 30.29, 46.51, 63.42, 73.57, 83.37),
        (14.79, 33.08, 50.62, 69.2, 80.13, 90.11),
        (15.89, 35.58, 54.26, 74.33, 86.53, 97.03),
        (16.7, 37.68, 57.69, 79.03, 91.88, 102.8),
        (17.32, 39.22, 60.13, 82.13, 95.4, 106.8),
        (17.59, 39.77, 61.07, 83.56, 97.1, 108.7),
        (17.74, 40.11, 61.67, 84.35, 98.04, 109.6),
        (17.86, 40.36, 62.15, 85.1, 98.89, 110.5),
        (17.89, 40.42, 62.25, 85.21, 99.0, 110.6),
        (17.92, 40.49, 62.33, 85.29, 99.08, 110.7),
    ),
    markets=0,
    at=None,
)


def tape_to_json(tape, rows):
    """A BuyTape as a JSON-safe dict holding its newest `rows` sweeps. A tape
    cut short is complete from its oldest kept sweep."""
    sweeps, first_at = tape.sweeps, tape.first_at
    if len(sweeps) > rows:
        sweeps = sweeps[-rows:]
        first_at = sweeps[0].at
    return {
        "v": EXIT_JSON_VERSION,
        "read_at": tape.read_at.isoformat(),
        "first_at": first_at,
        "sweeps": [[s.at, [[p.price, p.size] for p in s.pieces]] for s in sweeps],
    }


def tape_from_json(data) -> Optional[BuyTape]:
    """The BuyTape a tape_to_json dict holds, or None for anything else:
    another version, sweeps out of time order, a sweep that is not a buy of
    the favorite, a malformed value. Never raises."""
    try:
        if data.get("v") != EXIT_JSON_VERSION:
            return None
        sweeps = [Sweep(float(at), tuple(Piece(float(price), float(size))
                                         for price, size in pieces))
                  for at, pieces in data["sweeps"]]
        if any(a.at > b.at for a, b in zip(sweeps, sweeps[1:])):
            return None
        return BuyTape(sweeps, datetime.fromisoformat(data["read_at"]), float(data["first_at"]))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def exit_prior_to_json(prior):
    """An ExitPrior as a JSON-safe dict carrying its grid."""
    return {
        "v": EXIT_JSON_VERSION,
        "yields": list(YIELD_GRID),
        "sizes": list(SIZE_GRID),
        "arrival_per_day": prior.arrival_per_day,
        "marks": [list(row) for row in prior.marks],
        "markets": prior.markets,
        "at": prior.at.isoformat() if prior.at else None,
    }


def exit_prior_from_json(data) -> Optional[ExitPrior]:
    """The ExitPrior an exit_prior_to_json dict holds, or None for anything
    else: another version or grid, a malformed value. Never raises."""
    try:
        if (data.get("v") != EXIT_JSON_VERSION or tuple(data["yields"]) != YIELD_GRID
                or tuple(data["sizes"]) != SIZE_GRID):
            return None
        at = data["at"]
        prior = ExitPrior(float(data["arrival_per_day"]),
                          tuple(tuple(float(m) for m in row) for row in data["marks"]),
                          data["markets"], datetime.fromisoformat(at) if at is not None else None)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    valid = (math.isfinite(prior.arrival_per_day) and prior.arrival_per_day >= 0
             and len(prior.marks) == len(YIELD_GRID)
             and all(len(row) == len(SIZE_GRID) and all(math.isfinite(m) and m >= 0 for m in row)
                     for row in prior.marks)
             and type(prior.markets) is int and prior.markets >= 0)
    return prior if valid else None


class ExitPolicy(NamedTuple):
    """The farmer's exit policy: the lowest ask sits min_capture over cost, and
    capital costs hurdle_rate a year."""

    min_capture: float
    hurdle_rate: float


class Holding(NamedTuple):
    """The favorite inventory an exit is valued around: none for the screener
    and the console, the position for the farmer."""

    shares: float = 0.0
    avg_cost: float = 0.0


class ExitValue(NamedTuple):
    """Inventory of `size` shares resting at `price`, per share unless named.
    per_share is the discounted proceeds, an unsold remainder valued at p_true
    at resolution; hold_value the same share never offered; sold_per_year the
    shares a year that take the ask; hold_days the inventory's hold at that
    rate, cut short at resolution."""

    price: float
    size: float
    per_share: float
    hold_value: float
    sold_per_year: float
    hold_days: float

    @property
    def gain(self):
        return self.per_share - self.hold_value


class ExitCurve:
    """The value of `size` shares resting at any ask on one market.

    sold_per_year(r) is the shares a year that take an ask offering yield r.
    The farmer sells for sell_years and the share resolves at years, both from
    now, sell_years <= years; delta is the hurdle rate as a continuous rate.
    The asks that may rest are a grid of max(tick, MIN_STRIDE) steps up to
    1 - tick, valued once each, from the top down as far as a caller reaches.
    """

    def __init__(self, size, sold_per_year, p_true, years, sell_years, delta, tick):
        self.size = size
        self.p_true = p_true
        self.years = years
        self.sell_years = sell_years
        self.delta = delta
        self.hold_value = p_true * math.exp(-delta * years)
        self._sold_per_year = sold_per_year
        self._last = 1 - tick
        self._stride = max(tick, MIN_STRIDE)
        self._top = math.floor(round(self._last / self._stride, 6))
        self._down = []
        self._best = []

    def value_at(self, price):
        """V(a) = a * lam / k * (1 - exp(-k * Ys)) + p_true * exp(-lam * Ys - delta * Y),
        lam the inventory's sales a year at a's yield, k = lam + delta, at any price."""
        sold = self._sold_per_year(math.log(1 / price) / self.years if self.years > 0
                                   else math.inf)
        lam = sold / self.size
        k = lam + self.delta
        ys = self.sell_years
        selling = lam * ys if k == 0 else lam / k * -math.expm1(-k * ys)
        per_share = (price * selling
                     + self.p_true * math.exp(-lam * ys - self.delta * self.years))
        held = min(self.size / sold, self.years) if sold > 0 else self.years
        return ExitValue(price, self.size, per_share, self.hold_value, sold, YEAR_DAYS * held)

    def values_from(self, price):
        """The values at price and at each grid ask over it, ascending; none
        when price is over 1 - tick."""
        if price > self._last + 1e-9:
            return []
        k = self._grid_from(price)
        grid = self._down[self._top - k::-1] if k <= self._top else []
        return grid if self._on_grid(price, k) else [self.value_at(price)] + grid

    def best_from(self, price):
        """The best of values_from(price), the lowest price of equals; None when
        there is none. O(1) once the grid is valued down to price."""
        if price > self._last + 1e-9:
            return None
        k = self._grid_from(price)
        best = self._best[self._top - k] if k <= self._top else None
        if not self._on_grid(price, k):
            v = self.value_at(price)
            if best is None or v.per_share >= best.per_share:
                best = v
        return best

    def _grid_from(self, price):
        """The index of the lowest grid ask at or over price, valued down to it."""
        k = max(1, math.ceil(round(price / self._stride, 6)))
        while len(self._down) <= self._top - k:
            v = self.value_at(round((self._top - len(self._down)) * self._stride, 10))
            self._best.append(v if not self._best or v.per_share >= self._best[-1].per_share
                              else self._best[-1])
            self._down.append(v)
        return k

    def _on_grid(self, price, k):
        return abs(price / self._stride - k) < 1e-6


class ExitChoice(NamedTuple):
    """The ask to rest and the curve it was chosen on. floor is the value at
    lowest, what the lowest ask alone would earn. best None: no inventory, or
    no ask can rest (reason says which). source: "tape" or "prior"."""

    best: Optional[ExitValue] = None
    lowest: Optional[float] = None
    floor: Optional[ExitValue] = None
    curve: Optional[ExitCurve] = None
    source: str = ""
    reason: str = ""

    @property
    def price(self):
        return self.best and self.best.price


class ExitModel:
    """What leaving a favorite position is worth on one market.

    Built from stored evidence in O(1): the tape (None values on the prior
    alone, from_tape False), the prior and params that shrink it, p_true, the
    end date the share resolves at and expires_at the farmer stops selling
    at, the tick, and the holding the exits are valued around. A curve is
    built per inventory bucket on first use and kept, and the tape keeps its
    views and cumulatives, so the candidate bids of a cycle cost lookups.
    """

    def __init__(self, tape, prior, params, policy, p_true, end_date, expires_at, now, tick,
                 holding=Holding()):
        self.tape = tape
        self.prior = prior
        self.params = params
        self.policy = policy
        self.p_true = p_true
        self.end_date = end_date
        self.tick = tick
        self.holding = holding
        self.from_tape = tape is not None
        self.years = _years(end_date, now)
        self.sell_years = min(self.years, _years(expires_at, now))
        self.delta = math.log1p(policy.hurdle_rate)
        self._curves = {}
        self._held = None

    def curve(self, size):
        """The ExitCurve of size shares, on the 5% log grid of sizes."""
        bucket = round(math.log(size) / _LOG_SIZE_BASE)
        curve = self._curves.get(bucket)
        if curve is None:
            n = SIZE_BASE ** bucket
            view = (None if self.tape is None
                    else self.tape.view(self.end_date.timestamp(), self.params.half_life_days))
            curve = self._curves[bucket] = ExitCurve(
                n, sales_rate(view, self.prior, self.params, n), self.p_true, self.years,
                self.sell_years, self.delta, self.tick)
        return curve

    def choose(self, lowest):
        """The ask for the holding at lowest or above: the lowest whose value is
        within EXIT_TIE_USD over the holding of the best, so a curve with
        nothing to gain rests at lowest. lowest is the caller's policy floor."""
        source = "tape" if self.from_tape else "prior"
        shares = self.holding.shares
        if shares <= 0:
            return ExitChoice(lowest=lowest, source=source, reason="no inventory")
        curve = self.curve(shares)
        values = curve.values_from(lowest)
        if not values:
            return ExitChoice(lowest=lowest, curve=curve, source=source, reason="no ask under 1")
        top = max(v.per_share for v in values)
        best = next(v for v in values if v.per_share >= top - EXIT_TIE_USD / shares)
        return ExitChoice(best, lowest, values[0], curve, source)

    def fill_value(self, added, bid_price):
        """The exit value per share of the `added` shares a bid fill at
        bid_price adds to the holding: (W(H + added, c') - W(H, c)) / added,
        W(n, c) = n * the best ask's value at or over c + min_capture, c' the
        cost blended over the fill, so the floor binds as it will for the real
        ask. Marginal, as a fill also lengthens the hold of the shares held.
        Its price is the post-fill best ask, None when no ask can rest over
        the floor and the shares are held to resolution."""
        shares, cost = self.holding
        if self._held is None:
            self._held = shares * self._best(shares, cost).per_share if shares > 0 else 0.0
        total = shares + added
        best = self._best(total, (shares * cost + added * bid_price) / total)
        return best._replace(size=added, per_share=(total * best.per_share - self._held) / added)

    def _best(self, size, cost):
        curve = self.curve(size)
        floor = round(math.ceil(round((cost + self.policy.min_capture) / self.tick, 6))
                      * self.tick, 10)
        return curve.best_from(floor) or ExitValue(None, curve.size, curve.hold_value,
                                                   curve.hold_value, 0.0,
                                                   YEAR_DAYS * self.years)


def _years(t, now):
    return max(0.0, (t - now).total_seconds() / DAY_S / YEAR_DAYS)
