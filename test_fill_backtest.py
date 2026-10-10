import contextlib
import io
import json
import math
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import fill_backtest
import fill_cost
import reward_screener
from exit_backtest import _utc, folds
from fill_backtest import (
    FLOOR,
    FORECAST_DISTANCES,
    POST_FILL_HORIZONS,
    SPLITS,
    Market,
    calibrate,
    cases,
    events,
    observe,
    post_fill,
)
from fill_cost import (
    DISTANCES,
    FLOOR_RATE,
    PRE_SWEEP_S,
    STRENGTHS,
    Hit,
    fill_curve,
    fit_prior,
    make_history,
)

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
DAY = 86400.0
STEP = 2 * DAY
# On the case clock, so every case falls a whole number of steps after START.
START = math.floor((NOW.timestamp() - 120 * DAY) / STEP) * STEP
T = START + 70 * DAY
STEADY = ((START - 3600, 0.95),)
FAR = NOW + timedelta(days=400)


def market(cid, hits, mids=STEADY, start=START, end_date=FAR):
    return Market.of(cid, cid, end_date, 0.001, hits, start, NOW, mids)


def world(hits, n=25, **kwargs):
    """n identical markets with the (unix time, deepest price) hits."""
    return [market(f"m{i:02}", hits, **kwargs) for i in range(n)]


def sweeps(period, deep, since=START, until=START + 120 * DAY):
    """One sweep to deep each period, 0.3 of a period in, so none lands on
    the case clock."""
    return [(since + (k + 0.3) * period, deep) for k in range(int((until - since) / period))]


def at(values, distance):
    return values[FORECAST_DISTANCES.index(distance)]


def observed_at(markets, t):
    """({condition_id: Observed}, dropped) of the cases at t."""
    seen, dropped = observe(markets, [c for c in cases(markets) if c.t == t])
    return {o.case.market.condition_id: o for o in seen}, dropped


def row(result, split, distance):
    [found] = [r for r in result["splits"][split] if r["distance"] == distance]
    return found


class CasesTest(unittest.TestCase):
    """Catches a case clock that drifts off exit_backtest's: another step, a
    case before the warm-up, a case whose horizon runs past the read or the
    expiry, or a mid outside the favorite range."""

    def test_cases_run_every_two_days_from_a_week_of_tape_to_a_horizon_before_the_read(self):
        mids = STEADY + ((START + 60 * DAY, 0.85), (START + 80 * DAY, 0.95))
        found = cases([market("m", [], mids)])
        last = NOW.timestamp() - START - 14 * DAY
        self.assertEqual([c.t - START for c in found],
                         [k * STEP for k in range(100) if 7 * DAY <= k * STEP <= last
                          and not 60 * DAY <= k * STEP < 80 * DAY])
        self.assertEqual({(c.mid, c.fold) for c in found}, {(0.95, 0)})

    def test_cases_stop_a_horizon_before_the_farmer_would_retire_the_market(self):
        # Expiry is a week before the end date, 23 days after T.
        found = cases([market("m", [], end_date=_utc(T + 30 * DAY))])
        self.assertEqual(max(c.t for c in found), T + 8 * DAY)

    def test_each_case_carries_its_markets_fold(self):
        markets = world([], n=7)
        self.assertEqual({c.market.condition_id: c.fold for c in cases(markets)}, folds(markets))


class PlantedRateTest(unittest.TestCase):
    """Twenty-five identical markets swept 2.5c deep once every two days:
    seven events in any fourteen days at every distance under 2.5c, none past
    it. Catches a per-day rate not scaled to the horizon, and a forecast or a
    realised count read off the wrong grid index."""

    @classmethod
    def setUpClass(cls):
        cls.markets = world(sweeps(2 * DAY, 0.925))
        cls.found = cases(cls.markets)
        cls.observed, cls.dropped = observe(cls.markets, cls.found)
        [cls.one] = [o for o in cls.observed
                     if o.case.t == T and o.case.market.condition_id == "m00"]

    def test_every_case_with_a_prior_is_observed_in_the_order_given(self):
        self.assertEqual([o.case for o in self.observed], self.found)
        self.assertEqual(self.dropped, 0)

    def test_a_sweep_every_two_days_is_forecast_and_realised_as_seven_in_fourteen_days(self):
        self.assertAlmostEqual(at(self.one.forecast, 0.02), 7.0, delta=0.7)
        self.assertEqual(at(self.one.realised, 0.02), 7)
        self.assertEqual(self.one.source, "history")

    def test_a_distance_no_sweep_reached_is_forecast_at_the_floor_rate_and_realises_nothing(self):
        self.assertAlmostEqual(at(self.one.forecast, 0.03), FLOOR_RATE * 14)
        self.assertEqual(at(self.one.realised, 0.03), 0)

    def test_the_pooled_ratio_is_one_and_identical_markets_collapse_its_interval(self):
        r = row(calibrate(self.observed, reps=100), "all", 0.02)
        self.assertEqual((r["cases"], r["markets"]), (len(self.found), 25))
        self.assertAlmostEqual(r["forecast"], 7.0, delta=0.7)
        self.assertEqual(r["realised"], 7.0)
        self.assertTrue(0.9 <= r["ratio"] <= 1.1, r["ratio"])
        self.assertAlmostEqual(r["ratio"], r["forecast"] / 7.0)
        low, high = r["ratio_interval"]
        self.assertAlmostEqual(low, r["ratio"])
        self.assertAlmostEqual(high, r["ratio"])

    def test_the_chance_of_an_event_is_poisson_in_the_forecast_beside_the_share_that_saw_one(self):
        result = calibrate(self.observed, reps=100)
        near, far = row(result, "all", 0.02), row(result, "all", 0.03)
        self.assertAlmostEqual(near["p_forecast"], 1 - math.exp(-7.0), delta=0.002)
        self.assertEqual(near["p_realised"], 1.0)
        self.assertAlmostEqual(far["p_forecast"], 1 - math.exp(-FLOOR_RATE * 14))
        self.assertEqual(far["p_realised"], 0.0)

    def test_a_distance_that_realised_nothing_has_no_ratio(self):
        far = row(calibrate(self.observed, reps=100), "all", 0.03)
        self.assertEqual((far["realised"], far["ratio"], far["ratio_interval"]),
                         (0.0, None, None))

    def test_workers_observe_what_one_process_does(self):
        plain = lambda seen: [(o.case.market.condition_id, o.case.t, o.history, o.source,
                               o.forecast, o.realised) for o in seen]
        found = [c for c in self.found if T <= c.t <= T + 3 * STEP]
        seen, dropped = observe(self.markets, found, workers=2)
        self.assertEqual(plain(seen), plain([o for o in self.observed if o.case in found]))
        self.assertEqual(dropped, 0)


class NoLookAheadTest(unittest.TestCase):
    """Nothing reaches under the mid before T, then ten sweeps reach 3c in the
    fourteen days after it. Catches later hits leaking into the history as of
    T or into the prior fit from the other markets' histories."""

    EARLY = Hit(T - 5 * DAY, 0.955)
    markets = world([EARLY] + [(T + (k + 0.5) * DAY, 0.92) for k in range(10)])

    def test_the_forecast_at_a_case_knows_nothing_of_the_sweeps_after_it(self):
        seen, dropped = observed_at(self.markets, T)
        self.assertEqual((len(seen), dropped), (25, 0))
        for o in seen.values():
            self.assertEqual(o.history.events, (0,) * len(DISTANCES))
            self.assertEqual(o.history.read_at, _utc(T))
            for forecast in o.forecast:
                self.assertAlmostEqual(forecast, FLOOR_RATE * 14)
            self.assertEqual(at(o.realised, 0.02), 10)

    def test_no_history_as_of_a_time_is_handed_a_hit_after_it(self):
        handed, real = [], fill_cost.make_history

        def record(hits, mids, start, now, floor):
            hits = list(hits)
            handed.append((now, hits))
            return real(hits, mids, start, now, floor)

        with mock.patch.object(fill_cost, "make_history", record):
            observed_at(self.markets, T)
        as_of = [hits for now, hits in handed if now == _utc(T)]
        self.assertEqual(as_of, [[self.EARLY]] * 25)


class RealisedTest(unittest.TestCase):
    """Catches a realised counter with a looser definition than History's: one
    counting hits or undeclustered sweeps, fills at the bid, sweeps under the
    floor, or sweeps on either edge of the window."""

    MIDS = STEADY + ((T + 2 * DAY - 3600, 0.89), (T + 2 * DAY + 3600, 0.95))

    def observed(self, *hits):
        """The case at T of a market hit (seconds after T, deepest price)."""
        markets = world([(T + after, deep) for after, deep in hits], mids=self.MIDS)
        return observed_at(markets, T)[0]["m00"]

    def test_the_realised_count_is_historys_declustered_floored_strictly_past_events(self):
        # Half an hour apart, the first two are one event wherever both
        # reach; the third finds a 0.89 mid; the last fills a 0.94 bid
        # without trading through it.
        o = self.observed((3600, 0.93), (5400, 0.92), (2 * DAY, 0.85), (5 * DAY, 0.94))
        self.assertEqual(tuple(at(o.realised, d) for d in (0.0075, 0.01, 0.02, 0.03)),
                         (2, 1, 1, 0))

    def test_a_sweep_exactly_at_the_case_time_is_in_neither_the_history_nor_the_realised(self):
        o = self.observed((0, 0.90))
        self.assertEqual((o.history.events[0], o.realised[0]), (0, 0))

    def test_a_sweep_at_the_end_of_the_horizon_is_not_realised(self):
        o = self.observed((14 * DAY - 7200, 0.93), (14 * DAY, 0.93))
        self.assertEqual(at(o.realised, 0.01), 1)


def messy(seed=7):
    """Hits in bursts over 57 days, some before the first mid and some after
    the read, and a mid that twice drops under the floor."""
    rng, end, hits = random.Random(seed), NOW.timestamp(), []
    for _ in range(300):
        when = end - rng.uniform(-1, 56) * DAY
        for _ in range(rng.choice((1, 1, 2, 4))):
            hits.append(Hit(when, rng.uniform(0.90, 0.955)))
            when += rng.uniform(1, 90)
    mids = [(end - 53 * DAY, 0.95), (end - 40 * DAY, 0.89), (end - 38 * DAY, 0.95),
            (end - 20 * DAY, 0.93), (end - 19 * DAY, 0.96), (end - 5 * DAY, 0.895),
            (end - 4 * DAY, 0.95)]
    return hits, mids


class EventsTest(unittest.TestCase):
    """Catches the post-fill table's events drifting from the events History
    counts: another chaining, pre-sweep mid, floor, window or declustering."""

    def test_the_events_at_each_distance_are_the_ones_history_counts(self):
        hits, mids = messy()
        start = NOW.timestamp() - 55 * DAY
        history = make_history(hits, mids, start, NOW, FLOOR)
        found = events(hits, mids, start, NOW.timestamp(), FLOOR)
        self.assertEqual(tuple(len(found[d]) for d in DISTANCES), history.events)
        self.assertGreater(history.events[0], history.events[DISTANCES.index(0.03)])
        self.assertGreater(history.events[DISTANCES.index(0.03)], 10)

    def test_an_event_carries_its_sweeps_first_hit_time_and_the_mid_before_it_moved(self):
        mids = [(T - 3600, 0.95), (T + 30, 0.93)]
        hits = [Hit(T + 95, 0.92), Hit(T + 50, 0.93), Hit(T, 0.94)]
        [(when, mid, reach)] = events(hits, mids, START, T + DAY, FLOOR)[0.01]
        self.assertEqual((when, mid), (T, 0.95))
        self.assertAlmostEqual(reach, 0.03)

    def test_events_reach_back_past_historys_sixty_days(self):
        mids = [(T - 110 * DAY, 0.95)]
        hits = [Hit(T - 100 * DAY, 0.93), Hit(T - DAY, 0.93)]
        self.assertEqual(make_history(hits, mids, 0.0, _utc(T), FLOOR).events[0], 1)
        self.assertEqual([e[0] for e in events(hits, mids, 0.0, T, FLOOR)[0.01]],
                         [T - 100 * DAY, T - DAY])

    def test_a_sweep_before_the_first_mid_is_no_event(self):
        first = STEADY[0][0]
        hits = [Hit(first + PRE_SWEEP_S - 1, 0.93), Hit(first + 2 * DAY, 0.93)]
        self.assertEqual([e[0] for e in events(hits, STEADY, 0.0, T, FLOOR)[0.01]],
                         [first + 2 * DAY])


class PriorTest(unittest.TestCase):
    """Catches a prior fit on too few markets, on a market's own fold, or on
    histories that had not begun by the case's time."""

    HITS = sweeps(2 * DAY, 0.925)

    def test_a_case_whose_other_folds_hold_under_twenty_markets_is_dropped_and_counted(self):
        # Folds of 5, 5, 5, 5 and 4: only the last fold has 20 other markets.
        seen, dropped = observed_at(world(self.HITS, n=24), T)
        self.assertEqual((set(seen), dropped), ({"m04", "m09", "m14", "m19"}, 20))

    def test_a_market_whose_tape_starts_after_the_case_fits_no_prior(self):
        markets = world(self.HITS)
        markets[1] = market("m01", self.HITS, start=T + DAY)
        seen, dropped = observed_at(markets, T)
        self.assertEqual((set(seen), dropped), ({"m06", "m11", "m16", "m21"}, 20))

    def test_a_markets_history_is_shrunk_toward_a_prior_fit_without_its_own_fold(self):
        # m00 is swept every two hours, 720 times in sixty days; the twenty
        # markets of the other folds once every two days.
        markets = world(self.HITS)
        markets[0] = market("m00", sweeps(7200.0, 0.925))
        seen, _ = observed_at(markets, T)

        as_of = lambda m: make_history([h for h in m.hits if h.at <= T], m.mids, m.start,
                                       _utc(T), FLOOR)
        forecast = lambda prior: tuple(fill_curve(as_of(markets[0]), prior, FLOOR).per_day(d) * 14
                                       for d in FORECAST_DISTANCES)
        others = fit_prior([as_of(m) for i, m in enumerate(markets) if i % 5], _utc(T), FLOOR)
        everyone = fit_prior([as_of(m) for m in markets], _utc(T), FLOOR)
        self.assertEqual(seen["m00"].forecast, forecast(others))
        self.assertNotEqual(seen["m00"].forecast, forecast(everyone))
        # Identical counts are likeliest under the strongest prior.
        strength = STRENGTHS[-1]
        self.assertAlmostEqual(at(seen["m00"].forecast, 0.02),
                               (720 + strength * 0.5) / (60 + strength) * 14)


class SplitsTest(unittest.TestCase):
    """Catches a split that classifies by another threshold, or by anything
    not known at the case's time."""

    @classmethod
    def setUpClass(cls):
        hits = sweeps(2 * DAY, 0.925)
        cls.markets = world(hits) + [
            market("quiet", sweeps(2 * DAY, 0.945)),
            market("young", hits, start=T - 20 * DAY + 3600),
            market("twenty", hits, start=T - 20 * DAY),
            market("near", hits, end_date=_utc(T + 60 * DAY - 60)),
            market("sixty", hits, end_date=_utc(T + 60 * DAY)),
        ]
        cls.observed, cls.dropped = observe(cls.markets,
                                            [c for c in cases(cls.markets) if c.t == T])
        cls.everyone = {m.condition_id for m in cls.markets}

    def members(self, split):
        return {o.case.market.condition_id for o in self.observed if SPLITS[split](o)}

    def test_every_market_has_a_case(self):
        self.assertEqual((self.members("all"), self.dropped), (self.everyone, 0))

    def test_a_market_with_no_event_past_1c_is_quiet_and_not_active(self):
        self.assertEqual(self.members("quiet as of t (no event past 1c)"), {"quiet"})
        self.assertEqual(self.members("active as of t (an event past 1c)"),
                         self.everyone - {"quiet"})

    def test_twenty_days_of_history_is_not_under_twenty(self):
        self.assertEqual(self.members("history under 20d"), {"young"})
        self.assertEqual(self.members("history 20d+"), self.everyone - {"young"})

    def test_sixty_days_to_resolution_is_not_under_sixty(self):
        self.assertEqual(self.members("under 60d to resolution"), {"near"})
        self.assertEqual(self.members("60d+ to resolution"), self.everyone - {"near"})

    def test_each_split_is_calibrated_on_its_own_cases(self):
        result = calibrate(self.observed, reps=100)
        self.assertEqual(list(result["splits"]), list(SPLITS))
        for split in SPLITS:
            with self.subTest(split=split):
                r = row(result, split, 0.0025)
                self.assertEqual((r["cases"], r["markets"]),
                                 (len(self.members(split)), len(self.members(split))))

    def test_a_market_outside_a_split_adds_nothing_to_its_interval(self):
        # The quiet market's sweeps reach half a cent, seven a fortnight.
        r = row(calibrate(self.observed, reps=100), "quiet as of t (no event past 1c)", 0.0025)
        self.assertEqual(r["realised"], 7.0)
        self.assertAlmostEqual(r["ratio"], r["forecast"] / 7.0)
        low, high = r["ratio_interval"]
        self.assertAlmostEqual(low, r["ratio"])
        self.assertAlmostEqual(high, r["ratio"])
        beyond = row(calibrate(self.observed, reps=100), "quiet as of t (no event past 1c)", 0.01)
        self.assertEqual((beyond["realised"], beyond["ratio"]), (0.0, None))

    def test_a_split_without_cases_has_no_rows(self):
        active = [o for o in self.observed if o.case.market.condition_id != "quiet"]
        result = calibrate(active, reps=10)
        self.assertEqual(result["splits"]["quiet as of t (no event past 1c)"], [])
        self.assertEqual((result["cases"], result["markets_with_cases"]), (29, 29))


class PostFillTest(unittest.TestCase):
    """A sweep at S finds a 0.95 mid, which drops to 0.93 within the minute
    and is back two days later; the mids end twenty days after S. Catches a
    table measured from the fill price of another distance, at another
    horizon, past the mids' end, or resampled by event instead of market."""

    S = START + 30 * DAY
    MIDS = STEADY + ((S + 30, 0.93), (S + 2 * DAY, 0.95), (S + 20 * DAY, 0.951))

    def cells(self, *hits):
        markets = world(list(hits), n=3, mids=self.MIDS)
        return {(r["distance"], r["later"]): r for r in post_fill(markets, reps=100)}

    def test_the_mid_is_measured_against_the_bid_the_sweep_filled(self):
        cells = self.cells((self.S, 0.915))
        expected = {0.01: (-1.0, -1.0, 1.0, 1.0), 0.02: (0.0, 0.0, 2.0, 2.0),
                    0.03: (1.0, 1.0, 3.0, 3.0)}
        self.assertEqual(set(cells),
                         {(d, later) for d in expected for later, _ in POST_FILL_HORIZONS})
        for d, cents in expected.items():
            for (later, _), cent in zip(POST_FILL_HORIZONS, cents):
                with self.subTest(distance=d, later=later):
                    r = cells[d, later]
                    self.assertEqual((r["events"], r["markets"]), (3, 3))
                    for key in ("mean", "q25", "median", "q75"):
                        self.assertAlmostEqual(r[key], cent)
                    self.assertAlmostEqual(r["interval"][0], cent)
                    self.assertAlmostEqual(r["interval"][1], cent)

    def test_a_sweep_that_did_not_reach_a_distance_fills_no_bid_there(self):
        cells = self.cells((self.S, 0.925))
        self.assertEqual([cells[d, "1h"]["events"] for d in (0.01, 0.02, 0.03)], [3, 3, 0])
        self.assertEqual([cells[0.03, "1h"][key] for key in ("mean", "interval", "median")],
                         [None, None, None])

    def test_a_horizon_running_past_the_last_mid_is_skipped_for_that_event_alone(self):
        # The second sweep's fourteen days end four days after the mids do.
        cells = self.cells((self.S, 0.915), (self.S + 10 * DAY, 0.915))
        self.assertEqual([cells[0.01, later]["events"] for later, _ in POST_FILL_HORIZONS],
                         [6, 6, 6, 3])
        self.assertAlmostEqual(cells[0.01, "7d"]["mean"], 1.0)
        self.assertAlmostEqual(cells[0.01, "14d"]["mean"], 1.0)

    def test_quartiles_pool_the_events_and_the_interval_resamples_markets(self):
        # Each market holds one fill a cent under water an hour on and one a
        # cent over: every resample of markets has the same mean.
        hour = self.cells((self.S, 0.915), (self.S + 10 * DAY, 0.915))[0.01, "1h"]
        self.assertAlmostEqual(hour["mean"], 0.0)
        self.assertAlmostEqual(hour["q25"], -1.0)
        self.assertAlmostEqual(hour["median"], 0.0)
        self.assertAlmostEqual(hour["q75"], 1.0)
        self.assertAlmostEqual(hour["interval"][0], 0.0)
        self.assertAlmostEqual(hour["interval"][1], 0.0)


class ReportTest(unittest.TestCase):
    """Catches a report that loses the dropped cases, or a text table that
    cannot print a split without cases or a ratio without a value."""

    def test_the_report_counts_what_it_kept_and_dropped_and_prints_every_table(self):
        hits = sweeps(2 * DAY, 0.925)
        markets = world(hits, n=24) + [market("late", hits, start=START + 60 * DAY)]
        result = fill_backtest.report(markets, reps=20)
        found = cases(markets)
        self.assertEqual(result["markets"], 25)
        self.assertEqual(result["cases"] + result["dropped"], len(found))
        self.assertGreater(result["dropped"], 0)
        self.assertGreater(result["cases"], 0)
        self.assertEqual(result["markets_with_cases"], 25)
        json.dumps(result)
        text = fill_backtest.render(result)
        for split in SPLITS:
            self.assertIn(split, text)
        for later, _ in POST_FILL_HORIZONS:
            self.assertIn(later, text)


FAV = "fav"
FIRST_TRADE = NOW.timestamp() - 100 * DAY
EXIT_READ = NOW - timedelta(days=2)
# The minute mids the API would serve, where they change and at each edge.
RAW_MIDS = [(FIRST_TRADE - DAY, 0.95), (FIRST_TRADE - 1800, 0.95),
            (NOW.timestamp() - 60 * DAY, 0.94), (EXIT_READ.timestamp() - 30, 0.94),
            (NOW.timestamp() - DAY, 0.93), (NOW.timestamp() - 600, 0.93)]


def trade(when, price, side="SELL", tx=None):
    return {"transactionHash": tx or f"tx{when}", "proxyWallet": "w", "asset": FAV,
            "side": side, "size": 10.0, "price": price, "timestamp": when}


def exit_record(cid, mids):
    return {"condition_id": cid, "slug": cid, "end_date": FAR.isoformat(), "tick": 0.001,
            "fav_token_id": FAV, "tape": {"read_at": EXIT_READ.isoformat()}, "mids": mids}


class FetchTest(unittest.TestCase):
    """Catches hits parsed from the wrong listing or from before the trades
    are complete, mids refetched where the exit cache holds them or missing
    where it does not, and a rerun that refetches or loses a market."""

    def setUp(self):
        taker = [trade(FIRST_TRADE, 0.93), trade(NOW.timestamp() - 50 * DAY, 0.94, tx="deep"),
                 trade(NOW.timestamp() - DAY, 0.92), trade(NOW.timestamp() - 3600, 0.97, "BUY")]
        # The maker a taker sold to bought the favorite deeper than the
        # taker's average price.
        maker = trade(NOW.timestamp() - 50 * DAY, 0.91, "BUY", tx="deep")
        self.failing, self.read, self.spans = set(), [], []

        def fetch_trades(http, cid, taker_only, cutoff):
            if cid in self.failing:
                raise RuntimeError("no trades")
            self.read.append(cid)
            return (taker if taker_only else taker + [maker]), cutoff

        def fetch_mids(http, token, start, end):
            self.spans.append((start, end))
            return [(t, p) for t, p in RAW_MIDS if start <= t <= end]

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for patch in (mock.patch.object(reward_screener, "fetch_trades", fetch_trades),
                      mock.patch.object(reward_screener, "fetch_mids", fetch_mids),
                      mock.patch.object(fill_backtest.time, "sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    def fetch(self, *records):
        with contextlib.redirect_stdout(io.StringIO()):
            fill_backtest.fetch(list(records), self.dir.name, NOW, None)
        path = Path(self.dir.name) / fill_backtest.CACHE_FILE
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_hits_come_from_the_taker_rows_at_their_makers_deepest_price(self):
        [record] = self.fetch(exit_record("c1", [[FIRST_TRADE - DAY, 0.95]]))
        self.assertEqual(record["start"], FIRST_TRADE)
        self.assertEqual(record["hits"], [[FIRST_TRADE, 0.93],
                                          [NOW.timestamp() - 50 * DAY, 0.91],
                                          [NOW.timestamp() - DAY, 0.92]])
        self.assertEqual(record["read_at"], NOW.isoformat())

    def test_mids_the_exit_cache_holds_are_extended_to_now_and_not_refetched(self):
        cached = [[FIRST_TRADE - DAY, 0.95], [NOW.timestamp() - 60 * DAY, 0.94]]
        [record] = self.fetch(exit_record("c1", cached))
        self.assertEqual(self.spans, [(EXIT_READ.timestamp() - 60, NOW.timestamp())])
        # The last point repeats 0.93: it is how far the mids were read.
        self.assertEqual(record["mids"], cached + [[NOW.timestamp() - DAY, 0.93],
                                                   [NOW.timestamp() - 600, 0.93]])

    def test_mids_missing_before_the_first_hit_are_read_up_to_the_cached_ones(self):
        cached = [[NOW.timestamp() - 60 * DAY, 0.94]]
        [record] = self.fetch(exit_record("c1", cached))
        lead = PRE_SWEEP_S + reward_screener.MIDS_LEAD_S
        self.assertEqual(sorted(self.spans),
                         [(FIRST_TRADE - lead, NOW.timestamp() - 60 * DAY),
                          (EXIT_READ.timestamp() - 60, NOW.timestamp())])
        self.assertEqual(record["mids"], [[FIRST_TRADE - 1800, 0.95]] + cached
                         + [[NOW.timestamp() - DAY, 0.93], [NOW.timestamp() - 600, 0.93]])

    def test_a_failed_market_is_left_for_the_next_run_and_a_cached_one_is_not_read_again(self):
        records = [exit_record(cid, [[FIRST_TRADE - DAY, 0.95]]) for cid in ("c1", "c2")]
        self.failing = {"c2"}
        self.assertEqual([r["condition_id"] for r in self.fetch(*records)], ["c1"])
        self.failing, self.read = set(), []
        self.assertEqual([r["condition_id"] for r in self.fetch(*records)], ["c1", "c2"])
        self.assertEqual(set(self.read), {"c2"})

    def test_a_cached_market_loads_with_its_hits_and_mids(self):
        self.fetch(exit_record("c1", [[FIRST_TRADE - DAY, 0.95]]))
        [m] = fill_backtest.load(self.dir.name)
        self.assertEqual((m.condition_id, m.end_date, m.tick, m.start, m.read_at),
                         ("c1", FAR, 0.001, FIRST_TRADE, NOW))
        self.assertEqual(m.hits[0], Hit(FIRST_TRADE, 0.93))
        self.assertEqual(m.mids[-1], (NOW.timestamp() - 600, 0.93))
        self.assertEqual((m.mid.at(FIRST_TRADE), m.mid.at(NOW.timestamp())), (0.95, 0.93))


if __name__ == "__main__":
    unittest.main()
