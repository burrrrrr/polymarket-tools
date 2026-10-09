import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

import farm_db
import reward_screener
import fill_cost
from fill_cost import DISTANCES, FillCurve, History, Hit
from reward_screener import (
    Levels,
    Mode,
    Outlook,
    RewardMarket,
    ScreenConfig,
    Skip,
    assess,
    created_mid,
    fav_bid_candidates,
    fav_ceiling,
    longshot_bid,
    parse_gamma,
    parse_hits,
    plan_at,
    plan_quotes,
    resolution_signal,
    round_size_down,
    score,
)

MARKET = RewardMarket(
    condition_id="0xc0nd",
    slug="test-market",
    question="Test?",
    end_date=datetime(2027, 1, 1, tzinfo=timezone.utc),
    neg_risk=False,
    tick=0.001,
    fav_token_id="fav-token",
    fav_outcome="No",
    rate_per_day=100.0,
    v=0.035,
    min_size=20.0,
)

CONFIG = ScreenConfig(
    min_fav=0.90, expiry_buffer_days=7, min_window_days=30, horizon_cap_days=90,
    capital=500, max_lead=0.005, min_low_score=0.10, min_rate=0,
)

# The 0.955 bid is dust below the 20-share reward minimum, so the best bid is
# 0.95 and the reward midpoint (0.95 + 0.97) / 2 = 0.96.
TIGHT_BOOK = Levels(bids=[(0.955, 5.0), (0.95, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)
# A live favorite book with a 5.6c gap between the best bid and the best ask.
WORKED_BOOK = Levels(
    bids=[(0.934, 88.8), (0.922, 21.0), (0.574, 85.0), (0.573, 40.0)],
    asks=[(0.990, 240.99), (0.994, 111.0), (0.997, 50.0), (0.998, 200.0), (0.999, 729.1)],
    tick=0.001,
)
# The live Mistral favorite book: another maker's 487-share bid at 0.93 holds the
# midpoint up over bids too small or too deep to matter.
MISTRAL_MARKET = MARKET._replace(v=0.085)
MISTRAL_BOOK = Levels(
    bids=[(0.93, 487.0), (0.852, 18.64), (0.851, 10.0), (0.85, 20.0)],
    asks=[(0.996, 6.75), (0.997, 150.0), (0.998, 250.0), (0.999, 573.9)],
    tick=0.001,
)
# The Mistral book once the 0.93 bid left: the 0.1c longshot bid scores only 0.020.
MISTRAL_BOOK_BID_GONE = MISTRAL_BOOK._replace(bids=MISTRAL_BOOK.bids[1:])


class FavCeilingTest(unittest.TestCase):
    def test_worked_example_may_lead_the_best_bid_by_max_lead(self):
        self.assertEqual(fav_ceiling(WORKED_BOOK, 0.001, 20.0, ScreenConfig().max_lead), 0.939)

    def test_max_lead_keeps_the_bid_from_jumping_the_spread(self):
        wide = Levels(bids=[(0.95, 100.0)], asks=[(0.985, 100.0)], tick=0.001)
        # Unbounded, only post-only stops it, a tick under the ask.
        self.assertEqual(fav_ceiling(wide, 0.001, 20.0, 0.05), 0.984)
        self.assertEqual(fav_ceiling(wide, 0.001, 20.0, CONFIG.max_lead), 0.955)
        plan = plan_at(MARKET, wide, CONFIG, 0.955)
        self.assertAlmostEqual(plan.mid, 0.97)
        self.assertAlmostEqual(plan.fav_bid_score, score(0.035, 0.015))

    def test_tight_book_bid_under_the_best_bid_leaves_the_midpoint(self):
        plan = plan_at(MARKET, TIGHT_BOOK, CONFIG, 0.949)
        self.assertEqual((plan.fav_bid_price, plan.mid), (0.949, 0.96))

    def test_bid_stays_a_tick_under_a_dust_ask(self):
        book = Levels(bids=[(0.95, 100.0)], asks=[(0.953, 5.0), (0.985, 100.0)])
        self.assertEqual(fav_ceiling(book, 0.001, 20.0, CONFIG.max_lead), 0.952)

    def test_created_mid_moves_only_once_the_bid_passes_the_best_bid(self):
        self.assertAlmostEqual(created_mid(0.94, 0.95, 0.97), 0.96)
        self.assertAlmostEqual(created_mid(0.96, 0.95, 0.97), 0.965)


class LongshotBidTest(unittest.TestCase):
    """The favorite bid 0.855 x 584.79 scores 0.0271 against the 0.926 midpoint it
    creates, so the longshot bid must add 15.86 to Q_two."""

    def test_the_floor_covers_the_favorite_bid_within_the_naked_budget(self):
        plan = plan_at(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE, CONFIG, 0.855)
        # 15.86 / 0.0199 = 795.97 shares at 0.1c cost $0.80. The cheapest price
        # scoring 0.10 is 1.6c, a 98.4c ask in front of the 99.6c best ask.
        self.assertEqual((plan.low_price, plan.low_size), (0.001, 795.97))
        self.assertEqual(plan.mode, Mode.FLOOR)
        self.assertEqual((plan.fav_bid_price, plan.fav_bid_size), (0.855, 584.79))
        self.assertGreaterEqual(plan.low_score * plan.low_size,
                                plan.fav_bid_score * plan.fav_bid_size)
        self.assertLessEqual(plan.low_price * plan.low_size, CONFIG.naked_budget)

    def test_a_budget_short_at_every_price_falls_back_and_trims_the_favorite_bid(self):
        # Every cover costs at least the floor's $0.80. $0.50 buys 9.97 of
        # score at 0.1c (500 shares scoring 0.0199) and 3.15 at the 1.6c
        # ceiling (31.25 scoring 0.101), so the bid sits at the floor.
        plan = plan_at(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                       CONFIG._replace(naked_budget=0.5), 0.855)
        self.assertEqual((plan.low_price, plan.low_size), (0.001, 500.0))
        self.assertEqual(plan.mode, Mode.FLOOR)
        self.assertLess(plan.low_score * plan.low_size, plan.fav_bid_score * 584.79)
        self.assertAlmostEqual(plan.fav_bid_size,
                               plan.low_score * plan.low_size / plan.fav_bid_score, delta=0.01)

    def test_no_price_reaching_min_low_score_plans_nothing_even_if_one_covers(self):
        # No price under the 7.4c crossing limit scores 0.99, though 0.1c would cover.
        self.assertIsInstance(plan_at(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                                      CONFIG._replace(min_low_score=0.99), 0.855), Skip)

    def test_a_budget_under_the_reward_minimum_size_plans_nothing(self):
        # $0.019 buys 19 shares at 0.1c, the most of any price, under the
        # 20-share minimum.
        self.assertIsInstance(plan_at(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                                      CONFIG._replace(naked_budget=0.019), 0.855), Skip)

    def test_a_short_budget_buys_the_most_score_at_the_floor_near_the_band_edge(self):
        # The live Pfizer book: favorite midpoint 0.9395, v 0.065. 0.1c scores
        # 0.00716, so $10 buys 10000 shares and 71.6 of score; the 1.7c ceiling
        # scores 0.109 and buys 64.4. Neither covers the 72 needed.
        book = Levels(bids=[(0.934, 100.0)], asks=[(0.945, 100.0)], tick=0.001)
        bid = longshot_bid(book, 0.9395, 0.065, 0.001, 20.0, 72.0, 10.0, 0.1)
        self.assertEqual((bid.price, bid.size), (0.001, round_size_down(10.0 / 0.001)))
        self.assertAlmostEqual(bid.score * bid.size, 71.6, delta=0.05)
        self.assertAlmostEqual(bid.score, score(0.065, 0.0595))

    def test_a_short_budget_sits_at_the_ceiling_when_it_buys_the_most_score(self):
        # Favorite midpoint 0.96, v 0.035: nothing under 0.6c scores, and the
        # score $25 buys rises with price up to the 1.7c ceiling (172.9 at
        # 1470.58 shares versus 154.3 at 1.6c).
        book = Levels(bids=[(0.95, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)
        bid = longshot_bid(book, 0.96, 0.035, 0.001, 20.0, 247.63, 25.0, 0.1)
        self.assertEqual((bid.price, bid.size), (0.017, 1470.58))
        self.assertAlmostEqual(bid.score, score(0.035, 0.023))


# TIGHT_BOOK with a bid behind the best one, so it is not fragile.
DEEP_BOOK = TIGHT_BOOK._replace(bids=[(0.955, 5.0), (0.95, 1000.0), (0.895, 1000.0)])
SCAN_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
# A scan's fill prior: the default rates, fit from 40 markets.
PRIOR = fill_cost.DEFAULT_PRIOR._replace(markets=40, at=SCAN_NOW)


def curve(*rates, exit_per_day=0.0):
    """A FillCurve with these rates from the first of DISTANCES, the last one
    held to the end. By default a filled share never exits."""
    return FillCurve(rates + rates[-1:] * (len(DISTANCES) - len(rates)), True, exit_per_day)


NO_FILLS = curve(0.0)


def outlook(fills=NO_FILLS, years=0.25, rate=100.0, hurdle_rate=0.40, stuck_share=0.25):
    return Outlook(rate, years, fills, hurdle_rate, stuck_share)


class FavBidCandidatesTest(unittest.TestCase):
    def test_candidates_run_from_the_ceiling_to_the_last_scoring_price(self):
        # Ceiling 0.95 + 0.005. Under the best bid the midpoint stays 0.96, so
        # 0.926 is the deepest price scoring; 0.925 sits on the band edge.
        prices = fav_bid_candidates(TIGHT_BOOK, 0.035, 0.001, 20.0, 0.005)
        self.assertEqual((prices[0], prices[-1], len(prices)), (0.955, 0.926, 30))

    def test_a_cap_under_the_ceiling_starts_the_walk_there(self):
        prices = fav_bid_candidates(TIGHT_BOOK, 0.035, 0.001, 20.0, 0.005, cap=0.95)
        self.assertEqual(prices[:2], [0.95, 0.949])

    def test_a_one_sided_book_has_no_candidates(self):
        one_sided = TIGHT_BOOK._replace(asks=[])
        self.assertEqual(fav_bid_candidates(one_sided, 0.035, 0.001, 20.0, 0.005), [])


class ValueQuoteTest(unittest.TestCase):
    # Others score ((3.5 - 1) / 3.5)^2 x 100 = 51.02 on each side of the 0.96 midpoint.
    BOOK = Levels(bids=[(0.95, 100.0)], asks=[(0.97, 100.0)], tick=0.001)
    THEIR_Q = (0.025 / 0.035) ** 2 * 100

    def test_net_is_the_pool_share_less_fills_times_the_discounted_loss(self):
        # 2c under the 0.96 midpoint a sweep reaches the bid 0.1 times a day and
        # fills all 200 shares. A year at 40%: 0.98 / 1.4 = 0.70.
        fills = curve(0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1, 0.0)
        value = reward_screener.value_quote(self.BOOK, 0.96, 0.035, 20.0, self.THEIR_Q,
                                            (0.94, 200.0), 190.0, 0.98, outlook(fills, years=1.0))
        self.assertAlmostEqual(value.reward_per_day, 50.0)
        self.assertAlmostEqual(value.fills_per_day, 20.0)
        self.assertAlmostEqual(value.loss_per_share, 0.24)
        self.assertAlmostEqual(value.net_per_day, 50.0 - 20.0 * 0.24)
        self.assertAlmostEqual(value.net_apr, (50.0 - 4.8) * 365 / 190.0)

    def test_the_bid_fills_at_its_distance_below_the_mid_it_is_given(self):
        # 1.25c under the midpoint, halfway between the 1c and 1.5c rates.
        fills = curve(0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1, 0.0)
        value = reward_screener.value_quote(self.BOOK, 0.96, 0.035, 20.0, self.THEIR_Q,
                                            (0.9475, 100.0), 95.0, 0.98, outlook(fills))
        self.assertAlmostEqual(value.fills_per_day, 100 * 0.175)
        moved = reward_screener.value_quote(self.BOOK, 0.9625, 0.035, 20.0, self.THEIR_Q,
                                            (0.9475, 100.0), 95.0, 0.98, outlook(fills))
        self.assertAlmostEqual(moved.fills_per_day, 100 * 0.15)

    def test_a_fill_that_sells_back_fast_costs_less_than_one_held_to_resolution(self):
        # A year at 40%: held, 0.98 / 1.4 = 0.70 today. Selling back at once,
        # all but the stuck quarter are worth 0.98.
        rates = (0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1, 0.0)
        quote = (self.BOOK, 0.96, 0.035, 20.0, self.THEIR_Q, (0.94, 200.0), 190.0, 0.98)
        held = reward_screener.value_quote(*quote, outlook(curve(*rates), years=1.0))
        fast = reward_screener.value_quote(
            *quote, outlook(curve(*rates, exit_per_day=1e6), years=1.0))
        self.assertEqual(fast.fills_per_day, held.fills_per_day)
        self.assertAlmostEqual(held.loss_per_share, 0.94 - 0.98 / 1.4)
        self.assertAlmostEqual(fast.loss_per_share, 0.94 - 0.98 * (0.75 + 0.25 / 1.4), places=6)
        self.assertGreater(fast.net_per_day, held.net_per_day)

    def test_no_favorite_bid_earns_the_reward_with_no_fills(self):
        value = reward_screener.value_quote(self.BOOK, 0.96, 0.035, 20.0, self.THEIR_Q, None,
                                            5.0, 0.98, outlook(curve(1e6)))
        self.assertEqual(value.fills_per_day, 0.0)
        self.assertAlmostEqual(value.net_per_day, 50.0)


class PlanQuotesTest(unittest.TestCase):
    def test_with_no_fills_the_bid_sits_at_the_ceiling(self):
        plan, value = plan_quotes(MARKET, TIGHT_BOOK, CONFIG, outlook(), 0.96)
        self.assertEqual(plan.fav_bid_price, 0.955)
        self.assertEqual(value.fills_per_day, 0.0)

    def test_sweeps_through_the_top_of_the_book_push_the_bid_below_where_they_reach(self):
        # A sweep reaches 1c under the 0.96 midpoint every day and never 1.5c;
        # a fill there loses 6c, so the bid steps down to 1.5c under or more.
        swept = curve(1.0, 1.0, 1.0, 1.0, 1.0, 0.0)
        plan, value = plan_quotes(MARKET, TIGHT_BOOK, CONFIG, outlook(swept), 0.96)
        self.assertLessEqual(plan.fav_bid_price, 0.945)
        self.assertEqual(value.size, plan.fav_bid_size)
        self.assertEqual(value.fills_per_day, 0.0)

    def test_fills_costing_more_than_any_bid_earns_plan_nothing(self):
        # A sweep through every bid each day, two years out at a 1,000% hurdle.
        self.assertEqual(plan_quotes(MARKET, TIGHT_BOOK, CONFIG,
                                     outlook(curve(1.0), years=2.0, hurdle_rate=10.0), 0.96),
                         Skip("fills cost more than the rewards"))


class AssessTest(unittest.TestCase):
    def test_reward_stays_gross_and_the_rank_is_the_net_yield(self):
        a = assess(MARKET, DEEP_BOOK, CONFIG, SCAN_NOW, PRIOR)
        self.assertEqual(a.fill_source, "prior")
        self.assertIsNone(a.history)
        self.assertGreater(a.fill_cost_usd, 0)
        self.assertAlmostEqual(a.est_daily_usd - a.fill_cost_usd, a.net_daily_usd)
        self.assertAlmostEqual(a.apr, a.est_daily_usd / a.capital_usd * 365)
        self.assertAlmostEqual(a.rank_score,
                               a.net_daily_usd / a.capital_usd * min(a.days_left, 90))
        default = assess(MARKET, DEEP_BOOK, CONFIG, SCAN_NOW, fill_cost.DEFAULT_PRIOR)
        self.assertEqual(default.fill_source, "default")

    def test_the_stuck_share_setting_prices_the_fills(self):
        stuck = assess(MARKET, DEEP_BOOK, CONFIG._replace(stuck_share=1.0), SCAN_NOW, PRIOR)
        exiting = assess(MARKET, DEEP_BOOK, CONFIG._replace(stuck_share=0.0), SCAN_NOW, PRIOR)
        self.assertGreater(stuck.fills_per_day, 0)
        self.assertLess(stuck.net_daily_usd, exiting.net_daily_usd)

    def test_a_history_prices_the_fills_and_is_stored(self):
        history = History((30, 25, 20, 15, 10, 6, 3, 1, 0, 0, 0, 0), 60.0, SCAN_NOW, 0.90,
                          8, 40.0)
        a = assess(MARKET, DEEP_BOOK, CONFIG, SCAN_NOW, PRIOR, history)
        self.assertEqual(a.fill_source, "history")
        self.assertEqual(fill_cost.history_from_json(a.history), history)
        fills = fill_cost.fill_curve(history, PRIOR, CONFIG.min_fav)
        self.assertAlmostEqual(a.fills_per_day,
                               a.plan.fav_bid_size * fills.per_day(a.mid - a.plan.fav_bid_price))
        prior_only = assess(MARKET, DEEP_BOOK, CONFIG, SCAN_NOW, PRIOR)
        self.assertNotAlmostEqual(a.fills_per_day, prior_only.fills_per_day)


def listing_item(cid, rate):
    return {"condition_id": cid, "market_slug": cid, "question": f"{cid}?", "active": True,
            "accepting_orders": True, "enable_order_book": True, "minimum_tick_size": 0.001,
            "end_date_iso": "2028-10-01T00:00:00Z", "rewards": {"max_spread": 3.5, "min_size": 20},
            "tokens": [{"token_id": f"fav-{cid}", "price": 0.96, "outcome": "No"},
                       {"token_id": f"long-{cid}", "price": 0.04, "outcome": "Yes"}]}


class ExcludedCategoryTest(unittest.TestCase):
    def screen(self, tags, config=ScreenConfig()):
        item = {**listing_item("0x1", 100.0), "tags": tags}
        return reward_screener.screen_listing(item, {"0x1": 100.0}, config, SCAN_NOW)

    def test_sports_crypto_and_esports_are_skipped_by_default(self):
        for tags in (["Sports", "NBA"], ["Crypto", "Bitcoin"], ["Esports", "lol"],
                     ["football", "NFL (All)", "NFL"]):
            with self.subTest(tags=tags):
                self.assertEqual(self.screen(tags), Skip("excluded category"))

    def test_other_and_missing_tags_pass(self):
        for tags in (["Politics", "Elections"], [], None):
            with self.subTest(tags=tags):
                self.assertIsInstance(self.screen(tags), RewardMarket)

    def test_only_the_configured_categories_are_skipped(self):
        config = ScreenConfig(excluded_categories=("politics",))
        self.assertIsInstance(self.screen(["Sports"], config), RewardMarket)
        self.assertEqual(self.screen(["POLITICS"], config), Skip("excluded category"))
        self.assertIsInstance(self.screen(["Crypto"], ScreenConfig(excluded_categories=())),
                              RewardMarket)

    def test_the_cli_takes_a_comma_list_or_none(self):
        parse = lambda *argv: reward_screener.build_parser().parse_args(["scan", *argv]).exclude
        self.assertEqual(parse(), ("sports", "esports", "crypto"))
        self.assertEqual(parse("--exclude", "Politics, tech"), ("politics", "tech"))
        self.assertEqual(parse("--exclude", "none"), ())
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parse("--exclude", "sports,golf")


class RunScanTest(unittest.TestCase):
    RATES = {"0x1": 300.0, "0x2": 200.0, "0x3": 100.0}

    def scan(self, histories, prior=fill_cost.DEFAULT_PRIOR, tape_markets=2, sample=0,
             books={}, rejected=frozenset(), tags={}, gamma={}):
        asked = []

        def fake_histories(http, markets, now, floor):
            asked.append([m.condition_id for m in markets])
            return {cid: h for cid, h in histories.items() if cid in asked[-1]}

        with mock.patch.multiple(
                reward_screener,
                fetch_paginated=lambda http, path: [{**listing_item(c, r), "tags": tags.get(c)}
                                                    for c, r in self.RATES.items()],
                fetch_total_rates=lambda http: dict(self.RATES),
                fetch_levels=lambda http, ids: {t: books.get(t, DEEP_BOOK) for t in ids},
                fetch_gamma=lambda http, ids: dict(gamma),
                fetch_histories=fake_histories,
                PRIOR_SAMPLE=sample), mock.patch("builtins.print"):
            result = reward_screener.run_scan(CONFIG._replace(tape_markets=tape_markets),
                                              SCAN_NOW, prior, frozenset(rejected))
        return result, asked

    # A sweep through every bid each hour for 60 days, none of them recovered,
    # held two years at 40%.
    HEAVY = History((1440,) * len(DISTANCES), 60.0, SCAN_NOW, 0.90, 0, 43200.0)
    BUSY = History((30, 25, 20, 15, 10, 6, 3, 1, 0, 0, 0, 0), 60.0, SCAN_NOW, 0.90, 8, 40.0)
    QUIET = History((0,) * len(DISTANCES), 60.0, SCAN_NOW, 0.90, 0, 0.0)

    def test_only_the_top_markets_read_a_history_and_one_whose_fills_cost_too_much_drops(self):
        result, asked = self.scan({"0x1": self.HEAVY})
        self.assertEqual(asked, [["0x1", "0x2"]])
        self.assertEqual([a.market.condition_id for a in result.assessments], ["0x2", "0x3"])
        self.assertEqual([a.fill_source for a in result.assessments], ["default", "default"])
        self.assertEqual(result.funnel.assess_skips, {"fills cost more than the rewards": 1})
        self.assertEqual(result.funnel.assessed, 2)
        self.assertIsNone(result.prior)

    def test_listing_tags_and_the_gamma_event_land_in_the_stored_market(self):
        gamma = {"0x1": parse_gamma({"slug": "0x1", "events": [
                     {"slug": "2027-french-election", "title": "2027 French election",
                      "closed": False}]}),
                 "0x2": parse_gamma({"slug": "0x2"})}
        result, _ = self.scan({}, tags={"0x1": ["Politics", "French Election"]}, gamma=gamma)
        stored = {a.market.condition_id: json.loads(reward_screener.scan_row(a).metrics)["market"]
                  for a in result.assessments}
        topic = {cid: (m["tags"], m["event_slug"], m["event_title"]) for cid, m in stored.items()}
        self.assertEqual(topic, {
            "0x1": (["Politics", "French Election"], "2027-french-election",
                    "2027 French election"),
            "0x2": ([], None, None),
            "0x3": ([], None, None),
        })

    def test_a_rejected_market_gives_its_top_slot_to_the_next_but_is_still_assessed(self):
        result, asked = self.scan({}, rejected={"0x1"})
        self.assertEqual(asked, [["0x2", "0x3"]])
        self.assertIn("0x1", [a.market.condition_id for a in result.assessments])

    def test_a_rejected_market_can_still_be_sampled(self):
        result, asked = self.scan({}, tape_markets=0, sample=3, rejected={"0x1"})
        self.assertIn("0x1", asked[0])

    def test_the_top_markets_histories_alone_fit_no_prior(self):
        with mock.patch.object(fill_cost, "PRIOR_MIN_MARKETS", 1):
            result, asked = self.scan({"0x1": self.QUIET, "0x2": self.QUIET})
        self.assertEqual(asked, [["0x1", "0x2"]])
        self.assertIsNone(result.prior)

    def test_the_prior_is_fit_from_the_random_sample_and_each_sampled_market_its_own(self):
        with mock.patch.object(fill_cost, "PRIOR_MIN_MARKETS", 1):
            result, asked = self.scan(
                {"0x1": self.BUSY, "0x2": self.QUIET, "0x3": self.QUIET},
                tape_markets=1, sample=3)
        self.assertEqual((asked[0][0], sorted(asked[0])), ("0x1", ["0x1", "0x2", "0x3"]))
        self.assertEqual(result.prior.markets, 3)
        self.assertEqual(result.prior.rates,
                         tuple(e / 180.0 for e in self.BUSY.events))
        self.assertEqual({a.market.condition_id: a.fill_source for a in result.assessments},
                         {"0x1": "history", "0x2": "history", "0x3": "history"})

    def test_the_sample_draws_markets_the_stored_prior_dropped_but_not_unpriced_ones(self):
        steep = fill_cost.Prior((50.0,) * len(DISTANCES), (1000.0,) * len(DISTANCES), 1,
                                SCAN_NOW, 0.90, 0.0, 1000.0)
        cheap = Levels(bids=[(0.80, 1000.0)], asks=[(0.82, 1000.0)], tick=0.001)
        with mock.patch.object(fill_cost, "PRIOR_MIN_MARKETS", 2):
            result, asked = self.scan({c: self.QUIET for c in self.RATES}, steep,
                                      sample=3, books={"fav-0x3": cheap})
        self.assertEqual(sorted(asked[0]), ["0x1", "0x2"])
        self.assertEqual(result.prior.markets, 2)
        self.assertEqual([a.market.condition_id for a in result.assessments], ["0x1", "0x2"])

    def test_every_market_is_valued_again_on_the_prior_the_sample_fit(self):
        with mock.patch.object(fill_cost, "PRIOR_MIN_MARKETS", 2):
            result, asked = self.scan({c: self.QUIET for c in self.RATES}, PRIOR,
                                      tape_markets=0, sample=2)
        self.assertEqual((result.prior.markets, result.prior.at), (2, SCAN_NOW))
        [unread_id] = set(self.RATES) - set(asked[0])
        unread = {a.market.condition_id: a for a in result.assessments}[unread_id]
        self.assertEqual(unread.fill_source, "prior")
        fills = fill_cost.fill_curve(None, result.prior, CONFIG.min_fav)
        self.assertAlmostEqual(unread.fills_per_day, unread.plan.fav_bid_size
                               * fills.per_day(unread.mid - unread.plan.fav_bid_price))


class FragileMidTest(unittest.TestCase):
    NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def test_one_bid_holding_the_favorite_above_min_fav_is_skipped(self):
        # The live DeepSeek book: a lone 20-share bid at 79c under asks at 99.7c.
        lone = Levels(bids=[(0.943, 20.0), (0.79, 20.0)], asks=[(0.985, 100.0)], tick=0.001)
        self.assertEqual(assess(MARKET, lone, CONFIG, self.NOW, PRIOR), Skip("fragile midpoint"))

    def test_last_qualifying_bid_counts_as_fragile(self):
        self.assertEqual(assess(MARKET, TIGHT_BOOK._replace(bids=[(0.955, 5.0), (0.95, 1000.0)]),
                                CONFIG, self.NOW, PRIOR), Skip("fragile midpoint"))

    def test_dust_does_not_count_as_the_best_bid(self):
        # Pulling the 0.95 level leaves dust at 0.955 and a qualifying bid at 0.895:
        # (0.895 + 0.97) / 2 = 0.9325 is 2.75c under the 0.96 midpoint. Measured
        # from a midpoint that counted the dust, 0.9625, the fall would reach 3c.
        deep = TIGHT_BOOK._replace(bids=[(0.955, 5.0), (0.95, 1000.0), (0.895, 1000.0)])
        self.assertNotIsInstance(assess(MARKET, deep, CONFIG, self.NOW, PRIOR), Skip)

    def test_a_fall_of_fragile_drop_when_the_best_bid_leaves_is_fragile(self):
        # The Mistral book before its 0.93 bid left: pulling it drops the midpoint
        # from (0.93 + 0.997) / 2 = 0.9635 to (0.85 + 0.997) / 2 = 0.9235, still above
        # min_fav but 4c down: FAV_BID would follow from 0.935 (score 0.404) to 0.855 (0.027).
        self.assertEqual(assess(MISTRAL_MARKET, MISTRAL_BOOK, CONFIG, self.NOW, PRIOR),
                         Skip("fragile midpoint"))
        self.assertNotIsInstance(assess(MISTRAL_MARKET, MISTRAL_BOOK,
                                        CONFIG._replace(fragile_drop=0.05), self.NOW, PRIOR), Skip)

    def test_a_smaller_fall_when_the_best_bid_leaves_is_not_fragile(self):
        # A 0.91 bid behind it holds the midpoint at (0.91 + 0.997) / 2 = 0.9535, 1c down.
        backed = MISTRAL_BOOK._replace(bids=[(0.93, 487.0), (0.91, 100.0), (0.85, 20.0)])
        self.assertNotIsInstance(assess(MISTRAL_MARKET, backed, CONFIG, self.NOW, PRIOR), Skip)


class ResolutionSignalTest(unittest.TestCase):
    """Decided markets stay open on the CLOB until on-chain resolution."""

    def gamma(self, **fields):
        return parse_gamma({"slug": "m", "events": [{"slug": "e", "closed": False}],
                            "umaResolutionStatuses": "[]", **fields})

    def test_open_market_has_no_signal(self):
        self.assertIsNone(resolution_signal(self.gamma()))

    def test_closed_parent_event_means_decided(self):
        info = self.gamma(events=[{"slug": "wnba-2026-mvp", "closed": True}])
        self.assertEqual(resolution_signal(info), "event closed")

    def test_proposed_resolution_sent_as_a_json_string(self):
        info = self.gamma(umaResolutionStatuses='["proposed", "disputed"]')
        self.assertEqual(resolution_signal(info), "resolution proposed")

    def test_proposed_resolution_sent_as_a_list(self):
        self.assertEqual(resolution_signal(self.gamma(umaResolutionStatuses=["proposed"])),
                         "resolution proposed")

    def test_missing_gamma_record_has_no_signal(self):
        self.assertIsNone(resolution_signal(None))
        self.assertIsNone(resolution_signal(parse_gamma({"slug": "m"})))


HISTORY_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
FAV_TOKEN, LONG_TOKEN = "fav-token", "long-token"


def priced(side, asset, price, tx="0xt", wallet="0xw", size=10.0, seconds_ago=60):
    return {"transactionHash": tx, "proxyWallet": wallet, "side": side, "asset": asset,
            "size": size, "price": price, "timestamp": int(HISTORY_NOW.timestamp()) - seconds_ago}


class ParseHitsTest(unittest.TestCase):
    AT = HISTORY_NOW.timestamp() - 60

    def test_a_hit_reaches_as_deep_as_the_makers_it_filled(self):
        taker = [priced("SELL", FAV_TOKEN, 0.95, wallet="0xtaker")]
        makers = [priced("BUY", FAV_TOKEN, 0.94, wallet="0xm1"),
                  priced("SELL", LONG_TOKEN, 0.07, wallet="0xm2"),
                  priced("BUY", FAV_TOKEN, 0.90, tx="0xother", wallet="0xm3")]
        hits = parse_hits(taker, taker + makers, FAV_TOKEN)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0].deep, 0.93)

    def test_buying_the_other_outcome_hits_favorite_bids_at_one_minus_its_price(self):
        taker = [priced("BUY", LONG_TOKEN, 0.06), priced("BUY", FAV_TOKEN, 0.97, tx="0xlift"),
                 priced("SELL", LONG_TOKEN, 0.05, tx="0xask")]
        hits = parse_hits(taker, taker, FAV_TOKEN)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0].deep, 0.94)
        self.assertEqual(hits[0].at, self.AT)

    def test_maker_rows_are_what_is_left_after_each_taker_row_is_removed_once(self):
        # Two taker orders in one transaction buy the favorite at 0.90; the
        # full listing repeats them and adds one maker with the same key.
        lift = priced("BUY", FAV_TOKEN, 0.90)
        taker = [priced("SELL", FAV_TOKEN, 0.95), lift, lift]
        maker = [priced("BUY", FAV_TOKEN, 0.94, wallet="0xm")]
        self.assertAlmostEqual(parse_hits(taker, taker + maker, FAV_TOKEN)[0].deep, 0.94)
        self.assertAlmostEqual(parse_hits(taker, taker + maker + [lift], FAV_TOKEN)[0].deep, 0.90)

    def test_malformed_rows_are_dropped(self):
        good = priced("SELL", FAV_TOKEN, 0.95)
        bad = [
            {**good, "transactionHash": None}, {**good, "asset": ""},
            {**good, "side": "HOLD", "asset": LONG_TOKEN},
            {**good, "size": 0}, {**good, "size": "lots"}, {**good, "price": 1.0},
            {**good, "price": None}, {**good, "timestamp": None}, "row", None,
        ]
        self.assertEqual(parse_hits(bad, bad, FAV_TOKEN), [])
        deep_but_broken = {**priced("BUY", FAV_TOKEN, 0.80, wallet="0xm"), "size": -5}
        hits = parse_hits([good] + bad, [good, deep_but_broken] + bad, FAV_TOKEN)
        self.assertEqual(hits, [Hit(self.AT, 0.95)])


DAY_S = 86400


def hourly_mids(price, days=70):
    return [(int(HISTORY_NOW.timestamp()) - 3600 * n, price) for n in range(days * 24)]


def history_api(all_pages, taker_pages=(), mids=(), statuses=()):
    """An httpx client serving /trades pages by takerOnly and offset, and the
    mids inside each /prices-history interval, after statuses."""
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) <= len(statuses) and statuses[len(calls) - 1] == 429:
            return httpx.Response(429, headers={"retry-after": "2"})
        params = request.url.params
        if request.url.path == "/prices-history":
            lo, hi = int(params["startTs"]), int(params["endTs"])
            return httpx.Response(200, json={"history": [
                {"t": t, "p": p} for t, p in mids
                if not isinstance(t, (int, float)) or lo <= t <= hi]})
        pages = taker_pages if params["takerOnly"] == "true" else all_pages
        index = int(params["offset"]) // 1000
        return httpx.Response(200, json=pages[index] if index < len(pages) else [])

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


def listed(calls, path):
    return [c.url.params for c in calls if c.url.path == path]


class FetchHistoryTest(unittest.TestCase):
    def test_a_history_reads_all_rows_then_taker_rows_then_mids(self):
        taker = [priced("SELL", FAV_TOKEN, 0.95, tx="0xa", wallet="0xt", seconds_ago=DAY_S)]
        maker = [priced("BUY", FAV_TOKEN, 0.93, tx="0xa", wallet="0xm", seconds_ago=DAY_S)]
        http, calls = history_api([taker + maker], [taker], hourly_mids(0.96))
        history = reward_screener.fetch_history(http, "0xc0nd", FAV_TOKEN, HISTORY_NOW, 0.90)
        self.assertEqual([p["takerOnly"] for p in listed(calls, "/trades")], ["false", "true"])
        self.assertEqual(calls[-1].url.path, "/prices-history")
        reach = [history.events[fill_cost.DISTANCES.index(d)] for d in (0.02, 0.03)]
        self.assertEqual(reach, [1, 0])
        self.assertAlmostEqual(history.days, 60.0)
        self.assertEqual((history.read_at, history.floor), (HISTORY_NOW, 0.90))

    def test_mids_are_read_in_chunks_of_at_most_fourteen_days(self):
        http, calls = history_api([[]], [[]], hourly_mids(0.96))
        reward_screener.fetch_history(http, "0xc0nd", FAV_TOKEN, HISTORY_NOW, 0.90)
        chunks = [(int(p["startTs"]), int(p["endTs"])) for p in listed(calls, "/prices-history")]
        self.assertEqual(len(chunks), 5)
        self.assertTrue(all(0 < hi - lo <= 14 * DAY_S for lo, hi in chunks), chunks)
        self.assertEqual([lo for lo, _ in chunks[1:]], [hi for _, hi in chunks[:-1]])
        self.assertEqual(chunks[-1][1], int(HISTORY_NOW.timestamp()))
        self.assertEqual(listed(calls, "/prices-history")[0]["fidelity"], "1")

    def test_mids_repeated_across_chunks_or_malformed_are_read_once_or_dropped(self):
        end = int(HISTORY_NOW.timestamp())
        start = end - 20 * DAY_S
        mids = [(start + 14 * DAY_S, 0.95), (start + 7, "x"), (None, 0.95), (start + 9, 0.94)]
        http, _ = history_api([[]], mids=mids)
        self.assertEqual(reward_screener.fetch_mids(http, FAV_TOKEN, start, end),
                         [(start + 9, 0.94), (start + 14 * DAY_S, 0.95)])

    def test_a_listing_cut_by_the_row_cap_is_complete_only_from_its_oldest_row(self):
        # 2,000 rows reaching back 10 days: the taker rows are then read only
        # back to there, and the history covers those 10 days.
        first = [priced("BUY", FAV_TOKEN, 0.97, seconds_ago=432 * n) for n in range(1000)]
        second = [priced("BUY", FAV_TOKEN, 0.97, seconds_ago=432000 + 432 * n)
                  for n in range(1, 1001)]
        taker = [priced("BUY", FAV_TOKEN, 0.97, seconds_ago=3600 * n) for n in range(1000)]
        http, calls = history_api([first, second], [taker, taker], hourly_mids(0.96))
        history = reward_screener.fetch_history(http, "0xc0nd", FAV_TOKEN, HISTORY_NOW, 0.90)
        trades = listed(calls, "/trades")
        self.assertEqual([(p["takerOnly"], p["offset"]) for p in trades],
                         [("false", "0"), ("false", "1000"), ("true", "0")])
        self.assertAlmostEqual(history.days, 10.0)
        oldest = HISTORY_NOW.timestamp() - 10 * DAY_S
        self.assertEqual(int(listed(calls, "/prices-history")[0]["startTs"]), int(oldest - 60 - 3600))

    def test_a_full_page_reaching_past_the_cutoff_is_complete_from_the_cutoff(self):
        page = [priced("BUY", FAV_TOKEN, 0.97, seconds_ago=3600 * n) for n in range(1000)]
        http, calls = history_api([page, page])
        cutoff = HISTORY_NOW.timestamp() - 30 * DAY_S
        rows, complete_from = reward_screener.fetch_trades(http, "0xc0nd", False, cutoff)
        self.assertEqual((len(calls), len(rows), complete_from), (1, 1000, cutoff))

    def test_a_short_page_is_the_whole_listing(self):
        http, calls = history_api([], [[priced("SELL", FAV_TOKEN, 0.95)]])
        cutoff = HISTORY_NOW.timestamp() - 30 * DAY_S
        rows, complete_from = reward_screener.fetch_trades(http, "0xc0nd", True, cutoff)
        self.assertEqual((len(calls), len(rows), complete_from), (1, 1, cutoff))
        self.assertEqual(calls[0].url.params["takerOnly"], "true")

    def test_a_rate_limited_read_waits_and_retries(self):
        http, calls = history_api([[priced("SELL", FAV_TOKEN, 0.95)]], statuses=(429,))
        with mock.patch("time.sleep") as sleep:
            rows, _ = reward_screener.fetch_trades(http, "0xc0nd", False, 0.0)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c.args for c in sleep.call_args_list], [(2.0,)])
        self.assertEqual(len(rows), 1)

    def test_a_failed_market_is_left_out_of_the_histories(self):
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        with mock.patch("builtins.print") as printed:
            histories = reward_screener.fetch_histories(http, [MARKET], HISTORY_NOW, 0.90)
        self.assertEqual(histories, {})
        self.assertIn("test-market", printed.call_args.args[0])


REWARD_ROWS = [(f"0x{n:04d}", n % 7 + 1) for n in range(2000)]


def offset_cursor(offset):
    return base64.b64encode(str(offset).encode()).decode()


NATIVE_CONFIG = [{"asset_address": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",
                  "start_date": "2026-10-01", "end_date": "2500-12-31", "rate_per_day": 1}]


def rewards_api(rows, gone=lambda offset: 0, next_cursor=None, sponsored_only=()):
    """A /rewards/markets/current over rows [(condition_id, rate)], paged as the
    CLOB pages it: 500 rows from a base64 row offset, LTE= after a short page.
    gone(offset) is how many leading rows have left the listing by the time the
    page at that offset is read. A cursor that is not an offset ends the listing.
    Each row has a native reward config, except the sponsored_only ones."""
    offsets = []

    def handle(request):
        try:
            offset = int(base64.b64decode(request.url.params["next_cursor"]))
        except ValueError:
            return httpx.Response(200, json={"data": [], "next_cursor": "LTE="})
        offsets.append(offset)
        page = rows[gone(offset):][offset:offset + 500]
        cursor = offset_cursor(offset + 500) if len(page) == 500 else "LTE="
        return httpx.Response(200, json={
            "data": [{"condition_id": c, "total_daily_rate": r,
                      "rewards_config": [] if c in sponsored_only else NATIVE_CONFIG}
                     for c, r in page],
            "next_cursor": next_cursor or cursor, "limit": 500, "count": len(page)})

    return httpx.Client(transport=httpx.MockTransport(handle)), offsets


class FetchTotalRatesTest(unittest.TestCase):
    def test_markets_leaving_mid_read_skip_no_market_still_listed(self):
        http, _ = rewards_api(REWARD_ROWS, gone=lambda offset: 50 if offset >= 800 else 0)
        rates = reward_screener.fetch_total_rates(http)
        self.assertEqual([c for c, r in REWARD_ROWS[50:] if rates.get(c) != r], [])
        self.assertLessEqual(set(rates), {c for c, _ in REWARD_ROWS})

    def test_a_market_paid_only_by_sponsors_is_left_out(self):
        sponsored = {REWARD_ROWS[3][0], REWARD_ROWS[1200][0]}
        http, _ = rewards_api(REWARD_ROWS, sponsored_only=sponsored)
        rates = reward_screener.fetch_total_rates(http)
        self.assertEqual(sorted(sponsored & rates.keys()), [])
        self.assertEqual([c for c, r in REWARD_ROWS if c not in sponsored and rates.get(c) != r], [])

    def test_a_listing_that_keeps_shifting_past_the_overlap_raises_after_rereading(self):
        http, offsets = rewards_api(REWARD_ROWS, gone=lambda offset: 150 if offset >= 800 else 0)
        with self.assertRaises(RuntimeError):
            reward_screener.fetch_total_rates(http)
        self.assertEqual(offsets.count(0), reward_screener.REWARDS_ATTEMPTS)

    def test_a_listing_not_sorted_by_condition_id_raises(self):
        http, _ = rewards_api(REWARD_ROWS[::-1])
        with self.assertRaises(ValueError):
            reward_screener.fetch_total_rates(http)

    def test_a_cursor_that_is_not_a_row_offset_raises(self):
        http, _ = rewards_api(REWARD_ROWS, next_cursor="aWQ6MTI4NDk2OA==")
        with self.assertRaises(ValueError):
            reward_screener.fetch_total_rates(http)


class LoadPriorTest(unittest.TestCase):
    def setUp(self):
        self.conn = farm_db.open_db(":memory:")
        self.addCleanup(self.conn.close)

    def store(self, text):
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (farm_db.SCAN_FILL_PRIOR_KEY, text, "2026-10-04T12:00:00+00:00"))

    def test_no_stored_prior_reads_as_the_default(self):
        self.assertEqual(reward_screener.load_prior(self.conn), fill_cost.DEFAULT_PRIOR)

    def test_a_stored_prior_is_read_back(self):
        prior = fill_cost.DEFAULT_PRIOR._replace(markets=40, at=HISTORY_NOW)
        self.store(json.dumps(fill_cost.prior_to_json(prior)))
        self.assertEqual(reward_screener.load_prior(self.conn), prior)

    def test_a_malformed_stored_prior_reads_as_the_default(self):
        for text in ("{not json", json.dumps({"v": 1}), json.dumps([1, 2])):
            with self.subTest(text=text):
                self.conn.execute("DELETE FROM settings")
                self.store(text)
                self.assertEqual(reward_screener.load_prior(self.conn), fill_cost.DEFAULT_PRIOR)


if __name__ == "__main__":
    unittest.main()
