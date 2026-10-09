import math
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import exit_backtest
import exit_value
from exit_backtest import ARMS, Market, Mids, Replay, cases, fit, search, simulate
from exit_value import SIZE_GRID, YEAR_DAYS, BuyTape, ExitParams, Piece, Sweep

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
T = NOW.timestamp()
DAY = 86400.0
START = T - 150 * DAY
DELTA = math.log1p(exit_backtest.POLICY.hurdle_rate)
PARAMS = ExitParams(half_life_days=14.0, arrival_days=3.0, mark_buys=5.0)


def buy(at, *pieces):
    return Sweep(at, tuple(Piece(p, s) for p, s in pieces))


def market(cid, sweeps, mids=((START - 3600, 0.95),), end_days=400.0):
    return Market(cid, NOW + timedelta(days=end_days), 0.001, BuyTape(sweeps, NOW, START),
                  Mids(mids))


def daily(price_on_day, shares=10.0, days=150):
    return [buy(START + (d + 0.5) * DAY, (price_on_day(d), shares)) for d in range(days)]


class SimulateTest(unittest.TestCase):
    t = math.floor((START + 100 * DAY) / (2 * DAY)) * 2 * DAY
    sweeps = [buy(START + DAY / 2, (0.99, 5.0)), buy(t - DAY, (0.99, 500.0)),
              buy(t + DAY, (0.97, 30.0), (0.96, 50.0)),
              buy(t + 2 * DAY, (0.99, 20.0), (0.965, 40.0)),
              buy(t + 5 * DAY, (0.99, 20.0)),
              buy(t + 20 * DAY, (0.99, 100.0))]
    mids = ((START - 3600, 0.95), (t + 10 * DAY, 0.97), (t + 15 * DAY, 0.5))

    def case(self, size=100.0):
        [case] = [c for c in cases([market("m", self.sweeps, self.mids)], 14.0, sizes=(size,))
                  if c.t == self.t]
        return case

    def test_an_ask_sells_the_shares_later_buys_took_at_or_over_it_then_marks_the_rest(self):
        out = simulate(0.965, self.case(), 14.0)
        disc = lambda days: math.exp(-DELTA * days / YEAR_DAYS)
        pv = (30 * 0.965 * disc(1) + 60 * 0.965 * disc(2) + 10 * 0.965 * disc(5)) / 100
        self.assertAlmostEqual(out.pv_per_share, pv, places=12)
        self.assertEqual((out.price, out.sold_fraction), (0.965, 1.0))

    def test_an_ask_over_every_later_buy_is_marked_at_the_mid_the_horizon_ends_at(self):
        out = simulate(0.995, self.case(), 14.0)
        self.assertAlmostEqual(out.pv_per_share, 0.97 * math.exp(-DELTA * 14 / YEAR_DAYS),
                               places=12)
        self.assertEqual(out.sold_fraction, 0.0)

    def test_a_buy_fills_at_most_what_is_left(self):
        out = simulate(0.96, self.case(1000.0), 14.0)
        self.assertAlmostEqual(out.sold_fraction, (80 + 60 + 20) / 1000)


class CasesTest(unittest.TestCase):
    def test_cases_run_every_two_days_after_a_week_of_buys_until_the_horizon_ends_the_tape(self):
        mids = ((START - 3600, 0.95), (START + 60 * DAY, 0.85), (START + 80 * DAY, 0.95))
        found = cases([market("m", daily(lambda d: 0.97), mids)], 14.0, sizes=(100.0,))
        every = 2 * DAY
        grid = (k * every for k in range(math.ceil(START / every), math.floor(T / every) + 1))
        self.assertEqual([c.t for c in found],
                         [t for t in grid if START + 7.5 * DAY <= t <= T - 14 * DAY
                          and not START + 60 * DAY <= t < START + 80 * DAY])
        self.assertEqual({(c.mid, c.cost, c.lowest) for c in found}, {(0.95, 0.94, 0.945)})


class SlicesTest(unittest.TestCase):
    def test_cases_are_reported_by_size_and_time_to_resolution_and_over_all_of_them(self):
        found = cases([market("near", daily(lambda d: 0.97), end_days=40.0),
                       market("far", daily(lambda d: 0.97))], 14.0)
        near = lambda c: c.market.end_date.timestamp() - c.t < 60 * DAY
        every = range(len(found))
        got = exit_backtest.slices(found, every)
        self.assertEqual(got[exit_backtest.ALL_SIZES, "all"], list(every))
        self.assertEqual(got[100.0, "under 60d"],
                         [i for i, c in enumerate(found) if c.size == 100.0 and near(c)])
        self.assertEqual(got[exit_backtest.ALL_SIZES, "60d+"],
                         [i for i, c in enumerate(found) if not near(c)])
        self.assertTrue(got[100.0, "under 60d"])
        self.assertEqual({found[i].market.condition_id for i in got[1000.0, "60d+"]},
                         {"near", "far"})


def world(price_on_day, n=25):
    return [market(f"m{i:02}", daily(price_on_day)) for i in range(n)]


class SearchTest(unittest.TestCase):
    # No prior: what the half-life alone makes of the buys.
    GRID = {"half_life_days": (3.0, 60.0), "arrival_days": (0.0,), "mark_buys": (0.0,)}

    def chosen(self, markets):
        params, surface = search(markets, cases(markets, 14.0, sizes=(1000.0,)), self.GRID, 14.0)
        return params.half_life_days, surface

    def test_buyers_who_change_their_price_monthly_are_tracked_by_a_short_half_life(self):
        h, surface = self.chosen(world(lambda d: 0.99 if (d // 30) % 2 == 0 else 0.955))
        self.assertEqual(h, 3.0)
        short, long = (surface[ExitParams(h, 0.0, 0.0)] for h in (3.0, 60.0))
        self.assertGreater(short.mean - long.mean, short.se)

    def test_buyers_who_keep_their_price_leave_the_longest_half_life(self):
        h, _ = self.chosen(world(lambda d: 0.99))
        self.assertEqual(h, 60.0)


class FitTest(unittest.TestCase):
    def test_the_prior_is_the_rate_and_shares_of_the_buys_before_the_fit(self):
        at = T - 10 * DAY
        before = [buy(at - (k + 0.5) * DAY / 2, (0.96, 50.0)) for k in range(280)]
        after = [buy(at + (k + 0.5) * DAY / 10, (0.99, 500.0)) for k in range(90)]
        prior = fit([market(f"m{i:02}", before + after) for i in range(25)], PARAMS, at)
        self.assertAlmostEqual(prior.arrival_per_day, 2.0, delta=0.05)
        for row in prior.marks:
            for mark, size in zip(row, SIZE_GRID):
                self.assertAlmostEqual(mark, min(size, 50.0))


class NoLookAheadTest(unittest.TestCase):
    def test_every_arm_values_only_the_buys_before_the_case(self):
        markets = world(lambda d: 0.97 + 0.002 * (d % 10))
        case = next(c for c in cases(markets, 14.0, sizes=(300.0,)) if c.t > START + 80 * DAY)
        viewed = []
        init = exit_value.TapeView.__init__

        def record(view, tape, *args):
            viewed.append(tape)
            init(view, tape, *args)

        with mock.patch.object(exit_value.TapeView, "__init__", record):
            replay = Replay(markets, PARAMS.half_life_days)
            for name, arm in ARMS.items():
                with self.subTest(arm=name):
                    self.assertIsNotNone(arm(case, replay, PARAMS).price)
        self.assertTrue(viewed)
        self.assertEqual({t.read_at.timestamp() for t in viewed}, {case.t})
        self.assertLessEqual(max(s.at for t in viewed for s in t.sweeps), case.t)


if __name__ == "__main__":
    unittest.main()
