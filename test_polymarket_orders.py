"""Tests for polymarket.orders: the open-order listing, and posting an order and
reading the CLOB's answer. The client is a fake, so nothing is signed or sent."""

import unittest
from datetime import datetime, timezone

import httpx
from py_clob_client_v2.exceptions import PolyApiException

from polymarket.orders import (GTC, GTD, TICKS, LimitOrder, OpenOrder, Posted, Rejected, Side,
                               Unanswered, on_grid, open_orders, parse_order, post,
                               read_response, terminal, text)

TOKEN = "71" * 38
MARKET = "0x" + "08" * 32


def entry(**changes):
    """An open order as the CLOB lists it: numbers and the expiration are strings."""
    return {"id": "0xorder1", "status": "LIVE", "market": MARKET, "asset_id": TOKEN,
            "side": "BUY", "original_size": "2000", "size_matched": "150.5", "price": "0.986",
            "outcome": "No", "expiration": "1791157113", "order_type": "GTD",
            "associate_trades": [], "created_at": 1791153480, **changes}


def without(key):
    listed = entry()
    del listed[key]
    return listed


class Clob:
    def __init__(self, listing):
        self.listing = listing

    def get_open_orders(self, params=None):
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing


class ParseOrderTests(unittest.TestCase):
    def test_an_order_is_read_with_its_numbers_side_and_utc_expiration(self):
        self.assertEqual(parse_order(entry()), OpenOrder(
            order_id="0xorder1", token_id=TOKEN, market=MARKET, outcome="No", side=Side.BUY,
            price=0.986, size=2000.0, size_matched=150.5,
            expiration=datetime(2026, 10, 4, 23, 38, 33, tzinfo=timezone.utc)))

    def test_a_good_til_cancelled_order_has_no_expiration(self):
        for sent in ("0", 0, "", None):
            with self.subTest(sent=sent):
                self.assertIsNone(parse_order(entry(expiration=sent)).expiration)

    def test_a_sell_is_a_sell_whatever_its_case(self):
        self.assertEqual(parse_order(entry(side="sell")).side, Side.SELL)

    def test_an_order_missing_what_a_restore_needs_is_refused_by_name(self):
        for key in ("id", "asset_id", "market", "side", "price", "original_size", "size_matched"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, key):
                    parse_order(without(key))

    def test_a_value_that_does_not_parse_is_refused(self):
        for changes in ({"side": "HOLD"}, {"price": "cheap"}, {"original_size": None},
                        {"expiration": "soon"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    parse_order(entry(**changes))


class RemainingTests(unittest.TestCase):
    def test_what_is_left_of_an_order_is_cut_to_whole_hundredths_of_a_share(self):
        for size, matched, left in ((2000, 150.5, 1849.5), (3000, 204.137143, 2795.86),
                                    (100, 40, 60.0), (0.3, 0.1, 0.2), (100, 100, 0.0),
                                    (100, 99.999, 0.0), (100, 100.5, 0.0)):
            with self.subTest(size=size, matched=matched):
                listed = parse_order(entry(original_size=str(size), size_matched=str(matched)))
                self.assertEqual(listed.remaining, left)


class OpenOrdersTests(unittest.TestCase):
    def test_every_listed_order_comes_back_in_the_listed_order(self):
        listing = [entry(id="0xa"), entry(id="0xb", side="SELL"), entry(id="0xc")]
        self.assertEqual([o.order_id for o in open_orders(Clob(listing))], ["0xa", "0xb", "0xc"])

    def test_an_account_with_no_orders_lists_none(self):
        self.assertEqual(open_orders(Clob([])), [])

    def test_a_failed_listing_raises_and_is_never_an_empty_list(self):
        with self.assertRaisesRegex(ConnectionError, "timed out"):
            open_orders(Clob(ConnectionError("timed out")))

    def test_an_answer_that_is_not_a_list_raises(self):
        for answer in (None, {"error": "unauthorized"}, "busy"):
            with self.subTest(answer=answer):
                with self.assertRaises(ValueError):
                    open_orders(Clob(answer))

    def test_one_unreadable_order_fails_the_listing_instead_of_being_left_out(self):
        with self.assertRaises(ValueError):
            open_orders(Clob([entry(id="0xa"), without("price"), entry(id="0xc")]))


ACCEPTED = {"success": True, "errorMsg": "", "orderID": "0xnew", "status": "live",
            "transactionsHashes": [], "tradeIDs": []}
NO_FUNDS = "not enough balance / allowance"
CROSSES = "invalid post-only order: order crosses book"
GEOBLOCKED = ("Trading restricted in your region, please refer to available regions - "
              "https://docs.polymarket.com/developers/CLOB/geoblock")


def refused(status, body):
    """What the SDK raises when the CLOB answers with this status and JSON body."""
    return PolyApiException(httpx.Response(status, json=body))


class Venue:
    """A client that answers each post with the next of `answers`, raising the
    ones that are exceptions, and records what it was asked to post."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.posts = []

    def post_order(self, order, order_type="GTC", post_only=False, defer_exec=False):
        self.posts.append((order, order_type, post_only))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class ReadResponseTests(unittest.TestCase):
    def test_an_accepted_order_is_posted_with_its_id(self):
        self.assertEqual(read_response(ACCEPTED), Posted("0xnew"))

    def test_a_refused_order_is_rejected_even_when_the_answer_carries_an_order_id(self):
        for answer in ({**ACCEPTED, "success": False, "errorMsg": NO_FUNDS},
                       {**ACCEPTED, "errorMsg": CROSSES},
                       {"orderID": "0xnew"},
                       {"success": False, "orderID": "0xnew"}):
            with self.subTest(answer=answer):
                self.assertIsInstance(read_response(answer), Rejected,
                                      "a refused order was read as placed")

    def test_a_rejection_carries_the_venues_reason(self):
        self.assertEqual(read_response({"success": False, "errorMsg": NO_FUNDS}),
                         Rejected(NO_FUNDS))
        self.assertEqual(read_response({"error": "order_version_mismatch"}),
                         Rejected("order_version_mismatch"))

    def test_success_with_no_order_id_is_unanswered_because_the_order_may_rest(self):
        for answer in ({"success": True, "errorMsg": ""}, {"success": True, "orderID": ""}):
            with self.subTest(answer=answer):
                self.assertIsInstance(read_response(answer), Unanswered)

    def test_an_answer_that_is_not_an_object_is_unanswered(self):
        for answer in (None, "", "<html>502 Bad Gateway</html>", [ACCEPTED], 200):
            with self.subTest(answer=answer):
                self.assertIsInstance(read_response(answer), Unanswered)

    def test_a_posted_and_a_rejected_with_the_same_text_are_not_equal(self):
        self.assertNotEqual(Posted("0xnew"), Rejected("0xnew"))
        self.assertNotEqual(Rejected("timed out"), Unanswered("timed out"))


class PostTests(unittest.TestCase):
    def test_an_order_is_posted_post_only_unless_the_caller_says_otherwise(self):
        venue = Venue(ACCEPTED, ACCEPTED)
        self.assertEqual(post(venue, "signed-1", GTD), Posted("0xnew"))
        self.assertEqual(post(venue, "signed-2", GTC, post_only=False), Posted("0xnew"))
        self.assertEqual(venue.posts, [("signed-1", "GTD", True), ("signed-2", "GTC", False)])

    def test_a_refusal_in_the_answer_is_rejected(self):
        answer = {"success": False, "errorMsg": CROSSES, "orderID": "", "status": ""}
        self.assertEqual(post(Venue(answer), "signed", GTC), Rejected(CROSSES))

    def test_a_refusal_by_status_is_rejected_with_the_venues_message(self):
        self.assertEqual(post(Venue(refused(400, {"error": NO_FUNDS})), "signed", GTC),
                         Rejected(f"HTTP 400: {NO_FUNDS}"))
        self.assertEqual(post(Venue(refused(403, {"error": GEOBLOCKED})), "signed", GTC),
                         Rejected(f"HTTP 403: {GEOBLOCKED}"))

    def test_a_post_with_no_usable_answer_is_unanswered_and_sent_once(self):
        lost = (PolyApiException(error_msg="Request exception!"),
                httpx.ReadTimeout("timed out"),
                ConnectionError("reset by peer"),
                refused(502, {"error": "bad gateway"}),
                PolyApiException(httpx.Response(503, text="<html>unavailable</html>")),
                "<html>unavailable</html>",
                None)
        for answer in lost:
            with self.subTest(answer=answer):
                venue = Venue(answer)
                self.assertIsInstance(post(venue, "signed", GTC), Unanswered)
                self.assertEqual(len(venue.posts), 1, "an order that may rest was sent again")

    def test_an_unanswered_post_says_what_happened(self):
        timeout, gateway = httpx.ReadTimeout("timed out"), refused(502, {"error": "bad gateway"})
        self.assertIn("timed out", post(Venue(timeout), "signed", GTC).detail)
        self.assertIn("502", post(Venue(gateway), "signed", GTC).detail)

    def test_post_never_raises(self):
        for error in (ValueError("post_only is not supported for FOK/FAK orders"),
                      KeyError("orderID"), RuntimeError("anything")):
            with self.subTest(error=error):
                self.assertIsInstance(post(Venue(error), "signed", GTC), Unanswered)


class TerminalTests(unittest.TestCase):
    def test_a_refusal_that_no_retry_can_change_is_terminal(self):
        for reason in (f"HTTP 403: {GEOBLOCKED}", "HTTP 451: Unavailable For Legal Reasons",
                       NO_FUNDS, f"HTTP 400: {NO_FUNDS}", CROSSES, CROSSES.upper()):
            with self.subTest(reason=reason):
                self.assertTrue(terminal(Rejected(reason)))

    def test_a_refusal_that_may_pass_a_moment_later_is_not(self):
        for reason in ("order is invalid. Price (0.999) breaks minimum tick size rule: 0.01",
                       "the market is not yet ready to process new orders",
                       "HTTP 429: Too Many Requests", "order_version_mismatch", ""):
            with self.subTest(reason=reason):
                self.assertFalse(terminal(Rejected(reason)))


class LimitOrderTests(unittest.TestCase):
    def test_an_order_that_expires_is_good_til_date_and_one_that_does_not_til_cancelled(self):
        self.assertEqual(LimitOrder(TOKEN, Side.BUY, 0.999, 100.0, 0).order_type, GTC)
        self.assertEqual(LimitOrder(TOKEN, Side.BUY, 0.999, 100.0, 3600).order_type, GTD)

    def test_the_ticks_the_clob_uses_and_the_grid_of_each(self):
        self.assertEqual(TICKS, ("0.1", "0.01", "0.001", "0.0001"))
        self.assertTrue(on_grid(0.999, 0.001))
        self.assertFalse(on_grid(0.999, 0.01))
        self.assertEqual(text(0.001), "0.001")


if __name__ == "__main__":
    unittest.main()
