"""A restore is safe to run again. Its plan is made against the orders open at
that moment, so an order an earlier run placed is skipped as open.
"""

from collections import Counter
from datetime import timedelta
from typing import NamedTuple

from polymarket import orders, ticks
from polymarket.orders import GTC, GTD, MIN_EXPIRES_AFTER_S, Posted, Rejected, Unanswered
from tools import db
from tools.db import RestoreAction

# The minute on top of the CLOB's is for the job to reach the order.
EXPIRY_MARGIN = timedelta(seconds=MIN_EXPIRES_AFTER_S) + timedelta(minutes=1)
MAX_UNANSWERED_IN_A_ROW = 3
MAY_REST = "the order may rest: check your open orders"

SKIPS = (RestoreAction.SKIP_LIVE, RestoreAction.SKIP_EXPIRED, RestoreAction.SKIP_FARMER,
         RestoreAction.SKIP_FILLED)


class PlanLine(NamedTuple):
    """What a restore would do with one snapshot order: WOULD_PLACE or one of
    SKIPS, the size left of the order to place, and why."""

    order: db.SnapshotOrder
    action: RestoreAction
    size: float
    reason: str


class Stopped(Exception):
    """The restore ended before its last order. str() is the job's error."""


class Unsent(NamedTuple):
    """The order was never posted, so nothing rests. detail is why."""

    detail: str


def plan(snapshot_orders, live_orders, managed_markets, now, retired_markets=frozenset()):
    """One PlanLine for each snapshot order, in their order.

    An order is skipped when nothing of it was left to fill, when it is the
    farmer's to place, when it expires within EXPIRY_MARGIN of now, and when
    it is open: the same order still rests, or an open order that is not in
    the snapshot has its token, side, price and remaining size. Each such copy
    covers one snapshot order, so two identical orders with one copy are a
    skip and a place.

    An order is the farmer's when the farmer manages its market now, or managed
    it when the snapshot was taken. A snapshot with no record of that leaves an
    order to the farmer when its market is in retired_markets, the markets the
    farmer managed once and has retired.
    """
    snapshot_ids = {stored.order.order_id for stored in snapshot_orders}
    resting = {live.order_id for live in live_orders}
    copies = Counter(_key(live) for live in live_orders if live.order_id not in snapshot_ids)
    lines = []
    for stored in snapshot_orders:
        order = stored.order
        if order.remaining <= 0:
            action, reason = RestoreAction.SKIP_FILLED, "nothing of it was left to fill"
        elif order.market in managed_markets:
            action, reason = RestoreAction.SKIP_FARMER, "the farmer manages this market"
        elif stored.farmer:
            action, reason = (RestoreAction.SKIP_FARMER,
                              "the farmer managed this market when the snapshot was taken")
        elif stored.farmer is None and order.market in retired_markets:
            action, reason = (RestoreAction.SKIP_FARMER,
                              "the farmer has retired this market, and the snapshot does not "
                              "record whose order this was")
        elif order.expiration is not None and order.expiration <= now:
            action, reason = RestoreAction.SKIP_EXPIRED, "expired"
        elif order.expiration is not None and order.expiration <= now + EXPIRY_MARGIN:
            action, reason = RestoreAction.SKIP_EXPIRED, "expires too soon for the CLOB to take"
        elif order.order_id in resting:
            action, reason = RestoreAction.SKIP_LIVE, "still open"
        elif copies[_key(order)] > 0:
            copies[_key(order)] -= 1
            action, reason = RestoreAction.SKIP_LIVE, "an open order matches it"
        elif order.size_matched > 0:
            action = RestoreAction.WOULD_PLACE
            reason = f"{order.size_matched:g} of {order.size:g} had filled"
        else:
            action, reason = RestoreAction.WOULD_PLACE, "not open"
        lines.append(PlanLine(stored, action, order.remaining, reason))
    return lines


def preview(client, conn, job, now):
    """Store the plan of a preview job as its lines, and return them.

    Raises when the open orders cannot be listed, and then stores nothing.
    """
    lines = [db.RestoreLine(job.id, line.order, line.action, line.size, None, line.reason)
             for line in _planned(client, conn, job, now)]
    db.add_restore_lines(conn, lines)
    return lines


def place(client, conn, job, clock, stop):
    """Run a restore job: plan it against the orders open now, post each order
    that is missing, post-only, and store one line for each snapshot order as
    it goes. Returns the lines.

    Raises when the open orders cannot be listed, having placed nothing.
    Raises Stopped when the job waited longer than db.RESTORE_MAX_WAIT for a
    worker, when `stop` is set between two orders, and after
    MAX_UNANSWERED_IN_A_ROW posts got no answer. The lines stored by then stay.
    """
    waited, limit = clock() - job.created_at, db.RESTORE_MAX_WAIT
    if waited > limit:
        raise Stopped(f"queued {waited.total_seconds() / 60:.0f} minutes before a worker took "
                      f"it, and a restore runs within {limit.total_seconds() / 60:.0f} minutes "
                      f"of its confirmation or not at all: preview it again")
    planned = _planned(client, conn, job, clock())
    lines, tick_of, unanswered = [], {}, 0
    for line in planned:
        if stop.is_set() or unanswered == MAX_UNANSWERED_IN_A_ROW:
            why = ("the worker stopped" if stop.is_set() else
                   f"{unanswered} posts in a row got no answer from the CLOB")
            raise Stopped(f"{why} after {len(lines)} of {len(planned)} orders ({summary(lines)})")
        action, order_id, detail = line.action, None, line.reason
        if action is RestoreAction.WOULD_PLACE:
            answer = _send(client, line, tick_of)
            if isinstance(answer, Posted):
                action, order_id, unanswered = RestoreAction.PLACED, answer.order_id, 0
            elif isinstance(answer, Rejected):
                action, detail, unanswered = RestoreAction.REJECTED, answer.reason, 0
            elif isinstance(answer, Unanswered):
                action, detail = RestoreAction.ERROR, f"no answer ({answer.detail}): {MAY_REST}"
                unanswered += 1
            else:
                action, detail = RestoreAction.ERROR, f"not sent: {answer.detail}"
        lines.append(db.RestoreLine(job.id, line.order, action, line.size, order_id, detail))
        db.add_restore_lines(conn, lines[-1:])
    return lines


def summary(lines, preview=False):
    """A count of what became of these RestoreLines, as a sentence fragment."""
    counts = Counter(line.action for line in lines)
    others = [(counts[RestoreAction.REJECTED], "rejected"),
              (counts[RestoreAction.ERROR], "in error"),
              (sum(counts[skip] for skip in SKIPS), "skipped")]
    placing = (f"{counts[RestoreAction.WOULD_PLACE]} to place" if preview
               else f"{counts[RestoreAction.PLACED]} placed")
    return ", ".join([placing, *(f"{n} {what}" for n, what in others if n)])


def _planned(client, conn, job, now):
    snapshot_id = job.params["snapshot_id"]
    if db.read_snapshot(conn, snapshot_id) is None:
        raise ValueError(f"no snapshot {snapshot_id}")
    return plan(db.read_snapshot_orders(conn, snapshot_id), orders.open_orders(client),
                set(job.params["managed_markets"]), now, set(job.params["retired_markets"]))


def _key(order):
    return order.token_id, order.side, round(order.price, 6), order.remaining


def _send(client, line, tick_of):
    order = line.order.order
    try:
        if order.market not in tick_of:
            tick_of[order.market] = ticks.refresh(client, order.market, order.token_id)
        signed = ticks.sign(client, order.token_id, order.side, order.price, line.size,
                            order.expiration, tick_of[order.market])
    except Exception as e:
        return Unsent(f"{type(e).__name__}: {e}")
    return orders.post(client, signed, GTD if order.expiration else GTC)
