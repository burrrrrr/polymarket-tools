"""Tests for polymarket.gamma: market lookups by condition id and the search.
No request leaves the process."""

import unittest
from datetime import datetime, timezone

import httpx

from polymarket import gamma
from polymarket.gamma import Market, decode_list, fetch_markets, market_names, search


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def cid(n):
    return f"0x{n:064x}"


class Gamma:
    """A /markets endpoint over a set of open and closed markets. Like the real
    one it returns closed markets only when asked for them, and at most `limit`
    entries, 20 when no limit is sent."""

    def __init__(self, open_ids=(), closed_ids=(), fail_when=lambda ids: False):
        self.open_ids, self.closed_ids = set(open_ids), set(closed_ids)
        self.fail_when = fail_when
        self.requests = []

    def __call__(self, request):
        assert request.url.path == "/markets", request.url
        params = request.url.params
        ids = params.get_list("condition_ids")
        self.requests.append(params)
        if self.fail_when(ids):
            return httpx.Response(500)
        pool = self.closed_ids if params.get("closed") == "true" else self.open_ids
        found = [{"conditionId": c, "question": f"Q {c[-4:]}"} for c in ids if c in pool]
        return httpx.Response(200, json=found[:int(params.get("limit", 20))])


class FetchMarketsTests(unittest.TestCase):
    def test_every_market_comes_back_from_requests_of_at_most_100_ids(self):
        ids = [cid(n) for n in range(250)]
        api = Gamma(open_ids=ids)
        entries = fetch_markets(client(api), ids + ids[:5])
        self.assertEqual(sorted(e["conditionId"] for e in entries), sorted(ids))
        self.assertEqual(sorted(len(p.get_list("condition_ids")) for p in api.requests),
                         [50, 100, 100])

    def test_closed_markets_are_asked_for_only_on_request(self):
        api = Gamma(open_ids=[cid(1)], closed_ids=[cid(2)])
        http = client(api)
        self.assertEqual([e["conditionId"] for e in fetch_markets(http, [cid(1), cid(2)])],
                         [cid(1)])
        self.assertEqual(
            [e["conditionId"] for e in fetch_markets(http, [cid(1), cid(2)], closed=True)],
            [cid(2)])
        fetch_markets(http, [cid(1)], closed=False)
        self.assertEqual([p.get("closed") for p in api.requests], [None, "true", "false"])

    def test_one_failed_chunk_raises_instead_of_returning_the_other_markets(self):
        ids = [cid(n) for n in range(250)]
        api = Gamma(open_ids=ids, fail_when=lambda chunk: cid(120) in chunk)
        with self.assertRaises(httpx.HTTPStatusError):
            fetch_markets(client(api), ids)

    def test_no_ids_sends_no_request(self):
        api = Gamma()
        self.assertEqual(fetch_markets(client(api), []), [])
        self.assertEqual(api.requests, [])


class MarketNamesTests(unittest.TestCase):
    def test_closed_markets_are_named_too_from_a_second_read_of_the_missing_ids(self):
        api = Gamma(open_ids=[cid(1)], closed_ids=[cid(2)])
        names = market_names(client(api), [cid(1), cid(2), cid(3), "", None])
        self.assertEqual(names, {cid(1): "Q 0001", cid(2): "Q 0002"})
        self.assertEqual([(p.get_list("condition_ids"), p.get("closed")) for p in api.requests],
                         [([cid(1), cid(2), cid(3)], None), ([cid(2), cid(3)], "true")])

    def test_open_markets_alone_take_one_request(self):
        api = Gamma(open_ids=[cid(1), cid(2)])
        market_names(client(api), [cid(1), cid(2)])
        self.assertEqual(len(api.requests), 1)

    def test_a_market_with_no_question_is_named_by_its_title(self):
        def handler(request):
            return httpx.Response(200, json=[{"conditionId": cid(1), "title": "By title"},
                                             {"conditionId": cid(2)}])

        self.assertEqual(market_names(client(handler), [cid(1)]), {cid(1): "By title"})


SEARCH_PAYLOAD = {
    "events": [
        {"title": "Fed decision in December", "markets": [
            {"conditionId": cid(1), "question": "Will the Fed cut rates?",
             "slug": "fed-cut-december", "outcomes": '["Yes", "No"]',
             "clobTokenIds": '["111", "222"]', "endDate": "2026-12-10T19:00:00Z",
             "volume24hr": 1234.5, "volume": "99999", "orderPriceMinTickSize": 0.01,
             "orderMinSize": 5, "acceptingOrders": True, "closed": False},
            {"conditionId": cid(2), "question": "Resolved already", "closed": True,
             "clobTokenIds": '["333", "444"]'},
        ]},
        {"title": "Bare event", "markets": [{"conditionId": cid(3)}]},
        {"title": "No markets"},
    ],
}


class SearchTests(unittest.TestCase):
    def test_open_markets_of_every_event_come_back_as_markets(self):
        seen = []

        def handler(request):
            seen.append(request.url)
            return httpx.Response(200, json=SEARCH_PAYLOAD)

        found = search(client(handler), "fed rates")
        self.assertEqual(found[0], Market(
            condition_id=cid(1), question="Will the Fed cut rates?", slug="fed-cut-december",
            event_title="Fed decision in December", outcomes=("Yes", "No"),
            token_ids=("111", "222"),
            end_date=datetime(2026, 12, 10, 19, 0, tzinfo=timezone.utc),
            volume=1234.5, tick=0.01, closed=False, accepting_orders=True, min_size=5.0))
        self.assertEqual(found[1], Market(
            condition_id=cid(3), question="", slug="", event_title="Bare event", outcomes=(),
            token_ids=(), end_date=None, volume=0.0, tick=None, closed=False,
            accepting_orders=False, min_size=0.0))
        self.assertEqual(len(found), 2)
        self.assertEqual(seen[0].path, "/public-search")
        self.assertEqual(seen[0].params.get("q"), "fed rates")

    def test_it_asks_for_active_events_only(self):
        # Without events_status the search returns markets that resolved years ago.
        seen = []

        def handler(request):
            seen.append(request.url.params)
            return httpx.Response(200, json={})

        self.assertEqual(search(client(handler), "x"), [])
        self.assertEqual(seen[0].get("events_status"), "active")

    def test_a_failed_search_raises(self):
        with self.assertRaises(httpx.HTTPStatusError):
            search(client(lambda request: httpx.Response(451)), "x")

    def test_volume_falls_back_to_the_total_when_there_is_no_24_hour_figure(self):
        def handler(request):
            return httpx.Response(200, json={"events": [
                {"markets": [{"conditionId": cid(1), "volume": "5000"}]}]})

        self.assertEqual(search(client(handler), "x")[0].volume, 5000.0)


class DecodeListTests(unittest.TestCase):
    def test_a_list_and_a_json_string_of_a_list_both_decode(self):
        self.assertEqual(decode_list(["a", "b"]), ["a", "b"])
        self.assertEqual(decode_list('["a", "b"]'), ["a", "b"])

    def test_anything_else_is_an_empty_list(self):
        for value in (None, "", "not json", '{"a": 1}', "7", 7, {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(decode_list(value), [])


class HostTests(unittest.TestCase):
    def test_lookups_and_search_go_to_the_gamma_host(self):
        seen = []

        def handler(request):
            seen.append(f"{request.url.scheme}://{request.url.host}")
            return httpx.Response(200, json=[] if request.url.path == "/markets" else {})

        http = client(handler)
        fetch_markets(http, [cid(1)])
        search(http, "x")
        self.assertEqual(seen, [gamma.GAMMA_HOST] * 2)


if __name__ == "__main__":
    unittest.main()
