"""
The taker buys of a market's favorite, which a resting favorite ask sells into.

A taker that buys the favorite, or sells the longshot, sweeps the favorite
asks from the cheapest up. Its own /trades row prints a volume-weighted
average price, so the sweep is kept as the ladder of maker fills it took,
each a Piece at its own favorite-equivalent price. A market's BuyTape holds
its sweeps as read at read_at, complete from first_at: a stretch of tape
with no sweep is a stretch in which nobody bought.

Raw sweeps are kept, not a digest, so a tape can be replayed as of any past
time and valued again under another end date or recency weight.

Everything here is pure and imports nothing else from the project; the
screener parses the sweeps and the farmer keeps the tapes in farm.db.
"""

import math
from datetime import datetime, timezone
from typing import NamedTuple, Optional, Sequence

EXIT_JSON_VERSION = 1
# Sweeps the farmer keeps per market in farm.db, the newest.
TAPE_ROWS_FARMER = 500


class Piece(NamedTuple):
    """Shares that traded at one favorite-equivalent price inside a sweep."""

    price: float
    size: float


class Sweep(NamedTuple):
    """One taker buy of the favorite: its unix time and its pieces, highest
    price first."""

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
