"""The account's open orders, as the CLOB lists them."""

from datetime import datetime, timezone
from enum import Enum
from typing import NamedTuple, Optional

from polymarket.parse import as_float


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OpenOrder(NamedTuple):
    """One resting order. market is the condition id, size the size it was placed
    with, and expiration None for an order that is good until cancelled."""

    order_id: str
    token_id: str
    market: str
    outcome: str
    side: Side
    price: float
    size: float
    size_matched: float
    expiration: Optional[datetime]


def open_orders(client):
    """Every open order of the client's account, in the CLOB's order.

    Raises what the client raises when the listing fails, and ValueError when
    the answer is not a list of orders this module can read. A caller never
    sees a short or empty list in place of an error.
    """
    listed = client.get_open_orders()
    if not isinstance(listed, list):
        raise ValueError(f"the CLOB listed open orders as {type(listed).__name__}, not a list")
    return [parse_order(entry) for entry in listed]


def parse_order(entry):
    """The OpenOrder of one entry of the CLOB's listing; ValueError when a field
    is missing or does not parse."""
    order_id, sent_side = _text(entry, "id"), _text(entry, "side")
    try:
        side = Side(sent_side.upper())
    except ValueError:
        raise ValueError(f"order {order_id}: side is {sent_side!r}") from None
    return OpenOrder(
        order_id=order_id, token_id=_text(entry, "asset_id"), market=_text(entry, "market"),
        outcome=str(entry.get("outcome") or ""), side=side, price=_number(entry, "price"),
        size=_number(entry, "original_size"), size_matched=_number(entry, "size_matched"),
        expiration=_expiration(entry))


def _text(entry, key):
    value = entry.get(key)
    if not value:
        raise ValueError(f"order {entry.get('id') or '?'}: no {key}")
    return str(value)


def _number(entry, key):
    value = as_float(entry.get(key))
    if value is None:
        raise ValueError(f"order {entry.get('id') or '?'}: {key} is {entry.get(key)!r}")
    return value


def _expiration(entry):
    """The CLOB sends unix seconds, and 0 for an order with no expiration."""
    sent = entry.get("expiration")
    if not sent:
        return None
    seconds = as_float(sent)
    if seconds is None:
        raise ValueError(f"order {entry.get('id') or '?'}: expiration is {sent!r}")
    return datetime.fromtimestamp(seconds, timezone.utc) if seconds else None
