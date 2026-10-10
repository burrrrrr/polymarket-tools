"""The CLOB's market websocket channel: a pure parser from one frame to typed
Updates, and MarketFeed, the loop that holds the connection."""

import json
import time
from enum import Enum
from typing import NamedTuple, Optional

from polymarket.hosts import WSS_MARKET_URL
from polymarket.parse import as_float

PING_S = 10.0
RECV_S = 1.0
RECONNECT_S = 2.0


class Kind(Enum):
    TICK_SIZE_CHANGE = "tick_size_change"
    BOOK = "book"
    PRICE_CHANGE = "price_change"


class Update(NamedTuple):
    """What one message says about one token: its tick, or its best prices,
    never both. A field the message does not carry is None."""

    token_id: str
    tick: Optional[float]
    old_tick: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    kind: Kind


def parse_frame(raw):
    """The Updates in one websocket frame, in the order to apply them.

    A `book` message with a tick and prices gives the tick first. A consumer
    that keeps the last prices seen before a tick change must not have them
    overwritten by the message that announces the change.

    A frame that holds no market message, or a malformed one, gives an empty
    list. Never raises.
    """
    # RecursionError is a frame nested past the recursion limit. The SDK's
    # py_ecc raises that limit to 100000 for the whole process, and there a
    # frame nested about 14,000 deep overflows the C stack before the limit is
    # reached. The channel's own frames nest 4 deep.
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return []
    messages = decoded if isinstance(decoded, list) else [decoded]
    return [update for m in messages if isinstance(m, dict) for update in _updates(m)]


def _updates(message):
    event, payload = _unwrap(message)
    asset = _field(payload, "asset_id", "tokenId")

    if event == Kind.TICK_SIZE_CHANGE.value:
        tick = as_float(_field(payload, "new_tick_size", "newTickSize"))
        if asset and tick is not None:
            old_tick = as_float(_field(payload, "old_tick_size", "oldTickSize"))
            yield Update(str(asset), tick, old_tick, None, None, Kind.TICK_SIZE_CHANGE)

    elif event == Kind.BOOK.value and asset:
        tick = as_float(_field(payload, "tick_size", "tickSize"))
        if tick is not None:
            yield Update(str(asset), tick, None, None, None, Kind.BOOK)
        bid, ask = _best(payload.get("bids"), max), _best(payload.get("asks"), min)
        if bid is not None or ask is not None:
            yield Update(str(asset), None, None, bid, ask, Kind.BOOK)

    elif event == Kind.PRICE_CHANGE.value:
        changes = _field(payload, "price_changes", "priceChanges")
        for change in changes if isinstance(changes, list) else []:
            if not isinstance(change, dict):
                continue
            # A market's price_change lists both of its tokens, so each entry
            # is routed by its own asset id and not by the message's.
            change_asset = _field(change, "asset_id", "tokenId") or asset
            bid = as_float(_field(change, "best_bid", "bestBid"))
            ask = as_float(_field(change, "best_ask", "bestAsk"))
            if change_asset and (bid is not None or ask is not None):
                yield Update(str(change_asset), None, None, bid, ask, Kind.PRICE_CHANGE)


def _unwrap(message):
    """(event type, payload) of one message.

    The server sends flat snake_case events. The docs describe an envelope of
    {topic, type, payload} with camelCase fields. Both are read, so a switch on
    the server does not stop detection.
    """
    if "event_type" in message:
        return message["event_type"], message
    payload = message.get("payload")
    return message.get("type"), payload if isinstance(payload, dict) else message


def _field(payload, *names):
    return next((payload[name] for name in names if payload.get(name) is not None), None)


def _best(levels, pick):
    if not isinstance(levels, list):
        return None
    prices = [as_float(level.get("price")) for level in levels if isinstance(level, dict)]
    prices = [price for price in prices if price is not None]
    return pick(prices) if prices else None


def open_socket(url):
    """A websockets connection, used as a context manager with send and recv."""
    from websockets.sync.client import connect
    return connect(url, open_timeout=10, close_timeout=1)


class MarketFeed:
    """The market channel for a set of tokens, delivered to on_update as Updates.

    run(stop) holds one connection at a time. It subscribes to the token set
    and sends a PING every PING_S, because the server drops an idle socket.
    When the socket fails, or on_update raises, on_error gets the exception and
    run reconnects after RECONNECT_S. set_tokens replaces the set: within
    RECV_S run subscribes to the new tokens and unsubscribes from the dropped
    ones on the open connection, so the others keep their frames. While the
    set is empty run holds no connection.

    on_update and on_error run on the thread that calls run. on_update gets
    only Updates for tokens in the set, although one price_change message lists
    both tokens of a market. connect(url) opens the socket and clock() gives
    monotonic seconds; tests replace both.
    """

    def __init__(self, token_ids, on_update, *, on_error, connect=open_socket,
                 clock=time.monotonic):
        self._tokens = frozenset(token_ids)
        self._on_update = on_update
        self._on_error = on_error
        self._connect = connect
        self._clock = clock

    def set_tokens(self, token_ids):
        self._tokens = frozenset(token_ids)

    def run(self, stop):
        """Deliver updates until stop is set. stop is a threading.Event."""
        while not stop.is_set():
            tokens = self._tokens
            if not tokens:
                stop.wait(RECV_S)
                continue
            try:
                self._listen(stop, tokens)
            except Exception as e:
                if stop.is_set():
                    return
                self._on_error(e)
                stop.wait(RECONNECT_S)

    def _listen(self, stop, tokens):
        """One connection, until stop is set or the token set is emptied."""
        with self._connect(WSS_MARKET_URL) as socket:
            socket.send(json.dumps({"assets_ids": sorted(tokens), "type": "market"}))
            pinged = self._clock()
            while not stop.is_set() and self._tokens:
                wanted = self._tokens
                if wanted != tokens:
                    for ids, operation in ((wanted - tokens, "subscribe"),
                                           (tokens - wanted, "unsubscribe")):
                        if ids:
                            socket.send(json.dumps({"assets_ids": sorted(ids),
                                                    "operation": operation}))
                    tokens = wanted
                if self._clock() - pinged >= PING_S:
                    socket.send("PING")
                    pinged = self._clock()
                try:
                    raw = socket.recv(timeout=RECV_S)
                except TimeoutError:
                    continue
                for update in parse_frame(raw):
                    if update.token_id in tokens:
                        self._on_update(update)
