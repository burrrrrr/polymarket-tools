import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from analysis import pricing_backtest as pb
from analysis.exit_backtest import Mids
from farm import fill_cost
from farm.exit_value import Piece, Sweep
from farm.farm_books import BookSample
from farm.fill_cost import DAY_S, DEFAULT_PRIOR, WHOLE_BID, Hit
from farm.fill_watch import Priced
from farm.reward_farmer import (FarmConfig, FarmMarket, Position, Slot, market_outlook,
                                target_orders, with_book_mid)
from farm.reward_screener import Zones
from polymarket.books import Levels

T0 = 1_792_000_000.0
FLOOR = 0.90
CFG = FarmConfig(capital=500.0, max_lead=0.005, ask_offset=0.01, min_capture=0.005,
                 ev_margin=0.01, min_fav=FLOOR, min_low_score=0.10, keep_ratio=0.8,
                 drop_threshold=0.03, drop_window_s=60.0, cooldown_s=600.0,
                 naked_cooldown_s=3600.0, expiry_buffer_days=7.0, hurdle_rate=1.0,
                 bid_multiple=4.0, tie_band=0.05)
END = datetime.fromtimestamp(T0, timezone.utc) + timedelta(days=100)
FM = FarmMarket(condition_id="c1", slug="m", question="?", end_date=END, neg_risk=False,
                tick=0.001, fav_token_id="fav", fav_outcome="No", rate_per_day=100.0, v=0.035,
                min_size=50.0, long_token_id="", p_true=0.995, naked_budget_usd=25.0,
                expires_at=END, capital=500.0, zones=Zones(100 / 365.25, 0.96),
                bid_multiple=4.0)
BIDS = ((0.958, 300.0), (0.955, 2000.0), (0.95, 3000.0), (0.94, 5000.0))
ASKS = ((0.962, 400.0), (0.965, 2000.0), (0.97, 3000.0))
BOOK = Levels(bids=list(BIDS), asks=list(ASKS), tick=0.001)


def sweep(at, *pieces):
    return Sweep(at, tuple(Piece(p, s) for p, s in pieces))


def market(sells=(), buys=(), mids=None):
    mids = mids or [(T0 - 30 * DAY_S + 60 * k, 0.96) for k in range(0, 60 * 24 * 40, 7)]
    mid = Mids(mids)
    sells, buys = sorted(sells), sorted(buys)
    hits = [Hit(s.at, min(p.price for p in s.pieces)) for s in sells]
    return pb.Market("c1", FM, False, T0 - 30 * DAY_S, list(sells), hits,
                     pb.market_bursts(list(sells), mid) if sells else [],
                     pb.market_bursts(list(buys), mid) if buys else [], mids, mid,
                     pb.hit_mids(hits, mids))


def samples(n, every=300.0, start=T0, lift_after=math.inf):
    """n samples of BOOK every five minutes, the book 2c higher from
    lift_after seconds after T0 on."""
    def shifted(levels, by):
        return tuple((round(p + by, 3), s) for p, s in levels)

    out = []
    for k in range(n):
        by = 0.02 if every * k + start - T0 >= lift_after else 0.0
        out.append(BookSample("c1", datetime.fromtimestamp(start + every * k, timezone.utc),
                              0.96 + by, shifted(BIDS, by), shifted(ASKS, by), ()))
    return out


def walked(m, rules, n=24, lift_after=math.inf):
    """(Run, [(t - T0, bid)]) of one arm over n samples five minutes apart."""
    trace = []
    run = pb.walk_run(m, samples(n, lift_after=lift_after), CFG, rules, pb.Plans(m, CFG),
                      trace=trace)
    return run, [(t - T0, bid) for t, bid in trace]


class RulesTest(unittest.TestCase):
    def test_today_is_the_farmer_with_every_new_rule_off(self):
        fm = with_book_mid(FM, BOOK)
        outlook = market_outlook(fm, None, None, DEFAULT_PRIOR, CFG, END - timedelta(days=100))
        arm_fm, arm_cfg, arm_outlook = pb.TODAY.terms(fm, CFG, outlook)
        self.assertEqual((arm_fm.bid_multiple, arm_cfg.tie_band, arm_outlook.fills.sweep_sizes),
                         (1.0, 0.0, WHOLE_BID))
        self.assertEqual(pb.COMBINED.terms(fm, CFG, outlook), (fm, CFG, outlook))
        sold = pb.NO_FILLS._replace(last_exit=Priced(0.95, datetime.fromtimestamp(T0, timezone.utc)))
        self.assertIsNone(pb.TODAY.ceiling(sold, 0.0, None, 0.001, sold.last_exit.at))
        self.assertEqual(pb.COMBINED.ceiling(sold, 0.0, None, 0.001, sold.last_exit.at).price, 0.949)

    def test_each_ablation_switches_one_rule_back_to_todays(self):
        today, combined = pb.TODAY._asdict(), pb.COMBINED._asdict()
        for arm in pb.ABLATIONS:
            changed = {k for k, v in arm._asdict().items() if k != "name" and v != combined[k]}
            self.assertEqual(len(changed), 1, arm.name)
            (field,) = changed
            self.assertEqual(getattr(arm, field), today[field], arm.name)

    def test_the_today_arm_rests_what_target_orders_plans_from_flat(self):
        m = market()
        run, trace = walked(m, pb.TODAY, n=1)
        fm, levels, outlook = pb.Plans(m, CFG).at(samples(1)[0])
        arm_fm, arm_cfg, arm_outlook = pb.TODAY.terms(fm, CFG, outlook)
        planned = target_orders(arm_fm, levels, Position(), arm_cfg, True, arm_outlook,
                                bid_without_history=True)
        bid = next(i for i in planned.intents if i.slot is Slot.FAV_BID)
        self.assertEqual(trace, [(0.0, pb.Resting(bid.price, bid.size))])


class WalkTest(unittest.TestCase):
    def test_a_burst_fills_the_bid_which_is_gone_until_the_next_decision(self):
        burst = sweep(T0 + 400, (0.93, 50_000.0))
        run, trace = walked(market([burst]), pb.TODAY)
        first = trace[0][1]
        self.assertEqual(run.bought_shares, first.shares)
        self.assertAlmostEqual(run.bought, first.price * first.shares)
        # The fill makes the first sample after it, at 600 s, a decision.
        self.assertEqual([t for t, _ in trace][:2], [0.0, 600.0])
        self.assertEqual(run.fills, 1)

    def test_after_a_fill_the_combined_arm_bids_a_tick_under_it_and_today_does_not(self):
        burst = sweep(T0 + 400, (0.93, 50.0))
        today = walked(market([burst]), pb.TODAY)[1]
        combined = walked(market([burst]), pb.COMBINED)[1]
        self.assertEqual(today[1][1].price, today[0][1].price)
        self.assertLessEqual(combined[1][1].price, round(combined[0][1].price - 0.001, 3))

    def test_buys_over_the_ask_sell_and_hold_the_bid_under_the_sale(self):
        """The book lifts 2c after the sale, where a bid would rest over it."""
        fill = sweep(T0 + 400, (0.93, 50.0))
        buy = sweep(T0 + 1000, (0.999, 100_000.0))
        run, trace = walked(market([fill], [buy]), pb.COMBINED, lift_after=1200)
        self.assertGreater(run.sold, 0)
        self.assertEqual(run.held, 0.0)
        sold_at = run.sold / run.sold_shares
        after = [bid for t, bid in trace if t >= 1200 and bid is not None]
        self.assertTrue(after)
        self.assertTrue(all(bid.price <= round(sold_at - 0.001, 3) for bid in after))
        lifted = [bid for t, bid in walked(market(), pb.COMBINED, lift_after=1200)[1]
                  if t >= 1200]
        self.assertGreater(max(bid.price for bid in lifted if bid), sold_at)

    def test_inventory_at_the_end_is_marked_and_carried(self):
        burst = sweep(T0 + 400, (0.93, 50.0))
        run, _ = walked(market([burst]), pb.COMBINED)
        self.assertGreater(run.held, 0)
        self.assertGreater(run.carry, 0)
        self.assertEqual(run.marks, (0.96, 0.96))
        self.assertGreater(run.peak, 0)


class RunTest(unittest.TestCase):
    RUN = pb.Run(reward=10.0, bought=95.0, bought_shares=100.0, sold=48.5, sold_shares=50.0,
                 carry=0.2, held=50.0, cost=47.5, marks=(0.96, None), hold_s=30 * DAY_S,
                 decisions=4, bids=3, bid_usd=1500.0, room_usd=500.0, peak=0.19, fills=1)

    def test_net_is_reward_less_bought_plus_sold_and_what_is_held_marked_less_its_carry(self):
        carry_h = 47.5 * math.log(2) * DAY_S / (365 * DAY_S)
        self.assertAlmostEqual(self.RUN.net(0, 1.0), 10 - 95 + 48.5 - 0.2 + 50 * 0.96 - carry_h)
        held = 47.5 * math.log(2) * 30 / 365
        self.assertAlmostEqual(self.RUN.net(0, 1.0, held=True),
                               10 - 95 + 48.5 - 0.2 + 50 * 0.96 - held)
        self.assertIsNone(self.RUN.net(1, 1.0))

    def test_the_expected_hold_is_cut_short_at_resolution(self):
        rate = fill_cost.sales_per_day(500.0)
        self.assertAlmostEqual(pb.expected_hold_s(500.0, 10.0), DAY_S / rate, delta=1.0)
        self.assertAlmostEqual(pb.expected_hold_s(500.0, 2 / 365.25), 2 * DAY_S, delta=0.05 * DAY_S)

    def test_runs_split_where_the_samples_stop(self):
        at = samples(4) + samples(2, start=T0 + 5000)
        self.assertEqual([len(r) for r in pb.runs(at)], [4, 2])


class LeakageTest(unittest.TestCase):
    T = T0 + 5 * DAY_S
    PAST = [sweep(T0 + DAY_S, (0.93, 400.0)), sweep(T0 + 2 * DAY_S, (0.95, 50.0), (0.94, 30.0)),
            sweep(T - 40, (0.95, 100.0))]
    MIDS = [(T0 - 3 * DAY_S + 60 * k, 0.96 - 0.00001 * (k % 7)) for k in range(0, 15000, 3)]

    def test_a_decision_never_sees_a_sell_at_or_after_its_time(self):
        before = pb.history_as_of(market(self.PAST, mids=self.MIDS), self.T, FLOOR)
        after = pb.history_as_of(market(self.PAST + [sweep(self.T, (0.80, 9000.0))],
                                        mids=self.MIDS), self.T, FLOOR)
        self.assertEqual(before, after)

    def test_history_counts_the_same_from_the_mids_its_hits_read(self):
        m = market(self.PAST, mids=self.MIDS)
        hits = [h for h in m.hits if h.at < self.T]
        full = fill_cost.make_history(hits, [p for p in self.MIDS if p[0] < self.T], m.start,
                                      pb._utc(self.T), FLOOR)
        got = pb.history_as_of(m, self.T, FLOOR)
        self.assertEqual((got.events, got.days), (full.events, full.days))


class BookTest(unittest.TestCase):
    def test_our_orders_leave_the_book_where_they_show_in_it(self):
        sample = BookSample("c1", datetime.fromtimestamp(T0, timezone.utc), 0.96,
                            ((0.958, 300.0), (0.955, 2000.0)), ((0.962, 400.0), (0.97, 3000.0)),
                            (("FAV_BID", 0.955, 500.0), ("LONG_ASK", 0.042, 100.0),
                             ("LONG_BID", 0.03, 1000.0), ("FAV_ASK", 0.962, 400.0)))
        book = pb.others_book(sample, 0.001)
        self.assertEqual(book.bids, [(0.958, 200.0), (0.955, 1500.0)])
        self.assertEqual(book.asks, [(0.97, 2000.0)])


class SizesTest(unittest.TestCase):
    def test_a_burst_counts_the_shares_it_sold_over_a_cent_under_the_mid_before_it(self):
        record = {"sells": [[T0, [[0.955, 100.0], [0.949, 30.0], [0.94, 20.0]]],
                            [T0 + 7200, [[0.958, 40.0]]]],
                  "mids": [[T0 - 600, 0.96]]}
        self.assertEqual(pb.sweep_sizes(record, FLOOR), [50.0])
        self.assertEqual(pb.sweep_sizes(record, 0.97), [])

    def test_the_table_is_the_means_of_equal_probability_bins(self):
        self.assertEqual(pb.quantile_table([1.0, 2.0, 3.0, 4.0, 100.0, 200.0], points=3),
                         (1.5, 3.5, 150.0))
        self.assertEqual(pb.quantile_table([5.0, 1.0], points=3), (1.0, 5.0))


class FetchTest(unittest.TestCase):
    def test_a_record_cached_without_buys_is_fetched_again(self):
        with tempfile.TemporaryDirectory() as cache:
            path = os.path.join(cache, pb.CACHE_FILE)
            with open(path, "w") as out:
                out.write(json.dumps({"condition_id": "a", "sells": [], "buys": []}) + "\n")
                out.write(json.dumps({"condition_id": "b", "sells": []}) + "\n")
            listings = [mock.Mock(condition_id="a"), mock.Mock(condition_id="b")]
            fetched = {"condition_id": "b", "sells": [], "buys": [], "mids": [], "start": T0}
            with mock.patch.object(pb, "fetch_market", return_value=fetched) as fetch_market, \
                    mock.patch("builtins.print"), mock.patch.object(pb.time, "sleep"):
                pb.fetch(listings, cache, datetime.now(timezone.utc), None)
            self.assertEqual([c.args[1].condition_id for c in fetch_market.call_args_list], ["b"])
            with open(path) as lines:
                records = [json.loads(line) for line in lines]
            self.assertEqual([(r["condition_id"], "buys" in r) for r in records],
                             [("a", True), ("b", True)])


if __name__ == "__main__":
    unittest.main()
