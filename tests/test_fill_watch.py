import random
import unittest
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from farm import farm_db, reward_farmer
from farm.fill_watch import (
    BUY,
    EXIT_HOLD_S,
    FILL_CONFIRM_S,
    NO_FILLS,
    SELL,
    Ceiling,
    Fill,
    Gone,
    Priced,
    fill_pending,
    inventory_ceiling,
    orders_gone,
    seeded_fill_watch,
    watch_listing,
    watch_positions,
)
from farm.reward_farmer import Slot

NOW = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
FAV, LONG = "fav", "long"
LEFT = "order that left the book"


class Order(NamedTuple):
    """The fields of reward_farmer.LiveOrder the watch reads."""

    id: str
    token_id: str
    side: str
    price: float
    size_remaining: float


def at(seconds):
    return NOW + timedelta(seconds=seconds)


def left(price=0.93, shares=537.0, seconds=0, watch=NO_FILLS, side=BUY):
    """The watch after a listing showed a favorite order of ours at price left the book."""
    return watch_listing(watch, [Gone(side, "b1", price, shares, at(seconds))], at(seconds))


def filled(price=0.93, shares=537.0, seconds=0, watch=NO_FILLS):
    """The watch after a favorite bid at price left the book and positions showed it bought."""
    return watch_positions(left(price, shares, seconds, watch), shares, 0.0, None, None,
                           at(seconds)).watch


class OrdersGoneTest(unittest.TestCase):
    BID = Order("b1", FAV, BUY, 0.93, 537.0)
    ASK = Order("a1", FAV, SELL, 0.96, 100.0)

    def test_a_favorite_order_expected_to_rest_that_left_is_gone_whole(self):
        gone = orders_gone([self.BID, self.ASK], [self.ASK], FAV, NOW)
        self.assertEqual(gone, [Gone(BUY, "b1", 0.93, 537.0, NOW)])

    def test_an_order_that_shrank_is_gone_by_the_difference(self):
        partly = self.BID._replace(size_remaining=400.0)
        self.assertEqual(orders_gone([self.BID], [partly], FAV, NOW),
                         [Gone(BUY, "b1", 0.93, 137.0, NOW)])
        listed = self.BID._replace(size_remaining=536.99)
        self.assertEqual(orders_gone([self.BID], [listed], FAV, NOW), [])

    def test_an_order_posted_last_cycle_is_gone_only_when_the_listing_lacks_it(self):
        smaller = self.BID._replace(size_remaining=530.0)
        self.assertEqual(orders_gone([self.BID], [smaller], FAV, NOW, posted={"b1"}), [])
        self.assertEqual(orders_gone([self.BID], [], FAV, NOW, posted={"b1"}),
                         [Gone(BUY, "b1", 0.93, 537.0, NOW)])

    def test_an_ask_that_left_is_a_sale_and_a_longshot_order_is_not_watched(self):
        low = Order("l1", LONG, BUY, 0.01, 1000.0)
        self.assertEqual(orders_gone([self.ASK, low], [], FAV, NOW),
                         [Gone(SELL, "a1", 0.96, 100.0, NOW)])


class ListingTest(unittest.TestCase):
    def test_a_bid_that_left_pends_and_sets_no_ceiling(self):
        watch = left()
        self.assertTrue(fill_pending(watch))
        self.assertIsNone(watch.last_fill)

    def test_the_shares_positions_show_set_the_fill_as_of_when_it_left(self):
        watch = watch_positions(left(), 537.0, 0.0, None, None, at(60)).watch
        self.assertEqual(watch.last_fill, Priced(0.93, NOW))

    def test_an_ask_that_left_pends_no_bid_and_sets_the_exit_once_sold(self):
        watch = left(0.96, 100.0, side=SELL)
        self.assertFalse(fill_pending(watch))
        self.assertIsNone(watch.last_exit)
        sold = watch_positions(watch, 0.0, 100.0, None, None, at(30)).watch
        self.assertEqual(sold.last_exit, Priced(0.96, NOW))

    def test_orders_that_left_and_never_filled_leave_the_ceilings_as_they_were(self):
        before = NO_FILLS._replace(last_fill=Priced(0.95, at(-600)),
                                   last_exit=Priced(0.97, at(-300)))
        watch = watch_listing(before, [Gone(BUY, "b1", 0.93, 537.0, NOW),
                                       Gone(SELL, "a1", 0.96, 100.0, NOW)], NOW)
        seen = watch_positions(watch, 0.0, 0.0, None, None, at(FILL_CONFIRM_S))
        self.assertEqual([g.order_id for g in seen.stale], ["b1", "a1"])
        self.assertEqual((seen.watch.last_fill, seen.watch.last_exit),
                         (before.last_fill, before.last_exit))

    def test_shares_positions_showed_before_the_listing_are_not_waited_for_again(self):
        resting = Order("b1", FAV, BUY, 0.93, 537.0)
        seen = watch_positions(NO_FILLS, 537.0, 0.0, resting, None, NOW)
        watch = watch_listing(seen.watch, [Gone(BUY, "b1", 0.93, 537.0, at(5))], at(5))
        self.assertFalse(fill_pending(watch))
        late = at(FILL_CONFIRM_S)
        watch = watch_listing(seen.watch, [Gone(BUY, "b1", 0.93, 537.0, late)], late)
        self.assertTrue(fill_pending(watch), "a credit lasts as long as a pending bid would")

class PositionsTest(unittest.TestCase):
    def test_a_purchase_clears_the_bids_that_left_and_is_priced_at_them(self):
        watch = left(shares=500.0)
        watch = watch_listing(watch, [Gone(BUY, "b2", 0.92, 100.0, at(5))], at(5))
        seen = watch_positions(watch, 600.0, 0.0, None, None, at(60))
        self.assertFalse(fill_pending(seen.watch))
        self.assertAlmostEqual(seen.bid.price, (0.93 * 500 + 0.92 * 100) / 600, places=6)
        self.assertEqual((seen.bid.source, seen.bid.order_id, seen.bid.at), (LEFT, "b2", NOW))
        self.assertEqual(seen.watch.last_fill, Priced(seen.bid.price, NOW))
        self.assertEqual(seen.stale, ())

    def test_a_read_of_part_of_a_bid_that_left_keeps_the_rest_pending(self):
        first = watch_positions(left(), 300.0, 0.0, None, None, at(30))
        self.assertTrue(fill_pending(first.watch))
        self.assertEqual([g.shares for g in first.watch.pending], [237.0])
        rest = watch_positions(first.watch, 237.0, 0.0, None, None, at(60))
        self.assertFalse(fill_pending(rest.watch))
        self.assertEqual([(s.bid.price, s.bid.source, s.bid.at) for s in (first, rest)],
                         [(0.93, LEFT, NOW), (0.93, LEFT, NOW)])
        stale = watch_positions(first.watch, 0.0, 0.0, None, None, at(FILL_CONFIRM_S)).stale
        self.assertEqual([g.shares for g in stale], [237.0])

    def test_a_purchase_clears_the_bids_that_left_oldest_first(self):
        watch = watch_listing(left(shares=500.0), [Gone(BUY, "b2", 0.92, 100.0, at(5))], at(5))
        seen = watch_positions(watch, 550.0, 0.0, None, None, at(60))
        self.assertAlmostEqual(seen.bid.price, (0.93 * 500 + 0.92 * 50) / 550, places=6)
        self.assertEqual([(g.order_id, g.shares, g.price, g.at) for g in seen.watch.pending],
                         [("b2", 50.0, 0.92, at(5))])

    def test_a_purchase_the_listing_has_not_shown_is_priced_at_the_resting_bid(self):
        resting = Order("b1", FAV, BUY, 0.93, 537.0)
        seen = watch_positions(NO_FILLS, 100.0, 0.0, resting, None, NOW)
        self.assertEqual((seen.bid.price, seen.bid.source, seen.bid.order_id),
                         (0.93, "resting order at the last cycle", "b1"))
        self.assertEqual(seen.watch.last_fill, Priced(0.93, NOW))
        unknown = watch_positions(NO_FILLS, 100.0, 0.0, None, None, NOW)
        self.assertEqual((unknown.bid.price, unknown.bid.source),
                         (None, "unknown: no order rested in the slot"))
        self.assertIsNone(unknown.watch.last_fill)

    def test_a_sale_is_priced_at_the_ask_that_left(self):
        watch = watch_listing(NO_FILLS, [Gone(SELL, "a1", 0.96, 100.0, NOW)], NOW)
        seen = watch_positions(watch, 0.0, 100.0, None, None, at(30))
        self.assertEqual((seen.ask.price, seen.ask.source), (0.96, LEFT))
        self.assertEqual(seen.watch.pending, ())

    def test_a_bid_positions_never_show_filled_goes_stale(self):
        watch = left()
        early = watch_positions(watch, 0.0, 0.0, None, None, at(FILL_CONFIRM_S - 1))
        self.assertTrue(fill_pending(early.watch))
        self.assertEqual(early.stale, ())
        late = watch_positions(watch, 0.0, 0.0, None, None, at(FILL_CONFIRM_S))
        self.assertFalse(fill_pending(late.watch))
        self.assertEqual([g.order_id for g in late.stale], ["b1"])


class CeilingTest(unittest.TestCase):
    TICK = 0.01

    def test_holding_inventory_bids_a_tick_under_the_last_fill_and_the_cost(self):
        watch = filled(price=0.93)
        self.assertEqual(inventory_ceiling(watch, 478.0, 0.9305, self.TICK, NOW),
                         Ceiling(0.92, "holding 478 @ 93.05c, last fill 93c"))
        self.assertEqual(inventory_ceiling(watch, 478.0, 0.925, self.TICK, NOW).price, 0.91)
        self.assertEqual(inventory_ceiling(watch, 478.0, None, self.TICK, NOW),
                         Ceiling(0.92, "holding 478, last fill 93c"))
        self.assertIsNone(inventory_ceiling(watch, 0.0, 0.9305, self.TICK, NOW))

    def test_after_a_sale_bids_stay_a_tick_under_it_for_the_exit_hold(self):
        watch = NO_FILLS._replace(last_exit=Priced(0.95, NOW))
        self.assertEqual(inventory_ceiling(watch, 0.0, None, self.TICK, at(EXIT_HOLD_S - 1)),
                         Ceiling(0.94, "sold at 95c within 6h"))
        self.assertIsNone(inventory_ceiling(watch, 0.0, None, self.TICK, at(EXIT_HOLD_S)))

def stored_row(seen_at, slot, priced):
    """The FILL event the farmer stores for a fill positions showed at seen_at."""
    return farm_db.Event(seen_at, farm_db.EventKind.FILL, "FILL", "cid", slot.name,
                         priced.price, 0.0, None, reward_farmer.fill_data(slot, priced, None))


class SeedTest(unittest.TestCase):
    def test_a_restart_rebuilds_the_last_fill_and_sale_from_the_rows(self):
        fills = [Fill(BUY, 0.93, NOW, at(30)), Fill(SELL, 0.96, at(-3600), at(-3590)),
                 Fill(BUY, 0.94, at(-600), at(-590))]
        watch = seeded_fill_watch(fills)
        self.assertEqual((watch.last_fill, watch.last_exit),
                         (Priced(0.93, NOW), Priced(0.96, at(-3600))))
        self.assertEqual((watch.pending, watch.credits), ((), ()))

    def test_folding_the_live_events_equals_the_watch_seeded_from_the_rows_they_stored(self):
        rng = random.Random(7)
        for case in range(200):
            with self.subTest(case=case):
                live, rows = NO_FILLS, []
                t, mid = 0, 0.95
                for _ in range(rng.randint(1, 80)):
                    t += rng.choice((5, 30, 60, 60, 60, 300, 1800))
                    roll = rng.random()
                    if roll < 0.15:
                        side = rng.choice((BUY, BUY, SELL))
                        price = round(mid - rng.choice((0.0, 0.01, 0.02)), 2)
                        shares = rng.choice((100.0, 250.0))
                        live = watch_listing(live, [Gone(side, f"o{t}", price, shares, at(t))],
                                             at(t))
                    elif roll < 0.35:
                        bought, sold = (rng.choice((0.0, 0.0, 50.0, 100.0, 300.0))
                                        for _ in range(2))
                        resting = rng.choice((None, Order(f"r{t}", FAV, BUY, mid - 0.01, 100.0)))
                        seen = watch_positions(live, bought, sold, resting, None, at(t))
                        live = seen.watch
                        for slot, priced in ((Slot.FAV_BID, seen.bid), (Slot.FAV_ASK, seen.ask)):
                            if priced is not None:
                                rows.append(stored_row(at(t), slot, priced))
                    else:
                        mid = round(min(0.99, max(0.85, mid + rng.choice(
                            (-0.04, -0.01, 0.0, 0.0, 0.01, 0.02)))), 2)
                seeded = seeded_fill_watch(reward_farmer.stored_fills(list(reversed(rows))))
                self.assertEqual((seeded.last_fill, seeded.last_exit),
                                 (live.last_fill, live.last_exit))


if __name__ == "__main__":
    unittest.main()
