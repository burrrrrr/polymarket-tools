"""
How often a resting favorite bid fills, and what a filled share costs.

A fill is not free: the share ties up capital until the market resolves, and
capital has a hurdle rate r. Buying at b a share worth p_true at resolution,
t years out, loses L = b - p_true / (1 + r) ** t today. On a short-dated
market L is negative and fills are welcome.

Fill frequency comes from the taker tape: the shares that hit favorite bids,
grouped into sweeps because the book does not refill within a minute. A sweep
of X shares reaches a bid of size Q behind A shares only by what is left after
a haircut on the queue ahead, min(Q, max(0, X - alpha * A)): some of the depth
ahead is cancelled or sits on other books before a sweep arrives. One
synthetic sweep per lookback window clears the whole book, so a quiet tape
never reads as "never fills".

Everything here is pure; the screener fetches the tape and calls make_tape.
"""

from datetime import datetime
from typing import Iterable, NamedTuple, Optional

# Hits chain into one sweep while each lands within SWEEP_GAP_S of the last
# and the sweep spans at most MAX_SWEEP_SPAN_S, so a slow drip of small sells
# cannot add up to one book-clearing sweep.
SWEEP_GAP_S = 60.0
MAX_SWEEP_SPAN_S = 600.0
# A capped tape of a busy market can cover only minutes, and one burst over a
# few minutes would extrapolate to an absurd daily rate.
MIN_SPAN_DAYS = 1 / 24
STORED_SWEEPS = 500
DAY_S = 86400.0
YEAR_DAYS = 365.25


class FillParams(NamedTuple):
    hurdle_rate: float
    depth_haircut: float
    lookback_days: float
    prior_weight: float


class Hit(NamedTuple):
    """Shares of one taker trade that hit favorite bids, at a unix time."""

    at: float
    shares: float


class Sweep(NamedTuple):
    """Shares that hit favorite bids in one burst, from its first hit's unix time."""

    at: float
    shares: float


class Tape(NamedTuple):
    """Sweeps inside the lookback, oldest first, and the days they cover.

    span_days is always positive. observed is False only for the no-data
    tape; capped says the API's row cap, not the lookback, set the span.
    """

    sweeps: tuple
    span_days: float
    observed: bool = True
    capped: bool = False
    fetched_at: Optional[datetime] = None

    @classmethod
    def unobserved(cls, lookback_days):
        return cls((), lookback_days, observed=False)


def make_tape(hits: Iterable[Hit], oldest_row_at, now, lookback_days, full_page_seen):
    """Group hits into sweeps and pick the span they are divided by.

    Hits before now - lookback are dropped. oldest_row_at is the oldest
    fetched row of any kind, hit or not. When a full page came back and that
    row is still inside the lookback, the API's row cap (or a market younger
    than the lookback) ended the history, and the span is now - oldest_row_at;
    otherwise the history reaches the lookback and the span is lookback_days.
    """
    cutoff = now.timestamp() - lookback_days * DAY_S
    sweeps, last = [], None
    for hit in sorted(h for h in hits if h.at >= cutoff):
        if (sweeps and hit.at - last <= SWEEP_GAP_S
                and hit.at - sweeps[-1].at <= MAX_SWEEP_SPAN_S):
            sweeps[-1] = sweeps[-1]._replace(shares=sweeps[-1].shares + hit.shares)
        else:
            sweeps.append(Sweep(hit.at, hit.shares))
        last = hit.at
    capped = full_page_seen and oldest_row_at is not None and oldest_row_at > cutoff
    span = (max((now.timestamp() - oldest_row_at) / DAY_S, MIN_SPAN_DAYS) if capped
            else lookback_days)
    return Tape(tuple(sweeps), span, observed=True, capped=capped, fetched_at=now)


def depth_ahead(bids, price):
    """Bid shares a new favorite bid at price waits behind: every level above
    it and the one it joins at the back."""
    return sum(s for p, s in bids if p >= price - 1e-9)


def fills_per_day(tape, size, ahead, params):
    """Expected shares a day filled on a bid of size behind ahead shares."""
    if size <= 0:
        return 0.0
    reach = params.depth_haircut * ahead
    observed = sum(min(size, max(0.0, s.shares - reach)) for s in tape.sweeps)
    return observed / tape.span_days + params.prior_weight * size / params.lookback_days


def loss_per_share(price, p_true, years, hurdle_rate):
    """What a share bought at price costs today, against p_true discounted to now."""
    return price - p_true / (1 + hurdle_rate) ** years


def years_until(end_date, now):
    """Years to resolution, never negative. The end date, not the expiry buffer:
    a filled share is held until the market resolves."""
    return max(0.0, (end_date - now).total_seconds() / DAY_S / YEAR_DAYS)


def tape_summary(tape):
    """What a page shows of a tape: how much it saw, over what span, and when."""
    return {
        "sweeps": len(tape.sweeps),
        "shares": round(sum(s.shares for s in tape.sweeps), 2),
        "largest": round(max((s.shares for s in tape.sweeps), default=0.0), 2),
        "span_days": tape.span_days,
        "observed": tape.observed,
        "capped": tape.capped,
        "fetched_at": tape.fetched_at.isoformat() if tape.fetched_at else None,
    }


def tape_to_json(tape):
    """A tape as a JSON-safe dict, keeping the newest STORED_SWEEPS sweeps.

    Dropping older sweeps shortens the span to the oldest one kept, so the
    stored tape estimates the same rate over the time it still covers.
    """
    sweeps, span, capped = tape.sweeps, tape.span_days, tape.capped
    if len(sweeps) > STORED_SWEEPS and tape.fetched_at is not None:
        sweeps = sweeps[-STORED_SWEEPS:]
        span = max((tape.fetched_at.timestamp() - sweeps[0].at) / DAY_S, MIN_SPAN_DAYS)
        capped = True
    return {
        "sweeps": [[s.at, s.shares] for s in sweeps],
        "span_days": span,
        "observed": tape.observed,
        "capped": capped,
        "fetched_at": tape.fetched_at.isoformat() if tape.fetched_at else None,
    }


def tape_from_json(data):
    """The Tape a tape_to_json dict holds, or None for no stored tape."""
    if not data:
        return None
    fetched_at = data.get("fetched_at")
    return Tape(
        sweeps=tuple(Sweep(float(at), float(shares)) for at, shares in data["sweeps"]),
        span_days=float(data["span_days"]),
        observed=bool(data.get("observed", True)),
        capped=bool(data.get("capped", False)),
        fetched_at=datetime.fromisoformat(fetched_at) if fetched_at else None,
    )
