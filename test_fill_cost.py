import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from fill_cost import (
    DEFAULT_PRIOR,
    DISTANCES,
    FLOOR_RATE,
    STRENGTHS,
    History,
    Hit,
    Prior,
    fill_curve,
    fill_source,
    fit_prior,
    history_from_json,
    history_to_json,
    loss_per_share,
    make_history,
    prior_from_json,
    prior_to_json,
    years_until,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
T = NOW.timestamp()
DAY = 86400.0


class LossTest(unittest.TestCase):
    def test_a_long_dated_fill_costs_more_than_its_discount_to_p_true(self):
        # A year out at 40%: 0.97 / 1.4 = 0.692857 today.
        self.assertAlmostEqual(loss_per_share(0.95, 0.97, 1.0, 0.40), 0.95 - 0.97 / 1.4)

    def test_a_short_dated_fill_below_the_discounted_value_is_welcome(self):
        self.assertLess(loss_per_share(0.95, 0.99, 2 / 365.25, 0.40), 0)

    def test_years_run_to_the_end_date_and_never_go_negative(self):
        self.assertAlmostEqual(years_until(NOW + timedelta(days=365.25), NOW), 1.0)
        self.assertEqual(years_until(NOW - timedelta(days=1), NOW), 0.0)


B = T - 10 * DAY
STEADY = [(T - 70 * DAY, 0.95)]


def history_of(*hits, mids=STEADY, start=0.0, floor=0.90):
    """History of hits given as (seconds after B, deepest price)."""
    return make_history([Hit(B + at, deep) for at, deep in hits], mids, start, NOW, floor)


def at(values, distance):
    return values[DISTANCES.index(distance)]


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


PRIOR = Prior(
    rates=(0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05, 0.04, 0.03, 0.02, 0.01, 0.005),
    strengths=(20.0,) * len(DISTANCES),
    markets=50,
    at=NOW,
    floor=0.90,
)
HISTORY = History(events=(20, 16, 12, 10, 8, 6, 4, 2, 2, 1, 0, 0), days=20.0, read_at=NOW, floor=0.90)


class CurveTest(unittest.TestCase):
    def test_a_history_shrinks_toward_the_prior_by_the_prior_strength(self):
        curve = fill_curve(HISTORY, PRIOR, 0.90)
        self.assertTrue(curve.from_history)
        self.assertAlmostEqual(curve.per_day(0.0), (20 + 20 * 0.6) / (20 + 20))
        self.assertAlmostEqual(curve.per_day(0.01), (8 + 20 * 0.2) / (20 + 20))
        self.assertAlmostEqual(curve.per_day(0.10), (0 + 20 * 0.005) / (20 + 20))

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


def histories(counts, days=60.0, floor=0.90):
    return [History((k,) * len(DISTANCES), days, NOW, floor) for k in counts]


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

    def test_a_distance_without_events_pools_to_zero(self):
        events = (5,) * (len(DISTANCES) - 1) + (0,)
        prior = fit_prior([History(events, 30.0, NOW, 0.90)] * 25, NOW, 0.90)
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

    def test_an_old_stored_tape_reads_as_no_history(self):
        old = {"sweeps": [[T - DAY, 100.0]], "span_days": 5.0, "observed": True,
               "capped": True, "fetched_at": NOW.isoformat()}
        self.assertIsNone(history_from_json(old))
        self.assertIsNone(prior_from_json(old))

    def test_another_grid_or_version_reads_as_none(self):
        stored = history_to_json(HISTORY)
        regrid = {**stored, "distances": stored["distances"][:-1], "events": stored["events"][:-1]}
        self.assertIsNone(history_from_json(regrid))
        self.assertIsNone(history_from_json({**stored, "v": 2}))
        prior = prior_to_json(PRIOR)
        self.assertIsNone(prior_from_json({**prior, "distances": [d * 2 for d in DISTANCES]}))
        self.assertIsNone(prior_from_json({**prior, "v": 2}))

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
                    {**stored, "floor": "high"}):
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
                    {**prior, "floor": 1.5}):
            self.assertIsNone(prior_from_json(bad), bad)


if __name__ == "__main__":
    unittest.main()
