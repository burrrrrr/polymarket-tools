import math
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from analysis import exit_backtest
from analysis.exit_backtest import ARMS, Market, Mids, Replay, cases, fit, search, simulate
from farm import exit_value
from farm.exit_value import SIZE_GRID, YEAR_DAYS, BuyTape, ExitParams, Piece, Sweep

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

    def test_under_strictly_above_only_the_shares_a_buy_took_past_the_ask_fill_it(self):
        out = simulate(0.965, self.case(), 14.0, fill="strictly-above")
        disc = lambda days: math.exp(-DELTA * days / YEAR_DAYS)
        pv = (30 * 0.965 * disc(1) + 20 * 0.965 * disc(2) + 20 * 0.965 * disc(5)
              + 30 * 0.97 * disc(14)) / 100
        self.assertAlmostEqual(out.pv_per_share, pv, places=12)
        self.assertAlmostEqual(out.sold_fraction, 0.70)
        self.assertEqual(simulate(0.965, self.case(), 14.0, fill="at-or-above"),
                         simulate(0.965, self.case(), 14.0))


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


class CalibrateTest(unittest.TestCase):
    """Twenty identical far markets and five identical near ones, each day's
    buy walking every tick from the lowest ask to 0.999, so any ask prints
    exactly every day and never sells out: the metric is the model's
    predicted sold fraction over the realised, by cell."""

    @classmethod
    def setUpClass(cls):
        ladder = tuple((round(0.945 + 0.001 * k, 3), 0.1) for k in range(55))
        far = [buy(T - (40 - d - 0.5) * DAY, *ladder) for d in range(40)]
        near = [buy(T - (26 - d - 0.5) * DAY, *ladder) for d in range(26)]
        cls.markets = ([market(f"far{i:02}", far) for i in range(20)]
                       + [market(f"near{i}", near, end_days=40.0) for i in range(5)])
        cls.found = cases(cls.markets, 14.0)
        cls.picked = exit_backtest.picks(cls.markets, cls.found, PARAMS.half_life_days,
                                         [PARAMS], "model")[PARAMS]
        cls.priors = {}

    def expected(self, cell, fill):
        size, part = cell
        among = [i for i, c in enumerate(self.found)
                 if c.size == size and exit_backtest.PARTS[part](c)]
        predicted = [1 - math.exp(-self.picked[i].rate * 14 / YEAR_DAYS) for i in among]
        realised = [simulate(self.picked[i].price, self.found[i], 14.0, fill).sold_fraction
                    for i in among]
        return len(among), sum(predicted) / len(among), sum(realised) / len(among)

    def test_each_cell_holds_the_models_predicted_and_realised_sold_fraction(self):
        got = exit_backtest.calibrate(self.markets, self.found, PARAMS, 14.0, "at-or-above",
                                      reps=100)
        self.assertEqual(set(got["cells"]), {"100/under 60d", "100/60d+", "1000/under 60d",
                                             "1000/60d+"})
        ratios = []
        for cell in exit_backtest.CELLS:
            with self.subTest(cell=cell):
                n, predicted, realised = self.expected(cell, "at-or-above")
                r = got["cells"][f"{cell[0]:g}/{cell[1]}"]
                self.assertEqual((r["cases"], r["markets"]), (n, 5 if "under" in cell[1] else 20))
                self.assertAlmostEqual(r["predicted"], predicted)
                self.assertAlmostEqual(r["realised"], realised)
                self.assertAlmostEqual(r["ratio"], predicted / realised)
                low, high = r["ratio_interval"]
                self.assertAlmostEqual(low, r["ratio"])
                self.assertAlmostEqual(high, r["ratio"])
                ratios.append(r["ratio"])
        self.assertAlmostEqual(got["M"]["value"], max(abs(math.log(r)) for r in ratios))
        self.assertAlmostEqual(got["M"]["interval"][0], got["M"]["value"])
        self.assertAlmostEqual(got["M"]["interval"][1], got["M"]["value"])
        self.assertAlmostEqual(got["guards"]["model over anchor"]["mean"],
                               got["guards"]["model over floor"]["mean"]
                               - got["guards"]["anchor over floor"]["mean"])

    def forecast_at(self, i, price):
        """The model's predicted sold fraction at price for case i, through
        exit_value alone: the tape as of t, the prior fit on the other folds,
        once for each fold and t."""
        c, m = self.found[i], self.found[i].market
        if (c.fold, c.t) not in self.priors:
            fold = exit_backtest.folds(self.markets)
            self.priors[c.fold, c.t] = fit(
                [k for k in self.markets if fold[k.condition_id] != c.fold], PARAMS, c.t)
        prior = self.priors[c.fold, c.t]
        model = exit_value.ExitModel(m.tape.as_of(c.t), prior, PARAMS, exit_backtest.POLICY,
                                     c.mid, m.end_date, m.expires_at,
                                     datetime.fromtimestamp(c.t, timezone.utc), m.tick,
                                     exit_value.Holding(c.size, c.cost))
        v = model.curve(c.size).value_at(price)
        return 1 - math.exp(-v.sold_per_year / v.size * 14 / YEAR_DAYS)

    def test_each_arm_carries_the_models_forecast_at_its_own_ask(self):
        got = exit_backtest.calibrate(self.markets, self.found, PARAMS, 14.0, "at-or-above",
                                      reps=100)
        for cell in exit_backtest.CELLS:
            r = got["cells"][f"{cell[0]:g}/{cell[1]}"]
            among = [i for i, c in enumerate(self.found)
                     if c.size == cell[0] and exit_backtest.PARTS[cell[1]](c)]
            for arm in ("floor", "anchor"):
                with self.subTest(cell=cell, arm=arm):
                    price = {ARMS[arm](self.found[i], None, None).price for i in among}
                    self.assertEqual(len(price), 1)
                    [price] = price
                    predicted = sum(self.forecast_at(i, price) for i in among) / len(among)
                    a = r["arms"][arm]
                    self.assertAlmostEqual(a["predicted"], predicted)
                    self.assertAlmostEqual(a["ratio"], predicted / a["sold"])
                    self.assertAlmostEqual(a["ratio_interval"][0], a["ratio"])
                    self.assertAlmostEqual(a["ratio_interval"][1], a["ratio"])
            with self.subTest(cell=cell, arm="model"):
                self.assertEqual((r["arms"]["model"]["predicted"], r["arms"]["model"]["ratio"]),
                                 (r["predicted"], r["ratio"]))
            self.assertNotAlmostEqual(r["arms"]["anchor"]["predicted"], r["predicted"])
            self.assertNotAlmostEqual(r["arms"]["floor"]["predicted"],
                                      r["arms"]["anchor"]["predicted"])

    def test_the_strict_fill_rule_lowers_the_realised_sales_of_the_same_asks(self):
        strict = exit_backtest.calibrate(self.markets, self.found, PARAMS, 14.0,
                                         "strictly-above", reps=100)
        for cell in exit_backtest.CELLS:
            with self.subTest(cell=cell):
                r = strict["cells"][f"{cell[0]:g}/{cell[1]}"]
                n, predicted, realised = self.expected(cell, "strictly-above")
                self.assertAlmostEqual(r["realised"], realised)
                self.assertLess(realised, self.expected(cell, "at-or-above")[2])


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
