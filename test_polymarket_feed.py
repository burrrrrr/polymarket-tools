"""Tests for polymarket.feed: the market-channel parser and the MarketFeed
loop, driven by a scripted socket, a clock the socket advances and a stop that
records its waits. No socket opens and nothing sleeps."""

import json
import unittest

from polymarket.feed import (
    PING_S, RECONNECT_S, RECV_S, Kind, MarketFeed, Update, parse_frame)
from polymarket.hosts import WSS_MARKET_URL

YES, NO = "111", "222"
QUIET = None


def frame(**message):
    return json.dumps(message)


def book(asset_id, tick, bids=(), asks=()):
    return frame(event_type="book", asset_id=asset_id, market="0xabc", tick_size=tick,
                 bids=[{"price": p, "size": "10"} for p in bids],
                 asks=[{"price": p, "size": "10"} for p in asks])


def price_change(*entries, **message):
    return frame(event_type="price_change", market="0xabc",
                 price_changes=[dict(zip(("asset_id", "best_bid", "best_ask"), e))
                                for e in entries], **message)


def tick_change(asset_id, old, new):
    return frame(event_type="tick_size_change", asset_id=asset_id, market="0xabc",
                 old_tick_size=old, new_tick_size=new)


class ParseFrameTests(unittest.TestCase):
    def test_a_tick_size_change_gives_the_new_and_the_old_tick(self):
        self.assertEqual(parse_frame(tick_change(YES, "0.01", "0.001")),
                         [Update(YES, 0.001, 0.01, None, None, Kind.TICK_SIZE_CHANGE)])

    def test_a_book_gives_its_tick_first_and_then_its_best_prices(self):
        raw = book(YES, "0.001", bids=["0.97", "0.998", "0.99"], asks=["1.0", "0.999"])
        self.assertEqual(parse_frame(raw), [
            Update(YES, 0.001, None, None, None, Kind.BOOK),
            Update(YES, None, None, 0.998, 0.999, Kind.BOOK),
        ])

    def test_a_book_gives_only_what_it_carries(self):
        self.assertEqual(parse_frame(book(YES, None, bids=["0.5"])),
                         [Update(YES, None, None, 0.5, None, Kind.BOOK)])
        self.assertEqual(parse_frame(book(YES, "0.01")),
                         [Update(YES, 0.01, None, None, None, Kind.BOOK)])

    def test_each_price_change_entry_goes_to_its_own_token(self):
        # One message covers both tokens of a market: 99c on one side is 1c on the other.
        raw = price_change((YES, "0.99", "1"), (NO, "0", "0.01"))
        self.assertEqual(parse_frame(raw), [
            Update(YES, None, None, 0.99, 1.0, Kind.PRICE_CHANGE),
            Update(NO, None, None, 0.0, 0.01, Kind.PRICE_CHANGE),
        ])

    def test_an_entry_with_no_token_takes_the_messages_and_is_dropped_without_one(self):
        self.assertEqual(parse_frame(price_change((None, "0.4", "0.6"), asset_id=YES)),
                         [Update(YES, None, None, 0.4, 0.6, Kind.PRICE_CHANGE)])
        self.assertEqual(parse_frame(price_change((None, "0.4", "0.6"))), [])

    def test_the_documented_envelope_with_camel_case_fields_parses_the_same(self):
        self.assertEqual(
            parse_frame(frame(topic="market", type="tick_size_change",
                              payload={"tokenId": YES, "oldTickSize": "0.01",
                                       "newTickSize": "0.001"})),
            [Update(YES, 0.001, 0.01, None, None, Kind.TICK_SIZE_CHANGE)])
        self.assertEqual(
            parse_frame(frame(type="price_change", payload={"priceChanges": [
                {"tokenId": NO, "bestBid": "0.01", "bestAsk": "0.02"}]})),
            [Update(NO, None, None, 0.01, 0.02, Kind.PRICE_CHANGE)])
        self.assertEqual(
            parse_frame(frame(type="book", payload={"tokenId": YES, "tickSize": "0.01"})),
            [Update(YES, 0.01, None, None, None, Kind.BOOK)])

    def test_a_frame_holding_a_list_gives_every_messages_updates_in_order(self):
        raw = "[" + ",".join([price_change((YES, "0.99", "1")),
                              tick_change(YES, "0.01", "0.001"),
                              '"noise"',
                              book(NO, "0.01", asks=["0.02"])]) + "]"
        self.assertEqual([(u.token_id, u.kind) for u in parse_frame(raw)], [
            (YES, Kind.PRICE_CHANGE), (YES, Kind.TICK_SIZE_CHANGE),
            (NO, Kind.BOOK), (NO, Kind.BOOK)])

    def test_a_good_entry_survives_bad_ones_beside_it(self):
        raw = frame(event_type="price_change", price_changes=[
            "noise", {"asset_id": YES, "best_bid": "abc"},
            {"asset_id": YES, "best_bid": "abc", "best_ask": "0.6"}])
        self.assertEqual(parse_frame(raw), [Update(YES, None, None, None, 0.6, Kind.PRICE_CHANGE)])

    def test_a_frame_that_is_not_a_market_message_yields_nothing_and_never_raises(self):
        frames = [
            "PONG", "", None, 7, b"\xff\xfe", "{", "[1, 2]", '"text"', "null", "{}",
            "[" * 2000,
            frame(event_type="last_trade_price", asset_id=YES, price="0.5"),
            frame(event_type=["book"], asset_id=YES),
            frame(event_type={"a": 1}, asset_id=YES),
            frame(event_type="book"),
            frame(event_type="book", asset_id=YES, bids=5, asks="x", tick_size="fine"),
            frame(event_type="book", asset_id=YES, bids=[1, None, {"size": "3"}, {"price": []}]),
            frame(event_type="tick_size_change", asset_id=YES, new_tick_size="fine"),
            frame(event_type="tick_size_change", new_tick_size="0.001"),
            frame(event_type="price_change", price_changes="x"),
            frame(event_type="price_change", price_changes={"asset_id": YES}),
            frame(event_type="price_change", price_changes=[{"asset_id": YES, "best_bid": {}}]),
            frame(type="book", payload="x"),
            frame(type="book", payload=[YES]),
        ]
        for raw in frames:
            with self.subTest(raw=str(raw)[:60]):
                self.assertEqual(parse_frame(raw), [])


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Stop:
    """threading.Event's is_set and wait. A wait is recorded, not slept. A feed
    that spins without stopping is stopped after CHECKS_MAX checks, so a defect
    fails an assertion instead of hanging the suite."""

    CHECKS_MAX = 5000

    def __init__(self, set_after_waits=None):
        self.flag = False
        self.waits = []
        self.checks = 0
        self.set_after_waits = set_after_waits

    def set(self):
        self.flag = True

    def is_set(self):
        self.checks += 1
        return self.flag or self.checks > self.CHECKS_MAX

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.set_after_waits is not None and len(self.waits) >= self.set_after_waits:
            self.flag = True
        return self.flag


class Socket:
    """One scripted connection. Each recv plays the next step: a string is a
    frame, an exception is raised, QUIET times out after advancing the clock,
    and a callable runs first and its result is then played. The end of the
    script sets stop."""

    def __init__(self, harness, script):
        self.harness = harness
        self.script = list(script)
        self.sent = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def send(self, text):
        self.sent.append((self.harness.clock.now, text))

    def recv(self, timeout):
        if not self.script:
            self.harness.stop.set()
        step = self.script.pop(0) if self.script else QUIET
        if callable(step):
            step = step()
        if isinstance(step, str):
            return step
        if isinstance(step, BaseException):
            raise step
        self.harness.clock.now += timeout
        raise TimeoutError

    def subscription(self):
        return json.loads(self.sent[0][1])

    def texts(self):
        return [text for _, text in self.sent]


class Harness:
    """A MarketFeed over scripted connections, one script each. An exception
    in place of a script makes that connection fail to open."""

    def __init__(self, tokens, *scripts, stop=None, on_update=None):
        self.clock, self.stop = Clock(), stop or Stop()
        self.pending = list(scripts)
        self.sockets, self.urls, self.updates, self.errors = [], [], [], []
        self.unexpected = 0
        self.feed = MarketFeed(tokens, on_update or self.updates.append,
                               on_error=self.errors.append, connect=self.connect, clock=self.clock)

    def connect(self, url):
        self.urls.append(url)
        if not self.pending:
            self.unexpected += 1
            self.stop.set()
            raise AssertionError("the feed opened a connection the test did not script")
        script = self.pending.pop(0)
        if isinstance(script, BaseException):
            raise script
        self.sockets.append(Socket(self, script))
        return self.sockets[-1]

    def run(self, test):
        self.feed.run(self.stop)
        test.assertEqual(self.unexpected, 0, "unscripted connections")
        test.assertLessEqual(self.stop.checks, Stop.CHECKS_MAX, "the feed spun without stopping")
        return self


class MarketFeedTests(unittest.TestCase):
    def test_it_subscribes_to_its_tokens_on_the_market_channel_and_closes_when_stopped(self):
        h = Harness([YES, NO], [tick_change(YES, "0.01", "0.001")]).run(self)
        self.assertEqual(h.urls, [WSS_MARKET_URL])
        subscription = h.sockets[0].subscription()
        self.assertEqual(subscription["type"], "market")
        self.assertEqual(sorted(subscription["assets_ids"]), [YES, NO])
        self.assertEqual(h.updates, [Update(YES, 0.001, 0.01, None, None, Kind.TICK_SIZE_CHANGE)])
        self.assertTrue(h.sockets[0].closed)
        self.assertEqual((h.errors, h.stop.waits), ([], []))

    def test_the_other_token_of_a_market_is_not_delivered_to_a_feed_that_watches_one(self):
        h = Harness([YES], [price_change((YES, "0.99", "1"), (NO, "0", "0.01")),
                            book(NO, "0.01", bids=["0.005"])]).run(self)
        self.assertEqual(h.updates, [Update(YES, None, None, 0.99, 1.0, Kind.PRICE_CHANGE)])

    def test_a_consumer_sees_the_tick_change_before_the_prices_of_the_same_message(self):
        # The sniper's guard reads the last price seen before the tick changed.
        # This consumer stores the price first, which is the order that would
        # lose that price if one update carried both.
        seen = {}

        def on_update(update):
            if update.bid is not None:
                seen["bid"] = update.bid
            if update.tick is not None and update.tick <= 0.001:
                seen.setdefault("bid_at_change", seen.get("bid"))

        Harness([YES], [price_change((YES, "0.99", "1")),
                        book(YES, "0.001", bids=["0.998"], asks=["0.999"])],
                on_update=on_update).run(self)
        self.assertEqual(seen, {"bid": 0.998, "bid_at_change": 0.99})

    def test_it_pings_every_ping_interval_on_a_quiet_connection(self):
        h = Harness([YES], [QUIET] * 25).run(self)
        self.assertEqual(h.sockets[0].sent[1:], [(PING_S, "PING"), (2 * PING_S, "PING")])

    def test_a_dropped_connection_is_reported_and_reopened_after_the_reconnect_wait(self):
        gone = ConnectionError("gone")
        h = Harness([YES],
                    [price_change((YES, "0.5", "0.6")), gone],
                    [price_change((YES, "0.7", "0.8"))]).run(self)
        self.assertEqual(h.errors, [gone])
        self.assertEqual(h.stop.waits, [RECONNECT_S])
        self.assertTrue(h.sockets[0].closed)
        self.assertEqual(h.sockets[1].subscription()["assets_ids"], [YES])
        self.assertEqual([u.bid for u in h.updates], [0.5, 0.7])

    def test_a_connection_that_fails_to_open_is_reported_and_tried_again(self):
        refused = OSError("refused")
        h = Harness([YES], refused, [price_change((YES, "0.5", "0.6"))]).run(self)
        self.assertEqual((h.errors, h.stop.waits), ([refused], [RECONNECT_S]))
        self.assertEqual([u.bid for u in h.updates], [0.5])

    def test_a_consumer_that_raises_is_reported_and_the_feed_carries_on(self):
        broken = ValueError("consumer bug")
        delivered = []

        def on_update(update):
            delivered.append(update.bid)
            if len(delivered) == 1:
                raise broken

        h = Harness([YES], [price_change((YES, "0.5", "0.6"))],
                    [price_change((YES, "0.7", "0.8"))], on_update=on_update).run(self)
        self.assertEqual(h.errors, [broken])
        self.assertEqual(delivered, [0.5, 0.7])

    def test_a_changed_token_set_is_sent_on_the_open_connection_and_the_others_keep_their_frames(
            self):
        # Measured on the live market channel on 2026-10-11: a subscribe
        # operation adds tokens, an unsubscribe removes them, and the frames
        # of the first token keep arriving through both.
        h = Harness([YES])
        h.pending = [[lambda: h.feed.set_tokens([YES, NO]), book(NO, "0.01"), book(YES, "0.01"),
                      lambda: h.feed.set_tokens([NO]), book(YES, "0.001"), book(NO, "0.001")]]
        h.run(self)
        self.assertEqual(len(h.sockets), 1, "the feed reconnected to change its tokens")
        sent = [json.loads(text) for text in h.sockets[0].texts()]
        self.assertEqual(sent[1:], [{"assets_ids": [NO], "operation": "subscribe"},
                                    {"assets_ids": [YES], "operation": "unsubscribe"}])
        self.assertEqual([(u.token_id, u.tick) for u in h.updates],
                         [(NO, 0.01), (YES, 0.01), (NO, 0.001)])
        self.assertEqual((h.errors, h.stop.waits), ([], []))

    def test_an_emptied_token_set_closes_the_connection_and_holds_none(self):
        h = Harness([YES], [lambda: h.feed.set_tokens([]), book(YES, "0.01")],
                    stop=Stop(set_after_waits=2))
        h.run(self)
        self.assertTrue(h.sockets[0].closed)
        self.assertEqual((h.urls, h.updates, h.stop.waits), ([WSS_MARKET_URL], [], [RECV_S] * 2))

    def test_a_reconnection_subscribes_to_the_set_as_it_is_then(self):
        h = Harness([YES], [ConnectionError("gone")], [book(NO, "0.01")])
        h.stop.wait = lambda seconds: h.feed.set_tokens([NO])
        h.run(self)
        self.assertEqual(h.sockets[1].subscription()["assets_ids"], [NO])
        self.assertEqual([u.token_id for u in h.updates], [NO])

    def test_a_feed_with_no_tokens_holds_no_connection_and_waits_on_stop(self):
        h = Harness([], stop=Stop(set_after_waits=3)).run(self)
        self.assertEqual(h.urls, [])
        self.assertEqual(h.stop.waits, [RECV_S] * 3)

    def test_a_feed_given_its_first_tokens_while_idle_connects(self):
        stop = Stop()
        h = Harness([], [book(YES, "0.01")], stop=stop)
        stop.wait = lambda seconds: h.feed.set_tokens([YES])
        h.run(self)
        self.assertEqual(h.sockets[0].subscription()["assets_ids"], [YES])
        self.assertEqual(len(h.updates), 1)

    def test_a_socket_error_while_stopping_is_not_reported_or_waited_on(self):
        h = Harness([YES])

        def stop_then_fail():
            h.stop.set()
            return ConnectionError("closed under us")

        h.pending = [[stop_then_fail]]
        h.run(self)
        self.assertEqual((h.errors, h.stop.waits), ([], []))


if __name__ == "__main__":
    unittest.main()
