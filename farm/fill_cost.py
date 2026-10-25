"""
How often a resting favorite bid fills, and what a filled share costs.

A fill is not free: the share ties up capital, which has a hurdle rate r,
until it leaves. The farmer offers a filled share back through FAV_ASK, and
that ask sells slowly: 67% of a 100-share inventory and 42% of a 1,000-share
one in 30 days (SOLD). So a share is sold after an exponential hold T of rate
lambda a year, sales_per_day of the inventory the fill leaves the farmer
holding, cut short at resolution Y years out, where a share still unsold is
worth p_true. The shares that sell go at about that ask's price, so the sale
is at the ask the farmer would rest for the inventory, never credited over
p_true, and at p_true for a caller with no ask to give. With
delta = ln(1 + r) and k = lambda + delta, a share bought at b that sells at a
loses L = b - share_value today,
share_value = a * lambda / k * (1 - exp(-k * Y)) + p_true * exp(-k * Y).
reward_screener.value_quote is the one place that puts these together. On a
short-dated market L can be negative. A bid rests for its reward, so its
QuoteValue charges such fills nothing and never counts them as income.

A bid fills when a sweep reaches past it: taker trades hitting favorite bids
in one burst, whose deepest fill trades below the bid. A market's History
counts, at each distance in DISTANCES, the sweeps that reached that far below
the mid they found, over its last HISTORY_DAYS days of trades, at most one an
hour. Few markets see enough sweeps to be rated alone, so each count is
shrunk toward a Prior that a scan pools across markets, by as many days of
evidence as the markets' spread around the pooled rate is worth. A sweep
counted at a bid's distance brings Sweeps.sellers sellers to it, each
selling X shares, and of the A shares bid ahead of the bid, counting no
more than DEPTH_CREDIT_MAX, the share Sweeps.hold holds, so each fills
min(Q, max(0, X - hold * A)) of a bid of Q. X is heavy-tailed, so a bid of
Q shares fills SWEEPS.fill(Q, A) times the FillCurve's rate at its distance
a day: about Q per seller for a small
bid at the front of the book, little more than the typical seller for a
large one, and less the deeper the book ahead. The tail cap's sweep is one
seller selling X's TAIL_QUANTILE quantile (Sweeps.dump).

A History also times how long the mid took to be back where each sweep at
EXIT_DISTANCE found it (exits, held_days), a Prior pools that
(exit_rate, exit_strength) and a FillCurve carries the shrunk rate
(exit_per_day). Fills were valued on that clock until the ask's sales were
measured: the mid is back within days, and the ask sells over weeks. Nothing
values a fill with it now. It is still read, fit and stored, because the
rows in farm.db carry it, and dropping it would change JSON_VERSION and stop
every bid until each market's history is read again.

Everything here is pure; the screener reads trades and mids, and calls
make_history and fit_prior.
"""

import bisect
import math
from datetime import datetime
from statistics import NormalDist
from typing import Iterable, NamedTuple, Optional

# Hits chain into one sweep while each lands within SWEEP_GAP_S of the last
# and the sweep spans at most MAX_SWEEP_SPAN_S, so a slow drip of small sells
# cannot add up to one book-clearing sweep.
SWEEP_GAP_S = 60.0
MAX_SWEEP_SPAN_S = 600.0
DAY_S = 86400.0
YEAR_DAYS = 365.25

# Distances below the pre-sweep mid, in price units, at which a market's fill
# events are counted and the prior is fit. Dense near zero because tick-0.001
# books quote fractions of a cent off the mid; the far end covers the widest
# reward band.
DISTANCES = (0.0, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.075, 0.10)
# The exit clock runs on the sweeps reaching more than 1c below the mid, about
# where a favorite bid rests. One exit rate a market, not one per distance:
# the mid was back after 5.0, 7.2 and 7.6 days at 1, 1.5 and 2c.
EXIT_DISTANCE = 0.01
HISTORY_DAYS = 60.0
# A sweep is measured against the minute mid at least this long before it
# starts, so its own trades have not moved it yet.
PRE_SWEEP_S = 60.0
# Sweeps this close to the last counted one are one event: a burst of sweeps
# fills a resting bid once.
DECLUSTER_S = 3600.0
PRIOR_MIN_MARKETS = 20
STRENGTHS = tuple(10 ** (x / 10) for x in range(-10, 41))
# About one event in 27 years, so a distance no market ever reached is still
# not a free bid.
FLOOR_RATE = 1e-4
# A sweep fills a bid only by trading strictly below it; this absorbs float
# error in mid - price.
THROUGH_EPS = 1e-9
JSON_VERSION = 2

# What became of a filled share, measured on 2026-10-10 on the 250 markets
# `analysis/exit_backtest.py fetch` and `analysis/fill_backtest.py fetch` cached. A new
# measurement replaces the numbers here.
#
# (shares a sweep filled, the fraction of them the farmer's ask sold in the
# SOLD_DAYS after): every sweep of the cache replayed against the buys that
# followed it, the ask priced as the farmer prices it and a buy at or above
# it selling. The 30-day "sold >=" column of section 4 of replay.txt, 5,078
# bids on 169 markets, from
#   python work\exit-pricing\replay\replay.py --out work\exit-pricing\replay --reps 2000
# whose replay_rows.csv has one row per bid, size, horizon and fill rule: the
# mean of `sold` over the rows of a size with H 30 and rule at-or-above. The
# same column has 2,000 shares at 0.309, where the line through SOLD says
# 0.342.
SOLD_DAYS = 30.0
SOLD = ((100.0, 0.674), (1000.0, 0.419))
# The line through SOLD passes this near 28,000 shares, far past the largest
# fill replayed, and turns negative past 44,000.
MIN_SOLD = 0.05

TAIL_QUANTILE = 0.99
# No more of the shares bid ahead than this hold in front of a bid. The
# farmer's bids behind more were still filled, whole ones included, where
# the hold of all of them would stop every seller (fill_calibration report,
# by shares ahead).
DEPTH_CREDIT_MAX = 10_000.0
_NORMAL = NormalDist()


class Sweeps(NamedTuple):
    """What one sweep a FillCurve counts at a bid's distance does to the bid.
    It brings `sellers` sellers to the bid's price, each selling X shares, X
    lognormal(mu, sigma), sigma 0 being X = exp(mu) every time and mu inf a
    seller who fills any bid whole. Of the shares bid ahead of the bid, the
    share `hold` holds in front of it, up to DEPTH_CREDIT_MAX of them, and
    the rest are cancelled or swept with it, so a seller fills
    min(Q, max(0, X - held(ahead))) of a bid of Q. The tail is one seller
    dumping X's quantile-th quantile."""

    mu: float
    sigma: float
    hold: float = 0.0
    sellers: float = 1.0
    quantile: float = TAIL_QUANTILE

    def survival(self, x):
        """P(X > x)."""
        if x <= 0:
            return 1.0
        if self.sigma == 0:
            return 1.0 if math.log(x) < self.mu else 0.0
        return 0.5 * math.erfc((math.log(x) - self.mu) / self.sigma / math.sqrt(2))

    def _partial(self, x):
        """E[X; X <= x]."""
        if x <= 0:
            return 0.0
        if self.sigma == 0:
            return math.exp(self.mu) if self.mu <= math.log(x) else 0.0
        return (math.exp(self.mu + self.sigma ** 2 / 2)
                * _NORMAL.cdf((math.log(x) - self.mu - self.sigma ** 2) / self.sigma))

    def held(self, ahead):
        """The shares of `ahead` that hold in front of the bid."""
        return self.hold * min(ahead, DEPTH_CREDIT_MAX)

    def fill(self, shares, ahead=0.0):
        """The shares of a bid of `shares`, `ahead` bid ahead of it, that one
        sweep's sellers fill on average: sellers times the integral of
        P(X > x) from held(ahead) over the next `shares`."""
        lo = self.held(ahead)
        hi = lo + shares
        return self.sellers * (hi * self.survival(hi) - lo * self.survival(lo)
                               + self._partial(hi) - self._partial(lo))

    def events(self, ahead=0.0):
        """The fills one sweep brings a bid with `ahead` bid ahead of it: its
        sellers who get past the shares that hold."""
        return self.sellers * self.survival(self.held(ahead))

    def per_fill(self, shares, ahead=0.0):
        """The shares one fill takes, on average: the inventory a fill leaves.
        0 when no seller gets past the bids ahead. Never over `shares`, which
        far in the tail the ratio of two vanishing terms can pass."""
        events = self.events(ahead)
        return min(shares, self.fill(shares, ahead) / events) if events > 0 else 0.0

    @property
    def dump(self):
        """The shares the tail's seller brings."""
        return math.exp(self.mu + self.sigma * _NORMAL.inv_cdf(self.quantile))

    def tail(self, shares, ahead=0.0):
        """The shares of the bid the tail's seller fills."""
        return min(shares, max(0.0, self.dump - self.held(ahead)))


# What a seller brings to a bid, a censored lognormal, the share of the shares
# bid ahead of the bid that holds, and the sellers (FILL events) a counted
# sweep brings, fit jointly to 225 fills on 113 of the 630 markets
# that rested a bid, shares ahead counted up to 10,000, from
#   python -m analysis.fill_calibration fit --db data\farm.db --books data\farm_books.db
#       --cache work\fill-pricing\cache --until 2026-10-25T09:45:00Z
# against the scan prior fit 2026-10-24T02:40:38.263924+00:00 on 60 markets: sellers
# scales that prior's rates, so a new prior needs a refit.
SWEEPS = Sweeps(mu=3.9180, sigma=1.7609, hold=0.1765, sellers=3.1310)
SWEEPS_PRIOR_AT = datetime.fromisoformat("2026-10-24T02:40:38.263924+00:00")
WHOLE_BID = Sweeps(math.inf, 0.0)


def share_value(p_true, sale_price, per_day, years, hurdle_rate):
    """What a filled share is worth today at the hurdle rate: sale_price
    after an exponential hold of rate per_day, cut short at resolution
    `years` out, where a share still unsold is worth p_true,
    sale_price * lam / k * (1 - exp(-k * Y)) + p_true * exp(-k * Y)
    with delta = ln(1 + hurdle_rate), lam the sales a year, k = lam + delta.
    No sales leave p_true discounted to resolution."""
    lam = per_day * YEAR_DAYS
    k = lam + math.log1p(hurdle_rate)
    sold = 0.0 if k == 0 else lam / k * -math.expm1(-k * years)
    return sale_price * sold + p_true * math.exp(-k * years)


def sales_per_day(shares):
    """The rate, a day, at which the farmer's ask sells a share out of an
    inventory of `shares`: the hazard under which SOLD's fraction is gone in
    SOLD_DAYS. The fraction is linear in ln(shares) through SOLD's two sizes,
    the smaller size's for anything smaller, and never under MIN_SOLD."""
    (small, most), (large, less) = SOLD
    sold = most if shares <= small else max(MIN_SOLD, most + (less - most)
                                            * math.log(shares / small) / math.log(large / small))
    return -math.log1p(-sold) / SOLD_DAYS


def years_until(end_date, now):
    """Years to resolution, never negative: where a filled share's hold is
    cut short and what is unsold pays out. The end date, not the expiry
    buffer: the farmer retires early, but the share does not resolve early."""
    return max(0.0, (end_date - now).total_seconds() / DAY_S / YEAR_DAYS)


class Hit(NamedTuple):
    """One taker trade that hit favorite bids: its unix time and the deepest
    favorite-equivalent price it filled at, the makers it filled included."""

    at: float
    deep: float


class History(NamedTuple):
    """One market's fill evidence. events[i] counts the declustered sweeps that
    reached more than DISTANCES[i] below the pre-sweep mid while that mid was
    at or above floor, over `days` of usable history ending at read_at.

    events is non-increasing: a farther distance counts a subset of the
    sweeps, and greedy declustering never counts more of a subset.
    0 <= days <= HISTORY_DAYS.

    Each event counted at EXIT_DISTANCE starts a hold that ends when the mid
    is back at or above the mid the sweep found. exits counts the holds that
    ended, at most that event count, and held_days sums every hold, an
    unended one up to read_at.
    """

    events: tuple
    days: float
    read_at: datetime
    floor: float
    exits: int
    held_days: float


class Prior(NamedTuple):
    """The cross-market prior per DISTANCES point: the pooled events a day and
    the prior's strength in days, fit from `markets` histories read with this
    floor. markets 0 and at None mark DEFAULT_PRIOR. exit_rate is the pooled
    exits per held day, and exit_strength its strength in held days."""

    rates: tuple
    strengths: tuple
    markets: int
    at: Optional[datetime]
    floor: float
    exit_rate: float
    exit_strength: float


class FillCurve(NamedTuple):
    """Events a day that sweep past a bid at each DISTANCES point,
    non-increasing in distance. from_history says the market's own history
    went into it; False is the prior alone. exit_per_day is how often a day
    the mid was back where a sweep found it, which no valuation reads.
    sweeps is what a sweep reaching a bid fills of it: the fitted SWEEPS,
    or others for a backtest's arm."""

    rates: tuple
    from_history: bool
    exit_per_day: float
    sweeps: Sweeps = SWEEPS

    def per_day(self, distance):
        """Events a day reaching past a bid `distance` below the mid, linear
        between grid points and flat outside them. A bid of Q shares with A
        ahead of it fills sweeps.fill(Q, A) * per_day(distance) a day."""
        i = bisect.bisect_right(DISTANCES, distance)
        if i == 0:
            return self.rates[0]
        if i == len(DISTANCES):
            return self.rates[-1]
        lo, hi = DISTANCES[i - 1], DISTANCES[i]
        weight = (distance - lo) / (hi - lo)
        return self.rates[i - 1] + weight * (self.rates[i] - self.rates[i - 1])


# fit_prior over 256 markets of the 2026-10-05 backtest cache whose favorite
# was quotable; used until a scan stores a prior of its own.
DEFAULT_PRIOR = Prior(
    rates=(0.2272, 0.1753, 0.09013, 0.07785, 0.05608, 0.03205, 0.02107, 0.009753,
           0.006009, 0.004354, 0.002264, 0.00148),
    strengths=(1.995, 2.512, 6.31, 7.943, 10.0, 15.85, 25.12, 63.1, 79.43, 125.9,
               1000.0, 1000.0),
    markets=0,
    at=None,
    floor=0.90,
    # The exit rate is fit_prior's over the 74 approved markets of
    # 2026-10-05, a set biased toward markets that recovered.
    exit_rate=0.2143,
    exit_strength=3.981,
)


def make_history(hits: Iterable[Hit], mids, start, now, floor):
    """A market's History from its hits on favorite bids and the favorite's
    minute mids, (unix t, price) sorted by t.

    start is the unix time the trades behind the hits are complete from.
    Usable history runs from the latest of start, now - HISTORY_DAYS and the
    first mid + PRE_SWEEP_S, up to now; no mids leaves none. Hits chain into
    sweeps on SWEEP_GAP_S and MAX_SWEEP_SPAN_S, and a sweep counts at its
    first hit's time and its deepest price.

    A hold ends at the first mid at least PRE_SWEEP_S after its sweep began,
    so the minute the sweep moved is not taken for a recovery.
    """
    end = now.timestamp()
    start = max(start, end - HISTORY_DAYS * DAY_S, mids[0][0] + PRE_SWEEP_S if mids else end)
    times = [t for t, _ in mids]
    reached = []
    for at, deep in _sweeps(hits):
        if start <= at < end:
            mid = mids[bisect.bisect_right(times, at - PRE_SWEEP_S) - 1][1]
            if mid >= floor:
                reached.append((at, mid, mid - deep))
    counted = [_declustered(reached, d) for d in DISTANCES]
    exits, held = 0, 0.0
    for at, mid, _ in counted[DISTANCES.index(EXIT_DISTANCE)]:
        exit_at = _recovery(mids, times, at, mid, end)
        if exit_at is None:
            held += end - at
        else:
            exits, held = exits + 1, held + exit_at - at
    return History(tuple(len(c) for c in counted), max(0.0, (end - start) / DAY_S), now, floor,
                   exits, held / DAY_S)


def _recovery(mids, times, at, mid, end):
    """The time of the first mid from at + PRE_SWEEP_S, before end, at or
    above mid; None when there is none."""
    for i in range(bisect.bisect_left(times, at + PRE_SWEEP_S), len(mids)):
        t, price = mids[i]
        if t >= end:
            return None
        if price >= mid:
            return t
    return None


def _sweeps(hits):
    """[(first hit's time, deepest price)] of the hits, chained into sweeps."""
    sweeps, last = [], None
    for at, deep in sorted(hits):
        if sweeps and at - last <= SWEEP_GAP_S and at - sweeps[-1][0] <= MAX_SWEEP_SPAN_S:
            sweeps[-1] = (sweeps[-1][0], min(sweeps[-1][1], deep))
        else:
            sweeps.append((at, deep))
        last = at
    return sweeps


def _declustered(reached, distance):
    """The (at, mid, reach) sweeps counted as events at distance: those
    reaching past it, oldest first, one within DECLUSTER_S of the last one
    counted folded into it."""
    counted = []
    for sweep in reached:
        at, _, reach = sweep
        if reach > distance + THROUGH_EPS and (not counted or at - counted[-1][0] > DECLUSTER_S):
            counted.append(sweep)
    return counted


def fit_prior(histories: Iterable[History], at, floor):
    """The prior fit from the histories read with this floor, or None when
    fewer than PRIOR_MIN_MARKETS of them have any days (the caller keeps the
    prior it had).

    Per grid point the rate is the pooled events over the pooled days, and the
    strength the STRENGTHS value under which the markets' counts are likeliest
    as negative binomials around that rate. No events anywhere leaves rate 0
    at the largest strength. The exit rate pools the same way, exits over
    held days, across the histories with any held days; with none, it is
    DEFAULT_PRIOR's.
    """
    usable = [h for h in histories if h.days > 0 and h.floor == floor]
    if len(usable) < PRIOR_MIN_MARKETS:
        return None
    days = [h.days for h in usable]
    rates, strengths = zip(*(_pooled([h.events[i] for h in usable], days)
                             for i in range(len(DISTANCES))))
    held = [h for h in usable if h.held_days > 0]
    exit_rate, exit_strength = (
        _pooled([h.exits for h in held], [h.held_days for h in held]) if held
        else (DEFAULT_PRIOR.exit_rate, DEFAULT_PRIOR.exit_strength))
    return Prior(rates, strengths, len(usable), at, floor, exit_rate, exit_strength)


def _pooled(counts, exposures):
    """(rate, strength): the counts pooled over their exposures, and the
    STRENGTHS value under which they are likeliest; rate 0 takes the largest."""
    rate = sum(counts) / sum(exposures)
    return rate, STRENGTHS[-1] if rate <= 0 else max(
        STRENGTHS, key=lambda strength: _nb_loglik(counts, exposures, rate, strength))


def _nb_loglik(counts, days, rate, strength):
    a = strength * rate
    return sum(math.lgamma(k + a) - math.lgamma(a) + a * math.log(strength / (strength + t))
               + k * math.log(t / (strength + t)) for k, t in zip(counts, days))


def fill_curve(history: Optional[History], prior: Prior, floor):
    """The curve a reader whose favorite floor is floor values a bid with.

    With a history, each grid rate is the market's events shrunk toward the
    prior, (k + strength * rate) / (days + strength); without one, the
    prior's rates. A history read with another floor counted other sweeps
    and is dropped; a prior fit more than PRIOR_FLOOR_SLACK from floor gives
    way to DEFAULT_PRIOR (_usable).
    A history of no days, as a read that found no mids leaves, saw nothing
    and is dropped too, so it never passes for a market that was read. Each
    rate is then raised to the largest at any farther distance, so a
    nearer bid never fills less often than a farther one, and to FLOOR_RATE.
    The exit rate shrinks the same way, exits over held days.
    """
    history, prior = _usable(history, prior, floor)
    if history is None:
        rates = list(prior.rates)
        exit_per_day = prior.exit_rate
    else:
        rates = [(k + strength * rate) / (history.days + strength)
                 for k, strength, rate in zip(history.events, prior.strengths, prior.rates)]
        exit_per_day = ((history.exits + prior.exit_strength * prior.exit_rate)
                        / (history.held_days + prior.exit_strength))
    for i in range(len(rates) - 2, -1, -1):
        rates[i] = max(rates[i], rates[i + 1])
    return FillCurve(tuple(max(rate, FLOOR_RATE) for rate in rates), history is not None,
                     exit_per_day)


def fill_source(history: Optional[History], prior: Prior, floor):
    """Where fill_curve(history, prior, floor) takes its rates from: "history",
    "prior" (a scan's fit) or "default" (DEFAULT_PRIOR, before any scan)."""
    history, prior = _usable(history, prior, floor)
    if history is not None:
        return "history"
    return "default" if prior.markets == 0 else "prior"


# A prior fit this near the reader's floor stands in for one at it: the scan
# fits at its own min_fav, a few cents from the farmer's.
PRIOR_FLOOR_SLACK = 0.05


def _usable(history, prior, floor):
    """The history, None when it was read at another floor or saw no days,
    and the prior, DEFAULT_PRIOR when it was fit more than PRIOR_FLOOR_SLACK
    from floor."""
    return (history if history is not None and history.floor == floor and history.days > 0
            else None,
            prior if abs(prior.floor - floor) <= PRIOR_FLOOR_SLACK + 1e-9 else DEFAULT_PRIOR)


def history_to_json(history):
    """A History as a JSON-safe dict carrying its grid, so a row stored before
    a grid change reads back as no history."""
    return {
        "v": JSON_VERSION,
        "distances": list(DISTANCES),
        "events": list(history.events),
        "days": history.days,
        "read_at": history.read_at.isoformat(),
        "floor": history.floor,
        "exits": history.exits,
        "held_days": history.held_days,
    }


def history_from_json(data):
    """The History a history_to_json dict holds, or None for anything else: no
    data, an old tape, another grid or version, a malformed value. Never raises."""
    try:
        if data.get("v") != JSON_VERSION or tuple(data["distances"]) != DISTANCES:
            return None
        history = History(tuple(data["events"]), float(data["days"]),
                          datetime.fromisoformat(data["read_at"]), float(data["floor"]),
                          data["exits"], float(data["held_days"]))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    events = history.events
    valid = (len(events) == len(DISTANCES)
             and all(type(k) is int and k >= 0 for k in events)
             and all(a >= b for a, b in zip(events, events[1:]))
             and 0 <= history.days <= HISTORY_DAYS and 0 < history.floor < 1
             and type(history.exits) is int
             and 0 <= history.exits <= events[DISTANCES.index(EXIT_DISTANCE)]
             and math.isfinite(history.held_days) and history.held_days >= 0)
    return history if valid else None


def prior_to_json(prior):
    """A Prior as a JSON-safe dict carrying its grid."""
    return {
        "v": JSON_VERSION,
        "distances": list(DISTANCES),
        "rates": list(prior.rates),
        "strengths": list(prior.strengths),
        "markets": prior.markets,
        "at": prior.at.isoformat() if prior.at else None,
        "floor": prior.floor,
        "exit_rate": prior.exit_rate,
        "exit_strength": prior.exit_strength,
    }


def prior_from_json(data):
    """The Prior a prior_to_json dict holds, or None for anything else. Never raises."""
    try:
        if data.get("v") != JSON_VERSION or tuple(data["distances"]) != DISTANCES:
            return None
        at = data["at"]
        prior = Prior(tuple(float(r) for r in data["rates"]),
                      tuple(float(s) for s in data["strengths"]), data["markets"],
                      datetime.fromisoformat(at) if at is not None else None,
                      float(data["floor"]), float(data["exit_rate"]),
                      float(data["exit_strength"]))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    valid = (len(prior.rates) == len(prior.strengths) == len(DISTANCES)
             and all(math.isfinite(r) and r >= 0 for r in prior.rates)
             and all(math.isfinite(s) and s > 0 for s in prior.strengths)
             and type(prior.markets) is int and prior.markets >= 0
             and 0 < prior.floor < 1
             and math.isfinite(prior.exit_rate) and prior.exit_rate >= 0
             and math.isfinite(prior.exit_strength) and prior.exit_strength > 0)
    return prior if valid else None
