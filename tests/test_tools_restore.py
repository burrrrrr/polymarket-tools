import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from polymarket.orders import OpenOrder, Side
from tools import db, restore
from tools.db import JobKind, RestoreAction

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
FAR = NOW + timedelta(days=3)
MARKET_A, MARKET_B, MARKET_C = ("0x" + pair * 32 for pair in ("aa", "bb", "cc"))
ACCEPT = "accept"
NO_FUNDS = "not enough balance / allowance"

WOULD, PLACED, LIVE, EXPIRED, FARMER, FILLED, REJECTED, ERROR = (
    RestoreAction.WOULD_PLACE, RestoreAction.PLACED, RestoreAction.SKIP_LIVE,
    RestoreAction.SKIP_EXPIRED, RestoreAction.SKIP_FARMER, RestoreAction.SKIP_FILLED,
    RestoreAction.REJECTED, RestoreAction.ERROR)


def order(order_id="0xo1", market=MARKET_A, side=Side.BUY, price=0.98, size=100.0, matched=0.0,
          expiration=None, token=None):
    return OpenOrder(order_id, token or f"token-{market[-2:]}", market, "No", side, price, size,
                     matched, expiration)


def stored(*orders):
    return [db.SnapshotOrder(n, 1, None, o) for n, o in enumerate(orders, 1)]


def actions(lines):
    return [line.action for line in lines]


class PlanTests(unittest.TestCase):
    def plan(self, snapshot, live=(), managed=(), now=NOW):
        return restore.plan(stored(*snapshot), list(live), set(managed), now)

    def test_an_order_that_is_gone_is_placed_whole(self):
        (line,) = self.plan([order(size=100.0)])
        self.assertEqual((line.action, line.size), (WOULD, 100.0))

    def test_there_is_one_line_for_each_snapshot_order_in_the_snapshots_order(self):
        snapshot = stored(
            order("0xa"), order("0xb", MARKET_B), order("0xc", expiration=NOW - timedelta(days=1)),
            order("0xd", size=50.0, matched=50.0), order("0xe"), order("0xf", MARKET_C))
        lines = restore.plan(snapshot, [order("0xlive")], {MARKET_B}, NOW)
        self.assertEqual([line.order for line in lines], snapshot)
        self.assertEqual(actions(lines), [LIVE, FARMER, EXPIRED, FILLED, WOULD, WOULD])
        self.assertEqual(restore.plan([], [order()], set(), NOW), [])

    def test_a_partly_filled_order_is_placed_for_what_was_left(self):
        (line,) = self.plan([order(size=100.0, matched=40.0)])
        self.assertEqual((line.action, line.size), (WOULD, 60.0),
                         "a partly filled order was planned at the size it was first placed with")
        self.assertIn("40", line.reason)

    def test_what_was_left_is_cut_to_the_hundredth_of_a_share_an_order_can_have(self):
        (line,) = self.plan([order(size=3000.0, matched=204.137143)])
        self.assertEqual(line.size, 2795.86)

    def test_an_order_with_nothing_left_is_not_placed(self):
        for matched in (100.0, 99.999):
            with self.subTest(matched=matched):
                (line,) = self.plan([order(size=100.0, matched=matched)])
                self.assertEqual((line.action, line.size), (FILLED, 0.0))

    def test_an_expired_order_is_skipped(self):
        for expiration in (NOW - timedelta(days=5), NOW - timedelta(seconds=1), NOW):
            with self.subTest(expiration=expiration):
                (line,) = self.plan([order(expiration=expiration)])
                self.assertEqual(line.action, EXPIRED, "an expired order was planned for placing")

    def test_an_order_that_expires_too_soon_for_the_clob_to_take_is_skipped(self):
        (soon,) = self.plan([order(expiration=NOW + restore.EXPIRY_MARGIN)])
        (later,) = self.plan([order(expiration=NOW + restore.EXPIRY_MARGIN
                                    + timedelta(seconds=1))])
        self.assertEqual((soon.action, later.action), (EXPIRED, WOULD))
        self.assertGreater(restore.EXPIRY_MARGIN, timedelta(minutes=1))

    def test_an_order_with_no_expiration_never_expires(self):
        (line,) = self.plan([order(expiration=None)], now=NOW + timedelta(days=4000))
        self.assertEqual(line.action, WOULD)

    def test_an_order_on_a_market_the_farmer_manages_is_skipped(self):
        lines = self.plan([order("0xa", MARKET_A), order("0xb", MARKET_B)], managed={MARKET_B})
        self.assertEqual(actions(lines), [WOULD, FARMER])

    def test_an_order_the_snapshot_records_as_on_a_farmer_market_is_skipped_whatever_is_managed_now(
            self):
        farmers = db.SnapshotOrder(1, 1, None, order("0xa", MARKET_A), True)
        mine = db.SnapshotOrder(2, 1, None, order("0xb", MARKET_B), False)
        lines = restore.plan([farmers, mine], [], set(), NOW)
        self.assertEqual(actions(lines), [FARMER, WOULD])
        self.assertIn("when the snapshot was taken", lines[0].reason)

    def test_an_unrecorded_order_on_a_market_the_farmer_has_retired_is_skipped(self):
        unrecorded = db.SnapshotOrder(1, 1, None, order("0xa", MARKET_A))
        placed_since = db.SnapshotOrder(2, 1, None, order("0xb", MARKET_A), False)
        elsewhere = db.SnapshotOrder(3, 1, None, order("0xc", MARKET_B))
        lines = restore.plan([unrecorded, placed_since, elsewhere], [], set(), NOW,
                             retired_markets={MARKET_A})
        self.assertEqual(actions(lines), [FARMER, WOULD, WOULD],
                         "only an order with no record is judged by the market's retirement")
        self.assertIn("retired", lines[0].reason)

    def test_an_order_with_an_open_copy_is_skipped(self):
        (line,) = self.plan([order("0xold")], live=[order("0xnew")])
        self.assertEqual(line.action, LIVE)

    def test_two_identical_orders_with_one_open_copy_are_one_skip_and_one_place(self):
        twins = [order("0xa"), order("0xb")]
        self.assertEqual(actions(self.plan(twins, live=[order("0xlive")])), [LIVE, WOULD],
                         "one open copy hid both of two identical orders")
        self.assertEqual(actions(self.plan(twins, live=[order("0xl1"), order("0xl2")])),
                         [LIVE, LIVE])
        self.assertEqual(actions(self.plan(twins + [order("0xc")], live=[order("0xlive")])),
                         [LIVE, WOULD, WOULD])

    def test_a_copy_is_an_open_order_of_the_same_token_side_price_and_size_left(self):
        base = order("0xold", size=100.0, matched=40.0)
        self.assertEqual(self.plan([base], live=[order("0xnew", size=60.0)])[0].action, LIVE)
        for other in (order("0xnew", size=100.0), order("0xnew", size=60.0, price=0.97),
                      order("0xnew", size=60.0, side=Side.SELL),
                      order("0xnew", size=60.0, token="token-zz")):
            with self.subTest(other=other):
                self.assertEqual(self.plan([base], live=[other])[0].action, WOULD)

    def test_an_order_that_still_rests_is_skipped_however_much_more_of_it_filled(self):
        (line,) = self.plan([order("0xsame", size=100.0)],
                            live=[order("0xsame", size=100.0, matched=30.0)])
        self.assertEqual(line.action, LIVE, "an order that still rests was planned a second time")

    def test_an_order_that_still_rests_is_not_taken_for_the_copy_of_its_twin(self):
        lines = self.plan([order("0xgone"), order("0xrests")], live=[order("0xrests")])
        self.assertEqual(actions(lines), [WOULD, LIVE])

    def test_an_expired_or_managed_order_uses_up_no_open_copy(self):
        lines = self.plan([order("0xa", expiration=NOW - timedelta(hours=1)), order("0xb")],
                          live=[order("0xlive")])
        self.assertEqual(actions(lines), [EXPIRED, LIVE])

    def test_every_line_says_why(self):
        lines = self.plan(
            [order("0xa"), order("0xb", MARKET_B), order("0xc", expiration=NOW),
             order("0xd", MARKET_C, expiration=NOW + timedelta(seconds=30)),
             order("0xe", MARKET_C, size=5.0, matched=5.0)],
            live=[order("0xlive")], managed={MARKET_B})
        self.assertEqual(actions(lines), [LIVE, FARMER, EXPIRED, EXPIRED, FILLED])
        for line in lines:
            self.assertTrue(line.reason, line.action)
        self.assertNotEqual(lines[2].reason, lines[3].reason)


class Venue:
    def __init__(self, listing=(), answer=lambda signed: ACCEPT, tick="0.001"):
        self.listing = listing
        self.answer = answer
        self.tick = tick
        self.listed = 0
        self.markets_read = []
        self.signed = []
        self.posts = []
        self.after_post = lambda: None

    def get_open_orders(self, params=None):
        self.listed += 1
        if isinstance(self.listing, Exception):
            raise self.listing
        return [{"id": o.order_id, "asset_id": o.token_id, "market": o.market,
                 "outcome": o.outcome, "side": o.side.value, "price": str(o.price),
                 "original_size": str(o.size), "size_matched": str(o.size_matched),
                 "expiration": str(int(o.expiration.timestamp())) if o.expiration else "0"}
                for o in self.listing]

    def get_clob_market_info(self, condition_id):
        self.markets_read.append(condition_id)
        if isinstance(self.tick, Exception):
            raise self.tick
        return {"mts": self.tick}

    def get_tick_size(self, token_id):
        return self.tick

    def create_order(self, args, options):
        self.signed.append(args)
        return args

    def post_order(self, signed, order_type="GTC", post_only=False, defer_exec=False):
        self.posts.append((signed.token_id, signed.side, signed.price, signed.size,
                           signed.expiration, order_type, post_only))
        answer = self.answer(signed)
        self.after_post()
        if isinstance(answer, Exception):
            raise answer
        if answer == ACCEPT:
            return {"success": True, "errorMsg": "", "orderID": f"0xnew{len(self.posts)}",
                    "status": "live"}
        return {"success": False, "errorMsg": answer, "orderID": f"0xnew{len(self.posts)}"}


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = db.open_db(os.path.join(tmp.name, "tools.db"))
        self.addCleanup(self.db.close)
        self.now = NOW
        self.stop = threading.Event()

    def job(self, kind, orders, managed=(), queued_at=NOW, retired=()):
        snapshot = db.insert_snapshot(self.db, NOW - timedelta(days=1), "", orders, {})
        db.enqueue_job(self.db, kind, {"snapshot_id": snapshot, "managed_markets": list(managed),
                                       "retired_markets": list(retired)}, queued_at)
        return db.claim_job(self.db, queued_at)

    def place(self, venue, orders, **job):
        job = self.job(JobKind.RESTORE, orders, **job)
        try:
            restore.place(venue, self.db, job, lambda: self.now, self.stop)
        finally:
            self.lines = db.read_restore_lines(self.db, job.id)
        return self.lines

    def results(self):
        return [(line.action, line.size, line.order_id) for line in self.lines]


class PlaceTests(Scratch):
    def test_a_missing_order_is_signed_as_it_was_and_posted(self):
        venue = Venue()
        self.place(venue, [order(side=Side.SELL, price=0.985, size=250.0, expiration=FAR)])
        self.assertEqual(venue.posts, [("token-aa", "SELL", 0.985, 250.0, int(FAR.timestamp()),
                                        "GTD", True)])
        self.assertEqual(self.results(), [(PLACED, 250.0, "0xnew1")])

    def test_an_order_with_no_expiration_is_posted_good_til_cancelled(self):
        venue = Venue()
        self.place(venue, [order(expiration=None)])
        self.assertEqual(venue.posts, [("token-aa", "BUY", 0.98, 100.0, 0, "GTC", True)])

    def test_every_order_is_posted_post_only(self):
        venue = Venue()
        self.place(venue, [order("0xa"), order("0xb", MARKET_B, Side.SELL, expiration=FAR),
                           order("0xc", MARKET_C, price=0.5)])
        self.assertEqual(len(venue.posts), 3)
        self.assertEqual([post_only for *_, post_only in venue.posts], [True, True, True],
                         "a restored order was posted without post-only and could take liquidity")

    def test_a_partly_filled_order_is_posted_for_what_was_left(self):
        venue = Venue()
        self.place(venue, [order(size=3000.0, matched=204.137143)])
        self.assertEqual([size for _, _, _, size, *_ in venue.posts], [2795.86],
                         "a partly filled order was posted at the size it was first placed with")
        self.assertEqual(self.results(), [(PLACED, 2795.86, "0xnew1")])

    def test_a_failed_listing_places_nothing_and_stores_no_line(self):
        venue = Venue(listing=ConnectionError("timed out"))
        with self.assertRaisesRegex(ConnectionError, "timed out", msg="the restore went on "
                                    "although the open orders could not be listed"):
            self.place(venue, [order("0xa"), order("0xb", MARKET_B)])
        self.assertEqual(venue.posts, [], "orders were placed although the open orders could "
                                          "not be listed")
        self.assertEqual((venue.signed, self.lines), ([], []))

    def test_an_unreadable_listing_places_nothing(self):
        venue = Venue(listing=[order()])
        venue.get_open_orders = lambda params=None: {"error": "unauthorized"}
        with self.assertRaises(ValueError):
            self.place(venue, [order("0xa")])
        self.assertEqual(venue.posts, [])

    def test_an_order_that_is_open_expired_or_the_farmers_is_never_signed(self):
        venue = Venue(listing=[order("0xlive")])
        self.place(venue, [order("0xa"), order("0xb", expiration=NOW - timedelta(days=1)),
                           order("0xc", MARKET_B), order("0xd", size=9.0, matched=9.0)],
                   managed=[MARKET_B])
        self.assertEqual(self.results(), [(LIVE, 100.0, None), (EXPIRED, 100.0, None),
                                          (FARMER, 100.0, None), (FILLED, 0.0, None)])
        self.assertEqual((venue.signed, venue.posts), ([], []))

    def test_two_identical_orders_with_one_open_copy_place_one(self):
        venue = Venue(listing=[order("0xlive")])
        self.place(venue, [order("0xa"), order("0xb")])
        self.assertEqual(self.results(), [(LIVE, 100.0, None), (PLACED, 100.0, "0xnew1")])
        self.assertEqual(len(venue.posts), 1)

    def test_an_order_the_venue_refuses_is_rejected_with_its_reason_and_no_order_id(self):
        venue = Venue(answer=lambda signed: NO_FUNDS if signed.price == 0.5 else ACCEPT)
        self.place(venue, [order("0xa"), order("0xb", MARKET_B, price=0.5), order("0xc", MARKET_C)])
        self.assertEqual(self.results(), [(PLACED, 100.0, "0xnew1"), (REJECTED, 100.0, None),
                                          (PLACED, 100.0, "0xnew3")],
                         "an order the venue refused was recorded as placed")
        self.assertEqual(self.lines[1].detail, NO_FUNDS)

    def test_a_post_with_no_answer_is_an_error_that_may_rest_and_is_sent_once(self):
        venue = Venue(answer=lambda signed: (TimeoutError("timed out") if signed.price == 0.5
                                             else ACCEPT))
        self.place(venue, [order("0xa", price=0.5), order("0xb", MARKET_B)])
        self.assertEqual(self.results(), [(ERROR, 100.0, None), (PLACED, 100.0, "0xnew2")])
        self.assertIn("may rest", self.lines[0].detail)
        self.assertIn("timed out", self.lines[0].detail)
        self.assertEqual([price for _, _, price, *_ in venue.posts], [0.5, 0.98],
                         "an order that may rest was posted again")

    def test_a_venue_that_stops_answering_ends_the_restore(self):
        venue = Venue(answer=lambda signed: TimeoutError("timed out"))
        orders = [order(f"0x{n}", price=n / 100) for n in range(1, 9)]
        with self.assertRaisesRegex(restore.Stopped, "no answer"):
            self.place(venue, orders)
        self.assertEqual(len(venue.posts), restore.MAX_UNANSWERED_IN_A_ROW)
        self.assertEqual(actions(self.lines), [ERROR] * restore.MAX_UNANSWERED_IN_A_ROW)

    def test_answers_between_lost_posts_keep_the_restore_going(self):
        lost = {0.01, 0.02, 0.04, 0.05}
        venue = Venue(answer=lambda signed: (TimeoutError("timed out") if signed.price in lost
                                             else ACCEPT))
        self.place(venue, [order(f"0x{n}", price=n / 100) for n in range(1, 7)])
        self.assertEqual(actions(self.lines), [ERROR, ERROR, PLACED, ERROR, ERROR, PLACED])

    def test_an_order_that_was_not_sent_does_not_break_a_run_of_lost_posts(self):
        venue = Venue(tick="0.01", answer=lambda signed: TimeoutError("timed out"))
        prices = (0.01, 0.985, 0.02, 0.975, 0.03, 0.04)
        with self.assertRaises(restore.Stopped):
            self.place(venue, [order(f"0x{n}", price=price) for n, price in enumerate(prices)])
        self.assertEqual([price for _, _, price, *_ in venue.posts], [0.01, 0.02, 0.03])

    def test_a_stop_between_orders_keeps_the_lines_of_the_orders_already_placed(self):
        venue = Venue()
        venue.after_post = lambda: len(venue.posts) == 2 and self.stop.set()
        orders = [order("0xa"), order("0xb", MARKET_B), order("0xc", MARKET_C),
                  order("0xd", price=0.4)]
        with self.assertRaisesRegex(restore.Stopped, "2 of 4"):
            self.place(venue, orders)
        self.assertEqual(self.results(), [(PLACED, 100.0, "0xnew1"), (PLACED, 100.0, "0xnew2")])
        self.assertEqual(len(venue.posts), 2)

    def test_a_restore_that_was_stopped_before_it_began_places_nothing(self):
        venue = Venue()
        self.stop.set()
        with self.assertRaises(restore.Stopped):
            self.place(venue, [order()])
        self.assertEqual((venue.posts, self.lines), ([], []))

    def test_a_restore_queued_too_long_ago_places_nothing(self):
        venue = Venue()
        self.now = NOW + db.RESTORE_MAX_WAIT + timedelta(seconds=1)
        with self.assertRaisesRegex(restore.Stopped, "queued"):
            self.place(venue, [order()])
        self.assertEqual((venue.listed, venue.posts, self.lines), (0, [], []))

    def test_a_restore_claimed_in_time_runs(self):
        venue = Venue()
        self.now = NOW + db.RESTORE_MAX_WAIT
        self.place(venue, [order()])
        self.assertEqual(actions(self.lines), [PLACED])

    def test_the_market_is_read_once_and_its_tick_signed_with(self):
        venue = Venue(tick="0.001")
        self.place(venue, [order("0xa", price=0.985), order("0xb", price=0.986),
                           order("0xc", MARKET_B, price=0.5)])
        self.assertEqual(venue.markets_read, [MARKET_A, MARKET_B])
        self.assertEqual(actions(self.lines), [PLACED, PLACED, PLACED])

    def test_a_price_the_markets_tick_cannot_hold_is_not_sent(self):
        venue = Venue(tick="0.01")
        self.place(venue, [order("0xa", price=0.985), order("0xb", price=0.98)])
        self.assertEqual(self.results(), [(ERROR, 100.0, None), (PLACED, 100.0, "0xnew1")])
        self.assertIn("not sent", self.lines[0].detail)
        self.assertNotIn("may rest", self.lines[0].detail)
        self.assertEqual([price for _, _, price, *_ in venue.posts], [0.98])

    def test_an_order_whose_market_cannot_be_read_is_not_sent(self):
        venue = Venue(tick=ConnectionError("reset"))
        self.place(venue, [order("0xa"), order("0xb")])
        self.assertEqual(actions(self.lines), [ERROR, ERROR])
        self.assertIn("not sent", self.lines[0].detail)
        self.assertIn("reset", self.lines[0].detail)
        self.assertEqual(venue.posts, [])

    def test_a_snapshot_that_is_not_stored_fails_the_restore(self):
        job = self.job(JobKind.RESTORE, [order()])
        with self.assertRaisesRegex(ValueError, "no snapshot 99"):
            restore.place(Venue(), self.db, job._replace(params={"snapshot_id": 99,
                                                                 "managed_markets": [],
                                                                 "retired_markets": []}),
                          lambda: NOW, self.stop)

    def test_the_summary_counts_what_became_of_the_orders(self):
        venue = Venue(listing=[order("0xlive")],
                      answer=lambda signed: NO_FUNDS if signed.price == 0.5 else ACCEPT)
        lines = self.place(venue, [order("0xa"), order("0xb"), order("0xc", MARKET_B, price=0.5),
                                   order("0xd", MARKET_C), order("0xe", MARKET_C)],
                           managed=[MARKET_C])
        self.assertEqual(restore.summary(lines), "1 placed, 1 rejected, 3 skipped")


class PreviewTests(Scratch):
    def preview(self, venue, orders, **job):
        job = self.job(JobKind.PREVIEW, orders, **job)
        try:
            restore.preview(venue, self.db, job, NOW)
        finally:
            self.lines = db.read_restore_lines(self.db, job.id)
        return self.lines

    def test_a_preview_stores_the_plan_and_signs_and_posts_nothing(self):
        venue = Venue(listing=[order("0xlive")])
        self.preview(venue, [order("0xa"), order("0xb"), order("0xc", MARKET_B),
                             order("0xd", size=100.0, matched=40.0, price=0.5)],
                     managed=[MARKET_B])
        self.assertEqual(self.results(), [(LIVE, 100.0, None), (WOULD, 100.0, None),
                                          (FARMER, 100.0, None), (WOULD, 60.0, None)])
        self.assertEqual((venue.signed, venue.posts, venue.markets_read), ([], [], []))

    def test_a_failed_listing_stores_no_plan(self):
        with self.assertRaises(ConnectionError):
            self.preview(Venue(listing=ConnectionError("timed out")), [order()])
        self.assertEqual(self.lines, [])

    def test_a_previews_summary_never_says_placed(self):
        lines = self.preview(Venue(), [order("0xa"), order("0xb", expiration=NOW)])
        self.assertEqual(restore.summary(lines, preview=True), "1 to place, 1 skipped")


if __name__ == "__main__":
    unittest.main()
