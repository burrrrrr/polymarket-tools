import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

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
    farm_position,
    fill_events,
    naked_allowed,
    next_backoff,
    ratchet_baseline,
    reconcile,
    rewards_active,
    step_state,
    strip_own,
    target_orders,
    without_backed_off,
)
from reward_screener import Levels

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
FAV, LONG = "fav-token", "long-token"

CFG = FarmConfig(
    capital=500.0,
    bid_offset=0.01,
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
# Favorite mid 0.96: the longshot bid must sit at 1.7c to score (BUDGET).
BUDGET_BOOK = Levels(bids=[(0.95, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)

# Per-share score 1c from the mid with a 3.5c max spread: (2.5 / 3.5)^2.
SCORE_1C = (0.025 / 0.035) ** 2
# Longshot bid at 0.1c against a 1c longshot mid: (2.6 / 3.5)^2.
SCORE_LOW_FLOOR = (0.026 / 0.035) ** 2
# Longshot bid at 1.7c against a 4c longshot mid: (1.2 / 3.5)^2.
SCORE_LOW_BUDGET = (0.012 / 0.035) ** 2


def by_slot(target):
    return {i.slot: i for i in target.intents}


def q_sums(target):
    q_one = sum(i.score * i.size for i in target.intents if i.slot in (Slot.FAV_BID, Slot.LONG_ASK))
    q_two = sum(i.score * i.size for i in target.intents if i.slot in (Slot.FAV_ASK, Slot.LONG_BID))
    return q_one, q_two


class TargetOrdersTest(unittest.TestCase):
    def test_flat_floor_market_balances_fav_bid_with_floor_long_bid(self):
        target = target_orders(market(), FLOOR_BOOK, Position(), CFG, naked_allowed=True)
        intents = by_slot(target)
        self.assertEqual(set(intents), {Slot.FAV_BID, Slot.LONG_BID})
        fav, low = intents[Slot.FAV_BID], intents[Slot.LONG_BID]
        self.assertEqual(fav.price, 0.98)
        self.assertEqual(fav.size, 510.20)
        self.assertEqual((fav.token_id, fav.side), (FAV, "BUY"))
        self.assertAlmostEqual(fav.score, SCORE_1C)
        self.assertEqual(low.price, 0.001)
        self.assertEqual((low.token_id, low.side), (LONG, "BUY"))
        self.assertAlmostEqual(low.score, SCORE_LOW_FLOOR)
        q_one, q_two = q_sums(target)
        self.assertGreaterEqual(q_two, q_one)
        self.assertLess(q_two - q_one, low.score * 0.01 + 1e-9)

    def test_fav_inventory_covers_q_two_and_shrinks_long_bid(self):
        flat = by_slot(target_orders(market(), FLOOR_BOOK, Position(), CFG, True))
        held = Position(fav_shares=100.0, fav_avg=0.97)
        target = target_orders(market(), FLOOR_BOOK, held, CFG, True)
        intents = by_slot(target)
        ask = intents[Slot.FAV_ASK]
        self.assertEqual((ask.token_id, ask.side, ask.price, ask.size), (FAV, "SELL", 0.999, 100.0))
        self.assertAlmostEqual(ask.score, SCORE_LOW_FLOOR)
        # Inventory at cost uses part of the capital: (500 - 97) / 0.98.
        self.assertEqual(intents[Slot.FAV_BID].size, 411.22)
        self.assertLess(intents[Slot.LONG_BID].size, flat[Slot.LONG_BID].size)
        q_one, q_two = q_sums(target)
        self.assertGreaterEqual(q_two, q_one)
        expected_long = (q_one - ask.score * ask.size) / SCORE_LOW_FLOOR
        self.assertAlmostEqual(intents[Slot.LONG_BID].size, expected_long, delta=0.01)

    def test_fav_inventory_covering_all_of_q_one_needs_no_long_bid(self):
        held = Position(fav_shares=300.0, fav_avg=0.97)
        intents = by_slot(target_orders(market(), FLOOR_BOOK, held, CFG, True))
        self.assertIn(Slot.FAV_ASK, intents)
        self.assertIn(Slot.FAV_BID, intents)
        self.assertNotIn(Slot.LONG_BID, intents)

    def test_binding_budget_caps_long_bid_and_trims_fav_bid(self):
        fm = market(p_true=0.98, naked_budget_usd=25.0)
        target = target_orders(fm, BUDGET_BOOK, Position(), CFG, True)
        intents = by_slot(target)
        low, fav = intents[Slot.LONG_BID], intents[Slot.FAV_BID]
        self.assertEqual(low.price, 0.017)
        self.assertAlmostEqual(low.score, SCORE_LOW_BUDGET)
        self.assertLessEqual(low.price * low.size, 25.0)
        self.assertEqual(low.size, 1470.58)
        # Untrimmed the bid would be 500 / 0.95 = 526.31 shares.
        expected_fav = low.size * SCORE_LOW_BUDGET / SCORE_1C
        self.assertAlmostEqual(fav.size, expected_fav, delta=0.01)
        q_one, q_two = q_sums(target)
        self.assertLessEqual(q_one, q_two + 1e-9)

    def test_budget_counts_longshot_shares_already_held(self):
        fm = market(p_true=0.98, naked_budget_usd=25.0)
        held = Position(long_shares=10.0, long_avg=1.0)
        low = by_slot(target_orders(fm, BUDGET_BOOK, held, CFG, True))[Slot.LONG_BID]
        self.assertLessEqual(low.price * low.size + 10.0, 25.0)
        self.assertEqual(low.size, 882.35)

    def test_p_true_caps_fav_bid_price(self):
        fm = market(p_true=0.985)
        fav = by_slot(target_orders(fm, FLOOR_BOOK, Position(), CFG, True))[Slot.FAV_BID]
        self.assertEqual(fav.price, 0.975)
        self.assertAlmostEqual(fav.score, (0.020 / 0.035) ** 2)

    def test_p_true_cap_outside_band_drops_fav_bid(self):
        fm = market(p_true=0.96)
        target = target_orders(fm, FLOOR_BOOK, Position(), CFG, True)
        self.assertEqual(target.intents, [])

    def test_naked_cooldown_gives_no_long_bid_and_no_unmatched_fav_bid(self):
        target = target_orders(market(), FLOOR_BOOK, Position(), CFG, naked_allowed=False)
        self.assertEqual(target.intents, [])

    def test_naked_cooldown_keeps_covered_q_two(self):
        held = Position(fav_shares=100.0, fav_avg=0.97)
        intents = by_slot(target_orders(market(), FLOOR_BOOK, held, CFG, False))
        self.assertNotIn(Slot.LONG_BID, intents)
        self.assertIn(Slot.FAV_ASK, intents)
        fav, ask = intents[Slot.FAV_BID], intents[Slot.FAV_ASK]
        self.assertLessEqual(fav.score * fav.size, ask.score * ask.size + 1e-9)

    def test_longshot_inventory_produces_long_ask_at_fav_bid_mirror(self):
        held = Position(long_shares=200.0, long_avg=0.005)
        intents = by_slot(target_orders(market(), FLOOR_BOOK, held, CFG, True))
        ask = intents[Slot.LONG_ASK]
        self.assertEqual((ask.token_id, ask.side, ask.price, ask.size), (LONG, "SELL", 0.02, 200.0))
        self.assertAlmostEqual(ask.score, intents[Slot.FAV_BID].score)

    def test_inventory_below_min_size_gets_no_exit(self):
        held = Position(fav_shares=20.0, fav_avg=0.97, long_shares=20.0, long_avg=0.01)
        intents = by_slot(target_orders(market(), FLOOR_BOOK, held, CFG, True))
        self.assertNotIn(Slot.FAV_ASK, intents)
        self.assertNotIn(Slot.LONG_ASK, intents)

    def test_one_sided_book_plans_nothing(self):
        book = Levels(bids=[(0.985, 1000.0)], asks=[], tick=0.001)
        target = target_orders(market(), book, Position(), CFG, True)
        self.assertEqual((target.mid, target.intents), (None, []))


def live(order_id, slot, price, size):
    token = FAV if slot in (Slot.FAV_BID, Slot.FAV_ASK) else LONG
    return LiveOrder(order_id, slot, token, slot.side, price, size)


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.fm = market()
        self.target = target_orders(self.fm, FLOOR_BOOK, Position(), CFG, True)
        self.intents = by_slot(self.target)

    def run_reconcile(self, orders):
        return reconcile(self.target.intents, orders, self.target.mid, self.fm, Position(), CFG)

    def test_keeps_orders_within_hysteresis(self):
        orders = [
            live("a", Slot.FAV_BID, 0.979, 450.0),
            live("b", Slot.LONG_BID, 0.001, 560.0),
        ]
        self.assertEqual(self.run_reconcile(orders), [])

    def test_places_every_target_when_nothing_rests(self):
        actions = self.run_reconcile([])
        self.assertEqual(actions, [Place(i) for i in self.target.intents])

    def test_replaces_order_out_of_band(self):
        orders = [
            live("a", Slot.FAV_BID, 0.95, 510.2),
            live("b", Slot.LONG_BID, 0.001, 471.71),
        ]
        actions = self.run_reconcile(orders)
        self.assertEqual(actions, [Cancel(("a",)), Place(self.intents[Slot.FAV_BID])])

    def test_replaces_order_whose_size_left_the_band(self):
        orders = [
            live("a", Slot.FAV_BID, 0.98, 300.0),
            live("b", Slot.LONG_BID, 0.001, 471.71),
        ]
        actions = self.run_reconcile(orders)
        self.assertEqual(actions, [Cancel(("a",)), Place(self.intents[Slot.FAV_BID])])

    def test_replaces_fav_bid_above_ev_cap(self):
        fm = market(p_true=0.985)
        target = target_orders(fm, FLOOR_BOOK, Position(), CFG, True)
        orders = [live("a", Slot.FAV_BID, 0.98, 510.2)]
        actions = reconcile(target.intents, orders, target.mid, fm, Position(), CFG)
        self.assertEqual(actions[0], Cancel(("a",)))

    def test_cancels_extras_and_slots_without_intent(self):
        orders = [
            live("a", Slot.FAV_BID, 0.98, 510.2),
            live("b", Slot.FAV_BID, 0.98, 510.2),
            live("c", Slot.LONG_BID, 0.001, 471.71),
            live("d", Slot.FAV_ASK, 0.999, 100.0),
        ]
        self.assertEqual(self.run_reconcile(orders), [Cancel(("b", "d"))])

    def test_cancels_come_before_places(self):
        orders = [
            live("a", Slot.FAV_BID, 0.95, 510.2),
            live("b", Slot.LONG_BID, 0.050, 471.71),
        ]
        actions = self.run_reconcile(orders)
        self.assertEqual(actions[0], Cancel(("a", "b")))
        self.assertEqual(
            actions[1:],
            [Place(self.intents[Slot.FAV_BID]), Place(self.intents[Slot.LONG_BID])],
        )

    def test_no_intents_cancels_everything(self):
        orders = [live("a", Slot.FAV_BID, 0.98, 510.2), live("b", Slot.LONG_BID, 0.001, 471.71)]
        actions = reconcile([], orders, self.target.mid, self.fm, Position(), CFG)
        self.assertEqual(actions, [Cancel(("a", "b"))])


def observe(**changes):
    fields = dict(
        mid=0.96,
        recent_mids=(),
        rewards_active=True,
        accepting_orders=True,
        closed=False,
        approved=True,
        expires_at=NOW + timedelta(days=30),
    )
    fields.update(changes)
    return Observation(**fields)


QUOTING = State(Phase.QUOTING)


class StepStateTest(unittest.TestCase):
    def test_mid_drop_within_window_starts_cooldown(self):
        obs = observe(mid=0.94, recent_mids=((NOW - timedelta(seconds=30), 0.97),))
        state = step_state(QUOTING, obs, NOW, CFG)
        self.assertEqual(state, State(Phase.COOLDOWN, NOW + timedelta(seconds=600), "mid drop"))

    def test_mid_drop_outside_window_is_ignored(self):
        obs = observe(mid=0.94, recent_mids=((NOW - timedelta(seconds=120), 0.97),))
        self.assertEqual(step_state(QUOTING, obs, NOW, CFG), QUOTING)

    def test_cooldown_holds_until_expiry_then_quotes(self):
        cooling = State(Phase.COOLDOWN, NOW + timedelta(seconds=10), "mid drop")
        self.assertEqual(step_state(cooling, observe(), NOW, CFG), cooling)
        later = NOW + timedelta(seconds=11)
        self.assertEqual(step_state(cooling, observe(), later, CFG), QUOTING)

    def test_expiry_retires(self):
        obs = observe(expires_at=NOW)
        self.assertEqual(step_state(QUOTING, obs, NOW, CFG).phase, Phase.RETIRED)

    def test_rewards_removed_retires(self):
        state = step_state(QUOTING, observe(rewards_active=False), NOW, CFG)
        self.assertEqual(state, State(Phase.RETIRED, reason="rewards removed"))

    def test_not_approved_or_closed_retires(self):
        for obs in (observe(approved=False), observe(closed=True), observe(accepting_orders=False)):
            self.assertEqual(step_state(QUOTING, obs, NOW, CFG).phase, Phase.RETIRED)

    def test_retired_is_terminal(self):
        retired = State(Phase.RETIRED, reason="rewards removed")
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
        cooling = State(Phase.COOLDOWN, NOW + timedelta(seconds=10), "mid drop")
        for state in (cooling, State(Phase.BELOW_RANGE)):
            self.assertEqual(step_state(state, observe(paused=True), NOW, CFG).phase, Phase.PAUSED)

    def test_retire_beats_pause(self):
        paused = State(Phase.PAUSED, reason="paused by operator")
        for state in (QUOTING, paused):
            retired = step_state(state, observe(paused=True, rewards_active=False), NOW, CFG)
            self.assertEqual(retired, State(Phase.RETIRED, reason="rewards removed"))

    def test_naked_paused_forces_no_long_bid(self):
        held = Position(fav_shares=100.0, fav_avg=0.97)
        obs = observe(naked_paused=True)
        self.assertFalse(naked_allowed(obs, NOW))
        intents = by_slot(target_orders(market(), FLOOR_BOOK, held, CFG, naked_allowed(obs, NOW)))
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
        intents = by_slot(target_orders(market(), FLOOR_BOOK, farm, CFG, True))
        self.assertNotIn(Slot.FAV_ASK, intents)
        self.assertEqual(intents[Slot.FAV_BID].size, 510.20)

    def test_only_shares_above_the_baseline_are_sold(self):
        baseline = ratchet_baseline(None, Position(fav_shares=300.0, fav_avg=0.97))
        held = Position(fav_shares=400.0, fav_avg=0.972)
        farm = farm_position(held, ratchet_baseline(baseline, held))
        ask = by_slot(target_orders(market(), FLOOR_BOOK, farm, CFG, True))[Slot.FAV_ASK]
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
        target = target_orders(market(), FLOOR_BOOK, Position(), CFG, True)
        intents = by_slot(target)
        actions = [Cancel(("a",)), Place(intents[Slot.FAV_BID]), Place(intents[Slot.LONG_BID])]
        backoffs = {Slot.LONG_BID: next_backoff(None, NOW)}
        self.assertEqual(without_backed_off(actions, backoffs, NOW), actions[:2])
        later = NOW + timedelta(seconds=30)
        self.assertEqual(without_backed_off(actions, backoffs, later), actions)


class WideBookTest(unittest.TestCase):
    def test_wide_book_fav_bid_scores_one_offset_from_its_own_midpoint(self):
        book = Levels(bids=[(0.86, 100.0)], asks=[(0.985, 100.0)], tick=0.001)
        target = target_orders(market(min_size=20.0), book, Position(), CFG, True)
        fav = by_slot(target)[Slot.FAV_BID]
        self.assertEqual(fav.price, 0.965)
        self.assertAlmostEqual(target.mid, 0.975)
        self.assertAlmostEqual(fav.score, SCORE_1C)


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
        info = MarketInfo((FAV, LONG), 0.0, 0.085, 50.0, 0.001, False, True, False)
        self.assertTrue(rewards_active(info, "0xc0nd", {"0xc0nd": 2.1}))
        self.assertTrue(rewards_active(info, "0xc0nd", None))
        self.assertFalse(rewards_active(info, "0xc0nd", {"0xother": 2.1}))
        self.assertFalse(rewards_active(info._replace(native_rate=5.0, v=0.0), "0xc0nd", None))

    def test_fill_events_name_the_slot_that_filled(self):
        before = Position(fav_shares=100.0, fav_avg=0.97, long_shares=0.0)
        after = Position(fav_shares=60.0, fav_avg=0.97, long_shares=300.0, long_avg=0.001)
        self.assertEqual(fill_events(before, after), [(Slot.FAV_ASK, 40.0), (Slot.LONG_BID, 300.0)])


CID = "0xc0nd"
INFO = MarketInfo((FAV, LONG), 5.0, 0.035, 50.0, 0.001, False, True, False)
METRICS = {"market": {"fav_token_id": FAV, "slug": "test-market", "question": "Test?",
                      "end_date": (NOW + timedelta(days=100)).isoformat(),
                      "fav_outcome": "No", "rate_per_day": 100.0}}


class RanAway(BaseException):
    """Raised by a fake when the farmer keeps cycling after it should have stopped."""


class FarmerCycleTest(unittest.TestCase):
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
        )], [CID], NOW)
        farm_db.approve_market(self.ui, CID, 0.995, 25.0)
        self.book_fetches = 0
        self.on_book_fetch = lambda count: None
        for name, fake in (
            ("fetch_market_info", mock.Mock(return_value=INFO)),
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
        return {FAV: FLOOR_BOOK}

    def started_farmer(self):
        farmer = Farmer(self.args, config_from(self.args))
        self.addCleanup(farmer.http.close)
        self.addCleanup(farmer.db.close)
        farmer.start()
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

    def test_dry_cycle_publishes_state_status_and_places(self):
        farmer = self.started_farmer()
        farmer.cycle()

        row, slots = self.farm_state()
        self.assertEqual(row["phase"], "QUOTING")
        self.assertEqual(slots["FAV_BID"]["decision"], "place")
        self.assertEqual(slots["FAV_BID"]["target"]["price"], 0.98)
        self.assertEqual(json.loads(row["position"])["baseline"], {"fav": 0.0, "long": 0.0})
        status = self.query("SELECT * FROM farmer_status")[0]
        self.assertEqual((status["mode"], status["managed"], status["stopped_at"]), ("dry", 1, None))
        self.assertIsNotNone(status["last_cycle_at"])
        places = [(e["slot"], e["price"]) for e in self.events() if e["kind"] == "PLACE"]
        self.assertEqual(places, [("FAV_BID", 0.98), ("LONG_BID", 0.001)])
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

    def test_pause_cancels_everything_next_cycle_and_resume_quotes_again(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()

        farm_db.set_paused(self.ui, CID, True)
        farmer.cycle()
        row, slots = self.farm_state()
        self.assertEqual(row["phase"], "PAUSED")
        self.assertEqual((row["q1"], row["q2"]), (0.0, 0.0))
        self.assertEqual({name: s["target"] for name, s in slots.items()},
                         dict.fromkeys(slots, None))
        kinds = [(e["kind"], e["slot"]) for e in self.events(before)]
        self.assertEqual(kinds, [("PHASE", None), ("CANCEL", "FAV_BID"), ("CANCEL", "LONG_BID")])

        farm_db.set_paused(self.ui, CID, False)
        farmer.cycle()
        row, _ = self.farm_state()
        self.assertEqual(row["phase"], "QUOTING")

    def test_settings_row_changes_the_effective_config(self):
        farmer = self.started_farmer()
        farmer.cycle()
        before = self.last_event_id()

        farm_db.put_setting(self.ui, "bid_offset", 0.015, NOW)
        farmer.cycle()
        settings = [e for e in self.events(before) if e["kind"] == "SETTINGS"]
        self.assertEqual(len(settings), 1)
        self.assertEqual(json.loads(settings[0]["data"]), {"bid_offset": [0.01, 0.015]})
        _, slots = self.farm_state()
        self.assertEqual(slots["FAV_BID"]["target"]["price"], 0.975)
        status = self.query("SELECT config FROM farmer_status")[0]
        self.assertEqual(json.loads(status["config"])["bid_offset"], 0.015)

        farmer.cycle()
        self.assertEqual(len([e for e in self.events(before) if e["kind"] == "SETTINGS"]), 1)

    def test_market_capital_overrides_the_configured_capital(self):
        farm_db.set_capital(self.ui, CID, 98.0)
        farmer = self.started_farmer()
        farmer.cycle()
        row, slots = self.farm_state()
        self.assertEqual(row["capital_usd"], 98.0)
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

    def test_rows_of_markets_no_longer_managed_are_deleted(self):
        with self.ui:
            self.ui.execute("INSERT INTO farm_state (condition_id, updated_at, phase) "
                            "VALUES ('0xgone', '2026-01-01T00:00:00+00:00', 'QUOTING')")
        farmer = self.started_farmer()
        farmer.cycle()
        self.assertEqual([r[0] for r in self.query("SELECT condition_id FROM farm_state")], [CID])


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
        self.assertEqual([(r["fav_bid"], r["long_bid"]) for r in rows], [(0.98, 0.001)] * 3)
        self.assertEqual((rows[0]["mid"], rows[0]["book_mid"]), (0.99, 0.99))

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


if __name__ == "__main__":
    unittest.main()
