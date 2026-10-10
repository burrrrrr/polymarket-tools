"""Signing a limit order at the tick its market has now.

The SDK's client remembers a token's tick for the life of the process, and
Polymarket narrows a market's tick past 0.96. create_order rounds a price to
the remembered tick without an error, so a bid at 98.5c signed against a
remembered 0.01 goes out at 98c. Everything that signs goes through here:
refresh re-reads the tick, and sign refuses a price it would have to round.
"""

from py_clob_client_v2.clob_types import OrderArgsV2, PartialCreateOrderOptions


class OffGrid(ValueError):
    """The price is not a multiple of the tick, so it cannot be signed as it is."""


def text(tick):
    """The SDK's spelling of a tick: 0.001 is "0.001"."""
    return f"{tick:g}"


def on_grid(price, tick):
    steps = price / tick
    return abs(steps - round(steps)) < 1e-6


def refresh(client, condition_id, token_id):
    """The token's tick now, as a float.

    Re-reads the market's CLOB info, which replaces what the client remembered
    of both its tokens: the tick, the neg-risk flag and the fees. A later sign
    of either token then needs no request. Raises what the client raises when
    the market cannot be read.
    """
    client.get_clob_market_info(condition_id)
    return float(client.get_tick_size(token_id))


def sign(client, token_id, side, price, size, expiration, tick):
    """A signed limit order for exactly this price.

    side is an orders.Side, expiration an aware datetime or None for an order
    that is good until cancelled. Raises OffGrid when the price is not a
    multiple of the tick, and what the client raises when it remembers a
    coarser tick than this one or cannot sign.
    """
    if not on_grid(price, tick):
        raise OffGrid(f"price {price:g} is not a multiple of the market's tick {text(tick)}")
    return client.create_order(
        OrderArgsV2(token_id=token_id, price=price, size=size, side=side.value,
                    expiration=int(expiration.timestamp()) if expiration else 0),
        PartialCreateOrderOptions(tick_size=text(tick)))
