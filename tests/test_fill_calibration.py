import json
import math
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from analysis import fill_calibration as fc
from analysis import pricing_backtest as pb
from analysis.fill_calibration import Market, Model, OwnFill, Rested, Tally, Window
from farm import farm_books, farm_db, fill_cost
from farm.farm_db import Event, EventKind, FarmerStatus, FarmState, ScanRow
from farm.fill_cost import DISTANCES, History

T0 = 1_792_000_000.0
NOW = datetime.fromtimestamp(T0, timezone.utc)


def place(at, price, size, cid="a", order_id=None):
    return (at, cid, "PLACE", price, size, {"order_id": order_id} if order_id else {})


def fill(at, price, size, cid="a", order_id=None):
    return (at, cid, "FILL", price, size, {"order_id": order_id})


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

    def test_a_fill_of_the_whole_bid_or_more_is_a_whole_bid_fill(self):
        self.assertEqual([OwnFill("a", T0, z, 100.0).whole for z in (94.0, 95.0, 150.0)],
                         [False, True, True])
        self.assertFalse(OwnFill("a", T0, 150.0, None).whole)




FLOOR = 0.87
SPLIT = T0 + 10 * 86400.0
QUIET = History((2,) * len(DISTANCES), 30.0, NOW, FLOOR, 0, 0.0)
PRIOR = fill_cost.DEFAULT_PRIOR._replace(markets=40, at=NOW, floor=0.85)
# One seller a sweep, nothing ahead holding.
ONE = fill_cost.Sweeps(4.5, 1.7)
PRODUCTION = Model("production", PRIOR, ONE)
# A history of no events over a moment, so a prior this strong sets the rate.
UNREAD = QUIET._replace(events=(0,) * len(DISTANCES), days=1e-6)


def flat_prior(per_day):
    return PRIOR._replace(rates=(per_day,) * len(DISTANCES), strengths=(1e12,) * len(DISTANCES))


def swept_markets(sweeps, markets=40, days=40, per_day=0.6, seed=2):
    """Markets resting a bid a day at a time behind shares ahead drawn from
    nothing to 30,000, sellers arriving per_day times sweeps.sellers and each
    filling min(Q, max(0, X - hold * A)) of a bid of Q, X drawn from sweeps
    and A the shares ahead up to DEPTH_CREDIT_MAX."""
    rng = random.Random(seed)
    out = []
    for m in range(markets):
        rested, fills = [], []
        for d in range(days):
            at = T0 + 86400.0 * (m * days + d)
            ahead = 0.0 if rng.random() < 0.4 else fc._rounded(math.exp(rng.uniform(5.0, 10.3)))
            shares = round(math.exp(rng.gauss(math.log(400.0), 0.8)))
            rested.append(Rested(at, 1.0, 0.01, shares, UNREAD, ahead))
            for k in range(sum(rng.random() < per_day * sweeps.sellers / 20
                               for _ in range(20))):
                wanted = (math.exp(rng.gauss(sweeps.mu, sweeps.sigma))
                          - sweeps.hold * min(ahead, fill_cost.DEPTH_CREDIT_MAX))
                if wanted > 0:
                    fills.append(OwnFill(f"m{m}", at + 600.0 * (k + 1), min(shares, wanted),
                                         float(shares)))
        out.append(Market(f"m{m}", f"m{m}", tuple(rested), tuple(fills), 0))
    return out


def terms_of(markets, per_day=0.6, window=Window.ALL):
    return [fc.fit_terms(m, flat_prior(per_day), FLOOR, window, SPLIT) for m in markets]


class SweepFitTest(unittest.TestCase):
    TRUE = fill_cost.Sweeps(4.5, 1.7, hold=0.3, sellers=2.5)

    def test_the_fit_recovers_the_sweeps_the_hold_and_the_sellers(self):
        markets = swept_markets(self.TRUE)
        fills = [f for m in markets for f in m.fills]
        self.assertGreater(len(fills), 300)
        self.assertGreater(sum(f.censored for f in fills), 60)
        fit = fc.fit_sweeps(terms_of(markets))
        self.assertLess(abs(fit.hold - 0.3), 0.08)
        self.assertLess(abs(fit.mu - 4.5), 0.25)
        self.assertLess(abs(fit.sigma - 1.7), 0.25)
        self.assertLess(abs(fit.sellers / 2.5 - 1), 0.15)
        self.assertEqual(fit.n, len(fills))

    def test_a_rate_off_by_a_constant_moves_only_the_sellers(self):
        markets = swept_markets(self.TRUE, markets=10)
        fit = fc.fit_sweeps(terms_of(markets, per_day=0.6))
        tenfold = fc.fit_sweeps(terms_of(markets, per_day=6.0))
        self.assertEqual(tenfold._replace(sellers=fit.sellers), fit)
        self.assertAlmostEqual(tenfold.sellers, fit.sellers / 10)

    def test_shares_ahead_past_the_depth_credit_tell_the_fit_nothing_more(self):
        markets = swept_markets(self.TRUE, markets=10)

        def deeper(r):
            return r._replace(ahead=10 * r.ahead) if r.ahead >= fill_cost.DEPTH_CREDIT_MAX else r

        moved = [m._replace(rested=tuple(deeper(r) for r in m.rested)) for m in markets]
        self.assertNotEqual(moved, markets)
        self.assertEqual(fc.fit_sweeps(terms_of(moved)), fc.fit_sweeps(terms_of(markets)))

    def test_fills_with_no_sweeps_expected_cannot_be_fit(self):
        terms = fc.FitTerms({0.0: 0.0}, ((40.0, 0.0),) * fc.MIN_FIT_FILLS, (), ())
        with self.assertRaises(ValueError):
            fc.fit_sweeps([terms])

    def test_with_nothing_ahead_holding_the_fit_finds_no_hold(self):
        fit = fc.fit_sweeps(terms_of(swept_markets(self.TRUE._replace(hold=0.0))))
        self.assertLess(fit.hold, 0.05)

    def test_a_fill_takes_the_shares_ahead_of_the_stretch_it_landed_in(self):
        rested = (Rested(T0, 1.0, 0.01, 100.0, UNREAD, 0.0),
                  Rested(T0 + 86400.0, 1.0, 0.01, 100.0, UNREAD, 5000.0))
        fills = (OwnFill("a", T0 + 3600.0, 40.0, 100.0), OwnFill("a", T0 + 90000.0, 100.0, 100.0),
                 OwnFill("a", T0 + 90100.0, 10.0, None))
        terms = fc.fit_terms(Market("a", "a", rested, fills, 0), flat_prior(0.5), FLOOR,
                             Window.ALL, SPLIT)
        self.assertEqual(terms, fc.FitTerms({0.0: 0.5, 5000.0: 0.5}, ((40.0, 0.0),),
                                            ((100.0, 5000.0),), (5000.0,)))

    def test_too_few_fills_or_no_partial_one_cannot_be_fit(self):
        few = swept_markets(self.TRUE, markets=1, days=10)
        with self.assertRaises(ValueError):
            fc.fit_sweeps(terms_of(few))
        whole = [m._replace(fills=tuple(f._replace(shares=f.resting) for f in m.fills))
                 for m in swept_markets(self.TRUE, markets=5)]
        with self.assertRaises(ValueError):
            fc.fit_sweeps(terms_of(whole))

    def test_the_fit_text_executes_back_to_the_fit_and_the_prior_it_was_fit_against(self):
        markets = swept_markets(self.TRUE, markets=10)
        prior = flat_prior(0.6)._replace(at=NOW + timedelta(seconds=1.5))
        fit = fc.fit_sweeps([fc.fit_terms(m, prior, FLOOR, Window.ALL, SPLIT) for m in markets])
        text = fc.fit_text(fit, markets, NOW, prior)
        scope = {"Sweeps": fill_cost.Sweeps, "datetime": datetime}
        exec(text.split("\n\n")[0], scope)
        self.assertEqual(scope["SWEEPS"], fill_cost.Sweeps(
            round(fit.mu, 4), round(fit.sigma, 4), round(fit.hold, 4), round(fit.sellers, 4)))
        self.assertEqual(scope["SWEEPS_PRIOR_AT"], prior.at)
        self.assertIn("--until " + NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), text)
        self.assertIn(f"prior fit {prior.at.isoformat()}", text)
        self.assertIn("Q 1,000", text)
        self.assertIn("A 20,000", text)

    def test_the_intervals_cover_the_fit(self):
        terms = terms_of(swept_markets(self.TRUE, markets=20))
        fit = fc.fit_sweeps(terms)
        intervals = fc.fit_intervals(terms, reps=30)
        for name in ("mu", "sigma", "hold", "sellers"):
            low, high = intervals[name]
            self.assertLessEqual(low, getattr(fit, name) + 1e-9)
            self.assertGreaterEqual(high, getattr(fit, name) - 1e-9)


def rate_of(history=QUIET, distance=0.01, prior=PRIOR):
    return fill_cost.fill_curve(history, prior, FLOOR).per_day(distance)


def one_market(fills=(), days=20, shares=100.0, cid="a", history=QUIET, ahead=0.0):
    """A market resting `shares` 1c under the mid, a day an interval, from T0."""
    rested = tuple(Rested(T0 + 86400.0 * d, 1.0, 0.01, shares, history, ahead)
                   for d in range(days))
    return Market(cid, cid, rested, tuple(fills), 0)


class ScoringTest(unittest.TestCase):
    def test_a_market_is_predicted_its_rate_times_days_and_one_sweeps_fill(self):
        tally = fc.predicted(one_market(days=1), PRODUCTION, Window.ALL, SPLIT, FLOOR)
        self.assertAlmostEqual(tally.events, rate_of())
        self.assertAlmostEqual(tally.shares, rate_of() * ONE.fill(100.0))
        self.assertAlmostEqual(tally.whole, rate_of() * ONE.survival(95.0))
        whole = fc.predicted(one_market(days=1), PRODUCTION._replace(sweeps=fill_cost.WHOLE_BID),
                             Window.ALL, SPLIT, FLOOR)
        self.assertEqual((round(whole.shares, 9), whole.whole), (round(rate_of() * 100.0, 9),
                                                                 whole.events))

    def test_a_stretch_behind_depth_is_predicted_what_gets_past_it(self):
        sweeps = fill_cost.Sweeps(4.5, 1.7, hold=0.3)
        model = PRODUCTION._replace(sweeps=sweeps)
        tally = fc.predicted(one_market(days=1, ahead=3000.0), model, Window.ALL, SPLIT, FLOOR)
        self.assertAlmostEqual(tally.events, rate_of() * sweeps.events(3000.0))
        self.assertAlmostEqual(tally.shares, rate_of() * sweeps.fill(100.0, 3000.0))
        self.assertAlmostEqual(tally.whole, rate_of() * sweeps.survival(95.0 + 900.0))
        front = fc.predicted(one_market(days=1), model, Window.ALL, SPLIT, FLOOR)
        self.assertAlmostEqual(front.events, rate_of())
        self.assertGreater(front.shares, 3 * tally.shares)

    def test_a_stretch_behind_more_than_the_depth_credit_is_predicted_as_behind_it(self):
        model = PRODUCTION._replace(sweeps=fill_cost.Sweeps(4.5, 1.7, hold=0.3))
        at_credit = fc.predicted(one_market(days=1, ahead=fill_cost.DEPTH_CREDIT_MAX), model,
                                 Window.ALL, SPLIT, FLOOR)
        self.assertGreater(at_credit.whole, 0.0)
        for ahead in (3 * fill_cost.DEPTH_CREDIT_MAX, 1e9):
            with self.subTest(ahead=ahead):
                self.assertEqual(fc.predicted(one_market(days=1, ahead=ahead), model, Window.ALL,
                                              SPLIT, FLOOR), at_credit)

    def test_a_stretch_carries_the_others_shares_bid_above_ours(self):
        def at(k):
            return datetime.fromtimestamp(T0 + 300.0 * k, timezone.utc)

        ours = (("FAV_BID", 0.95, 300.0),)
        samples = [farm_books.sample("a", at(0), 0.96, ((0.955, 1234.0), (0.95, 300.0)),
                                     ((0.965, 400.0),), ours),
                   farm_books.sample("a", at(1), 0.96, ((0.955, 20.0), (0.952, 6.0), (0.95, 5e4)),
                                     ((0.965, 400.0),), ours)]
        market = SimpleNamespace(start=math.inf, fm=SimpleNamespace(tick=0.001))
        self.assertEqual([r[-1] for r in fc.rested_of(samples, market, FLOOR)], [1200.0, 26.0])
        self.assertEqual([fc._rounded(x) for x in (0.0, 4.0, 1234.0, 20_001.0)],
                         [0.0, 4.0, 1200.0, 20_000.0])

    def test_the_windows_split_the_rested_days_and_the_fills(self):
        fills = [OwnFill("a", T0 + 86400.0 * d, 50.0, 100.0) for d in (1, 5, 12)]
        m = one_market(fills)
        predicted = {w: fc.predicted(m, PRODUCTION, w, SPLIT, FLOOR) for w in Window}
        realized = {w: fc.realized(m, w, SPLIT) for w in Window}
        for got in (predicted, realized):
            self.assertAlmostEqual(got[Window.ALL].events,
                                   got[Window.TRAIN].events + got[Window.TEST].events)
        self.assertAlmostEqual(predicted[Window.TEST].events, 10 * rate_of())
        self.assertEqual(realized[Window.TEST], Tally(1, 50.0))

    def test_realized_counts_the_fills_their_shares_and_the_whole_bid_ones(self):
        fills = [OwnFill("a", T0 + 60.0 * k, z, 100.0) for k, z in enumerate((10.0, 100.0, 150.0))]
        self.assertEqual(fc.realized(one_market(fills), Window.ALL, SPLIT), Tally(3, 260.0, 2))

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
        rested = (Rested(T0, 1.0, 0.01, 100.0, None, 0.0),
                  Rested(T0 + 86400.0, 1.0, 0.01, 100.0, QUIET, 0.0))
        m = Market("a", "a", rested, (OwnFill("a", T0 + 3600.0, 5.0, 100.0),), 0)
        rows = {r["source"]: r for r in fc.by_source([m], PRODUCTION, Window.ALL, SPLIT, FLOOR)}
        self.assertEqual(rows["prior"]["realized_events"], 1)
        self.assertEqual(rows["history, weight 0.5 or more"]["realized_events"], 0)
        self.assertAlmostEqual(rows["prior"]["predicted_events"], rate_of(None))

    def test_fills_and_stretches_count_by_the_shares_ahead_of_the_bid(self):
        sweeps = fill_cost.Sweeps(4.5, 1.7, hold=0.3)
        rested = (Rested(T0, 1.0, 0.01, 100.0, QUIET, 0.0),
                  Rested(T0 + 86400.0, 2.0, 0.01, 100.0, QUIET, 3000.0),
                  Rested(T0 + 3 * 86400.0, 1.0, 0.01, 100.0, QUIET, 25_000.0))
        fills = (OwnFill("a", T0 + 3600.0, 100.0, 100.0), OwnFill("a", T0 + 90000.0, 30.0, 100.0))
        rows = {r["bucket"]: r for r in fc.by_ahead([Market("a", "a", rested, fills, 0)],
                                                     PRODUCTION._replace(sweeps=sweeps),
                                                     Window.ALL, SPLIT, FLOOR)}
        self.assertEqual(list(rows), ["0-500", "500-2k", "2k-10k", "10k-20k", "20k+"])
        self.assertEqual([rows[b]["days"] for b in rows], [1.0, 0.0, 2.0, 0.0, 1.0])
        self.assertEqual((rows["0-500"]["realized"], rows["2k-10k"]["realized"]),
                         ([1, 100.0, 1], [1, 30.0, 0]))
        self.assertAlmostEqual(rows["2k-10k"]["predicted"][0], 2 * rate_of() * sweeps.events(3000.0))
        self.assertAlmostEqual(rows["20k+"]["predicted"][0], rate_of() * sweeps.events(25_000.0))
        self.assertGreater(rows["20k+"]["predicted"][0], 0.0)


def ratio(value, low, high):
    return fc.Ratio(value, (low, high))


def scored(events, shares, whole=ratio(1.0, 0.7, 1.4), model="refit", window=Window.TEST):
    return fc.Score(model, window, Tally(1, 1), Tally(1, 1), events, shares, whole)


PERFECT = ratio(1.0, 0.95, 1.05)


class GateTest(unittest.TestCase):
    CALIBRATED = scored(ratio(1.0, 0.8, 1.2), ratio(0.9, 0.7, 1.2))

    def test_the_sweeps_refit_before_the_split_calibrated_over_the_test_passes(self):
        self.assertEqual(fc.gate([self.CALIBRATED]), fc.Verdict(True, ()))

    def test_a_test_window_interval_outside_the_band_fails(self):
        for events in (ratio(0.5, 0.42, 0.6), ratio(2.0, 1.7, 2.4)):
            with self.subTest(events=events):
                verdict = fc.gate([scored(events, events)])
                self.assertFalse(verdict.passes)
                self.assertEqual(len(verdict.reasons), 2)

    def test_a_test_window_ratio_outside_the_band_fails_though_its_interval_reaches_it(self):
        verdict = fc.gate([scored(ratio(1.0, 0.76, 1.26), ratio(0.57, 0.39, 0.80))])
        self.assertEqual(verdict.reasons, ("test shares realized/predicted 0.57 [0.39, 0.80] "
                                           "is outside the band [0.67, 1.50]",))

    def test_whole_bid_fills_over_the_test_outside_the_band_fail(self):
        for whole, why in ((ratio(0.5, 0.35, 0.62), "misses"), (ratio(1.6, 1.1, 2.3), "is outside")):
            with self.subTest(whole=whole):
                verdict = fc.gate([self.CALIBRATED._replace(whole=whole)])
                self.assertEqual(verdict.reasons, (
                    f"test whole-bid fills realized/predicted {whole.value:.2f} "
                    f"[{whole.interval[0]:.2f}, {whole.interval[1]:.2f}] {why} the band "
                    f"[0.67, 1.50]",))

    def test_the_gate_fails_on_the_test_window_though_every_all_window_score_is_perfect(self):
        in_sample = [scored(PERFECT, PERFECT, PERFECT, model, Window.ALL)
                     for model in ("production", "refit")]
        verdict = fc.gate(in_sample + [scored(ratio(0.63, 0.48, 0.82), PERFECT, PERFECT)])
        self.assertEqual(verdict.reasons, ("test events realized/predicted 0.63 [0.48, 0.82] "
                                           "is outside the band [0.67, 1.50]",))

    def test_with_no_held_out_fit_the_gate_fails(self):
        verdict = fc.gate([scored(PERFECT, PERFECT, PERFECT, "production", Window.ALL),
                           scored(PERFECT, PERFECT, PERFECT, "production", Window.TEST)])
        self.assertEqual(verdict, fc.Verdict(False, ("no held-out size fit",)))


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
        self.assertEqual((result["data"]["over_bid"], result["data"]["whole"]), (0, 1))
        production = next(s for s in result["scores"]
                          if s["model"] == "production" and s["window"] == "all")
        self.assertEqual(production["realized"], [2, 300.0, 1])
        self.assertGreater(production["predicted"][0], 0)
        self.assertEqual([(row["quantile"], row["tail_days"]) for row in
                          result["tail_effect"]["rows"] if row["hurdle_rate"] == 1.0],
                         [(None, 0), (0.95, 10), (0.95, 30), (0.95, 60), (0.99, 10), (0.99, 30),
                          (0.99, 60)])
        self.assertEqual([row["bucket"] for row in result["ahead"]["all"]], list(fc.AHEAD_ORDER))
        for heading in ("Data", "Sweep fit", "Scores", "By predicted events", "By fill source",
                        "By shares ahead", "Tail share", "Top markets", "Tail cap on current markets",
                        "Gate"):
            self.assertIn(heading, rendered)

    def test_the_report_warns_when_the_stored_prior_is_not_the_one_sweeps_was_fit_against(self):
        warning = "warning: the stored prior was fit"
        _, rendered = self.run_report()
        self.assertIn(f"{warning} {PRIOR.at.isoformat()}", rendered)
        self.assertIn(fill_cost.SWEEPS_PRIOR_AT.isoformat(), rendered)
        with mock.patch.object(fill_cost, "SWEEPS_PRIOR_AT", PRIOR.at):
            _, rendered = self.run_report()
        self.assertNotIn(warning, rendered)

    def test_the_gate_section_prints_the_in_sample_scores_as_not_gated(self):
        result, rendered = self.run_report()
        self.assertEqual(result["gate"]["candidate"], ["refit", "test"])
        gate = rendered[rendered.index("\nGate\n"):]
        self.assertIn("in sample, not gated", gate)
        self.assertIn("production all", gate)

    def test_until_leaves_out_the_fills_and_bids_after_it(self):
        until = datetime.fromtimestamp(T0 + 50000.0, timezone.utc).isoformat()
        result, rendered = self.run_report("--until", until)
        self.assertEqual((result["data"]["fills"], result["data"]["covered_fills"]), (1, 1))
        self.assertLessEqual(fc._until(result["data"]["last"]), T0 + 50000.0 + 300.0)
        self.assertIn(f"data before {result['data']['until']}", rendered)


if __name__ == "__main__":
    unittest.main()
