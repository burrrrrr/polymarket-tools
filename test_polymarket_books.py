"""Tests for polymarket.books: parsing a book and reading books from the CLOB.
No request leaves the process."""

import json
import unittest

import httpx

from polymarket import books
from polymarket.books import (
    Levels,
    best_ask,
    best_bid,
    fetch_book,
    fetch_books,
    parse_books,
    parse_levels,
    request_books,
)

ENTRIES = [
    {"asset_id": "1", "tick_size": "0.01",
     "bids": [{"price": "0.97", "size": "10"}, {"price": "0.99", "size": "5"}],
     "asks": [{"price": "1.00", "size": "3"}, {"price": "0.995", "size": "8"}]},
    {"bids": [{"price": "0.5", "size": "0"}, {"price": "0.4", "size": "7"}], "asks": []},
    {"bids": [{"price": "x", "size": "1"}, {"price": "0.3"}, {"size": "4"}], "asks": None},
    {"bids": [{"price": 0.2, "size": 3}], "asks": [{"price": 0.8, "size": 2.5}],
     "tick_size": 0.001},
    {"tick_size": "fine"},
    {},
]


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def book_entry(token_id):
    return {"asset_id": token_id, "tick_size": "0.01",
            "bids": [{"price": "0.98", "size": "10"}], "asks": [{"price": "0.99", "size": "10"}]}


class ParseLevelsTests(unittest.TestCase):
    def test_levels_are_sorted_from_the_touch_and_empty_or_unreadable_ones_dropped(self):
        self.assertEqual(parse_levels(ENTRIES[0]),
                         Levels(bids=[(0.99, 5.0), (0.97, 10.0)],
                                asks=[(0.995, 8.0), (1.0, 3.0)], tick=0.01))
        self.assertEqual(parse_levels(ENTRIES[1]), Levels(bids=[(0.4, 7.0)], asks=[], tick=None))
        self.assertEqual(parse_levels(ENTRIES[2]), Levels(bids=[], asks=[], tick=None))

    def test_numbers_parse_like_strings_and_a_missing_side_or_unreadable_tick_is_empty(self):
        self.assertEqual(parse_levels(ENTRIES[3]),
                         Levels(bids=[(0.2, 3.0)], asks=[(0.8, 2.5)], tick=0.001))
        self.assertEqual(parse_levels(ENTRIES[4]), Levels(bids=[], asks=[], tick=None))
        self.assertEqual(parse_levels(ENTRIES[5]), Levels(bids=[], asks=[], tick=None))

    def test_levels_are_bids_asks_and_a_tick_that_defaults_to_none(self):
        self.assertEqual(Levels._fields, ("bids", "asks", "tick"))
        self.assertEqual(Levels([], []), Levels([], [], None))

    def test_best_prices_are_the_touch_or_none_on_an_empty_side(self):
        levels = parse_levels(ENTRIES[0])
        self.assertEqual((best_bid(levels), best_ask(levels)), (0.99, 0.995))
        empty = Levels([], [])
        self.assertEqual((best_bid(empty), best_ask(empty)), (None, None))


class FetchBookTests(unittest.TestCase):
    def test_one_book_is_read_by_token_id(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json=ENTRIES[0])

        self.assertEqual(fetch_book(client(handler), "1"), parse_levels(ENTRIES[0]))
        self.assertEqual(seen, [f"{books.CLOB_HOST}/book?token_id=1"])

    def test_a_refused_read_raises(self):
        with self.assertRaises(httpx.HTTPStatusError):
            fetch_book(client(lambda request: httpx.Response(404)), "1")


class RequestBooksTests(unittest.TestCase):
    def test_one_request_asks_for_every_token_and_returns_the_entries_unparsed(self):
        seen = []

        def handler(request):
            seen.append((request.method, str(request.url), json.loads(request.content),
                         request.extensions["timeout"]["read"]))
            return httpx.Response(200, json=[book_entry("1"), "noise"])

        self.assertEqual(request_books(client(handler), ["1", "2"], timeout=7),
                         [book_entry("1"), "noise"])
        self.assertEqual(seen, [("POST", f"{books.CLOB_HOST}/books",
                                 [{"token_id": "1"}, {"token_id": "2"}], 7)])

    def test_a_refused_request_raises(self):
        with self.assertRaises(httpx.HTTPStatusError):
            request_books(client(lambda request: httpx.Response(502)), ["1"])

    def test_a_null_answer_is_no_entries(self):
        http = client(lambda request: httpx.Response(200, content=b"null"))
        self.assertEqual(request_books(http, ["1"]), [])


class ParseBooksTests(unittest.TestCase):
    def test_each_book_is_keyed_by_its_token_and_an_entry_naming_no_token_is_left_out(self):
        entries = [book_entry("1"), "noise", {"bids": []}, None, {**ENTRIES[0], "asset_id": 2}]
        self.assertEqual(parse_books(entries), {"1": parse_levels(book_entry("1")),
                                                "2": parse_levels(ENTRIES[0])})

    def test_a_book_that_does_not_parse_raises(self):
        with self.assertRaises(AttributeError):
            parse_books([{"asset_id": "1", "bids": ["not a level"]}])


class FetchBooksTests(unittest.TestCase):
    def recording(self, fail_when=lambda tokens: False):
        """A /books endpoint that answers each request, and the token lists it was sent."""
        requests = []

        def handler(request):
            self.assertEqual((request.method, str(request.url)),
                             ("POST", f"{books.CLOB_HOST}/books"))
            tokens = [item["token_id"] for item in json.loads(request.content)]
            requests.append(tokens)
            if fail_when(tokens):
                return httpx.Response(502)
            return httpx.Response(200, json=[book_entry(t) for t in tokens])

        return client(handler), requests

    def test_every_token_is_asked_for_once_in_requests_of_at_most_500(self):
        http, requests = self.recording()
        token_ids = [str(n) for n in range(1201)]
        result = fetch_books(http, token_ids + token_ids[:10])
        self.assertEqual(sorted(len(r) for r in requests), [201, 500, 500])
        self.assertEqual(sorted(t for r in requests for t in r), sorted(token_ids))
        self.assertEqual(set(result), set(token_ids))
        self.assertEqual(result["1200"], parse_levels(book_entry("1200")))

    def test_one_failed_chunk_raises_instead_of_returning_the_other_books(self):
        http, _ = self.recording(fail_when=lambda tokens: "700" in tokens)
        with self.assertRaises(httpx.HTTPStatusError):
            fetch_books(http, [str(n) for n in range(1201)])

    def test_a_token_the_clob_does_not_know_is_absent(self):
        def handler(request):
            return httpx.Response(200, json=[book_entry("1"), "noise", {"bids": []}])

        self.assertEqual(set(fetch_books(client(handler), ["1", "2"])), {"1"})

    def test_no_tokens_sends_no_request(self):
        http, requests = self.recording()
        self.assertEqual(fetch_books(http, []), {})
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
