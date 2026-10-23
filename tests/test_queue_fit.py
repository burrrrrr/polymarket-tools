import contextlib
import io
import json
import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from analysis import queue_fit
from analysis.queue_fit import Listing, Market, bursts, fit_unit, market_rows
from farm import reward_screener
from farm.exit_value import Piece, Sweep
from farm.farm_books import BookSample

T0 = 1_792_000_000.0
READ = datetime(2026, 10, 23, tzinfo=timezone.utc)


def book(at, mid, bids, asks=(), ours=()):
    return BookSample("c1", datetime.fromtimestamp(at, timezone.utc), mid, tuple(bids),
                      tuple(asks), tuple(ours))


def market(sells, tick=0.01):
    return Market("c1", "fav", tick, READ, T0 - 3600, sells)


class RowsTest(unittest.TestCase):
    # Bids best first; 200 of the 300 at 0.93 are our own. 0.90 lies more
    # than BOOK_RECORD_DEPTH under the 0.96 mid.
    SAMPLE = book(T0, 0.96, [(0.95, 100.0), (0.93, 300.0), (0.92, 50.0), (0.90, 500.0)],
                  asks=[(0.97, 80.0)], ours=[("fav_bid", 0.93, 200.0)])

    def test_each_bid_level_and_a_tick_over_it_is_a_row_of_the_depth_ahead_and_shares_past(self):
        sweep = Sweep(T0 + 100, (Piece(0.95, 80.0), Piece(0.93, 120.0), Piece(0.92, 30.0)))
        rows = [r for r in market_rows(market([sweep]), [self.SAMPLE]) if r.unit == "tx"]
        got = [(round(r.b, 9), r.A, r.below, r.X, r.age_s, r.sweep) for r in rows]
        self.assertEqual(got, [(0.96, 0.0, 230.0, 230.0, 100.0, 0),
                               (0.95, 100.0, 150.0, 230.0, 100.0, 0),
                               (0.94, 100.0, 150.0, 230.0, 100.0, 0),
                               (0.93, 400.0, 30.0, 230.0, 100.0, 0),
                               (0.92, 450.0, 0.0, 230.0, 100.0, 0)])

    def test_no_bid_is_priced_at_the_best_ask(self):
        sample = book(T0, 0.955, [(0.95, 100.0)], asks=[(0.96, 80.0)])
        rows = market_rows(market([Sweep(T0 + 1, (Piece(0.95, 10.0),))]), [sample])
        self.assertEqual({round(r.b, 9) for r in rows}, {0.95})

    def test_a_sweep_is_set_beside_the_last_sample_at_most_max_age_before_it(self):
        later = book(T0 + 1000, 0.96, [(0.95, 100.0)], asks=[(0.97, 80.0)])
        sweeps = [Sweep(T0 - 1, (Piece(0.95, 10.0),)),
                  Sweep(T0 + queue_fit.MAX_AGE_S, (Piece(0.95, 10.0),)),
                  Sweep(T0 + queue_fit.MAX_AGE_S + 1, (Piece(0.95, 10.0),)),
                  Sweep(T0 + 1100, (Piece(0.95, 10.0),))]
        rows = [r for r in market_rows(market(sweeps), [self.SAMPLE, later]) if r.unit == "tx"]
        self.assertEqual(sorted({(r.sweep, r.age_s) for r in rows}),
                         [(1, queue_fit.MAX_AGE_S), (3, 100.0)])


class BurstsTest(unittest.TestCase):
    def test_sweeps_within_the_gap_merge_into_one_timed_at_the_first(self):
        sweeps = [Sweep(T0, (Piece(0.95, 10.0),)),
                  Sweep(T0 + 60, (Piece(0.95, 5.0), Piece(0.94, 20.0))),
                  Sweep(T0 + 121, (Piece(0.93, 1.0),))]
        self.assertEqual(bursts(sweeps), [Sweep(T0, (Piece(0.95, 15.0), Piece(0.94, 20.0))),
                                          Sweep(T0 + 121, (Piece(0.93, 1.0),))])

    def test_a_burst_spans_at_most_max_sweep_span(self):
        sweeps = [Sweep(T0 + 50 * k, (Piece(0.95, 1.0),)) for k in range(14)]
        self.assertEqual([(s.at - T0, s.pieces) for s in bursts(sweeps)],
                         [(0.0, (Piece(0.95, 13.0),)), (650.0, (Piece(0.95, 1.0),))])


def walk(levels, x):
    """The pieces of a sell of x shares down levels, best first."""
    pieces = []
    for price, size in levels:
        take = min(x, size)
        if take > 0:
            pieces.append(Piece(price, take))
        x -= take
    return tuple(pieces)


def world(multiple, n=300, seed=1):
    """Rows of sells that walk a true book `multiple` times as deep as the
    sampled one, with a bottomless bid far under it for the rest."""
    rng, sells, samples = random.Random(seed), [], []
    for k in range(n):
        at = T0 + 1000 * k
        levels = [(round(0.95 - 0.01 * j, 2), float(rng.randint(0, 800))) for j in range(5)]
        samples.append(book(at, 0.96, levels, asks=[(0.97, 100.0)]))
        true = [(p, multiple * s) for p, s in levels] + [(0.80, 1e9)]
        sells.append(Sweep(at + rng.uniform(0, 300), walk(true, rng.uniform(1, 4000))))
    return market_rows(market(sells), samples)


class FitTest(unittest.TestCase):
    def test_the_fit_recovers_how_much_deeper_the_true_book_is_than_the_sample(self):
        for multiple in (0.5, 2.0):
            with self.subTest(multiple=multiple):
                u = fit_unit([r for r in world(multiple) if r.unit == "tx"], reps=10)
                self.assertAlmostEqual(u["alpha"], multiple, places=4)
                self.assertAlmostEqual(u["all"]["q1000"]["ratio_fit"], 1.0, places=4)
                matched = [c["q1000"]["matching_alpha"] for b, c in u["by_depth"].items()
                           if b != "0" and c["rows"] and c["q1000"]["observed"] > 0]
                self.assertTrue(matched)
                for alpha in matched:
                    self.assertAlmostEqual(alpha, multiple, places=4)

    def test_an_alpha_of_zero_overstates_fills_where_depth_rests_ahead(self):
        u = fit_unit([r for r in world(2.0) if r.unit == "tx"], reps=0)
        self.assertGreater(u["by_depth"]["(500,2000]"]["q200"]["ratio_0"], 1.0)
        self.assertLess(u["by_depth"]["(500,2000]"]["q200"]["ratio_fit"], 1.0001)


class FetchTest(unittest.TestCase):
    def setUp(self):
        first = T0 + 7200
        taker = [{"transactionHash": "early", "proxyWallet": "t", "side": "SELL", "asset": "fav",
                  "size": 5, "price": 0.95, "timestamp": first - 1000},
                 {"transactionHash": "hit", "proxyWallet": "t", "side": "SELL", "asset": "fav",
                  "size": 30, "price": 0.946, "timestamp": first + 60},
                 {"transactionHash": "lift", "proxyWallet": "t", "side": "BUY", "asset": "fav",
                  "size": 9, "price": 0.97, "timestamp": first + 70}]
        makers = [{"transactionHash": "hit", "proxyWallet": "m", "side": "BUY", "asset": "fav",
                   "size": 18, "price": 0.95, "timestamp": first + 60},
                  {"transactionHash": "hit", "proxyWallet": "m2", "side": "SELL", "asset": "long",
                   "size": 12, "price": 0.06, "timestamp": first + 60}]
        self.listing = Listing("c1", "fav", 0.01, first)
        self.read, self.failing = [], set()

        def fetch_trades(http, cid, taker_only, cutoff):
            if cid in self.failing:
                raise RuntimeError("no trades")
            self.read.append(cid)
            # The row cap ended both listings after the first trade they hold.
            return (taker if taker_only else taker + makers), first - 500

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for patch in (mock.patch.object(reward_screener, "fetch_trades", fetch_trades),
                      mock.patch.object(queue_fit.time, "sleep")):
            patch.start()
            self.addCleanup(patch.stop)

    def fetch(self, *found):
        with contextlib.redirect_stdout(io.StringIO()):
            queue_fit.fetch(list(found), self.dir.name, READ, None)
        path = Path(self.dir.name) / queue_fit.CACHE_FILE
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_sells_from_where_the_trades_are_complete_load_back_as_sweeps(self):
        [record] = self.fetch(self.listing)
        self.assertEqual(record["start"], T0 + 6700)
        [m] = queue_fit.load(self.dir.name)
        self.assertEqual((m.condition_id, m.fav_token_id, m.tick, m.read_at),
                         ("c1", "fav", 0.01, READ))
        self.assertEqual(m.sells, [Sweep(T0 + 7260, (Piece(0.95, 18.0), Piece(0.94, 12.0)))])

    def test_a_failed_market_is_left_for_the_next_run_and_a_cached_one_is_not_read_again(self):
        other = self.listing._replace(condition_id="c2")
        self.failing = {"c2"}
        self.assertEqual([r["condition_id"] for r in self.fetch(self.listing, other)], ["c1"])
        self.failing, self.read = set(), []
        self.assertEqual([r["condition_id"] for r in self.fetch(self.listing, other)],
                         ["c1", "c2"])
        self.assertEqual(self.read, ["c2", "c2"])


if __name__ == "__main__":
    unittest.main()
