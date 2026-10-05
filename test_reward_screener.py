import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

import reward_screener
import fill_cost
from fill_cost import Hit, Sweep, Tape
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
    fetch_tape,
    fill_params,
    parse_gamma,
    parse_hits,
    plan_at,
    plan_quotes,
    resolution_signal,
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
        # Every cover costs at least the floor's $0.80.
        plan = plan_at(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                       CONFIG._replace(naked_budget=0.5), 0.855)
        self.assertEqual((plan.low_price, plan.low_size), (0.016, 31.25))
        self.assertGreaterEqual(plan.low_score, CONFIG.min_low_score)
        self.assertEqual(plan.mode, Mode.BUDGET)
        self.assertAlmostEqual(plan.fav_bid_size,
                               plan.low_score * plan.low_size / plan.fav_bid_score, delta=0.01)

    def test_no_price_reaching_min_low_score_plans_nothing_even_if_one_covers(self):
        # No price under the 7.4c crossing limit scores 0.99, though 0.1c would cover.
        self.assertIsInstance(plan_at(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                                      CONFIG._replace(min_low_score=0.99), 0.855), Skip)

    def test_a_budget_under_the_reward_minimum_size_plans_nothing(self):
        # $0.30 buys 18.75 shares at 1.6c, under the 20-share minimum.
        self.assertIsInstance(plan_at(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                                      CONFIG._replace(naked_budget=0.3), 0.855), Skip)


# TIGHT_BOOK with a bid behind the best one, so it is not fragile.
DEEP_BOOK = TIGHT_BOOK._replace(bids=[(0.955, 5.0), (0.95, 1000.0), (0.895, 1000.0)])
SCAN_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
NO_PRIOR = fill_params(CONFIG._replace(sweep_prior_weight=0.0))


def outlook(sweeps=(), params=NO_PRIOR, years=0.25, rate=100.0):
    return Outlook(rate, years, Tape(tuple(Sweep(n, x) for n, x in enumerate(sweeps)), 30.0),
                   params)


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
        # Behind 100 shares, haircut to 50: a 300-share sweep fills all 200
        # shares, once in 10 days. A year at 40%: 0.98 / 1.4 = 0.70.
        params = fill_cost.FillParams(0.40, 0.5, 30.0, 0.0)
        view = Outlook(100.0, 1.0, Tape((Sweep(0, 300.0),), 10.0), params)
        value = reward_screener.value_quote(self.BOOK, 0.96, 0.035, 20.0, self.THEIR_Q,
                                            (0.94, 200.0), 190.0, 0.98, view)
        self.assertAlmostEqual(value.reward_per_day, 50.0)
        self.assertEqual(value.ahead, 100.0)
        self.assertAlmostEqual(value.fills_per_day, 20.0)
        self.assertAlmostEqual(value.loss_per_share, 0.24)
        self.assertAlmostEqual(value.net_per_day, 50.0 - 20.0 * 0.24)
        self.assertAlmostEqual(value.net_apr, (50.0 - 4.8) * 365 / 190.0)

    def test_no_favorite_bid_earns_the_reward_with_no_fills(self):
        value = reward_screener.value_quote(self.BOOK, 0.96, 0.035, 20.0, self.THEIR_Q, None,
                                            5.0, 0.98, outlook(sweeps=(1e6,)))
        self.assertEqual(value.fills_per_day, 0.0)
        self.assertAlmostEqual(value.net_per_day, 50.0)


class PlanQuotesTest(unittest.TestCase):
    def test_with_no_fills_the_bid_sits_at_the_ceiling(self):
        plan, value = plan_quotes(MARKET, TIGHT_BOOK, CONFIG, outlook(), 0.96)
        self.assertEqual(plan.fav_bid_price, 0.955)
        self.assertEqual(value.fills_per_day, 0.0)

    def test_sweeps_that_clear_the_top_of_the_book_push_the_bid_behind_it(self):
        # 510-share sweeps fill all of a bid behind the 5-share dust at 0.955,
        # but only 7.5 shares of one behind the 1,000 bid at 0.95.
        plan, value = plan_quotes(MARKET, TIGHT_BOOK, CONFIG, outlook(sweeps=[510.0] * 60),
                                  0.96)
        self.assertLess(plan.fav_bid_price, 0.95)
        self.assertEqual(value.size, plan.fav_bid_size)
        self.assertAlmostEqual(value.fills_per_day, 60 * 7.5 / 30)

    def test_fills_costing_more_than_any_bid_earns_plan_nothing(self):
        # 30 full clears a month, two years out at a 1,000% hurdle.
        params = fill_params(CONFIG._replace(hurdle_rate=10.0, sweep_prior_weight=30.0))
        self.assertEqual(plan_quotes(MARKET, TIGHT_BOOK, CONFIG, outlook(params=params, years=2.0),
                                     0.96),
                         Skip("fills cost more than the rewards"))


class AssessTest(unittest.TestCase):
    def test_reward_stays_gross_and_the_rank_is_the_net_yield(self):
        a = assess(MARKET, DEEP_BOOK, CONFIG, SCAN_NOW)
        self.assertEqual(a.fill_source, "prior")
        self.assertIsNone(a.tape)
        self.assertGreater(a.fill_cost_usd, 0)
        self.assertAlmostEqual(a.est_daily_usd - a.fill_cost_usd, a.net_daily_usd)
        self.assertAlmostEqual(a.apr, a.est_daily_usd / a.capital_usd * 365)
        self.assertAlmostEqual(a.rank_score,
                               a.net_daily_usd / a.capital_usd * min(a.days_left, 90))

    def test_a_tape_replaces_the_prior_and_is_stored(self):
        tape = Tape((Sweep(SCAN_NOW.timestamp() - 60, 510.0),), 30.0, fetched_at=SCAN_NOW)
        a = assess(MARKET, DEEP_BOOK, CONFIG, SCAN_NOW, tape)
        self.assertEqual(a.fill_source, "tape")
        self.assertEqual(fill_cost.tape_from_json(a.tape), tape)


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

    def scan(self, tapes):
        asked = []

        def fake_tapes(http, markets, now, lookback_days):
            asked.append([m.condition_id for m in markets])
            return tapes

        with mock.patch.multiple(
                reward_screener,
                fetch_paginated=lambda http, path: [listing_item(c, r) for c, r in self.RATES.items()],
                fetch_total_rates=lambda http: dict(self.RATES),
                fetch_levels=lambda http, ids: {t: DEEP_BOOK for t in ids},
                fetch_gamma=lambda http, ids: {},
                fetch_tapes=fake_tapes), mock.patch("builtins.print"):
            result = reward_screener.run_scan(CONFIG._replace(tape_markets=2), SCAN_NOW)
        return result, asked

    def test_only_the_top_markets_read_a_tape_and_one_whose_fills_cost_too_much_drops(self):
        # 100 sweeps a month that clear any bid, held two years at 40%.
        heavy = Tape(tuple(Sweep(SCAN_NOW.timestamp() - n, 1e6) for n in range(100)), 30.0,
                     fetched_at=SCAN_NOW)
        result, asked = self.scan({"0x1": heavy})
        self.assertEqual(asked, [["0x1", "0x2"]])
        self.assertEqual([a.market.condition_id for a in result.assessments], ["0x2", "0x3"])
        self.assertEqual([a.fill_source for a in result.assessments], ["prior", "prior"])
        self.assertEqual(result.funnel.assess_skips, {"fills cost more than the rewards": 1})
        self.assertEqual(result.funnel.assessed, 2)


class FragileMidTest(unittest.TestCase):
    NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def test_one_bid_holding_the_favorite_above_min_fav_is_skipped(self):
        # The live DeepSeek book: a lone 20-share bid at 79c under asks at 99.7c.
        lone = Levels(bids=[(0.943, 20.0), (0.79, 20.0)], asks=[(0.985, 100.0)], tick=0.001)
        self.assertEqual(assess(MARKET, lone, CONFIG, self.NOW), Skip("fragile midpoint"))

    def test_last_qualifying_bid_counts_as_fragile(self):
        self.assertEqual(assess(MARKET, TIGHT_BOOK._replace(bids=[(0.955, 5.0), (0.95, 1000.0)]),
                                CONFIG, self.NOW), Skip("fragile midpoint"))

    def test_dust_does_not_count_as_the_best_bid(self):
        # Pulling the 0.95 level leaves dust at 0.955 and a qualifying bid at 0.895:
        # (0.895 + 0.97) / 2 = 0.9325 is 2.75c under the 0.96 midpoint. Measured
        # from a midpoint that counted the dust, 0.9625, the fall would reach 3c.
        deep = TIGHT_BOOK._replace(bids=[(0.955, 5.0), (0.95, 1000.0), (0.895, 1000.0)])
        self.assertNotIsInstance(assess(MARKET, deep, CONFIG, self.NOW), Skip)

    def test_a_fall_of_fragile_drop_when_the_best_bid_leaves_is_fragile(self):
        # The Mistral book before its 0.93 bid left: pulling it drops the midpoint
        # from (0.93 + 0.997) / 2 = 0.9635 to (0.85 + 0.997) / 2 = 0.9235, still above
        # min_fav but 4c down: FAV_BID would follow from 0.935 (score 0.404) to 0.855 (0.027).
        self.assertEqual(assess(MISTRAL_MARKET, MISTRAL_BOOK, CONFIG, self.NOW),
                         Skip("fragile midpoint"))
        self.assertNotIsInstance(assess(MISTRAL_MARKET, MISTRAL_BOOK,
                                        CONFIG._replace(fragile_drop=0.05), self.NOW), Skip)

    def test_a_smaller_fall_when_the_best_bid_leaves_is_not_fragile(self):
        # A 0.91 bid behind it holds the midpoint at (0.91 + 0.997) / 2 = 0.9535, 1c down.
        backed = MISTRAL_BOOK._replace(bids=[(0.93, 487.0), (0.91, 100.0), (0.85, 20.0)])
        self.assertNotIsInstance(assess(MISTRAL_MARKET, backed, CONFIG, self.NOW), Skip)


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


TAPE_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
FAV_TOKEN, LONG_TOKEN = "fav-token", "long-token"


def trade(side, asset, size, seconds_ago):
    return {"side": side, "asset": asset, "size": size,
            "timestamp": int(TAPE_NOW.timestamp()) - seconds_ago, "price": 0.5}


class ParseHitsTest(unittest.TestCase):
    def test_only_trades_that_sell_into_favorite_bids_are_hits(self):
        rows = [
            trade("SELL", FAV_TOKEN, 100, 10),
            trade("BUY", LONG_TOKEN, 40, 20),
            trade("BUY", FAV_TOKEN, 999, 30),
            trade("SELL", LONG_TOKEN, 999, 40),
        ]
        hits, _ = parse_hits(rows, FAV_TOKEN)
        now = TAPE_NOW.timestamp()
        self.assertEqual(hits, [Hit(now - 10, 100.0), Hit(now - 20, 40.0)])

    def test_a_malformed_row_is_no_hit_but_still_dates_the_history(self):
        rows = [trade("SELL", FAV_TOKEN, 100, 10), trade("SELL", FAV_TOKEN, None, 500)]
        hits, oldest = parse_hits(rows, FAV_TOKEN)
        self.assertEqual(len(hits), 1)
        self.assertEqual(oldest, TAPE_NOW.timestamp() - 500)


def trades_api(pages, statuses=()):
    """An httpx client whose /trades answers pages[offset // 1000], after statuses."""
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) <= len(statuses) and statuses[len(calls) - 1] == 429:
            return httpx.Response(429, headers={"retry-after": "2"})
        offset = int(request.url.params["offset"])
        page = pages[offset // 1000] if offset // 1000 < len(pages) else []
        return httpx.Response(200, json=page)

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


class FetchTapeTest(unittest.TestCase):
    def test_a_history_cut_by_the_row_cap_spans_only_the_rows_it_covers(self):
        # 1,000 rows over the last 5 days, then 800 reaching back 8 days.
        first = [trade("SELL", FAV_TOKEN, 1, 432 * n) for n in range(1000)]
        second = [trade("BUY", FAV_TOKEN, 1, 432000 + 324 * n) for n in range(1, 801)]
        http, calls = trades_api([first, second])
        tape = fetch_tape(http, "0xc0nd", FAV_TOKEN, TAPE_NOW, 30.0)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c.url.params["offset"] for c in calls], ["0", "1000"])
        self.assertEqual(calls[0].url.params["takerOnly"], "true")
        self.assertTrue(tape.capped)
        self.assertAlmostEqual(tape.span_days, 8.0)
        self.assertAlmostEqual(sum(s.shares for s in tape.sweeps), 1000.0)

    def test_a_full_page_reaching_past_the_lookback_reads_no_further(self):
        page = [trade("SELL", FAV_TOKEN, 1, 3600 * n) for n in range(1000)]
        http, calls = trades_api([page, page])
        tape = fetch_tape(http, "0xc0nd", FAV_TOKEN, TAPE_NOW, 30.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual((tape.span_days, tape.capped), (30.0, False))
        self.assertEqual(len(tape.sweeps), 721)

    def test_a_short_page_is_the_whole_history(self):
        http, calls = trades_api([[trade("SELL", FAV_TOKEN, 5, 60)]])
        tape = fetch_tape(http, "0xc0nd", FAV_TOKEN, TAPE_NOW, 30.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual((tape.span_days, tape.capped, tape.observed), (30.0, False, True))

    def test_a_rate_limited_read_waits_and_retries(self):
        http, calls = trades_api([[trade("SELL", FAV_TOKEN, 5, 60)]], statuses=(429,))
        with mock.patch("time.sleep") as sleep:
            tape = fetch_tape(http, "0xc0nd", FAV_TOKEN, TAPE_NOW, 30.0)
        self.assertEqual(len(calls), 2)
        self.assertEqual([c.args for c in sleep.call_args_list], [(2.0,)])
        self.assertEqual(len(tape.sweeps), 1)

    def test_a_failed_market_is_left_out_of_the_tapes(self):
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        with mock.patch("builtins.print"):
            tapes = reward_screener.fetch_tapes(http, [MARKET], TAPE_NOW, 30.0)
        self.assertEqual(tapes, {})


if __name__ == "__main__":
    unittest.main()
