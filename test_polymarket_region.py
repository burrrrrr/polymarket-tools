"""Tests for polymarket.region: where the API takes new orders from. No request
leaves the process."""

import unittest

import httpx

from polymarket import region
from polymarket.region import Verdict, check, is_geoblocked, read_region, verdict

TRACE = "fl=1f1\nh=clob.polymarket.com\nip=203.0.113.9\nloc=pt\ncolo=LIS\n"


def edge(geoblock=None, trace=None, time=200):
    """A client over the three hosts the check reads. geoblock is the JSON the
    geo lookup answers, trace the CDN trace text, time the CLOB's status. None
    makes that host unreachable."""
    def handler(request):
        url = str(request.url)
        if url == region.GEOBLOCK_URL and geoblock is not None:
            return httpx.Response(200, json=geoblock)
        if url == region.TRACE_URL and trace is not None:
            return httpx.Response(200, text=trace)
        if url == f"{region.CLOB_HOST}/time" and time is not None:
            return httpx.Response(time)
        raise httpx.ConnectError("unreachable", request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


class VerdictTests(unittest.TestCase):
    def test_a_blocked_country_is_not_allowed(self):
        for country in ("US", "GB", "FR", "SG"):
            with self.subTest(country=country):
                result = verdict(country, None)
                self.assertFalse(result.allowed)
                self.assertIn(country, result.message)

    def test_a_partly_blocked_country_is_decided_by_its_region(self):
        self.assertFalse(verdict("CA", "ON").allowed)
        self.assertIn("CA-ON", verdict("CA", "ON").message)
        self.assertTrue(verdict("CA", "NS").allowed)
        self.assertFalse(verdict("UA", "43").allowed)
        self.assertTrue(verdict("UA", "30").allowed)

    def test_a_partly_blocked_country_with_no_region_is_allowed_and_says_it_was_not_checked(self):
        result = verdict("CA", None)
        self.assertTrue(result.allowed)
        self.assertIn("not checked", result.message)

    def test_a_place_blocked_only_on_the_website_is_allowed_and_says_so(self):
        for country in ("NL", "JP", "IE", "MT"):
            with self.subTest(country=country):
                result = verdict(country, "NH")
                self.assertTrue(result.allowed)
                self.assertIn("website", result.message)

    def test_any_other_place_is_allowed(self):
        self.assertEqual(verdict("PT", "11"), Verdict(True, "PT-11: new orders permitted"))
        self.assertEqual(verdict("CH", None), Verdict(True, "CH: new orders permitted"))

    def test_an_unread_country_is_allowed_and_says_it_was_not_checked(self):
        result = verdict(None, None)
        self.assertTrue(result.allowed)
        self.assertIn("not checked", result.message)


class ReadRegionTests(unittest.TestCase):
    def test_the_geo_lookup_gives_country_and_region_in_upper_case(self):
        http = edge(geoblock={"blocked": False, "ip": "203.0.113.9", "country": "ca",
                              "region": "on"})
        self.assertEqual(read_region(http), ("CA", "ON"))

    def test_the_cdn_trace_gives_the_country_when_the_geo_lookup_is_down(self):
        self.assertEqual(read_region(edge(trace=TRACE)), ("PT", None))

    def test_an_answer_with_no_country_falls_back_to_the_trace_too(self):
        for answer in ({"blocked": False}, ["unexpected"], "text"):
            with self.subTest(answer=answer):
                self.assertEqual(read_region(edge(geoblock=answer, trace=TRACE)), ("PT", None))

    def test_nothing_is_known_when_neither_answers(self):
        self.assertEqual(read_region(edge()), (None, None))
        self.assertEqual(read_region(edge(trace="no fields here")), (None, None))


class CheckTests(unittest.TestCase):
    def test_a_clob_that_refuses_this_address_is_not_allowed_in_an_allowed_country(self):
        result = check(edge(geoblock={"country": "PT", "region": "11"}, time=451))
        self.assertFalse(result.allowed)
        self.assertIn("451", result.message)
        self.assertIn("PT-11", result.message)

    def test_the_websites_own_blocked_flag_is_ignored(self):
        # The geo lookup reports blocked=true for places whose API access is open.
        result = check(edge(geoblock={"blocked": True, "country": "NL", "region": "NH"}))
        self.assertTrue(result.allowed)

    def test_a_blocked_country_is_not_allowed_though_every_read_succeeds(self):
        self.assertFalse(check(edge(geoblock={"blocked": True, "country": "US",
                                              "region": "NY"})).allowed)

    def test_unreachable_hosts_are_allowed_and_say_nothing_was_checked(self):
        result = check(edge(time=None))
        self.assertTrue(result.allowed)
        self.assertIn("not checked", result.message)


class Rejection(Exception):
    def __init__(self, status_code=None, error_msg=None):
        super().__init__(error_msg)
        self.status_code = status_code
        self.error_msg = error_msg


class IsGeoblockedTests(unittest.TestCase):
    MARKER = {"error": "Trading restricted in your region, please refer to the docs"}

    def test_a_451_is_a_geoblock(self):
        self.assertTrue(is_geoblocked(Rejection(451)))

    def test_a_403_is_a_geoblock_only_when_it_says_so(self):
        self.assertTrue(is_geoblocked(Rejection(403, self.MARKER)))
        self.assertFalse(is_geoblocked(Rejection(403, {"error": "invalid signature"})))

    def test_another_status_is_not_a_geoblock_whatever_it_says(self):
        self.assertFalse(is_geoblocked(Rejection(400, self.MARKER)))

    def test_an_error_with_no_status_is_judged_by_its_text(self):
        self.assertTrue(is_geoblocked(RuntimeError("Trading Restricted In Your Region")))
        self.assertFalse(is_geoblocked(RuntimeError("timed out")))


if __name__ == "__main__":
    unittest.main()
