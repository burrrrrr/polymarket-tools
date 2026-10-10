"""Tests for polymarket.http: the rate-limit retry, chunked requests, and the
client the order path keeps open. No request leaves the process."""

import threading
import unittest
from unittest import mock

import httpx

from polymarket.http import chunks, get_with_retry, in_chunks, persistent_client, use_for_sdk


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class GetWithRetryTests(unittest.TestCase):
    def answering(self, *responses):
        """A client that gives these responses in turn, and the requests it saw."""
        replies, seen = list(responses), []

        def handler(request):
            seen.append(request)
            return replies.pop(0)

        return client(handler), seen

    def test_a_429_is_waited_out_for_the_seconds_the_server_names(self):
        http, seen = self.answering(httpx.Response(429, headers={"retry-after": "2"}),
                                    httpx.Response(200, json={"ok": True}))
        with mock.patch("time.sleep") as sleep:
            resp = get_with_retry(http, "https://clob.test/x", {"a": "1"}, 5)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual([c.args for c in sleep.call_args_list], [(2.0,)])
        self.assertEqual([str(r.url) for r in seen], ["https://clob.test/x?a=1"] * 2)

    def test_the_wait_is_capped_and_defaults_to_a_second(self):
        http, _ = self.answering(httpx.Response(429, headers={"retry-after": "600"}),
                                 httpx.Response(429),
                                 httpx.Response(200))
        with mock.patch("time.sleep") as sleep:
            get_with_retry(http, "https://clob.test/x", None, 5)
        self.assertEqual([c.args for c in sleep.call_args_list], [(5.0,), (1.0,)])

    def test_it_gives_up_after_three_retries(self):
        http, seen = self.answering(*[httpx.Response(429) for _ in range(4)])
        with mock.patch("time.sleep") as sleep, self.assertRaises(httpx.HTTPStatusError):
            get_with_retry(http, "https://clob.test/x", None, 5)
        self.assertEqual(len(seen), 4)
        self.assertEqual(sleep.call_count, 3)

    def test_any_other_failure_raises_without_a_retry(self):
        http, seen = self.answering(httpx.Response(500))
        with mock.patch("time.sleep") as sleep, self.assertRaises(httpx.HTTPStatusError):
            get_with_retry(http, "https://clob.test/x", None, 5)
        self.assertEqual(len(seen), 1)
        sleep.assert_not_called()


class ChunkTests(unittest.TestCase):
    def test_chunks_cover_every_item_once_in_order(self):
        self.assertEqual(chunks([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])
        self.assertEqual(chunks([], 2), [])

    def test_in_chunks_returns_one_result_for_each_chunk_of_distinct_items_in_order(self):
        self.assertEqual(in_chunks(sum, [1, 2, 2, 3, 1, 4, 5], 2), [3, 7, 5])
        self.assertEqual(in_chunks(sum, [], 2), [])

    def test_in_chunks_raises_what_a_chunk_raises(self):
        def fetch(chunk):
            if 3 in chunk:
                raise LookupError("chunk failed")
            return chunk

        with self.assertRaises(LookupError):
            in_chunks(fetch, [1, 2, 3, 4, 5, 6], 2)

    def test_a_single_chunk_runs_on_the_calling_thread(self):
        threads = in_chunks(lambda chunk: threading.current_thread(), [1, 2], 2)
        self.assertEqual(threads, [threading.current_thread()])


class PersistentClientTests(unittest.TestCase):
    def test_its_idle_connections_never_expire_and_it_speaks_http2(self):
        http = persistent_client()
        self.addCleanup(http.close)
        pool = http._transport._pool
        self.assertIsNone(pool._keepalive_expiry)
        self.assertTrue(pool._http2)


class UseForSdkTests(unittest.TestCase):
    def test_the_sdk_sends_its_requests_through_the_given_client(self):
        import py_clob_client_v2.http_helpers.helpers as sdk_http
        self.addCleanup(setattr, sdk_http, "_http_client", sdk_http._http_client)
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"through": "ours"})

        use_for_sdk(client(handler))
        self.assertEqual(sdk_http.request("https://clob.test/time", "GET"), {"through": "ours"})
        self.assertEqual(seen, ["https://clob.test/time"])


if __name__ == "__main__":
    unittest.main()
