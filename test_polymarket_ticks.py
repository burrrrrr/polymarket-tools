import unittest
from datetime import datetime, timezone

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgsV2
from py_clob_client_v2.exceptions import PolyException

from polymarket import ticks
from polymarket.orders import LimitOrder, Side

KEY = "0x" + "11" * 32
MARKET = "0x" + "08" * 32
TOKEN, OTHER_TOKEN = "71" * 38, "72" * 38
EXPIRES = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


class OfflineClob(ClobClient):
    def __init__(self, tick):
        super().__init__("https://clob.invalid", chain_id=137, key=KEY)
        self.tick = tick
        self.gets = []

    def _get(self, endpoint, headers=None, params=None):
        path = endpoint.removeprefix(self.host)
        self.gets.append(path)
        if path == "/tick-size":
            return {"minimum_tick_size": self.tick}
        if path == "/neg-risk":
            return {"neg_risk": False}
        if path == "/version":
            return {"version": 2}
        if path == f"/clob-markets/{MARKET}":
            return {"t": [{"t": TOKEN, "o": "Yes"}, {"t": OTHER_TOKEN, "o": "No"}],
                    "mts": self.tick, "nr": False, "fd": {}}
        if path.startswith("/clob-markets/"):
            return {}
        raise AssertionError(f"unexpected GET {path}")

    def _post(self, endpoint, headers=None, data=None):
        raise AssertionError(f"unexpected POST {endpoint}")


def bid(signed):
    return int(signed.makerAmount), int(signed.takerAmount)


class RefreshTests(unittest.TestCase):
    def test_the_sdk_rounds_a_price_to_a_tick_it_remembers(self):
        clob = OfflineClob(tick=0.01)
        clob.get_tick_size(TOKEN)
        clob.tick = 0.001
        self.assertEqual(clob.get_tick_size(TOKEN), "0.01")
        signed = clob.create_order(OrderArgsV2(token_id=TOKEN, price=0.985, size=100, side="BUY"))
        self.assertEqual(bid(signed), (98_000_000, 100_000_000))

    def test_refresh_replaces_a_coarser_tick_the_client_remembered(self):
        clob = OfflineClob(tick=0.01)
        clob.get_tick_size(TOKEN)
        clob.tick = 0.001
        self.assertEqual(ticks.refresh(clob, MARKET, TOKEN), 0.001)
        signed = ticks.sign(clob, TOKEN, Side.BUY, 0.985, 100, None, 0.001)
        self.assertEqual(bid(signed), (98_500_000, 100_000_000))

    def test_refresh_replaces_a_finer_tick_too(self):
        clob = OfflineClob(tick=0.001)
        clob.get_tick_size(TOKEN)
        clob.tick = 0.01
        self.assertEqual(ticks.refresh(clob, MARKET, TOKEN), 0.01)

    def test_one_refresh_covers_both_tokens_and_signing_then_asks_for_no_tick(self):
        clob = OfflineClob(tick=0.001)
        ticks.refresh(clob, MARKET, TOKEN)
        ticks.sign(clob, TOKEN, Side.BUY, 0.985, 100, None, 0.001)
        ticks.sign(clob, OTHER_TOKEN, Side.SELL, 0.015, 100, None, 0.001)
        self.assertEqual(clob.gets, [f"/clob-markets/{MARKET}", "/version"])

    def test_a_market_that_cannot_be_read_raises(self):
        with self.assertRaises(PolyException):
            ticks.refresh(OfflineClob(tick=0.01), "0x" + "09" * 32, TOKEN)


class SignTests(unittest.TestCase):
    def setUp(self):
        self.clob = OfflineClob(tick=0.01)
        ticks.refresh(self.clob, MARKET, TOKEN)

    def test_an_order_is_signed_for_its_exact_price_and_size(self):
        signed = ticks.sign(self.clob, TOKEN, Side.BUY, 0.07, 1849.49, None, 0.01)
        self.assertEqual(bid(signed), (129_464_300, 1_849_490_000))
        self.assertEqual(signed.tokenId, TOKEN)

    def test_a_sell_gives_shares_for_dollars(self):
        signed = ticks.sign(self.clob, TOKEN, Side.SELL, 0.93, 60, None, 0.01)
        self.assertEqual((int(signed.makerAmount), int(signed.takerAmount)),
                         (60_000_000, 55_800_000))
        self.assertNotEqual(signed.side, ticks.sign(self.clob, TOKEN, Side.BUY, 0.93, 60, None,
                                                    0.01).side)

    def test_an_expiration_is_signed_in_unix_seconds_and_none_as_zero(self):
        self.assertEqual(ticks.sign(self.clob, TOKEN, Side.BUY, 0.5, 10, EXPIRES, 0.01).expiration,
                         str(int(EXPIRES.timestamp())))
        self.assertEqual(ticks.sign(self.clob, TOKEN, Side.BUY, 0.5, 10, None, 0.01).expiration,
                         "0")

    def test_a_price_between_ticks_is_refused_and_never_rounded(self):
        with self.assertRaisesRegex(ticks.OffGrid, "0.985.*0.01"):
            ticks.sign(self.clob, TOKEN, Side.BUY, 0.985, 100, None, 0.01)

    def test_a_tick_finer_than_the_client_remembers_is_refused_and_never_rounded(self):
        with self.assertRaisesRegex(PolyException, "invalid tick size"):
            ticks.sign(self.clob, TOKEN, Side.BUY, 0.985, 100, None, 0.001)


class SignerTests(unittest.TestCase):
    def setUp(self):
        self.clob = OfflineClob(tick=0.01)
        self.signer = ticks.Signer(self.clob, lambda: EXPIRES)

    def order(self, expires_after_s=0, price=0.999):
        return LimitOrder(TOKEN, Side.BUY, price, 100, expires_after_s)

    def test_prewarm_reads_the_token_and_the_venue_once_and_sign_then_sends_nothing(self):
        self.signer.prewarm(TOKEN)
        self.signer.prewarm(TOKEN)
        self.assertEqual(self.clob.gets, ["/neg-risk", "/version"])
        signed = self.signer.sign(self.order(), "0.001")
        self.assertEqual(self.clob.gets, ["/neg-risk", "/version"], "sign reached the network")
        self.assertEqual(bid(signed), (99_900_000, 100_000_000))
        self.assertEqual(signed.tokenId, TOKEN)

    def test_it_signs_at_the_target_tick_whatever_tick_the_client_remembers(self):
        self.clob.get_tick_size(TOKEN)
        self.signer.prewarm(TOKEN)
        self.assertEqual(bid(self.signer.sign(self.order(), "0.001")), (99_900_000, 100_000_000))
        self.assertEqual(bid(self.signer.sign(self.order(price=0.9999), "0.0001")),
                         (99_990_000, 100_000_000))
        self.assertEqual(bid(self.signer.sign(self.order(price=0.99), "0.01")),
                         (99_000_000, 100_000_000))

    def test_a_price_illegal_at_the_tick_is_refused_and_never_rounded(self):
        self.signer.prewarm(TOKEN)
        with self.assertRaises(PolyException):
            self.signer.sign(self.order(price=0.9995), "0.001")

    def test_a_token_that_was_not_prewarmed_cannot_be_signed(self):
        with self.assertRaises(KeyError):
            self.signer.sign(self.order(), "0.001")
        self.assertEqual(self.clob.gets, [])

    def test_an_expiration_is_the_clock_plus_the_orders_seconds_and_none_is_zero(self):
        self.signer.prewarm(TOKEN)
        self.assertEqual(self.signer.sign(self.order(3600), "0.001").expiration,
                         str(int(EXPIRES.timestamp()) + 3600))
        self.assertEqual(self.signer.sign(self.order(0), "0.001").expiration, "0")

    def test_a_venue_that_cannot_be_read_fails_the_prewarm(self):
        class Unreachable(OfflineClob):
            def _get(self, endpoint, headers=None, params=None):
                raise ConnectionError("timed out")

        with self.assertRaises(ConnectionError):
            ticks.Signer(Unreachable(tick=0.01), lambda: EXPIRES).prewarm(TOKEN)


class GridTests(unittest.TestCase):
    def test_a_multiple_of_the_tick_is_on_the_grid_whatever_the_float(self):
        for price, tick in ((0.985, 0.001), (0.07, 0.01), (0.57, 0.01), (0.999, 0.001),
                            (0.3, 0.1), (0.0015, 0.0001), (0.01, 0.01)):
            with self.subTest(price=price, tick=tick):
                self.assertTrue(ticks.on_grid(price, tick))

    def test_a_price_between_ticks_is_off_it(self):
        for price, tick in ((0.985, 0.01), (0.9995, 0.001), (0.25, 0.1)):
            with self.subTest(price=price, tick=tick):
                self.assertFalse(ticks.on_grid(price, tick))

    def test_a_tick_is_spelled_as_the_sdk_spells_it(self):
        self.assertEqual([ticks.text(tick) for tick in (0.1, 0.01, 0.001, 0.0001)],
                         ["0.1", "0.01", "0.001", "0.0001"])


if __name__ == "__main__":
    unittest.main()
