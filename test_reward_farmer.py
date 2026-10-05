import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

import farm_db
import reward_farmer
from farm_db import CommandKind
from reward_farmer import (
    Baseline,
    Cancel,
    FarmConfig,
    FarmMarket,
    LiveOrder,
    MarketInfo,
    Observation,
    Phase,
    Place,
    Position,
    Slot,
    State,
    Farmer,
    build_parser,
    config_from,
    drop_ask,
    farm_position,
    fill_events,
    keep_failure,
    market_outlook,
    naked_allowed,
    next_backoff,
    quotes_at,
    ratchet_baseline,
    reconcile,
    rewards_active,
    step_state,
    strip_own,
    target_orders,
    without_backed_off,
)
import fill_cost
from fill_cost import DISTANCES, FillCurve, History
from reward_screener import Levels
from test_reward_screener import MISTRAL_BOOK, MISTRAL_BOOK_BID_GONE

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
FAV, LONG = "fav-token", "long-token"

CFG = FarmConfig(
    capital=500.0,
    max_lead=0.005,
    ask_offset=0.01,
    min_capture=0.005,
    ev_margin=0.01,
    min_fav=0.90,
    min_low_score=0.10,
    keep_ratio=0.8,
    drop_threshold=0.03,
    drop_window_s=60.0,
    cooldown_s=600.0,
    naked_cooldown_s=3600.0,
    expiry_buffer_days=7.0,
    hurdle_rate=0.40,
    stuck_share=0.25,
)


def market(**changes):
    fields = dict(
        condition_id="0xc0nd",
        slug="test-market",
        question="Test?",
        end_date=NOW + timedelta(days=100),
        neg_risk=False,
        tick=0.001,
        fav_token_id=FAV,
        fav_outcome="No",
        rate_per_day=100.0,
        v=0.035,
        min_size=50.0,
        long_token_id=LONG,
        p_true=0.995,
        naked_budget_usd=25.0,
        expires_at=NOW + timedelta(days=93),
        capital=500.0,
    )
    fields.update(changes)
    return FarmMarket(**fields)


# Favorite mid 0.99: the cheapest scoring longshot bid is 0.1c (FLOOR).
FLOOR_BOOK = Levels(bids=[(0.985, 1000.0)], asks=[(0.995, 1000.0)], tick=0.001)
# Favorite mid 0.96: no longshot bid under 0.6c scores and the cheapest scoring
# 0.10 is 1.7c (BUDGET). Balancing the full favorite bid there costs $35.83.
BUDGET_BOOK = Levels(bids=[(0.95, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)

# The favorite bid sits 1.1c under the midpoint in both books, the deepest
# price scoring at least 0.45 with a 3.5c max spread: (2.4 / 3.5)^2.
SCORE_FAV = (0.024 / 0.035) ** 2
# Longshot bid at 0.1c against a 1c longshot mid: (2.6 / 3.5)^2.
SCORE_LOW_FLOOR = (0.026 / 0.035) ** 2
# Longshot bid at 1.7c against a 4c longshot mid: (1.2 / 3.5)^2.
SCORE_LOW_BUDGET = (0.012 / 0.035) ** 2


# The Mistral market on its approved terms.
MISTRAL = market(v=0.085, min_size=20.0, p_true=0.96, naked_budget_usd=10.0)
MISTRAL_CFG = CFG._replace(ev_margin=0.02)


def by_slot(target):
    return {i.slot: i for i in target.intents}


def q_sums(target):
    q_one = sum(i.score * i.size for i in target.intents if i.slot in (Slot.FAV_BID, Slot.LONG_ASK))
    q_two = sum(i.score * i.size for i in target.intents if i.slot in (Slot.FAV_ASK, Slot.LONG_BID))
    return q_one, q_two


def curve(*rates):
    """Fills drawn from the market's history: these rates from the first of
    DISTANCES, the last one held to the end. A filled share never exits."""
    return FillCurve(rates + rates[-1:] * (len(DISTANCES) - len(rates)), True, 0.0)


# A quiet market: a sweep reaches a bid at the midpoint about once a month,
# and one 3c under about once a quarter.
QUIET = curve(0.033, 0.03, 0.027, 0.024, 0.021, 0.018, 0.015, 0.011)
# A sweep reaches 1c under the midpoint every day, and never 1.5c.
SWEPT = curve(1.0, 1.0, 1.0, 1.0, 1.0, 0.0)
# A scan's fill prior, and a market history with no sweeps in 60 days.
PRIOR = fill_cost.DEFAULT_PRIOR._replace(markets=40, at=NOW)
NO_SWEEPS = History((0,) * len(DISTANCES), 60.0, NOW, 0.90, 0, 0.0)


def plan(fm, book, position=Position(), cfg=CFG, naked=True, fills=QUIET):
    """target_orders against a book, valued at the scan's rate with these fills."""
    outlook = market_outlook(fm, None, None, PRIOR, cfg, NOW)._replace(fills=fills)
    return target_orders(fm, book, position, cfg, naked, outlook)


class BalancingTest(unittest.TestCase):
    """The orders around a favorite bid at a given price."""

    def test_flat_floor_market_balances_fav_bid_with_floor_long_bid(self):
        target = quotes_at(market(), FLOOR_BOOK, Position(), CFG, True, 0.979)
        intents = by_slot(target)
        self.assertEqual(set(intents), {Slot.FAV_BID, Slot.LONG_BID})
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual(fav.price, 0.979)
        self.assertEqual(fav.size, 510.72)
        self.assertEqual((fav.token_id, fav.side), (FAV, "BUY"))
        self.assertAlmostEqual(fav.score, SCORE_FAV)
        self.assertEqual(low.price, 0.001)
        self.assertEqual((low.token_id, low.side), (LONG, "BUY"))
        self.assertAlmostEqual(low.score, SCORE_LOW_FLOOR)
        q_one, q_two = q_sums(target)
        self.assertGreaterEqual(q_two, q_one)
        self.assertLess(q_two - q_one, low.score * 0.01 + 1e-9)

    def test_fav_inventory_covers_q_two_and_shrinks_long_bid(self):
        flat = by_slot(quotes_at(market(), FLOOR_BOOK, Position(), CFG, True, 0.979))
        held = Position(fav_shares=100.0, fav_avg=0.97)
        target = quotes_at(market(), FLOOR_BOOK, held, CFG, True, 0.979)
        intents = by_slot(target)
        ask = intents[Slot.FAV_ASK]
        self.assertEqual((ask.token_id, ask.side, ask.price, ask.size), (FAV, "SELL", 0.999, 100.0))
        self.assertAlmostEqual(ask.score, SCORE_LOW_FLOOR)
        # Inventory at cost uses part of the capital: (500 - 97) / 0.979.
        self.assertEqual(intents[Slot.FAV_BID].size, 411.64)
        self.assertLess(intents[Slot.LONG_BID].size, flat[Slot.LONG_BID].size)
        q_one, q_two = q_sums(target)
        self.assertGreaterEqual(q_two, q_one)
        expected_long = (q_one - ask.score * ask.size) / SCORE_LOW_FLOOR
        self.assertAlmostEqual(intents[Slot.LONG_BID].size, expected_long, delta=0.01)

    def test_fav_inventory_covering_all_of_q_one_needs_no_long_bid(self):
        held = Position(fav_shares=300.0, fav_avg=0.97)
        intents = by_slot(quotes_at(market(), FLOOR_BOOK, held, CFG, True, 0.979))
        self.assertIn(Slot.FAV_ASK, intents)
        self.assertIn(Slot.FAV_BID, intents)
        self.assertNotIn(Slot.LONG_BID, intents)

    def test_binding_budget_caps_long_bid_and_trims_fav_bid(self):
        fm = market(p_true=0.98, naked_budget_usd=25.0)
        target = quotes_at(fm, BUDGET_BOOK, Position(), CFG, True, 0.949)
        intents = by_slot(target)
        low, fav = intents[Slot.LONG_BID], intents[Slot.FAV_BID]
        self.assertEqual(low.price, 0.017)
        self.assertAlmostEqual(low.score, SCORE_LOW_BUDGET)
        self.assertLessEqual(low.price * low.size, 25.0)
        self.assertEqual(low.size, 1470.58)
        # Untrimmed the bid would be 500 / 0.949 = 526.87 shares.
        self.assertEqual(fav.price, 0.949)
        expected_fav = low.size * SCORE_LOW_BUDGET / SCORE_FAV
        self.assertAlmostEqual(fav.size, expected_fav, delta=0.01)
        q_one, q_two = q_sums(target)
        self.assertLessEqual(q_one, q_two + 1e-9)

    def test_short_budget_never_lifts_long_bid_above_the_min_score_price(self):
        # From $10.24 up, a price between 2.1c and 3.9c would balance the full
        # favorite bid within the budget. The bid stays at 1.7c, the cheapest
        # price scoring 0.10, and the favorite bid shrinks instead.
        for budget in (5.0, 10.0, 15.0, 25.0, 35.0):
            with self.subTest(budget=budget):
                fm = market(p_true=0.98, naked_budget_usd=budget)
                intents = by_slot(quotes_at(fm, BUDGET_BOOK, Position(), CFG, True, 0.949))
                self.assertEqual(intents[Slot.LONG_BID].price, 0.017)
                self.assertLess(intents[Slot.FAV_BID].size, 526.87)

    def test_budget_counts_longshot_shares_already_held(self):
        fm = market(p_true=0.98, naked_budget_usd=25.0)
        held = Position(long_shares=10.0, long_avg=1.0)
        low = by_slot(quotes_at(fm, BUDGET_BOOK, held, CFG, True, 0.949))[Slot.LONG_BID]
        self.assertLessEqual(low.price * low.size + 10.0, 25.0)
        self.assertEqual(low.size, 882.35)

    def test_floor_bid_covers_the_fav_bid_after_the_big_bid_leaves(self):
        # Mistral's favorite bid 0.855 x 584.79 scores 0.0271, 15.86 of Q_one. At 0.1c
        # the longshot bid scores 0.0199 and covers it with 795.97 shares for $0.80.
        # The cheapest price scoring 0.10 is 1.6c, first in line for a No buyer.
        target = quotes_at(MISTRAL, MISTRAL_BOOK_BID_GONE, Position(), MISTRAL_CFG, True, 0.855)
        intents = by_slot(target)
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual((fav.price, fav.size), (0.855, 584.79))
        self.assertEqual((low.price, low.size), (0.001, 795.97))
        self.assertLessEqual(low.price * low.size, MISTRAL.naked_budget_usd)
        q_one, q_two = q_sums(target)
        self.assertGreaterEqual(q_two, q_one)

    def test_floor_bid_scoring_over_min_low_score_stays_at_the_floor(self):
        target = quotes_at(MISTRAL, MISTRAL_BOOK, Position(), MISTRAL_CFG, True, 0.935)
        low = by_slot(target)[Slot.LONG_BID]
        self.assertEqual(low.price, 0.001)
        self.assertGreaterEqual(low.score, MISTRAL_CFG.min_low_score)

    def test_longshot_inventory_produces_long_ask_at_fav_bid_mirror(self):
        held = Position(long_shares=200.0, long_avg=0.005)
        intents = by_slot(quotes_at(market(), FLOOR_BOOK, held, CFG, True, 0.979))
        ask = intents[Slot.LONG_ASK]
        self.assertEqual((ask.token_id, ask.side, ask.price, ask.size), (LONG, "SELL", 0.021, 200.0))
        self.assertAlmostEqual(ask.score, intents[Slot.FAV_BID].score)

    def test_inventory_below_min_size_gets_no_exit(self):
        held = Position(fav_shares=20.0, fav_avg=0.97, long_shares=20.0, long_avg=0.01)
        intents = by_slot(quotes_at(market(), FLOOR_BOOK, held, CFG, True, 0.979))
        self.assertNotIn(Slot.FAV_ASK, intents)
        self.assertNotIn(Slot.LONG_ASK, intents)


class TargetOrdersTest(unittest.TestCase):
    def test_p_true_caps_fav_bid_price(self):
        fm = market(p_true=0.985)
        fav = by_slot(plan(fm, FLOOR_BOOK))[Slot.FAV_BID]
        self.assertEqual(fav.price, 0.975)
        self.assertAlmostEqual(fav.score, (0.020 / 0.035) ** 2)

    def test_p_true_cap_outside_band_drops_fav_bid(self):
        target = plan(market(p_true=0.96), FLOOR_BOOK)
        self.assertEqual((target.intents, target.fav.reason), ([], "no scoring favorite bid"))

    def test_naked_cooldown_gives_no_long_bid_and_no_unmatched_fav_bid(self):
        self.assertEqual(plan(market(), FLOOR_BOOK, naked=False).intents, [])

    def test_naked_cooldown_keeps_covered_q_two(self):
        held = Position(fav_shares=100.0, fav_avg=0.97)
        intents = by_slot(plan(market(), FLOOR_BOOK, held, naked=False))
        self.assertNotIn(Slot.LONG_BID, intents)
        self.assertIn(Slot.FAV_ASK, intents)
        fav, ask = intents[Slot.FAV_BID], intents[Slot.FAV_ASK]
        self.assertLessEqual(fav.score * fav.size, ask.score * ask.size + 1e-9)

    def test_one_sided_book_plans_nothing(self):
        book = Levels(bids=[(0.985, 1000.0)], asks=[], tick=0.001)
        target = plan(market(), book)
        self.assertEqual((target.mid, target.intents), (None, []))


class NetPricingTest(unittest.TestCase):
    def outlook(self, fm, history):
        return market_outlook(fm, None, history, PRIOR, CFG, NOW)

    def test_no_history_places_no_favorite_bid_but_keeps_the_exits(self):
        fm, held = market(), Position(long_shares=200.0, long_avg=0.005)
        target = target_orders(fm, FLOOR_BOOK, held, CFG, True, self.outlook(fm, None))
        intents = by_slot(target)
        self.assertNotIn(Slot.FAV_BID, intents)
        self.assertEqual(target.fav.reason, "no fill history yet")
        # With no favorite bid the exit mirrors the highest bid allowed, 99.0c.
        self.assertEqual(intents[Slot.LONG_ASK].price, 0.010)

    def test_a_history_of_no_days_places_no_favorite_bid(self):
        # A read whose mids came back empty saw nothing of the market.
        fm = market()
        read = target_orders(fm, FLOOR_BOOK, Position(), CFG, True,
                             self.outlook(fm, NO_SWEEPS))
        self.assertIn(Slot.FAV_BID, by_slot(read))
        empty = target_orders(fm, FLOOR_BOOK, Position(), CFG, True,
                              self.outlook(fm, NO_SWEEPS._replace(days=0.0)))
        self.assertNotIn(Slot.FAV_BID, by_slot(empty))
        self.assertEqual(empty.fav.reason, "no fill history yet")

    def test_a_preview_without_a_history_prices_on_the_prior(self):
        fm = market()
        target = target_orders(fm, FLOOR_BOOK, Position(), CFG, True, self.outlook(fm, None),
                               bid_without_history=True)
        self.assertIn(Slot.FAV_BID, by_slot(target))

    def test_a_quiet_market_bids_the_highest_price_allowed(self):
        # p_true 0.995 less the 1c margin caps the 99.0c ceiling at 98.5c.
        target = plan(market(), FLOOR_BOOK)
        self.assertEqual(by_slot(target)[Slot.FAV_BID].price, 0.985)
        self.assertEqual(target.fav.best.price, 0.985)

    def test_sweeps_through_the_top_of_the_book_push_the_bid_below_where_they_reach(self):
        # The 98.5c cap sits 0.5c under the 99.0c midpoint, where a sweep fills
        # the bid daily; it steps down to 1.5c under or more, which none reach.
        fm = market()
        target = plan(fm, FLOOR_BOOK, fills=SWEPT)
        fav = by_slot(target)[Slot.FAV_BID]
        self.assertLessEqual(fav.price, 0.975)
        self.assertEqual(target.fav.best.fills_per_day, 0.0)
        balanced = by_slot(quotes_at(fm, FLOOR_BOOK, Position(), CFG, True, fav.price))
        self.assertEqual(fav.size, balanced[Slot.FAV_BID].size)

    def test_fills_costing_more_than_the_bid_earns_place_none_but_keep_the_exit(self):
        # A sweep through every bid each day at a 1,000% hurdle: each fill
        # loses most of its price.
        costly = CFG._replace(hurdle_rate=10.0)
        held = Position(fav_shares=100.0, fav_avg=0.97)
        target = plan(market(), FLOOR_BOOK, held, cfg=costly, fills=curve(1.0))
        intents = by_slot(target)
        self.assertNotIn(Slot.FAV_BID, intents)
        self.assertIn(Slot.FAV_ASK, intents)
        self.assertRegex(target.fav.reason,
                         r"^no favorite bid pays: best net -\$\d+\.\d\d/day at \d+\.\dc$")

    def test_a_short_dated_market_welcomes_fills_up_to_the_ev_cap(self):
        # Two days out a 98.5c fill of a 99.5c share gains 0.8c: the sweeps
        # that push a long-dated bid down only add here.
        fm = market(end_date=NOW + timedelta(days=2))
        target = plan(fm, FLOOR_BOOK, fills=SWEPT)
        self.assertLess(target.fav.best.loss_per_share, 0)
        self.assertEqual(by_slot(target)[Slot.FAV_BID].price, 0.985)

    def test_the_outlook_takes_the_live_rate_the_end_date_and_the_fill_curve(self):
        fm = market(rate_per_day=100.0, end_date=NOW + timedelta(days=365.25))
        outlook = market_outlook(fm, {"0xc0nd": 40.0}, NO_SWEEPS, PRIOR, CFG, NOW)
        self.assertEqual((outlook.rate_per_day, outlook.years_left, outlook.hurdle_rate,
                          outlook.stuck_share),
                         (40.0, 1.0, CFG.hurdle_rate, CFG.stuck_share))
        self.assertEqual(outlook.fills, fill_cost.fill_curve(NO_SWEEPS, PRIOR, CFG.min_fav))
        self.assertTrue(outlook.fills.from_history)
        unread = market_outlook(fm, {"0xother": 40.0}, None, PRIOR, CFG, NOW)
        self.assertEqual(unread.rate_per_day, 100.0)
        self.assertEqual(unread.fills, fill_cost.fill_curve(None, PRIOR, CFG.min_fav))


def live(order_id, slot, price, size):
    token = FAV if slot in (Slot.FAV_BID, Slot.FAV_ASK) else LONG
    return LiveOrder(order_id, slot, token, slot.side, price, size)


class ReconcileTest(unittest.TestCase):
    """On FLOOR_BOOK in a quiet market: FAV_BID 98.5c x 507.61, LONG_BID 0.1c x 675.82."""

    def setUp(self):
        self.fm = market()
        self.target = plan(self.fm, FLOOR_BOOK)
        self.intents = by_slot(self.target)

    def run_reconcile(self, orders):
        return reconcile(self.target, orders, self.fm, Position(), CFG)

    def keep(self, price, target=None):
        target = target or self.target
        order = live("a", Slot.FAV_BID, price, self.intents[Slot.FAV_BID].size)
        return keep_failure(order, self.intents[Slot.FAV_BID], target, self.fm, Position(), CFG)

    def test_keeps_orders_within_hysteresis(self):
        orders = [
            live("a", Slot.FAV_BID, 0.984, 450.0),
            live("b", Slot.LONG_BID, 0.001, 600.0),
        ]
        self.assertEqual(self.run_reconcile(orders), [])

    def test_a_deeper_bid_earning_most_of_the_best_net_stays(self):
        # 98.1c scores (2.6 / 3.5)^2 = 0.55, under 0.8 of the target's 0.73,
        # but nets within a fifth of the best price's.
        self.assertLess(0.552, CFG.keep_ratio * self.intents[Slot.FAV_BID].score)
        self.assertIsNone(self.keep(0.981))

    def test_a_bid_trailing_the_best_net_by_more_than_the_keep_ratio_is_replaced(self):
        curve = self.target.fav.curve
        self.assertLess(curve[0.979].net_per_day, 0.8 * curve[0.985].net_per_day)
        self.assertEqual(self.keep(0.979), "net below keep ratio")

    def test_a_bid_netting_no_more_than_the_plan_without_it_is_replaced(self):
        no_better = self.target.fav._replace(
            baseline_per_day=self.target.fav.curve[0.981].net_per_day)
        self.assertEqual(self.keep(0.981, self.target._replace(fav=no_better)),
                         "net below keep ratio")

    def test_a_bid_above_the_candidates_is_replaced(self):
        self.assertEqual(self.keep(0.99), "outside the bid candidates")

    def test_places_every_target_when_nothing_rests(self):
        actions = self.run_reconcile([])
        self.assertEqual(actions, [Place(i) for i in self.target.intents])

    def test_replaces_order_out_of_band(self):
        orders = [
            live("a", Slot.FAV_BID, 0.95, 507.61),
            live("b", Slot.LONG_BID, 0.001, 675.82),
        ]
        actions = self.run_reconcile(orders)
        self.assertEqual(actions, [Cancel(("a",)), Place(self.intents[Slot.FAV_BID])])

    def test_replaces_order_whose_size_left_the_band(self):
        orders = [
            live("a", Slot.FAV_BID, 0.985, 300.0),
            live("b", Slot.LONG_BID, 0.001, 675.82),
        ]
        actions = self.run_reconcile(orders)
        self.assertEqual(actions, [Cancel(("a",)), Place(self.intents[Slot.FAV_BID])])

    def test_replaces_fav_bid_above_ev_cap(self):
        fm = market(p_true=0.985)
        target = plan(fm, FLOOR_BOOK)
        orders = [live("a", Slot.FAV_BID, 0.98, 510.2)]
        actions = reconcile(target, orders, fm, Position(), CFG)
        self.assertEqual(actions[0], Cancel(("a",)))

    def test_cancels_extras_and_slots_without_intent(self):
        orders = [
            live("a", Slot.FAV_BID, 0.985, 507.61),
            live("b", Slot.FAV_BID, 0.985, 507.61),
            live("c", Slot.LONG_BID, 0.001, 675.82),
            live("d", Slot.FAV_ASK, 0.999, 100.0),
        ]
        self.assertEqual(self.run_reconcile(orders), [Cancel(("b", "d"))])

    def test_cancels_come_before_places(self):
        orders = [
            live("a", Slot.FAV_BID, 0.95, 507.61),
            live("b", Slot.LONG_BID, 0.050, 675.82),
        ]
        actions = self.run_reconcile(orders)
        self.assertEqual(actions[0], Cancel(("a", "b")))
        self.assertEqual(
            actions[1:],
            [Place(self.intents[Slot.FAV_BID]), Place(self.intents[Slot.LONG_BID])],
        )

    def test_no_intents_cancels_everything(self):
        orders = [live("a", Slot.FAV_BID, 0.985, 507.61), live("b", Slot.LONG_BID, 0.001, 675.82)]
        actions = reconcile(self.target._replace(intents=[]), orders, self.fm, Position(), CFG)
        self.assertEqual(actions, [Cancel(("a", "b"))])


def observe(**changes):
    fields = dict(
        mid=0.96,
        ask=0.97,
        recent_asks=(),
        rewards_active=True,
        accepting_orders=True,
        closed=False,
        approved=True,
        expires_at=NOW + timedelta(days=30),
    )
    fields.update(changes)
    return Observation(**fields)


QUOTING = State(Phase.QUOTING)


def asked(price, seconds_ago=30):
    """recent_asks holding one ask seen seconds_ago."""
    return ((NOW - timedelta(seconds=seconds_ago), price),)


class DropAskTest(unittest.TestCase):
    def test_a_reward_minimum_lot_flickering_on_a_thin_book_does_not_move_it(self):
        # A live book with min_size 40: the 40@0.962 lot came and went each cycle.
        rest = [(0.980, 5.81), (0.992, 10.45), (0.993, 380.0), (0.994, 250.0)]
        self.assertEqual(drop_ask([(0.962, 40.0)] + rest, 40.0), 0.993)
        self.assertEqual(drop_ask(rest, 40.0), 0.993)

    def test_it_is_the_level_where_depth_reaches_three_minimums(self):
        asks = [(0.95, 30.0), (0.96, 50.0), (0.97, 50.0), (0.98, 500.0)]
        self.assertEqual(drop_ask(asks, 40.0), 0.97)

    def test_depth_exactly_three_minimums_counts(self):
        self.assertEqual(drop_ask([(0.95, 60.0), (0.96, 60.0), (0.97, 100.0)], 40.0), 0.96)

    def test_a_book_shallower_than_three_minimums_has_none(self):
        self.assertIsNone(drop_ask([(0.95, 50.0), (0.96, 60.0)], 40.0))


class StepStateTest(unittest.TestCase):
    def test_ask_drop_within_window_starts_cooldown(self):
        obs = observe(ask=0.97, recent_asks=asked(1.0))
        state = step_state(QUOTING, obs, NOW, CFG)
        self.assertEqual(state, State(Phase.COOLDOWN, NOW + timedelta(seconds=600), "ask drop"))

    def test_ask_falling_a_tick_less_than_the_threshold_keeps_quoting(self):
        obs = observe(ask=0.971, recent_asks=asked(1.0))
        self.assertEqual(step_state(QUOTING, obs, NOW, CFG), QUOTING)

    def test_ask_drop_outside_window_is_ignored(self):
        obs = observe(ask=0.97, recent_asks=asked(1.0, seconds_ago=61))
        self.assertEqual(step_state(QUOTING, obs, NOW, CFG), QUOTING)

    def test_mid_falling_with_the_ask_steady_keeps_quoting(self):
        # The Mistral book losing its 0.93 bid: the mid falls 0.9635 -> 0.9235.
        obs = observe(mid=0.9235, ask=0.997, recent_asks=asked(0.997))
        self.assertEqual(step_state(QUOTING, obs, NOW, CFG), QUOTING)

    def test_cooldown_rearms_while_the_ask_keeps_falling(self):
        cooling = State(Phase.COOLDOWN, NOW + timedelta(seconds=10), "ask drop")
        state = step_state(cooling, observe(ask=0.94, recent_asks=asked(0.97)), NOW, CFG)
        self.assertEqual(state, State(Phase.COOLDOWN, NOW + timedelta(seconds=600), "ask drop"))

    def test_cooldown_holds_until_expiry_then_quotes(self):
        cooling = State(Phase.COOLDOWN, NOW + timedelta(seconds=10), "ask drop")
        self.assertEqual(step_state(cooling, observe(), NOW, CFG), cooling)
        later = NOW + timedelta(seconds=11)
        self.assertEqual(step_state(cooling, observe(), later, CFG), QUOTING)

    def test_expiry_retires(self):
        obs = observe(expires_at=NOW)
        self.assertEqual(step_state(QUOTING, obs, NOW, CFG).phase, Phase.RETIRED)

    def test_rewards_ending_pulls_the_quotes_without_retiring(self):
        state = step_state(QUOTING, observe(rewards_active=False), NOW, CFG)
        self.assertEqual(state, State(Phase.UNREWARDED, reason="rewards removed"))

    def test_rewards_returning_go_through_the_normal_checks(self):
        unrewarded = State(Phase.UNREWARDED, reason="rewards removed")
        self.assertEqual(step_state(unrewarded, observe(rewards_active=False), NOW, CFG),
                         unrewarded)
        self.assertEqual(step_state(unrewarded, observe(), NOW, CFG), QUOTING)
        self.assertEqual(step_state(unrewarded, observe(mid=None), NOW, CFG), QUOTING)
        self.assertEqual(step_state(unrewarded, observe(mid=0.89), NOW, CFG),
                         State(Phase.BELOW_RANGE))

    def test_decided_market_retires_with_the_reason(self):
        for signal in ("event closed", "resolution proposed"):
            state = step_state(QUOTING, observe(resolution=signal), NOW, CFG)
            self.assertEqual(state, State(Phase.RETIRED, reason=signal))

    def test_decided_market_retires_even_while_paused(self):
        paused = State(Phase.PAUSED)
        state = step_state(paused, observe(resolution="event closed", paused=True), NOW, CFG)
        self.assertEqual(state.phase, Phase.RETIRED)

    def test_not_approved_or_closed_retires(self):
        for obs in (observe(approved=False), observe(closed=True), observe(accepting_orders=False)):
            self.assertEqual(step_state(QUOTING, obs, NOW, CFG).phase, Phase.RETIRED)

    def test_retired_is_terminal(self):
        retired = State(Phase.RETIRED, reason="market closed")
        self.assertEqual(step_state(retired, observe(), NOW, CFG), retired)

    def test_below_range_then_resume_above_margin(self):
        below = step_state(QUOTING, observe(mid=0.89), NOW, CFG)
        self.assertEqual(below, State(Phase.BELOW_RANGE))
        self.assertEqual(step_state(below, observe(mid=0.903), NOW, CFG), below)
        self.assertEqual(step_state(below, observe(mid=0.906), NOW, CFG), QUOTING)

    def test_pause_then_resume_goes_through_the_normal_checks(self):
        paused = step_state(QUOTING, observe(paused=True), NOW, CFG)
        self.assertEqual(paused.phase, Phase.PAUSED)
        self.assertEqual(step_state(paused, observe(paused=True), NOW, CFG), paused)
        self.assertEqual(step_state(paused, observe(), NOW, CFG), QUOTING)
        self.assertEqual(step_state(paused, observe(mid=0.89), NOW, CFG), State(Phase.BELOW_RANGE))
        self.assertEqual(step_state(paused, observe(mid=None), NOW, CFG), QUOTING)

    def test_pause_overrides_cooldown_and_below_range(self):
        cooling = State(Phase.COOLDOWN, NOW + timedelta(seconds=10), "ask drop")
        for state in (cooling, State(Phase.BELOW_RANGE)):
            self.assertEqual(step_state(state, observe(paused=True), NOW, CFG).phase, Phase.PAUSED)

    def test_retire_beats_rewards_ending_and_pause(self):
        paused = State(Phase.PAUSED, reason="paused by operator")
        unrewarded = State(Phase.UNREWARDED, reason="rewards removed")
        obs = observe(closed=True, paused=True, rewards_active=False)
        for state in (QUOTING, paused, unrewarded):
            self.assertEqual(step_state(state, obs, NOW, CFG),
                             State(Phase.RETIRED, reason="market closed"))

    def test_rewards_ending_beats_pause(self):
        paused = State(Phase.PAUSED, reason="paused by operator")
        obs = observe(paused=True, rewards_active=False)
        for state in (QUOTING, paused):
            self.assertEqual(step_state(state, obs, NOW, CFG),
                             State(Phase.UNREWARDED, reason="rewards removed"))

    def test_rewards_returning_to_a_paused_market_leave_it_paused(self):
        unrewarded = State(Phase.UNREWARDED, reason="rewards removed")
        self.assertEqual(step_state(unrewarded, observe(paused=True), NOW, CFG),
                         State(Phase.PAUSED, reason="paused by operator"))

    def test_naked_paused_forces_no_long_bid(self):
        held = Position(fav_shares=100.0, fav_avg=0.97)
        obs = observe(naked_paused=True)
        self.assertFalse(naked_allowed(obs, NOW))
        intents = by_slot(plan(market(), FLOOR_BOOK, held, naked=naked_allowed(obs, NOW)))
        self.assertNotIn(Slot.LONG_BID, intents)
        self.assertIn(Slot.FAV_BID, intents)

    def test_naked_cooldown_ends_on_time_unless_paused(self):
        obs = observe(naked_cooldown_until=NOW + timedelta(seconds=10))
        self.assertFalse(naked_allowed(obs, NOW))
        self.assertTrue(naked_allowed(obs, NOW + timedelta(seconds=10)))
        self.assertFalse(naked_allowed(obs._replace(naked_paused=True), NOW + timedelta(seconds=10)))


class BaselineTest(unittest.TestCase):
    def test_preexisting_fav_shares_without_farm_fills_get_no_fav_ask(self):
        held = Position(fav_shares=300.0, fav_avg=0.97)
        farm = farm_position(held, ratchet_baseline(None, held))
        intents = by_slot(plan(market(), FLOOR_BOOK, farm))
        self.assertNotIn(Slot.FAV_ASK, intents)
        self.assertEqual(intents[Slot.FAV_BID].size, 507.61)

    def test_only_shares_above_the_baseline_are_sold(self):
        baseline = ratchet_baseline(None, Position(fav_shares=300.0, fav_avg=0.97))
        held = Position(fav_shares=400.0, fav_avg=0.972)
        farm = farm_position(held, ratchet_baseline(baseline, held))
        ask = by_slot(plan(market(), FLOOR_BOOK, farm))[Slot.FAV_ASK]
        self.assertEqual(ask.size, 100.0)

    def test_manual_sale_lowers_the_baseline(self):
        baseline = Baseline(fav=300.0, long=50.0)
        sold = Position(fav_shares=250.0, long_shares=50.0)
        lowered = ratchet_baseline(baseline, sold)
        self.assertEqual(lowered, Baseline(fav=250.0, long=50.0))
        refilled = Position(fav_shares=300.0, long_shares=50.0)
        self.assertEqual(ratchet_baseline(lowered, refilled), lowered)
        self.assertEqual(farm_position(refilled, lowered).fav_shares, 50.0)


class BackoffTest(unittest.TestCase):
    def test_delay_doubles_to_the_cap(self):
        delays, backoff = [], None
        for _ in range(7):
            backoff = next_backoff(backoff, NOW)
            delays.append(backoff.delay_s)
        self.assertEqual(delays, [30, 60, 120, 240, 480, 900, 900])
        self.assertEqual(backoff.until, NOW + timedelta(seconds=900))

    def test_waiting_slot_drops_only_its_places(self):
        target = plan(market(), FLOOR_BOOK)
        intents = by_slot(target)
        actions = [Cancel(("a",)), Place(intents[Slot.FAV_BID]), Place(intents[Slot.LONG_BID])]
        backoffs = {Slot.LONG_BID: next_backoff(None, NOW)}
        self.assertEqual(without_backed_off(actions, backoffs, NOW), actions[:2])
        later = NOW + timedelta(seconds=30)
        self.assertEqual(without_backed_off(actions, backoffs, later), actions)


# A 3.5c gap between the best bid and the best ask.
WIDE_BOOK = Levels(bids=[(0.95, 100.0)], asks=[(0.985, 100.0)], tick=0.001)
# The favorite book of the console's EV check example, with an 8.5c max spread.
WORKED_BOOK = Levels(
    bids=[(0.934, 88.8), (0.922, 21.0), (0.574, 85.0), (0.573, 40.0)],
    asks=[(0.990, 240.99), (0.994, 111.0), (0.997, 50.0), (0.998, 200.0), (0.999, 729.1)],
    tick=0.001,
)


class WideBookTest(unittest.TestCase):
    def test_fav_bid_stops_max_lead_over_the_best_bid_and_scores_against_its_mid(self):
        target = plan(market(min_size=20.0), WIDE_BOOK)
        fav = by_slot(target)[Slot.FAV_BID]
        self.assertEqual(fav.price, 0.955)
        self.assertAlmostEqual(target.mid, 0.97)
        self.assertAlmostEqual(fav.score, (0.020 / 0.035) ** 2)

    def test_ev_cap_above_the_best_bid_scores_against_the_mid_it_creates(self):
        fm = market(min_size=20.0, p_true=0.963)
        target = plan(fm, WIDE_BOOK)
        fav = by_slot(target)[Slot.FAV_BID]
        # The 0.953 cap moves the midpoint to (0.953 + 0.985) / 2 = 0.969.
        self.assertEqual(fav.price, 0.953)
        self.assertAlmostEqual(target.mid, 0.969)
        self.assertAlmostEqual(fav.score, (0.019 / 0.035) ** 2)

    def test_worked_example_at_the_default_config(self):
        defaults = config_from(build_parser().parse_args([]))
        fm = market(v=0.085, min_size=20.0, p_true=0.96)
        target = plan(fm, WORKED_BOOK, cfg=defaults)
        fav = by_slot(target)[Slot.FAV_BID]
        # A quiet market bids the 93.9c ceiling, 0.5c over the best bid; the 0.940
        # EV cap does not bind.
        self.assertEqual(fav.price, 0.939)
        self.assertAlmostEqual(target.mid, 0.9645)
        self.assertAlmostEqual(fav.score, ((0.085 - 0.0255) / 0.085) ** 2)


class BoundaryTest(unittest.TestCase):
    def test_strip_own_removes_our_orders_from_both_sides(self):
        book = Levels(bids=[(0.985, 1200.0), (0.98, 500.0)], asks=[(0.995, 1000.0), (0.999, 400.0)])
        orders = [
            live("a", Slot.FAV_BID, 0.985, 200.0),
            live("b", Slot.LONG_ASK, 0.02, 500.0),
            live("c", Slot.LONG_BID, 0.001, 400.0),
        ]
        stripped = strip_own(book, orders, market())
        self.assertEqual(stripped.bids, [(0.985, 1000.0)])
        self.assertEqual(stripped.asks, [(0.995, 1000.0)])

    def test_sponsored_only_rewards_stay_active(self):
        info = MarketInfo((FAV, LONG), 0.085, 50.0, 0.001, False, True, False)
        self.assertTrue(rewards_active(info, "0xc0nd", {"0xc0nd": 2.1}))
        self.assertTrue(rewards_active(info, "0xc0nd", None))
        self.assertFalse(rewards_active(info, "0xc0nd", {"0xother": 2.1}))
        self.assertFalse(rewards_active(info._replace(v=0.0), "0xc0nd", None))

    def test_rewards_end_when_the_listing_drops_the_market(self):
        info = MarketInfo((FAV, LONG), 0.085, 20.0, 0.001, False, True, False)
        self.assertFalse(rewards_active(info, "0xc0nd", {"0xother": 2.1}))
        self.assertTrue(rewards_active(info, "0xc0nd", None))

    def test_fill_events_name_the_slot_that_filled(self):
        before = Position(fav_shares=100.0, fav_avg=0.97, long_shares=0.0)
        after = Position(fav_shares=60.0, fav_avg=0.97, long_shares=300.0, long_avg=0.001)
        self.assertEqual(fill_events(before, after), [(Slot.FAV_ASK, 40.0), (Slot.LONG_BID, 300.0)])


def positions_api(*statuses):
    """An httpx client whose /positions answers with statuses in turn, then 200."""
    calls = []

    def handle(request):
        calls.append(request)
        status = statuses[len(calls) - 1] if len(calls) <= len(statuses) else 200
        if status == 429:
            return httpx.Response(429, headers={"retry-after": "1"},
                                  json={"error": "Too Many Requests"})
        return httpx.Response(status, json=[{"asset": FAV, "size": "120", "avgPrice": "0.97"}])

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


class FetchHoldingsTest(unittest.TestCase):
    def test_a_rate_limited_read_waits_and_retries(self):
        http, calls = positions_api(429, 429)
        with mock.patch("time.sleep") as sleep:
            holdings = reward_farmer.fetch_holdings(http, "0xme")
        self.assertEqual(holdings, {FAV: (120.0, 0.97)})
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args for c in sleep.call_args_list], [(1.0,), (1.0,)])

    def test_a_read_still_rate_limited_after_the_retries_raises(self):
        http, calls = positions_api(*[429] * 10)
        with mock.patch("time.sleep"), self.assertRaises(httpx.HTTPStatusError):
            reward_farmer.fetch_holdings(http, "0xme")
        self.assertEqual(len(calls), reward_farmer.RATE_LIMIT_RETRIES + 1)


class HistoriesTest(unittest.TestCase):
    WANT = reward_farmer.Want(FAV, 0.90, "test-market")

    def setUp(self):
        self.fetch = mock.Mock(return_value=NO_SWEEPS)
        self.histories = reward_farmer.Histories(threading.Event(), self.fetch)
        self.histories.wanted = {"0xc0nd": self.WANT}
        patcher = mock.patch.object(reward_farmer, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def refresh_at(self, *seconds):
        """Refresh at each NOW + seconds; the read after each."""
        seen = []
        for s in seconds:
            self.histories.refresh(None, NOW + timedelta(seconds=s))
            seen.append(self.histories.read.get("0xc0nd"))
        return seen

    def test_a_wanted_market_is_read_at_once_then_hourly(self):
        self.refresh_at(0, 5, 3599, 3600)
        self.assertEqual([c.args[3] for c in self.fetch.call_args_list],
                         [NOW, NOW + timedelta(seconds=3600)])
        self.assertEqual(self.fetch.call_args.args[1:], ("0xc0nd", FAV,
                                                         NOW + timedelta(seconds=3600), 0.90))

    def test_a_failed_first_read_leaves_no_history_and_retries_after_two_minutes(self):
        self.fetch.side_effect = [httpx.ConnectError("data API down"), NO_SWEEPS]
        self.assertEqual(self.refresh_at(0, 119, 120), [None, None, NO_SWEEPS])
        self.assertEqual(self.fetch.call_count, 2)

    def test_a_failed_reread_keeps_the_last_history(self):
        later = NO_SWEEPS._replace(events=(1,) * len(DISTANCES))
        self.fetch.side_effect = [NO_SWEEPS, httpx.ConnectError("data API down"), later]
        self.assertEqual(self.refresh_at(0, 3600, 3700, 3720), [NO_SWEEPS] * 3 + [later])

    def test_a_market_no_longer_wanted_is_forgotten(self):
        other = reward_farmer.Want("other-fav", 0.90, "other")
        self.histories.wanted = {"0xc0nd": self.WANT, "0xother": other}
        self.refresh_at(0)
        self.histories.wanted = {"0xother": other}
        self.refresh_at(5)
        self.assertEqual(self.histories.read, {"0xother": NO_SWEEPS})
        self.histories.wanted = {"0xc0nd": self.WANT}
        self.refresh_at(10)
        self.assertEqual(self.fetch.call_count, 3)

    def test_a_changed_floor_rereads_at_once(self):
        self.refresh_at(0)
        self.histories.wanted = {"0xc0nd": self.WANT._replace(floor=0.92)}
        self.refresh_at(5)
        self.assertEqual([c.args[4] for c in self.fetch.call_args_list], [0.90, 0.92])


CID = "0xc0nd"
INFO = MarketInfo((FAV, LONG), 0.035, 50.0, 0.001, False, True, False)
METRICS = {"market": {"fav_token_id": FAV, "slug": "test-market", "question": "Test?",
                      "end_date": (NOW + timedelta(days=100)).isoformat(),
                      "fav_outcome": "No", "rate_per_day": 100.0}}


class MarketInfosTest(unittest.TestCase):
    OTHER = "0xother"

    def setUp(self):
        self.fetch = mock.Mock(return_value={CID: INFO})
        self.infos = reward_farmer.MarketInfos(threading.Event(), self.fetch)
        self.infos.wanted = frozenset({CID})
        patcher = mock.patch.object(reward_farmer, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def refresh_at(self, *seconds):
        """Refresh at each NOW + seconds; CID's info after each."""
        seen = []
        for s in seconds:
            self.infos.refresh(None, NOW + timedelta(seconds=s))
            seen.append(self.infos.read.get(CID))
        return seen

    def fetch_counts_at(self, *seconds):
        """Refresh at each NOW + seconds; the fetch count after each."""
        counts = []
        for s in seconds:
            self.infos.refresh(None, NOW + timedelta(seconds=s))
            counts.append(self.fetch.call_count)
        return counts

    def test_wanted_markets_are_read_at_once_then_every_five_minutes(self):
        self.assertEqual(self.fetch_counts_at(0, 5, 299, 300, 305), [1, 1, 1, 2, 2])
        self.assertEqual(self.fetch.call_args.args[1], {CID})
        self.assertEqual(self.infos.read, {CID: INFO})

    def test_a_newly_wanted_market_is_read_at_once(self):
        self.refresh_at(0)
        self.fetch.return_value = {CID: INFO, self.OTHER: INFO}
        self.infos.wanted = frozenset({CID, self.OTHER})
        self.assertEqual(self.fetch_counts_at(5), [2])
        self.assertEqual(self.fetch.call_args.args[1], {CID, self.OTHER})
        self.assertEqual(self.infos.read, {CID: INFO, self.OTHER: INFO})

    def test_no_wanted_markets_make_no_request(self):
        self.infos.wanted = frozenset()
        self.assertEqual(self.fetch_counts_at(0, 300), [0, 0])

    def test_a_failed_first_read_leaves_nothing_and_retries_after_a_minute(self):
        self.fetch.side_effect = [httpx.ConnectError("Gamma down"), {CID: INFO}]
        self.assertEqual(self.refresh_at(0, 59, 60), [None, None, INFO])
        self.assertEqual(self.fetch.call_count, 2)

    def test_a_failed_reread_keeps_the_last_infos(self):
        later = INFO._replace(v=0.045)
        self.fetch.side_effect = [{CID: INFO}, httpx.ConnectError("Gamma down"), {CID: later}]
        self.assertEqual(self.refresh_at(0, 300, 359, 360), [INFO] * 3 + [later])

    def test_a_market_no_longer_wanted_drops_out_and_rejoining_rereads_at_once(self):
        self.fetch.return_value = {CID: INFO, self.OTHER: INFO}
        self.infos.wanted = frozenset({CID, self.OTHER})
        self.refresh_at(0)
        self.infos.wanted = frozenset({self.OTHER})
        self.assertEqual(self.fetch_counts_at(5), [1])
        self.assertEqual(self.infos.read, {self.OTHER: INFO})
        self.infos.wanted = frozenset({CID, self.OTHER})
        self.assertEqual(self.fetch_counts_at(10), [2])
        self.assertEqual(self.infos.read, {CID: INFO, self.OTHER: INFO})

    def test_a_market_the_fetch_leaves_out_keeps_its_last_info(self):
        later = INFO._replace(v=0.045)
        self.infos.wanted = frozenset({CID, self.OTHER})
        self.fetch.side_effect = [{CID: INFO, self.OTHER: INFO}, {self.OTHER: later}]
        self.assertEqual(self.refresh_at(0, 300, 305), [INFO] * 3)
        self.assertEqual(self.infos.read, {CID: INFO, self.OTHER: later})
        self.assertEqual(self.fetch.call_count, 2)

    def test_a_market_the_fetch_never_returns_is_not_refetched_every_tick(self):
        self.infos.wanted = frozenset({CID, self.OTHER})
        self.assertEqual(self.fetch_counts_at(0, 5, 10), [1, 1, 1])
        self.assertEqual(self.infos.read, {CID: INFO})


def gamma_api(rows):
    """An httpx client whose Gamma /markets answers with the rows asked for,
    open or closed as the request asks, as Gamma does; and its requests."""
    calls = []

    def handle(request):
        calls.append(request)
        ids = request.url.params.get_list("condition_ids")
        closed = request.url.params["closed"] == "true"
        return httpx.Response(200, json=[r for r in rows
                                         if r["conditionId"] in ids and r["closed"] == closed])

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


GAMMA_ROW = {"conditionId": CID, "clobTokenIds": json.dumps([FAV, LONG]),
             "rewardsMaxSpread": 3.5, "rewardsMinSize": 50, "orderPriceMinTickSize": 0.001,
             "negRisk": False, "acceptingOrders": True, "closed": False}


class FetchMarketInfosTest(unittest.TestCase):
    def test_ids_are_read_in_chunks_of_a_hundred_open_and_closed(self):
        ids = [f"0x{i:03d}" for i in range(150)]
        http, calls = gamma_api([])
        self.assertEqual(reward_farmer.fetch_market_infos(http, ids), {})
        asked = [(c.url.params["closed"], c.url.params.get_list("condition_ids"),
                  c.url.params["limit"]) for c in calls]
        self.assertEqual(len(asked), 4)
        for closed, chunk, limit in asked:
            self.assertLessEqual(len(chunk), 100)
            self.assertEqual(limit, str(len(chunk)))
        for closed in ("false", "true"):
            self.assertEqual(sorted(i for c, chunk, _ in asked if c == closed for i in chunk), ids)

    def test_open_and_closed_gamma_rows_parse_to_market_infos(self):
        shut = {**GAMMA_ROW, "conditionId": "0xshut", "acceptingOrders": False, "closed": True}
        http, _ = gamma_api([GAMMA_ROW, shut])
        self.assertEqual(reward_farmer.fetch_market_infos(http, [CID, "0xshut", "0xnone"]),
                         {CID: INFO, "0xshut": INFO._replace(accepting_orders=False, closed=True)})


class RanAway(BaseException):
    """Raised by a fake when the farmer keeps cycling after it should have stopped."""


class FarmerHarness(unittest.TestCase):
    """The daemon against a scratch farm.db, with the network faked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "farm.db")
        self.ui = farm_db.open_db(self.path)
        self.addCleanup(self.ui.close)
        farm_db.store_scan(self.ui, [farm_db.ScanRow(
            CID, "test-market", "Test?", (NOW + timedelta(days=100)).isoformat(), 1.0,
            json.dumps(METRICS),
        )], [CID], NOW, prior=fill_cost.prior_to_json(PRIOR))
        farm_db.approve_market(self.ui, CID, 0.995, 25.0)
        self.book_fetches = 0
        self.on_book_fetch = lambda count: None
        self.infos = mock.Mock(return_value={CID: INFO})
        self.history = mock.Mock(return_value=NO_SWEEPS)
        self.book = FLOOR_BOOK
        for name, fake in (
            ("fetch_market_infos", self.infos),
            ("fetch_history", self.history),
            ("fetch_holdings", mock.Mock(return_value={})),
            ("fetch_levels", self.fake_levels),
            ("fetch_total_rates", mock.Mock(return_value={})),
            ("log", mock.Mock()),
        ):
            patcher = mock.patch.object(reward_farmer, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.args = build_parser().parse_args(["--db", self.path, "--interval", "0.01"])

    def fake_levels(self, http, token_ids):
        self.book_fetches += 1
        self.on_book_fetch(self.book_fetches)
        return dict.fromkeys(token_ids, self.book)

    def started_farmer(self, read_histories=True, client=None):
        """A started farmer, its histories read as the reader thread would."""
        farmer = Farmer(self.args, config_from(self.args), client)
        self.addCleanup(farmer.http.close)
        self.addCleanup(farmer.db.close)
        farmer.start()
        if read_histories:
            farmer.histories.refresh(farmer.http, NOW)
        return farmer

    def query(self, sql, *params):
        return self.ui.execute(sql, params).fetchall()

    def events(self, after_id=0):
        return self.query("SELECT * FROM farm_events WHERE id > ? ORDER BY id", after_id)

    def last_event_id(self):
        return self.query("SELECT COALESCE(MAX(id), 0) FROM farm_events")[0][0]

    def farm_state(self):
        row = self.query("SELECT * FROM farm_state WHERE condition_id = ?", CID)[0]
        return row, json.loads(row["slots"])


class FarmerCycleTest(FarmerHarness):
    def test_dry_cycle_publishes_state_status_and_places(self):
        farmer = self.started_farmer()
        farmer.cycle()

        row, slots = self.farm_state()
        self.assertEqual(row["phase"], "QUOTING")
        self.assertEqual(slots["FAV_BID"]["decision"], "place")
        self.assertEqual(slots["FAV_BID"]["target"]["price"], 0.975)
        self.assertEqual(json.loads(row["position"])["baseline"], {"fav": 0.0, "long": 0.0})
        status = self.query("SELECT * FROM farmer_status")[0]
        self.assertEqual((status["mode"], status["managed"], status["stopped_at"]), ("dry", 1, None))
        self.assertIsNotNone(status["last_cycle_at"])
        places = [(e["slot"], e["price"]) for e in self.events() if e["kind"] == "PLACE"]
        self.assertEqual(places, [("FAV_BID", 0.975), ("LONG_BID", 0.001)])
        market_row = farm_db.find_market(self.ui, CID)
        self.assertEqual((market_row["status"], market_row["baseline_fav"]), ("approved", None))

    def test_a_cycle_with_no_changes_adds_no_events(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()
        farmer.cycle()
        self.assertEqual(self.events(before), [])
        _, slots = self.farm_state()
        self.assertEqual(slots["FAV_BID"]["decision"], "hold")

    def test_summary_line_prints_only_when_it_changes(self):
        farmer = self.started_farmer()

        def summaries():
            lines = [c.args[0] for c in reward_farmer.log.call_args_list]
            reward_farmer.log.reset_mock()
            return [line for line in lines if " | hold " in line]

        farmer.cycle()
        self.assertRegex(summaries()[0], r"QUOTING .* \| hold 0 cancel 0 place 2$")
        farmer.cycle()
        self.assertRegex(summaries()[0], r"QUOTING .* \| hold 2 cancel 0 place 0$")
        farmer.cycle()
        self.assertEqual(summaries(), [])

        farm_db.set_paused(self.ui, CID, True)
        farmer.cycle()
        self.assertRegex(summaries()[0], r"^test-market: PAUSED .* hold 0 cancel 2 place 0$")

    def test_pause_cancels_everything_next_cycle_and_resume_quotes_again(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()

        farm_db.set_paused(self.ui, CID, True)
        farmer.cycle()
        row, slots = self.farm_state()
        self.assertEqual(row["phase"], "PAUSED")
        self.assertEqual((row["q1"], row["q2"]), (0.0, 0.0))
        self.assertEqual({name: s["decision"] for name, s in slots.items()},
                         {"FAV_BID": "cancel", "LONG_BID": "cancel",
                          "FAV_ASK": "none", "LONG_ASK": "none"})
        kinds = [(e["kind"], e["slot"]) for e in self.events(before)]
        self.assertEqual(kinds, [("PHASE", None), ("CANCEL", "FAV_BID"), ("CANCEL", "LONG_BID")])

        farm_db.set_paused(self.ui, CID, False)
        farmer.cycle()
        row, _ = self.farm_state()
        self.assertEqual(row["phase"], "QUOTING")

    def test_approved_paused_previews_the_orders_and_resume_places_them(self):
        farm_db.set_approval(self.ui, CID, 0.995, 25.0, None, paused=True)
        farmer = self.started_farmer()
        farmer.cycle()
        row, slots = self.farm_state()
        self.assertEqual((row["phase"], row["q1"], row["q2"]), ("PAUSED", 0.0, 0.0))
        self.assertNotIn("PLACE", [e["kind"] for e in self.events()])
        preview = {name: (s["decision"], s["target"] and (s["target"]["price"], s["target"]["size"]))
                   for name, s in slots.items()}
        self.assertEqual(preview, {"FAV_BID": ("preview", (0.975, 512.82)),
                                   "LONG_BID": ("preview", (0.001, 303.45)),
                                   "FAV_ASK": ("none", None), "LONG_ASK": ("none", None)})
        self.assertEqual(slots["FAV_BID"]["reason"], "would place on resume")

        farm_db.set_paused(self.ui, CID, False)
        farmer.cycle()
        places = {e["slot"]: (e["price"], e["size"]) for e in self.events() if e["kind"] == "PLACE"}
        self.assertEqual(places, {slot: preview[slot][1] for slot in ("FAV_BID", "LONG_BID")})

    def test_paused_market_previews_nothing_when_resume_would_not_quote(self):
        farm_db.set_approval(self.ui, CID, 0.995, 25.0, None, paused=True)
        farm_db.put_setting(self.ui, "min_fav", 0.995, NOW)
        farmer = self.started_farmer()
        farmer.cycle()
        _, slots = self.farm_state()
        self.assertEqual({name: (s["decision"], s["target"]) for name, s in slots.items()},
                         dict.fromkeys(slots, ("none", None)))

    def test_settings_row_changes_the_effective_config(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()

        farm_db.put_setting(self.ui, "ev_margin", 0.022, NOW)
        farmer.cycle()
        settings = [e for e in self.events(before) if e["kind"] == "SETTINGS"]
        self.assertEqual(len(settings), 1)
        self.assertEqual(json.loads(settings[0]["data"]), {"ev_margin": [0.02, 0.022]})
        _, slots = self.farm_state()
        self.assertEqual(slots["FAV_BID"]["target"]["price"], 0.973)
        status = self.query("SELECT config FROM farmer_status")[0]
        self.assertEqual(json.loads(status["config"])["ev_margin"], 0.022)

        farmer.cycle()
        self.assertEqual(len([e for e in self.events(before) if e["kind"] == "SETTINGS"]), 1)

    def test_console_scan_rows_in_settings_raise_no_alert(self):
        farm_db.put_scan_params(self.ui, {"min_fav": 0.92}, NOW)
        farmer = self.started_farmer()
        farmer.cycle()
        self.assertEqual([e["message"] for e in self.events() if e["kind"] == "ALERT"], [])

    def test_market_capital_overrides_the_configured_capital(self):
        farm_db.set_capital(self.ui, CID, 97.5)
        farmer = self.started_farmer()
        farmer.cycle()
        row, slots = self.farm_state()
        self.assertEqual(row["capital_usd"], 97.5)
        self.assertEqual(slots["FAV_BID"]["target"]["size"], 100.0)

    def test_stop_command_ends_the_loop_like_ctrl_c(self):
        def send_stop(count):
            if count == 1:
                farm_db.send_command(self.ui, CommandKind.STOP, NOW)

        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 3:
                raise RanAway

        self.on_book_fetch = send_stop
        with mock.patch.dict(os.environ, {"POLYMARKET_FUNDER_ADDRESS": "0xme"}), \
                mock.patch.object(reward_farmer.time, "sleep", sleep):
            reward_farmer.run(self.args)

        self.assertEqual((self.book_fetches, len(sleeps)), (1, 1))
        command = self.query("SELECT * FROM commands")[0]
        self.assertEqual(command["result"], "stopping")
        self.assertIsNotNone(command["consumed_at"])
        self.assertIsNotNone(self.query("SELECT stopped_at FROM farmer_status")[0][0])
        stop = self.query("SELECT message FROM farm_events WHERE kind = 'STOP'")
        self.assertEqual([r[0] for r in stop], [f"Stopped: stop command {command['id']}"])

    def test_stop_sent_before_start_is_expired_not_obeyed(self):
        farm_db.send_command(self.ui, CommandKind.STOP, NOW)
        farmer = self.started_farmer()
        farmer.cycle()
        self.assertIsNone(farmer.stop_reason)
        self.assertTrue(self.query("SELECT result FROM commands")[0][0].startswith("expired"))

    def phase_and_kinds(self, farmer, before):
        farmer.cycle()
        row, _ = self.farm_state()
        return row["phase"], [e["kind"] for e in self.events(before)]

    def test_retired_market_approved_again_is_managed_afresh(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()

        farm_db.retire_market(self.ui, CID, "console")
        self.assertEqual(self.phase_and_kinds(farmer, before),
                         ("RETIRED", ["PHASE", "CANCEL", "CANCEL", "RETIRE"]))
        before = self.last_event_id()
        self.assertEqual(self.phase_and_kinds(farmer, before), ("RETIRED", []))

        farm_db.set_approval(self.ui, CID, 0.995, 25.0, None)
        phase, kinds = self.phase_and_kinds(farmer, before)
        self.assertEqual((phase, kinds), ("QUOTING", ["PHASE", "PLACE", "PLACE"]))
        self.assertIn("Managing test-market", self.events(before)[0]["message"])

    def test_rewards_ending_pulls_orders_and_returning_rewards_quote_again(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()

        farmer.total_rates.rates = {"0xother": 1.0}
        self.assertEqual(self.phase_and_kinds(farmer, before),
                         ("UNREWARDED", ["PHASE", "CANCEL", "CANCEL"]))
        before = self.last_event_id()
        self.assertEqual(self.phase_and_kinds(farmer, before), ("UNREWARDED", []))

        farmer.total_rates.rates = {CID: 100.0}
        self.assertEqual(self.phase_and_kinds(farmer, before),
                         ("QUOTING", ["PHASE", "PLACE", "PLACE"]))

    def test_approved_again_still_retires_on_expiry(self):
        farmer = self.started_farmer()
        farmer.cycle()
        farm_db.retire_market(self.ui, CID, "console")
        farmer.cycle()
        farm_db.put_setting(self.ui, "expiry_buffer_days", 1000, NOW)
        farm_db.set_approval(self.ui, CID, 0.995, 25.0, None)
        before = self.last_event_id()
        farmer.cycle()
        row, _ = self.farm_state()
        self.assertEqual((row["phase"], row["phase_reason"]), ("RETIRED", "expiry buffer reached"))
        self.assertNotIn("PLACE", [e["kind"] for e in self.events(before)])

    def test_dry_run_retirement_of_an_approved_row_does_not_restart(self):
        farm_db.put_setting(self.ui, "expiry_buffer_days", 1000, NOW)
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()
        for _ in range(3):
            farmer.cycle()
        self.assertEqual(farm_db.find_market(self.ui, CID)["status"], "approved")
        self.assertEqual(self.events(before), [])

    def test_a_retired_market_still_approved_is_no_longer_read(self):
        farm_db.put_setting(self.ui, "expiry_buffer_days", 1000, NOW)
        farmer = self.started_farmer()
        self.assertEqual(farmer.infos.wanted, {CID})
        farmer.cycle()
        farmer.cycle()
        self.assertEqual(farmer.infos.wanted, frozenset())
        farmer.infos.refresh(farmer.http, NOW + timedelta(seconds=600))
        self.assertEqual(farmer.infos.read, {})
        self.assertEqual(self.infos.call_count, 1)

    def test_rows_of_markets_no_longer_managed_are_deleted(self):
        with self.ui:
            self.ui.execute("INSERT INTO farm_state (condition_id, updated_at, phase) "
                            "VALUES ('0xgone', '2026-01-01T00:00:00+00:00', 'QUOTING')")
        farmer = self.started_farmer()
        farmer.cycle()
        self.assertEqual([r[0] for r in self.query("SELECT condition_id FROM farm_state")], [CID])

    def replay(self, *books):
        """Cycle a market on the Mistral terms once per book. Returns, per cycle,
        (phase, reason, {slot: (price, size)} of the bids it targets), and the
        events of all the cycles."""
        self.infos.return_value = {CID: INFO._replace(v=0.085, min_size=20.0)}
        farmer = self.started_farmer()
        before = self.last_event_id()
        seen = []
        for book in books:
            self.book = book
            farmer.cycle()
            row, slots = self.farm_state()
            bids = {slot: (s["target"]["price"], s["target"]["size"])
                    for slot, s in slots.items() if slot.endswith("_BID") and s["target"]}
            seen.append((row["phase"], row["phase_reason"], bids))
        return seen, [(e["kind"], e["message"]) for e in self.events(before)]

    def test_a_bid_leaving_keeps_quoting_and_follows_the_bids_that_remain(self):
        (before, after), events = self.replay(MISTRAL_BOOK, MISTRAL_BOOK_BID_GONE)
        self.assertEqual((before[0], before[2]["FAV_BID"][0]), ("QUOTING", 0.935))
        self.assertEqual(after[:2], ("QUOTING", ""))
        self.assertEqual(after[2], {"FAV_BID": (0.855, 584.79), "LONG_BID": (0.001, 795.97)})
        self.assertNotIn("PHASE", [kind for kind, _ in events])

    def test_deep_asks_falling_by_the_threshold_cool_down(self):
        fallen = MISTRAL_BOOK._replace(asks=[(0.967, 150.0)] + MISTRAL_BOOK.asks[1:])
        (_, after), events = self.replay(MISTRAL_BOOK, fallen)
        self.assertEqual(after, ("COOLDOWN", "ask drop", {}))
        self.assertIn("PHASE", [kind for kind, _ in events])

    def test_a_dust_ask_far_below_does_not_cool_down(self):
        dusted = MISTRAL_BOOK._replace(asks=[(0.95, 5.0)] + MISTRAL_BOOK.asks)
        (_, after), _ = self.replay(MISTRAL_BOOK, dusted)
        self.assertEqual((after[0], after[2]["FAV_BID"][0]), ("QUOTING", 0.935))

    def test_mid_history_is_sampled_at_most_once_a_minute(self):
        clock = [NOW]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        with mock.patch.object(reward_farmer, "datetime", Clock):
            farmer = self.started_farmer()
            for seconds in (0, 30, 59, 60, 90, 125):
                clock[0] = NOW + timedelta(seconds=seconds)
                farmer.cycle()

        rows = self.query("SELECT * FROM farm_mids WHERE condition_id = ? ORDER BY at", CID)
        self.assertEqual([r["at"] for r in rows],
                         [farm_db.stamp(NOW + timedelta(seconds=s)) for s in (0, 60, 125)])
        self.assertEqual([(r["fav_bid"], r["long_bid"]) for r in rows], [(0.975, 0.001)] * 3)
        self.assertEqual((rows[0]["mid"], rows[0]["book_mid"]), (0.99, 0.99))

    def test_until_its_history_lands_a_market_places_no_favorite_bid(self):
        farmer = self.started_farmer(read_histories=False)
        farmer.cycle()
        fav = self.farm_state()[1]["FAV_BID"]
        self.assertEqual((fav["decision"], fav["reason"]), ("none", "no fill history yet"))
        self.assertEqual(self.history.call_count, 0)
        self.assertEqual(farmer.histories.wanted,
                         {CID: reward_farmer.Want(FAV, CFG.min_fav, "test-market")})
        farmer.histories.refresh(farmer.http, NOW)
        farmer.cycle()
        self.assertEqual(self.farm_state()[1]["FAV_BID"]["decision"], "place")

    def first_bid_on_stored_history(self, history):
        """The favorite bid slot of a restarted farmer's first cycle, its
        farm_state row holding `history`, before the reader reads any."""
        self.started_farmer().cycle()
        slots = self.farm_state()[1]
        slots["FAV_BID"]["history"] = history
        with self.ui:
            self.ui.execute("UPDATE farm_state SET slots = ?", (json.dumps(slots),))
        self.started_farmer(read_histories=False).cycle()
        self.assertEqual(self.history.call_count, 1)
        self.history.reset_mock()
        return self.farm_state()[1]["FAV_BID"]

    def stored(self, age_s):
        return fill_cost.history_to_json(NO_SWEEPS._replace(
            read_at=datetime.now(timezone.utc) - timedelta(seconds=age_s)))

    def test_a_restart_quotes_the_favorite_bid_on_a_fresh_stored_history(self):
        fav = self.first_bid_on_stored_history(self.stored(60))
        self.assertEqual((fav["decision"], fav["target"]["price"]), ("place", 0.975))

    def test_a_restart_ignores_a_stored_history_older_than_its_refresh(self):
        fav = self.first_bid_on_stored_history(self.stored(reward_farmer.HISTORY_S + 60))
        self.assertEqual((fav["decision"], fav["reason"]), ("none", "no fill history yet"))

    def test_a_restart_ignores_a_missing_or_malformed_stored_history(self):
        fresh = self.stored(60)
        for history in (None, "junk", {**fresh, "events": "junk"}, {**fresh, "read_at": "soon"},
                        {**fresh, "v": 99}):
            with self.subTest(history=history):
                fav = self.first_bid_on_stored_history(history)
                self.assertEqual((fav["decision"], fav["reason"]),
                                 ("none", "no fill history yet"))

    def test_a_history_of_no_days_places_no_favorite_bid(self):
        self.history.return_value = NO_SWEEPS._replace(days=0.0)
        self.started_farmer().cycle()
        fav = self.farm_state()[1]["FAV_BID"]
        self.assertEqual((fav["decision"], fav["reason"]), ("none", "no fill history yet"))
        self.assertNotIn("PLACE FAV_BID", [f"{e['kind']} {e['slot']}" for e in self.events()])

    def test_the_favorite_bid_slot_shows_its_economics_and_history_on_the_stored_prior(self):
        history = History((4, 3, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0), 60.0, NOW, 0.90, 2, 10.0)
        self.history.return_value = history
        self.started_farmer().cycle()
        row, slots = self.farm_state()
        economics, target = slots["FAV_BID"]["economics"], slots["FAV_BID"]["target"]
        self.assertEqual(economics["price"], target["price"])
        self.assertAlmostEqual(economics["net_per_day"], economics["reward_per_day"]
                               - economics["fills_per_day"] * economics["loss_per_share"])
        fills = fill_cost.fill_curve(history, PRIOR, 0.90)
        self.assertAlmostEqual(economics["fills_per_day"],
                               target["size"] * fills.per_day(row["mid"] - target["price"]))
        self.assertEqual(slots["FAV_BID"]["history"], fill_cost.history_to_json(history))

    def test_paused_market_samples_no_resting_bids(self):
        farm_db.set_paused(self.ui, CID, True)
        farmer = self.started_farmer()
        farmer.cycle()
        row = self.query("SELECT * FROM farm_mids WHERE condition_id = ?", CID)[0]
        self.assertEqual((row["fav_bid"], row["long_bid"]), (None, None))
        self.assertIsNotNone(row["book_mid"])

    def test_start_prunes_mid_history_older_than_a_week(self):
        now = datetime.now(timezone.utc)
        status = farm_db.FarmerStatus("dry", 1, now, now, 5.0, False, 0, {})
        farm_db.publish(self.ui, status, mids=[
            farm_db.MidSample(CID, now - timedelta(days=8), 0.9, 0.9, None, None),
            farm_db.MidSample(CID, now - timedelta(days=6), 0.9, 0.9, None, None),
        ])
        self.started_farmer()
        self.assertEqual([r["at"] for r in self.query("SELECT at FROM farm_mids")],
                         [farm_db.stamp(now - timedelta(days=6))])


CID2, FAV2, LONG2 = "0xc0nd2", "fav-token-2", "long-token-2"
INFO2 = INFO._replace(token_ids=(FAV2, LONG2))


class LiveFarmerTest(FarmerHarness):
    """A live farmer on two markets against a mock CLOB that rests what it accepts."""

    def setUp(self):
        super().setUp()
        metrics = {"market": {**METRICS["market"], "fav_token_id": FAV2, "slug": "other-market"}}
        farm_db.store_scan(self.ui, [farm_db.ScanRow(
            CID2, "other-market", "Other?", (NOW + timedelta(days=100)).isoformat(), 1.0,
            json.dumps(metrics),
        )], [CID, CID2], NOW)
        farm_db.approve_market(self.ui, CID2, 0.995, 25.0)
        self.infos.return_value = {CID: INFO, CID2: INFO2}
        self.market_of = {FAV: CID, LONG: CID, FAV2: CID2, LONG2: CID2}
        self.resting = {}
        self.posted = 0
        self.rejected_tokens = set()
        self.client = mock.Mock()
        self.client.get_balance_allowance.return_value = {"balance": "10000000000"}
        self.client.get_reward_percentages.return_value = {}
        self.client.get_earnings_for_user_for_day.return_value = []
        self.clob_ticks = {CID: "0.001", CID2: "0.001"}
        self.cached_ticks = {}
        self.client.get_clob_market_info.side_effect = self.get_clob_market_info
        self.client.get_tick_size.side_effect = self.get_tick_size
        self.client.create_order.side_effect = self.create_order
        self.client.post_orders.side_effect = self.post_orders
        self.client.get_open_orders.side_effect = self.get_open_orders
        self.client.cancel_orders.side_effect = self.cancel_orders

    def get_clob_market_info(self, condition_id):
        for token_id, cid in self.market_of.items():
            if cid == condition_id:
                self.cached_ticks[token_id] = self.clob_ticks[cid]
        return {}

    def get_tick_size(self, token_id):
        return self.cached_ticks.setdefault(token_id, self.clob_ticks[self.market_of[token_id]])

    def create_order(self, args, options):
        """Signs as the client does: never finer than the tick it has cached."""
        cached = self.get_tick_size(args.token_id)
        if float(options.tick_size) < float(cached):
            raise ValueError(f"invalid tick size ({options.tick_size}), "
                             f"minimum for the market is {cached}")
        return args

    def post_orders(self, batch, post_only):
        responses = []
        for item in batch:
            order = item.order
            if order.token_id in self.rejected_tokens:
                responses.append({"success": False, "errorMsg": "post-only order crosses"})
                continue
            self.posted += 1
            order_id = f"o{self.posted}"
            self.resting[order_id] = {
                "id": order_id, "market": self.market_of[order.token_id],
                "asset_id": order.token_id, "side": order.side, "price": str(order.price),
                "original_size": str(order.size), "size_matched": "0",
            }
            responses.append({"success": True, "orderID": order_id, "errorMsg": ""})
        return responses

    def get_open_orders(self):
        return list(self.resting.values())

    def cancel_orders(self, order_ids):
        for order_id in order_ids:
            del self.resting[order_id]
        return {"canceled": list(order_ids), "not_canceled": {}}

    def posted_tokens(self):
        return [sorted(item.order.token_id for item in c.args[0])
                for c in self.client.post_orders.call_args_list]

    def test_one_cycle_posts_both_markets_places_in_one_batch(self):
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        self.assertEqual(self.posted_tokens(), [sorted([FAV, LONG, FAV2, LONG2])])
        self.client.cancel_orders.assert_not_called()

    def test_one_cycle_cancels_both_markets_orders_in_one_call(self):
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        resting = set(self.resting)
        self.assertEqual(len(resting), 4)
        for cid in (CID, CID2):
            farm_db.set_paused(self.ui, cid, True)
        farmer.cycle()
        self.assertEqual([set(c.args[0]) for c in self.client.cancel_orders.call_args_list],
                         [resting])
        self.assertEqual(self.resting, {})

    def test_a_rejection_in_the_shared_batch_backs_off_only_its_markets_slot(self):
        self.rejected_tokens = {FAV2}
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        self.assertEqual(self.client.post_orders.call_count, 1)
        self.assertEqual({cid: set(m.backoffs) for cid, m in farmer.markets.items()},
                         {CID: set(), CID2: {Slot.FAV_BID}})
        flagged = [(e["kind"], e["condition_id"], e["slot"]) for e in self.events()
                   if e["kind"] in ("REJECT", "BACKOFF")]
        self.assertEqual(flagged, [("REJECT", CID2, "FAV_BID"), ("BACKOFF", CID2, "FAV_BID")])

    def test_a_failed_batch_alerts_each_market_in_it_with_its_own_count(self):
        self.client.post_orders.side_effect = httpx.ConnectError("down")
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        alerts = [(e["condition_id"], e["message"]) for e in self.events() if e["kind"] == "ALERT"]
        self.assertEqual(alerts, [(CID, "post of 2 orders failed on test-market: down"),
                                  (CID2, "post of 2 orders failed on other-market: down")])

    def test_a_cycle_lists_every_open_order_once_and_holds_each_markets_orders(self):
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        resting = dict(self.resting)
        self.client.reset_mock()
        farmer.cycle()
        self.client.get_open_orders.assert_called_once_with()
        self.client.cancel_orders.assert_not_called()
        self.client.post_orders.assert_not_called()
        self.assertEqual(self.resting, resting)

    def test_an_order_on_an_unmanaged_token_is_left_alone(self):
        self.resting["foreign"] = {
            "id": "foreign", "market": CID, "asset_id": "foreign-token", "side": "BUY",
            "price": "0.5", "original_size": "10", "size_matched": "0",
        }
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        farmer.cycle()
        self.assertEqual(self.posted_tokens(), [sorted([FAV, LONG, FAV2, LONG2])])
        self.client.cancel_orders.assert_not_called()
        self.assertIn("foreign", self.resting)
        self.assertEqual({cid: sorted(o.token_id for o in m.orders)
                          for cid, m in farmer.markets.items()},
                         {CID: sorted([FAV, LONG]), CID2: sorted([FAV2, LONG2])})

    def test_a_failed_listing_plans_no_market_until_it_reads_again(self):
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        gone = next(i for i, e in self.resting.items() if e["asset_id"] == FAV2)
        del self.resting[gone]
        self.client.reset_mock()
        self.client.get_open_orders.side_effect = httpx.ConnectError("down")
        farmer.cycle()
        self.assertIn(mock.call("Warning: open orders unavailable (down); planning no market"),
                      reward_farmer.log.call_args_list)
        self.client.post_orders.assert_not_called()
        self.client.cancel_orders.assert_not_called()
        self.client.get_open_orders.side_effect = self.get_open_orders
        farmer.cycle()
        self.assertEqual(self.posted_tokens(), [[FAV2]])
        self.client.cancel_orders.assert_not_called()

    def stopped_after_a_cycle(self):
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        self.client.reset_mock()
        return farmer

    def test_stopping_lists_once_and_cancels_only_the_managed_markets_orders(self):
        farmer = self.stopped_after_a_cycle()
        managed_ids = set(self.resting)
        self.resting["foreign"] = {"id": "foreign", "market": "0xunmanaged", "asset_id": "x"}
        farmer.shutdown("test", cancel=True)
        self.client.get_open_orders.assert_called_once_with()
        self.assertEqual([set(c.args[0]) for c in self.client.cancel_orders.call_args_list],
                         [managed_ids])
        self.assertEqual(list(self.resting), ["foreign"])
        self.client.cancel_market_orders.assert_not_called()
        cancels = [(e["condition_id"], e["message"]) for e in self.events() if e["kind"] == "CANCEL"]
        self.assertEqual(cancels, [(CID, "Cancelled 2 orders on test-market"),
                                   (CID2, "Cancelled 2 orders on other-market")])

    def test_stopping_finds_orders_by_token_whatever_the_listing_calls_the_market(self):
        farmer = self.stopped_after_a_cycle()
        for entry in self.resting.values():
            entry["market"] = entry["market"].upper()
        farmer.shutdown("test", cancel=True)
        self.assertEqual(self.resting, {})
        self.client.cancel_market_orders.assert_not_called()

    def test_an_order_the_stop_could_not_cancel_alerts_on_its_market(self):
        farmer = self.stopped_after_a_cycle()
        stuck = next(i for i, e in self.resting.items() if e["market"] == CID2)
        self.client.cancel_orders.side_effect = None
        self.client.cancel_orders.return_value = {"not_canceled": {stuck: "matched"}}
        farmer.shutdown("test", cancel=True)
        flagged = [(e["kind"], e["condition_id"], e["message"]) for e in self.events()
                   if e["kind"] in ("CANCEL", "ALERT")]
        self.assertEqual(flagged, [
            ("CANCEL", CID, "Cancelled 2 orders on test-market"),
            ("CANCEL", CID2, "Cancelled 1 orders on other-market"),
            ("ALERT", CID2, f"Warning: cancelling {stuck} on other-market failed (matched)"),
        ])

    def test_a_failed_listing_on_stop_falls_back_to_a_cancel_per_market(self):
        farmer = self.stopped_after_a_cycle()
        self.client.get_open_orders.side_effect = httpx.ConnectError("down")
        farmer.shutdown("test", cancel=True)
        self.client.cancel_orders.assert_not_called()
        self.assertEqual([c.args[0].market for c in self.client.cancel_market_orders.call_args_list],
                         [CID, CID2])

    def market_info_fetches(self):
        return sorted(c.args[0] for c in self.client.get_clob_market_info.call_args_list)

    def test_a_new_markets_clob_info_is_read_once(self):
        farmer = self.started_farmer(client=self.client)
        self.assertEqual(self.market_info_fetches(), [CID, CID2])
        farmer.cycle()
        self.assertEqual(self.market_info_fetches(), [CID, CID2])

    def test_a_failed_clob_info_read_does_not_stop_the_cycle(self):
        self.client.get_clob_market_info.side_effect = httpx.ConnectError("down")
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        warnings = [c.args[0] for c in reward_farmer.log.call_args_list
                    if c.args[0].startswith("Warning: market info")]
        self.assertEqual(sorted(warnings), ["Warning: market info for other-market unavailable (down)",
                                            "Warning: market info for test-market unavailable (down)"])
        self.assertEqual(self.posted_tokens(), [sorted([FAV, LONG, FAV2, LONG2])])

    def test_a_market_whose_tick_shrank_refreshes_the_clients_tick_before_signing(self):
        self.clob_ticks[CID] = "0.01"
        farmer = self.started_farmer(client=self.client)
        self.clob_ticks[CID] = "0.001"
        farmer.cycle()
        self.assertEqual(self.market_info_fetches(), [CID, CID, CID2])
        self.assertEqual({c.args[1].tick_size for c in self.client.create_order.call_args_list},
                         {"0.001"})
        self.assertEqual(self.posted_tokens(), [sorted([FAV, LONG, FAV2, LONG2])])

    def test_a_failed_tick_refresh_warns_and_the_other_market_still_posts(self):
        self.clob_ticks[CID] = "0.01"
        farmer = self.started_farmer(client=self.client)
        self.client.get_clob_market_info.side_effect = httpx.ConnectError("down")
        farmer.cycle()
        self.assertIn(mock.call("Warning: tick size of test-market unavailable (down)"),
                      reward_farmer.log.call_args_list)
        self.assertEqual(self.posted_tokens(), [sorted([FAV2, LONG2])])
        alerts = [(e["condition_id"], e["slot"]) for e in self.events() if e["kind"] == "ALERT"]
        self.assertEqual(alerts, [(CID, "FAV_BID"), (CID, "LONG_BID")])

    def run_live(self, *flags, stop):
        """run() live, calling stop() after the first cycle that leaves orders
        resting; self.at_stop is what rested then."""
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 500:
                raise RanAway
            self.at_stop = dict(self.resting)
            if self.resting:
                stop()

        args = build_parser().parse_args(["--db", self.path, "--interval", "0.01", "--live",
                                          *flags])
        with mock.patch.dict(os.environ, {"POLYMARKET_FUNDER_ADDRESS": "0xme"}), \
                mock.patch("config.load_config"), \
                mock.patch("config.create_client", return_value=self.client), \
                mock.patch("key_loader.cleanup_key_source"), \
                mock.patch.object(reward_farmer.time, "sleep", sleep):
            reward_farmer.run(args)

    def stop_command(self, payload=None):
        def stop():
            if not self.query("SELECT 1 FROM commands"):
                farm_db.send_command(self.ui, CommandKind.STOP, NOW, payload)
        return stop

    def stop_event(self):
        event = next(e for e in self.events() if e["kind"] == "STOP")
        return event["message"], json.loads(event["data"])

    def interrupt(self):
        raise KeyboardInterrupt

    def test_a_stop_command_keeping_orders_leaves_them_resting(self):
        self.run_live(stop=self.stop_command({"keep_orders": True}))
        self.assertTrue(self.at_stop)
        self.assertEqual(self.resting, self.at_stop)
        self.client.cancel_market_orders.assert_not_called()
        command = self.query("SELECT * FROM commands")[0]
        self.assertEqual(command["result"], "stopping, keeping orders")
        self.assertEqual(self.stop_event(), (
            f"Stopped: stop command {command['id']}; orders left resting",
            {"reason": f"stop command {command['id']}", "orders_kept": True}))

    def test_a_plain_stop_command_still_cancels(self):
        self.run_live(stop=self.stop_command())
        self.assertTrue(self.at_stop)
        self.assertEqual(self.resting, {})
        self.assertEqual(self.stop_event()[1]["orders_kept"], False)

    def test_an_interrupt_under_keep_orders_leaves_them_resting(self):
        self.run_live("--keep-orders", stop=self.interrupt)
        self.assertTrue(self.at_stop)
        self.assertEqual(self.resting, self.at_stop)
        self.assertEqual(self.stop_event(), ("Stopped: interrupted; orders left resting",
                                             {"reason": "interrupted", "orders_kept": True}))

    def test_an_interrupt_without_keep_orders_cancels(self):
        self.run_live(stop=self.interrupt)
        self.assertTrue(self.at_stop)
        self.assertEqual(self.resting, {})

    def test_a_crash_under_keep_orders_still_cancels(self):
        def crash():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.run_live("--keep-orders", stop=crash)
        self.assertTrue(self.at_stop)
        self.assertEqual(self.resting, {})

    def test_keeping_orders_under_the_heartbeat_alerts_that_it_will_cancel_them(self):
        self.args = build_parser().parse_args(["--db", self.path, "--heartbeat"])
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        farm_db.send_command(self.ui, CommandKind.STOP, NOW, {"keep_orders": True})
        farmer.cycle()
        farmer.shutdown(farmer.stop_reason, cancel=not farmer.keep_orders)
        alerts = [e["message"] for e in self.events() if e["kind"] == "ALERT"]
        self.assertEqual(alerts, ["ALERT: keeping orders, but the heartbeat stops with the "
                                  "farmer and Polymarket cancels them within about 10s"])
        self.assertEqual(self.stop_event()[1]["orders_kept"], False)

    def test_a_restarted_farmer_holds_the_orders_the_last_run_kept(self):
        first = self.started_farmer(client=self.client)
        first.cycle()
        kept = dict(self.resting)
        self.assertEqual(len(kept), 4)
        first.shutdown("test", cancel=False)
        self.client.reset_mock()

        second = self.started_farmer(client=self.client)
        second.cycle()
        self.client.cancel_orders.assert_not_called()
        self.client.post_orders.assert_not_called()
        self.assertEqual(self.resting, kept)
        _, slots = self.farm_state()
        self.assertEqual({s["decision"] for s in slots.values() if s["order"]}, {"hold"})

    def test_a_restart_holds_the_kept_orders_before_reading_any_history(self):
        self.history.return_value = NO_SWEEPS._replace(read_at=datetime.now(timezone.utc))
        first = self.started_farmer(client=self.client)
        first.cycle()
        kept = dict(self.resting)
        self.assertEqual(len(kept), 4)
        first.shutdown("test", cancel=False)
        self.client.reset_mock()
        self.history.reset_mock()

        second = self.started_farmer(read_histories=False, client=self.client)
        second.cycle()
        self.history.assert_not_called()
        self.client.cancel_orders.assert_not_called()
        self.client.post_orders.assert_not_called()
        self.assertEqual(self.resting, kept)


class ParserTest(unittest.TestCase):
    def test_keep_orders_and_heartbeat_are_rejected_together(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            build_parser().parse_args(["--keep-orders", "--heartbeat"])
        for flag in ("--keep-orders", "--heartbeat"):
            build_parser().parse_args([flag])


if __name__ == "__main__":
    unittest.main()
