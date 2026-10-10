"""Tests for tools.sniper: the pure rules (the guard, the settlement, the log
lines) and the four lanes as steps a test calls in the order that stages each
race. No thread runs, nothing sleeps, no clock ticks on its own, and the book,
the feed, the signer and the post are fakes, so nothing is sent."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from polymarket.feed import Kind, Update
from polymarket.orders import LimitOrder, Posted, Rejected, Side, Unanswered
from tools import db, sniper
from tools.db import Guard, Reason, Settlement, WatchState
from tools.sniper import (
    PRESIGN_MAX_AGE_S, PRESIGN_S, RETRY_DELAY_S, RETRY_WINDOW_S, Cut, CutBy, GuardRefused,
    Missed, Presigned, Quote, Rehearsed, Source, Unsigned, fresh, judge, newest, settle)

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
MARKET = "0x" + "aa" * 32
T, U = "71" * 38, "72" * 38
NO_FUNDS = "not enough balance / allowance"
TICK_RULE = "order is invalid. Price (0.999) breaks minimum tick size rule: 0.01"


def quote(bid=0.99, ask=0.995, tick="0.01", source=Source.POLL, at=0.0):
    return Quote(bid, ask, tick, source, at)


class Signed:
    """What the fake signer hands back: the order, the tick and a serial."""

    def __init__(self, order, tick, serial):
        self.order, self.tick, self.serial = order, tick, serial


class FakeSigner:
    def __init__(self):
        self.prewarmed = []
        self.signed = []
        self.prewarm_error = None
        self.sign_error = None

    def prewarm(self, token_id):
        if self.prewarm_error:
            raise self.prewarm_error
        self.prewarmed.append(token_id)

    def sign(self, order, tick):
        if self.sign_error:
            raise self.sign_error
        self.signed.append((order, tick))
        return Signed(order, tick, len(self.signed))


class FakePost:
    """orders.post as a fake: answers come from `answers`, a list consumed in
    order or a callable of the signed order; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers) or [Posted("0xnew")]
        self.calls = []
        self.thread_ids = []
        self.on_call = lambda: None

    def __call__(self, client, signed, order_type, post_only=True):
        self.on_call()
        self.calls.append((client, signed, order_type, post_only))
        self.thread_ids.append(threading.get_ident())
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return answer(signed) if callable(answer) else answer


class FakeFeed:
    """The feed as the Sniper builds it: it records every token set."""

    built = []

    def __init__(self, token_ids, on_update, *, on_error):
        self.sets = [sorted(token_ids)]
        self.on_update, self.on_error = on_update, on_error
        FakeFeed.built.append(self)

    def set_tokens(self, token_ids):
        self.sets.append(sorted(token_ids))

    def run(self, stop):
        stop.wait()


class Monotonic:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Harness(unittest.TestCase):
    """A Sniper over a scratch tools.db, with every lane a step to call."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "tools.db")
        self.db = db.open_db(self.path)
        self.addCleanup(self.db.close)
        self.books = {}
        self.http = httpx.Client(transport=httpx.MockTransport(self.serve_books))
        self.addCleanup(self.http.close)
        self.post = FakePost()
        self.signer = FakeSigner()
        self.mono = Monotonic()
        self.clock_now = NOW
        self.stop = threading.Event()
        self.client = object()
        FakeFeed.built = []
        self.snipe = None

    def build(self, client="keyed", **kwargs):
        self.snipe = sniper.Sniper(
            self.path, self.http, self.stop, client=self.client if client == "keyed" else client,
            signer=self.signer, post=self.post, feed=FakeFeed, clock=lambda: self.clock_now,
            monotonic=self.mono, **kwargs)
        self.snipe.open()
        self.addCleanup(self.snipe.close)
        return self.snipe

    def serve_books(self, request):
        def entry(token):
            book = self.books.get(token)
            if isinstance(book, Exception):
                raise book
            if book is None:
                return None
            tick, bid, ask = book
            return {"asset_id": token, "tick_size": tick,
                    "bids": [{"price": str(bid), "size": "10"}] if bid is not None else [],
                    "asks": [{"price": str(ask), "size": "10"}] if ask is not None else []}

        if request.url.path == "/book":
            found = entry(request.url.params["token_id"])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        entries = [entry(t["token_id"]) for t in json.loads(request.content)]
        return httpx.Response(200, json=[e for e in entries if e])

    def arm(self, token=T, dry=False, guard=db.GUARD_DEFAULT, price=0.999, target="0.001",
            expires=0):
        new = db.NewWatch(LimitOrder(token, Side.BUY, price, 100.0, expires), MARKET,
                          f"Will it? -> {token[:2]}", target, guard, dry)
        return db.arm_watch(self.db, new, self.clock_now)

    def tick(self, seconds=1.0):
        """One control tick, due for reconcile and publish."""
        self.mono.now += seconds
        self.snipe.tick()

    def adopt(self, *tokens, book=("0.01", 0.99, 0.995)):
        """Arm and route these tokens: the book is read, the order presigned."""
        ids = []
        for token in tokens:
            self.books[token] = book
            ids.append(self.arm(token))
        self.tick()
        for _ in tokens:
            self.snipe.work_once()
        self.tick()
        return ids[0] if len(ids) == 1 else ids

    def cross(self, token=T, source="feed", tick=0.001):
        if source == "feed":
            self.snipe.on_update(Update(token, tick, 0.01, None, None, Kind.TICK_SIZE_CHANGE))
        else:
            self.books[token] = (str(tick), 0.998, 0.999)
            self.snipe.poll_once()

    def row(self, watch_id=1):
        return db.read_watch(self.db, watch_id)

    def states(self):
        return [w.state for w in db.watches(self.db)]

    def lines(self, watch_id=1):
        return [line.message for line in db.read_watch_log(self.db, watch_id)]

    def assert_closed(self, watch_id, state, reason=None, detail=None):
        row = self.row(watch_id)
        self.assertEqual((row.state, row.reason), (state, reason), self.lines(watch_id))
        if detail is not None:
            self.assertEqual(row.detail, detail)
        self.assertFalse(row.open)


class GuardTests(unittest.TestCase):
    GUARD = Guard(0.99, 0.005, 5.0)

    def test_no_guard_lets_any_order_go(self):
        self.assertIsNone(judge(None, Side.BUY, None, 10.0))

    def test_a_buy_is_judged_by_the_bid_and_a_sell_by_the_ask(self):
        self.assertIsNone(judge(self.GUARD, Side.BUY, quote(bid=0.99, ask=0.5), 1.0))
        self.assertIsNone(judge(self.GUARD, Side.SELL, quote(bid=0.5, ask=0.99), 1.0))
        self.assertEqual(judge(self.GUARD, Side.SELL, quote(bid=0.99, ask=0.5), 1.0).reason,
                         Reason.GUARD_PRICE)

    def test_the_price_must_be_within_tolerance(self):
        self.assertIsNone(judge(self.GUARD, Side.BUY, quote(bid=0.985), 1.0))
        self.assertIsNone(judge(self.GUARD, Side.BUY, quote(bid=0.995), 1.0))
        refused = judge(self.GUARD, Side.BUY, quote(bid=0.98), 1.0)
        self.assertEqual(refused.reason, Reason.GUARD_PRICE)
        self.assertIn("98.0c", refused.detail)
        self.assertIn("99.0c", refused.detail)

    def test_a_missing_quote_or_a_missing_side_refuses(self):
        self.assertEqual(judge(self.GUARD, Side.BUY, None, 1.0).reason, Reason.GUARD_NO_QUOTE)
        self.assertEqual(judge(self.GUARD, Side.BUY, quote(bid=None), 1.0).reason,
                         Reason.GUARD_NO_QUOTE)

    def test_a_stale_quote_refuses(self):
        self.assertIsNone(judge(self.GUARD, Side.BUY, quote(at=10.0), 15.0))
        refused = judge(self.GUARD, Side.BUY, quote(at=10.0), 15.1)
        self.assertEqual(refused.reason, Reason.GUARD_STALE)
        self.assertIn("5.1s", refused.detail)


class RuleTests(unittest.TestCase):
    def test_newest_is_the_latest_quote_among_those_seen(self):
        older, newer = quote(at=1.0, source=Source.BASELINE), quote(at=2.0, source=Source.FEED)
        self.assertIs(newest(older, None, newer), newer)
        self.assertIs(newest(None, newer, older), newer)
        self.assertIsNone(newest(None, None, None))

    def test_a_presigned_order_is_used_only_for_its_tick_and_while_young(self):
        signed = Presigned("order", "0.001", at=10.0)
        self.assertEqual(fresh(signed, "0.001", 10.0 + PRESIGN_MAX_AGE_S), "order")
        self.assertIsNone(fresh(signed, "0.001", 10.0 + PRESIGN_MAX_AGE_S + 0.01))
        self.assertIsNone(fresh(signed, "0.0001", 10.0), "a different tick is a different order")
        self.assertIsNone(fresh(None, "0.001", 10.0))

    def test_settle_maps_every_outcome_and_only_a_posted_to_fired(self):
        cases = [
            (Missed("0.001"), (WatchState.MISSED, Reason.AT_TARGET, "0.001", None)),
            (GuardRefused(Reason.GUARD_PRICE, "bid was 97.0c"),
             (WatchState.REJECTED, Reason.GUARD_PRICE, "bid was 97.0c", None)),
            (Posted("0xnew"), (WatchState.FIRED, None, "", "0xnew")),
            (Rehearsed(signed=True), (WatchState.REHEARSED, None, "signed, not sent", None)),
            (Rehearsed(signed=False),
             (WatchState.REHEARSED, None, "not signed (no key), not sent", None)),
            (Rejected(NO_FUNDS), (WatchState.FAILED, Reason.VENUE, NO_FUNDS, None)),
            (Unanswered("timed out"), (WatchState.FAILED, Reason.UNANSWERED, "timed out", None)),
            (Unsigned("KeyError"), (WatchState.FAILED, Reason.SIGN, "KeyError", None)),
            (Cut(CutBy.DISARM, TICK_RULE), (WatchState.DISARMED, None, TICK_RULE, None)),
            (Cut(CutBy.STOP, TICK_RULE), (WatchState.FAILED, Reason.STOPPED, TICK_RULE, None)),
        ]
        for outcome, expected in cases:
            with self.subTest(outcome=type(outcome).__name__):
                settled = settle(outcome, 2)
                self.assertEqual((settled.state, settled.reason, settled.detail,
                                  settled.order_id), expected)
                self.assertEqual(settled.attempts, 2)
        fired = [o for o, _ in cases if settle(o, 1).state is WatchState.FIRED]
        self.assertEqual(fired, [Posted("0xnew")])
        with self.assertRaises(TypeError):
            settle("something else", 1)


class AdoptionTests(Harness):
    def test_an_armed_row_is_adopted_its_book_read_and_its_order_presigned_then_routed(self):
        self.build()
        self.books[T] = ("0.01", 0.99, 0.995)
        watch_id = self.arm()
        self.tick()
        self.assertEqual(FakeFeed.built[0].sets, [[]], "routed before its book was read")
        self.snipe.work_once()
        self.assertEqual(self.signer.prewarmed, [T])
        self.assertEqual([(o.price, tick) for o, tick in self.signer.signed], [(0.999, "0.001")])
        self.tick()
        self.assertEqual(FakeFeed.built[0].sets[-1], [T])
        row = self.row(watch_id)
        self.assertEqual((row.state, row.tick, row.bid, row.ask), (WatchState.ARMED, "0.01", 0.99,
                                                                   0.995))
        self.assertEqual(self.lines(watch_id),
                         ["adopted at tick 0.01 (bid 99.0c, ask 99.5c), presigned for 0.001"])

    def test_a_token_already_at_the_target_tick_is_missed_and_never_routed(self):
        self.build()
        self.books[T] = ("0.001", 0.998, 0.999)
        watch_id = self.arm()
        self.tick()
        self.snipe.work_once()
        self.tick()
        self.assert_closed(watch_id, WatchState.MISSED, Reason.AT_TARGET, "0.001")
        self.assertEqual(FakeFeed.built[0].sets, [[]])
        self.assertEqual((self.post.calls, self.signer.signed), ([], []))
        self.cross()
        self.assertEqual(self.post.calls, [])

    def test_a_book_that_cannot_be_read_is_tried_again_later(self):
        self.build()
        self.books[T] = httpx.ConnectError("down")
        watch_id = self.arm()
        self.tick()
        self.snipe.work_once()
        self.snipe.work_once()
        self.tick()
        self.assertEqual(self.row(watch_id).state, WatchState.ARMED)
        self.assertEqual(len(self.lines(watch_id)), 1)
        self.assertIn("book read failed", self.lines(watch_id)[0])
        self.books[T] = ("0.01", 0.99, 0.995)
        self.mono.now += sniper.JOIN_RETRY_S
        self.snipe.work_once()
        self.tick()
        self.assertEqual(FakeFeed.built[0].sets[-1], [T])

    def test_a_keyless_sniper_adopts_dry_rows_and_leaves_live_rows_armed(self):
        self.signer = None
        self.build(client=None)
        self.books[T] = self.books[U] = ("0.01", 0.99, 0.995)
        live, dry = self.arm(T), self.arm(U, dry=True)
        self.tick()
        self.snipe.work_once()
        self.snipe.work_once()
        self.tick()
        self.assertEqual(FakeFeed.built[0].sets[-1], [U])
        self.assertEqual(self.row(live).seen_at, None)
        self.assertIsNotNone(self.row(dry).seen_at)
        self.cross(T)
        self.cross(U)
        self.tick()
        self.assertEqual(self.row(live).state, WatchState.ARMED)
        self.assertEqual(self.lines(live), [], "a keyless sniper touched a live row")
        self.assert_closed(dry, WatchState.REHEARSED, None, "not signed (no key), not sent")
        self.assertEqual(self.post.calls, [])

    def test_a_restart_closes_a_firing_row_as_interrupted_and_adopts_the_armed_ones(self):
        firing, armed = self.arm(T), self.arm(U)
        db.mark_firing(self.db, firing, NOW, "feed", "0.001", 0.99, 0.995, NOW)
        self.build()
        self.assert_closed(firing, WatchState.FAILED, Reason.INTERRUPTED)
        self.assertTrue(self.row(firing).reason.may_rest)
        self.assertEqual(self.lines(firing), [db.INTERRUPTED_LINE])
        self.books[U] = ("0.01", 0.99, 0.995)
        self.tick()
        self.snipe.work_once()
        self.tick()
        self.assertEqual(FakeFeed.built[0].sets[-1], [U])
        self.cross(T)
        self.assertEqual(self.post.calls, [], "a watch whose fire was interrupted fired again")

    def test_a_row_closed_by_hand_is_forgotten(self):
        self.build()
        watch_id = self.adopt(T)
        with self.db:
            self.db.execute("UPDATE watches SET state = 'disarmed' WHERE id = ?", (watch_id,))
        self.tick()
        self.assertEqual(FakeFeed.built[0].sets[-1], [])
        self.cross()
        self.assertEqual(self.post.calls, [])


class FireTests(Harness):
    def setUp(self):
        super().setUp()
        self.build()

    def test_the_feed_tick_change_posts_the_presigned_order_once_from_its_own_thread(self):
        watch_id = self.adopt(T)
        self.cross()
        ((client, signed, order_type, post_only),) = self.post.calls
        self.assertIs(client, self.client)
        self.assertEqual((signed.order.price, signed.tick, order_type, post_only),
                         (0.999, "0.001", "GTC", True))
        self.assertEqual(self.post.thread_ids, [threading.get_ident()])
        self.tick()
        row = self.row(watch_id)
        self.assertEqual((row.state, row.order_id, row.attempts, row.source, row.tick, row.bid),
                         (WatchState.FIRED, "0xnew", 1, "feed", "0.001", 0.99))
        self.assertIsNotNone(row.triggered_at)
        self.assertIsNotNone(row.send_ms)
        self.assertEqual(self.lines(watch_id)[1:], [
            "tick 0.001 seen by the feed; guard passed: bid 99.0c from the baseline 1.0 s "
            "before; posting",
            "fired: order 0xnew accepted on attempt 1"])

    def test_the_poll_fires_too_and_a_crossing_both_see_is_one_post(self):
        watch_id = self.adopt(T)
        self.cross(source="poll")
        self.cross(source="feed")
        self.snipe.poll_once()
        self.assertEqual(len(self.post.calls), 1)
        self.tick()
        self.assertEqual((self.row(watch_id).state, self.row(watch_id).source),
                         (WatchState.FIRED, "poll"))

    def test_the_frame_that_carries_the_new_tick_cannot_change_the_book_the_guard_reads(self):
        # A book message at the new tick gives its tick first and then its
        # prices, which sit far from the guard's price. The guard judged the
        # last quote before the change.
        watch_id = self.adopt(T)
        self.snipe.on_update(Update(T, None, None, 0.99, 0.995, Kind.PRICE_CHANGE))
        self.snipe.on_update(Update(T, 0.001, None, None, None, Kind.BOOK))
        self.snipe.on_update(Update(T, None, None, 0.9, 0.91, Kind.BOOK))
        self.assertEqual(len(self.post.calls), 1)
        self.tick()
        row = self.row(watch_id)
        self.assertEqual((row.state, row.bid, row.ask), (WatchState.FIRED, 0.99, 0.995))

    def test_a_poll_book_at_the_new_tick_is_a_trigger_and_not_a_quote(self):
        watch_id = self.adopt(T)
        self.books[T] = ("0.001", 0.9, 0.91)
        self.snipe.poll_once()
        self.tick()
        self.assertEqual((self.row(watch_id).state, self.row(watch_id).bid),
                         (WatchState.FIRED, 0.99))

    def test_a_stale_presigned_order_is_signed_fresh_on_the_hot_path(self):
        self.adopt(T)
        self.mono.now += PRESIGN_MAX_AGE_S + 1
        self.cross()
        self.assertEqual(self.post.calls[0][1].serial, 2)

    def test_the_slow_lane_keeps_the_presigned_order_young(self):
        self.adopt(T)
        self.mono.now += PRESIGN_S
        self.snipe.work_once()
        self.assertEqual(len(self.signer.signed), 2)
        self.snipe.work_once()
        self.assertEqual(len(self.signer.signed), 2, "signed again before PRESIGN_S passed")
        self.cross()
        self.assertEqual(self.post.calls[0][1].serial, 2)

    def test_a_watch_without_a_guard_fires_with_no_quote_at_all(self):
        self.books[T] = ("0.01", None, None)
        watch_id = self.arm(T, guard=None)
        self.tick()
        self.snipe.work_once()
        self.tick()
        self.cross()
        self.tick()
        self.assertEqual(self.row(watch_id).state, WatchState.FIRED)

    def test_each_guard_refusal_closes_rejected_with_its_reason_and_posts_nothing(self):
        cases = [("no quote", (None, None), 0.0, Reason.GUARD_NO_QUOTE),
                 ("price", (0.98, 0.985), 0.0, Reason.GUARD_PRICE),
                 ("stale", (0.99, 0.995), 6.0, Reason.GUARD_STALE)]
        for n, (name, (bid, ask), age, reason) in enumerate(cases):
            with self.subTest(name=name):
                token = f"{n}" * 38
                self.books[token] = ("0.01", bid, ask)
                watch_id = self.arm(token)
                self.tick()
                self.snipe.work_once()
                self.tick()
                self.mono.now += age
                self.cross(token)
                self.tick()
                self.assert_closed(watch_id, WatchState.REJECTED, reason)
                self.assertEqual(self.post.calls, [])
                self.assertIn("rejected by the guard", self.lines(watch_id)[-1])
                self.assertIn("nothing sent", self.lines(watch_id)[-1])

    def test_a_second_crossing_of_a_closed_watch_does_nothing(self):
        self.adopt(T)
        self.cross()
        self.tick()
        self.cross()
        self.cross(source="poll")
        self.tick()
        self.assertEqual(len(self.post.calls), 1)

    def test_a_tick_change_for_a_token_nobody_watches_is_ignored(self):
        self.cross(U)
        self.assertEqual(self.post.calls, [])

    def test_a_sell_is_judged_by_the_ask_and_signed_as_a_sell(self):
        self.books[T] = ("0.01", 0.005, 0.01)
        new = db.NewWatch(LimitOrder(T, Side.SELL, 0.001, 100.0, 0), MARKET, "x -> No", "0.001",
                          Guard(0.01, 0.005, 5.0), False)
        watch_id = db.arm_watch(self.db, new, NOW)
        self.tick()
        self.snipe.work_once()
        self.tick()
        self.cross()
        self.tick()
        self.assertEqual(self.row(watch_id).state, WatchState.FIRED)
        self.assertEqual(self.post.calls[0][1].order.side, Side.SELL)


class AnswerTests(Harness):
    def setUp(self):
        super().setUp()
        self.build()

    def test_a_refusal_is_never_fired_whatever_the_answer_carries(self):
        for answer in (Rejected("HTTP 400: bad"), Rejected(""), Unanswered("TimeoutError"),
                       Unanswered("the CLOB reported success with no order id")):
            with self.subTest(answer=answer):
                self.post.answers = [answer]
                token = str(abs(hash(answer.__class__.__name__ + str(answer))))[:8] * 5
                watch_id = self.adopt(token)
                self.cross(token)
                self.mono.now += RETRY_WINDOW_S + 1
                self.snipe.work_once()
                self.tick()
                row = self.row(watch_id)
                self.assertEqual((row.state, row.order_id), (WatchState.FAILED, None))

    def test_a_plain_refusal_is_retried_with_a_fresh_signature_inside_the_window(self):
        self.post.answers = [Rejected(TICK_RULE), Rejected(TICK_RULE), Posted("0xnew")]
        watch_id = self.adopt(T)
        self.cross()
        self.assertEqual(len(self.post.calls), 1)
        self.snipe.work_once()
        self.assertEqual(len(self.post.calls), 1, "retried before RETRY_DELAY_S passed")
        self.mono.now += RETRY_DELAY_S
        self.snipe.work_once()
        self.mono.now += RETRY_DELAY_S
        self.snipe.work_once()
        self.assertEqual([signed.serial for _, signed, _, _ in self.post.calls], [1, 2, 3])
        self.assertEqual(self.post.thread_ids[1:], [threading.get_ident()] * 2)
        self.tick()
        row = self.row(watch_id)
        self.assertEqual((row.state, row.order_id, row.attempts), (WatchState.FIRED, "0xnew", 3))
        self.assertEqual(self.lines(watch_id)[2:], [
            f"attempt 1 refused: {TICK_RULE}; retrying",
            f"attempt 2 refused: {TICK_RULE}; retrying",
            "fired: order 0xnew accepted on attempt 3"])

    def test_a_refusal_that_outlasts_the_window_fails_with_the_venues_message(self):
        self.post.answers = [Rejected(TICK_RULE)]
        watch_id = self.adopt(T)
        self.cross()
        steps = 0
        while self.row(watch_id).open and steps < 100:
            self.mono.now += RETRY_DELAY_S
            self.snipe.work_once()
            self.snipe.tick()
            steps += 1
        self.assert_closed(watch_id, WatchState.FAILED, Reason.VENUE, TICK_RULE)
        # The first attempt, then one every RETRY_DELAY_S until the window closes.
        self.assertEqual(len(self.post.calls), int(RETRY_WINDOW_S / RETRY_DELAY_S))
        self.assertEqual(self.row(watch_id).attempts, len(self.post.calls))
        self.assertIn("the retry window closed", self.lines(watch_id)[-1])

    def test_a_refusal_no_retry_can_change_stops_at_once(self):
        for reason in (NO_FUNDS, "HTTP 451: Unavailable For Legal Reasons",
                       "HTTP 403: Trading is restricted in your region", "post-only crosses book"):
            with self.subTest(reason=reason):
                self.post.answers = [Rejected(reason)]
                token = str(abs(hash(reason)))[:8] * 5
                watch_id = self.adopt(token)
                self.cross(token)
                self.mono.now += RETRY_DELAY_S
                self.snipe.work_once()
                self.tick()
                self.assert_closed(watch_id, WatchState.FAILED, Reason.VENUE, reason)
                self.assertEqual(len(self.post.calls), 1)
                self.post.calls.clear()

    def test_an_unanswered_post_is_never_retried_and_says_the_order_may_rest(self):
        self.post.answers = [Unanswered("ReadTimeout: timed out")]
        watch_id = self.adopt(T)
        self.cross()
        self.mono.now += RETRY_DELAY_S
        self.snipe.work_once()
        self.tick()
        self.assertEqual(len(self.post.calls), 1)
        self.assert_closed(watch_id, WatchState.FAILED, Reason.UNANSWERED, "ReadTimeout: timed out")
        self.assertTrue(self.row(watch_id).reason.may_rest)
        self.assertIn("the order may rest", self.lines(watch_id)[-1])

    def test_an_order_that_cannot_be_signed_fails_without_a_post(self):
        self.adopt(T)
        self.mono.now += PRESIGN_MAX_AGE_S + 1
        self.signer.sign_error = KeyError("no neg risk")
        self.cross()
        self.tick()
        self.assert_closed(1, WatchState.FAILED, Reason.SIGN)
        self.assertEqual(self.post.calls, [])


class DryRunTests(Harness):
    def test_a_dry_row_never_reaches_a_function_that_can_post_and_ends_rehearsed(self):
        self.build()

        def never():
            raise AssertionError("a dry run reached the post")

        self.post.on_call = never
        self.books[T] = ("0.01", 0.99, 0.995)
        watch_id = self.arm(T, dry=True)
        self.tick()
        self.snipe.work_once()
        self.tick()
        self.assertEqual(self.signer.prewarmed, [T], "a keyed rehearsal signs what a fire signs")
        self.cross()
        self.tick()
        self.assert_closed(watch_id, WatchState.REHEARSED, None, "signed, not sent")
        row = self.row(watch_id)
        self.assertEqual((row.order_id, row.attempts, row.tick, row.bid), (None, 1, "0.001", 0.99))
        self.assertEqual(self.post.calls, [])
        self.assertIn("rehearsed", self.lines(watch_id)[-1])

    def test_a_dry_row_still_meets_the_guard(self):
        self.build()
        self.books[T] = ("0.01", 0.95, 0.96)
        watch_id = self.arm(T, dry=True)
        self.tick()
        self.snipe.work_once()
        self.tick()
        self.cross()
        self.tick()
        self.assert_closed(watch_id, WatchState.REJECTED, Reason.GUARD_PRICE)


class StopTests(Harness):
    def setUp(self):
        super().setUp()
        self.build()

    def test_stop_set_before_the_claim_posts_nothing_and_leaves_the_row_armed(self):
        watch_id = self.adopt(T)
        self.stop.set()
        self.cross()
        self.cross(source="poll")
        self.assertEqual(self.post.calls, [])
        self.snipe.close()
        self.assertEqual(self.row(watch_id).state, WatchState.ARMED)

    def test_stop_set_after_the_claim_lets_the_first_post_go_and_starts_no_retry(self):
        self.post.answers = [Rejected(TICK_RULE)]
        self.post.on_call = self.stop.set
        watch_id = self.adopt(T)
        self.cross()
        self.assertEqual(len(self.post.calls), 1)
        self.mono.now += RETRY_DELAY_S
        self.snipe.work_once()
        self.assertEqual(len(self.post.calls), 1, "a retry started after the stop")
        self.snipe.close()
        self.assert_closed(watch_id, WatchState.FAILED, Reason.STOPPED, TICK_RULE)
        self.assertEqual(self.row(watch_id).attempts, 1)

    def test_a_posted_order_closes_fired_even_when_the_worker_stops_on_the_answer(self):
        self.post.on_call = self.stop.set
        watch_id = self.adopt(T)
        self.cross()
        self.snipe.close()
        self.assertEqual(self.row(watch_id).state, WatchState.FIRED)


class DisarmTests(Harness):
    def setUp(self):
        super().setUp()
        self.build()

    def test_a_disarm_seen_before_the_trigger_wins(self):
        watch_id = self.adopt(T)
        db.request_disarm(self.db, watch_id, NOW)
        self.tick()
        self.cross()
        self.cross(source="poll")
        self.assertEqual(self.post.calls, [])
        self.assert_closed(watch_id, WatchState.DISARMED)
        self.assertEqual(FakeFeed.built[0].sets[-1], [])
        self.assertEqual(self.lines(watch_id)[-1], "disarmed")

    def test_a_disarm_requested_during_the_post_closes_fired_and_still_shows_the_request(self):
        watch_id = self.adopt(T)
        self.post.on_call = lambda: db.request_disarm(self.db, watch_id, NOW)
        self.cross()
        self.tick()
        row = self.row(watch_id)
        self.assertEqual((row.state, row.order_id), (WatchState.FIRED, "0xnew"))
        self.assertEqual(row.disarm_requested_at, NOW)

    def test_a_disarm_inside_the_retry_window_stops_the_retries_and_closes_disarmed(self):
        self.post.answers = [Rejected(TICK_RULE)]
        watch_id = self.adopt(T)
        self.cross()
        db.request_disarm(self.db, watch_id, NOW)
        self.tick()
        self.assertEqual(self.row(watch_id).state, WatchState.FIRING)
        self.mono.now += RETRY_DELAY_S
        self.snipe.work_once()
        self.tick()
        self.assertEqual(len(self.post.calls), 1)
        self.assert_closed(watch_id, WatchState.DISARMED, None, TICK_RULE)
        self.assertEqual(self.row(watch_id).disarm_requested_at, NOW)

    def test_a_disarm_of_a_row_not_yet_adopted_closes_it_at_once(self):
        self.books[T] = ("0.01", 0.99, 0.995)
        watch_id = self.arm(T)
        db.request_disarm(self.db, watch_id, NOW)
        self.tick()
        self.assert_closed(watch_id, WatchState.DISARMED)
        self.snipe.work_once()
        self.assertEqual(self.signer.prewarmed, [])


class HotPathTests(Harness):
    def test_no_statement_runs_between_the_claim_and_the_post(self):
        statements = []
        real_open = db.open_db

        def spied(path):
            conn = real_open(path)

            class Spy:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, *args):
                    statements.append(sql)
                    return conn.execute(sql, *args)

                def __enter__(self):
                    return conn.__enter__()

                def __exit__(self, *exc):
                    return conn.__exit__(*exc)

            return Spy()

        with mock.patch.object(db, "open_db", spied):
            self.build()
        watch_id = self.adopt(T)
        seen = []
        before = []
        self.post.on_call = lambda: seen.append((len(statements), self.row(watch_id).state))
        before.append(len(statements))
        self.cross()
        self.assertEqual(seen, [(before[0], WatchState.ARMED)])
        self.tick()
        self.assertEqual(self.row(watch_id).state, WatchState.FIRED)

    def test_the_outbox_is_written_as_firing_then_fired_in_one_control_tick(self):
        self.build()
        watch_id = self.adopt(T)
        self.post.answers = [Rejected(TICK_RULE)]
        self.cross()
        self.tick()
        row = self.row(watch_id)
        self.assertEqual((row.state, row.source, row.tick), (WatchState.FIRING, "feed", "0.001"))
        self.assertTrue(row.open)


class PublishTests(Harness):
    def setUp(self):
        super().setUp()
        self.build()

    def test_the_latest_quote_is_published_once_a_second_with_its_tick(self):
        watch_id = self.adopt(T)
        self.snipe.on_update(Update(T, None, None, 0.985, 0.99, Kind.PRICE_CHANGE))
        self.tick(0.1)
        self.assertEqual(self.row(watch_id).bid, 0.99, "published before PUBLISH_S passed")
        self.tick(1.0)
        row = self.row(watch_id)
        self.assertEqual((row.tick, row.bid, row.ask, row.seen_at),
                         ("0.01", 0.985, 0.99,
                          (self.clock_now - timedelta(seconds=1.1)).replace(microsecond=0)))

    def test_a_poll_quote_newer_than_the_feeds_wins(self):
        watch_id = self.adopt(T)
        self.snipe.on_update(Update(T, None, None, 0.985, 0.99, Kind.PRICE_CHANGE))
        self.mono.now += 0.5
        self.books[T] = ("0.01", 0.98, 0.985)
        self.snipe.poll_once()
        self.tick()
        self.assertEqual(self.row(watch_id).bid, 0.98)

    def test_a_poll_that_fails_is_logged_and_the_next_one_is_the_retry(self):
        watch_id = self.adopt(T)
        self.books[T] = httpx.ConnectError("down")
        self.snipe.poll_once()
        self.tick()
        self.assertIn("poll failed", self.lines(watch_id)[-1])
        self.books[T] = ("0.01", 0.98, 0.985)
        self.snipe.poll_once()
        self.tick()
        self.assertEqual(self.row(watch_id).bid, 0.98)

    def test_a_feed_error_is_logged_against_every_routed_watch(self):
        first, second = self.adopt(T, U)
        FakeFeed.built[0].on_error(ConnectionError("gone"))
        self.tick()
        for watch_id in (first, second):
            self.assertIn("feed: ConnectionError: gone", self.lines(watch_id)[-1])

    def test_the_sniper_polls_nothing_while_no_watch_is_routed(self):
        self.snipe.poll_once()
        self.assertEqual(self.lines(1), [])


class RunTests(Harness):
    """run() on its own threads, stopped at once: the one test that starts them."""

    def test_run_opens_starts_the_lanes_and_closes_them_when_stopped(self):
        self.build()
        self.snipe.close()
        self.stop.set()
        self.snipe.run()
        self.assertEqual(len(FakeFeed.built), 1)
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("sniper")], [])

    def test_a_lane_that_raises_stops_the_sniper_and_run_raises_it(self):
        self.build()
        self.snipe.close()
        self.books[T] = ("0.01", 0.99, 0.995)
        self.arm(T)
        self.mono.now += 1

        def broken():
            raise RuntimeError("lane bug")

        self.snipe.poll_once = broken
        with self.assertRaisesRegex(RuntimeError, "lane bug"):
            self.snipe.run()
        self.assertTrue(self.stop.is_set())


if __name__ == "__main__":
    unittest.main()
