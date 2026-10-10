import math
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from farm import exit_value
from farm.exit_value import (
    DEFAULT_EXIT_PRIOR,
    EXIT_JSON_VERSION,
    EXIT_TIE_USD,
    SIZE_BASE,
    SIZE_GRID,
    YEAR_DAYS,
    YIELD_GRID,
    BuyTape,
    ExitCurve,
    ExitModel,
    ExitParams,
    ExitPolicy,
    ExitPrior,
    ExitValue,
    Holding,
    Piece,
    Sweep,
    exit_prior_from_json,
    exit_prior_to_json,
    fit_exit_prior,
    sales_rate,
    tape_from_json,
    tape_to_json,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
T = NOW.timestamp()
DAY = 86400.0
START = T - 60 * DAY


def sweep(days_ago, *pieces):
    return Sweep(T - days_ago * DAY, tuple(Piece(p, s) for p, s in pieces))


# Three buys, the newest swept two levels.
SWEEPS = (sweep(30, (0.95, 100.0)), sweep(10, (0.96, 40.0)),
          sweep(2, (0.97, 25.0), (0.965, 300.0)))


def fields(tape):
    return tape.sweeps, tape.read_at, tape.first_at


class BuyTapeTest(unittest.TestCase):
    def test_sweeps_are_held_oldest_first_and_pieces_highest_first(self):
        shuffled = [SWEEPS[2]._replace(pieces=SWEEPS[2].pieces[::-1]), SWEEPS[0], SWEEPS[1]]
        tape = BuyTape(shuffled, NOW, START)
        self.assertEqual(tape.sweeps, SWEEPS)

    def test_a_sweep_that_is_not_a_buy_of_the_favorite_is_refused(self):
        for bad in (sweep(1), sweep(1, (1.2, 10.0)), sweep(1, (0.0, 10.0)),
                    sweep(1, (0.95, 0.0)), sweep(1, (0.95, math.nan)), sweep(61, (0.95, 10.0))):
            with self.subTest(sweep=bad), self.assertRaises(ValueError):
                BuyTape([bad], NOW, START)

    def test_as_of_is_the_tape_as_it_was_read_then(self):
        tape = BuyTape(SWEEPS, NOW, START).as_of(T - 5 * DAY)
        self.assertEqual(fields(tape), (SWEEPS[:2], datetime.fromtimestamp(T - 5 * DAY,
                                                                           timezone.utc), START))

    def test_as_of_before_the_tape_begins_has_no_sweeps_and_no_exposure(self):
        tape = BuyTape(SWEEPS, NOW, START).as_of(START - DAY)
        self.assertEqual((tape.sweeps, tape.first_at, tape.read_at.timestamp()),
                         ((), START - DAY, START - DAY))


class TapeJsonTest(unittest.TestCase):
    def test_a_tape_reads_back_as_it_was_written(self):
        tape = tape_from_json(tape_to_json(BuyTape(SWEEPS, NOW, START), 500))
        self.assertEqual(fields(tape), (SWEEPS, NOW, START))

    def test_an_empty_tape_keeps_where_it_is_complete_from(self):
        tape = tape_from_json(tape_to_json(BuyTape((), NOW, START), 500))
        self.assertEqual(fields(tape), ((), NOW, START))

    def test_a_long_tape_keeps_its_newest_sweeps_complete_from_the_oldest_kept(self):
        tape = tape_from_json(tape_to_json(BuyTape(SWEEPS, NOW, START), 2))
        self.assertEqual(fields(tape), (SWEEPS[1:], NOW, SWEEPS[1].at))

    def test_junk_reads_as_no_tape(self):
        good = tape_to_json(BuyTape(SWEEPS, NOW, START), 500)
        newest, oldest = good["sweeps"][-1], good["sweeps"][0]
        for data in (
            None, "junk", [], {}, {**good, "v": EXIT_JSON_VERSION + 1}, {**good, "v": None},
            {**good, "read_at": "soon"}, {**good, "first_at": "then"},
            {**good, "first_at": newest[0] + 1}, {**good, "sweeps": "junk"},
            {**good, "sweeps": good["sweeps"][::-1]},
            {**good, "sweeps": [oldest, [newest[0], []]]},
            {**good, "sweeps": [oldest, [newest[0], [[1.2, 10.0]]]]},
            {**good, "sweeps": [oldest, [newest[0], [[0.97, -1.0]]]]},
            {**good, "sweeps": [oldest, [newest[0], [[0.97]]]]},
            {**good, "sweeps": [oldest, [None, [[0.97, 1.0]]]]},
        ):
            with self.subTest(data=data):
                self.assertIsNone(tape_from_json(data))


HURDLE = 0.40
DELTA = math.log1p(HURDLE)
TICK = 0.001


def flat(sold):
    return lambda r: sold


class ExitCurveTest(unittest.TestCase):
    def test_the_value_is_the_expected_discounted_proceeds(self):
        a, n, sold, p_true, years, sell_years = 0.97, 200.0, 500.0, 0.95, 0.5, 0.45
        lam = sold / n
        steps = 200_000
        dt = sell_years / steps
        sold_value = sum(a * math.exp(-DELTA * t) * lam * math.exp(-lam * t) * dt
                         for t in ((i + 0.5) * dt for i in range(steps)))
        unsold_value = math.exp(-lam * sell_years) * p_true * math.exp(-DELTA * years)
        v = ExitCurve(n, flat(sold), p_true, years, sell_years, DELTA, TICK).value_at(a)
        self.assertAlmostEqual(v.per_share, sold_value + unsold_value, places=7)

    def test_selling_until_resolution_is_the_closed_form_of_an_exponential_hold(self):
        a, n, sold, p_true, years = 0.97, 200.0, 500.0, 0.95, 0.5
        lam = sold / n
        k = lam + DELTA
        v = ExitCurve(n, flat(sold), p_true, years, years, DELTA, TICK).value_at(a)
        self.assertAlmostEqual(v.per_share, a * lam / k * (1 - math.exp(-k * years))
                               + p_true * math.exp(-k * years), places=12)

    def test_an_ask_nobody_takes_is_held_to_resolution(self):
        curve = ExitCurve(200.0, flat(0.0), 0.95, 0.5, 0.45, DELTA, TICK)
        hold = 0.95 * math.exp(-DELTA * 0.5)
        for a in (0.96, 0.99):
            with self.subTest(a=a):
                self.assertEqual(curve.value_at(a),
                                 ExitValue(a, 200.0, hold, hold, 0.0, 0.5 * YEAR_DAYS))

    def test_the_ask_is_sold_at_the_rate_of_the_yield_it_offers(self):
        # Buyers take a return of 10% a year or more: 0.96 a year out offers
        # ln(1 / 0.96) = 4.1%, 0.90 offers 10.5%.
        curve = ExitCurve(200.0, lambda r: 1000.0 if r >= 0.10 else 0.0, 0.95, 1.0, 1.0, DELTA,
                          TICK)
        self.assertEqual((curve.value_at(0.96).sold_per_year, curve.value_at(0.90).sold_per_year),
                         (0.0, 1000.0))

    def test_the_hold_is_the_inventory_over_the_rate_cut_short_at_resolution(self):
        curve = lambda sold: ExitCurve(200.0, flat(sold), 0.95, 0.5, 0.45, DELTA,
                                       TICK).value_at(0.97)
        self.assertAlmostEqual(curve(1000.0).hold_days, 0.2 * YEAR_DAYS)
        self.assertAlmostEqual(curve(100.0).hold_days, 0.5 * YEAR_DAYS)

    def test_the_gain_is_over_holding_the_share_to_resolution(self):
        v = ExitCurve(200.0, flat(500.0), 0.95, 0.5, 0.45, DELTA, TICK).value_at(0.97)
        self.assertAlmostEqual(v.gain, v.per_share - 0.95 * math.exp(-DELTA * 0.5), places=12)
        self.assertGreater(v.gain, 0)


H = 14.0
PARAMS = ExitParams(half_life_days=H, arrival_days=3.0, mark_buys=5.0)
END = T + 180 * DAY


def years_at(at, end=END):
    return (end - at) / DAY / YEAR_DAYS


def tape(*sweeps, span_days=60.0):
    return BuyTape(sweeps, NOW, T - span_days * DAY)


def prior(arrival_per_day=2.0, mark=lambda r, size: min(size, 40.0)):
    return ExitPrior(arrival_per_day, tuple(tuple(mark(r, size) for size in SIZE_GRID)
                                            for r in YIELD_GRID), 30, NOW)


def ask_yield(price, end=END):
    return math.log(1 / price) / years_at(T, end)


class TapeViewTest(unittest.TestCase):
    def test_a_piece_yields_what_its_buyer_accepted_to_the_end_date(self):
        old = sweep(10, (0.96, 40.0))
        for end in (END, T + 20 * DAY):
            with self.subTest(end=end):
                [r] = tape(old).view(end, H).yields
                self.assertAlmostEqual(r, math.log(1 / 0.96) / years_at(old.at, end))

    def test_a_sweep_weighs_half_as_much_each_half_life_back_from_the_read(self):
        view = tape(sweep(0, (0.96, 10.0)), sweep(H, (0.96, 10.0)),
                    sweep(2 * H, (0.96, 10.0))).view(END, H)
        self.assertAlmostEqual(view.W, 1.75)

    def test_the_exposure_is_the_weight_over_the_span_the_tape_is_complete_for(self):
        self.assertAlmostEqual(tape(span_days=H).view(END, H).D, H / (2 * math.log(2)))
        self.assertEqual(tape(span_days=0.0).view(END, H).D, 0.0)

    def test_a_sweep_fills_at_most_the_inventory(self):
        self.assertEqual(tape(sweep(0, (0.96, 5000.0))).view(END, H).cum(100.0), [0.0, 100.0])

    def test_the_pieces_of_one_sweep_add_up_to_the_inventory_once(self):
        view = tape(sweep(0, (0.97, 80.0), (0.96, 80.0))).view(END, H)
        self.assertEqual(view.cum(100.0), [0.0, 80.0, 100.0])

    def test_a_view_is_built_once_per_end_date_and_half_life(self):
        t = tape(sweep(0, (0.96, 10.0)))
        self.assertIs(t.view(END, H), t.view(END, H))
        self.assertIsNot(t.view(END, H), t.view(END, 2 * H))


class ExitPriorTest(unittest.TestCase):
    MARKS = prior(mark=lambda r, size: 10 * YIELD_GRID.index(r) + SIZE_GRID.index(size))

    def test_the_marks_are_read_bilinear_in_yield_and_log_size(self):
        r = (YIELD_GRID[1] + YIELD_GRID[2]) / 2
        size = math.sqrt(SIZE_GRID[0] * SIZE_GRID[1])
        self.assertAlmostEqual(self.MARKS.mark(r, size), 15.5)

    def test_no_shares_trade_at_no_yield_and_the_marks_are_flat_past_the_grid(self):
        self.assertEqual(self.MARKS.mark(0.0, 100.0), 0.0)
        self.assertAlmostEqual(self.MARKS.mark(YIELD_GRID[0] / 2, SIZE_GRID[1]), 0.5)
        self.assertEqual(self.MARKS.mark(50.0, 1e6), self.MARKS.marks[-1][-1])
        self.assertEqual(self.MARKS.mark(YIELD_GRID[3], 1.0), self.MARKS.marks[3][0])


class SalesRateTest(unittest.TestCase):
    def test_a_tape_with_no_sweeps_and_no_exposure_is_the_prior(self):
        empty = sales_rate(tape(span_days=0.0).view(END, H), prior(), PARAMS, 100.0)
        alone = sales_rate(None, prior(), PARAMS, 100.0)
        for r in (0.0, 0.03, 0.2, 4.0):
            with self.subTest(r=r):
                self.assertAlmostEqual(empty(r), alone(r))
                self.assertAlmostEqual(alone(r), YEAR_DAYS * 2.0 * prior().mark(r, 100.0))

    def test_a_quiet_tape_shrinks_the_arrival_rate_by_its_exposure(self):
        quiet = tape(span_days=60.0).view(END, H)
        rate = sales_rate(quiet, prior(), PARAMS, 100.0)
        self.assertAlmostEqual(rate(0.2), YEAR_DAYS * 2.0 * 3.0 / (quiet.D + 3.0) * 40.0)

    def test_a_sweep_too_cheap_to_fill_the_ask_never_counts(self):
        nothing = prior(mark=lambda r, size: 0.0)
        rate = sales_rate(tape(sweep(0, (0.95, 500.0))).view(END, H), nothing, PARAMS, 100.0)
        self.assertEqual(rate(ask_yield(0.96)), 0.0)
        self.assertGreater(rate(ask_yield(0.94)), 0.0)

    def test_the_same_buy_fills_a_near_ask_and_not_a_far_one(self):
        # 0.96 bought 30 days before the end yields more than 0.965 sold 20
        # days before it; bought 310 days before, less than 0.965 sold 300 before.
        nothing = prior(mark=lambda r, size: 0.0)
        old = tape(sweep(10, (0.96, 500.0)))
        for days, sells in ((20, True), (300, False)):
            end = T + days * DAY
            with self.subTest(days=days):
                rate = sales_rate(old.view(end, H), nothing, PARAMS, 100.0)
                self.assertEqual(rate(ask_yield(0.965, end)) > 0, sells)

    def test_the_tape_mix_is_shrunk_toward_the_prior_by_its_weight(self):
        view = tape(sweep(0, (0.96, 500.0)), span_days=0.0).view(END, H)
        rate = sales_rate(view, prior(), PARAMS, 100.0)
        r = ask_yield(0.95)
        mix = (100.0 + 5.0 * prior().mark(r, 100.0)) / (1.0 + 5.0)
        self.assertAlmostEqual(rate(r), YEAR_DAYS * (1.0 + 3.0 * 2.0) / (0.0 + 3.0) * mix)


# 1,328 prior-only markets, 30 bids each, two passes: 2.0-2.2 s measured on
# 2026-10-10, so three times that fails only a real slowdown.
SCAN_BUDGET_S = 6.0
POLICY = ExitPolicy(min_capture=0.005, hurdle_rate=HURDLE)
NOTHING_SELLS = prior(1.0, lambda r, size: 0.0)
# Ten buys a day, each of the whole inventory at any yield over 5%.
EVERYTHING_SELLS = prior(10.0, lambda r, size: size)


def model(tape=None, prior=NOTHING_SELLS, holding=Holding(), end_days=30.0, tick=TICK,
          params=PARAMS):
    end = NOW + timedelta(days=end_days)
    return ExitModel(tape, prior, params, POLICY, 0.95, end, end - timedelta(days=7), NOW,
                     tick, holding)


def hours_ago(hours, *pieces):
    return sweep(hours / 24, *pieces)


# Five buys in the last five hours, each taking 500 shares at 0.975.
RECENT = tuple(hours_ago(h, (0.975, 500.0)) for h in range(1, 6))


class ExitChoiceTest(unittest.TestCase):
    def test_a_curve_with_nothing_to_gain_rests_at_the_lowest_ask(self):
        choice = model(holding=Holding(1000.0, 0.95)).choose(0.96)
        self.assertEqual((choice.price, choice.floor, choice.source, choice.reason),
                         (0.96, choice.best, "prior", ""))

    def test_the_ask_rises_to_the_price_the_buys_reached(self):
        choice = model(tape(*RECENT), holding=Holding(1000.0, 0.95)).choose(0.96)
        self.assertEqual((choice.price, choice.lowest, choice.floor.price, choice.source),
                         (0.975, 0.96, 0.96, "tape"))
        self.assertGreater(choice.best.per_share, choice.floor.per_share)

    def test_old_buys_however_high_do_not_move_the_ask_once_their_weight_is_small(self):
        old = tuple(sweep(200 - d, (0.99, 5000.0)) for d in range(10))
        choice = model(tape(*old, *RECENT, span_days=210.0),
                       holding=Holding(1000.0, 0.95)).choose(0.96)
        self.assertEqual(choice.price, 0.975)

    def test_the_lowest_price_within_the_tie_slack_of_the_best_is_chosen(self):
        # Everything sells at once, so V(a) is about a: a tick of 1c is $0.01
        # a share, more than $0.25 over 1000 shares and less over 20.
        for shares, price in ((1000.0, 0.99), (20.0, 0.98)):
            with self.subTest(shares=shares):
                choice = model(prior=EVERYTHING_SELLS, holding=Holding(shares, 0.95),
                               tick=0.01).choose(0.96)
                self.assertEqual(choice.price, price)
                self.assertLessEqual(choice.best.per_share,
                                     choice.curve.value_at(0.99).per_share)
                self.assertGreaterEqual(choice.best.per_share,
                                        choice.curve.value_at(0.99).per_share
                                        - EXIT_TIE_USD / shares)

    def test_no_inventory_or_no_room_under_one_rests_no_ask(self):
        for holding, lowest, reason in ((Holding(), 0.96, "no inventory"),
                                        (Holding(100.0, 0.995), 1.0, "no ask under 1")):
            with self.subTest(reason=reason):
                choice = model(holding=holding).choose(lowest)
                self.assertEqual((choice.best, choice.price, choice.lowest, choice.reason),
                                 (None, None, lowest, reason))


class FillValueTest(unittest.TestCase):
    def best_ask(self, m, size, floor):
        values = [m.curve(size).value_at(round(floor + i * TICK, 3))
                  for i in range(round((0.999 - floor) / TICK) + 1)]
        return max(v.per_share for v in values)

    def test_with_nothing_held_a_fill_is_worth_its_best_ask_over_the_bid(self):
        m = model(tape(*RECENT))
        v = m.fill_value(300.0, 0.95)
        self.assertAlmostEqual(v.per_share, self.best_ask(m, 300.0, 0.955), places=12)
        self.assertEqual((v.price, v.size), (0.975, 300.0))

    def test_a_fill_on_top_of_inventory_is_worth_less_the_more_is_held(self):
        values = [model(prior=prior(1.0, lambda r, size: min(size, 40.0)),
                        holding=Holding(held, 0.95)).fill_value(100.0, 0.95).per_share
                  for held in (0.0, 500.0, 2000.0)]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertGreater(values[0] - values[-1], 0.001)

    def test_a_fill_is_floored_at_the_blended_cost_of_the_position_it_joins(self):
        # (1000 * 0.985 + 100 * 0.95) / 1100 + 0.005 is 0.98681..., so no
        # ask under 0.987, though the buys reached only 0.975.
        v = model(tape(*RECENT), holding=Holding(1000.0, 0.985)).fill_value(100.0, 0.95)
        self.assertEqual(v.price, 0.987)

    def test_a_fill_priced_too_high_to_ask_over_is_held_to_resolution(self):
        m = model(tape(*RECENT))
        v = m.fill_value(100.0, 0.996)
        self.assertEqual((v.price, v.sold_per_year), (None, 0.0))
        self.assertAlmostEqual(v.per_share, 0.95 * math.exp(-DELTA * 30 / YEAR_DAYS))


class ExitModelCostTest(unittest.TestCase):
    def test_bid_candidates_share_one_curve_per_inventory_bucket(self):
        sizes = [1000.0 / (0.94 + i * TICK) for i in range(30)]
        buckets = {round(math.log(s) / math.log(SIZE_BASE)) for s in sizes}
        m = model(tape(*RECENT))
        with mock.patch.object(exit_value, "ExitCurve", wraps=ExitCurve) as built:
            for _ in range(2):
                for i, size in enumerate(sizes):
                    m.fill_value(size, 0.94 + i * TICK)
        self.assertEqual(built.call_count, len(buckets))
        self.assertLessEqual(len(buckets), 2)

    def test_a_scan_of_prior_only_markets_values_its_bids_quickly(self):
        scan_prior = prior(0.5, lambda r, size: min(size, 30.0 * r))
        started = time.perf_counter()
        for _ in range(2):
            for i in range(1328):
                tick = 0.01 if i % 4 == 0 else 0.001
                mid = 0.90 + (i % 90) * 0.001
                m = model(prior=scan_prior, end_days=2 + i % 400, tick=tick)
                for j in range(30):
                    bid = round(mid - 0.005 - j * tick, 3)
                    m.fill_value((200 + i % 1800) / bid, bid)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, SCAN_BUDGET_S, f"{elapsed:.2f}s")


def market(*sweeps, span_days=H):
    return tape(*sweeps, span_days=span_days), NOW + timedelta(days=180)


def bought(n, *pieces):
    return market(*(sweep(0, *pieces),) * n)


class FitExitPriorTest(unittest.TestCase):
    def test_too_few_markets_with_any_exposure_fit_no_prior(self):
        markets = [bought(1, (0.96, 10.0)) for _ in range(19)] + [market(span_days=0.0)] * 5
        self.assertIsNone(fit_exit_prior(markets, PARAMS, NOW))

    def test_the_arrival_rate_is_the_median_market_s_so_one_busy_market_does_not_set_it(self):
        exposure = H / (2 * math.log(2))
        markets = [bought(n, (0.96, 10.0)) for n in range(21)]
        for busiest in (20, 1000):
            with self.subTest(busiest=busiest):
                fit = fit_exit_prior(markets[:-1] + [bought(busiest, (0.96, 10.0))], PARAMS, NOW)
                self.assertAlmostEqual(fit.arrival_per_day, 10 / exposure)
                self.assertEqual((fit.markets, fit.at), (21, NOW))

    def test_the_marks_are_the_mean_capped_shares_a_sweep_took_over_markets_that_saw_one(self):
        # 0.96 and 0.90 bought 180 days from the end yield 8.3% and 21.4%.
        markets = ([bought(1, (0.96, 50.0))] * 10 + [bought(2, (0.90, 500.0))] * 10
                   + [market()] * 5)
        marks = fit_exit_prior(markets, PARAMS, NOW).marks
        self.assertEqual(marks[0], (0.0,) * len(SIZE_GRID))
        self.assertEqual(marks[1], tuple(min(size, 50.0) / 2 for size in SIZE_GRID))
        self.assertEqual(marks[4], tuple((min(size, 50.0) + min(size, 500.0)) / 2
                                         for size in SIZE_GRID))


class ExitPriorJsonTest(unittest.TestCase):
    PRIOR = prior(2.5, lambda r, size: min(size, 40.0 * r))

    def test_a_prior_reads_back_as_it_was_written(self):
        for written in (self.PRIOR, DEFAULT_EXIT_PRIOR):
            with self.subTest(markets=written.markets):
                self.assertEqual(exit_prior_from_json(exit_prior_to_json(written)), written)

    def test_the_default_prior_marks_itself_as_no_scan_s(self):
        self.assertEqual((DEFAULT_EXIT_PRIOR.markets, DEFAULT_EXIT_PRIOR.at), (0, None))

    def test_junk_reads_as_no_prior(self):
        good = exit_prior_to_json(self.PRIOR)
        marks = good["marks"]
        for data in (
            None, "junk", [], {}, {**good, "v": EXIT_JSON_VERSION + 1},
            {**good, "yields": good["yields"][:-1]}, {**good, "sizes": good["sizes"][::-1]},
            {**good, "arrival_per_day": -1.0}, {**good, "arrival_per_day": "fast"},
            {**good, "marks": marks[:-1]}, {**good, "marks": [marks[0][:-1]] + marks[1:]},
            {**good, "marks": [[-1.0] * len(SIZE_GRID)] + marks[1:]},
            {**good, "marks": [[math.nan] * len(SIZE_GRID)] + marks[1:]},
            {**good, "markets": 2.5}, {**good, "markets": -1}, {**good, "at": "soon"},
        ):
            with self.subTest(data=data):
                self.assertIsNone(exit_prior_from_json(data))


if __name__ == "__main__":
    unittest.main()
