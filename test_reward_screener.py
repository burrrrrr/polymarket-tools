import unittest
from datetime import datetime, timezone

from reward_screener import (
    Levels,
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
        # Pulling the 0.95 level leaves dust at 0.955 and a qualifying bid at 0.88:
        # (0.88 + 0.97) / 2 = 0.925 stays above min_fav.
        deep = TIGHT_BOOK._replace(bids=[(0.955, 5.0), (0.95, 1000.0), (0.88, 1000.0)])
        self.assertNotIsInstance(assess(MARKET, deep, CONFIG, self.NOW), Skip)


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
