import unittest
from datetime import timedelta

from farm import reward_farmer
from farm.reward_farmer import (
    Cancel,
    Place,
    Position,
    Slot,
    around_kept_bid,
    keep_failure,
    order_score,
    quotes_at,
    reconcile,
)
from farm.reward_screener import Zones, adjusted_mid
from polymarket.books import Levels
from test_reward_farmer import CFG, FLOOR_BOOK, NOW, by_slot, live, market, plan, steady

# Shaped like Pam Bondi's live book on 2026-10-10, v 5.5c, where another
# maker's 107-share bid came and went and what a 0.1c share scores crossed
# 0.002, $0.50 a unit, with it. Here that bid sits one tick over the next:
# with it the midpoint is 94.65c and a 0.1c longshot share scores 0.00207,
# and without it the midpoint is 94.6c and the share scores 0.00132. Free of
# its $13 budget, the 0.1c bid is 353,509 shares beside a favorite bid of
# 93.7c x 1,067.23.
EDGE = market(v=0.055, min_size=100.0, p_true=0.98, naked_budget_usd=13.0, capital=1000.0)
EDGE_UP = Levels(bids=[(0.937, 107.0), (0.936, 2000.0)], asks=[(0.956, 2000.0)], tick=0.001)
EDGE_DOWN = Levels(bids=[(0.936, 2000.0)], asks=[(0.956, 2000.0)], tick=0.001)
# No lead over the best bid, so no bid of ours moves the midpoint.
EDGE_CFG = CFG._replace(max_lead=0.0)
# 30 days out only 0.1c to 0.3c offer under 5% a year, and without the
# 107-share bid none of them costs under $0.50 a unit. Under the budget the
# bids are 1.4c x 928.57 and 90.7c x 928.57.
EDGE_NEAR = EDGE._replace(end_date=NOW + timedelta(days=30), zones=Zones(30 / 365.25))


def current_mid(watch, now):
    """No dwell: a price is free whenever it passes its test this cycle."""
    return watch.mids[-1][1]


def run_cycles(fm, books, steady=reward_farmer.steady_mid, step_s=5, cfg=EDGE_CFG):
    """The farmer's planning in closed loop over one book a cycle, step_s
    apart, with its watch of the midpoint: per cycle, the orders replaced
    and the orders resting after it. steady(watch, now) is the midpoint a
    free price must also pass its test at; the farmer's own unless given."""
    orders, watch, serial, replaced, resting = [], None, 0, [], []
    for n, book in enumerate(books):
        now = NOW + timedelta(seconds=step_s * n)
        watch = reward_farmer.watch_mid(
            watch, adjusted_mid(book.bids, book.asks, fm.min_size), now)
        seen = fm._replace(zones=fm.zones._replace(steady_mid=steady(watch, now)))
        target = plan(seen, book, cfg=cfg, resting=orders)
        target = around_kept_bid(target, orders, seen, book, Position(), cfg, True)
        actions = reconcile(target, orders, seen, Position(), cfg)
        cancelled = {i for a in actions if isinstance(a, Cancel) for i in a.order_ids}
        placing = [a.intent for a in actions if isinstance(a, Place)]
        replaced.append(sorted(o.slot.name for o in orders if o.id in cancelled))
        orders = [o for o in orders if o.id not in cancelled]
        for intent in placing:
            serial += 1
            orders.append(live(f"r{serial}", intent.slot, intent.price, intent.size))
        resting.append({o.slot: (o.price, o.size_remaining) for o in orders})
    return replaced, resting


class FreeDwellTest(unittest.TestCase):
    """A price is free of the naked budget only once it has passed its test
    on every cycle for FREE_DWELL_S: the test is run at the lowest midpoint
    of the dwell."""

    def test_what_a_share_at_the_lowest_tick_scores_on_the_two_books(self):
        self.assertAlmostEqual(order_score(Slot.LONG_BID, 0.001, 0.9465, 0.055), 0.00207,
                               places=5)
        self.assertAlmostEqual(order_score(Slot.LONG_BID, 0.001, 0.946, 0.055), 0.00132,
                               places=5)

    def test_the_watch_keeps_the_midpoints_of_the_dwell(self):
        self.assertEqual(reward_farmer.FREE_DWELL_S, 600)
        watch = reward_farmer.watch_mid(None, 0.948, NOW)
        self.assertEqual(watch, reward_farmer.MidWatch(NOW, ((NOW, 0.948),)))
        for seconds in (300, 600, 605):
            watch = reward_farmer.watch_mid(watch, 0.95, NOW + timedelta(seconds=seconds))
        # Still watched since the first cycle. That cycle's midpoint is over
        # ten minutes old and gone.
        self.assertEqual(watch.since, NOW)
        self.assertEqual([mid for _, mid in watch.mids], [0.95, 0.95, 0.95])

    def test_a_one_sided_book_ends_the_watch(self):
        watch = reward_farmer.watch_mid(None, 0.948, NOW)
        self.assertIsNone(reward_farmer.watch_mid(watch, None, NOW + timedelta(seconds=5)))
        self.assertIsNone(reward_farmer.watch_mid(None, None, NOW))

    def test_the_steady_midpoint_is_the_lowest_of_the_dwell_once_watched_that_long(self):
        def at(seconds):
            return NOW + timedelta(seconds=seconds)

        self.assertIsNone(reward_farmer.steady_mid(None, NOW))
        watch = reward_farmer.watch_mid(None, 0.948, NOW)
        watch = reward_farmer.watch_mid(watch, 0.9455, at(5))
        watch = reward_farmer.watch_mid(watch, 0.948, at(10))
        self.assertIsNone(reward_farmer.steady_mid(watch, at(599)))
        self.assertEqual(reward_farmer.steady_mid(watch, at(600)), 0.9455)
        # The dip counts for ten minutes, to the second, and no longer.
        watch = reward_farmer.watch_mid(watch, 0.947, at(605))
        self.assertEqual(reward_farmer.steady_mid(watch, at(605)), 0.9455)
        watch = reward_farmer.watch_mid(watch, 0.949, at(610))
        self.assertEqual(reward_farmer.steady_mid(watch, at(610)), 0.947)

    def test_a_price_not_yet_steady_is_limited_by_the_budget(self):
        # On FLOOR_BOOK a $0.30 budget buys 300 shares at 0.1c, which balance
        # 352.08 of the 510.72 favorite shares at 97.9c. A market read from
        # its terms has no watch behind it, and its free zone is closed.
        fm = market(naked_budget_usd=0.3)
        self.assertIsNone(fm.zones.steady_mid)
        intents = by_slot(quotes_at(fm, FLOOR_BOOK, Position(), CFG, True, 0.979))
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual((low.price, low.size), (0.001, 300.0))
        self.assertEqual((fav.price, fav.size), (0.979, 352.08))
        held = by_slot(quotes_at(fm._replace(zones=steady(0.99)), FLOOR_BOOK, Position(), CFG,
                                 True, 0.979))
        self.assertEqual(held[Slot.FAV_BID].size, 510.72)

    def test_a_free_bid_over_the_budget_is_replaced_once_its_price_is_not_steady(self):
        # 675.82 shares at 0.1c cost $0.68 on a $0.50 budget. With the
        # midpoint at 96c in the last ten minutes a share there scored
        # nothing.
        fm = market(naked_budget_usd=0.5, zones=steady(0.99))
        target = plan(fm, FLOOR_BOOK)
        low = by_slot(target)[Slot.LONG_BID]
        order = live("b", Slot.LONG_BID, 0.001, 675.82)
        self.assertIsNone(keep_failure(order, low, target, fm, Position(), CFG))
        for zones in (steady(0.96), Zones(100 / 365.25)):
            with self.subTest(steady_mid=zones.steady_mid):
                self.assertEqual(
                    keep_failure(order, low, target, fm._replace(zones=zones), Position(), CFG),
                    "over the naked budget")

    def test_a_steady_midpoint_frees_the_price_and_one_dip_takes_it_back_for_the_dwell(self):
        # 121 cycles five seconds apart are the ten minutes. The midpoint
        # then dips for one cycle and holds again for another 121.
        books = [EDGE_UP] * 122 + [EDGE_DOWN] + [EDGE_UP] * 122
        replaced, resting = run_cycles(EDGE_NEAR, books)
        budgeted = {Slot.FAV_BID: (0.907, 928.57), Slot.LONG_BID: (0.014, 928.57)}
        free = {Slot.FAV_BID: (0.937, 1067.23), Slot.LONG_BID: (0.001, 353509.27)}
        # Under the budget until the dwell has passed, then free.
        self.assertEqual(resting[0], budgeted)
        self.assertEqual(resting[119], budgeted)
        self.assertEqual(replaced[120], ["FAV_BID", "LONG_BID"])
        self.assertEqual(resting[120], free)
        self.assertEqual(replaced[121], [])
        # One cycle that fails the test, and the budget limits the bid at once.
        self.assertEqual(replaced[122], ["FAV_BID", "LONG_BID"])
        self.assertLessEqual(resting[122][Slot.LONG_BID][0] * resting[122][Slot.LONG_BID][1],
                             13.0)
        # It stays there until the midpoint has held for the dwell again.
        self.assertEqual([r for r in replaced[123:243] if r], [])
        self.assertEqual(resting[242][Slot.LONG_BID], resting[122][Slot.LONG_BID])
        self.assertEqual(replaced[243], ["FAV_BID", "LONG_BID"])
        self.assertEqual(resting[243], free)
        # Only those three changes replaced an order.
        self.assertEqual(sum(1 for r in replaced if r), 3)

    def test_a_flickering_midpoint_never_frees_the_price_and_replaces_nothing(self):
        # The 107-share bid comes and goes every three cycles for half an
        # hour. The plan is the budgeted one throughout, and after the first
        # cycle places it nothing is replaced.
        books = ([EDGE_UP] * 3 + [EDGE_DOWN] * 3) * 60
        replaced, resting = run_cycles(EDGE_NEAR, books)
        self.assertEqual([r for r in replaced if r], [])
        for after in resting:
            price, size = after[Slot.LONG_BID]
            self.assertLessEqual(price * size, 13.0)
        # With the price free whenever it passes its test, the favorite bid
        # is replaced on every crossing: 119 times.
        flapping, _ = run_cycles(EDGE_NEAR, books, steady=current_mid)
        self.assertEqual(sum(r.count("FAV_BID") for r in flapping), 119)

    def test_a_dip_moves_a_far_dated_bid_to_the_cheapest_price_that_held_and_no_further(self):
        # 100 days out prices up to 1.3c offer under 5% a year. Without the
        # 107-share bid 0.1c costs $0.76 a unit of score, and 0.4c, at 1.5% a
        # year, is the cheapest price under $0.50. After a dip the longshot
        # bid rests there, sized to cover the whole favorite bid, and stays
        # when 0.1c has passed for the dwell again: a resting bid that covers
        # is kept over a cheaper target.
        books = [EDGE_UP] * 122 + [EDGE_DOWN] + [EDGE_UP] * 244
        replaced, resting = run_cycles(EDGE, books)
        self.assertEqual(resting[121][Slot.LONG_BID], (0.001, 353509.27))
        self.assertEqual(resting[122], {Slot.FAV_BID: (0.936, 1068.37),
                                        Slot.LONG_BID: (0.004, 86537.97)})
        self.assertEqual([r for r in replaced[123:] if r], [])
        self.assertEqual(resting[-1], resting[122])

    def test_a_flickering_midpoint_rests_a_far_dated_bid_at_the_price_that_held(self):
        # The flicker of the test above, 100 days out. Once the ten minutes
        # are watched the bid goes to 0.4c, which passed on every cycle, and
        # the longshot bid is never replaced again. The favorite bid is
        # replaced once more, when the 107-share bid it joined leaves.
        books = ([EDGE_UP] * 3 + [EDGE_DOWN] * 3) * 60
        replaced, resting = run_cycles(EDGE, books)
        self.assertEqual(resting[119][Slot.LONG_BID], (0.016, 812.5))
        self.assertEqual(replaced[120], ["FAV_BID", "LONG_BID"])
        self.assertEqual(resting[120][Slot.LONG_BID], (0.004, 73039.11))
        self.assertEqual([n for n, r in enumerate(replaced) if "LONG_BID" in r], [120])
        self.assertEqual([n for n, r in enumerate(replaced) if "FAV_BID" in r], [120, 123])
        self.assertEqual(resting[-1][Slot.FAV_BID], (0.936, 1068.37))

    def test_the_watch_is_seeded_from_the_last_runs_midpoints(self):
        def seeded(mids, last_ago_s=30, gap_s=64):
            """Samples gap_s apart ending last_ago_s ago, oldest first, and
            the steady midpoint the watch seeded from them gives now."""
            start = NOW - timedelta(seconds=last_ago_s + gap_s * (len(mids) - 1))
            samples = [(start + timedelta(seconds=gap_s * n), mid) for n, mid in enumerate(mids)]
            watch = reward_farmer.seeded_watch(samples, NOW)
            return samples, watch, reward_farmer.steady_mid(watch, NOW)

        # Twelve steady minutes: watched since the first sample, so steady at once.
        samples, watch, mid = seeded([0.948] * 12)
        self.assertEqual(watch.since, samples[0][0])
        self.assertEqual(mid, 0.948)
        # One dip five samples ago is the lowest midpoint of the dwell.
        self.assertEqual(seeded([0.948] * 7 + [0.9455] + [0.948] * 4)[2], 0.9455)
        # A dip over ten minutes ago no longer counts.
        self.assertEqual(seeded([0.9455] + [0.948] * 11)[2], 0.948)
        # A one-sided book now, or no samples: no evidence.
        self.assertIsNone(seeded([0.948] * 11 + [None])[1])
        self.assertIsNone(reward_farmer.seeded_watch([], NOW))
        # A one-sided book five samples ago: watched only since the sample after.
        samples, watch, mid = seeded([0.948] * 7 + [None] + [0.948] * 4)
        self.assertEqual(watch.since, samples[8][0])
        self.assertIsNone(mid)
        # A last sample over three minutes old vouches for nothing.
        self.assertIsNone(seeded([0.948] * 12, last_ago_s=200)[1])
        # A gap over three minutes between samples starts the watch again.
        gap = NOW - timedelta(seconds=900), NOW - timedelta(seconds=600)
        samples = [(gap[0], 0.948), (gap[1], 0.948), (NOW - timedelta(seconds=30), 0.948)]
        watch = reward_farmer.seeded_watch(samples, NOW)
        self.assertEqual(watch.since, samples[2][0])
        self.assertIsNone(reward_farmer.steady_mid(watch, NOW))
