"""Tests for the console's Orders page and the worker's place in the layout. The
worker's rows are written straight through tools.db: no worker runs here."""

import asyncio
import os
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from farm_console import views
from farm_console.app import create_app
from farm_console.pages import stream
from polymarket.orders import OpenOrder, Side
from test_farm_console import ORIGIN, Console
from test_polymarket_imports import loaded_by
from tools import db
from tools.db import JobKind, JobState

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
MARKET = "0x" + "aa" * 32
KEYED_BY_THIS_BUILD = ("polymarket.keys", "polymarket.client", "tools.worker", "tools.snapshot",
                       "tools.__main__")


def worker(beat=NOW, stopped=None, keyed=True):
    return db.WorkerStatus("instance", 41, keyed, NOW - timedelta(hours=1), beat, stopped)


class WorkerLivenessTest(unittest.TestCase):
    def test_running_while_it_beats(self):
        live = views.worker_liveness(worker(beat=NOW - timedelta(seconds=3)), NOW)
        self.assertEqual((live.configured, live.running, live.keyed, live.message),
                         (True, True, True, ""))

    def test_stale_says_how_long_ago_it_last_beat(self):
        live = views.worker_liveness(worker(beat=NOW - timedelta(seconds=95)), NOW)
        self.assertFalse(live.running)
        self.assertIn("last heartbeat was 1m 35s ago", live.message)

    def test_stopped_is_not_running_even_when_recent(self):
        live = views.worker_liveness(worker(stopped=NOW - timedelta(seconds=2)), NOW)
        self.assertFalse(live.running)
        self.assertIn("stopped 2s ago", live.message)

    def test_never_ran(self):
        live = views.worker_liveness(None, NOW)
        self.assertEqual((live.configured, live.running), (True, False))
        self.assertIn("never run", live.message)

    def test_no_database_is_not_configured(self):
        self.assertEqual((views.NO_WORKER.configured, views.NO_WORKER.running), (False, False))


class Orders(Console):
    """The console over a scratch farm.db and a scratch tools.db beside it."""

    def setUp(self):
        super().setUp()
        self.tools = db.open_db(self.tools_path)
        self.addCleanup(self.tools.close)

    def app_kwargs(self):
        self.tools_path = os.path.join(os.path.dirname(self.path), "tools.db")
        return {"tools_db": self.tools_path}

    def post(self, url, **fields):
        return self.client.post(url, data={"console_token": self.app.state.token, **fields},
                                follow_redirects=False)

    def start_worker(self, keyed=True):
        return db.start_worker(self.tools, self.now, pid=41, keyed=keyed)

    def take(self, note="before the upgrade"):
        """What the worker writes for one snapshot job: two orders, one of them named."""
        job = db.enqueue_job(self.tools, JobKind.SNAPSHOT, {"note": note}, self.now)
        db.claim_job(self.tools, self.now)
        orders = [
            OpenOrder("0xo1", "token-1", MARKET, "No", Side.BUY, 0.986, 2000.0, 150.5,
                      self.now + timedelta(days=3)),
            OpenOrder("0xo2", "token-2", "0x" + "bb" * 32, "Yes", Side.SELL, 0.015, 60.0, 0.0,
                      None)]
        snapshot = db.insert_snapshot(self.tools, self.now, note, orders,
                                      {MARKET: "Will alpha happen?"}, job_id=job)
        db.append_log(self.tools, self.now, f"stored snapshot {snapshot} with 2 open orders",
                      job_id=job)
        db.finish_job(self.tools, job, self.now)
        return job, snapshot, orders


class LayoutTest(Orders):
    def test_the_nav_links_to_orders_and_marks_it_on_its_own_page(self):
        self.assertIn('<a href="/orders" class="">Orders</a>', self.client.get("/").text)
        self.assertIn('<a href="/orders" class="on">Orders</a>', self.client.get("/orders").text)

    def test_the_sidebar_lists_the_farmer_and_the_worker(self):
        text = self.client.get("/partials/chrome/status").text
        self.assertIn("Farmer · DRY RUN", text)
        self.assertIn("Worker · never ran", text)
        self.start_worker()
        text = self.client.get("/partials/chrome/status").text
        self.assertIn("Worker · keyed", text)
        self.assertIn(f"beat {views.clock(self.now)}", text)
        self.assertNotIn("stopped", text)

    def test_the_sidebar_says_when_the_worker_has_no_key_or_stopped(self):
        instance = self.start_worker(keyed=False)
        status = "/partials/chrome/status"
        self.assertIn("Worker · no key", self.client.get(status).text)
        db.stop_worker(self.tools, instance, self.now)
        self.assertIn("Worker · no key · stopped", self.client.get(status).text)

    def test_a_tool_page_says_the_worker_is_not_running_and_how_to_start_it(self):
        for url in ("/orders", "/partials/chrome/worker-banner"):
            with self.subTest(url=url):
                text = self.client.get(url).text
                self.assertIn("The worker has never run against this database.", text)
                self.assertIn(f"python -m tools --db {self.tools_path}", text)
        instance = self.start_worker()
        self.assertNotIn("Not running", self.client.get("/orders").text)
        db.stop_worker(self.tools, instance, self.now)
        self.assertIn("The worker is not running. It stopped", self.client.get("/orders").text)

    def test_a_tool_page_shows_the_workers_banner_and_a_farm_page_the_farmers(self):
        self.publish(stopped=self.now)
        orders, overview = self.client.get("/orders").text, self.client.get("/").text
        self.assertNotIn("The farmer is not running", orders)
        self.assertIn('hx-get="/partials/chrome/worker-banner"', orders)
        self.assertIn("The farmer is not running. It stopped", overview)
        self.assertNotIn("The worker has never run", overview)
        self.assertIn('hx-get="/partials/chrome/banner"', overview)

    def test_the_page_refreshes_its_regions_on_job_events(self):
        text = self.client.get("/orders").text
        self.assertIn('hx-get="/partials/orders/job" hx-trigger="sse:job, sse:status"', text)
        self.assertIn('hx-get="/partials/orders/snapshots" hx-trigger="sse:job"', text)


class TakeSnapshotTest(Orders):
    def test_a_job_is_queued_with_the_worker_down_and_the_page_says_it_waits(self):
        resp = self.post("/orders/snapshot", note="  before the upgrade ")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "/orders"))
        job = db.latest_job(self.tools, JobKind.SNAPSHOT)
        self.assertEqual((job.state, job.params), (JobState.QUEUED, {"note": "before the upgrade"}))
        text = self.client.get("/orders").text
        for needle in ("Snapshot job 1", '<span class="badge t-n">queued</span>',
                       "before the upgrade", "waits for the worker, which is not running",
                       "The worker has never run against this database."):
            self.assertIn(needle, text)

    def test_a_queued_job_does_not_say_it_waits_once_the_worker_runs(self):
        self.post("/orders/snapshot")
        self.start_worker()
        self.assertNotIn("waits for the worker", self.client.get("/partials/orders/job").text)

    def test_the_note_is_optional(self):
        self.assertEqual(self.post("/orders/snapshot").status_code, 303)
        self.assertEqual(db.latest_job(self.tools, JobKind.SNAPSHOT).params, {"note": ""})

    def test_a_post_without_the_console_token_is_refused_and_queues_nothing(self):
        for data, headers in (({"note": "x"}, {}), ({"note": "x", "console_token": "guess"}, {}),
                              ({"note": "x"}, {"X-Console-Token": "guess"})):
            with self.subTest(data=data, headers=headers):
                resp = self.client.post("/orders/snapshot", data=data, headers=headers)
                self.assertEqual(resp.status_code, 403)
        resp = self.client.post("/orders/snapshot", headers={"Origin": "http://evil.example"},
                                data={"console_token": self.app.state.token})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNone(db.latest_job(self.tools, JobKind.SNAPSHOT))

    def test_a_second_snapshot_job_is_refused_and_the_page_says_which_one_is_open(self):
        self.post("/orders/snapshot", note="first")
        resp = self.post("/orders/snapshot", note="second")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("A snapshot job is already queued (job 1).", resp.text)
        self.assertEqual(self.tools.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_a_note_that_is_too_long_is_shown_back_and_queues_nothing(self):
        resp = self.post("/orders/snapshot", note="n" * 201)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("at most 200 characters", resp.text)
        self.assertIn(f'value="{"n" * 201}"', resp.text)
        self.assertIsNone(db.latest_job(self.tools, JobKind.SNAPSHOT))
        self.assertEqual(self.post("/orders/snapshot", note="n" * 200).status_code, 303)


class SnapshotsTest(Orders):
    def test_a_finished_job_shows_its_state_its_log_and_its_snapshot(self):
        job, snapshot, _ = self.take()
        for url in ("/orders", "/partials/orders/job"):
            with self.subTest(url=url):
                text = self.client.get(url).text
                for needle in (f"Snapshot job {job}", '<span class="badge t-q">done</span>',
                               f"stored snapshot {snapshot} with 2 open orders",
                               f'href="/orders/snapshots/{snapshot}"'):
                    self.assertIn(needle, text)

    def test_a_failed_job_shows_its_error(self):
        job = db.enqueue_job(self.tools, JobKind.SNAPSHOT, {"note": ""}, self.now)
        db.claim_job(self.tools, self.now)
        db.finish_job(self.tools, job, self.now, error="ConnectionError: timed out")
        text = self.client.get("/partials/orders/job").text
        self.assertIn('<span class="badge t-r">failed</span>', text)
        self.assertIn('role="alert">ConnectionError: timed out<', text)

    def test_the_list_shows_when_each_snapshot_was_taken_its_note_and_its_order_count(self):
        _, snapshot, _ = self.take("before the upgrade")
        old = db.insert_snapshot(self.tools, self.now - timedelta(days=80), "", [], {},
                                 source="orders_snapshot_20260718_221655.json")
        for url in ("/orders", "/partials/orders/snapshots"):
            with self.subTest(url=url):
                text = self.client.get(url).text
                self.assertIn(f'<a href="/orders/snapshots/{snapshot}">'
                              f'{views.stamp_local(self.now)}</a>', text)
                self.assertIn("before the upgrade", text)
                self.assertIn('<td class="mono">2</td>', text)
                self.assertIn("orders_snapshot_20260718_221655.json", text)
                self.assertLess(text.index(f"/orders/snapshots/{snapshot}"),
                                text.index(f"/orders/snapshots/{old}"))

    def test_no_snapshots_yet(self):
        self.assertIn("No snapshots yet.", self.client.get("/partials/orders/snapshots").text)

    def test_a_snapshots_page_lists_its_orders(self):
        _, snapshot, orders = self.take()
        text = self.client.get(f"/orders/snapshots/{snapshot}").text
        expires = views.stamp_local(orders[0].expiration)
        for needle in (f"Snapshot {snapshot}", "before the upgrade", "2 orders",
                       "<th>Market</th><th>Outcome</th><th>Side</th><th>Price</th><th>Size</th>"
                       "<th>Matched</th><th>Expiration</th>",
                       '<td class="wrap">Will alpha happen?</td><td>No</td><td class="mono">BUY'
                       '</td><td class="mono">98.6c</td><td class="mono">2,000</td>'
                       f'<td class="mono">150.5</td><td class="mono">{expires}</td>',
                       f'<td class="wrap mono muted">{"0x" + "bb" * 32}</td><td>Yes</td>'
                       '<td class="mono">SELL</td><td class="mono">1.5c</td><td class="mono">60'
                       '</td><td class="mono">0</td><td class="mono">GTC</td>',
                       '<a href="/orders" class="on">Orders</a>'):
            self.assertIn(needle, text)

    def test_an_unknown_snapshot_or_region_is_refused(self):
        self.assertEqual(self.client.get("/orders/snapshots/99").status_code, 404)
        self.assertEqual(self.client.get("/partials/orders/nope").status_code, 404)


class NotConfiguredTest(Console):
    """A console started with no tools.db, as the farm console's own tests start it."""

    def test_the_orders_page_says_so_and_offers_no_form(self):
        text = self.client.get("/orders").text
        self.assertIn("The worker database is not configured", text)
        self.assertIn("--tools-db", text)
        self.assertNotIn("<form", text)

    def test_the_sidebar_says_so_and_farm_pages_show_no_worker_banner(self):
        self.assertIn("Worker · not configured", self.client.get("/partials/chrome/status").text)
        self.assertNotIn("Not running", self.client.get("/").text)

    def test_nothing_can_be_queued_or_read(self):
        resp = self.client.post("/orders/snapshot", data={"console_token": self.app.state.token})
        self.assertEqual(resp.status_code, 404)
        for url in ("/orders/snapshots/1", "/partials/orders/job", "/partials/orders/snapshots"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)


class StreamTest(Orders):
    def read_stream(self, during=lambda conn: None):
        """The stream's text over three polls. `during` writes to tools.db in the
        first wait between polls, after the stream has read where it starts
        from. The waits take no time."""
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

    def test_a_queued_job_and_each_line_it_logs_send_a_job_event(self):
        text = self.read_stream(
            lambda conn: db.enqueue_job(conn, JobKind.SNAPSHOT, {"note": ""}, self.now))
        self.assertEqual(text.count("event: job\n"), 1)
        db.claim_job(self.tools, self.now)
        text = self.read_stream(lambda conn: db.append_log(conn, self.now, "listed", job_id=1))
        self.assertEqual(text.count("event: job\n"), 1)

    def test_a_worker_that_starts_sends_a_status_event(self):
        text = self.read_stream(lambda conn: db.start_worker(conn, self.now, 41, True))
        self.assertIn("event: status\ndata: worker running\n", text)
        self.assertNotIn("event: job", text)

    def test_a_quiet_worker_sends_no_signals(self):
        self.start_worker()
        self.take()
        self.assertNotIn("event:", self.read_stream())


class ImportTest(unittest.TestCase):
    def test_the_console_loads_no_key_loader_no_client_and_no_worker(self):
        # reward_farmer, which the console imports for the farmer's defaults,
        # loads py_clob_client_v2, so the SDK itself cannot be ruled out here.
        loaded = loaded_by(["farm_console.app"])
        self.assertIn("tools.db", loaded)
        self.assertEqual([name for name in loaded if name in KEYED_BY_THIS_BUILD], [])


if __name__ == "__main__":
    unittest.main()
