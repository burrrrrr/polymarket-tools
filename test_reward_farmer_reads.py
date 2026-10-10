import threading
import unittest
from unittest import mock

import httpx

import reward_farmer


def gamma_api(*statuses):
    calls = []

    def handle(request):
        calls.append((request, threading.current_thread()))
        status = statuses[len(calls) - 1] if len(calls) <= len(statuses) else 200
        return httpx.Response(status, headers={"retry-after": "2"}, json=[])

    return httpx.Client(transport=httpx.MockTransport(handle)), calls


def asked(calls):
    return [(request.url.params["closed"], request.url.params.get_list("condition_ids"))
            for request, _ in calls]


class FetchMarketInfosOrderTest(unittest.TestCase):
    IDS = [f"0x{i:03d}" for i in range(150)]

    def test_each_chunk_is_read_open_then_closed_before_the_next_chunk_on_the_calling_thread(self):
        http, calls = gamma_api()
        reward_farmer.fetch_market_infos(http, self.IDS)
        self.assertEqual(asked(calls), [("false", self.IDS[:100]), ("true", self.IDS[:100]),
                                        ("false", self.IDS[100:]), ("true", self.IDS[100:])])
        self.assertEqual({thread for _, thread in calls}, {threading.current_thread()})
        self.assertEqual({request.extensions["timeout"]["read"] for request, _ in calls}, {20})

    def test_a_rate_limited_read_waits_and_is_read_again(self):
        http, calls = gamma_api(429, 429)
        with mock.patch("time.sleep") as sleep:
            reward_farmer.fetch_market_infos(http, self.IDS[:2])
        self.assertEqual(asked(calls), [("false", self.IDS[:2])] * 3 + [("true", self.IDS[:2])])
        self.assertEqual([c.args for c in sleep.call_args_list], [(2.0,), (2.0,)])

    def test_a_read_still_rate_limited_after_the_retries_raises(self):
        http, calls = gamma_api(*[429] * 10)
        with mock.patch("time.sleep"), self.assertRaises(httpx.HTTPStatusError):
            reward_farmer.fetch_market_infos(http, self.IDS)
        self.assertEqual(len(calls), reward_farmer.RATE_LIMIT_RETRIES + 1)

    def test_a_failed_read_raises_and_nothing_after_it_is_read(self):
        http, calls = gamma_api(200, 500)
        with self.assertRaises(httpx.HTTPStatusError):
            reward_farmer.fetch_market_infos(http, self.IDS)
        self.assertEqual(asked(calls), [("false", self.IDS[:100]), ("true", self.IDS[:100])])


if __name__ == "__main__":
    unittest.main()
