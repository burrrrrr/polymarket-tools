"""Signing a limit order at the tick its market has now, or at the tick the
sniper waits for.

The SDK's client remembers a token's tick for the life of the process, and
Polymarket narrows a market's tick past 0.96. create_order rounds a price to
the remembered tick without an error, so a bid at 98.5c signed against a
remembered 0.01 goes out at 98c. Everything that signs goes through here:
refresh re-reads the tick, sign refuses a price it would have to round, and
Signer writes the tick it wants into the client's cache, the one place that
touches the SDK's name-mangled attributes.
"""

from py_clob_client_v2.clob_types import OrderArgsV2, PartialCreateOrderOptions

from polymarket.orders import on_grid, text


class OffGrid(ValueError):
    """The price is not a multiple of the tick, so it cannot be signed as it is."""


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


class Signer:
    """Signs limit orders at a tick the client has not seen the market reach.

    The client is the sniper's own and signs for nothing else, because sign
    leaves the target tick in its cache. clock() gives the aware UTC time an
    expiration counts from.
    """

    def __init__(self, client, clock):
        self._client = client
        self._clock = clock
        self._neg_risk = {}

    def prewarm(self, token_id):
        """Read what signing would otherwise fetch: the token's neg-risk flag
        and, once, the exchange version. Raises when the venue cannot be read."""
        self._neg_risk[token_id] = self._client.get_neg_risk(token_id)
        if self._client._ClobClient__cached_version is None:
            self._client._ClobClient__cached_version = self._client.get_version()

    def sign(self, order, tick):
        """A signed order for exactly order.price at the tick spelled `tick`,
        with no request. Raises KeyError for a token that was not prewarmed,
        and what the SDK raises for a price that is illegal at the tick."""
        # create_order checks the price against the tick the client remembers
        # and refuses an explicit tick finer than it, so the remembered tick
        # is replaced before every signing.
        self._client._ClobClient__tick_sizes[order.token_id] = tick
        expires_after_s = order.expires_after_s
        return self._client.create_order(
            OrderArgsV2(token_id=order.token_id, price=order.price, size=order.size,
                        side=order.side.value,
                        expiration=(int(self._clock().timestamp()) + expires_after_s
                                    if expires_after_s else 0)),
            PartialCreateOrderOptions(tick_size=tick, neg_risk=self._neg_risk[order.token_id]))
