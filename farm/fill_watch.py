"""
What a market's own favorite fills say about its next favorite bid.

The farmer learns of a fill two ways. The open-order listing, read every
cycle, shows at once that a favorite order of ours left the book without our
cancel, or shrank in it. Positions, read every minute, show the shares the
fill traded, often a cycle or more later. Only shares positions show count
as a fill or a sale: an order that left the book sets nothing until then,
and one that no positions read shows within FILL_CONFIRM_S is reported and
changes nothing.

One FillWatch per managed market (reward_farmer.Managed.fill_watch) holds
the state, replaced, never edited: by watch_listing after each listing and
by watch_positions after each positions read. From it:

  fill_pending  a favorite bid left the book and positions have not shown
                all its shares yet: no favorite bid rests meanwhile
                (reward_farmer.Phase.FILLED), so none is placed into the
                sweep that took it.
  inventory_ceiling
                while the farm holds favorite shares, no favorite bid over a
                tick under the last fill and their average cost; for
                EXIT_HOLD_S after a favorite ask sold, none over a tick
                under its price. This is the farmer's FarmMarket.bid_ceiling.

A market that joins the managed set, at start or approved later, rebuilds
its watch from the FILL rows farm.db holds (seeded_fill_watch); the watch is
never stored.

Pure, and it imports nothing from reward_farmer: orders are the farmer's
LiveOrders, read by token, side, price and size.
"""

from datetime import datetime
from typing import NamedTuple, Optional

from farm.reward_screener import floor_to_tick

BUY, SELL = "BUY", "SELL"

# The CLOB lists some orders one size step (0.01 share) under the size placed,
# so a cover check exact to the share would replace them every cycle.
SIZE_STEP = 0.01

# A favorite order that left the book, and that positions have not shown
# traded after this long, is reported and forgotten: two positions reads.
FILL_CONFIRM_S = 120
# How long after a favorite ask sold the favorite bid stays a tick under its price.
EXIT_HOLD_S = 6 * 3600


class Gone(NamedTuple):
    """Shares of a favorite order of ours someone else traded with: side is
    the order's, BUY for our favorite bid and SELL for our favorite ask."""

    side: str
    order_id: str
    price: float
    shares: float
    at: datetime


class Credit(NamedTuple):
    """Shares of a side that positions showed traded before the listing did:
    a fill between the positions read and the listing of one cycle."""

    side: str
    shares: float
    at: datetime


class Priced(NamedTuple):
    price: float
    at: datetime


class FillWatch(NamedTuple):
    """pending: the Gone no positions read has shown yet; credits: shares
    positions showed first; and the last favorite fill and sale positions
    showed."""

    pending: tuple = ()
    credits: tuple = ()
    last_fill: Optional[Priced] = None
    last_exit: Optional[Priced] = None


NO_FILLS = FillWatch()


class Ceiling(NamedTuple):
    """The highest favorite bid a market's own fills allow, and why."""

    price: float
    why: str


class Filled(NamedTuple):
    """How a fill a positions read showed is priced: the price, or None, what
    it was taken from, that order's id, and when the fill happened as far as
    the farmer knows."""

    price: Optional[float]
    source: str
    order_id: Optional[str]
    at: datetime


class Seen(NamedTuple):
    """watch_positions' result: the watch, the Filled of the favorite
    shares bought and sold (None for no change), and the pending Gone it
    dropped as stale."""

    watch: FillWatch
    bid: Optional[Filled]
    ask: Optional[Filled]
    stale: tuple


class Fill(NamedTuple):
    """A stored FILL row as seeded_fill_watch replays it: at is when the
    fill happened as far as the farmer knew, seen_at when positions showed
    it."""

    side: str
    price: float
    at: datetime
    seen_at: datetime


LEFT_THE_BOOK = "order that left the book"
RESTING = "resting order at the last cycle"
UNKNOWN = "unknown: no order rested in the slot"


def orders_gone(expected, current, token_id, now, posted=frozenset()):
    """[Gone] of the favorite orders of ours (token_id) that someone else
    traded with: those expected to rest after the last cycle (its listing
    less what our cancels removed, plus our accepted places) against this
    cycle's listing. One missing from current is gone with all it had left.
    One a listing showed, whose size_remaining fell by more than SIZE_STEP,
    is gone by the difference; one in posted, placed last cycle and never
    listed, only when missing, since the size the CLOB lists can differ
    from the size posted. A cancel that failed because the order had filled
    removed nothing, and a bid placed last cycle was expected, so a fill of
    either lands here."""
    listed = {o.id: o for o in current}
    gone = []
    for o in expected:
        if o.token_id != token_id:
            continue
        after = listed.get(o.id)
        if after is None:
            shares = o.size_remaining
        elif o.id in posted:
            continue
        else:
            shares = o.size_remaining - after.size_remaining
        if shares > SIZE_STEP + 1e-9:
            gone.append(Gone(o.side, o.id, o.price, round(shares, 2), now))
    return gone


def _traded(watch, side, price, at):
    """The watch after a favorite fill (BUY) or sale (SELL) at price that
    happened at `at`: the last of its side."""
    if side == SELL:
        return watch._replace(last_exit=Priced(price, at))
    return watch._replace(last_fill=Priced(price, at))


def _fresh(credits, now):
    return [c for c in credits if (now - c.at).total_seconds() < FILL_CONFIRM_S]


def watch_listing(watch, gone, now):
    """The watch after a listing showed `gone`. Shares positions already
    showed absorb a Gone of their side; the rest pends until a positions read
    shows it. Nothing else changes: only shares
    positions show are a fill."""
    credits, pending = _fresh(watch.credits, now), list(watch.pending)
    for g in gone:
        left = g.shares
        for i, c in enumerate(credits):
            if c.side == g.side and left > 0:
                used = min(left, c.shares)
                left -= used
                credits[i] = c._replace(shares=c.shares - used)
        credits = [c for c in credits if c.shares > SIZE_STEP]
        if left > SIZE_STEP:
            pending.append(g._replace(shares=round(left, 2)))
    return watch._replace(pending=tuple(pending), credits=tuple(credits))


def _seen(watch, side, shares, resting, now):
    """(watch, Filled) of `shares` of a side positions showed traded. They
    clear that side's pending Gone oldest first, one cleared in part pending
    with the rest; the Filled is priced at the shares cleared, from the
    first one's time, and sets the last fill or sale as stored."""
    credits = _fresh(watch.credits, now)
    left, cleared, pending = shares, [], []
    for g in watch.pending:
        if g.side != side or left <= SIZE_STEP:
            pending.append(g)
            continue
        used = min(left, g.shares)
        left -= used
        cleared.append((g, used))
        if g.shares - used > SIZE_STEP:
            pending.append(g._replace(shares=round(g.shares - used, 2)))
    if cleared:
        first = cleared[0][0]
        # Rounded so that shares of one order come back at its price exactly.
        price = round(sum(g.price * used for g, used in cleared)
                      / sum(used for _, used in cleared), 6)
        filled = Filled(price, LEFT_THE_BOOK, cleared[-1][0].order_id, first.at)
        if left > SIZE_STEP:
            credits.append(Credit(side, round(left, 2), now))
    else:
        credits.append(Credit(side, shares, now))
        filled = rested(resting, now)
    watch = watch._replace(pending=tuple(pending), credits=tuple(credits))
    if filled.price is not None:
        watch = _traded(watch, side, filled.price, filled.at)
    return watch, filled


def rested(order, now):
    """The Filled of a fill priced at the order resting in its slot at the
    last cycle, or of one with no price when none rested."""
    if order is None:
        return Filled(None, UNKNOWN, None, now)
    return Filled(order.price, RESTING, order.id, now)


def watch_positions(watch, bought, sold, resting_bid, resting_ask, now):
    """The Seen of a positions read that showed `bought` more favorite shares
    and `sold` fewer (reward_farmer.fill_events), with resting_bid and
    resting_ask the favorite orders of the listing before it, or None.

    Shares bought clear the pending bids oldest first, as many shares as
    they show, and are priced at the orders that left the book; with none
    pending, at the bid that rested. Shares sold do the same with the asks.
    A Gone, or what is left of one, still pending FILL_CONFIRM_S after it
    left the book is dropped as stale: an order that left without filling,
    as an expiry or a cancel by hand does, and that changed nothing."""
    bid = ask = None
    if bought > 0:
        watch, bid = _seen(watch, BUY, bought, resting_bid, now)
    if sold > 0:
        watch, ask = _seen(watch, SELL, sold, resting_ask, now)
    stale = tuple(g for g in watch.pending if (now - g.at).total_seconds() >= FILL_CONFIRM_S)
    watch = watch._replace(pending=tuple(g for g in watch.pending if g not in stale))
    return Seen(watch, bid, ask, stale)


def fill_pending(watch):
    """Whether a favorite bid of ours left the book and positions have not shown it."""
    return any(g.side == BUY for g in watch.pending)


def _cents(price):
    return f"{round(price * 100, 2):g}c"


def lowest(*ceilings):
    """The lowest of the ceilings given, or None for none."""
    return min((c for c in ceilings if c is not None), key=lambda c: c.price, default=None)


def inventory_ceiling(watch, held, cost, tick, now):
    """The farm's inventory ceiling: while it holds `held` favorite shares,
    a tick under the last fill and `cost`, their average; without a cost,
    as when shares held before the farm blend the wallet's average, under
    the last fill alone. Within EXIT_HOLD_S of a favorite sale, a tick under
    its price. The lower of the two, or None."""
    holding = None
    if held > 0:
        anchors = [p for p in (watch.last_fill and watch.last_fill.price, cost) if p is not None]
        if anchors:
            why = f"holding {held:g}" + (f" @ {_cents(cost)}" if cost is not None else "")
            if watch.last_fill is not None:
                why += f", last fill {_cents(watch.last_fill.price)}"
            holding = Ceiling(floor_to_tick(min(anchors) - tick, tick), why)
    sold = None
    exit_ = watch.last_exit
    if exit_ is not None and (now - exit_.at).total_seconds() < EXIT_HOLD_S:
        sold = Ceiling(floor_to_tick(exit_.price - tick, tick),
                       f"sold at {_cents(exit_.price)} within {EXIT_HOLD_S // 3600}h")
    return lowest(holding, sold)


def seeded_fill_watch(fills):
    """The watch at start: the stored Fills (any order) replayed in the
    order positions showed them, each as of when it happened. Nothing
    pends: no listing has been read yet."""
    watch = NO_FILLS
    for f in sorted(fills, key=lambda f: f.seen_at):
        watch = _traded(watch, f.side, f.price, f.at)
    return watch
