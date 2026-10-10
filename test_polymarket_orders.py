"""Tests for polymarket.orders: the open-order listing. The client is a fake, so
nothing is signed or sent."""

import unittest
from datetime import datetime, timezone

from polymarket.orders import OpenOrder, Side, open_orders, parse_order

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


if __name__ == "__main__":
    unittest.main()
