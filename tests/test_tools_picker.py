import unittest
from datetime import datetime, timezone

from polymarket.books import Levels
from polymarket.gamma import Market
from polymarket.orders import Side
from tools import picker
from tools.db import Guard

END = datetime(2027, 1, 1, tzinfo=timezone.utc)
GUARD = Guard(0.99, 0.005, 5.0)


def market(question, outcomes=("Yes", "No"), tokens=("111", "222"), volume=1000.0, tick=0.01,
           closed=False, accepting=True, event="Fed decision in 2026", slug=None):
    return Market("0x" + question[:4].encode().hex(), question, slug or question.lower()
                  .replace(" ", "-"), event, tuple(outcomes), tuple(tokens), END, volume, tick,
                  closed, accepting, 5.0)


def levels(bid=None, ask=None, tick=0.01):
    return Levels([(bid, 10.0)] if bid is not None else [], [(ask, 10.0)] if ask is not None
                  else [], tick)


class ScoreTests(unittest.TestCase):
    def test_a_title_holding_the_whole_query_scores_above_a_partial_match(self):
        whole = picker.score("fed rate", "Will the Fed cut the rate in March?")
        partial = picker.score("fed rate", "Will the Fed meet in March?")
        none = picker.score("fed rate", "Will it rain?")
        self.assertGreater(whole, partial)
        self.assertGreater(partial, none)
        self.assertEqual(picker.score("", "anything"), 0.0)

    def test_the_shortest_title_holding_the_query_scores_highest(self):
        self.assertGreater(picker.score("fed", "Fed cut?"), picker.score("fed", "Will the Fed cut?"))

    def test_words_out_of_order_still_match(self):
        self.assertGreater(picker.score("rate fed", "Fed rate cut?"), 0.5)


class CandidateTests(unittest.TestCase):
    def test_each_outcome_of_a_market_is_a_candidate_with_its_token_and_the_best_score(self):
        found = picker.candidates("fed", [market("Will the Fed cut?", slug="fed-cut-2026")])
        self.assertEqual([(c.outcome, c.token_id, c.label) for c in found],
                         [("Yes", "111", "Will the Fed cut? -> Yes"),
                          ("No", "222", "Will the Fed cut? -> No")])
        self.assertEqual(found[0].score, found[1].score)
        self.assertGreaterEqual(found[0].score, picker.score("fed", "Will the Fed cut?"))

    def test_an_outcome_with_no_token_is_left_out(self):
        found = picker.candidates("x", [market("x", outcomes=("Yes", "No"), tokens=("111",))])
        self.assertEqual([c.outcome for c in found], ["Yes"])


class StatusTests(unittest.TestCase):
    def status(self, m=None, book=levels(0.99, 0.995), side=Side.BUY, guard=GUARD,
               target="0.001"):
        (candidate, _) = picker.candidates("q", [m or market("q")])
        return picker.status(candidate, book, side, guard, target)

    def test_a_market_at_the_guards_price_on_a_coarser_tick_is_ready(self):
        self.assertEqual(self.status(), picker.Verdict(True, "ready"))
        self.assertEqual(self.status(side=Side.SELL, book=levels(0.5, 0.99)),
                         picker.Verdict(True, "ready"))

    def test_a_closed_or_non_accepting_market_is_not(self):
        self.assertEqual(self.status(market("q", closed=True)).note, "closed")
        self.assertEqual(self.status(market("q", accepting=False)).note, "not accepting orders")

    def test_a_tick_already_at_the_target_is_not_and_the_book_tick_wins(self):
        self.assertEqual(self.status(market("q", tick=0.001), book=None).note,
                         "tick 0.001 already")
        self.assertEqual(self.status(book=levels(0.99, 0.995, tick=0.001)).note,
                         "tick 0.001 already")
        self.assertEqual(self.status(market("q", tick=0.001), book=levels(0.99, 0.995, 0.01)).ready,
                         True)

    def test_without_a_guard_any_open_coarse_market_is_ready(self):
        self.assertEqual(self.status(book=None, guard=None), picker.Verdict(True, "ready, no guard"))

    def test_the_guards_side_must_be_quoted_near_its_price(self):
        self.assertEqual(self.status(book=None).note, "no book")
        self.assertEqual(self.status(book=levels(None, 0.995)).note, "no bid")
        self.assertEqual(self.status(book=levels(0.97, 0.995)).note, "bid at 97.0c")
        self.assertEqual(self.status(side=Side.SELL, book=levels(0.99, None)).note, "no ask")


class RankTests(unittest.TestCase):
    def test_ready_first_then_relevance_then_volume(self):
        markets = [market("Will the Fed cut?", volume=10.0),
                   market("Fed cut?", volume=5.0, tokens=("333", "444")),
                   market("Fed cut again?", volume=50.0, tokens=("555", "666"))]
        found = picker.candidates("fed cut", markets)
        books = {"111": levels(0.99, 0.995), "333": levels(0.5, 0.6), "555": levels(0.99, 0.995),
                 "666": levels(0.5, 0.6)}
        ranked = picker.rank(found, books, Side.BUY, GUARD, "0.001")
        self.assertEqual([(r.candidate.token_id, r.verdict.ready) for r in ranked][:3],
                         [("555", True), ("111", True), ("333", False)])
        self.assertEqual([r.candidate.token_id for r in ranked][3:], ["444", "666", "222"])


if __name__ == "__main__":
    unittest.main()
