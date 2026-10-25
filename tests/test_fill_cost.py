import json
import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from farm.fill_cost import (
    DEFAULT_PRIOR,
    DISTANCES,
    EXIT_DISTANCE,
    FLOOR_RATE,
    PRE_SWEEP_S,
    STRENGTHS,
    SWEEP_SIZES,
    WHOLE_BID,
    History,
    Hit,
    Prior,
    fill_curve,
    fill_source,
    fit_prior,
    history_from_json,
    history_to_json,
    make_history,
    prior_from_json,
    prior_to_json,
    sales_per_day,
    share_value,
    sweep_fill,
    years_until,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
T = NOW.timestamp()
DAY = 86400.0


def summed_value(p_true, sale_price, per_day, years, hurdle_rate, steps=200000):
    """share_value by summing the sale density over a grid instead of its
    closed form."""
    lam, delta = per_day * 365.25, math.log(1 + hurdle_rate)
    dt = years / steps
    sold = sum(lam * math.exp(-(lam + delta) * (i + 0.5) * dt) * dt for i in range(steps))
    return sale_price * sold + p_true * math.exp(-(lam + delta) * years)


class LossTest(unittest.TestCase):
    def test_a_share_that_never_sells_is_worth_p_true_discounted_to_resolution(self):
        # A year out at 40%: 0.97 / 1.4 = 0.692857 today, whatever it would sell for.
        self.assertAlmostEqual(share_value(0.97, 0.95, 0.0, 1.0, 0.40), 0.97 / 1.4)

    def test_a_share_sold_at_once_is_worth_its_sale_price(self):
        self.assertAlmostEqual(share_value(0.97, 0.95, 1e6, 1.0, 0.40), 0.95, places=6)

    def test_a_week_long_hold_matches_the_sale_density_summed(self):
        for years in (1.0, 0.02):
            with self.subTest(years=years):
                self.assertAlmostEqual(share_value(0.97, 0.95, 1 / 7, years, 0.40),
                                       summed_value(0.97, 0.95, 1 / 7, years, 0.40), places=7)

    def test_what_is_unsold_at_resolution_is_worth_p_true_not_the_sale_price(self):
        # No hurdle, 14 days out, sold at 1 / 37.44 a day: 31.2% sell at 94.5c
        # and the 68.8% left resolve worth 97c.
        self.assertAlmostEqual(share_value(0.97, 0.945, 1 / 37.44, 14 / 365.25, 0.0),
                               0.312 * 0.945 + 0.688 * 0.97, places=5)
        # At 40%, 0.228 years out: 90% sell, discounted over their wait, and
        # the tenth unsold is worth 97c at resolution. 91.9c today.
        self.assertAlmostEqual(share_value(0.97, 0.945, 1 / 37.44, 0.228, 0.40), 0.91915,
                               places=5)

    def test_without_a_hurdle_or_a_sale_nothing_is_lost(self):
        self.assertEqual(share_value(0.97, 0.95, 0.0, 1.0, 0.0), 0.97)

    def test_a_short_dated_fill_below_the_discounted_value_loses_less_than_nothing(self):
        self.assertLess(0.95 - share_value(0.99, 0.953, 1 / 37.44, 2 / 365.25, 0.40), 0)

    def test_years_run_to_the_end_date_and_never_go_negative(self):
        self.assertAlmostEqual(years_until(NOW + timedelta(days=365.25), NOW), 1.0)
        self.assertEqual(years_until(NOW - timedelta(days=1), NOW), 0.0)


class MeasuredExitTest(unittest.TestCase):
    """In the 30 days after a fill the farmer's ask sold 67.4% of a 100-share
    fill and 41.9% of a 1,000-share one."""

    def sold(self, shares):
        """The fraction of an inventory gone in 30 days at sales_per_day's rate."""
        return 1 - math.exp(-30 * sales_per_day(shares))

    def test_the_measured_inventories_sell_their_measured_fractions(self):
        self.assertAlmostEqual(self.sold(100.0), 0.674)
        self.assertAlmostEqual(self.sold(1000.0), 0.419)
        # 67.4% in 30 days is a mean hold of 27 days, 41.9% one of 55.
        self.assertAlmostEqual(sales_per_day(100.0), 0.03736, places=5)
        self.assertAlmostEqual(sales_per_day(1000.0), 0.01810, places=5)

    def test_between_them_the_fraction_is_linear_in_the_log_of_the_size(self):
        # 316.2 shares is halfway from 100 to 1,000 in ln.
        self.assertAlmostEqual(self.sold(math.sqrt(100.0 * 1000.0)), (0.674 + 0.419) / 2)

    def test_2000_shares_sell_what_the_line_through_the_two_sizes_says(self):
        # Each tenfold in size takes 25.5 points off, so 34.2% of 2,000 shares
        # sell in 30 days, a mean hold of 72 days. Measured, 30.9% did.
        self.assertAlmostEqual(self.sold(2000.0), 0.674 - 0.255 * math.log10(20.0))
        self.assertAlmostEqual(self.sold(2000.0), 0.342, places=3)
        self.assertAlmostEqual(sales_per_day(2000.0), 0.01396, places=5)

    def test_a_smaller_inventory_sells_no_faster_than_100_shares(self):
        for shares in (0.0, 1.0, 50.0):
            with self.subTest(shares=shares):
                self.assertAlmostEqual(self.sold(shares), 0.674)

    def test_a_larger_inventory_sells_slower_down_to_5_percent(self):
        # 20,000 shares sell 8.7%, and the line would reach 4.2% at 30,000.
        self.assertAlmostEqual(self.sold(20000.0), 0.674 - 0.255 * math.log10(200.0))
        self.assertAlmostEqual(self.sold(30000.0), 0.05)
        self.assertAlmostEqual(self.sold(1e6), 0.05)


B = T - 10 * DAY
STEADY = [(T - 70 * DAY, 0.95)]


def history_of(*hits, mids=STEADY, start=0.0, floor=0.90):
    """History of hits given as (seconds after B, deepest price)."""
    return make_history([Hit(B + at, deep) for at, deep in hits], mids, start, NOW, floor)


def at(values, distance):
    return values[DISTANCES.index(distance)]


class SweepFillTest(unittest.TestCase):
    """A sweep that reaches a bid fills min(bid, its size), on average over
    the measured sweep sizes."""

    def test_a_bid_fills_the_mean_of_min_bid_and_each_sweep(self):
        self.assertAlmostEqual(sweep_fill(150.0, (100.0, 200.0, 300.0, 400.0)),
                               (100 + 150 + 150 + 150) / 4)

    def test_a_bid_under_every_sweep_fills_whole(self):
        self.assertEqual(sweep_fill(20.0, (100.0, 200.0)), 20.0)

    def test_a_bid_over_every_sweep_fills_the_mean_sweep(self):
        self.assertEqual(sweep_fill(10_000.0, (100.0, 200.0)), 150.0)

    def test_the_whole_bid_table_fills_any_bid_whole(self):
        self.assertEqual(sweep_fill(1e6, WHOLE_BID), 1e6)

    def test_the_measured_table_grows_slower_than_the_bid(self):
        fills = [sweep_fill(q) for q in (10.0, 100.0, 1000.0, 10_000.0)]
        self.assertEqual(fills, sorted(fills))
        self.assertGreater(fills[0], 0.8 * 10.0)
        self.assertLess(fills[3], 1000.0)
        self.assertLess((fills[3] - fills[2]) / 9000, (fills[2] - fills[1]) / 900)
        self.assertEqual(len(SWEEP_SIZES), 40)
        self.assertEqual(list(SWEEP_SIZES), sorted(SWEEP_SIZES))

    def test_a_curve_fills_with_its_own_table_the_measured_one_by_default(self):
        curve = fill_curve(None, DEFAULT_PRIOR, DEFAULT_PRIOR.floor)
        self.assertEqual(curve.sweep_fill(500.0), sweep_fill(500.0))
        whole = curve._replace(sweep_sizes=WHOLE_BID)
        self.assertEqual(whole.sweep_fill(500.0), 500.0)


class MakeHistoryTest(unittest.TestCase):
    def test_a_sweep_is_measured_from_its_first_hit_against_the_mid_before_it_moved(self):
        # The sweep itself drops the mid to 0.93 after 30 s; its third hit,
        # measured alone, would reach only 1c.
        mids = [(B - 3600, 0.95), (B + 30, 0.93)]
        history = history_of((0, 0.94), (50, 0.93), (95, 0.92), mids=mids)
        self.assertEqual(at(history.events, 0.02), 1)

    def test_hits_arriving_newest_first_chain_the_same(self):
        # /trades lists newest first; unsorted, the sweep would start at its
        # last hit and be measured against the mid it had already moved.
        mids = [(B - 3600, 0.95), (B + 30, 0.93)]
        history = history_of((95, 0.92), (50, 0.93), (0, 0.94), mids=mids)
        self.assertEqual(at(history.events, 0.02), 1)

    def test_a_drip_cannot_chain_past_the_sweep_span_cap(self):
        # A hit every 59 s for 20 minutes: the deep last hit starts a second
        # sweep at 649 s, measured against the 0.94 mid it had by then.
        hits = [(59 * n, 0.945) for n in range(20)] + [(59 * 20, 0.92)]
        history = history_of(*hits, mids=[(B - 3600, 0.95), (B + 300, 0.94)])
        self.assertEqual((at(history.events, 0.015), at(history.events, 0.02)), (1, 0))

    def test_the_pre_sweep_mid_is_the_last_one_a_minute_or_more_before_the_sweep(self):
        mids = [(B - 3600, 0.99), (B - 60, 0.95), (B - 59, 0.90)]
        history = history_of((0, 0.93), mids=mids)
        self.assertEqual([at(history.events, d) for d in (0.015, 0.02, 0.05)], [1, 0, 0])

    def test_a_fill_at_the_bid_does_not_trade_through_it(self):
        history = history_of((0, 0.94))
        self.assertEqual((at(history.events, 0.0075), at(history.events, 0.01)), (1, 0))

    def test_sweeps_count_only_while_the_mid_is_at_or_above_the_floor(self):
        at_floor = history_of((0, 0.88), mids=[(T - 70 * DAY, 0.90)])
        below = history_of((0, 0.88), mids=[(T - 70 * DAY, 0.8999)])
        lower_floor = history_of((0, 0.88), mids=[(T - 70 * DAY, 0.8999)], floor=0.85)
        self.assertEqual([h.events[0] for h in (at_floor, below, lower_floor)], [1, 0, 1])
        self.assertEqual(lower_floor.floor, 0.85)

    def test_sweeps_within_an_hour_of_the_last_counted_event_are_one_event(self):
        # The third is within an hour of the second but not of the first counted.
        history = history_of((0, 0.93), (3000, 0.93), (6000, 0.93))
        self.assertEqual(history.events[0], 2)

    def test_a_deep_sweep_soon_after_a_shallow_one_counts_at_its_own_reach(self):
        history = history_of((0, 0.945), (1800, 0.92))
        self.assertEqual(history.events[:8], (1, 1, 1, 1, 1, 1, 1, 0))

    def test_history_starts_where_the_trades_are_complete(self):
        history = history_of((-DAY, 0.93), (DAY, 0.93), start=T - 10 * DAY + 3600)
        self.assertEqual(history.events[0], 1)
        self.assertAlmostEqual(history.days, 10 - 1 / 24)

    def test_history_reaches_back_no_further_than_the_lookback(self):
        history = history_of((-51 * DAY, 0.93), (-49 * DAY, 0.93), (DAY, 0.93))
        self.assertEqual((history.events[0], history.days), (2, 60.0))

    def test_history_starts_a_minute_after_the_first_mid(self):
        history = history_of((0, 0.93), (30, 0.93), mids=[(B - 30, 0.95)])
        self.assertEqual(history.events[0], 0)
        later = history_of((30, 0.93), mids=[(B - 30, 0.95)])
        self.assertEqual(later.events[0], 1)
        self.assertAlmostEqual(later.days, 10 - 30 / DAY)

    def test_no_mids_is_no_history(self):
        history = history_of((0, 0.5), mids=[])
        self.assertEqual((history.events, history.days), ((0,) * len(DISTANCES), 0.0))

    def test_sweeps_from_now_on_are_left_for_the_next_read(self):
        history = history_of((10 * DAY, 0.93))
        self.assertEqual(history.events[0], 0)


class ExitTest(unittest.TestCase):
    def test_a_hold_ends_when_the_mid_is_back_where_the_sweep_found_it(self):
        mids = [(B - 3600, 0.95), (B + 30, 0.93), (B + DAY, 0.949), (B + 3 * DAY, 0.95)]
        history = history_of((0, 0.93), mids=mids)
        self.assertEqual(history.exits, 1)
        self.assertAlmostEqual(history.held_days, 3.0)

    def test_a_hold_still_running_counts_up_to_the_read(self):
        # The mid recovers only after the read, ten days after the sweep.
        mids = [(B - 3600, 0.95), (B + 30, 0.93), (T + DAY, 0.95)]
        history = history_of((0, 0.93), mids=mids)
        self.assertEqual(history.exits, 0)
        self.assertAlmostEqual(history.held_days, 10.0)

    def test_the_minute_the_sweep_began_in_is_no_recovery(self):
        early = history_of((0, 0.93), mids=[(B - 3600, 0.95), (B + 59, 0.95), (B + DAY, 0.95)])
        self.assertAlmostEqual(early.held_days, 1.0)
        on_time = history_of((0, 0.93), mids=[(B - 3600, 0.95), (B + PRE_SWEEP_S, 0.95)])
        self.assertAlmostEqual(on_time.held_days, PRE_SWEEP_S / DAY)

    def test_a_sweep_reaching_no_farther_than_the_exit_distance_starts_no_hold(self):
        mids = [(B - 3600, 0.95), (B + DAY, 0.95)]
        history = history_of((0, 0.95 - EXIT_DISTANCE), (7200, 0.942), mids=mids)
        self.assertEqual(at(history.events, 0.0075), 2)
        self.assertEqual((history.exits, history.held_days), (0, 0.0))

    def test_a_burst_of_sweeps_starts_one_hold(self):
        mids = [(B - 3600, 0.95), (B + 2 * DAY, 0.95)]
        history = history_of((0, 0.93), (1800, 0.93), mids=mids)
        self.assertEqual((at(history.events, EXIT_DISTANCE), history.exits), (1, 1))
        self.assertAlmostEqual(history.held_days, 2.0)


PRIOR = Prior(
    rates=(0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05, 0.04, 0.03, 0.02, 0.01, 0.005),
    strengths=(20.0,) * len(DISTANCES),
    markets=50,
    at=NOW,
    floor=0.90,
    exit_rate=0.2,
    exit_strength=10.0,
)
HISTORY = History(events=(20, 16, 12, 10, 8, 6, 4, 2, 2, 1, 0, 0), days=20.0, read_at=NOW,
                  floor=0.90, exits=7, held_days=30.0)


class CurveTest(unittest.TestCase):
    def test_a_history_shrinks_toward_the_prior_by_the_prior_strength(self):
        curve = fill_curve(HISTORY, PRIOR, 0.90)
        self.assertTrue(curve.from_history)
        self.assertAlmostEqual(curve.per_day(0.0), (20 + 20 * 0.6) / (20 + 20))
        self.assertAlmostEqual(curve.per_day(0.01), (8 + 20 * 0.2) / (20 + 20))
        self.assertAlmostEqual(curve.per_day(0.10), (0 + 20 * 0.005) / (20 + 20))

    def test_the_exit_rate_shrinks_toward_the_prior_by_its_strength(self):
        self.assertAlmostEqual(fill_curve(HISTORY, PRIOR, 0.90).exit_per_day,
                               (7 + 10.0 * 0.2) / (30.0 + 10.0))
        self.assertEqual(fill_curve(None, PRIOR, 0.90).exit_per_day, 0.2)
        self.assertEqual(fill_curve(HISTORY._replace(floor=0.92), PRIOR, 0.90).exit_per_day, 0.2)

    def test_between_grid_points_the_rate_is_linear(self):
        curve = fill_curve(HISTORY, PRIOR, 0.90)
        self.assertAlmostEqual(curve.per_day(0.0125), (0.3 + 0.2) / 2)

    def test_outside_the_grid_the_rate_is_flat(self):
        curve = fill_curve(HISTORY, PRIOR, 0.90)
        self.assertAlmostEqual(curve.per_day(-0.003), 0.8)
        self.assertAlmostEqual(curve.per_day(0.2), 0.0025)

    def test_a_nearer_bid_never_fills_less_often_than_a_farther_one(self):
        bumped = PRIOR._replace(rates=(0.6, 0.5, 0.4, 0.3, 0.1, 0.25) + PRIOR.rates[6:])
        curve = fill_curve(None, bumped, 0.90)
        self.assertAlmostEqual(curve.per_day(0.01), 0.25)
        self.assertAlmostEqual(curve.per_day(0.0075), 0.3)

    def test_a_distance_no_market_reached_still_fills_at_the_floor_rate(self):
        empty = PRIOR._replace(rates=PRIOR.rates[:-1] + (0.0,))
        curve = fill_curve(HISTORY, empty, 0.90)
        self.assertEqual(curve.per_day(0.10), FLOOR_RATE)

    def test_without_a_history_the_prior_alone_prices_fills(self):
        curve = fill_curve(None, PRIOR, 0.90)
        self.assertFalse(curve.from_history)
        self.assertAlmostEqual(curve.per_day(0.01), 0.2)

    def test_a_history_read_at_another_floor_is_not_this_markets_history(self):
        other = HISTORY._replace(floor=0.92)
        self.assertEqual(fill_curve(other, PRIOR, 0.90), fill_curve(None, PRIOR, 0.90))

    def test_a_history_of_no_days_is_no_history(self):
        # A read whose mids came back empty saw nothing of the market.
        empty = HISTORY._replace(events=(0,) * len(DISTANCES), days=0.0)
        curve = fill_curve(empty, PRIOR, 0.90)
        self.assertEqual(curve, fill_curve(None, PRIOR, 0.90))
        self.assertFalse(curve.from_history)
        self.assertEqual(fill_source(empty, PRIOR, 0.90), "prior")

    def test_a_prior_fit_at_another_floor_gives_way_to_the_default(self):
        curve = fill_curve(None, PRIOR._replace(floor=0.92), 0.90)
        self.assertAlmostEqual(curve.per_day(0.01), DEFAULT_PRIOR.rates[DISTANCES.index(0.01)])

    def test_the_source_names_where_the_rates_came_from(self):
        self.assertEqual(fill_source(HISTORY, PRIOR, 0.90), "history")
        self.assertEqual(fill_source(None, PRIOR, 0.90), "prior")
        self.assertEqual(fill_source(HISTORY._replace(floor=0.92), PRIOR, 0.90), "prior")
        self.assertEqual(fill_source(None, DEFAULT_PRIOR, 0.90), "default")
        self.assertEqual(fill_source(None, PRIOR._replace(floor=0.92), 0.90), "default")


def histories(counts, days=60.0, floor=0.90, exits=None, held=None):
    exits = exits or [0] * len(counts)
    held = held or [0.0] * len(counts)
    return [History((k,) * len(DISTANCES), days, NOW, floor, e, h)
            for k, e, h in zip(counts, exits, held)]


def poisson(rng, mean):
    limit, k, p = math.exp(-mean), 0, rng.random()
    while p > limit:
        k, p = k + 1, p * rng.random()
    return k


class FitPriorTest(unittest.TestCase):
    def test_the_strength_recovers_how_much_rates_differ_across_markets(self):
        # Rates gamma-distributed around 0.1 a day with strength 10 days.
        rng = random.Random(7)
        counts = [poisson(rng, rng.gammavariate(10 * 0.1, 1 / 10) * 60) for _ in range(600)]
        prior = fit_prior(histories(counts), NOW, 0.90)
        self.assertAlmostEqual(prior.rates[0], sum(counts) / (60.0 * 600))
        self.assertTrue(10 ** 0.8 <= prior.strengths[0] <= 10 ** 1.2, prior.strengths[0])

    def test_markets_sharing_one_rate_pool_at_the_largest_strength(self):
        prior = fit_prior(histories([6] * 30), NOW, 0.90)
        self.assertAlmostEqual(prior.rates[0], 0.1)
        self.assertEqual(prior.strengths[0], STRENGTHS[-1])

    def test_exits_pool_over_the_held_days(self):
        prior = fit_prior(histories([5] * 30, exits=[1, 4] * 15, held=[10.0] * 30), NOW, 0.90)
        self.assertAlmostEqual(prior.exit_rate, 75 / 300.0)
        self.assertEqual(prior.exit_strength, STRENGTHS[-1])

    def test_markets_whose_exit_rates_differ_pool_at_a_weaker_strength(self):
        prior = fit_prior(histories([50] * 30, exits=[0, 40] * 15, held=[40.0, 4.0] * 15),
                          NOW, 0.90)
        self.assertAlmostEqual(prior.exit_rate, 600 / 660.0)
        self.assertLess(prior.exit_strength, 10.0)

    def test_without_any_held_days_the_exit_rate_is_the_defaults(self):
        prior = fit_prior(histories([3] * 30), NOW, 0.90)
        self.assertEqual((prior.exit_rate, prior.exit_strength),
                         (DEFAULT_PRIOR.exit_rate, DEFAULT_PRIOR.exit_strength))

    def test_a_distance_without_events_pools_to_zero(self):
        events = (5,) * (len(DISTANCES) - 1) + (0,)
        prior = fit_prior([History(events, 30.0, NOW, 0.90, 0, 0.0)] * 25, NOW, 0.90)
        self.assertEqual((prior.rates[-1], prior.strengths[-1]), (0.0, STRENGTHS[-1]))

    def test_too_few_markets_leave_the_prior_unfit(self):
        others = histories([3] * 10, days=0.0) + histories([3] * 10, floor=0.92)
        self.assertIsNone(fit_prior(histories([3] * 19) + others, NOW, 0.90))
        prior = fit_prior(histories([3] * 20) + others, NOW, 0.90)
        self.assertEqual((prior.markets, prior.at, prior.floor), (20, NOW, 0.90))


class StoredHistoryTest(unittest.TestCase):
    def test_a_history_and_a_prior_round_trip_through_json(self):
        self.assertEqual(history_from_json(history_to_json(HISTORY)), HISTORY)
        self.assertEqual(prior_from_json(prior_to_json(PRIOR)), PRIOR)
        self.assertEqual(prior_from_json(prior_to_json(DEFAULT_PRIOR)), DEFAULT_PRIOR)

    def test_a_history_the_live_farmer_stored_reads_and_writes_back_unchanged(self):
        # A farm_state FAV_BID history from 2026-10-09, as farm.db holds it.
        stored = ('{"v": 2, "distances": [0.0, 0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, '
                  '0.04, 0.05, 0.075, 0.1], "events": [4, 4, 3, 3, 1, 1, 1, 1, 0, 0, 0, 0], '
                  '"days": 36.77603665377255, "read_at": "2026-10-09T19:59:44.566886+00:00", '
                  '"floor": 0.9, "exits": 1, "held_days": 3.777974537037037}')
        history = history_from_json(json.loads(stored))
        self.assertEqual((history.events[0], history.exits), (4, 1))
        self.assertEqual(json.dumps(history_to_json(history)), stored)

    def test_an_old_stored_tape_reads_as_no_history(self):
        old = {"sweeps": [[T - DAY, 100.0]], "span_days": 5.0, "observed": True,
               "capped": True, "fetched_at": NOW.isoformat()}
        self.assertIsNone(history_from_json(old))
        self.assertIsNone(prior_from_json(old))

    def test_another_grid_or_version_reads_as_none(self):
        stored = history_to_json(HISTORY)
        regrid = {**stored, "distances": stored["distances"][:-1], "events": stored["events"][:-1]}
        self.assertIsNone(history_from_json(regrid))
        self.assertIsNone(history_from_json({**stored, "v": 3}))
        prior = prior_to_json(PRIOR)
        self.assertIsNone(prior_from_json({**prior, "distances": [d * 2 for d in DISTANCES]}))
        self.assertIsNone(prior_from_json({**prior, "v": 3}))

    def test_a_row_stored_before_exit_times_reads_as_none(self):
        stored, prior = history_to_json(HISTORY), prior_to_json(PRIOR)
        v1 = {k: v for k, v in stored.items() if k not in ("exits", "held_days")}
        self.assertIsNone(history_from_json({**v1, "v": 1}))
        self.assertIsNone(history_from_json({**stored, "v": 1}))
        v1 = {k: v for k, v in prior.items() if k not in ("exit_rate", "exit_strength")}
        self.assertIsNone(prior_from_json({**v1, "v": 1}))
        self.assertIsNone(prior_from_json({**prior, "v": 1}))

    def test_malformed_values_read_as_none(self):
        stored = history_to_json(HISTORY)
        for bad in (None, "", "x", [], 5, {}, {"v": 1},
                    {**stored, "events": list(range(len(DISTANCES)))},
                    {**stored, "events": [-1] * len(DISTANCES)},
                    {**stored, "events": [1.5] * len(DISTANCES)},
                    {**stored, "events": [1] * (len(DISTANCES) + 1)},
                    {**stored, "days": 61.0},
                    {**stored, "days": float("nan")},
                    {**stored, "read_at": "yesterday"},
                    {**stored, "floor": "high"},
                    {**stored, "exits": 9},
                    {**stored, "exits": -1},
                    {**stored, "exits": 1.0},
                    {**stored, "held_days": -0.5},
                    {**stored, "held_days": float("inf")},
                    {**stored, "held_days": "x"}):
            self.assertIsNone(history_from_json(bad), bad)
        prior = prior_to_json(PRIOR)
        n = len(DISTANCES)
        for bad in (None, "x", {"v": 1},
                    {**prior, "rates": ["x"] * n},
                    {**prior, "rates": [float("nan")] * n},
                    {**prior, "rates": [-0.1] * n},
                    {**prior, "strengths": [0.0] * n},
                    {**prior, "strengths": [1.0] * (n - 1)},
                    {**prior, "markets": "3"},
                    {**prior, "at": 5},
                    {**prior, "floor": 1.5},
                    {**prior, "exit_rate": -0.1},
                    {**prior, "exit_rate": float("nan")},
                    {**prior, "exit_strength": 0.0},
                    {**prior, "exit_strength": float("inf")}):
            self.assertIsNone(prior_from_json(bad), bad)


if __name__ == "__main__":
    unittest.main()
