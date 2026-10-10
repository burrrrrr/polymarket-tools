"""Tests for polymarket.client: an authenticated client from a key and the
environment. The client class is a fake, so nothing is signed or sent."""

import os
import unittest
from unittest import mock

from py_clob_client_v2.client import ClobClient

from polymarket import client
from polymarket.client import ConfigError, connect

KEY = "0x" + "ab" * 32
FUNDER = "0x" + "12" * 20


class FakeClob:
    built = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.creds = None
        FakeClob.built.append(self)

    def create_or_derive_api_key(self):
        return {"derived_for": self.kwargs["key"]}

    def set_api_creds(self, creds):
        self.creds = creds


class ConnectTests(unittest.TestCase):
    def setUp(self):
        FakeClob.built = []

    def connected(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return connect(KEY, client_class=FakeClob)

    def test_the_client_is_built_for_the_funder_and_signed_in_with_its_derived_key(self):
        made = self.connected({"POLYMARKET_FUNDER_ADDRESS": FUNDER})
        self.assertEqual(made.kwargs, {"host": client.CLOB_HOST, "chain_id": 137, "key": KEY,
                                       "signature_type": 1, "funder": FUNDER})
        self.assertEqual(made.creds, {"derived_for": KEY})

    def test_the_environment_sets_the_signature_type_chain_and_builder_code(self):
        made = self.connected({"POLYMARKET_FUNDER_ADDRESS": FUNDER,
                               "POLYMARKET_SIGNATURE_TYPE": "2", "POLYMARKET_CHAIN_ID": "80002",
                               "POLY_BUILDER_CODE": "builder-7"})
        self.assertEqual(made.kwargs["signature_type"], 2)
        self.assertEqual(made.kwargs["chain_id"], 80002)
        self.assertEqual(made.kwargs["builder_config"], {"builderCode": "builder-7"})

    def test_a_missing_funder_raises_before_any_client_is_built(self):
        with self.assertRaises(ConfigError) as raised:
            self.connected({})
        self.assertIn("POLYMARKET_FUNDER_ADDRESS", str(raised.exception))
        self.assertEqual(FakeClob.built, [])

    def test_the_sdk_is_left_to_post_once_and_never_retry_on_its_own(self):
        # With retry_on_error the SDK re-posts after 30 ms on a timeout or a
        # 5xx: the answers after which the first order may rest, which the
        # restore and the sniper never retry.
        self.assertNotIn("retry_on_error",
                         self.connected({"POLYMARKET_FUNDER_ADDRESS": FUNDER}).kwargs)
        self.assertFalse(ClobClient(client.CLOB_HOST, chain_id=137, key=KEY).retry_on_error)

    def test_a_number_that_does_not_parse_raises_naming_its_variable(self):
        for name in ("POLYMARKET_SIGNATURE_TYPE", "POLYMARKET_CHAIN_ID"):
            with self.subTest(name=name), self.assertRaises(ConfigError) as raised:
                self.connected({"POLYMARKET_FUNDER_ADDRESS": FUNDER, name: "one"})
            self.assertIn(name, str(raised.exception))
        self.assertEqual(FakeClob.built, [])


if __name__ == "__main__":
    unittest.main()
