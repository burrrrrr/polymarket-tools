"""
How often a resting favorite bid fills, and what a filled share costs.

A fill is not free: the share ties up capital, which has a hurdle rate r,
until it leaves. The farmer offers a filled share back through FAV_ASK, and
the mid mostly recovers to where the sweep found it within days, so a share
mostly exits after an exponential hold T of rate lambda a year, cut short at
resolution Y years out. A share in stuck_share q never exits: the mid falls
under min_fav and the ask is pulled, or the market collapses, and it is held
to resolution. With delta = ln(1 + r), a share bought at b that exits at the
ask a and is worth p_true at resolution loses L = b - share_value today,
share_value = (1 - q) * a * E[exp(-delta * min(T, Y))]
+ q * p_true * exp(-delta * Y); without an ask, a is p_true and share_value
is p_true times hold_discount. On a short-dated market, or one whose fills
exit fast, L can be negative and fills are welcome.

A bid fills when a sweep reaches past it: taker trades hitting favorite bids
in one burst, whose deepest fill trades below the bid. A market's History
counts, at each distance in DISTANCES, the sweeps that reached that far below
the mid they found, over its last HISTORY_DAYS days of trades, at most one an
hour, and times how long the mid took to recover after those at
EXIT_DISTANCE. Few markets see enough sweeps to be rated alone, so each count
is shrunk toward a Prior that a scan pools across markets, by as many days of
evidence as the markets' spread around the pooled rate is worth, and the exit
rate likewise. A bid of Q shares then fills Q times the FillCurve's rate at
its distance a day: a sweep that reaches it fills all of it.

Everything here is pure; the screener reads trades and mids, and calls
make_history and fit_prior.
"""

import bisect
import math
from datetime import datetime
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
# measured holds were 5.0, 7.2 and 7.6 days at 1, 1.5 and 2c, and at holds of
# days the cost barely depends on their exact length.
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


def hold_discount(exit_per_day, years, hurdle_rate, stuck_share):
    """What p_true is worth today per share filled, at the hurdle rate: the
    stuck share discounted over `years` to resolution, the rest over an
    exponential hold at exit_per_day cut short there,
    E[exp(-delta * min(T, Y))] = lam / k * (1 - exp(-k * Y)) + exp(-k * Y)
    with delta = ln(1 + hurdle_rate), lam the exits a year, k = lam + delta.
    No exits, or stuck_share 1, discounts everything to resolution."""
    delta = math.log1p(hurdle_rate)
    lam = exit_per_day * YEAR_DAYS
    k = lam + delta
    exiting = 1.0 if k == 0 else lam / k * -math.expm1(-k * years) + math.exp(-k * years)
    return (1 - stuck_share) * exiting + stuck_share * math.exp(-delta * years)


def share_value(p_true, exit_price, exit_per_day, years, hurdle_rate, stuck_share):
    """What a filled share is worth today at the hurdle rate: the exiting
    share sells at exit_price after hold_discount's hold, the stuck share is
    worth p_true at resolution. exit_price None sells at p_true, and an ask
    over p_true is credited at p_true: the hold is the mid's recovery, and
    asks sell far slower than that, so a fill never looks worth more than
    p_true."""
    exit_price = p_true if exit_price is None else min(exit_price, p_true)
    exiting = hold_discount(exit_per_day, years, hurdle_rate, 0.0)
    held = math.exp(-math.log1p(hurdle_rate) * years)
    return (1 - stuck_share) * exit_price * exiting + stuck_share * p_true * held


def years_until(end_date, now):
    """Years to resolution, never negative: how long a stuck share is held, and
    where an exiting share's hold is cut short. The end date, not the expiry
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
    non-increasing in distance, and the exits a day of a filled share.
    from_history says the market's own history went into it; False is the
    prior alone."""

    rates: tuple
    from_history: bool
    exit_per_day: float

    def per_day(self, distance):
        """Events a day reaching past a bid `distance` below the mid, linear
        between grid points and flat outside them. A bid of Q shares fills
        Q * per_day(distance) a day: an event fills the whole bid."""
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
    # 2026-10-05, a set biased toward markets that recovered, which
    # stuck_share covers.
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
    prior's rates. A history or prior read with another floor counted other
    sweeps, so the history is dropped and the prior replaced by DEFAULT_PRIOR.
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


def _usable(history, prior, floor):
    return (history if history is not None and history.floor == floor and history.days > 0
            else None,
            prior if prior.floor == floor else DEFAULT_PRIOR)


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
