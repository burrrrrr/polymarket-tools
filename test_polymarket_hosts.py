"""Tests for polymarket.hosts: the defaults and the environment overrides."""

import importlib
import os
import unittest
from unittest import mock

from polymarket import hosts


class HostsTests(unittest.TestCase):
    def reloaded(self, env):
        self.addCleanup(importlib.reload, hosts)
        with mock.patch.dict(os.environ, env, clear=True):
            return importlib.reload(hosts)

    def test_defaults_are_the_production_hosts(self):
        h = self.reloaded({})
        self.assertEqual(h.CLOB_HOST, "https://clob.polymarket.com")
        self.assertEqual(h.GAMMA_HOST, "https://gamma-api.polymarket.com")
        self.assertEqual(h.DATA_API_HOST, "https://data-api.polymarket.com")
        self.assertEqual(h.WSS_MARKET_URL, "wss://ws-subscriptions-clob.polymarket.com/ws/market")
        self.assertEqual(h.GEOBLOCK_URL, "https://polymarket.com/api/geoblock")

    def test_each_host_is_overridden_by_the_variable_the_farmer_reads_today(self):
        h = self.reloaded({
            "POLYMARKET_HOST": "https://clob.test",
            "POLYMARKET_GAMMA_HOST": "https://gamma.test",
            "POLYMARKET_DATA_API_HOST": "https://data.test",
            "POLYMARKET_WSS_HOST": "wss://ws.test",
        })
        self.assertEqual(h.CLOB_HOST, "https://clob.test")
        self.assertEqual(h.GAMMA_HOST, "https://gamma.test")
        self.assertEqual(h.DATA_API_HOST, "https://data.test")
        self.assertEqual(h.WSS_MARKET_URL, "wss://ws.test/ws/market")


if __name__ == "__main__":
    unittest.main()
