import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from analysis import trades_dump
from farm import farm_db
from polymarket.client import ConfigError
from test_polymarket_trades import ME, THEM, made, maker_order, trade

KEY = "0x" + "ab" * 32
SIGNER = "0x" + "5a" * 20
FARMER_ORDER = "0x" + "5e" * 32
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
TAKEN = trade()
MADE = made(maker_order(order_id=FARMER_ORDER, maker_address=ME.upper(), side="BUY",
                        matched_amount="200", price="0.97"),
            maker_order(order_id="0xtheirs"), id="trade-2", status="MATCHED")
BY_HAND = made(maker_order(order_id="0xbyhand", maker_address=ME), id="trade-4")
ODD = made(maker_order(), id="trade-3")


class ReportTests(unittest.TestCase):
    def report(self, with_address, without, show=3, farmer=None):
        return "\n".join(trades_dump.report(with_address, without, ME, show, farmer))

    def test_it_counts_each_listing_and_the_trades_only_one_of_them_holds(self):
        out = self.report([MADE], [MADE, TAKEN])
        self.assertIn("With maker_address: 1 trades (MAKER/MATCHED 1)", out)
        self.assertIn("With no address:    2 trades (MAKER/MATCHED 1, TAKER/CONFIRMED 1)", out)
        self.assertIn("In one listing only: 0 with maker_address, 1 with no address", out)

    def test_each_shown_trade_is_followed_by_the_fills_read_from_it(self):
        out = self.report([MADE, TAKEN], [])
        self.assertIn("Read as fills: 2 fills from 2 trades; 0 trades unreadable", out)
        self.assertIn("MAKER trades read: 1, each filling 1 to 1 of the account's orders", out)
        maker, taker = out.split("TAKER trade 1 of 1 shown")
        self.assertIn('"matched_amount": "200"', maker)
        self.assertIn("-> maker fill: BUY 200 shares @ 0.97", maker)
        self.assertIn("order 0x5e5e5e5e..., pending", maker)
        self.assertIn("-> taker fill: BUY 120.5 shares @ 0.97", taker)

    def test_maker_fills_are_matched_to_the_orders_farm_db_names_as_the_farmers(self):
        out = self.report([MADE, BY_HAND, TAKEN], [], farmer={FARMER_ORDER, "0xunfilled"})
        self.assertIn("Maker fills on the farmer's orders: 1 of 2 (farm.db names 2 order ids)",
                      out)
        self.assertIn("order 0x5e5e5e5e..., pending, the farmer's order", out)
        self.assertIn("order 0xbyhand, confirmed, not a farmer order", out)
        self.assertIn("order 0xtaking, confirmed\n", out + "\n")
        self.assertIn("not checked, farm.db could not be read", self.report([MADE], []))

    def test_a_trade_that_does_not_read_as_a_fill_says_why(self):
        out = self.report([ODD], [])
        self.assertIn("Read as fills: 0 fills from 0 trades; 1 trades unreadable", out)
        self.assertEqual(out.count("unreadable: trade trade-3: a MAKER trade with none of the "
                                   "account's maker orders"), 2)

    def test_show_limits_the_trades_printed_of_each_side(self):
        more = [made(maker_order(maker_address=ME), id=f"trade-{n}") for n in range(5)]
        out = self.report(more, [], show=2)
        self.assertEqual(out.count('"trader_side": "MAKER"'), 2)
        self.assertIn("MAKER trades read: 5", out)

    def test_what_names_the_account_or_its_counterparties_is_not_printed(self):
        out = self.report([MADE, TAKEN, ODD], [], farmer={FARMER_ORDER})
        for secret in (ME, THEM, "api-key", TAKEN["transaction_hash"], FARMER_ORDER):
            self.assertNotIn(secret.lower(), out.lower())
        self.assertIn('"maker_address": "ME"', out)
        self.assertIn('"maker_address": "OTHER"', out)


class Clob:
    def __init__(self, listing):
        self.listing, self.asked = listing, []

    def get_address(self):
        return SIGNER

    def get_open_orders(self):
        return []

    def get_trades(self, params, only_first_page=False):
        self.asked.append((params.maker_address, params.after))
        listing = self.listing(params) if callable(self.listing) else self.listing
        if isinstance(listing, Exception):
            raise listing
        return listing


class StartTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.farm_path = os.path.join(tmp.name, "farm.db")

    def main(self, argv, clob, connect_raises=None):
        def connect(private_key):
            if connect_raises:
                raise connect_raises
            return clob

        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target, fake in (
                    ("polymarket.keys.load_private_key", lambda ask_password: KEY),
                    ("polymarket.client.connect", connect),
                    ("time.time", lambda: 1791160000.0)):
                stack.enter_context(mock.patch(target, fake))
            stack.enter_context(mock.patch.object(trades_dump, "load_dotenv"))
            stack.enter_context(mock.patch.dict(os.environ, {"POLYMARKET_FUNDER_ADDRESS": ME}))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = trades_dump.main([*argv, "--db", self.farm_path])
        return code, out.getvalue()

    def test_it_lists_the_trades_with_and_without_the_address_and_only_lists(self):
        clob = Clob([MADE, TAKEN])
        code, out = self.main(["--hours", "2", "--show", "1"], clob)
        self.assertEqual(code, 0)
        self.assertEqual(clob.asked, [(ME, 1791152800), (None, 1791152800)])
        self.assertIn("Trades matched in the last 2 hours", out)
        self.assertIn("MAKER trade 1 of 1 shown", out)
        self.assertIn("not checked, farm.db could not be read", out)
        self.assertNotIn(KEY, out)
        self.assertFalse(os.path.exists(self.farm_path))

    def test_it_reads_the_farmers_order_ids_from_farm_db_without_writing_it(self):
        conn = farm_db.open_db(self.farm_path)
        farm_db.record_farmer_orders(conn, placed=[("0xopen", "0xa")])
        status = farm_db.FarmerStatus("live", 1, NOW, NOW, 1.0, False, 1, {}, None)
        farm_db.publish(conn, status, events=[
            farm_db.Event(NOW, farm_db.EventKind.FILL, "FILL", "0xa", "FAV_BID", 0.97, 200.0,
                          None, {"order_id": FARMER_ORDER}),
            farm_db.Event(NOW, farm_db.EventKind.FILL, "FILL", "0xa", "FAV_ASK", 0.98, 5.0,
                          None, {"order_id": None})])
        conn.close()
        before = os.path.getmtime(self.farm_path)
        code, out = self.main([], Clob([MADE]))
        self.assertEqual(code, 0)
        self.assertIn("Maker fills on the farmer's orders: 1 of 1 (farm.db names 2 order ids)",
                      out)
        self.assertEqual(os.path.getmtime(self.farm_path), before)

    def test_a_listing_that_fails_ends_it_without_printing_the_address(self):
        code, out = self.main([], Clob(ConnectionError(f"GET /trades?maker_address={ME}")))
        self.assertEqual(code, 1)
        self.assertIn("the trades could not be listed (GET /trades?maker_address=ME)", out)
        self.assertNotIn(ME, out)

    def test_a_listing_with_no_address_that_fails_still_reports_the_one_with_it(self):
        def listing(params):
            return [MADE] if params.maker_address else RuntimeError("500 Internal server error")

        code, out = self.main(["--show", "1"], Clob(listing))
        self.assertEqual(code, 0)
        self.assertIn("With no address: not listed (500 Internal server error)", out)
        self.assertIn("MAKER trade 1 of 1 shown", out)

    def test_the_probe_says_which_ways_of_asking_work_without_printing_an_address(self):
        def listing(params):
            if params.maker_address:
                return RuntimeError(f"500 for {params.maker_address}")
            return [MADE]

        code, out = self.main(["--probe"], Clob(listing))
        self.assertEqual(code, 0)
        self.assertIn("open orders: ok, 0 orders", out)
        self.assertIn("trades, no parameters: ok, 1 trades", out)
        self.assertIn("trades, maker_address = funder: failed (500 for ME)", out)
        self.assertIn("trades, maker_address = funder in lowercase: failed (500 for ME)", out)
        self.assertIn("trades, maker_address = signer: failed (500 for SIGNER)", out)
        self.assertNotIn(ME, out)
        self.assertNotIn(SIGNER, out)

    def test_a_refused_time_bound_is_told_apart_by_a_listing_without_it(self):
        def listing(params):
            return RuntimeError("500 Internal server error") if params.after else [MADE]

        clob = Clob(listing)
        code, out = self.main(["--show", "1"], clob)
        self.assertEqual(code, 0)
        self.assertIn("so the CLOB refused the time bound", out)
        self.assertIn("MAKER trade 1 of 1 shown", out)
        self.assertEqual(clob.asked[:2], [(ME, 1791160000 - 72 * 3600), (ME, None)])

    def test_an_account_the_environment_does_not_describe_ends_it(self):
        code, out = self.main([], Clob([]), ConfigError("POLYMARKET_FUNDER_ADDRESS is not set"))
        self.assertEqual(code, 1)
        self.assertIn("POLYMARKET_FUNDER_ADDRESS is not set", out)


if __name__ == "__main__":
    unittest.main()
