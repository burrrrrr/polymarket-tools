import unittest
from datetime import datetime, timedelta, timezone

from fill_cost import (
    FillParams,
    Hit,
    Sweep,
    Tape,
    depth_ahead,
    fills_per_day,
    loss_per_share,
    make_tape,
    tape_from_json,
    tape_to_json,
    years_until,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
T = NOW.timestamp()
DAY = 86400.0
PARAMS = FillParams(hurdle_rate=0.40, depth_haircut=0.5, lookback_days=30.0, prior_weight=1.0)


def tape_of(*offsets_and_shares, full_page_seen=False, oldest=None):
    hits = [Hit(T - DAY + at, shares) for at, shares in offsets_and_shares]
    return make_tape(hits, oldest, NOW, 30.0, full_page_seen)


class GroupingTest(unittest.TestCase):
    def test_hits_within_a_minute_of_the_last_form_one_sweep(self):
        tape = tape_of((0, 100), (30, 50), (59, 25), (120, 10))
        self.assertEqual([s.shares for s in tape.sweeps], [175, 10])

    def test_a_burst_chaining_past_the_first_minute_is_still_one_sweep(self):
        # An anchored 60 s window would split this at 95 s.
        tape = tape_of((0, 100), (50, 100), (95, 100))
        self.assertEqual([s.shares for s in tape.sweeps], [300])

    def test_a_drip_cannot_chain_past_the_sweep_span_cap(self):
        # A hit every 59 s for 20 minutes: chained without a cap it is one
        # 2,100-share sweep that would clear any queue.
        tape = tape_of(*[(59 * n, 100) for n in range(21)])
        self.assertEqual([s.shares for s in tape.sweeps], [1100, 1000])

    def test_hits_older_than_the_lookback_are_dropped(self):
        hits = [Hit(T - 31 * DAY, 500), Hit(T - DAY, 20)]
        tape = make_tape(hits, None, NOW, 30.0, False)
        self.assertEqual([s.shares for s in tape.sweeps], [20])

    def test_hits_arriving_newest_first_group_the_same(self):
        hits = [Hit(T - 10, 5), Hit(T - 40, 7), Hit(T - 4000, 9)]
        tape = make_tape(hits, None, NOW, 30.0, False)
        self.assertEqual(tape.sweeps, (Sweep(T - 4000, 9), Sweep(T - 40, 12)))


class SpanTest(unittest.TestCase):
    def test_a_history_reaching_the_lookback_spans_the_lookback(self):
        self.assertEqual(tape_of((0, 1), oldest=T - 12 * DAY).span_days, 30.0)

    def test_a_capped_history_spans_only_what_it_covers(self):
        # Dividing a 12-day capped tape by 30 would understate fills 2.5x.
        tape = tape_of((0, 1), full_page_seen=True, oldest=T - 12 * DAY)
        self.assertEqual((tape.span_days, tape.capped), (12.0, True))

    def test_a_full_page_reaching_past_the_lookback_spans_the_lookback(self):
        tape = tape_of((0, 1), full_page_seen=True, oldest=T - 40 * DAY)
        self.assertEqual((tape.span_days, tape.capped), (30.0, False))

    def test_a_capped_history_of_minutes_extrapolates_from_an_hour(self):
        tape = tape_of((0, 1), full_page_seen=True, oldest=T - 600)
        self.assertEqual(tape.span_days, 1 / 24)


class FillsPerDayTest(unittest.TestCase):
    def test_sweeps_reach_the_bid_past_the_haircut_queue_plus_the_prior(self):
        # Behind 400 shares, haircut to 200: 1000 fills all 500, 300 fills 100,
        # 150 reaches nothing (not -50). The prior adds one full fill a month.
        tape = Tape((Sweep(0, 1000), Sweep(1, 300), Sweep(2, 150)), 30.0)
        self.assertAlmostEqual(fills_per_day(tape, 500, 400, PARAMS), (500 + 100) / 30 + 500 / 30)

    def test_no_size_never_fills(self):
        tape = Tape((Sweep(0, 1000),), 30.0)
        self.assertEqual(fills_per_day(tape, 0, 0, PARAMS), 0.0)

    def test_an_unobserved_tape_fills_at_the_prior_alone(self):
        self.assertAlmostEqual(fills_per_day(Tape.unobserved(30.0), 300, 0, PARAMS), 10.0)
        no_prior = PARAMS._replace(prior_weight=0.0)
        self.assertEqual(fills_per_day(Tape.unobserved(30.0), 300, 0, no_prior), 0.0)

    def test_depth_ahead_counts_higher_bids_and_the_level_joined(self):
        bids = [(0.95, 100.0), (0.94, 50.0), (0.93, 70.0)]
        self.assertEqual(depth_ahead(bids, 0.94), 150.0)
        self.assertEqual(depth_ahead(bids, 0.945), 100.0)
        self.assertEqual(depth_ahead(bids, 0.96), 0.0)


class LossTest(unittest.TestCase):
    def test_a_long_dated_fill_costs_more_than_its_discount_to_p_true(self):
        # A year out at 40%: 0.97 / 1.4 = 0.692857 today.
        self.assertAlmostEqual(loss_per_share(0.95, 0.97, 1.0, 0.40), 0.95 - 0.97 / 1.4)

    def test_a_short_dated_fill_below_the_discounted_value_is_welcome(self):
        self.assertLess(loss_per_share(0.95, 0.99, 2 / 365.25, 0.40), 0)

    def test_years_run_to_the_end_date_and_never_go_negative(self):
        self.assertAlmostEqual(years_until(NOW + timedelta(days=365.25), NOW), 1.0)
        self.assertEqual(years_until(NOW - timedelta(days=1), NOW), 0.0)


class StoredTapeTest(unittest.TestCase):
    def test_a_tape_round_trips_through_json(self):
        tape = tape_of((0, 100), (500, 20), full_page_seen=True, oldest=T - 5 * DAY)
        self.assertEqual(tape_from_json(tape_to_json(tape)), tape)

    def test_no_stored_tape_reads_as_none(self):
        self.assertIsNone(tape_from_json(None))

    def test_trimming_to_the_newest_sweeps_rates_them_over_the_time_they_cover(self):
        # 600 sweeps of 10 shares, one an hour: 240 shares a day.
        hits = [Hit(T - 3600 * (n + 1), 10) for n in range(600)]
        tape = make_tape(hits, T - 30 * DAY, NOW, 30.0, False)
        stored = tape_from_json(tape_to_json(tape))
        self.assertEqual(len(stored.sweeps), 500)
        self.assertEqual(stored.sweeps[-1], tape.sweeps[-1])
        no_prior = PARAMS._replace(prior_weight=0.0)
        self.assertAlmostEqual(fills_per_day(stored, 1000, 0, no_prior), 240.0)


if __name__ == "__main__":
    unittest.main()
