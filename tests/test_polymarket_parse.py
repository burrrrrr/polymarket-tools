import unittest
from datetime import datetime, timedelta, timezone

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

    def test_an_offset_is_kept_and_a_date_alone_is_midnight_utc(self):
        self.assertEqual(parse_time("2026-11-03T12:00:00+02:00"),
                         datetime(2026, 11, 3, 12, 0, tzinfo=timezone(timedelta(hours=2))))
        midnight = datetime(2026, 11, 3, tzinfo=timezone.utc)
        self.assertEqual(parse_time("2026-11-03"), midnight)
        self.assertEqual(parse_time(20261103), midnight)
        self.assertEqual(parse_time("2026-11-03 12:00:00"),
                         datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc))

    def test_an_empty_or_unreadable_value_is_none(self):
        for value in ("", None, "soon", 0):
            with self.subTest(value=value):
                self.assertIsNone(parse_time(value))


if __name__ == "__main__":
    unittest.main()
