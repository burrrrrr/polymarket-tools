"""Tests for polymarket.parse: the loosely typed values the APIs send."""

import unittest
from datetime import datetime, timezone

import reward_screener
from polymarket.parse import as_float, parse_time


class AsFloatTests(unittest.TestCase):
    def test_numbers_and_numeric_strings_parse(self):
        self.assertEqual(as_float("0.001"), 0.001)
        self.assertEqual(as_float(3), 3.0)

    def test_anything_else_is_none(self):
        for value in (None, "", "abc", [], {}, object()):
            with self.subTest(value=value):
                self.assertIsNone(as_float(value))


class ParseTimeTests(unittest.TestCase):
    def test_a_z_suffix_and_a_naive_time_are_both_utc(self):
        expected = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_time("2026-11-03T12:00:00Z"), expected)
        self.assertEqual(parse_time("2026-11-03T12:00:00"), expected)

    def test_it_reads_every_value_as_the_screener_does(self):
        for value in ("2026-11-03T12:00:00Z", "2026-11-03T12:00:00+02:00", "2026-11-03",
                      "2026-11-03 12:00:00", "", None, "soon", 0, 20261103):
            with self.subTest(value=value):
                self.assertEqual(parse_time(value), reward_screener.parse_time(value))


if __name__ == "__main__":
    unittest.main()
