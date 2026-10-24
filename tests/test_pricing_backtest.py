import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from analysis import pricing_backtest as pb
from analysis.exit_backtest import Mids
from farm import fill_cost
from farm.exit_value import Piece, Sweep
from farm.farm_books import BookSample
from farm.fill_cost import DAY_S, DEFAULT_PRIOR, Hit
from farm.reward_farmer import (FarmConfig, FarmMarket, Position, Slot, target_orders,
                                with_book_mid)
from farm.reward_screener import Outlook, QuoteValue, Zones
from polymarket.books import Levels

T0 = 1_792_000_000.0
FLOOR = 0.90
CFG = FarmConfig(capital=500.0, max_lead=0.005, ask_offset=0.01, min_capture=0.005,
                 ev_margin=0.01, min_fav=FLOOR, min_low_score=0.10, keep_ratio=0.8,
                 drop_threshold=0.03, drop_window_s=60.0, cooldown_s=600.0,
                 naked_cooldown_s=3600.0, expiry_buffer_days=7.0, hurdle_rate=0.30,
                 cash_buffer=0.0)
END = datetime.fromtimestamp(T0, timezone.utc) + timedelta(days=100)
FM = FarmMarket(condition_id="c1", slug="m", question="?", end_date=END, neg_risk=False,
                tick=0.001, fav_token_id="fav", fav_outcome="No", rate_per_day=100.0, v=0.035,
                min_size=50.0, long_token_id="", p_true=0.995, naked_budget_usd=25.0,
                expires_at=END, capital=500.0, zones=Zones(100 / 365.25, 0.96))
BOOK = Levels(bids=[(0.958, 300.0), (0.955, 2000.0), (0.95, 3000.0), (0.94, 5000.0)],
              asks=[(0.962, 400.0), (0.965, 2000.0), (0.97, 3000.0)], tick=0.001)


def sweep(at, *pieces):
    return Sweep(at, tuple(Piece(p, s) for p, s in pieces))


def market(sells, mids):
    mid = Mids(mids)
    hits = [Hit(s.at, min(p.price for p in s.pieces)) for s in sells]
    return pb.Market("c1", FM, False, T0 - 30 * DAY_S, sells, hits,
                     pb.market_bursts(sells, mid), mids, mid, pb.hit_mids(hits, mids))


def outlook(curve):
    return Outlook(FM.rate_per_day, FM.zones.years_left, curve, CFG.hurdle_rate)


class ScoreTest(unittest.TestCase):
    H = DAY_S
    CHOICE = pb.Choice(0.95, 300.0, 0.96, 2.4)
    BURSTS = [pb.Burst(T0 - 10, (Piece(0.90, 500.0),), 500.0, 0.96, T0 - 10),
              pb.Burst(T0 + 100, (Piece(0.95, 100.0), Piece(0.94, 150.0)), 250.0, 0.96, T0 + 100),
              pb.Burst(T0 + 900, (Piece(0.93, 200.0),), 200.0, 0.96, T0 + 900),
              pb.Burst(T0 + 1800, (Piece(0.90, 500.0),), 500.0, 0.96, T0 + 1800)]
    MID = Mids([(T0, 0.96), (T0 + 100 + DAY_S, 0.96), (T0 + 900 + DAY_S, 0.94)])

    def test_bursts_in_the_interval_fill_what_they_sold_under_b_up_to_the_size_left(self):
        s = pb.score(self.CHOICE, self.BURSTS, T0, T0 + 1800, T0 + 10 * DAY_S, self.MID, 0.30,
                     self.H)
        carry = 0.95 * math.log(1.3) / 365
        self.assertAlmostEqual(s.reward_usd, 2.4 * 1800 / DAY_S)
        self.assertAlmostEqual(s.fill_shares, 300.0)
        self.assertAlmostEqual(s.fill_pnl,
                               150 * (0.96 - 0.95 - carry) + 150 * (0.94 - 0.95 - carry))
        self.assertEqual(s.dropped_fills, 0)
        self.assertAlmostEqual(s.net, s.reward_usd + s.fill_pnl)

    def test_a_fill_with_no_mid_a_horizon_later_is_counted_and_left_out(self):
        s = pb.score(self.CHOICE, self.BURSTS, T0, T0 + 1800, T0 + 500 + DAY_S, self.MID, 0.30,
                     self.H)
        self.assertAlmostEqual(s.fill_shares, 300.0)
        self.assertAlmostEqual(s.fill_pnl, 150 * (0.96 - 0.95 - 0.95 * math.log(1.3) / 365))
        self.assertEqual(s.dropped_fills, 1)

    def test_held_fills_carry_the_expected_hold_beyond_the_horizon(self):
        hold = 40 * DAY_S
        s = pb.score(self.CHOICE, self.BURSTS, T0, T0 + 1800, T0 + 500 + DAY_S, self.MID, 0.30,
                     self.H, hold)
        self.assertAlmostEqual(s.hold_cost, 150 * 0.95 * math.log(1.3) * 39 / 365)

    def test_the_expected_hold_is_cut_short_at_resolution(self):
        rate = fill_cost.sales_per_day(500.0)
        self.assertAlmostEqual(pb.expected_hold_s(500.0, 10.0), DAY_S / rate, delta=1.0)
        self.assertAlmostEqual(pb.expected_hold_s(500.0, 2 / 365.25), 2 * DAY_S, delta=0.05 * DAY_S)

    def test_no_bid_earns_its_plan_reward_alone(self):
        s = pb.score(pb.Choice(None, 0.0, None, 1.2), self.BURSTS, T0, T0 + 1800, math.inf,
                     self.MID, 0.30, self.H)
        self.assertEqual(s, pb.Scored(1.2 * 1800 / DAY_S, 0.0, 0.0, 0))


class DepthTest(unittest.TestCase):
    def test_fills_walk_the_book_ahead_shrunk_toward_the_prior(self):
        own = pb.Swept.of([1000.0, 50.0, 300.0], 2.0)
        prior = pb.Swept.of([500.0], 10.0)
        levels = Levels(bids=[(0.96, 100.0), (0.95, 150.0), (0.94, 400.0)], asks=[])
        # A(0.95) = 250: the bursts fill 0, 50 and 200 of 200; the prior 200 in 10 days.
        fills = pb.depth_fills(own, prior, levels)
        self.assertAlmostEqual(fills(0.95, 200.0, 10.0), (250.0 + 10.0 * 20.0) / (2.0 + 10.0))
        self.assertAlmostEqual(fills(0.95, 200.0, 0.0), 250.0 / 2.0)

    def test_swept_sums_match_the_bursts_one_by_one(self):
        rng = random.Random(1)
        sizes = [rng.expovariate(1 / 400) for _ in range(200)]
        swept = pb.Swept.of(sizes, 5.0)
        for A, Q in [(0.0, 100.0), (250.0, 300.0), (1e4, 50.0), (37.5, 1e6)]:
            self.assertAlmostEqual(swept.filled(A, Q),
                                   sum(min(Q, max(0.0, x - A)) for x in sizes))


class LeakageTest(unittest.TestCase):
    T = T0 + 5 * DAY_S
    PAST = [sweep(T0 + DAY_S, (0.93, 400.0)), sweep(T0 + 2 * DAY_S, (0.95, 50.0), (0.94, 30.0)),
            sweep(T - 40, (0.95, 100.0))]
    MIDS = [(T0 - 3 * DAY_S + 60 * k, 0.96 - 0.00001 * (k % 7)) for k in range(0, 15000, 3)]

    def known(self, sells):
        m = market(sells, self.MIDS)
        history = pb.history_as_of(m, self.T, FLOOR)
        return history, pb.own_swept(m, self.T, history, FLOOR)

    def test_a_decision_never_sees_a_sell_at_or_after_its_time(self):
        before = self.known(self.PAST)
        # One sell at t, and one 30 s later that chains into the burst begun 40 s before t.
        after = self.known(self.PAST + [sweep(self.T, (0.80, 9000.0)),
                                        sweep(self.T + 30, (0.85, 7000.0))])
        self.assertEqual(before, after)
        self.assertEqual(sorted(before[1].sizes), [80.0, 100.0, 400.0])

    def test_history_counts_the_same_from_the_mids_its_hits_read(self):
        m = market(self.PAST, self.MIDS)
        hits = [h for h in m.hits if h.at < self.T]
        full = fill_cost.make_history(hits, [p for p in self.MIDS if p[0] < self.T], m.start,
                                      pb._utc(self.T), FLOOR)
        got = pb.history_as_of(m, self.T, FLOOR)
        self.assertEqual((got.events, got.days), (full.events, full.days))


class ArmsTest(unittest.TestCase):
    KNOWN = pb.Known({"24h": 0.002, "72h": 0.003}, 0.30, lambda b, Q, s: 0.0)

    @staticmethod
    def candidate(b, Q, reward, fills, loss):
        return pb.Candidate(None, b, Q, 0.96, QuoteValue(b, Q, b * Q, reward, fills, loss))

    def test_with_no_fills_every_arm_picks_the_same_bid_and_reward(self):
        found = [self.candidate(0.955, 500.0, 2.0, 0.0, 0.01),
                 self.candidate(0.95, 500.0, 2.5, 0.0, 0.01),
                 self.candidate(0.95, 250.0, 1.5, 0.0, 0.01)]
        picks = {pb.choose(found, 0.5, arm, self.KNOWN)
                 for arms in pb.ARMS.values() for arm in arms}
        self.assertEqual(picks, {pb.Choice(0.95, 500.0, 0.96, 2.5)})

    def test_arms_differ_only_in_how_fills_are_valued(self):
        # The nearer bid earns more reward and fills often; today's rule
        # charges no fill that looks profitable at p_true.
        near = self.candidate(0.958, 500.0, 3.0, 2000.0, -0.03)
        far = self.candidate(0.945, 500.0, 2.0, 10.0, -0.04)
        current, priced = pb.ARMS["24h"][0], pb.ARMS["24h"][1]
        # near: e(b) = 0.2c - phi 0.2c - carry, so 2,000 fills a day cost more than its extra $1.
        self.assertEqual(pb.choose([near, far], 0.0, current, self.KNOWN),
                         pb.Choice(0.958, 500.0, 0.96, 3.0, 2000.0))
        self.assertEqual(pb.choose([near, far], 0.0, priced, self.KNOWN),
                         pb.Choice(0.945, 500.0, 0.96, 2.0, 10.0))
        for arm in pb.ARMS["24h"]:
            choice = pb.choose([near, far], 0.0, arm, self.KNOWN)
            self.assertEqual(choice.reward_per_day, 3.0 if choice.b == 0.958 else 2.0)

    def test_the_depth_arm_forecasts_its_own_fills_and_values_them_signed(self):
        near = self.candidate(0.958, 500.0, 3.0, 0.0, 0.01)
        far = self.candidate(0.945, 500.0, 2.0, 0.0, 0.01)
        known = self.KNOWN._replace(depth_fills=lambda b, Q, s: 2000.0 if b > 0.95 else 10.0)
        self.assertEqual(pb.choose([near, far], 0.0, pb.ARMS["24h"][2], known),
                         pb.Choice(0.945, 500.0, 0.96, 2.0, 10.0))
        self.assertEqual(pb.choose([near, far], 0.0, pb.ARMS["24h"][1], known).b, 0.958)

    def test_no_bid_unless_its_net_beats_the_plan_without_one(self):
        found = [self.candidate(0.95, 500.0, 1.0, 0.0, 0.01)]
        self.assertEqual(pb.choose(found, 1.0, pb.ARMS["24h"][0], self.KNOWN),
                         pb.Choice(None, 0.0, None, 1.0))

    def test_the_current_arm_picks_what_target_orders_picks(self):
        for curve in (fill_cost.fill_curve(None, DEFAULT_PRIOR, FLOOR),
                      fill_cost.FillCurve(tuple(3.0 / (1 + 50 * d) for d in fill_cost.DISTANCES),
                                          True, 0.0)):
            for fm in (FM, FM._replace(p_true=0.97), FM._replace(capital=5000.0)):
                with self.subTest(curve=curve.rates[0], fm=(fm.p_true, fm.capital)):
                    base_reward, found = pb.candidates(fm, BOOK, CFG, outlook(curve))
                    pick = pb.choose(found, base_reward, pb.ARMS["24h"][0], self.KNOWN)
                    planned = target_orders(fm, BOOK, Position(), CFG, True, outlook(curve),
                                            bid_without_history=True)
                    bid = next(((i.price, i.size) for i in planned.intents
                                if i.slot is Slot.FAV_BID), None)
                    self.assertEqual(bid, None if pick.b is None else (pick.b, pick.Q))


    def test_the_follow_market_arm_picks_what_the_farmer_plans_with_no_p_true(self):
        # BOOK's midpoint is 96.0c. Followed, the market bids at most that,
        # a tick under the 96.1c the 99.5c view less its 1c margin allows.
        follow_fm, cfg = pb.terms(FM, CFG, BOOK)[pb.FOLLOW]
        self.assertEqual((follow_fm.p_true, follow_fm.book_mid, cfg), (None, 0.96, CFG))
        farmer_fm = with_book_mid(FM._replace(p_true=None), BOOK)
        for curve in (fill_cost.fill_curve(None, DEFAULT_PRIOR, FLOOR),
                      fill_cost.FillCurve(tuple(3.0 / (1 + 50 * d) for d in fill_cost.DISTANCES),
                                          True, 0.0)):
            with self.subTest(curve=curve.rates[0]):
                base_reward, found = pb.candidates(follow_fm, BOOK, cfg, outlook(curve))
                self.assertEqual(max(c.b for c in found), 0.960)
                self.assertEqual(max(c.b for c in pb.candidates(FM, BOOK, CFG, outlook(curve))[1]),
                                 0.961)
                pick = pb.choose(found, base_reward, pb.ARMS["24h"][0], self.KNOWN)
                planned = target_orders(farmer_fm, BOOK, Position(), CFG, True, outlook(curve),
                                        bid_without_history=True)
                bid = next(((i.price, i.size) for i in planned.intents
                            if i.slot is Slot.FAV_BID), None)
                self.assertIsNotNone(bid)
                self.assertEqual(bid, (pick.b, pick.Q))

    def test_a_decision_is_grouped_by_its_view_against_the_mid(self):
        self.assertEqual([pb.view_group(p, 0.96) for p in (0.966, 0.965, 0.955, 0.954)],
                         ["view over mid", "view at mid", "view at mid", "view under mid"])


class BookTest(unittest.TestCase):
    def test_our_orders_leave_the_book_where_they_show_in_it(self):
        sample = BookSample("c1", datetime.fromtimestamp(T0, timezone.utc), 0.96,
                            ((0.958, 300.0), (0.955, 2000.0)), ((0.962, 400.0), (0.97, 3000.0)),
                            (("FAV_BID", 0.955, 500.0), ("LONG_ASK", 0.042, 100.0),
                             ("LONG_BID", 0.03, 1000.0), ("FAV_ASK", 0.962, 400.0)))
        book = pb.others_book(sample, 0.001)
        self.assertEqual(book.bids, [(0.958, 200.0), (0.955, 1500.0)])
        self.assertEqual(book.asks, [(0.97, 2000.0)])


class DecisionsTest(unittest.TestCase):
    def test_a_decision_each_half_hour_held_to_the_next_or_the_run_end(self):
        at = [T0 + 300 * k for k in range(8)] + [T0 + 5000 + 300 * k for k in range(2)]
        samples = [BookSample("c1", datetime.fromtimestamp(t, timezone.utc), 0.96, (), (), ())
                   for t in at]
        got = [(d.t - T0, d.end - T0) for d in pb.decisions(samples)]
        self.assertEqual(got, [(0, 1800), (1800, 2400), (5000, 5600)])


if __name__ == "__main__":
    unittest.main()
