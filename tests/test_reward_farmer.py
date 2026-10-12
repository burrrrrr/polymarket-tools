import functools
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from farm import farm_books, farm_db, fill_cost, reward_farmer, reward_screener
from farm.farm_db import CommandKind
from farm.reward_farmer import (
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
    around_kept_bid,
    ask_dropped,
    build_parser,
    capital_room,
    config_from,
    drop_ask,
    ev_cap,
    farm_position,
    fill_events,
    keep_failure,
    market_outlook,
    naked_allowed,
    next_backoff,
    order_score,
    quotes_at,
    ratchet_baseline,
    reconcile,
    rewards_active,
    sole_bid_target,
    step_state,
    strip_own,
    supported,
    target_orders,
    value_of,
    without_backed_off,
)
from farm.exit_value import TAPE_ROWS_FARMER, BuyTape, Piece, Sweep, tape_to_json
from farm.fill_cost import DISTANCES, FillCurve, History
from farm.reward_screener import (
    Read,
    Zones,
    fav_bid_candidates,
    over_max_return,
    round_size_down,
    round_size_up,
    under_free_return,
)
from polymarket.books import Levels
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
        # 100 days to the end date, where a longshot bid may rest up to 4.8c
        # and offers under 5% a year up to 1.3c. No midpoint has been watched,
        # so the free zone is closed and the naked budget limits every price.
        zones=Zones(100 / 365.25),
    )
    fields.update(changes)
    return FarmMarket(**fields)


def steady(mid, days=100):
    """The zones of a market `days` from its end date whose midpoint has held
    at mid for the dwell: the free zone is open."""
    return Zones(days / 365.25, mid)


# Favorite mid 0.99: the cheapest scoring longshot bid is 0.1c, which 100
# days out offers a favorite buyer 0.4% a year.
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
    DISTANCES, the last one held to the end."""
    return FillCurve(rates + rates[-1:] * (len(DISTANCES) - len(rates)), True, 0.0)


# A quiet market: a sweep reaches a bid at the midpoint about once a month,
# and one 3c under about once a quarter.
QUIET = curve(0.033, 0.03, 0.027, 0.024, 0.021, 0.018, 0.015, 0.011)
# A sweep reaches 1c under the midpoint every day, and never 1.5c.
SWEPT = curve(1.0, 1.0, 1.0, 1.0, 1.0, 0.0)
# A scan's fill prior, and a market history with no sweeps in 60 days.
PRIOR = fill_cost.DEFAULT_PRIOR._replace(markets=40, at=NOW)
NO_SWEEPS = History((0,) * len(DISTANCES), 60.0, NOW, 0.90, 0, 0.0)
NO_BUYS = BuyTape((), NOW, NOW.timestamp() - 60 * 86400)


def outlook_with(fm, fills, cfg=CFG):
    """The market's Outlook at the scan's rate, with these fills."""
    return market_outlook(fm, None, None, PRIOR, cfg, NOW)._replace(fills=fills)


def plan(fm, book, position=Position(), cfg=CFG, naked=True, fills=QUIET, resting=()):
    """target_orders against a book with these orders resting, valued at the
    scan's rate with these fills."""
    return target_orders(fm, book, position, cfg, naked, outlook_with(fm, fills, cfg),
                         resting=resting)


def bid_value(fm, book, price, size=None, position=Position(), fills=QUIET, fill_mid=None):
    """The QuoteValue of the orders planned around a favorite bid at price,
    of all the capital left unless a size is given."""
    target = quotes_at(fm, book, position, CFG, True, price, size, fill_mid)
    return value_of(target, fm, book, position, CFG, outlook_with(fm, fills), fill_mid)


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

    def test_a_free_long_bid_over_the_naked_budget_leaves_the_fav_bid_whole(self):
        # The 0.1c bid costs $0.44. Neither a $0.30 budget nor a $5 one
        # that 10 longshot shares held at $1 have used up limits it. 10
        # shares are under the 50-share minimum, so no exit rests for them.
        held = steady(0.99)
        used_up = (market(naked_budget_usd=5.0, zones=held),
                   Position(long_shares=10.0, long_avg=1.0))
        for fm, position in ((market(naked_budget_usd=0.3, zones=held), Position()), used_up):
            with self.subTest(budget=fm.naked_budget_usd):
                target = quotes_at(fm, FLOOR_BOOK, position, CFG, True, 0.979)
                intents = by_slot(target)
                fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
                self.assertEqual((fav.price, fav.size), (0.979, 510.72))
                self.assertEqual((low.price, low.size), (0.001, 435.17))
                q_one, q_two = q_sums(target)
                self.assertGreaterEqual(q_two, q_one)

    def test_the_long_bid_stays_at_its_free_price_as_the_fav_bid_outgrows_the_budget(self):
        # $1 buys 1,000 shares at 0.1c, which balance 1,173 favorite shares
        # at 97.9c. The longshot bid stays at 0.1c and covers the whole
        # favorite bid on both sides of that size.
        for capital in (100.0, 1000.0, 1200.0, 20000.0):
            with self.subTest(capital=capital):
                fm = market(capital=capital, naked_budget_usd=1.0, zones=steady(0.99))
                target = quotes_at(fm, FLOOR_BOOK, Position(), CFG, True, 0.979)
                intents = by_slot(target)
                self.assertEqual(intents[Slot.LONG_BID].price, 0.001)
                self.assertEqual(intents[Slot.FAV_BID].size, round_size_down(capital / 0.979))
                q_one, q_two = q_sums(target)
                self.assertGreaterEqual(q_two, q_one)

    def test_the_lowest_tick_costing_too_much_gives_way_to_the_next_free_price(self):
        # At a 0.9655 midpoint with v 3.5c a 0.1c share scores 0.0018, $0.54
        # for each unit of score, just over the $0.50 a bid sized without
        # the budget may cost. A 0.2c share scores 0.0051, $0.39 a unit, and
        # 100 days out offers 0.7% a year: 50,282.71 of them, $100.57, cover
        # the 523.56 favorite shares $500 buys, on a $25 budget.
        book = Levels(bids=[(0.961, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)
        fm = market(p_true=0.98, naked_budget_usd=25.0, zones=steady(0.9655))
        intents = by_slot(quotes_at(fm, book, Position(), CFG, True, 0.955))
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual((low.price, low.size), (0.002, 50282.71))
        self.assertEqual(fav.size, 523.56)
        self.assertGreaterEqual(low.score * low.size, fav.score * fav.size)
        # With the free zone closed the longshot bid rests at 1.1c within
        # its $25, and the favorite bid is trimmed to match.
        intents = by_slot(quotes_at(fm._replace(zones=Zones(100 / 365.25)), book, Position(),
                                    CFG, True, 0.955))
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual((low.price, low.size), (0.011, 2272.72))
        self.assertLessEqual(low.price * low.size, 25.0)
        self.assertEqual(fav.size, 500.73)
        self.assertAlmostEqual(fav.score * fav.size, low.score * low.size,
                               delta=fav.score * 0.01)

    def test_a_filled_free_bid_does_not_grow_the_next_one(self):
        # At a 0.9495 midpoint with v 5.5c a 0.1c share scores 0.01, and
        # 36,402.87 shares, $36.40, cover a 531.91-share favorite bid at 94c.
        # Once they fill, their exit at 6c scores 24,900 of Q_one, which a
        # bid sized without the budget would cover with 2.5 million shares.
        # The budget, less the $36.40 held, limits every price again:
        # nothing on $10, the $63.60 left on $100.
        book = Levels(bids=[(0.945, 1000.0)], asks=[(0.954, 1000.0)], tick=0.001)
        fm = market(naked_budget_usd=10.0, v=0.055, p_true=0.98, zones=steady(0.9495))
        first = by_slot(quotes_at(fm, book, Position(), CFG, True, 0.94))
        self.assertEqual(first[Slot.FAV_BID].size, 531.91)
        self.assertEqual((first[Slot.LONG_BID].price, first[Slot.LONG_BID].size),
                         (0.001, 36402.87))
        filled = Position(long_shares=36402.87, long_avg=0.001)
        after = by_slot(quotes_at(fm, book, filled, CFG, True, 0.94))
        self.assertEqual(set(after), {Slot.LONG_ASK})
        self.assertGreater(after[Slot.LONG_ASK].score * after[Slot.LONG_ASK].size, 24000.0)
        roomy = by_slot(quotes_at(fm._replace(naked_budget_usd=100.0), book, filled, CFG, True,
                                  0.94))
        self.assertEqual((roomy[Slot.LONG_BID].price, roomy[Slot.LONG_BID].size),
                         (0.001, 63597.13))

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
        # the bid daily. A fill there loses 2.5c a share, more than a $10 pool
        # pays for 507 shares a day, so the bid steps down to 1.5c under or
        # more, which none reach.
        fm = market(rate_per_day=10.0)
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
        self.assertEqual((outlook.rate_per_day, outlook.years_left, outlook.hurdle_rate),
                         (40.0, 1.0, CFG.hurdle_rate))
        self.assertEqual(outlook.fills, fill_cost.fill_curve(NO_SWEEPS, PRIOR, CFG.min_fav))
        self.assertTrue(outlook.fills.from_history)
        unread = market_outlook(fm, {"0xother": 40.0}, None, PRIOR, CFG, NOW)
        self.assertEqual(unread.rate_per_day, 100.0)
        self.assertEqual(unread.fills, fill_cost.fill_curve(None, PRIOR, CFG.min_fav))

    def test_the_outlook_drops_a_history_read_at_another_floor_unless_any_floor(self):
        fm, lower = market(), CFG._replace(min_fav=0.85)
        self.assertFalse(market_outlook(fm, None, NO_SWEEPS, PRIOR, lower, NOW).fills.from_history)
        kept = market_outlook(fm, None, NO_SWEEPS, PRIOR, lower, NOW, any_floor=True)
        self.assertEqual(kept.fills, fill_cost.fill_curve(NO_SWEEPS, PRIOR, 0.90))
        unread = market_outlook(fm, None, None, PRIOR, lower, NOW, any_floor=True)
        self.assertEqual(unread.fills, fill_cost.fill_curve(None, PRIOR, 0.85))


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
        target = plan(self.fm, FLOOR_BOOK, resting=orders)
        return reconcile(target, orders, self.fm, Position(), CFG)

    def rest(self, price, size=507.61):
        """A favorite bid resting at price, and the target planned with it resting."""
        order = live("a", Slot.FAV_BID, price, size)
        return order, plan(self.fm, FLOOR_BOOK, resting=[order])

    def keep(self, price, size=507.61):
        order, target = self.rest(price, size)
        return keep_failure(order, self.intents[Slot.FAV_BID], target, self.fm, Position(), CFG)

    def long_keep(self, price, size, cfg=CFG):
        order = live("b", Slot.LONG_BID, price, size)
        return keep_failure(order, self.intents[Slot.LONG_BID], self.target, self.fm, Position(), cfg)

    def test_keeps_orders_within_hysteresis(self):
        orders = [
            live("a", Slot.FAV_BID, 0.984, 450.0),
            live("b", Slot.LONG_BID, 0.001, 700.0),
        ]
        self.assertEqual(self.run_reconcile(orders), [])

    def test_a_long_bid_covering_more_than_the_need_but_under_twice_stays(self):
        orders = [
            live("a", Slot.FAV_BID, 0.985, 507.61),
            live("b", Slot.LONG_BID, 0.001, 900.0),
        ]
        self.assertEqual(self.run_reconcile(orders), [])

    def test_a_long_bid_covering_most_of_the_need_stays(self):
        # 600 of 675.82 shares cover 0.89 of the need, over the 0.8 keep ratio.
        self.assertIsNone(self.long_keep(0.001, 600.0))

    def test_a_long_bid_short_of_the_need_is_replaced(self):
        # 500 shares cover 0.74 of the need.
        self.assertEqual(self.long_keep(0.001, 500.0), "short of the cover needed")

    def test_a_long_bid_the_clob_lists_a_cent_short_stays(self):
        # The CLOB lists some orders 0.01 share under the size placed. Held to
        # the whole need, that share step is the only slack.
        exact = CFG._replace(keep_ratio=1.0)
        size = self.intents[Slot.LONG_BID].size
        self.assertIsNone(self.long_keep(0.001, round(size - 0.01, 2), exact))
        self.assertEqual(self.long_keep(0.001, round(size - 0.02, 2), exact),
                         "short of the cover needed")

    def test_a_long_bid_at_the_lowest_tick_covering_over_twice_the_need_stays(self):
        # At 0.1c the extra cover costs a few cents of naked budget.
        self.assertIsNone(self.long_keep(0.001, 1400.0))

    def test_a_budgeted_long_bid_covering_over_twice_the_need_is_replaced(self):
        fm = market(naked_budget_usd=100.0)
        target = plan(fm, BUDGET_BOOK)
        low = by_slot(target)[Slot.LONG_BID]
        self.assertGreater(low.price, 0.001)
        order = live("b", Slot.LONG_BID, low.price, round(low.size * 2.1, 2))
        self.assertEqual(keep_failure(order, low, target, fm, Position(), CFG),
                         "over twice the cover needed")

    def test_a_free_long_bid_above_the_lowest_tick_covering_over_twice_the_need_stays(self):
        # With BUDGET_BOOK's midpoint steady at 0.96 the plan rests its
        # longshot bid at 1c, the cheapest price costing at most $0.50 a
        # unit of score there, free of the budget. A tick of mid moves what
        # a share that near the band edge scores by half, so a bid sized a
        # tick lower covers over twice the need now. It stays: it is already
        # bounded by what it cost when it was sized. With the free zone
        # closed the same bid is one the budget limits, and it goes.
        fm = market(p_true=0.98, naked_budget_usd=25.0, zones=steady(0.96))
        target = plan(fm, BUDGET_BOOK)
        low = by_slot(target)[Slot.LONG_BID]
        self.assertEqual(low.price, 0.01)
        order = live("b", Slot.LONG_BID, low.price, round(low.size * 2.1, 2))
        self.assertIsNone(keep_failure(order, low, target, fm, Position(), CFG))
        closed = fm._replace(zones=Zones(100 / 365.25))
        self.assertEqual(keep_failure(order, low, target, closed, Position(), CFG),
                         "over twice the cover needed")

    def test_a_free_long_bid_over_the_naked_budget_stays(self):
        # 675.82 shares at 0.1c cost $0.68: over a $0.50 budget, and over a
        # $25 one that 30 longshot shares held at $1 have used up. 30 shares
        # are under the 50-share minimum, so no exit rests for them.
        order = live("b", Slot.LONG_BID, 0.001, 675.82)
        low = self.intents[Slot.LONG_BID]
        tight = market(naked_budget_usd=0.5, zones=steady(0.99))
        self.assertIsNone(keep_failure(order, low, self.target, tight, Position(), CFG))
        dust = Position(long_shares=30.0, long_avg=1.0)
        held = self.fm._replace(zones=steady(0.99))
        self.assertIsNone(keep_failure(order, low, self.target, held, dust, CFG))

    def test_a_free_long_bid_drifting_out_of_the_free_zone_goes_back_under_the_budget(self):
        # 100 days out 0.1c offers 0.4% a year. 7 days out it offers 5.4%,
        # and the $0.68 bid is over its $0.50 budget.
        order = live("b", Slot.LONG_BID, 0.001, 675.82)
        low = self.intents[Slot.LONG_BID]
        tight = market(naked_budget_usd=0.5, zones=steady(0.99, days=7))
        self.assertEqual(keep_failure(order, low, self.target, tight, Position(), CFG),
                         "over the naked budget")
        self.assertIsNone(keep_failure(order, low, self.target, tight._replace(
            zones=steady(0.99, days=8)), Position(), CFG))

    def test_a_long_bid_is_held_to_the_budget_while_longshot_shares_await_an_exit(self):
        # 100 longshot shares held rest an exit. At 30c they have used up the
        # $25, and the budget limits the 0.1c bid as it limits any other. At
        # 10c they leave $15, which the $0.68 bid fits.
        order = live("b", Slot.LONG_BID, 0.001, 675.82)
        low = self.intents[Slot.LONG_BID]
        fm = self.fm._replace(zones=steady(0.99))
        used_up = Position(long_shares=100.0, long_avg=0.3)
        self.assertEqual(keep_failure(order, low, self.target, fm, used_up, CFG),
                         "over the naked budget")
        room_left = Position(long_shares=100.0, long_avg=0.1)
        self.assertIsNone(keep_failure(order, low, self.target, fm, room_left, CFG))

    def test_a_long_bid_outside_the_free_zone_over_the_naked_budget_is_replaced(self):
        # On BUDGET_BOOK the bid rests at 1.4c x 1785.71, all of its $25. A
        # $20 budget is $5 short of it, and so is $25 with 10 longshot shares
        # held at $1.
        fm = market(p_true=0.98, naked_budget_usd=25.0)
        target = plan(fm, BUDGET_BOOK)
        low = by_slot(target)[Slot.LONG_BID]
        self.assertEqual((low.price, low.size), (0.014, 1785.71))
        order = live("b", Slot.LONG_BID, low.price, low.size)
        self.assertIsNone(keep_failure(order, low, target, fm, Position(), CFG))
        self.assertEqual(
            keep_failure(order, low, target, fm._replace(naked_budget_usd=20.0), Position(),
                         CFG),
            "over the naked budget")
        held = Position(long_shares=10.0, long_avg=1.0)
        self.assertEqual(keep_failure(order, low, target, fm, held, CFG),
                         "over the naked budget")

    def test_a_long_bid_on_a_one_cent_tick_is_free_only_far_from_the_end_date(self):
        # On a 1c tick the lowest price is 1c, where 675.82 shares cost
        # $6.76, over a $5 budget. 100 days out 1c offers 3.7% a year and the
        # budget leaves the bid alone. 60 days out it offers 6.3%.
        order = live("b", Slot.LONG_BID, 0.01, 675.82)
        low = self.intents[Slot.LONG_BID]
        coarse = market(tick=0.01, naked_budget_usd=5.0, zones=steady(0.99))
        self.assertIsNone(keep_failure(order, low, self.target, coarse, Position(), CFG))
        self.assertEqual(
            keep_failure(order, low, self.target, coarse._replace(zones=steady(0.99, days=60)),
                         Position(), CFG),
            "over the naked budget")

    def test_a_long_bid_up_to_two_ticks_over_the_min_score_price_stays(self):
        # At a 0.9355 midpoint with v 6.5c the cheapest longshot price scoring
        # 0.1 is 2.1c, and the plan covers at 1.4c. A bid placed when the mid
        # sat two ticks lower rests at 2.3c; it still covers, so it stays.
        book = Levels(bids=[(0.93, 1000.0)], asks=[(0.936, 1000.0)], tick=0.001)
        fm = market(v=0.065, min_size=50.0, naked_budget_usd=30.0, p_true=0.96, capital=100.0)
        cfg = CFG._replace(ev_margin=0.02)
        target = plan(fm, book, cfg=cfg)
        low = by_slot(target)[Slot.LONG_BID]
        self.assertEqual(low.price, 0.014)

        def keep_at(price):
            size = round_size_up(low.score * low.size / order_score(Slot.LONG_BID, price, target.mid, fm.v))
            return keep_failure(live("b", Slot.LONG_BID, price, size), low, target, fm, Position(), cfg)

        self.assertIsNone(keep_at(0.023))
        self.assertEqual(keep_at(0.024), "above the target price")

    def test_a_long_bid_priced_above_the_target_is_replaced(self):
        # The min-score price on FLOOR_BOOK is 0.1c, so 0.4c is three ticks over.
        self.assertEqual(self.long_keep(0.004, 675.82), "above the target price")

    def test_balancing_orders_are_planned_around_the_kept_favorite_bid(self):
        kept, target = self.rest(0.981)
        around = around_kept_bid(target, [kept], self.fm, FLOOR_BOOK, Position(), CFG, True)
        intents = by_slot(around)
        self.assertEqual((intents[Slot.FAV_BID].price, intents[Slot.FAV_BID].size), (0.981, 507.61))
        q_one, q_two = q_sums(around)
        self.assertGreaterEqual(q_two, q_one - 1e-9)
        self.assertLess(intents[Slot.LONG_BID].size, self.intents[Slot.LONG_BID].size)
        self.assertEqual(around.fav, target.fav)
        orders = [kept, live("b", Slot.LONG_BID, 0.001, intents[Slot.LONG_BID].size)]
        self.assertEqual(reconcile(around, orders, self.fm, Position(), CFG), [])

    def test_a_target_whose_favorite_bid_is_not_kept_is_left_as_planned(self):
        order, target = self.rest(0.95)
        around = around_kept_bid(target, [order], self.fm, FLOOR_BOOK, Position(), CFG, True)
        self.assertIs(around, target)

    def test_a_deeper_bid_earning_most_of_the_best_net_stays(self):
        # 98.1c scores (2.6 / 3.5)^2 = 0.55, under 0.8 of the target's 0.73,
        # but nets within a fifth of the best price's.
        self.assertLess(0.552, CFG.keep_ratio * self.intents[Slot.FAV_BID].score)
        self.assertIsNone(self.keep(0.981))

    def test_a_bid_trailing_the_best_net_by_more_than_the_keep_ratio_is_replaced(self):
        order, target = self.rest(0.979)
        self.assertLess(target.fav.resting["a"].net_per_day, 0.8 * target.fav.best.net_per_day)
        self.assertEqual(self.keep(0.979), "net below keep ratio")

    def test_a_bid_netting_no_more_than_the_plan_without_it_is_replaced(self):
        order, target = self.rest(0.981)
        no_better = target.fav._replace(baseline_per_day=target.fav.resting["a"].net_per_day)
        self.assertEqual(
            keep_failure(order, self.intents[Slot.FAV_BID], target._replace(fav=no_better),
                         self.fm, Position(), CFG),
            "net below keep ratio")

    def test_a_bid_above_the_candidates_is_replaced(self):
        self.assertEqual(self.keep(0.99), "outside the bid candidates")

    def test_a_resting_bid_is_valued_at_its_own_price_and_size(self):
        order, target = self.rest(0.981, 300.0)
        value = target.fav.resting["a"]
        self.assertEqual((value.price, value.size), (0.981, 300.0))
        self.assertEqual(value, bid_value(self.fm, FLOOR_BOOK, 0.981, 300.0))

    def test_a_bid_the_plan_was_not_told_rests_is_replaced(self):
        order = live("a", Slot.FAV_BID, 0.985, 507.61)
        self.assertEqual(
            keep_failure(order, self.intents[Slot.FAV_BID], self.target, self.fm, Position(),
                         CFG),
            "outside the bid candidates")

    def test_a_bid_the_budget_cannot_balance_is_held_to_what_it_balances(self):
        # On BUDGET_BOOK a $10 naked budget trims the favorite bid to what
        # the longshot bid above the floor covers, so the plan rests 124.91
        # shares at the target's 95.5c and 131.68 at 95.4c, and 95.4c nets
        # within 3% of the best.
        fm = market(p_true=0.98, naked_budget_usd=10.0)

        def keep(size):
            order = live("a", Slot.FAV_BID, 0.954, size)
            target = plan(fm, BUDGET_BOOK, resting=[order])
            fav = by_slot(target)[Slot.FAV_BID]
            self.assertEqual((fav.price, fav.size), (0.955, 124.91))
            return keep_failure(order, fav, target, fm, Position(), CFG)

        self.assertIsNone(keep(131.68))
        self.assertEqual(keep(170.0), "size out of range")

    def test_a_bid_over_the_capital_is_replaced(self):
        # $500 buys 507.61 shares at 98.5c. A bid a quarter over the capital
        # stays, as one a quarter over its planned size did.
        self.assertIsNone(self.keep(0.985, 634.0))
        self.assertEqual(self.keep(0.985, 635.0), "over the capital")
        self.assertEqual(capital_room(self.fm, Position(fav_shares=100.0, fav_avg=0.97)), 403.0)

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

    def test_asks_too_thin_to_read_are_no_ask_drop(self):
        self.assertFalse(ask_dropped(asked(1.0), None, NOW, CFG))
        self.assertEqual(step_state(QUOTING, observe(ask=None, recent_asks=asked(1.0)), NOW, CFG),
                         QUOTING)

    def test_a_sole_bid_takes_the_place_of_below_range_and_of_a_one_sided_book(self):
        sole_bid = State(Phase.SOLE_BID)
        for state in (QUOTING, State(Phase.BELOW_RANGE), sole_bid):
            for mid in (0.885, 0.96, None):
                with self.subTest(state=state, mid=mid):
                    obs = observe(mid=mid, sole_bid=True)
                    self.assertEqual(step_state(state, obs, NOW, CFG), sole_bid)

    def test_a_sole_bid_ends_when_its_orders_cannot_rest(self):
        sole_bid = State(Phase.SOLE_BID)
        self.assertEqual(step_state(sole_bid, observe(), NOW, CFG), QUOTING)
        self.assertEqual(step_state(sole_bid, observe(mid=None), NOW, CFG), QUOTING)
        self.assertEqual(step_state(sole_bid, observe(mid=0.885), NOW, CFG),
                         State(Phase.BELOW_RANGE))

    def test_an_ask_drop_a_pause_and_lost_rewards_all_beat_a_sole_bid(self):
        sole_bid = State(Phase.SOLE_BID)
        dropped = observe(mid=None, sole_bid=True, ask=0.97, recent_asks=asked(1.0))
        cooling = step_state(sole_bid, dropped, NOW, CFG)
        self.assertEqual(cooling.phase, Phase.COOLDOWN)
        self.assertEqual(step_state(cooling, observe(sole_bid=True), NOW, CFG), cooling)
        self.assertEqual(
            step_state(sole_bid, observe(sole_bid=True, paused=True), NOW, CFG).phase,
            Phase.PAUSED)
        self.assertEqual(
            step_state(sole_bid, observe(sole_bid=True, rewards_active=False), NOW, CFG).phase,
            Phase.UNREWARDED)

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


# The lone other bid of a thin book has left: 80c is all that is bid under a
# 97c ask, so the midpoint without us is 88.5c. With a 5.5c max spread a 0.1c
# longshot bid scores while the favorite midpoint is over 94.4c, which a bid
# of ours over 91.8c makes: 91.9c is the lowest that pays and 92.1c the target.
THIN_BOOK = Levels(bids=[(0.80, 500.0)], asks=[(0.97, 1000.0)], tick=0.001)
THIN = market(v=0.055, min_size=100.0, sole_bid=True)


def sole(orders=(), fm=THIN, book=THIN_BOOK, position=Position(), cfg=CFG):
    return sole_bid_target(fm, book, position, cfg, orders)


class SoleBidTest(unittest.TestCase):
    def test_a_thin_book_is_unsupported_below_range_or_with_no_scoring_price(self):
        self.assertTrue(supported(FLOOR_BOOK, market(), CFG))
        self.assertFalse(supported(THIN_BOOK, THIN, CFG))
        # A 91c midpoint, but 85.5c is the highest bid allowed and scores nothing.
        lead_capped = THIN_BOOK._replace(bids=[(0.85, 500.0)])
        self.assertEqual(fav_bid_candidates(lead_capped, 0.055, 0.001, 100.0, CFG.max_lead), [])
        self.assertFalse(supported(lead_capped, THIN, CFG))
        self.assertFalse(supported(THIN_BOOK._replace(bids=[]), THIN, CFG))

    def test_support_can_be_asked_for_a_margin_over_min_fav(self):
        book = Levels(bids=[(0.86, 500.0)], asks=[(0.944, 1000.0)], tick=0.001)
        self.assertTrue(supported(book, THIN, CFG))
        self.assertFalse(supported(book, THIN, CFG, margin=0.005))

    def test_the_bid_rests_two_ticks_over_the_lowest_price_that_keeps_the_longshot_scoring(self):
        target = sole()
        intents = by_slot(target)
        self.assertEqual(set(intents), {Slot.FAV_BID, Slot.LONG_BID})
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual((fav.price, fav.size), (0.921, round_size_down(500.0 / 0.921)))
        self.assertEqual((low.price, low.size), (0.001, 25000.0))
        self.assertAlmostEqual(target.mid, 0.9455)
        self.assertGreater(min(fav.score, low.score), 0)
        self.assertEqual(target.sole_kept, frozenset())
        # A bid at 91.8c leaves the longshot bid on the band's edge, scoring nothing.
        self.assertEqual(order_score(Slot.LONG_BID, 0.001, (0.918 + 0.97) / 2, 0.055), 0.0)
        self.assertGreater(order_score(Slot.LONG_BID, 0.001, (0.919 + 0.97) / 2, 0.055), 0)

    def test_it_rises_a_tick_for_each_tick_the_ask_falls(self):
        lower_ask = THIN_BOOK._replace(asks=[(0.969, 1000.0)])
        self.assertEqual(by_slot(sole(book=lower_ask))[Slot.FAV_BID].price, 0.922)

    def test_a_book_with_no_other_bid_at_all_rests_the_same_orders(self):
        empty = THIN_BOOK._replace(bids=[])
        self.assertEqual(sole(book=empty).intents, sole().intents)

    def test_the_resting_longshot_bid_stays_whatever_it_costs(self):
        resting = [live("low", Slot.LONG_BID, 0.001, 40000.0)]
        target = sole(resting)
        low = by_slot(target)[Slot.LONG_BID]
        self.assertEqual((low.price, low.size), (0.001, 40000.0))
        self.assertEqual(target.sole_kept, frozenset({"low"}))
        actions = reconcile(target, resting, THIN, Position(), CFG)
        self.assertEqual([a.intent.slot for a in actions], [Slot.FAV_BID])

    def test_a_dearer_resting_longshot_bid_lets_the_favorite_bid_rest_lower(self):
        # At 1.8c the longshot bid scores down to a 92.7c midpoint.
        target = sole([live("low", Slot.LONG_BID, 0.018, 600.0)])
        self.assertEqual(by_slot(target)[Slot.FAV_BID].price, 0.887)

    def test_a_longshot_bid_under_the_minimum_or_over_the_return_limit_is_replaced(self):
        self.assertTrue(over_max_return(0.049, THIN.zones.years_left))
        for order in (live("low", Slot.LONG_BID, 0.001, 99.0),
                      live("low", Slot.LONG_BID, 0.049, 500.0)):
            with self.subTest(order=order):
                target = sole([order])
                self.assertEqual(target.sole_kept, frozenset())
                self.assertEqual(by_slot(target)[Slot.LONG_BID].price, 0.001)
                actions = reconcile(target, [order], THIN, Position(), CFG)
                self.assertEqual(actions[0].order_ids, ("low",))

    def test_a_longshot_bid_left_off_the_grid_by_a_tick_change_is_replaced_on_it(self):
        coarse = THIN._replace(tick=0.01)
        resting = [live("low", Slot.LONG_BID, 0.001, 25000.0)]
        target = sole(resting, fm=coarse, book=THIN_BOOK._replace(tick=0.01))
        self.assertEqual(target.sole_kept, frozenset())
        self.assertEqual(by_slot(target)[Slot.LONG_BID].price, 0.01)

    def test_a_resting_favorite_bid_stays_from_the_lowest_paying_price_to_a_cent_over_target(self):
        for price, kept in ((0.918, False), (0.919, True), (0.921, True), (0.931, True),
                            (0.932, False)):
            with self.subTest(price=price):
                resting = [live("fav", Slot.FAV_BID, price, 300.0)]
                target = sole(resting)
                fav = by_slot(target)[Slot.FAV_BID]
                self.assertEqual((fav.price, fav.size), (price, 300.0) if kept
                                 else (0.921, round_size_down(500.0 / 0.921)))
                self.assertEqual(target.sole_kept, frozenset({"fav"} if kept else ()))
                cancelled = [i for a in reconcile(target, resting, THIN, Position(), CFG)
                             if isinstance(a, Cancel) for i in a.order_ids]
                self.assertEqual(cancelled, [] if kept else ["fav"])

    def test_a_resting_favorite_bid_over_the_capital_is_replaced(self):
        target = sole([live("fav", Slot.FAV_BID, 0.921, 700.0)])
        self.assertEqual(target.sole_kept, frozenset())
        self.assertEqual(by_slot(target)[Slot.FAV_BID].size, round_size_down(500.0 / 0.921))

    def test_the_bid_never_tops_the_ev_cap_or_crosses_an_ask_of_any_size(self):
        capped = sole(fm=THIN._replace(p_true=0.93))
        self.assertEqual(by_slot(capped)[Slot.FAV_BID].price, 0.920)
        self.assertIsNone(sole(fm=THIN._replace(p_true=0.90)))
        dust = THIN_BOOK._replace(asks=[(0.92, 5.0), (0.97, 1000.0)])
        self.assertEqual(by_slot(sole(book=dust))[Slot.FAV_BID].price, 0.919)
        self.assertIsNone(sole(book=THIN_BOOK._replace(asks=[(0.88, 5.0), (0.97, 1000.0)])))

    def test_nothing_rests_without_an_ask_a_longshot_bid_or_the_capital_for_a_minimum(self):
        self.assertIsNone(sole(book=THIN_BOOK._replace(asks=[(0.97, 99.0)])))
        self.assertIsNone(sole(fm=THIN._replace(naked_budget_usd=0.05)))
        self.assertIsNone(sole(fm=THIN._replace(capital=80.0)))
        # An ask this low leaves no bid under it that keeps the midpoint up.
        self.assertIsNone(sole(book=THIN_BOOK._replace(asks=[(0.93, 1000.0)])))

    # Under a 94c ask no bid keeps a 0.1c longshot bid in the band: it would
    # have to rest over 94.8c. THIN is 100 days out, where a longshot bid
    # offers the favorite buyer under 5% a year up to 1.3c.
    LOW_ASK = THIN_BOOK._replace(asks=[(0.94, 1000.0)])

    def test_the_longshot_bid_rises_to_the_top_of_the_free_zone_only_when_it_must(self):
        self.assertEqual(by_slot(sole())[Slot.LONG_BID].price, 0.001)
        resting = [live("low", Slot.LONG_BID, 0.001, 40000.0)]
        target = sole(resting, book=self.LOW_ASK)
        fav, low = by_slot(target)[Slot.FAV_BID], by_slot(target)[Slot.LONG_BID]
        self.assertTrue(under_free_return(0.013, THIN.zones.years_left))
        self.assertFalse(under_free_return(0.014, THIN.zones.years_left))
        self.assertEqual((low.price, low.size), (0.013, round_size_down(25.0 / 0.013)))
        # It scores down to a 93.2c midpoint, so 92.5c is the lowest favorite bid.
        self.assertEqual(fav.price, 0.927)
        self.assertGreater(min(fav.score, low.score), 0)
        self.assertEqual(target.sole_kept, frozenset())
        actions = reconcile(target, resting, THIN, Position(), CFG)
        self.assertEqual(actions[0].order_ids, ("low",))
        self.assertEqual({a.intent.slot for a in actions[1:]}, {Slot.FAV_BID, Slot.LONG_BID})

    def test_a_longshot_bid_that_rose_stays_as_it_rests(self):
        resting = [live("low", Slot.LONG_BID, 0.013, 1923.07),
                   live("fav", Slot.FAV_BID, 0.927, 539.37)]
        target = sole(resting, book=self.LOW_ASK)
        self.assertEqual(target.sole_kept, frozenset({"low", "fav"}))
        self.assertEqual(reconcile(target, resting, THIN, Position(), CFG), [])

    def test_a_raised_longshot_bid_also_lets_the_bid_under_a_low_ev_cap_or_small_capital(self):
        for fm in (THIN._replace(p_true=0.928), THIN._replace(capital=90.0)):
            with self.subTest(fm=fm):
                intents = by_slot(sole(fm=fm))
                self.assertEqual((intents[Slot.FAV_BID].price, intents[Slot.LONG_BID].price),
                                 (0.897, 0.013))

    def test_the_longshot_bid_rises_no_higher_than_the_naked_budget_buys_a_minimum(self):
        low = by_slot(sole(fm=THIN._replace(naked_budget_usd=1.0), book=self.LOW_ASK))[
            Slot.LONG_BID]
        self.assertEqual((low.price, low.size), (0.01, 100.0))

    def test_the_longshot_bid_rises_no_higher_than_keeps_the_midpoint_over_90c(self):
        # A year out the free zone reaches 4.7c, where the midpoint would be 89.95c.
        year_out = THIN._replace(zones=Zones(1.0))
        self.assertTrue(under_free_return(0.047, 1.0))
        target = sole(fm=year_out, book=self.LOW_ASK)
        self.assertEqual(by_slot(target)[Slot.LONG_BID].price, 0.046)
        self.assertAlmostEqual(target.mid, 0.9005)

    def test_a_longshot_bid_inside_the_spread_is_the_ask_the_midpoint_is_made_from(self):
        # 1.3c mirrors to a 98.7c favorite ask, under the book's 99.5c.
        wide = THIN_BOOK._replace(asks=[(0.995, 1000.0)])
        target = sole(fm=THIN._replace(p_true=0.90), book=wide)
        fav, low = by_slot(target)[Slot.FAV_BID], by_slot(target)[Slot.LONG_BID]
        self.assertEqual((fav.price, low.price), (0.88, 0.013))
        self.assertAlmostEqual(target.mid, (0.88 + 0.987) / 2)
        self.assertGreater(min(fav.score, low.score), 0)

    def test_a_midpoint_under_min_fav_rests_nothing(self):
        wide = THIN._replace(v=0.12)
        self.assertIsNotNone(sole(fm=wide, cfg=CFG._replace(min_fav=0.85)))
        self.assertIsNone(sole(fm=wide))

    def test_favorite_shares_held_rest_their_exit_and_the_bid_prices_off_it(self):
        held = Position(fav_shares=200.0, fav_avg=0.921)
        target = sole(position=held)
        intents = by_slot(target)
        ask, fav = intents[Slot.FAV_ASK], intents[Slot.FAV_BID]
        # A cent over the 94.4c midpoint the longshot bid needs, under the book's 97c.
        self.assertEqual((ask.price, ask.size), (0.954, 200.0))
        self.assertEqual(fav.price, 0.937)
        self.assertEqual(fav.size, round_size_down((500.0 - 200.0 * 0.921) / 0.937))
        self.assertAlmostEqual(target.mid, (0.937 + 0.954) / 2)
        self.assertGreater(intents[Slot.LONG_BID].score, 0)


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

    def test_the_exit_never_sells_more_than_the_wallet_holds(self):
        held = Position(fav_shares=199.457693, fav_avg=0.97)
        farm = farm_position(held, Baseline(fav=0.0, long=0.0))
        self.assertEqual(farm.fav_shares, 199.45)
        ask = by_slot(plan(market(), FLOOR_BOOK, farm))[Slot.FAV_ASK]
        self.assertLessEqual(ask.size, held.fav_shares)


# OpenSI on 2026-10-09 at 19:08 UTC without our orders, the day after a
# 2,715-share fill of our 93.9c bid. The mid was 94.8c before the fill.
OPENSI = market(v=0.065, min_size=20.0, p_true=0.97, naked_budget_usd=40.0, capital=4000.0)
OPENSI_BOOK = Levels(
    bids=[(0.912, 21.0), (0.901, 290.0), (0.9, 226.5), (0.896, 1664.79), (0.895, 888.0),
          (0.891, 40.0), (0.89, 509.54), (0.881, 1000.0), (0.88, 1000.0), (0.871, 10.0)],
    asks=[(0.938, 100.0), (0.941, 39.0), (0.946, 266.0), (0.947, 280.0), (0.948, 307.0),
          (0.95, 100.0), (0.951, 100.0), (0.952, 100.0), (0.953, 41.0), (0.959, 257.0),
          (0.966, 11.14), (0.967, 499.99), (0.968, 883.55), (0.969, 1631.28), (0.978, 5.0),
          (0.979, 5.85), (0.98, 530.0), (0.982, 766.46), (0.987, 8.59), (0.988, 416.6),
          (0.989, 1465.62), (0.99, 2280.1)],
    tick=0.001,
)
OPENSI_HELD = Position(fav_shares=2920.63, fav_avg=0.9413)


def opensi_ask(book=OPENSI_BOOK, held=OPENSI_HELD, fill_mid=0.948):
    target = quotes_at(OPENSI, book, held, CFG, True, None, fill_mid=fill_mid)
    return by_slot(target)[Slot.FAV_ASK].price


class ExitPricingTest(unittest.TestCase):
    """The favorite ask prices off the mid before the fill, when that is
    higher than the book's, plus the offset."""

    def test_the_ask_prices_off_the_mid_before_the_fill(self):
        self.assertEqual(opensi_ask(), 0.958)

    def test_without_a_fill_mid_the_ask_keeps_the_mid_rule(self):
        # 92.5c + 1c is under the exit floor, 94.13c average cost + 0.5c.
        self.assertEqual(opensi_ask(fill_mid=None), 0.947)

    def test_a_fill_mid_under_the_book_mid_leaves_the_ask_at_the_book_mid(self):
        book = OPENSI_BOOK._replace(asks=[(0.95, 500.0)])
        ask = opensi_ask(book=book, held=OPENSI_HELD._replace(fav_avg=0.90), fill_mid=0.90)
        # (0.912 + 0.95) / 2 = 0.931, plus 1c.
        self.assertEqual(ask, 0.941)

    def test_the_exit_floor_still_binds(self):
        self.assertEqual(opensi_ask(held=OPENSI_HELD._replace(fav_avg=0.97)), 0.975)


class AskKeepTest(unittest.TestCase):
    """OpenSI's exit targets 95.8c, off the 94.8c mid before its fill; a
    cheaper ask scores more against the 92.5c mid, so score alone keeps it."""

    def setUp(self):
        self.target = quotes_at(OPENSI, OPENSI_BOOK, OPENSI_HELD, CFG, True, None,
                                fill_mid=0.948)
        self.intent = by_slot(self.target)[Slot.FAV_ASK]

    def order(self, price):
        return live("a", Slot.FAV_ASK, price, self.intent.size)

    def keep(self, price):
        return keep_failure(self.order(price), self.intent, self.target, OPENSI, OPENSI_HELD,
                            CFG)

    def test_an_ask_resting_well_under_the_target_is_replaced(self):
        self.assertEqual(self.intent.price, 0.958)
        self.assertEqual(self.keep(0.947), "below the target price")
        actions = reconcile(self.target, [self.order(0.947)], OPENSI, OPENSI_HELD, CFG)
        placed = [a.intent for a in actions if isinstance(a, Place)]
        self.assertEqual(actions[0], Cancel(("a",)))
        self.assertEqual([(i.slot, i.price) for i in placed], [(Slot.FAV_ASK, 0.958)])

    def test_an_ask_within_two_ticks_under_the_target_stays(self):
        self.assertIsNone(self.keep(0.956))
        self.assertEqual(self.keep(0.955), "below the target price")

    def test_an_ask_over_the_target_is_judged_by_its_score(self):
        self.assertIsNone(self.keep(0.961))
        self.assertEqual(self.keep(0.962), "score below keep ratio")


# Qatar's shape once both its bids had filled and no favorite bid paid: the
# longshot exit shows in the favorite book as a bid, and 5.8c is the 94.2c
# best bid, a tick under the lowest ask.
QATAR = market(v=0.045, p_true=0.98, naked_budget_usd=10.0)
QATAR_BOOK = Levels(bids=[(0.942, 631.0), (0.94, 32.0), (0.939, 1031.5), (0.938, 50.0)],
                    asks=[(0.943, 5.0), (0.944, 5.0), (0.945, 5.0), (0.95, 16.0),
                          (0.951, 550.0)], tick=0.001)
QATAR_HELD = Position(fav_shares=266.08, fav_avg=0.9412, long_shares=303.02, long_avg=0.033)


class LongAskKeepTest(unittest.TestCase):
    """Qatar's exit rested at 6.2c, a 93.8c bid behind 1,694 shares, for 11
    minutes while its target was 5.8c: it scored 0.81 of the target."""

    def keep(self, price, book=QATAR_BOOK):
        target = quotes_at(QATAR, book, QATAR_HELD, CFG, False, None)
        order = live("a", Slot.LONG_ASK, price, QATAR_HELD.long_shares)
        return (keep_failure(order, by_slot(target)[Slot.LONG_ASK], target, QATAR, QATAR_HELD,
                             CFG),
                reconcile(target, [order], QATAR, QATAR_HELD, CFG))

    def test_an_exit_resting_behind_the_target_is_replaced(self):
        failure, actions = self.keep(0.062)
        self.assertEqual(failure, "above the target price")
        placed = [a.intent for a in actions if isinstance(a, Place)]
        self.assertEqual(actions[0], Cancel(("a",)))
        self.assertIn((Slot.LONG_ASK, 0.058), [(i.slot, i.price) for i in placed])

    def test_an_exit_one_tick_behind_the_target_is_replaced(self):
        self.assertEqual(self.keep(0.059)[0], "above the target price")

    def test_an_exit_at_the_target_stays(self):
        self.assertIsNone(self.keep(0.058)[0])

    def test_an_exit_ahead_of_a_target_that_fell_back_stays(self):
        fell = QATAR_BOOK._replace(bids=[(0.936, 631.0)] + QATAR_BOOK.bids[1:])
        target = quotes_at(QATAR, fell, QATAR_HELD, CFG, False, None)
        self.assertEqual(by_slot(target)[Slot.LONG_ASK].price, 0.059)
        self.assertIsNone(self.keep(0.058, fell)[0])

    def test_an_exit_stays_when_the_bid_it_leads_falls_two_ticks(self):
        """It rests at 6.0c, half a cent over a 93.5c best bid that wobbles
        to 93.3c and back. The plan's midpoint at 93.3c is under the order,
        which scored nothing there, and following the target both ways
        replaced it on every wobble."""
        wobbled = Levels(bids=[(0.933, 600.0), (0.93, 1000.0)], asks=[(0.941, 500.0)],
                         tick=0.001)
        target = quotes_at(QATAR, wobbled, QATAR_HELD, CFG, False, None)
        self.assertEqual(by_slot(target)[Slot.LONG_ASK].price, 0.062)
        failure, actions = self.keep(0.060, wobbled)
        self.assertIsNone(failure)
        self.assertFalse([a for a in actions if isinstance(a, Cancel)])


# Qatar's favorite risen to 97.4c: the front of the queue is a 97.4c bid, a
# 2.6c longshot ask, under the 3.3c the shares cost.
RALLIED_BOOK = Levels(bids=[(0.972, 600.0), (0.97, 1000.0)], asks=[(0.975, 500.0)], tick=0.001)


class LongExitFloorTest(unittest.TestCase):
    """The longshot exit sells for a profit or waits: never under the
    shares' average cost plus min_capture, 3.8c on Qatar's 3.3c."""

    def exit_price(self, held=QATAR_HELD, fav_price=None):
        target = quotes_at(QATAR, RALLIED_BOOK, held, CFG, fav_price is not None, fav_price)
        return by_slot(target)[Slot.LONG_ASK].price

    def test_the_exit_rests_at_the_floor_when_the_front_is_under_it(self):
        self.assertEqual(self.exit_price(), 0.038)

    def test_a_cost_between_ticks_rounds_the_floor_up(self):
        self.assertEqual(self.exit_price(QATAR_HELD._replace(long_avg=0.0334)), 0.039)

    def test_the_floor_holds_at_the_mirror_of_a_favorite_bid(self):
        self.assertEqual(self.exit_price(fav_price=0.972), 0.038)

    def test_an_exit_resting_under_the_floor_is_replaced(self):
        target = quotes_at(QATAR, RALLIED_BOOK, QATAR_HELD, CFG, False, None)
        order = live("a", Slot.LONG_ASK, 0.026, QATAR_HELD.long_shares)
        self.assertEqual(
            keep_failure(order, by_slot(target)[Slot.LONG_ASK], target, QATAR, QATAR_HELD, CFG),
            "below the exit floor")
        actions = reconcile(target, [order], QATAR, QATAR_HELD, CFG)
        placed = [a.intent for a in actions if isinstance(a, Place)]
        self.assertEqual(actions[0], Cancel(("a",)))
        self.assertIn((Slot.LONG_ASK, 0.038), [(i.slot, i.price) for i in placed])


# Newsom's shape: a 98c favorite 0.228 years out with a 97.4c mid and $2,000
# behind its bid. SURE is the same market held to be worth 99c, over the ask
# a fill would leave resting.
NEWSOM = market(p_true=0.98, end_date=NOW + timedelta(days=0.228 * 365.25), capital=2000.0)
SURE = NEWSOM._replace(p_true=0.99)
NEWSOM_BOOK = Levels(bids=[(0.969, 1000.0), (0.950, 5000.0)],
                     asks=[(0.979, 1000.0), (0.990, 5000.0)], tick=0.001)


def newsom_bid_value(position=Position(), fm=NEWSOM, fill_mid=None, price=0.960):
    return bid_value(fm, NEWSOM_BOOK, price, position=position, fill_mid=fill_mid)


def sold_at(sale_price, shares=2083.33, p_true=0.99):
    """What a share bought by a Newsom fill is worth today, sold at
    sale_price out of an inventory of `shares`."""
    return fill_cost.share_value(p_true, sale_price, fill_cost.sales_per_day(shares), 0.228,
                                 CFG.hurdle_rate)


class FillValueTest(unittest.TestCase):
    """A favorite bid's fill is valued sold at the favorite ask the farmer
    would rest for the inventory the fill leaves it holding, never over
    p_true, at the measured rate that ask sells that inventory."""

    def test_a_fill_sells_at_the_ask_off_the_mid_it_found(self):
        # 97.4c + 1c: 33.8% of 2,083 shares sell at 98.4c in 30 days, and a
        # 96.0c fill is worth 94.22c today.
        value = newsom_bid_value(fm=SURE)
        self.assertEqual(value.size, 2083.33)
        self.assertAlmostEqual(value.loss_per_share, 0.960 - sold_at(0.984))
        self.assertAlmostEqual(value.loss_per_share, 0.01783, places=5)

    def test_an_ask_over_p_true_is_credited_at_p_true(self):
        # The 98.4c ask is over Newsom's 98c: a share sells for 98c, and the
        # fill costs 2.3c a share.
        value = newsom_bid_value()
        self.assertAlmostEqual(value.loss_per_share, 0.960 - sold_at(0.98, p_true=0.98))
        self.assertAlmostEqual(value.loss_per_share, 0.02342, places=5)

    def test_a_fill_mid_above_the_book_mid_anchors_the_ask(self):
        value = newsom_bid_value(fm=SURE, fill_mid=0.977)
        self.assertAlmostEqual(value.loss_per_share, 0.960 - sold_at(0.987))

    def test_the_exit_floor_takes_the_cost_blended_over_the_fill(self):
        # 4,000 shares at 98.5c rest their ask at 99.0c. 1,104.16 more at
        # 96.0c blend the cost to 97.96c, flooring the ask at 98.5c, over the
        # 98.4c the mid sets.
        held = Position(fav_shares=4000.0, fav_avg=0.985)
        value = newsom_bid_value(held, SURE._replace(capital=5000.0))
        self.assertEqual(value.size, 1104.16)
        self.assertAlmostEqual(value.loss_per_share, 0.960 - sold_at(0.985, 5104.16))

    def test_the_sale_rate_is_that_of_the_shares_held_and_the_bid_together(self):
        # 1,000 shares at 96c leave capital for 1,083.33 more, so the fill
        # leaves the same 2,083.33 to sell and costs the same.
        held = Position(fav_shares=1000.0, fav_avg=0.96)
        value = newsom_bid_value(held)
        self.assertEqual(value.size, 1083.33)
        self.assertAlmostEqual(value.loss_per_share, newsom_bid_value().loss_per_share)

    def test_shares_already_held_make_the_same_fill_cost_more(self):
        # With the capital for the full bid on top of them, the fill leaves
        # 3,083.33 shares, 29.4% of which sell in 30 days.
        held = Position(fav_shares=1000.0, fav_avg=0.96)
        value = newsom_bid_value(held, NEWSOM._replace(capital=2960.0))
        self.assertEqual(value.size, 2083.33)
        self.assertAlmostEqual(value.loss_per_share, 0.02666, places=5)


# Trump goes to space: a 99c favorite 0.228 years out with a 97.7c mid, a
# 4.5c band and a $3 pool, $2,000 behind its bid, on a book deep enough that
# the bid earns cents a day.
TRUMP = market(p_true=0.99, v=0.045, rate_per_day=3.0, min_size=200.0, capital=2000.0,
               naked_budget_usd=10.0, end_date=NOW + timedelta(days=0.228 * 365.25))
TRUMP_BOOK = Levels(bids=[(0.976, 4000.0), (0.970, 3000.0), (0.968, 5000.0)],
                    asks=[(0.978, 3000.0), (0.983, 2000.0), (0.988, 5000.0)], tick=0.001)
# A sweep takes the top of the book every other day, reaches 1c under the
# mid every 11 weeks and 4c under every 6 months.
TRUMP_FILLS = curve(0.46, 0.089, 0.076, 0.015, 0.013, 0.0096, 0.0076, 0.0054, 0.0054)


class FillNeverGainsTest(unittest.TestCase):
    """A bid rests for its reward. A fill worth more than its price costs
    nothing, and is never counted as income."""

    def test_a_fill_worth_more_than_its_price_adds_nothing_to_the_net(self):
        # At the 93.3c band edge a share sells at the 98.7c ask and is worth
        # 1.09c more than it costs, 11.6 shares a day. The bid nets its
        # $0.0004 reward.
        edge = bid_value(TRUMP, TRUMP_BOOK, 0.933, fills=TRUMP_FILLS)
        self.assertAlmostEqual(edge.loss_per_share, -0.01093, places=5)
        self.assertAlmostEqual(edge.fills_per_day, 11.6, places=1)
        self.assertEqual(edge.fill_cost_per_day, 0.0)
        self.assertAlmostEqual(edge.reward_per_day, 0.0004, places=4)
        self.assertEqual(edge.net_per_day, edge.reward_per_day)

    def test_the_bid_does_not_go_to_the_band_edge_for_its_fills(self):
        # Counted as income, the edge's fills made it the best bid at $0.13 a
        # day. All the capital nets the most at 94.4c, the highest price
        # where its fills cost nothing, for a $0.061 reward. Half of it at
        # 95.7c nets $0.067: a $0.128 reward less fills that cost $0.061.
        full = max((bid_value(TRUMP, TRUMP_BOOK, price, fills=TRUMP_FILLS)
                    for price in fav_bid_candidates(TRUMP_BOOK, TRUMP.v, TRUMP.tick,
                                                    TRUMP.min_size, CFG.max_lead,
                                                    ev_cap(TRUMP, CFG))),
                   key=lambda value: value.net_per_day)
        self.assertEqual((full.price, full.fill_cost_per_day), (0.944, 0.0))
        self.assertAlmostEqual(full.net_per_day, 0.0613, places=4)
        target = plan(TRUMP, TRUMP_BOOK, fills=TRUMP_FILLS)
        bid = by_slot(target)[Slot.FAV_BID]
        self.assertEqual((bid.price, bid.size), (0.957, 1044.93))
        self.assertAlmostEqual(target.fav.best.net_per_day, 0.0671, places=4)

    def test_a_fill_that_costs_is_still_charged(self):
        # At 95.3c a fill loses 0.89c a share, 14.1 shares a day.
        value = bid_value(TRUMP, TRUMP_BOOK, 0.953, fills=TRUMP_FILLS)
        self.assertAlmostEqual(value.loss_per_share, 0.00890, places=5)
        self.assertAlmostEqual(value.net_per_day,
                               value.reward_per_day - value.fills_per_day * value.loss_per_share)
        self.assertLess(value.net_per_day, value.reward_per_day)


# Newsom on its $3 pool, in a book 3,000 shares deep at the top, where a
# sweep reaches a bid 1c under the mid about four times a year.
POOL = NEWSOM._replace(rate_per_day=3.0)
POOL_BOOK = Levels(bids=[(0.969, 3000.0), (0.950, 5000.0)],
                   asks=[(0.979, 3000.0), (0.990, 5000.0)], tick=0.001)
FEW = curve(0.05, 0.04, 0.03, 0.02, 0.012, 0.010, 0.006, 0.003, 0.002)


def exhaustive(fm, book, position=Position(), cfg=CFG, naked=True, fills=QUIET):
    """The QuoteValue of the favorite bid that nets the most, with every
    candidate price planned and valued at every size of the ladder: the bid
    target_orders must choose without planning them all. A tie goes to the
    lower price, then to the smaller size."""
    outlook, room = outlook_with(fm, fills, cfg), capital_room(fm, position)
    best = None
    for price in fav_bid_candidates(book, fm.v, fm.tick, fm.min_size, cfg.max_lead,
                                    ev_cap(fm, cfg)):
        for fraction in reward_farmer.SIZE_LADDER:
            target = quotes_at(fm, book, position, cfg, naked, price,
                               round_size_down(fraction * room / price))
            if Slot.FAV_BID in by_slot(target):
                value = value_of(target, fm, book, position, cfg, outlook)
                if best is None or value.net_per_day >= best.net_per_day - 1e-12:
                    best = value
    return best


# A bid that leads this book by half a cent is the only one in a 2c band, so
# it earns the whole pool at any size.
ALONE = market(v=0.02, capital=2000.0)
ALONE_BOOK = Levels(bids=[(0.95, 1000.0)], asks=[(0.985, 1000.0)], tick=0.001)
FREE = curve(0.0)


class SizeChoiceTest(unittest.TestCase):
    """The capital is the most a favorite bid may be, and the farmer rests
    the size at or under it that nets the most. At 96.4c on POOL all $2,000
    nets $0.16 a day, three quarters of it $0.21, half $0.22, a quarter $0.16
    and an eighth $0.10."""

    def chosen(self, fm=POOL, book=POOL_BOOK, position=Position(), fills=FEW):
        target = plan(fm, book, position, fills=fills)
        bid, best = by_slot(target)[Slot.FAV_BID], target.fav.best
        self.assertEqual((bid.price, bid.size), (best.price, best.size))
        return best

    def test_a_bid_whose_fills_cost_rests_under_its_capital(self):
        # All of it earns $0.85 and loses 24.9 shares a day at 2.74c. Half
        # earns $0.49 and loses 12.4 shares a day at 2.20c.
        best = self.chosen()
        self.assertEqual((best.price, best.size), (0.964, 1037.34))
        self.assertAlmostEqual(best.net_per_day, 0.2181, places=4)
        self.assertLess(best.price * best.size, POOL.capital / 1.9)

    def test_a_bid_whose_fills_cost_nothing_rests_all_its_capital(self):
        # Two days out a 98.5c fill of a 99.5c share is worth more than it
        # costs, and $500 buys 507.61 shares.
        best = self.chosen(market(end_date=NOW + timedelta(days=2)), FLOOR_BOOK, fills=SWEPT)
        self.assertEqual(best.fill_cost_per_day, 0.0)
        self.assertEqual((best.price, best.size), (0.985, 507.61))

    def test_doubling_the_capital_does_not_lower_the_net_chosen(self):
        # $1,000 buys the 1,037 shares that net the most, and more capital
        # leaves the bid there: all of $1,000 is half of $2,000, a quarter of
        # $4,000 and an eighth of $8,000.
        nets = [self.chosen(POOL._replace(capital=capital)).net_per_day
                for capital in (250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0)]
        for less, more in zip(nets, nets[1:]):
            self.assertGreaterEqual(more, less)
        self.assertEqual(nets[2:], nets[2:3] * 4)
        self.assertLess(nets[1], nets[2])

    def test_shares_already_held_shrink_the_bid(self):
        # 5,000 shares held at 96c leave the same $2,000 of a $6,800 capital,
        # and a fill on top of them takes longer to sell.
        held = Position(fav_shares=5000.0, fav_avg=0.96)
        fm = POOL._replace(capital=6800.0)
        self.assertEqual(capital_room(fm, held), 2000.0)
        best = self.chosen(fm, position=held)
        self.assertEqual((best.price, best.size), (0.964, 518.67))

    def test_a_size_under_the_reward_minimum_is_not_planned(self):
        # At a 1,100-share minimum half the capital buys too few shares to
        # score, so three quarters of it rests.
        best = self.chosen(POOL._replace(min_size=1100.0))
        self.assertEqual((best.price, best.size), (0.964, 1556.01))

    def test_the_naked_budget_still_trims_a_bid_balanced_above_the_floor(self):
        # On BUDGET_BOOK $10 buys 714.28 longshot shares at 1.4c, which
        # balance 124.91 favorite shares at 95.5c whatever size the bid
        # starts at. $500 buys 523.56.
        intents = by_slot(plan(market(p_true=0.98, naked_budget_usd=10.0), BUDGET_BOOK))
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual((fav.price, fav.size), (0.955, 124.91))
        self.assertEqual((low.price, low.size), (0.014, 714.28))

    def test_the_naked_budget_does_not_trim_a_bid_balanced_at_a_free_price(self):
        # $0.30 buys 300 longshot shares at 0.1c and the bid needs 675.82.
        intents = by_slot(plan(market(naked_budget_usd=0.3, zones=steady(0.99)), FLOOR_BOOK))
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual((fav.price, fav.size), (0.985, 507.61))
        self.assertEqual((low.price, low.size), (0.001, 675.82))

    def test_a_tie_goes_to_the_smaller_size(self):
        # Every size earns the whole $100 pool and no fill costs anything.
        best = self.chosen(ALONE, ALONE_BOOK, fills=FREE)
        self.assertEqual((best.reward_per_day, best.fill_cost_per_day), (100.0, 0.0))
        self.assertEqual((best.price, best.size),
                         (0.955, round_size_down(0.125 * 2000.0 / 0.955)))

    def test_the_bid_chosen_is_the_best_of_every_price_and_size(self):
        held = Position(fav_shares=5000.0, fav_avg=0.96)
        cases = {
            "a quiet market": (market(), FLOOR_BOOK, Position(), CFG, QUIET),
            "swept at the top": (market(rate_per_day=10.0), FLOOR_BOOK, Position(), CFG, SWEPT),
            "two days out": (market(end_date=NOW + timedelta(days=2)), FLOOR_BOOK, Position(),
                             CFG, SWEPT),
            "a budget longshot bid": (market(p_true=0.98), BUDGET_BOOK, Position(), CFG, QUIET),
            "a free bid over its budget": (market(naked_budget_usd=0.3, zones=steady(0.99)),
                                           FLOOR_BOOK,
                                            Position(), CFG, QUIET),
            "trimmed by the budget": (market(p_true=0.98, naked_budget_usd=10.0), BUDGET_BOOK,
                                      Position(), CFG, QUIET),
            "Mistral": (MISTRAL, MISTRAL_BOOK, Position(), MISTRAL_CFG, QUIET),
            "Newsom on a $100 pool": (NEWSOM, NEWSOM_BOOK, Position(), CFG, QUIET),
            "Newsom in a thin book": (POOL, NEWSOM_BOOK, Position(), CFG, QUIET),
            "Newsom": (POOL, POOL_BOOK, Position(), CFG, FEW),
            "Newsom with $250": (POOL._replace(capital=250.0), POOL_BOOK, Position(), CFG, FEW),
            "Newsom with $8,000": (POOL._replace(capital=8000.0), POOL_BOOK, Position(), CFG,
                                   FEW),
            "Newsom holding 5,000": (POOL._replace(capital=6800.0), POOL_BOOK, held, CFG, FEW),
            "Newsom at a 1,100-share minimum": (POOL._replace(min_size=1100.0), POOL_BOOK,
                                                Position(), CFG, FEW),
            "Trump": (TRUMP, TRUMP_BOOK, Position(), CFG, TRUMP_FILLS),
            "OpenSI": (OPENSI, OPENSI_BOOK, OPENSI_HELD, CFG, QUIET),
            "alone in the band": (ALONE, ALONE_BOOK, Position(), CFG, FREE),
            "alone, fills costing": (ALONE, ALONE_BOOK, Position(), CFG, QUIET),
        }
        sizes = set()
        for name, (fm, book, position, cfg, fills) in cases.items():
            with self.subTest(name):
                best = plan(fm, book, position, cfg, fills=fills).fav.best
                self.assertIsNotNone(best)
                self.assertEqual(best, exhaustive(fm, book, position, cfg, fills=fills))
                full = round_size_down(capital_room(fm, position) / best.price)
                sizes.add(round(best.size / full, 3))
        self.assertTrue({1.0, 0.75, 0.5, 0.25, 0.125} <= sizes, sizes)


class RestingBidTest(unittest.TestCase):
    """A resting favorite bid is judged by what the orders planned around it
    net as it rests. At 96.4c on POOL half the capital is the target at $0.22
    a day. Three quarters of it nets $0.21, all of it $0.16 and an eighth
    $0.10."""

    def keep(self, price, size):
        order = live("a", Slot.FAV_BID, price, size)
        target = plan(POOL, POOL_BOOK, fills=FEW, resting=[order])
        return keep_failure(order, by_slot(target)[Slot.FAV_BID], target, POOL, Position(), CFG)

    def test_the_nets_at_each_size(self):
        nets = [bid_value(POOL, POOL_BOOK, 0.964, size, fills=FEW).net_per_day
                for size in (2074.68, 1556.01, 1037.34, 259.33)]
        for net, expected in zip(nets, (0.1635, 0.2134, 0.2181, 0.1017)):
            self.assertAlmostEqual(net, expected, places=4)

    def test_a_bid_one_size_off_the_target_netting_within_the_keep_margin_stays(self):
        self.assertIsNone(self.keep(0.964, 1556.01))

    def test_a_bid_netting_far_less_is_replaced(self):
        self.assertEqual(self.keep(0.964, 259.33), "net below keep ratio")
        self.assertEqual(self.keep(0.964, 2074.68), "net below keep ratio")


def days_out(days, **changes):
    """A market this many days from its end date, as the farmer sees it then."""
    return market(end_date=NOW + timedelta(days=days), zones=Zones(days / 365.25), **changes)


class ReturnLimitTest(unittest.TestCase):
    """No longshot bid rests where a fill gives the favorite buyer over
    MAX_RETURN a year. On BUDGET_BOOK the cheapest price scoring 0.10 is
    1.7c, which offers 23.2% a year 30 days out, and nothing under 0.6c
    scores."""

    def test_the_long_bid_rests_under_the_limit_and_the_fav_bid_shrinks_to_match(self):
        far = quotes_at(market(p_true=0.98), BUDGET_BOOK, Position(), CFG, True, 0.949)
        near = quotes_at(days_out(30, p_true=0.98), BUDGET_BOOK, Position(), CFG, True, 0.949)
        self.assertEqual(by_slot(far)[Slot.LONG_BID].price, 0.017)
        low, fav = by_slot(near)[Slot.LONG_BID], by_slot(near)[Slot.FAV_BID]
        # $25 buys the most score under the limit at 1.4c.
        self.assertEqual((low.price, low.size), (0.014, 1785.71))
        self.assertEqual(fav.size, 251.11)
        self.assertLess(fav.size, by_slot(far)[Slot.FAV_BID].size)
        q_one, q_two = q_sums(near)
        self.assertLessEqual(q_one, q_two + 1e-9)

    def test_with_no_price_under_the_limit_neither_bid_rests(self):
        # 10 days out nothing over 0.4c is allowed.
        target = quotes_at(days_out(10, p_true=0.98), BUDGET_BOOK, Position(), CFG, True, 0.949)
        self.assertEqual(target.intents, [])

    def test_the_exits_rest_whatever_the_limit(self):
        fm = days_out(10, p_true=0.98)
        long_held = Position(long_shares=200.0, long_avg=0.02)
        self.assertEqual(set(by_slot(quotes_at(fm, BUDGET_BOOK, long_held, CFG, True, 0.949))),
                         {Slot.LONG_ASK})
        fav_held = Position(fav_shares=100.0, fav_avg=0.94)
        intents = by_slot(quotes_at(fm, BUDGET_BOOK, fav_held, CFG, True, 0.949))
        self.assertIn(Slot.FAV_ASK, intents)
        self.assertNotIn(Slot.LONG_BID, intents)

    def test_a_resting_bid_over_the_limit_is_replaced(self):
        # The net-best favorite bid lifts the midpoint to 0.9625, where the
        # cheapest price scoring 0.10 is 1.4c. 20 days out that offers 29%.
        order = live("low", Slot.LONG_BID, 0.014, 1785.71)
        far = market(p_true=0.98)
        target = plan(far, BUDGET_BOOK)
        self.assertIsNone(keep_failure(order, by_slot(target)[Slot.LONG_BID], target, far,
                                       Position(), CFG))
        near = days_out(20, p_true=0.98)
        target = plan(near, BUDGET_BOOK)
        low = by_slot(target)[Slot.LONG_BID]
        self.assertEqual(low.price, 0.009)
        self.assertEqual(keep_failure(order, low, target, near, Position(), CFG),
                         "over the return limit")
        actions = reconcile(target, [order], near, Position(), CFG)
        self.assertEqual(actions[0], Cancel(("low",)))
        self.assertIn(Place(low), actions)

    def test_a_bid_drifting_toward_the_end_date_steps_down_and_never_back(self):
        # One cycle every six hours from 45 days out to 8, on the same book.
        # The limit falls from 2.2c to 0.4c. Each price the bid rests at is
        # left once, when it comes to offer over the limit, and never
        # rested again.
        orders, serial, rested, over_limit = [], 0, [], 0
        for step in range(149):
            days = 45 - step / 4
            fm = days_out(days, p_true=0.98)
            target = plan(fm, BUDGET_BOOK, resting=orders)
            target = around_kept_bid(target, orders, fm, BUDGET_BOOK, Position(), CFG, True)
            intents = by_slot(target)
            actions = reconcile(target, orders, fm, Position(), CFG)
            cancelled = {i for a in actions if isinstance(a, Cancel) for i in a.order_ids}
            for o in orders:
                if o.slot is Slot.LONG_BID and o.id in cancelled and Slot.LONG_BID in intents:
                    why = keep_failure(o, intents[Slot.LONG_BID], target, fm, Position(), CFG)
                    over_limit += why == "over the return limit"
            orders = [o for o in orders if o.id not in cancelled]
            for a in actions:
                if isinstance(a, Place):
                    serial += 1
                    orders.append(live(f"r{serial}", a.intent.slot, a.intent.price,
                                       a.intent.size))
            low = next((o.price for o in orders if o.slot is Slot.LONG_BID), None)
            if low is not None:
                self.assertFalse(over_max_return(low, days / 365.25), days)
            if not rested or rested[-1] != low:
                rested.append(low)
        prices = [p for p in rested if p is not None]
        self.assertEqual(prices[0], 0.014)
        self.assertEqual(prices, sorted(set(prices), reverse=True))
        self.assertIsNone(rested[-1])
        self.assertEqual(rested.count(None), 1)
        self.assertGreaterEqual(over_limit, 3)

    def test_a_market_is_read_with_the_time_its_end_date_leaves(self):
        row = {"condition_id": CID, "slug": "", "question": "", "p_true": 0.98,
               "naked_budget_usd": 25.0, "capital_usd": None, "paused": 0, "naked_paused": 0,
               "sole_bid": 0, "end_date": (NOW + timedelta(days=30)).isoformat(),
               "metrics": json.dumps(METRICS)}
        fm = reward_farmer.farm_market(row, INFO, CFG, NOW)
        self.assertAlmostEqual(fm.zones.years_left, 30 / 365.25)
        self.assertEqual(reward_farmer.farm_market(row, INFO, CFG, NOW + timedelta(days=31))
                         .zones.years_left, 0.0)


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

    def test_rewards_need_a_listed_rate_and_a_max_spread(self):
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
    READ = Read(NO_SWEEPS, NO_BUYS)

    def setUp(self):
        self.fetch = mock.Mock(return_value=self.READ)
        self.at = NOW
        self.stop = threading.Event()
        self.histories = reward_farmer.Histories(self.stop, self.fetch, lambda: self.at)
        self.histories.wanted = {"0xc0nd": self.WANT}
        patcher = mock.patch.object(reward_farmer, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def refresh_at(self, *seconds):
        """Refresh with the clock at each NOW + seconds; the read after each."""
        seen = []
        for s in seconds:
            self.at = NOW + timedelta(seconds=s)
            self.histories.refresh(None)
            seen.append(self.histories.read.get("0xc0nd"))
        return seen

    def reads(self):
        """(market, time) of each read so far, in order."""
        return [(c.args[1], c.args[3]) for c in self.fetch.call_args_list]

    def want(self, *cids):
        self.histories.wanted = {cid: reward_farmer.Want(f"fav-{cid}", 0.90, cid)
                                 for cid in cids}

    def test_a_wanted_market_is_read_at_once_then_hourly(self):
        self.refresh_at(0, 5, 3599, 3600)
        self.assertEqual([c.args[3] for c in self.fetch.call_args_list],
                         [NOW, NOW + timedelta(seconds=3600)])
        self.assertEqual(self.fetch.call_args.args[1:], ("0xc0nd", FAV,
                                                         NOW + timedelta(seconds=3600), 0.90))

    def test_a_failed_first_read_leaves_no_history_and_retries_after_two_minutes(self):
        self.fetch.side_effect = [httpx.ConnectError("data API down"), self.READ]
        self.assertEqual(self.refresh_at(0, 119, 120), [None, None, self.READ])
        self.assertEqual(self.fetch.call_count, 2)

    def test_a_failed_reread_keeps_the_last_history(self):
        later = Read(NO_SWEEPS._replace(events=(1,) * len(DISTANCES)), NO_BUYS)
        self.fetch.side_effect = [self.READ, httpx.ConnectError("data API down"), later]
        self.assertEqual(self.refresh_at(0, 3600, 3700, 3720), [self.READ] * 3 + [later])

    def test_a_market_no_longer_wanted_is_forgotten(self):
        other = reward_farmer.Want("other-fav", 0.90, "other")
        self.histories.wanted = {"0xc0nd": self.WANT, "0xother": other}
        self.refresh_at(0)
        self.histories.wanted = {"0xother": other}
        self.refresh_at(5)
        self.assertEqual(self.histories.read, {"0xother": self.READ})
        self.histories.wanted = {"0xc0nd": self.WANT}
        self.refresh_at(10)
        self.assertEqual(self.fetch.call_count, 3)

    def test_a_changed_floor_rereads_at_once(self):
        self.refresh_at(0)
        self.histories.wanted = {"0xc0nd": self.WANT._replace(floor=0.92)}
        self.refresh_at(5)
        self.assertEqual([c.args[4] for c in self.fetch.call_args_list], [0.90, 0.92])

    def test_each_read_has_its_own_time_and_is_next_due_an_hour_after_it(self):
        def ten_minutes(*args):
            self.at += timedelta(minutes=10)
            return self.READ

        self.want("a", "b")
        self.fetch.side_effect = ten_minutes
        self.refresh_at(0)
        self.assertEqual(self.reads(), [("a", NOW), ("b", NOW + timedelta(minutes=10))])
        self.fetch.side_effect = None
        self.refresh_at(3599, 3600, 4199)
        self.assertEqual(self.reads()[2:], [("a", NOW + timedelta(seconds=3600))])
        self.refresh_at(4200)
        self.assertEqual(self.reads()[3:], [("b", NOW + timedelta(seconds=4200))])

    def test_unread_markets_are_read_before_read_ones_each_in_the_order_wanted(self):
        self.want("a", "b")
        self.refresh_at(0)
        self.want("b", "d", "a", "c")
        self.refresh_at(3600)
        self.assertEqual([cid for cid, _ in self.reads()[2:]], ["d", "c", "b", "a"])

    def test_a_market_wanted_during_a_pass_is_read_in_that_pass(self):
        def want_d(http, cid, *rest):
            if cid == "a":
                self.want("a", "b", "c", "d")
            return self.READ

        self.want("a", "b", "c")
        self.refresh_at(0)
        self.fetch.side_effect = want_d
        self.refresh_at(3600)
        self.assertEqual([cid for cid, _ in self.reads()[3:]], ["a", "d", "b", "c"])

    def test_a_stop_ends_the_pass(self):
        def stop(*args):
            self.stop.set()
            return self.READ

        self.want("a", "b")
        self.fetch.side_effect = stop
        self.refresh_at(0)
        self.assertEqual([cid for cid, _ in self.reads()], ["a"])
        self.assertEqual(list(self.histories.read), ["a"])


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
            CID, "test-market", "Test?", self.end_date().isoformat(), 1.0,
            json.dumps(METRICS),
        )], [CID], NOW, prior=fill_cost.prior_to_json(PRIOR))
        farm_db.approve_market(self.ui, CID, 0.995, 25.0, now=NOW)
        self.book_fetches = 0
        self.on_book_fetch = lambda count: None
        self.infos = mock.Mock(return_value={CID: INFO})
        self.history = mock.Mock(return_value=NO_SWEEPS)
        self.sweeps = ()
        self.book = FLOOR_BOOK
        self.read_at = NOW
        for name, fake in (
            ("fetch_market_infos", self.infos),
            ("fetch_read", self.fake_read),
            ("Histories", functools.partial(reward_farmer.Histories,
                                            clock=lambda: self.read_at)),
            ("fetch_holdings", mock.Mock(return_value={})),
            ("fetch_levels", self.fake_levels),
            ("fetch_total_rates", mock.Mock(return_value={})),
            ("fetch_gamma", mock.Mock(return_value={})),
            ("log", mock.Mock()),
        ):
            patcher = mock.patch.object(reward_farmer, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.args = build_parser().parse_args(["--db", self.path, "--interval", "0.01"])

    @staticmethod
    def end_date(days=100):
        """An end date this many days from today. The farmer reads the clock,
        and what a longshot bid may rest at turns on the time left."""
        return datetime.now(timezone.utc) + timedelta(days=days)

    def fake_read(self, http, condition_id, fav_token_id, now, floor):
        """A read of self.history's History and a new tape of self.sweeps."""
        history = self.history(http, condition_id, fav_token_id, now, floor)
        return Read(history, BuyTape(self.sweeps, now, now.timestamp() - 60 * 86400))

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
            farmer.histories.refresh(farmer.http)
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
        farm_db.set_approval(self.ui, CID, 0.995, 25.0, None, paused=True, now=NOW)
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
        farm_db.set_approval(self.ui, CID, 0.995, 25.0, None, paused=True, now=NOW)
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

        farm_db.set_approval(self.ui, CID, 0.995, 25.0, None, now=NOW)
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
        farm_db.set_approval(self.ui, CID, 0.995, 25.0, None, now=NOW)
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

    # FLOOR_BOOK once its only bid has left for 80c: an 89.75c midpoint
    # without us. A bid of ours over 93.3c keeps the 0.1c longshot bid in the
    # 3.5c band, so 93.4c is the lowest that pays and 93.6c the sole bid.
    UNSUPPORTED = FLOOR_BOOK._replace(bids=[(0.80, 1000.0)])

    def test_a_market_that_opted_in_holds_a_sole_bid_while_no_other_bid_supports_it(self):
        farm_db.set_sole_bid(self.ui, CID, True)
        farmer = self.started_farmer()
        farmer.cycle()
        farmer.cycle()
        low = self.farm_state()[1]["LONG_BID"]["order"]
        self.assertEqual(self.farm_state()[1]["FAV_BID"]["order"]["price"], 0.975)

        before = self.last_event_id()
        self.book = self.UNSUPPORTED
        farmer.cycle()
        row, slots = self.farm_state()
        self.assertEqual(row["phase"], "SOLE_BID")
        self.assertAlmostEqual(row["mid"], (0.936 + 0.995) / 2)
        self.assertEqual((slots["FAV_BID"]["decision"], slots["FAV_BID"]["target"]["price"]),
                         ("replace", 0.936))
        self.assertEqual((slots["LONG_BID"]["decision"], slots["LONG_BID"]["order"]),
                         ("hold", low))
        self.assertEqual([(e["kind"], e["slot"]) for e in self.events(before)],
                         [("PHASE", None), ("CANCEL", "FAV_BID"), ("PLACE", "FAV_BID")])

        before = self.last_event_id()
        farmer.cycle()
        row, slots = self.farm_state()
        self.assertEqual((row["phase"], slots["FAV_BID"]["decision"], slots["LONG_BID"]["decision"]),
                         ("SOLE_BID", "hold", "hold"))
        self.assertEqual(self.events(before), [])

        self.book = FLOOR_BOOK
        farmer.cycle()
        self.assertEqual(self.farm_state()[0]["phase"], "QUOTING")

    def test_a_market_that_did_not_opt_in_takes_its_bids_down_on_the_same_book(self):
        farmer = self.started_farmer()
        farmer.cycle()
        self.book = self.UNSUPPORTED
        farmer.cycle()
        row, slots = self.farm_state()
        self.assertEqual(row["phase"], "BELOW_RANGE")
        self.assertEqual((slots["FAV_BID"]["decision"], slots["LONG_BID"]["decision"]),
                         ("cancel", "cancel"))

    def test_deep_asks_falling_by_the_threshold_cool_down(self):
        fallen = MISTRAL_BOOK._replace(asks=[(0.967, 150.0)] + MISTRAL_BOOK.asks[1:])
        (_, after), events = self.replay(MISTRAL_BOOK, fallen)
        self.assertEqual(after, ("COOLDOWN", "ask drop", {}))
        self.assertIn("PHASE", [kind for kind, _ in events])

    def test_an_end_cooldown_command_quotes_again_and_the_same_drop_does_not_cool_down(self):
        self.infos.return_value = {CID: INFO._replace(v=0.085, min_size=20.0)}
        farmer = self.started_farmer()
        fallen = MISTRAL_BOOK._replace(asks=[(0.967, 150.0)] + MISTRAL_BOOK.asks[1:])
        for self.book in (MISTRAL_BOOK, fallen):
            farmer.cycle()
        self.assertEqual(self.farm_state()[0]["phase"], "COOLDOWN")
        before = self.last_event_id()

        farm_db.send_command(self.ui, CommandKind.END_COOLDOWN, NOW, {"condition_id": CID})
        for _ in range(2):
            farmer.cycle()
            row, slots = self.farm_state()
            self.assertEqual((row["phase"], row["phase_until"]), ("QUOTING", None))
            self.assertIsNotNone(slots["FAV_BID"]["target"])
        phases = [e for e in self.events(before) if e["kind"] == "PHASE"]
        self.assertEqual([(e["condition_id"], json.loads(e["data"])) for e in phases],
                         [(CID, {"from": "COOLDOWN", "to": "QUOTING",
                                 "reason": "ended by operator", "until": None})])
        self.assertIn("ended by operator", phases[0]["message"])
        command = self.query("SELECT * FROM commands")[0]
        self.assertEqual(command["result"], "cooldown ended")
        self.assertIsNotNone(command["consumed_at"])

    def test_an_end_cooldown_command_for_a_market_not_cooling_down_changes_nothing(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()
        for cid in (CID, "0xnope"):
            farm_db.send_command(self.ui, CommandKind.END_COOLDOWN, NOW, {"condition_id": cid})
        farmer.cycle()
        self.assertEqual(self.events(before), [])
        self.assertEqual(self.farm_state()[0]["phase"], "QUOTING")
        commands = self.query("SELECT * FROM commands ORDER BY id")
        self.assertEqual([c["result"] for c in commands],
                         ["not in cooldown", "market not managed"])
        self.assertNotIn(None, [c["consumed_at"] for c in commands])

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
        farmer.histories.refresh(farmer.http)
        farmer.cycle()
        self.assertEqual(self.farm_state()[1]["FAV_BID"]["decision"], "place")

    def test_a_min_fav_change_keeps_the_favorite_bid_until_its_history_is_read_again(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()
        farm_db.put_setting(self.ui, "min_fav", 0.85, NOW)
        farmer.cycle()
        fav = self.farm_state()[1]["FAV_BID"]
        self.assertEqual((fav["decision"], fav["history"]["floor"]), ("hold", 0.90))
        self.assertEqual([e["kind"] for e in self.events(before)], ["SETTINGS"])
        self.assertEqual(farmer.histories.wanted,
                         {CID: reward_farmer.Want(FAV, 0.85, "test-market")})

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

    def test_a_restart_quotes_on_a_stored_history_a_minute_overdue_for_its_refresh(self):
        fav = self.first_bid_on_stored_history(self.stored(reward_farmer.HISTORY_S + 60))
        self.assertEqual((fav["decision"], fav["target"]["price"]), ("place", 0.975))

    def test_a_restart_ignores_a_stored_history_older_than_the_seed_limit(self):
        fav = self.first_bid_on_stored_history(self.stored(reward_farmer.HISTORY_SEED_S + 60))
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


class FreeDwellFarmerTest(FarmerHarness):
    """The farmer watches each market's midpoint and seeds the watch at
    start. On FLOOR_BOOK a $0.30 budget buys 300 shares at 0.1c."""

    def setUp(self):
        super().setUp()
        farm_db.set_approval(self.ui, CID, 0.995, 0.3, None, now=NOW)

    def record_mids(self, book_mids, last_ago_s=30):
        """farm_mids samples a minute apart, the last one last_ago_s ago."""
        now = datetime.now(timezone.utc)
        for n, book_mid in enumerate(reversed(book_mids)):
            at = now - timedelta(seconds=last_ago_s + 60 * n)
            self.ui.execute("INSERT INTO farm_mids VALUES (?, ?, ?, ?, ?, ?)",
                            (CID, farm_db.stamp(at), book_mid, book_mid, None, None))
        self.ui.commit()

    def planned(self, farmer):
        """One cycle: the longshot bid target, the favorite bid target's size,
        and whether the farmer published the longshot bid as free of the
        naked budget."""
        farmer.cycle()
        slots = self.farm_state()[1]
        low, fav = slots["LONG_BID"]["target"], slots["FAV_BID"]["target"]
        return (low and (low["price"], low["size"]), fav and fav["size"],
                slots["LONG_BID"]["budget_free"])

    def test_a_market_with_no_midpoint_history_starts_under_its_budget(self):
        # 300 shares at 0.1c balance 507 of the 512.82 favorite shares $500
        # buys at 97.5c.
        low, fav_size, free = self.planned(self.started_farmer())
        self.assertEqual(low, (0.001, 300.0))
        self.assertEqual(fav_size, 507.0)
        self.assertFalse(free)

    def test_a_steady_last_run_frees_the_bid_at_the_first_cycle(self):
        self.record_mids([0.99] * 13)
        low, fav_size, free = self.planned(self.started_farmer())
        self.assertEqual(low[0], 0.001)
        self.assertGreater(low[0] * low[1], 0.3)
        self.assertEqual(fav_size, round_size_down(500.0 / 0.975))
        self.assertTrue(free)

    def test_the_farmer_publishes_the_return_the_longshot_bid_offers(self):
        # The market's row ends 100 days from today, where 0.1c offers a
        # favorite buyer 0.37% a year.
        farmer = self.started_farmer()
        farmer.cycle()
        low = self.farm_state()[1]["LONG_BID"]
        self.assertAlmostEqual(low["fill_return"],
                               reward_screener.fill_return(0.001, 100 / 365.25), places=5)
        self.assertAlmostEqual(low["fill_return"], 0.0037, places=4)

    def test_a_dip_in_the_last_run_keeps_the_bid_under_its_budget(self):
        self.record_mids([0.99] * 8 + [0.96] + [0.99] * 4)
        low, fav_size, free = self.planned(self.started_farmer())
        self.assertEqual(low, (0.001, 300.0))
        self.assertFalse(free)

    def test_one_cycle_under_the_test_puts_the_bid_back_under_its_budget(self):
        self.record_mids([0.99] * 13)
        farmer = self.started_farmer()
        self.assertTrue(self.planned(farmer)[2])
        # BUDGET_BOOK's midpoint is 0.96, where a 0.1c share scores nothing.
        self.book = BUDGET_BOOK
        self.planned(farmer)
        self.book = FLOOR_BOOK
        low, fav_size, free = self.planned(farmer)
        self.assertEqual(low, (0.001, 300.0))
        self.assertFalse(free)
        # Once that cycle is over ten minutes old the bid is free again.
        managed = farmer.markets[CID]
        ago = timedelta(seconds=reward_farmer.FREE_DWELL_S + 1)
        managed.mid_watch = reward_farmer.MidWatch(
            managed.mid_watch.since - ago,
            tuple((at - ago, mid) for at, mid in managed.mid_watch.mids))
        self.assertTrue(self.planned(farmer)[2])


class ReturnLimitFarmerTest(FarmerHarness):
    def test_the_farmer_reads_the_time_left_from_the_end_date_each_cycle(self):
        # The market's row ends 100 days from today. With its end date moved
        # to 20 days from today the 1.4c bid offers over the limit.
        self.book = BUDGET_BOOK
        farmer = self.started_farmer()
        farmer.cycle()
        self.assertEqual(self.farm_state()[1]["LONG_BID"]["target"]["price"], 0.014)
        self.ui.execute("UPDATE markets SET end_date = ? WHERE condition_id = ?",
                        (self.end_date(20).isoformat(), CID))
        self.ui.commit()
        farmer.cycle()
        low = self.farm_state()[1]["LONG_BID"]
        self.assertEqual(low["target"]["price"], 0.009)
        self.assertEqual((low["decision"], low["reason"]), ("replace", "over the return limit"))


class BuyTapeFarmerTest(FarmerHarness):
    def setUp(self):
        super().setUp()
        self.sweeps = (Sweep(NOW.timestamp() - 3600, (Piece(0.995, 40.0), Piece(0.99, 60.0))),)
        saves = mock.patch.object(farm_db, "save_buys", wraps=farm_db.save_buys)
        self.save = saves.start()
        self.addCleanup(saves.stop)

    def test_a_read_tape_is_saved_once_not_every_cycle(self):
        farmer = self.started_farmer()
        for _ in range(3):
            farmer.cycle()
        self.assertEqual(self.save.call_count, 1)
        self.read_at = NOW + timedelta(seconds=reward_farmer.HISTORY_S)
        farmer.histories.refresh(farmer.http)
        farmer.cycle()
        farmer.cycle()
        self.assertEqual(self.save.call_count, 2)
        tape = farmer.histories.read[CID].tape
        self.assertEqual(farm_db.read_buys(self.ui),
                         {CID: tape_to_json(tape, TAPE_ROWS_FARMER)})
        self.assertEqual(tape.read_at, NOW + timedelta(seconds=reward_farmer.HISTORY_S))

    def restarted(self):
        """A farmer that read and saved a fresh tape, then a second one started
        after it, before its reader reads anything."""
        now = datetime.now(timezone.utc)
        self.history.return_value = NO_SWEEPS._replace(read_at=now)
        first = self.started_farmer(read_histories=False)
        self.read_at = now
        first.histories.refresh(first.http)
        first.cycle()
        first.shutdown("test", cancel=False)
        self.history.reset_mock()
        self.save.reset_mock()
        return first.histories.read[CID], self.started_farmer(read_histories=False)

    def test_a_restart_seeds_the_history_and_the_tape_it_saved(self):
        read, farmer = self.restarted()
        seeded = farmer.histories.read[CID]
        self.assertEqual(seeded.history, read.history)
        self.assertEqual((seeded.tape.sweeps, seeded.tape.read_at, seeded.tape.first_at),
                         (self.sweeps, read.tape.read_at, read.tape.first_at))
        farmer.cycle()
        self.history.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(self.farm_state()[1]["FAV_BID"]["decision"], "place")

    def test_a_history_without_a_fresh_tape_still_seeds_and_bids(self):
        stale = tape_to_json(BuyTape(self.sweeps, datetime.now(timezone.utc) - timedelta(
            seconds=reward_farmer.HISTORY_SEED_S + 60), NOW.timestamp() - 86400), TAPE_ROWS_FARMER)
        for tape in (None, stale, {"v": 99}):
            with self.subTest(tape=tape and tape["v"]):
                with mock.patch.object(farm_db, "read_buys",
                                       return_value={} if tape is None else {CID: tape}):
                    read, farmer = self.restarted()
                self.assertEqual(farmer.histories.read[CID], Read(read.history, None))
                farmer.cycle()
                self.assertEqual(self.farm_state()[1]["FAV_BID"]["decision"], "place")


class BookRecordingTest(FarmerHarness):
    """Each quoting market's favorite book goes to farm_books.db beside
    farm.db, off the trading loop."""

    def recorded(self):
        conn = farm_books.open_books(farm_books.books_path(self.path))
        try:
            return farm_books.read_books(conn, CID)
        finally:
            conn.close()

    def test_a_quoting_market_records_its_book_once_an_interval(self):
        farmer = self.started_farmer()
        self.addCleanup(farmer.books.close)
        farmer.cycle()
        farmer.cycle()
        farmer.books.drain()
        [sample] = self.recorded()
        self.assertEqual((sample.mid, sample.bids, sample.asks, sample.ours),
                         (0.99, ((0.985, 1000.0),), ((0.995, 1000.0),), ()))

    def test_a_sample_holds_our_resting_orders(self):
        farmer = self.started_farmer()
        self.addCleanup(farmer.books.close)
        with mock.patch.object(farm_books, "BOOK_SAMPLE_S", 0):
            farmer.cycle()
            farmer.cycle()
        farmer.books.drain()
        later = self.recorded()[-1]
        self.assertEqual([(slot, price) for slot, price, _ in later.ours],
                         [("FAV_BID", 0.975), ("LONG_BID", 0.001)])

    def test_a_failing_book_write_warns_and_the_cycles_carry_on(self):
        farmer = self.started_farmer()
        self.addCleanup(farmer.books.close)
        failing = mock.Mock(side_effect=farm_books.sqlite3.OperationalError("disk I/O error"))
        with mock.patch.object(farm_books, "record", failing):
            farmer.cycle()
            farmer.books.drain()
            farmer.cycle()
        failing.assert_called_once()
        warnings = [c.args[0] for c in reward_farmer.log.call_args_list
                    if c.args[0].startswith("Warning: recording books")]
        self.assertEqual(len(warnings), 1)
        self.assertIn("disk I/O error", warnings[0])
        _, slots = self.farm_state()
        self.assertEqual(slots["FAV_BID"]["decision"], "hold")

    def test_a_stalled_book_write_never_holds_up_a_cycle(self):
        farmer = self.started_farmer()
        writing, release = threading.Event(), threading.Event()

        def stall(conn, samples):
            writing.set()
            release.wait(10)

        with mock.patch.object(farm_books, "record", stall),                 mock.patch.object(farm_books, "BOOK_SAMPLE_S", 0):
            farmer.books.start()
            farmer.cycle()
            self.assertTrue(writing.wait(10))
            started = time.monotonic()
            farmer.cycle()
            farmer.cycle()
            elapsed = time.monotonic() - started
            release.set()
            farmer.stop.set()
            farmer.books.join(10)
        self.assertLess(elapsed, 5)


CID2, FAV2, LONG2 = "0xc0nd2", "fav-token-2", "long-token-2"
INFO2 = INFO._replace(token_ids=(FAV2, LONG2))


class ExitFarmerTest(FarmerHarness):
    """The farmer prices a favorite exit from the mid before the fill it saw.

    The book falls to a 96.5c mid after the favorite bid fills; 97.5c was the
    mid when the positions were read before the fill."""

    FALLEN = Levels(bids=[(0.96, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)

    def setUp(self):
        super().setUp()
        self.holdings = mock.Mock(return_value={})
        patcher = mock.patch.object(reward_farmer, "fetch_holdings", self.holdings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.book = self.FALLEN

    def sample_mid(self, book_mid, seconds_ago):
        at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        self.ui.execute("INSERT INTO farm_mids (condition_id, at, mid, book_mid) VALUES (?, ?, ?, ?)",
                        (CID, farm_db.stamp(at), book_mid, book_mid))
        self.ui.commit()

    def fill(self, farmer, shares=100.0):
        self.holdings.return_value = {FAV: (shares, 0.95)}
        farmer.refresh_positions(datetime.now(timezone.utc))

    def ask_target(self, farmer):
        farmer.cycle()
        _, slots = self.farm_state()
        return slots["FAV_ASK"]["target"]["price"]

    def test_after_a_favorite_fill_the_exit_prices_off_the_mid_before_it(self):
        self.sample_mid(0.99, seconds_ago=300)
        self.sample_mid(0.975, seconds_ago=120)
        self.sample_mid(0.965, seconds_ago=30)
        farmer = self.started_farmer()
        self.fill(farmer)
        # 97.5c + 1c, where the book mid alone would give 97.5c.
        self.assertEqual(self.ask_target(farmer), 0.985)
        fills = [json.loads(e["data"]) for e in self.events() if e["kind"] == "FILL"]
        self.assertEqual(fills[-1]["fill_mid"], 0.975)

    def test_the_fill_mid_stops_pricing_once_the_mid_is_back_at_it(self):
        self.sample_mid(0.975, seconds_ago=120)
        farmer = self.started_farmer()
        self.fill(farmer)
        self.assertEqual(self.ask_target(farmer), 0.985)
        self.book = Levels(bids=[(0.975, 1000.0)], asks=[(0.985, 1000.0)], tick=0.001)
        self.assertEqual(self.ask_target(farmer), 0.99)
        self.book = self.FALLEN
        self.assertEqual(self.ask_target(farmer), 0.975)

    def test_a_restart_recovers_the_fill_mid_from_the_last_favorite_fill(self):
        self.sample_mid(0.975, seconds_ago=300)
        self.sample_mid(0.965, seconds_ago=170)
        fill_at = datetime.now(timezone.utc) - timedelta(seconds=180)
        farm_db.publish(self.ui, farm_db.FarmerStatus(
            "dry", 1, fill_at, fill_at, 1.0, False, 1, {}, fill_at), events=[farm_db.Event(
                fill_at, farm_db.EventKind.FILL, "FILL", CID, "FAV_BID", 0.95, 100.0, None, None)])
        farm_db.save_baseline(self.ui, CID, 0.0, 0.0)
        self.holdings.return_value = {FAV: (100.0, 0.95)}
        self.assertEqual(self.ask_target(self.started_farmer()), 0.985)

    def test_a_restart_keeps_the_higher_fill_mid_the_last_fill_recorded(self):
        # An earlier fill of the same inventory found the mid at 97.5c; it had
        # fallen to 96.5c by the last one, which recorded the 97.5c in force.
        self.sample_mid(0.965, seconds_ago=300)
        fill_at = datetime.now(timezone.utc) - timedelta(seconds=180)
        farm_db.publish(self.ui, farm_db.FarmerStatus(
            "dry", 1, fill_at, fill_at, 1.0, False, 1, {}, fill_at), events=[farm_db.Event(
                fill_at, farm_db.EventKind.FILL, "FILL", CID, "FAV_BID", 0.95, 100.0, None,
                {"fill_mid": 0.975})])
        farm_db.save_baseline(self.ui, CID, 0.0, 0.0)
        self.holdings.return_value = {FAV: (100.0, 0.95)}
        self.assertEqual(self.ask_target(self.started_farmer()), 0.985)


class LiveFarmerTest(FarmerHarness):
    """A live farmer on two markets against a mock CLOB that rests what it accepts."""

    def setUp(self):
        super().setUp()
        metrics = {"market": {**METRICS["market"], "fav_token_id": FAV2, "slug": "other-market"}}
        farm_db.store_scan(self.ui, [farm_db.ScanRow(
            CID2, "other-market", "Other?", self.end_date().isoformat(), 1.0,
            json.dumps(metrics),
        )], [CID, CID2], NOW)
        farm_db.approve_market(self.ui, CID2, 0.995, 25.0, now=NOW)
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

    def favorite_asks_with_balance(self, balance):
        """The sizes of the favorite asks a cycle rests when the data API says
        51.76 shares are held and the CLOB's balance read does `balance`."""
        farm_db.save_baseline(self.ui, CID, 0.0, 0.0)
        reward_farmer.fetch_holdings.return_value = {FAV: (51.76, 0.95)}

        def balance_allowance(params):
            if params.token_id == FAV:
                return balance()
            return {"balance": "10000000000"}

        self.client.get_balance_allowance.side_effect = balance_allowance
        self.started_farmer(client=self.client).cycle()
        return [o["original_size"] for o in self.resting.values()
                if o["asset_id"] == FAV and o["side"] == "SELL"]

    def test_a_favorite_ask_is_sized_to_the_shares_the_clob_holds(self):
        # The data API books fills to the cent: it said 51.76 while the CLOB
        # held 51.757576, and rejected the 51.76 ask for want of balance.
        self.assertEqual(self.favorite_asks_with_balance(lambda: {"balance": "51757576"}),
                         ["51.75"])

    def test_a_failed_balance_read_rests_the_planned_ask(self):
        def down():
            raise httpx.ConnectError("down")

        self.assertEqual(self.favorite_asks_with_balance(down), ["51.76"])

    def test_a_failed_batch_alerts_each_market_in_it_with_its_own_count(self):
        self.client.post_orders.side_effect = httpx.ConnectError("down")
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        alerts = [(e["condition_id"], e["message"]) for e in self.events() if e["kind"] == "ALERT"]
        self.assertEqual(alerts, [(CID, "post of 2 orders failed on test-market: down"),
                                  (CID2, "post of 2 orders failed on other-market: down")])

    def test_histories_are_wanted_of_the_quoting_markets_first(self):
        farmer = self.started_farmer(client=self.client)
        farmer.cycle()
        self.assertEqual(list(farmer.histories.wanted), [CID, CID2])
        farm_db.set_paused(self.ui, CID, True)
        farmer.cycle()
        farmer.cycle()
        self.assertEqual(list(farmer.histories.wanted), [CID2, CID])

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
                mock.patch.object(reward_farmer.time, "sleep", sleep):
            reward_farmer.run(args, self.client)

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
