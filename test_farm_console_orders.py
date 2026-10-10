import asyncio
import os
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import farm_db
from farm_console import views
from farm_console.app import create_app
from farm_console.pages import stream
from polymarket.orders import OpenOrder, Side
from test_farm_console import A, B, C, ORIGIN, Console
from test_polymarket_imports import loaded_by
from tools import db
from tools.db import JobKind, JobState, RestoreAction

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
MARKET = "0x" + "aa" * 32
KEYED_BY_THIS_BUILD = ("polymarket.keys", "polymarket.client", "polymarket.ticks", "tools.worker",
                       "tools.snapshot", "tools.restore", "tools.__main__")


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


class RestoreTest(Orders):
    def setUp(self):
        super().setUp()
        _, self.snapshot, _ = self.take()
        self.url = f"/orders/snapshots/{self.snapshot}"
        self.stored = db.read_snapshot_orders(self.tools, self.snapshot)

    def ran(self, kind, lines, state=JobState.DONE, error=None):
        job = db.enqueue_job(self.tools, kind, {"snapshot_id": self.snapshot,
                                                "managed_markets": []}, self.now)
        if state is not JobState.QUEUED:
            db.claim_job(self.tools, self.now)
            db.add_restore_lines(self.tools, [
                db.RestoreLine(job, self.stored[index], action, self.stored[index].order.remaining,
                               order_id, detail)
                for index, action, order_id, detail in lines])
        if state in (JobState.DONE, JobState.FAILED):
            db.finish_job(self.tools, job, self.now, error)
        return job

    def previewed(self):
        return self.ran(JobKind.PREVIEW, [
            (0, RestoreAction.WOULD_PLACE, None, "150.5 of 2000 had filled"),
            (1, RestoreAction.SKIP_LIVE, None, "still open")])

    def restore_jobs(self):
        rows = self.tools.execute("SELECT id FROM jobs WHERE kind = 'restore'")
        return [row["id"] for row in rows]

    def test_a_snapshots_page_offers_a_preview_and_no_placing_before_one(self):
        text = self.client.get(self.url).text
        self.assertIn(f'action="{self.url}/preview"', text)
        self.assertIn("Preview restore</button>", text)
        self.assertNotIn(f'action="{self.url}/restore"', text)
        self.assertIn(f'hx-get="/partials/orders/restore/{self.snapshot}" '
                      'hx-trigger="sse:job, sse:status"', text)

    def test_preview_queues_a_job_with_the_markets_the_farmer_manages_at_the_click(self):
        resp = self.post(f"{self.url}/preview")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, self.url))
        job = db.latest_restore_job(self.tools, self.snapshot)
        self.assertEqual((job.kind, job.state, job.params), (JobKind.PREVIEW, JobState.QUEUED, {
            "snapshot_id": self.snapshot, "managed_markets": sorted([A, B])}))
        db.claim_job(self.tools, self.now)
        db.finish_job(self.tools, job.id, self.now)
        farm_db.approve_market(self.db, C, 0.99, 25.0, now=self.now)
        farm_db.mark_retired(self.db, A, "closed")
        self.post(f"{self.url}/preview")
        self.assertEqual(db.latest_restore_job(self.tools, self.snapshot).params["managed_markets"],
                         sorted([B, C]))

    def test_a_queued_preview_says_it_waits_for_a_worker_that_is_not_running(self):
        self.post(f"{self.url}/preview")
        text = self.client.get(self.url).text
        for needle in ("Preview job 2", '<span class="badge t-n">queued</span>',
                       "waits for the worker, which is not running"):
            self.assertIn(needle, text)
        self.start_worker()
        self.assertNotIn("waits for the worker",
                         self.client.get(f"/partials/orders/restore/{self.snapshot}").text)

    def test_a_preview_or_a_restore_is_refused_while_one_is_open(self):
        self.post(f"{self.url}/preview")
        for action, fields, said in (
                ("preview", {}, "A preview job is already queued (job 2)."),
                ("restore", {"confirm": "PLACE"}, "Preview job 2 is still queued.")):
            with self.subTest(action=action):
                resp = self.post(f"{self.url}/{action}", **fields)
                self.assertEqual(resp.status_code, 409)
                self.assertIn(said, resp.text)
        self.assertEqual(self.tools.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_a_preview_shows_what_a_restore_would_do_and_never_says_placed(self):
        job = self.previewed()
        for url in (self.url, f"/partials/orders/restore/{self.snapshot}"):
            with self.subTest(url=url):
                text = self.client.get(url).text
                for needle in (f"Preview job {job}", '<span class="badge t-q">done</span>',
                               "<h2>Would place</h2>", "<h2>Skipped: already open</h2>",
                               "150.5 of 2000 had filled", "still open", "Will alpha happen?",
                               '<td class="mono">1,849.5</td>',
                               f'action="{self.url}/restore"', 'name="confirm"',
                               "Place 1 order</button>"):
                    self.assertIn(needle, text)
                self.assertNotIn("Placed", text)

    def test_a_preview_with_nothing_to_place_offers_no_placing(self):
        self.ran(JobKind.PREVIEW, [(0, RestoreAction.SKIP_EXPIRED, None, "expired"),
                                   (1, RestoreAction.SKIP_LIVE, None, "still open")])
        text = self.client.get(self.url).text
        self.assertIn("<h2>Skipped: expired</h2>", text)
        self.assertIn("Nothing to place", text)
        self.assertNotIn(f'action="{self.url}/restore"', text)

    def test_placing_needs_the_typed_word_and_a_wrong_word_queues_nothing(self):
        self.previewed()
        for word in ("", "place", "PLACE NOW", "CANCEL"):
            with self.subTest(word=word):
                resp = self.post(f"{self.url}/restore", confirm=word)
                self.assertEqual(resp.status_code, 422)
                self.assertIn('role="alert">Type PLACE to confirm.<', resp.text)
                self.assertIn("Place 1 order</button>", resp.text)
        self.assertEqual(self.restore_jobs(), [], "a restore was queued without the typed word")

    def test_the_typed_word_queues_a_restore_with_the_markets_the_farmer_manages(self):
        self.previewed()
        resp = self.post(f"{self.url}/restore", confirm=" PLACE ")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, self.url))
        job = db.latest_restore_job(self.tools, self.snapshot)
        self.assertEqual((job.kind, job.state, job.params), (JobKind.RESTORE, JobState.QUEUED, {
            "snapshot_id": self.snapshot, "managed_markets": sorted([A, B])}))

    def test_a_queued_restore_says_how_long_it_may_wait_for_a_worker(self):
        self.previewed()
        self.post(f"{self.url}/restore", confirm="PLACE")
        text = self.client.get(self.url).text
        self.assertIn("waits for the worker, which is not running", text)
        self.assertIn(f"waits longer than {views.span(db.RESTORE_MAX_WAIT.total_seconds())} "
                      "is not run", text)

    def test_placing_without_the_console_token_is_refused(self):
        self.previewed()
        resp = self.client.post(f"{self.url}/restore", data={"confirm": "PLACE"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.restore_jobs(), [])

    def test_placing_needs_a_finished_preview_as_the_last_thing_that_ran(self):
        def refused(why):
            resp = self.post(f"{self.url}/restore", confirm="PLACE")
            self.assertEqual(resp.status_code, 409, why)
            self.assertIn("Preview the restore first", resp.text)

        refused("no preview yet")
        self.ran(JobKind.PREVIEW, [], state=JobState.FAILED, error="ConnectionError: timed out")
        refused("the preview failed")
        self.previewed()
        self.ran(JobKind.RESTORE, [(0, RestoreAction.PLACED, "0xnew", "not open")])
        refused("the preview was already placed")
        self.assertEqual(len(self.restore_jobs()), 1)

    def test_a_preview_of_another_snapshot_does_not_allow_placing_this_one(self):
        other = db.insert_snapshot(self.tools, self.now, "", [o.order for o in self.stored], {})
        self.previewed()
        resp = self.post(f"/orders/snapshots/{other}/restore", confirm="PLACE")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.restore_jobs(), [])

    def test_a_restore_shows_what_became_of_each_order(self):
        self.previewed()
        job = self.ran(JobKind.RESTORE, [
            (0, RestoreAction.PLACED, "0xnew1", "150.5 of 2000 had filled"),
            (1, RestoreAction.REJECTED, None, "not enough balance / allowance")])
        text = self.client.get(self.url).text
        for needle in (f"Restore job {job}", "<h2>Placed</h2>", '<td class="mono">0xnew1</td>',
                       "<h2>Rejected by the exchange</h2>", "not enough balance / allowance",
                       "Preview restore</button>"):
            self.assertIn(needle, text)
        self.assertNotIn("Would place", text)
        self.assertNotIn(f'action="{self.url}/restore"', text)

    def test_an_order_that_got_no_answer_reads_as_an_error_that_may_rest(self):
        detail = "no answer (TimeoutError: timed out): the order may rest: check your open orders"
        self.ran(JobKind.RESTORE, [(0, RestoreAction.ERROR, None, detail),
                                   (1, RestoreAction.SKIP_FARMER, None,
                                    "the farmer manages this market")])
        text = self.client.get(self.url).text
        for needle in ("<h2>Error</h2>", detail, "<h2>Skipped: the farmer&#39;s market</h2>",
                       "the farmer manages this market"):
            self.assertIn(needle, text)
        self.assertNotIn("<h2>Placed</h2>", text)

    def test_a_running_restore_shows_how_far_it_is(self):
        self.ran(JobKind.RESTORE, [(0, RestoreAction.PLACED, "0xnew1", "not open")],
                 state=JobState.RUNNING)
        text = self.client.get(f"/partials/orders/restore/{self.snapshot}").text
        self.assertIn('<span class="badge t-c">running</span>', text)
        self.assertIn("1 of 2 orders so far", text)

    def test_a_stopped_restore_shows_its_error_and_the_lines_it_stored(self):
        error = "the worker stopped after 1 of 2 orders (1 placed)"
        self.ran(JobKind.RESTORE, [(0, RestoreAction.PLACED, "0xnew1", "not open")],
                 state=JobState.FAILED, error=error)
        text = self.client.get(self.url).text
        for needle in ('<span class="badge t-r">failed</span>', f'role="alert">{error}<',
                       "<h2>Placed</h2>", "0xnew1"):
            self.assertIn(needle, text)

    def test_an_unknown_snapshot_is_refused_everywhere(self):
        self.assertEqual(self.client.get("/partials/orders/restore/99").status_code, 404)
        for action in ("preview", "restore"):
            with self.subTest(action=action):
                resp = self.post(f"/orders/snapshots/99/{action}", confirm="PLACE")
                self.assertEqual(resp.status_code, 404)
        self.assertIsNone(db.latest_restore_job(self.tools, 99))


class NotConfiguredTest(Console):
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
        for url in ("/orders/snapshots/1", "/partials/orders/job", "/partials/orders/snapshots",
                    "/partials/orders/restore/1"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        for action in ("preview", "restore"):
            with self.subTest(action=action):
                resp = self.client.post(f"/orders/snapshots/1/{action}", data={
                    "console_token": self.app.state.token, "confirm": "PLACE"})
                self.assertEqual(resp.status_code, 404)


class StreamTest(Orders):
    def read_stream(self, during=lambda conn: None):
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
    def test_the_console_loads_no_keys_no_client_and_no_worker(self):
        loaded = loaded_by(["farm_console.app"])
        self.assertIn("tools.db", loaded)
        self.assertEqual([name for name in loaded if name in KEYED_BY_THIS_BUILD], [])


if __name__ == "__main__":
    unittest.main()
