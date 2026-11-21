import unittest
from datetime import datetime, timezone

from polymarket.orders import Side
from polymarket.trades import Fill, FillState, UnreadableTrade, fetch_fills, parse_trades

ME = "0x" + "aB" * 20
THEM = "0x" + "cd" * 20
MARKET = "0x" + "08" * 32
YES, NO = "71" * 38, "52" * 38
MATCHED_AT = datetime(2026, 10, 4, 23, 38, 33, tzinfo=timezone.utc)


def maker_order(**changes):
    return {"order_id": "0xresting", "owner": "maker-api-key", "maker_address": THEM,
            "matched_amount": "120.5", "price": "0.97", "fee_rate_bps": "0", "asset_id": YES,
            "outcome": "Yes", "side": "SELL", **changes}


def trade(**changes):
    """A trade the account took, as the CLOB's docs show one."""
    return {"id": "trade-1", "taker_order_id": "0xtaking", "market": MARKET, "asset_id": YES,
            "side": "BUY", "size": "120.5", "fee_rate_bps": "0", "price": "0.97",
            "status": "CONFIRMED", "match_time": "1791157113", "last_update": "1791157140",
            "outcome": "Yes", "owner": "taker-api-key", "maker_address": ME,
            "transaction_hash": "0x" + "9f" * 32, "bucket_index": 0, "trader_side": "TAKER",
            "maker_orders": [maker_order()], **changes}


def made(*maker_orders, **changes):
    """A trade that filled the account's resting orders: the top level is the taker's."""
    return trade(trader_side="MAKER", maker_address=THEM, asset_id=NO, side="BUY",
                 size="300", price="0.03", maker_orders=list(maker_orders), **changes)


class ParseTradesTests(unittest.TestCase):
    def test_a_trade_the_account_took_is_one_fill_from_the_top_level(self):
        self.assertEqual(parse_trades([trade()], ME), [Fill(
            trade_id="trade-1", order_id="0xtaking", condition_id=MARKET, token_id=YES,
            side=Side.BUY, shares=120.5, price=0.97, matched_at=MATCHED_AT, taker=True,
            state=FillState.CONFIRMED)])

    def test_a_trade_the_account_made_is_one_fill_for_each_of_its_own_maker_orders(self):
        entry = made(maker_order(order_id="0xmine-1", maker_address=ME, side="BUY",
                                 matched_amount="200", price="0.97"),
                     maker_order(order_id="0xtheirs", matched_amount="60"),
                     maker_order(order_id="0xmine-2", maker_address=ME.upper(), asset_id=NO,
                                 side="SELL", matched_amount="40", price="0.03"),
                     status="MATCHED")
        fills = parse_trades([entry], ME.lower())
        self.assertEqual([(f.order_id, f.token_id, f.side, f.shares, f.price) for f in fills],
                         [("0xmine-1", YES, Side.BUY, 200.0, 0.97),
                          ("0xmine-2", NO, Side.SELL, 40.0, 0.03)])
        self.assertEqual({(f.trade_id, f.condition_id, f.matched_at, f.taker, f.state)
                          for f in fills},
                         {("trade-1", MARKET, MATCHED_AT, False, FillState.PENDING)})
        self.assertEqual(len({f.key for f in fills}), 2)

    def test_a_trade_the_account_took_from_its_own_resting_order_is_both_fills(self):
        entry = trade(maker_orders=[maker_order(order_id="0xmine", maker_address=ME)])
        self.assertEqual([(f.order_id, f.side, f.taker) for f in parse_trades([entry], ME)],
                         [("0xmine", Side.SELL, False), ("0xtaking", Side.BUY, True)])

    def test_every_status_is_pending_confirmed_or_failed(self):
        states = {"MATCHED": FillState.PENDING, "MINED": FillState.PENDING,
                  "RETRYING": FillState.PENDING, "CONFIRMED": FillState.CONFIRMED,
                  "FAILED": FillState.FAILED, "TRADE_STATUS_MINED": FillState.PENDING,
                  "TRADE_STATUS_FAILED": FillState.FAILED}
        for status, state in states.items():
            with self.subTest(status=status):
                self.assertEqual(parse_trades([trade(status=status)], ME)[0].state, state)

    def test_no_trades_are_no_fills(self):
        self.assertEqual(parse_trades([], ME), [])

    def test_a_trade_it_cannot_read_is_refused_by_its_id(self):
        unreadable = {
            "match_time": trade(match_time=None),
            "status": trade(status="SETTLED"),
            "trader_side": trade(trader_side="BOTH"),
            "no taker_order_id": {k: v for k, v in trade().items() if k != "taker_order_id"},
            "size": trade(size="many"),
            "not a valid Side": trade(side="HOLD"),
            "none of the account's maker orders": made(maker_order()),
            "matched_amount": made(maker_order(maker_address=ME, matched_amount=None)),
            "no order_id": made({k: v for k, v in maker_order(maker_address=ME).items()
                                 if k != "order_id"}),
        }
        for reason, entry in unreadable.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(UnreadableTrade, f"trade trade-1: .*{reason}"):
                    parse_trades([trade(id="trade-0"), entry], ME)

    def test_a_listing_that_is_not_a_list_or_a_trade_without_an_id_is_refused(self):
        for listing in ({"data": []}, None, [trade(id="")], ["trade-1"]):
            with self.subTest(listing=listing), self.assertRaises(UnreadableTrade):
                parse_trades(listing, ME)

    def test_an_unreadable_trade_is_a_value_error_that_names_no_address(self):
        with self.assertRaises(ValueError) as raised:
            parse_trades([made(maker_order())], ME)
        self.assertNotIn(ME.lower(), str(raised.exception).lower())
        self.assertNotIn(THEM, str(raised.exception))


class Clob:
    def __init__(self, listing):
        self.listing, self.asked = listing, []

    def get_trades(self, params):
        self.asked.append((params.market, params.maker_address, params.after))
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing


class FetchFillsTests(unittest.TestCase):
    def test_it_asks_for_one_markets_trades_after_a_time_without_the_address(self):
        clob = Clob([trade()])
        fills = fetch_fills(clob, ME, "0xmarket", 1791150000)
        self.assertEqual(clob.asked, [("0xmarket", None, 1791150000)])
        self.assertEqual([f.order_id for f in fills], ["0xtaking"])

    def test_a_failed_listing_raises_what_the_client_raised(self):
        with self.assertRaises(ConnectionError):
            fetch_fills(Clob(ConnectionError("timed out")), ME, "0xmarket", 0)


if __name__ == "__main__":
    unittest.main()
