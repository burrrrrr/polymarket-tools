import unittest
from datetime import datetime, timezone

from reward_screener import (
    Levels,
    Mode,
    RewardMarket,
    ScreenConfig,
    Skip,
    assess,
    created_mid,
    fav_bid_price,
    parse_gamma,
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
    capital=500, fav_target_score=0.45, max_lead=0.005, min_low_score=0.10, min_rate=0,
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


def fav_bid(levels, v=0.035, target_score=CONFIG.fav_target_score, max_lead=CONFIG.max_lead):
    return fav_bid_price(levels, v, 0.001, 20.0, target_score, max_lead)


class FavBidPriceTest(unittest.TestCase):
    def test_worked_example_takes_the_deepest_price_reaching_the_default_target(self):
        defaults = ScreenConfig()
        # 0.935 creates mid 0.9625 and scores 0.4576; 0.934 creates 0.962 and scores 0.4497.
        self.assertEqual(fav_bid_price(WORKED_BOOK, 0.085, 0.001, 20.0,
                                       defaults.fav_target_score, defaults.max_lead), 0.935)

    def test_max_lead_keeps_the_bid_from_jumping_the_spread(self):
        wide = Levels(bids=[(0.95, 100.0)], asks=[(0.985, 100.0)], tick=0.001)
        # Unbounded, the target is first reached at 0.962, 2.3c over the best bid.
        self.assertEqual(fav_bid(wide, max_lead=0.05), 0.962)
        self.assertEqual(fav_bid(wide), 0.955)
        plan = plan_quotes(MARKET, wide, CONFIG)
        self.assertEqual(plan.fav_bid_price, 0.955)
        self.assertAlmostEqual(plan.mid, 0.97)
        self.assertAlmostEqual(plan.fav_bid_score, score(0.035, 0.015))

    def test_unreachable_target_returns_the_highest_allowed_price(self):
        self.assertEqual(fav_bid(TIGHT_BOOK, target_score=0.9), 0.955)

    def test_tight_book_bid_under_the_best_bid_leaves_the_midpoint(self):
        # Under the best bid the midpoint stays 0.96: 0.949 is 1.1c off and scores
        # 0.4702, 0.948 is 1.2c off and scores 0.4318.
        self.assertEqual(fav_bid(TIGHT_BOOK), 0.949)
        plan = plan_quotes(MARKET, TIGHT_BOOK, CONFIG)
        self.assertEqual((plan.fav_bid_price, plan.mid), (0.949, 0.96))

    def test_bid_stays_a_tick_under_a_dust_ask(self):
        book = Levels(bids=[(0.95, 100.0)], asks=[(0.953, 5.0), (0.985, 100.0)])
        self.assertEqual(fav_bid(book), 0.952)

    def test_created_mid_moves_only_once_the_bid_passes_the_best_bid(self):
        self.assertAlmostEqual(created_mid(0.94, 0.95, 0.97), 0.96)
        self.assertAlmostEqual(created_mid(0.96, 0.95, 0.97), 0.965)


class LongshotBidTest(unittest.TestCase):
    """The favorite bid 0.855 x 584.79 scores 0.0271 against the 0.926 midpoint it
    creates, so the longshot bid must add 15.86 to Q_two."""

    def test_the_floor_covers_the_favorite_bid_within_the_naked_budget(self):
        plan = plan_quotes(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE, CONFIG)
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
        plan = plan_quotes(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                           CONFIG._replace(naked_budget=0.5))
        self.assertEqual((plan.low_price, plan.low_size), (0.016, 31.25))
        self.assertGreaterEqual(plan.low_score, CONFIG.min_low_score)
        self.assertEqual(plan.mode, Mode.BUDGET)
        self.assertAlmostEqual(plan.fav_bid_size,
                               plan.low_score * plan.low_size / plan.fav_bid_score, delta=0.01)

    def test_no_price_reaching_min_low_score_plans_nothing_even_if_one_covers(self):
        # No price under the 7.4c crossing limit scores 0.99, though 0.1c would cover.
        self.assertIsInstance(plan_quotes(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                                          CONFIG._replace(min_low_score=0.99)), Skip)

    def test_a_budget_under_the_reward_minimum_size_plans_nothing(self):
        # $0.30 buys 18.75 shares at 1.6c, under the 20-share minimum.
        self.assertIsInstance(plan_quotes(MISTRAL_MARKET, MISTRAL_BOOK_BID_GONE,
                                          CONFIG._replace(naked_budget=0.3)), Skip)


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


if __name__ == "__main__":
    unittest.main()
