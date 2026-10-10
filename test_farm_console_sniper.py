"""Tests for the console's Sniper page: the search, the arm form, the watch
list and the disarm. The search and the books are fakes injected through
create_app, and the worker's rows are written straight through tools.db."""

import asyncio
import os
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from fastapi.testclient import TestClient

from farm_console.app import create_app
from farm_console.pages import stream
from polymarket.books import Levels
from polymarket.gamma import Market
from test_farm_console import ORIGIN, Console
from test_polymarket_imports import loaded_by
from tools import db
from tools.db import Reason, Sample, Settlement, WatchState

MARKET = "0x" + "aa" * 32
YES, NO = "71" * 38, "72" * 38
END = datetime(2027, 1, 1, tzinfo=timezone.utc)
KEYED_BY_THIS_BUILD = ("polymarket.keys", "polymarket.client", "polymarket.ticks", "tools.worker",
                       "tools.snapshot", "tools.restore", "tools.sniper", "tools.__main__")


def market(question="Will the Fed cut in March?", tokens=(YES, NO), tick=0.01, volume=12345.0,
           accepting=True):
    return Market(MARKET, question, "fed-cut-march", "Fed decisions", ("Yes", "No"),
                  tuple(tokens), END, volume, tick, False, accepting, 5.0)


def levels(bid, ask, tick=0.01):
    return Levels([(bid, 10.0)] if bid is not None else [], [(ask, 10.0)] if ask is not None
                  else [], tick)


class Sniper(Console):
    """The console over a scratch farm.db and tools.db, with a fake search and fake books."""

    def setUp(self):
        self.markets = []
        self.books = {}
        self.searches = []
        self.book_requests = []
        super().setUp()
        self.tools = db.open_db(self.tools_path)
        self.addCleanup(self.tools.close)

    def app_kwargs(self):
        # A fixed clock, so an age the page shows is exact.
        self.tools_path = os.path.join(os.path.dirname(self.path), "tools.db")
        return {"tools_db": self.tools_path, "search_markets": self.search,
                "fetch_books": self.fetch_books, "clock": lambda: self.now}

    def search(self, query):
        self.searches.append(query)
        if isinstance(self.markets, Exception):
            raise self.markets
        return self.markets

    def fetch_books(self, token_ids):
        self.book_requests.append(list(token_ids))
        if isinstance(self.books, Exception):
            raise self.books
        return {t: self.books[t] for t in token_ids if t in self.books}

    def post(self, url, **fields):
        return self.client.post(url, data={"console_token": self.app.state.token, **fields},
                                follow_redirects=False)

    def arm_form(self, **changes):
        fields = {"token_id": YES, "market": MARKET, "label": "Will the Fed cut in March? -> Yes",
                  "side": "BUY", "price": "0.999", "size": "100", "target_tick": "0.001",
                  "guard": "1", "guard_price": "0.99", "guard_tolerance": "0.005",
                  "guard_max_age_s": "5", "expires_after_s": "0"}
        fields.update(changes)
        return fields

    def arm(self, **changes):
        """A watch as the console would insert it, straight through tools.db."""
        parsed, errors = db.parse_watch(self.arm_form(**changes))
        assert not errors, errors
        return db.arm_watch(self.tools, parsed, self.now)

    def start_worker(self, keyed=True):
        return db.start_worker(self.tools, self.now, pid=41, keyed=keyed)

    def watch_rows(self):
        return db.watches(self.tools)


class LayoutTest(Sniper):
    def test_the_nav_links_to_the_sniper_and_marks_it_on_its_own_page(self):
        self.assertIn('<a href="/sniper" class="">Sniper</a>', self.client.get("/").text)
        self.assertIn('<a href="/sniper" class="on">Sniper</a>', self.client.get("/sniper").text)

    def test_the_page_shows_the_workers_banner_and_refreshes_its_watches_on_watch_events(self):
        text = self.client.get("/sniper").text
        self.assertIn('hx-get="/partials/chrome/worker-banner"', text)
        self.assertIn("The worker has never run against this database.", text)
        self.assertIn('hx-get="/partials/sniper/watches" hx-trigger="sse:watch, sse:status"', text)
        self.assertIn("No watches yet.", text)


class SearchTest(Sniper):
    def test_a_search_lists_each_outcome_with_its_live_book_and_a_pick_link(self):
        self.markets = [market()]
        self.books = {YES: levels(0.99, 0.995), NO: levels(0.005, 0.01)}
        text = self.client.get("/sniper?q=fed+cut").text
        self.assertEqual(self.searches, ["fed cut"])
        self.assertEqual(self.book_requests, [[YES, NO]])
        for needle in ("Will the Fed cut in March?", "Fed decisions", "<td>Yes</td>", "<td>No</td>",
                       '<td class="mono">99.0c</td>', '<td class="mono">99.5c</td>',
                       '<td class="mono">0.01</td>', "12.3k", '<span class="badge t-q">ready</span>',
                       '<span class="badge t-n">bid at 0.5c</span>',
                       f'href="/sniper/arm?token_id={YES}&amp;market={MARKET}&amp;label='
                       f'{quote("Will the Fed cut in March? -> Yes")}"'):
            self.assertIn(needle, text)
        self.assertLess(text.index("<td>Yes</td>"), text.index("<td>No</td>"),
                        "the ready outcome did not come first")
        self.assertIn('value="fed cut"', text)

    def test_a_search_that_fails_says_so_and_lists_nothing(self):
        self.markets = ConnectionError("gamma down")
        text = self.client.get("/sniper?q=fed").text
        self.assertIn("The search failed: ConnectionError: gamma down", text)
        self.assertNotIn("/sniper/arm?", text)

    def test_books_that_cannot_be_read_leave_the_results_without_prices(self):
        self.markets = [market()]
        self.books = ConnectionError("clob down")
        text = self.client.get("/sniper?q=fed").text
        self.assertIn("Will the Fed cut in March?", text)
        self.assertIn('<span class="badge t-n">no book</span>', text)
        self.assertIn("The books could not be read", text)

    def test_no_query_searches_nothing(self):
        self.client.get("/sniper")
        self.client.get("/sniper?q=+")
        self.assertEqual(self.searches, [])

    def test_a_search_with_no_open_market_says_so(self):
        self.assertIn("No open market matched", self.client.get("/sniper?q=zzz").text)


class ArmFormTest(Sniper):
    URL = f"/sniper/arm?token_id={YES}&market={MARKET}&label={quote('Will it? -> Yes')}"

    def test_the_form_carries_the_pick_the_old_defaults_and_the_live_book(self):
        self.books = {YES: levels(0.99, 0.995)}
        text = self.client.get(self.URL).text
        for needle in (f'name="token_id" value="{YES}"', f'name="market" value="{MARKET}"',
                       'name="label" value="Will it? -&gt; Yes"', "Will it? -&gt; Yes",
                       'name="price" value="0.999"', 'name="target_tick" value="0.001"',
                       'name="guard_price" value="0.99"', 'name="guard_tolerance" value="0.005"',
                       'name="guard_max_age_s" value="5"', 'name="expires_after_s" value="0"',
                       'name="guard" value="1" checked', 'name="dry_run" value="1"',
                       'placeholder="Type ARM to confirm"', "bid 99.0c", "ask 99.5c", "tick 0.01",
                       'name="confirm"', 'value="BUY" selected'):
            self.assertIn(needle, text)
        self.assertNotIn('name="dry_run" value="1" checked', text)
        self.assertEqual(self.book_requests, [[YES]])

    def test_the_form_needs_a_pick(self):
        self.assertEqual(self.client.get("/sniper/arm").status_code, 422)
        self.assertEqual(self.client.get(f"/sniper/arm?token_id={YES}").status_code, 422)

    def test_a_dry_run_arms_without_the_word_and_goes_back_to_the_page(self):
        resp = self.post("/sniper/arm", **self.arm_form(dry_run="1"))
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/sniper#watch-1"))
        (row,) = self.watch_rows()
        self.assertEqual((row.order.token_id, row.order.price, row.order.size, row.dry_run,
                          row.state, row.guard), (YES, 0.999, 100.0, True, WatchState.ARMED,
                                                  db.GUARD_DEFAULT))

    def test_a_live_arm_needs_the_typed_word_and_a_wrong_word_arms_nothing(self):
        for word in ("", "arm", "ARM NOW", "PLACE"):
            with self.subTest(word=word):
                resp = self.post("/sniper/arm", **self.arm_form(confirm=word))
                self.assertEqual(resp.status_code, 422)
                self.assertIn('role="alert">Type ARM to confirm.<', resp.text)
                self.assertIn('value="0.999"', resp.text)
        self.assertEqual(self.watch_rows(), [], "a live watch was armed without the typed word")

    def test_the_typed_word_arms_a_live_watch(self):
        resp = self.post("/sniper/arm", **self.arm_form(confirm=" ARM ", guard="",
                                                        expires_after_s="3600", side="sell",
                                                        price="0.001"))
        self.assertEqual(resp.status_code, 303)
        (row,) = self.watch_rows()
        self.assertEqual((row.dry_run, row.guard, row.order.expires_after_s, row.order.side.value,
                          row.disarm_requested_at), (False, None, 3600, "SELL", None))

    def test_bad_fields_are_shown_back_by_name_and_arm_nothing(self):
        resp = self.post("/sniper/arm", **self.arm_form(price="0.9995", size="0", confirm="ARM"))
        self.assertEqual(resp.status_code, 422)
        self.assertIn("A multiple of 0.001", resp.text)
        self.assertIn("A positive number of shares.", resp.text)
        self.assertIn('name="size" value="0"', resp.text)
        self.assertEqual(self.watch_rows(), [])

    def test_a_token_with_an_open_watch_is_refused(self):
        self.arm()
        resp = self.post("/sniper/arm", **self.arm_form(dry_run="1"))
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already has an open watch", resp.text)
        self.assertEqual(len(self.watch_rows()), 1)

    def test_arming_without_the_console_token_is_refused(self):
        resp = self.client.post("/sniper/arm", data=self.arm_form(dry_run="1"))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.watch_rows(), [])


class WatchListTest(Sniper):
    def test_an_armed_watch_shows_its_order_its_state_and_what_it_waits_for(self):
        watch_id = self.arm()
        for url in ("/sniper", "/partials/sniper/watches"):
            with self.subTest(url=url):
                text = self.client.get(url).text
                for needle in (f'id="watch-{watch_id}"', "Will the Fed cut in March? -&gt; Yes",
                               '<span class="badge t-c">armed</span>', "BUY 100 @ 99.9c",
                               "GTC", "guard: bid 99.0c &#177; 0.5c within 5s", "target 0.001",
                               "Armed. Waiting for a worker.",
                               f'hx-post="/sniper/{watch_id}/disarm"'):
                    self.assertIn(needle, text)
                self.assertNotIn("dry run", text)

    def test_the_workers_published_quote_and_its_age_are_shown(self):
        watch_id = self.arm()
        self.start_worker()
        db.publish_samples(self.tools, [Sample(watch_id, "0.01", 0.99, 0.995,
                                               self.now - timedelta(seconds=3))])
        text = self.client.get("/partials/sniper/watches").text
        self.assertIn("Armed. Tick 0.01, bid 99.0c, ask 99.5c, seen 3s ago.", text)
        self.assertIn('<td class="mono">0.01</td><td class="mono">99.0c</td>'
                      '<td class="mono">99.5c</td><td class="mono">3s</td>', text)

    def test_a_live_watch_under_a_keyless_worker_says_it_waits_for_a_key(self):
        self.arm()
        self.arm(token_id=NO, dry_run="1")
        self.start_worker(keyed=False)
        text = self.client.get("/partials/sniper/watches").text
        self.assertIn("Waiting for a keyed worker: the running worker holds no key.", text)
        self.assertIn("Armed. The worker has not read the book yet.", text)
        self.assertIn('<span class="badge t-n">dry run</span>', text)

    def test_a_closed_watch_shows_its_outcome_its_log_and_no_disarm(self):
        watch_id = self.arm()
        db.mark_firing(self.tools, watch_id, self.now, "feed", "0.001", 0.99, 0.995, self.now)
        db.append_log(self.tools, self.now, "tick 0.001 seen by the feed; posting",
                      watch_id=watch_id)
        db.close_watch(self.tools, watch_id, Settlement(WatchState.FIRED, None, "", "0xnew", 2),
                       self.now, send_ms=0.4, post_ms=180.0)
        db.append_log(self.tools, self.now, "fired: order 0xnew accepted on attempt 2",
                      watch_id=watch_id)
        text = self.client.get("/partials/sniper/watches").text
        for needle in ('<span class="badge t-q">fired</span>',
                       "Fired. Order 0xnew rests after 2 attempts.", "0xnew",
                       "tick 0.001 seen by the feed; posting",
                       "fired: order 0xnew accepted on attempt 2", "180 ms", "0.4 ms", "feed"):
            self.assertIn(needle, text)
        self.assertNotIn(f'hx-post="/sniper/{watch_id}/disarm"', text)

    def test_each_closed_state_has_its_tone(self):
        cases = [(WatchState.REHEARSED, None, "t-q"), (WatchState.REJECTED, Reason.GUARD_PRICE,
                                                         "t-n"),
                 (WatchState.MISSED, Reason.AT_TARGET, "t-n"),
                 (WatchState.FAILED, Reason.UNANSWERED, "t-r"), (WatchState.DISARMED, None, "t-n")]
        for n, (state, reason, tone) in enumerate(cases):
            watch_id = self.arm(token_id=f"{n}" * 38, dry_run="1" if state is
                                WatchState.REHEARSED else "")
            db.close_watch(self.tools, watch_id, Settlement(state, reason, "x"), self.now)
        text = self.client.get("/partials/sniper/watches").text
        for state, _, tone in cases:
            self.assertIn(f'<span class="badge {tone}">{state.value}</span>', text)
        self.assertIn("An order may rest: check your open orders.", text)

    def test_open_watches_come_first(self):
        closed = self.arm()
        db.close_watch(self.tools, closed, Settlement(WatchState.DISARMED, None, ""), self.now)
        armed = self.arm(token_id=NO)
        text = self.client.get("/partials/sniper/watches").text
        self.assertLess(text.index(f'id="watch-{armed}"'), text.index(f'id="watch-{closed}"'))


class DisarmTest(Sniper):
    def test_a_disarm_asks_the_worker_and_the_page_says_so(self):
        watch_id = self.arm()
        resp = self.post(f"/sniper/{watch_id}/disarm")
        self.assertEqual(resp.status_code, 200)
        row = db.read_watch(self.tools, watch_id)
        self.assertEqual((row.state, row.disarm_requested_at), (WatchState.ARMED, self.now))
        self.assertIn("Disarm requested. Waiting for the worker to answer.", resp.text)
        self.assertNotIn(f'hx-post="/sniper/{watch_id}/disarm"', resp.text)

    def test_a_disarm_of_a_closed_or_unknown_watch_changes_nothing(self):
        watch_id = self.arm()
        db.close_watch(self.tools, watch_id, Settlement(WatchState.DISARMED, None, ""), self.now)
        self.assertEqual(self.post(f"/sniper/{watch_id}/disarm").status_code, 200)
        self.assertIsNone(db.read_watch(self.tools, watch_id).disarm_requested_at)
        self.assertEqual(self.post("/sniper/99/disarm").status_code, 404)

    def test_a_disarm_without_the_console_token_is_refused(self):
        watch_id = self.arm()
        self.assertEqual(self.client.post(f"/sniper/{watch_id}/disarm").status_code, 403)
        self.assertIsNone(db.read_watch(self.tools, watch_id).disarm_requested_at)


class NotConfiguredTest(Console):
    def test_the_sniper_page_says_so_and_nothing_can_be_armed(self):
        text = self.client.get("/sniper").text
        self.assertIn("The worker database is not configured", text)
        self.assertNotIn("<form", text)
        self.assertEqual(self.client.get("/sniper/arm?token_id=1&market=2&label=3").status_code,
                         404)
        resp = self.client.post("/sniper/arm", data={"console_token": self.app.state.token})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.client.get("/partials/sniper/watches").status_code, 404)


class StreamTest(Sniper):
    def read_stream(self, during):
        waits = []

        async def wait(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                with closing(db.open_db(self.tools_path)) as conn:
                    during(conn)
            await asyncio.sleep(0)

        app = create_app(self.path, max_polls=3, tools_db=self.tools_path)
        with mock.patch.object(stream, "asyncio", SimpleNamespace(sleep=wait)):
            return TestClient(app, base_url=ORIGIN).get("/stream").text

    def test_an_armed_watch_and_a_published_quote_send_a_watch_event(self):
        text = self.read_stream(lambda conn: db.arm_watch(
            conn, db.parse_watch(self.arm_form())[0], self.now))
        self.assertEqual(text.count("event: watch\n"), 1)
        self.assertNotIn("event: job", text)
        text = self.read_stream(lambda conn: db.publish_samples(
            conn, [Sample(1, "0.01", 0.99, 0.995, self.now)]))
        self.assertEqual(text.count("event: watch\n"), 1)

    def test_a_quiet_watch_sends_none(self):
        self.arm()
        self.assertNotIn("event: watch", self.read_stream(lambda conn: None))


class ImportTest(unittest.TestCase):
    def test_the_console_loads_the_picker_and_no_sniper_worker_key_loader_or_client(self):
        loaded = loaded_by(["farm_console.app"])
        self.assertIn("tools.picker", loaded)
        self.assertEqual([name for name in loaded if name in KEYED_BY_THIS_BUILD], [])


if __name__ == "__main__":
    unittest.main()
