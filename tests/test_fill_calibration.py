import json
import math
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from types import SimpleNamespace
from unittest import mock

from analysis import fill_calibration as fc
from analysis import pricing_backtest as pb
from analysis.fill_calibration import Market, Model, OwnFill, Rested, Tally, Unit, Window
from farm import farm_books, farm_db, fill_cost
from farm.farm_db import Event, EventKind, FarmerStatus, FarmState, ScanRow
from farm.fill_cost import DISTANCES, History

T0 = 1_792_000_000.0
NOW = datetime.fromtimestamp(T0, timezone.utc)


def place(at, price, size, cid="a", order_id=None):
    return (at, cid, "PLACE", price, size, {"order_id": order_id} if order_id else {})


def fill(at, price, size, cid="a", order_id=None):
    return (at, cid, "FILL", price, size, {"order_id": order_id})


def censored_lognormal(n, mu, sigma, seed=1):
    """n fills of sellers drawn lognormal(mu, sigma) on bids drawn around
    300 shares: a seller wanting more than the bid takes all of it."""
    rng = random.Random(seed)
    out = []
    for k in range(n):
        wanted = math.exp(rng.gauss(mu, sigma))
        resting = math.exp(rng.gauss(math.log(300.0), 1.0))
        out.append(OwnFill(f"m{k % 50}", T0 + 3600.0 * k, min(wanted, resting), resting))
    return out


class OwnFillsTest(unittest.TestCase):
    def test_a_fill_takes_its_shares_from_the_bid_so_the_next_sees_what_is_left(self):
        fills = fc.own_fills([place(T0, 0.95, 300.0), fill(T0 + 60, 0.95, 100.0),
                              fill(T0 + 120, 0.95, 200.0)])
        self.assertEqual([(f.shares, f.resting, f.censored) for f in fills["a"]],
                         [(100.0, 300.0, False), (200.0, 200.0, True)])

    def test_a_fill_with_no_price_hit_the_last_place(self):
        fills = fc.own_fills([place(T0, 0.95, 300.0), place(T0 + 5, 0.94, 250.0),
                              fill(T0 + 60, None, 250.0)])
        self.assertEqual(fills["a"][0].resting, 250.0)

    def test_a_fill_at_a_price_no_place_rested_has_no_resting_bid(self):
        fills = fc.own_fills([place(T0, 0.95, 300.0), fill(T0 + 60, 0.93, 50.0)])
        self.assertIsNone(fills["a"][0].resting)
        self.assertFalse(fills["a"][0].censored)

    def test_a_fill_joins_the_place_with_its_order_id_whatever_came_after(self):
        fills = fc.own_fills([place(T0, 0.95, 300.0, order_id="x"),
                              place(T0 + 5, 0.95, 80.0, cid="b"),
                              place(T0 + 10, 0.95, 120.0, order_id="y"),
                              fill(T0 + 60, 0.95, 290.0, order_id="x")])
        self.assertEqual((fills["a"][0].resting, fills["a"][0].censored), (300.0, True))

    def test_markets_replay_apart(self):
        fills = fc.own_fills([place(T0, 0.95, 300.0), place(T0 + 1, 0.95, 40.0, cid="b"),
                              fill(T0 + 60, 0.95, 100.0)])
        self.assertEqual(fills["a"][0].resting, 300.0)
        self.assertNotIn("b", fills)

    def test_censored_at_ninety_five_percent_of_the_bid(self):
        self.assertTrue(OwnFill("a", T0, 95.0, 100.0).censored)
        self.assertTrue(OwnFill("a", T0, 100.0, 100.0).censored)
        self.assertFalse(OwnFill("a", T0, 94.0, 100.0).censored)

    def test_a_fill_larger_than_its_bid_is_not_censored_at_it(self):
        fills = fc.own_fills([place(T0, 0.95, 100.0), fill(T0 + 30, 0.95, 150.0)])
        self.assertEqual((fills["a"][0].resting, fills["a"][0].censored), (100.0, False))
        self.assertTrue(fills["a"][0].over_bid)
        self.assertFalse(OwnFill("a", T0, 100.0, 100.0).over_bid)


class UnitTest(unittest.TestCase):
    def test_an_hour_folds_the_fills_after_the_first_into_one_event(self):
        fills = [OwnFill("a", T0, 50.0, 300.0), OwnFill("b", T0 + 10, 5.0, 10.0),
                 OwnFill("a", T0 + 1800, 100.0, 250.0), OwnFill("a", T0 + 3700, 20.0, 150.0)]
        self.assertEqual(fc.in_unit(fills, Unit.FILL), fills)
        self.assertEqual(fc.in_unit(fills, Unit.HOUR),
                         [OwnFill("a", T0, 150.0, 300.0), OwnFill("b", T0 + 10, 5.0, 10.0),
                          OwnFill("a", T0 + 3700, 20.0, 150.0)])


class SizeFitTest(unittest.TestCase):
    def test_the_fit_recovers_a_censored_lognormal(self):
        fills = censored_lognormal(800, 4.9, 2.1)
        self.assertGreater(sum(f.censored for f in fills), 200)
        fit = fc.fit_sizes(fills)
        self.assertLess(abs(fit.mu - 4.9), 0.2)
        self.assertLess(abs(fit.sigma - 2.1), 0.2)
        self.assertEqual((fit.n, fit.unit), (800, Unit.FILL))

    def test_reading_censored_fills_as_exact_would_shrink_the_fit(self):
        fills = censored_lognormal(800, 4.9, 2.1)
        exact = [f._replace(resting=f.shares * 2) for f in fills]
        self.assertLess(fc.fit_sizes(exact).mu, fc.fit_sizes(fills).mu - 0.3)

    def test_too_few_fills_or_no_uncensored_one_cannot_be_fit(self):
        with self.assertRaises(ValueError):
            fc.fit_sizes(censored_lognormal(10, 4.9, 2.1))
        with self.assertRaises(ValueError):
            fc.fit_sizes([OwnFill("a", T0, 100.0, 100.0)] * 30)

    def test_fills_without_a_resting_bid_are_left_out(self):
        fills = censored_lognormal(100, 4.9, 2.1) + [OwnFill("a", T0, 5.0, None)] * 50
        self.assertEqual(fc.fit_sizes(fills).n, 100)

    def test_the_table_is_equal_probability_bin_means_with_the_distributions_mean(self):
        fit = fc.SizeFit(4.0, 1.5, 100, 10, Unit.FILL)
        table = fit.table()
        self.assertEqual(len(table), 40)
        self.assertEqual(list(table), sorted(table))
        mean = math.exp(4.0 + 1.5 ** 2 / 2)
        self.assertLess(abs(sum(table) / 40 / mean - 1), 0.01)
        q, phi = 300.0, NormalDist().cdf
        exact = (mean * phi((math.log(q) - 4.0 - 1.5 ** 2) / 1.5)
                 + q * (1 - phi((math.log(q) - 4.0) / 1.5)))
        self.assertLess(abs(fill_cost.sweep_fill(q, table) / exact - 1), 0.03)

    def test_kaplan_meier_on_a_hand_case(self):
        fills = [OwnFill("a", T0, 10.0, 100.0), OwnFill("a", T0, 20.0, 20.0),
                 OwnFill("a", T0, 30.0, 100.0), OwnFill("a", T0, 40.0, 100.0)]
        self.assertEqual(fc.kaplan_meier(fills), [(10.0, 0.75), (30.0, 0.375), (40.0, 0.0)])

    def test_the_fit_text_executes_back_to_the_table(self):
        fills = censored_lognormal(200, 4.9, 2.1)
        fit = fc.fit_sizes(fills)
        text = fc.fit_text(fit, fills, NOW)
        scope = {}
        exec(text.split("\n\n")[0], scope)
        self.assertEqual(scope["SWEEP_SIZES"], fit.table())
        self.assertIn("python -m analysis.fill_calibration fit --db data\\farm.db --until "
                      + NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), text)
        self.assertIn("Q 1,000", text)

    def test_the_shipped_table_is_a_non_decreasing_table_of_size_points(self):
        self.assertEqual(len(fill_cost.SWEEP_SIZES), fc.SIZE_POINTS)
        self.assertEqual(list(fill_cost.SWEEP_SIZES), sorted(fill_cost.SWEEP_SIZES))


FLOOR = 0.87
SPLIT = T0 + 10 * 86400.0
QUIET = History((2,) * len(DISTANCES), 30.0, NOW, FLOOR, 0, 0.0)
PRIOR = fill_cost.DEFAULT_PRIOR._replace(markets=40, at=NOW, floor=0.85)
PRODUCTION = Model("production", PRIOR, fill_cost.SWEEP_SIZES, Unit.FILL)


def rate_of(history=QUIET, distance=0.01, prior=PRIOR):
    return fill_cost.fill_curve(history, prior, FLOOR).per_day(distance)


def one_market(fills=(), days=20, shares=100.0, cid="a", history=QUIET, deep=False):
    """A market resting `shares` 1c under the mid, a day an interval, from T0."""
    rested = tuple(Rested(T0 + 86400.0 * d, 1.0, 0.01, shares, history, deep)
                   for d in range(days))
    return Market(cid, cid, rested, tuple(fills), 0)


class ScoringTest(unittest.TestCase):
    def test_a_market_is_predicted_its_rate_times_days_and_one_sweeps_fill(self):
        tally = fc.predicted(one_market(days=1), PRODUCTION, Window.ALL, SPLIT, FLOOR)
        self.assertAlmostEqual(tally.events, rate_of())
        self.assertAlmostEqual(tally.shares, rate_of() * fill_cost.sweep_fill(100.0))
        whole = fc.predicted(one_market(days=1), PRODUCTION._replace(sizes=fill_cost.WHOLE_BID),
                             Window.ALL, SPLIT, FLOOR)
        self.assertAlmostEqual(whole.shares, rate_of() * 100.0)

    def test_a_bid_behind_more_than_the_free_depth_is_predicted_no_fills(self):
        fills = [OwnFill("a", T0 + 3600.0, 50.0, 100.0)]
        deep = fc.predicted(one_market(fills, deep=True), PRODUCTION, Window.ALL, SPLIT, FLOOR)
        self.assertEqual(deep, Tally(0.0, 0.0))
        self.assertEqual(fc.realized(one_market(fills, deep=True), Window.ALL, SPLIT, Unit.FILL),
                         Tally(1, 50.0))

    def test_a_stretch_is_deep_by_the_others_bids_above_ours(self):
        def at(k):
            return datetime.fromtimestamp(T0 + 300.0 * k, timezone.utc)

        ours = (("FAV_BID", 0.95, 300.0),)
        samples = [farm_books.sample("a", at(0), 0.96, ((0.955, 20_001.0), (0.95, 300.0)),
                                     ((0.965, 400.0),), ours),
                   farm_books.sample("a", at(1), 0.96, ((0.955, 20_000.0), (0.95, 50_000.0)),
                                     ((0.965, 400.0),), ours)]
        market = SimpleNamespace(start=math.inf, fm=SimpleNamespace(tick=0.001))
        self.assertEqual([r[-1] for r in fc.rested_of(samples, market, FLOOR)], [True, False])

    def test_the_windows_split_the_rested_days_and_the_fills(self):
        fills = [OwnFill("a", T0 + 86400.0 * d, 50.0, 100.0) for d in (1, 5, 12)]
        m = one_market(fills)
        predicted = {w: fc.predicted(m, PRODUCTION, w, SPLIT, FLOOR) for w in Window}
        realized = {w: fc.realized(m, w, SPLIT, Unit.FILL) for w in Window}
        for got in (predicted, realized):
            self.assertAlmostEqual(got[Window.ALL].events,
                                   got[Window.TRAIN].events + got[Window.TEST].events)
        self.assertAlmostEqual(predicted[Window.TEST].events, 10 * rate_of())
        self.assertEqual(realized[Window.TEST], Tally(1, 50.0))

    def test_realized_events_count_in_the_unit_and_shares_do_not(self):
        fills = [OwnFill("a", T0 + 60.0 * k, 10.0, 100.0) for k in range(3)]
        m = one_market(fills)
        self.assertEqual(fc.realized(m, Window.ALL, SPLIT, Unit.FILL), Tally(3, 30.0))
        self.assertEqual(fc.realized(m, Window.ALL, SPLIT, Unit.HOUR), Tally(1, 30.0))

    def test_a_score_is_realized_over_predicted_with_a_seeded_interval(self):
        markets = [one_market([OwnFill(f"m{i}", T0, 60.0, 100.0)] * (i % 3), cid=f"m{i}")
                   for i in range(30)]
        first = fc.score(markets, PRODUCTION, Window.ALL, SPLIT, FLOOR, reps=200)
        self.assertEqual(first, fc.score(markets, PRODUCTION, Window.ALL, SPLIT, FLOOR, reps=200))
        realized = sum(i % 3 for i in range(30))
        self.assertAlmostEqual(first.events.value, realized / first.predicted.events)
        low, high = first.events.interval
        self.assertLess(low, first.events.value)
        self.assertGreater(high, first.events.value)

    def test_markets_bucket_by_their_predicted_events(self):
        quiet = one_market(days=1, cid="q")
        loud = one_market(days=20, history=QUIET._replace(events=(40,) * len(DISTANCES)),
                          fills=[OwnFill("l", T0, 5.0, 100.0)], cid="l")
        rows = fc.by_bucket([quiet, loud], PRODUCTION, Window.ALL, SPLIT, FLOOR)
        self.assertEqual([(r["bucket"], r["markets"], r["realized"]) for r in rows],
                         [("<0.1", 1, 0), ("2+", 1, 1)])

    def test_a_fill_counts_toward_the_source_of_the_interval_it_landed_in(self):
        rested = (Rested(T0, 1.0, 0.01, 100.0, None, False),
                  Rested(T0 + 86400.0, 1.0, 0.01, 100.0, QUIET, False))
        m = Market("a", "a", rested, (OwnFill("a", T0 + 3600.0, 5.0, 100.0),), 0)
        rows = {r["source"]: r for r in fc.by_source([m], PRODUCTION, Window.ALL, SPLIT, FLOOR)}
        self.assertEqual(rows["prior"]["realized_events"], 1)
        self.assertEqual(rows["history, weight 0.5 or more"]["realized_events"], 0)
        self.assertAlmostEqual(rows["prior"]["predicted_events"], rate_of(None))


def ratio(value, low, high):
    return fc.Ratio(value, (low, high))


def scored(events, shares, window=Window.ALL):
    return fc.Score("candidate", window, Tally(1, 1), Tally(1, 1), events, shares)


class GateTest(unittest.TestCase):
    CALIBRATED = scored(ratio(1.0, 0.8, 1.2), ratio(0.9, 0.7, 1.2))

    def test_calibrated_over_all_and_from_the_split_passes(self):
        verdict = fc.gate(self.CALIBRATED, self.CALIBRATED._replace(window=Window.TEST), True)
        self.assertEqual(verdict, fc.Verdict(True, ()))

    def test_a_test_window_interval_outside_the_band_fails(self):
        for events in (ratio(0.5, 0.42, 0.6), ratio(2.0, 1.7, 2.4)):
            with self.subTest(events=events):
                verdict = fc.gate(self.CALIBRATED, scored(events, events, Window.TEST), True)
                self.assertFalse(verdict.passes)
                self.assertEqual(len(verdict.reasons), 2)

    def test_a_test_window_ratio_outside_the_band_fails_though_its_interval_reaches_it(self):
        test = scored(ratio(1.0, 0.76, 1.26), ratio(0.57, 0.39, 0.80), Window.TEST)
        verdict = fc.gate(self.CALIBRATED, test, True)
        self.assertEqual(verdict.reasons, ("test shares realized/predicted 0.57 [0.39, 0.80] "
                                           "is outside the band [0.67, 1.50]",))

    def test_an_all_window_interval_without_1_fails(self):
        biased = scored(ratio(1.5, 1.2, 1.9), ratio(1.0, 0.8, 1.2))
        verdict = fc.gate(biased, self.CALIBRATED._replace(window=Window.TEST), True)
        self.assertEqual(verdict.reasons, ("all events realized/predicted 1.50 [1.20, 1.90] "
                                           "excludes 1",))

    def test_sizes_fit_in_another_unit_than_the_rates_fail(self):
        verdict = fc.gate(self.CALIBRATED, self.CALIBRATED._replace(window=Window.TEST), False)
        self.assertFalse(verdict.passes)


class UnitCheckTest(unittest.TestCase):
    """Fills come three to an hour: the rate predicts either the fills or the
    hours, and the check says which."""

    def markets(self, per_day):
        history = QUIET._replace(events=(0,) * len(DISTANCES), days=1e-6)
        prior = PRIOR._replace(rates=(per_day,) * len(DISTANCES),
                               strengths=(1e12,) * len(DISTANCES))
        out = []
        for i in range(20):
            fills = [OwnFill(f"m{i}", T0 + 86400.0 * d + 600.0 * k, 50.0, 100.0)
                     for d in range(0, 20, 2) for k in range(3)]
            out.append(one_market(fills, cid=f"m{i}", history=history))
        return out, prior, [f for m in out for f in m.fills]

    def test_the_unit_the_rate_predicts_is_the_one_checked(self):
        for per_day, unit in ((1.5, Unit.FILL), (0.5, Unit.HOUR)):
            with self.subTest(unit=unit):
                markets, prior, fills = self.markets(per_day)
                check = fc.unit_check(markets, prior, fills, SPLIT, FLOOR, reps=100)
                self.assertEqual(check["calibrated"], unit.value)
                self.assertEqual(check["passes"], unit is fc.SWEEP_UNIT)


def write_fixture(root):
    """farm.db, farm_books.db and a cache of two markets the farmer rested a
    favorite bid on for two days from T0, one filled twice: (db, books, cache)."""
    db_path, books_path = os.path.join(root, "farm.db"), os.path.join(root, "farm_books.db")
    cache = os.path.join(root, "cache")
    os.makedirs(cache)
    end = NOW + timedelta(days=60)
    history = History((3, 3, 3, 2, 2, 2, 1, 1, 1, 0, 0, 0), 30.0, NOW, FLOOR, 0, 0.0)
    rows, records, samples, states = [], [], [], []
    for cid in ("a", "b"):
        market = {"fav_token_id": f"fav-{cid}", "slug": f"market-{cid}",
                  "end_date": end.isoformat(), "fav_outcome": "No", "rate_per_day": 20.0,
                  "tick": 0.001, "v": 0.035, "min_size": 50.0}
        rows.append(ScanRow(cid, f"market-{cid}", "?", end.isoformat(), 1.0,
                            json.dumps({"market": market})))
        records.append({"condition_id": cid, "start": T0 - 20 * 86400.0,
                        "sells": [[T0 - 86400.0 * k, [[0.94, 100.0]]] for k in range(1, 15)],
                        "buys": [],
                        "mids": [[T0 - 21 * 86400.0, 0.96], [T0 + 3 * 86400.0, 0.96]]})
        for k in range(2 * 288):
            at = datetime.fromtimestamp(T0 + 300.0 * k, timezone.utc)
            samples.append(farm_books.sample(cid, at, 0.96, ((0.955, 400.0), (0.95, 2000.0)),
                                             ((0.965, 400.0), (0.97, 2000.0)),
                                             (("FAV_BID", 0.95, 300.0),)))
        states.append(FarmState(cid, NOW, "QUOTING", "", None, 0.96, 0.96, 0.0, 0.0, 500.0, 0.0,
                                None, {"FAV_BID": {"history": fill_cost.history_to_json(history)}},
                                {}, None, {}, None))
    conn = farm_db.open_db(db_path)
    farm_db.store_scan(conn, rows, ["a", "b"], NOW, prior=fill_cost.prior_to_json(PRIOR))
    for cid in ("a", "b"):
        farm_db.approve_market(conn, cid, 0.99, 25.0, now=NOW)

    def at(seconds):
        return datetime.fromtimestamp(T0 + seconds, timezone.utc)

    events = [
        Event(at(0), EventKind.PLACE, "", "a", "FAV_BID", 0.95, 300.0, None, {"order_id": "x"}),
        Event(at(0), EventKind.PLACE, "", "b", "FAV_BID", 0.95, 300.0),
        Event(at(3600), EventKind.FILL, "", "a", "FAV_BID", 0.95, 120.0, None, {"order_id": "x"}),
        Event(at(100000), EventKind.FILL, "", "a", "FAV_BID", 0.95, 180.0, None,
              {"order_id": "x"}),
    ]
    cfg = pb.config_from(pb.build_parser().parse_args([]))._replace(min_fav=FLOOR)
    farm_db.publish(conn, FarmerStatus("dry", 1, NOW, NOW, 5.0, False, 2, cfg._asdict()),
                    states, events)
    conn.close()
    books = farm_books.open_books(books_path)
    farm_books.record(books, samples)
    books.close()
    with open(os.path.join(cache, pb.CACHE_FILE), "w") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
    return db_path, books_path, cache


class ReportTest(unittest.TestCase):
    def run_report(self, *extra):
        """(the JSON result, the text) of the report on write_fixture."""
        with tempfile.TemporaryDirectory() as root:
            db, books, cache = write_fixture(root)
            before = (os.path.getmtime(db), os.path.getmtime(books))
            text, out = os.path.join(root, "report.txt"), os.path.join(root, "report.json")
            split = datetime.fromtimestamp(T0 + 86400.0, timezone.utc).date().isoformat()
            with mock.patch("builtins.print"):
                fc.main(["report", "--db", db, "--books", books, "--cache", cache,
                         "--split", split, "--reps", "50", "--report", text, "--json", out,
                         *extra])
            self.assertEqual((os.path.getmtime(db), os.path.getmtime(books)), before)
            with open(out) as f:
                result = json.load(f)
            with open(text) as f:
                return result, f.read()

    def test_the_report_scores_the_rested_bids_and_writes_text_and_json(self):
        result, rendered = self.run_report()
        self.assertEqual((result["data"]["markets"], result["data"]["covered_fills"]), (2, 2))
        self.assertEqual(result["data"]["uncovered_fills"], 0)
        self.assertEqual((result["data"]["over_bid"], result["data"]["deep_fills"]), (0, 0))
        production = next(s for s in result["scores"]
                          if s["model"] == "production" and s["window"] == "all")
        self.assertEqual(production["realized"], [2, 300.0])
        self.assertGreater(production["predicted"][0], 0)
        self.assertEqual([row["tail_days"] for row in result["tail_effect"]["rows"]
                          if row["hurdle_rate"] == 1.0], [0, 10, 30, 60])
        for heading in ("Data", "Size fit", "Scores", "By predicted events", "By fill source",
                        "Unit check", "Tail share", "Top markets", "Tail cap on current markets",
                        "Gate"):
            self.assertIn(heading, rendered)

    def test_until_leaves_out_the_fills_and_bids_after_it(self):
        until = datetime.fromtimestamp(T0 + 50000.0, timezone.utc).isoformat()
        result, rendered = self.run_report("--until", until)
        self.assertEqual((result["data"]["fills"], result["data"]["covered_fills"]), (1, 1))
        self.assertLessEqual(fc._until(result["data"]["last"]), T0 + 50000.0 + 300.0)
        self.assertIn(f"data before {result['data']['until']}", rendered)


if __name__ == "__main__":
    unittest.main()
