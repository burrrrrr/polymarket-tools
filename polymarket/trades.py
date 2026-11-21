"""The account's fills, read from the CLOB's trade listing.

parse_trades is the only code that reads Fills out of the listing. The docs show
a trade the account took. How a trade one of its resting orders made is laid
out is assumed here, and `python -m analysis.trades_dump` prints real trades
beside the Fills read from them, so the assumption can be checked.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import NamedTuple

from py_clob_client_v2.clob_types import TradeParams

from polymarket.orders import Side
from polymarket.parse import as_float


class UnreadableTrade(ValueError):
    """A trade entry is not laid out as parse_trades reads it."""


class FillState(Enum):
    # The chain may still undo it.
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


STATES = {
    "MATCHED": FillState.PENDING,
    "MINED": FillState.PENDING,
    "RETRYING": FillState.PENDING,
    "CONFIRMED": FillState.CONFIRMED,
    "FAILED": FillState.FAILED,
}
STATUS_PREFIX = "TRADE_STATUS_"
TAKER, MAKER = "TAKER", "MAKER"


class Fill(NamedTuple):
    """One execution of one of the account's own orders, from the account's side."""

    trade_id: str
    # The account's order: its maker order, or the trade's taker order.
    order_id: str
    condition_id: str
    token_id: str
    side: Side
    shares: float
    price: float
    matched_at: datetime
    taker: bool
    state: FillState

    @property
    def key(self):
        """What tells this fill from every other: a trade can fill several
        of the account's orders."""
        return (self.trade_id, self.order_id)


def parse_trades(entries, account):
    """The account's Fills in the CLOB's trade entries.

    A trade the account took (trader_side TAKER) gives one Fill from the top
    level: taker_order_id, asset_id, side, size and price. Each maker_orders
    entry whose maker_address is the account, whatever the letter case,
    gives one more from its own order_id, asset_id, side, matched_amount and
    price. A trade the account made (MAKER) has at least one such entry.
    Raises UnreadableTrade, naming the trade, for an entry laid out any other
    way.
    """
    if not isinstance(entries, list):
        raise UnreadableTrade(f"expected a list of trades, got {type(entries).__name__}")
    me = account.casefold()
    return [fill for entry in entries for fill in _fills(entry, me)]


def fetch_fills(client, account, after):
    """Every Fill of the account the CLOB lists for `after`, unix seconds, a
    bound that may include that second. Raises what the client raises, and
    UnreadableTrade."""
    return parse_trades(client.get_trades(TradeParams(maker_address=account, after=after)),
                        account)


def _fills(entry, me):
    trade_id = entry.get("id") if isinstance(entry, dict) else None
    if not trade_id:
        raise UnreadableTrade("a trade has no id")
    try:
        return _read(entry, str(trade_id), me)
    except KeyError as e:
        raise UnreadableTrade(f"trade {trade_id}: no {e.args[0]}") from None
    except (AttributeError, TypeError, ValueError) as e:
        raise UnreadableTrade(f"trade {trade_id}: {e}") from None


def _read(entry, trade_id, me):
    status = str(entry["status"]).removeprefix(STATUS_PREFIX)
    if status not in STATES:
        raise ValueError(f"unknown status {status!r}")
    trader_side = entry["trader_side"]
    if trader_side not in (TAKER, MAKER):
        raise ValueError(f"unknown trader_side {trader_side!r}")
    shared = dict(
        trade_id=trade_id,
        condition_id=str(entry["market"]),
        matched_at=datetime.fromtimestamp(_number(entry, "match_time"), timezone.utc),
        state=STATES[status],
    )
    fills = [Fill(order_id=str(maker["order_id"]), token_id=str(maker["asset_id"]),
                  side=Side(str(maker["side"]).upper()),
                  shares=_number(maker, "matched_amount"), price=_number(maker, "price"),
                  taker=False, **shared)
             for maker in entry.get("maker_orders") or []
             if str(maker.get("maker_address") or "").casefold() == me]
    if trader_side == TAKER:
        fills.append(Fill(order_id=str(entry["taker_order_id"]), token_id=str(entry["asset_id"]),
                          side=Side(str(entry["side"]).upper()),
                          shares=_number(entry, "size"), price=_number(entry, "price"),
                          taker=True, **shared))
    elif not fills:
        raise ValueError("a MAKER trade with none of the account's maker orders")
    return fills


def _number(entry, key):
    value = as_float(entry[key])
    if value is None:
        raise ValueError(f"{key} {entry[key]!r} is not a number")
    return value
