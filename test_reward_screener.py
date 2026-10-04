import unittest
from datetime import datetime, timezone

from reward_screener import (
    Levels,
    RewardMarket,
    ScreenConfig,
    Skip,
    fav_bid_price,
    plan_quotes,
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
    capital=500, bid_offset=0.01, min_low_score=0.10, min_rate=0,
)

# The 0.97 bid is dust below the 20-share reward minimum, so the reward
# midpoint is (0.86 + 0.985) / 2 = 0.9225.
WIDE_BOOK = Levels(bids=[(0.97, 5.0), (0.86, 100.0)], asks=[(0.985, 100.0)], tick=0.001)
TIGHT_BOOK = Levels(bids=[(0.955, 5.0), (0.95, 1000.0)], asks=[(0.97, 1000.0)], tick=0.001)


class FavBidPriceTest(unittest.TestCase):
    def test_wide_book_bids_two_offsets_under_the_best_ask(self):
        self.assertEqual(fav_bid_price(0.9225, WIDE_BOOK, 0.001, 0.01, 20.0), 0.965)

    def test_wide_book_bid_scores_one_offset_from_the_midpoint_it_creates(self):
        plan = plan_quotes(MARKET, WIDE_BOOK, 0.9225, CONFIG)
        self.assertNotIsInstance(plan, Skip)
        self.assertEqual(plan.fav_bid_price, 0.965)
        self.assertAlmostEqual(plan.mid, 0.975)
        self.assertAlmostEqual(plan.fav_bid_score, score(0.035, 0.01))

    def test_tight_book_keeps_offset_under_the_midpoint(self):
        self.assertEqual(fav_bid_price(0.96, TIGHT_BOOK, 0.001, 0.01, 20.0), 0.95)
        plan = plan_quotes(MARKET, TIGHT_BOOK, 0.96, CONFIG)
        self.assertEqual((plan.fav_bid_price, plan.mid), (0.95, 0.96))

    def test_bid_that_would_cross_is_skipped(self):
        book = Levels(bids=[(0.86, 100.0)], asks=[(0.96, 5.0), (0.985, 100.0)])
        self.assertEqual(fav_bid_price(0.9225, book, 0.001, 0.01, 20.0),
                         Skip("favorite bid would cross"))


if __name__ == "__main__":
    unittest.main()
