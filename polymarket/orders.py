import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import NamedTuple, Optional

from polymarket.parse import as_float
from polymarket.region import GEOBLOCK_MARKER

GTC, GTD = "GTC", "GTD"

# The CLOB refuses an order that expires within a minute.
MIN_EXPIRES_AFTER_S = 60

TERMINAL_REFUSALS = (GEOBLOCK_MARKER, "http 451", "not enough balance", "allowance",
                     "crosses book")


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


# The ticks the CLOB uses, spelled as the SDK spells them.
TICKS = ("0.1", "0.01", "0.001", "0.0001")


def text(tick):
    """The SDK's spelling of a tick: 0.001 is "0.001"."""
    return f"{tick:g}"


def on_grid(price, tick):
    """Whether the price is a multiple of the tick, whatever the floats."""
    steps = price / tick
    return abs(steps - round(steps)) < 1e-6


class LimitOrder(NamedTuple):
    """What to sign and post: expires_after_s is seconds from signing, or 0 for
    an order that rests until it is cancelled."""

    token_id: str
    side: Side
    price: float
    size: float
    expires_after_s: int

    @property
    def order_type(self):
        return GTD if self.expires_after_s else GTC


@dataclass(frozen=True)
class Posted:
    """The CLOB accepted the order, and this is its id."""

    order_id: str


@dataclass(frozen=True)
class Rejected:
    """The CLOB refused the order, so nothing rests. reason is its message."""

    reason: str


@dataclass(frozen=True)
class Unanswered:
    """The post got no usable answer, so the order may rest. Sending it again
    could place a second order: a caller never retries one."""

    detail: str


def post(client, signed, order_type, post_only=True):
    """Post one signed order, once, and say what the CLOB answered. Never raises.

    A refusal is Rejected: an answer that says so, or a 4xx status. A timeout,
    a connection error, a 5xx status or an answer that cannot be read is
    Unanswered, as is anything else the client raises.
    """
    try:
        answer = client.post_order(signed, order_type, post_only=post_only)
    except Exception as e:
        status = getattr(e, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500:
            return Rejected(f"HTTP {status}: {_message(getattr(e, 'error_msg', None))}")
        return Unanswered(f"{type(e).__name__}: {e}")
    return read_response(answer)


def read_response(answer):
    """What the CLOB's answer to a post says became of the order.

    An order counts as placed only when the answer has success true, no
    errorMsg and an order id. Success with no order id is Unanswered, and so is
    an answer that is not an object.
    """
    if not isinstance(answer, dict):
        return Unanswered(f"the CLOB answered {str(answer)[:200]!r}")
    if answer.get("errorMsg") or not answer.get("success"):
        return Rejected(_message(answer))
    if not answer.get("orderID"):
        return Unanswered("the CLOB reported success with no order id")
    return Posted(str(answer["orderID"]))


def terminal(rejected):
    """Whether no retry of this refused order can succeed."""
    reason = rejected.reason.lower()
    return any(refusal.lower() in reason for refusal in TERMINAL_REFUSALS)


def _message(body):
    """The text of a refusal: the CLOB sends errorMsg in an answer and error in
    the body of a 4xx."""
    if isinstance(body, dict):
        return str(body.get("errorMsg") or body.get("error") or body)
    return str(body)


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

    @property
    def remaining(self):
        """What is left of the order to fill, cut to the whole hundredths of a
        share an order can be placed for."""
        return max(0.0, math.floor(round((self.size - self.size_matched) * 100, 4)) / 100)


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
