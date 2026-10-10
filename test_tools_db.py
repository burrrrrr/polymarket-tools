import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from polymarket.orders import OpenOrder, Side
from tools import db
from tools.db import JobKind, JobState, RestoreAction

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
MARKET_A, MARKET_B = "0x" + "aa" * 32, "0x" + "bb" * 32


def later(seconds):
    return NOW + timedelta(seconds=seconds)


def order(order_id="0xo1", market=MARKET_A, side=Side.BUY, price=0.98, size=100.0,
          matched=0.0, expiration=None):
    return OpenOrder(order_id, f"token-{market[-2:]}", market, "No", side, price, size,
                     matched, expiration)


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "tools.db")
        self.db = db.open_db(self.path)
        self.addCleanup(self.db.close)


class OpenTests(Scratch):
    def test_opening_again_keeps_the_rows_and_the_write_ahead_log(self):
        job = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": "kept"}, NOW)
        again = db.open_db(self.path)
        self.addCleanup(again.close)
        self.assertEqual(db.read_job(again, job).params, {"note": "kept"})
        self.assertEqual(again.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class WorkerStatusTests(Scratch):
    def test_no_worker_has_ever_run(self):
        self.assertIsNone(db.read_worker_status(self.db))
        self.assertFalse(db.is_running(None, NOW))

    def test_a_started_worker_is_running_until_it_stops(self):
        instance = db.start_worker(self.db, NOW, pid=41, keyed=True)
        status = db.read_worker_status(self.db)
        self.assertEqual((status.pid, status.keyed, status.started_at, status.last_beat_at,
                          status.stopped_at), (41, True, NOW, NOW, None))
        self.assertTrue(db.is_running(status, later(1)))
        self.assertTrue(db.beat(self.db, instance, later(2)))
        self.assertEqual(db.read_worker_status(self.db).last_beat_at, later(2))
        db.stop_worker(self.db, instance, later(3))
        stopped = db.read_worker_status(self.db)
        self.assertEqual(stopped.stopped_at, later(3))
        self.assertFalse(db.is_running(stopped, later(3)))

    def test_a_worker_without_a_key_says_so(self):
        db.start_worker(self.db, NOW, pid=41, keyed=False)
        self.assertIs(db.read_worker_status(self.db).keyed, False)

    def test_a_worker_whose_beats_stopped_is_not_running(self):
        db.start_worker(self.db, NOW, pid=41, keyed=True)
        status = db.read_worker_status(self.db)
        self.assertTrue(db.is_running(status, later(db.STALE_AFTER_S - 1)))
        self.assertFalse(db.is_running(status, later(db.STALE_AFTER_S)))

    def test_a_second_worker_is_refused_while_the_first_one_beats(self):
        first = db.start_worker(self.db, NOW, pid=41, keyed=True)
        other = db.open_db(self.path)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(db.WorkerRunning, "pid 41"):
            db.start_worker(other, later(db.STALE_AFTER_S - 1), pid=42, keyed=False)
        status = db.read_worker_status(self.db)
        self.assertEqual((status.pid, status.keyed, status.started_at), (41, True, NOW))
        self.assertTrue(db.beat(self.db, first, later(db.STALE_AFTER_S)))

    def test_a_worker_starts_after_the_last_one_stopped(self):
        first = db.start_worker(self.db, NOW, pid=41, keyed=True)
        db.stop_worker(self.db, first, later(1))
        db.start_worker(self.db, later(2), pid=42, keyed=True)
        status = db.read_worker_status(self.db)
        self.assertEqual((status.pid, status.started_at, status.stopped_at),
                         (42, later(2), None))

    def test_a_worker_that_was_replaced_cannot_beat_or_stop_its_successor(self):
        crashed = db.start_worker(self.db, NOW, pid=41, keyed=True)
        taken_over = later(db.STALE_AFTER_S)
        successor = db.start_worker(self.db, taken_over, pid=42, keyed=True)
        self.assertFalse(db.beat(self.db, crashed, later(60)))
        db.stop_worker(self.db, crashed, later(61))
        status = db.read_worker_status(self.db)
        self.assertEqual((status.pid, status.last_beat_at, status.stopped_at),
                         (42, taken_over, None))
        self.assertTrue(db.beat(self.db, successor, later(62)))


class JobTests(Scratch):
    def test_a_queued_job_holds_its_kind_and_params(self):
        job = db.read_job(self.db, db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": "x"}, NOW))
        self.assertEqual((job.kind, job.params, job.state, job.error, job.created_at,
                          job.started_at, job.finished_at),
                         (JobKind.SNAPSHOT, {"note": "x"}, JobState.QUEUED, None, NOW, None, None))

    def test_a_second_open_job_of_the_same_kind_is_refused_and_says_which_job_is_open(self):
        first = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": "one"}, NOW)
        with self.assertRaisesRegex(db.JobOpen, f"snapshot job is already queued .job {first}."):
            db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": "two"}, later(1))
        db.claim_job(self.db, later(2))
        with self.assertRaisesRegex(db.JobOpen, "already running"):
            db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": "three"}, later(3))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_a_job_of_another_kind_or_after_the_first_finished_is_accepted(self):
        first = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": ""}, NOW)
        db.enqueue_job(self.db, JobKind.PREVIEW, {"snapshot_id": 1}, NOW)
        db.claim_job(self.db, later(1))
        db.finish_job(self.db, first, later(2))
        second = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": ""}, later(3))
        self.assertEqual(db.read_job(self.db, second).state, JobState.QUEUED)

    def test_the_oldest_queued_job_is_claimed_first(self):
        first = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": ""}, NOW)
        second = db.enqueue_job(self.db, JobKind.PREVIEW, {"snapshot_id": 1}, NOW)
        claimed = db.claim_job(self.db, later(5))
        self.assertEqual((claimed.id, claimed.state, claimed.started_at),
                         (first, JobState.RUNNING, later(5)))
        self.assertEqual(db.read_job(self.db, first).state, JobState.RUNNING)
        self.assertEqual(db.claim_job(self.db, later(6)).id, second)
        self.assertIsNone(db.claim_job(self.db, later(7)))

    def test_a_finished_job_is_done_or_failed_with_its_error_and_stays_so(self):
        done = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": ""}, NOW)
        failed = db.enqueue_job(self.db, JobKind.PREVIEW, {"snapshot_id": 1}, NOW)
        db.claim_job(self.db, NOW)
        db.claim_job(self.db, NOW)
        db.finish_job(self.db, done, later(1))
        db.finish_job(self.db, failed, later(2), error="ConnectionError: timed out")
        db.finish_job(self.db, done, later(9), error="late")
        self.assertEqual([(j.state, j.error, j.finished_at)
                          for j in (db.read_job(self.db, done), db.read_job(self.db, failed))],
                         [(JobState.DONE, None, later(1)),
                          (JobState.FAILED, "ConnectionError: timed out", later(2))])

    def test_jobs_left_running_are_closed_as_failed_once(self):
        left = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": ""}, NOW)
        queued = db.enqueue_job(self.db, JobKind.PREVIEW, {"snapshot_id": 1}, NOW)
        db.claim_job(self.db, later(1))
        self.assertEqual(db.fail_running_jobs(self.db, later(60), "the worker stopped"), 1)
        closed = db.read_job(self.db, left)
        self.assertEqual((closed.state, closed.error, closed.finished_at),
                         (JobState.FAILED, "the worker stopped", later(60)))
        self.assertEqual(db.read_job(self.db, queued).state, JobState.QUEUED)
        self.assertEqual(db.fail_running_jobs(self.db, later(90), "again"), 0)
        self.assertEqual(db.read_job(self.db, left), closed)

    def test_the_latest_job_of_a_kind(self):
        self.assertIsNone(db.latest_job(self.db, JobKind.SNAPSHOT))
        first = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": "a"}, NOW)
        db.enqueue_job(self.db, JobKind.PREVIEW, {"snapshot_id": 1}, NOW)
        self.assertEqual(db.latest_job(self.db, JobKind.SNAPSHOT).id, first)
        db.claim_job(self.db, NOW)
        db.finish_job(self.db, first, NOW)
        second = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": "b"}, later(1))
        self.assertEqual(db.latest_job(self.db, JobKind.SNAPSHOT).id, second)

    def test_an_unknown_job_reads_as_none(self):
        self.assertIsNone(db.read_job(self.db, 99))


class LogTests(Scratch):
    def test_a_jobs_lines_read_back_oldest_first_and_apart_from_other_jobs(self):
        db.append_log(self.db, NOW, "listed 3 open orders", job_id=1)
        db.append_log(self.db, later(1), "other job", job_id=2)
        db.append_log(self.db, later(2), "stored snapshot 7", job_id=1)
        self.assertEqual([(line.at, line.job_id, line.message) for line in db.read_log(self.db, 1)],
                         [(NOW, 1, "listed 3 open orders"), (later(2), 1, "stored snapshot 7")])

    def test_every_change_to_a_job_moves_the_change_mark(self):
        seen = [db.last_job_change(self.db)]

        def moved():
            seen.append(db.last_job_change(self.db))
            return seen[-1] != seen[-2]

        job = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": ""}, NOW)
        self.assertTrue(moved(), "queued")
        db.claim_job(self.db, NOW)
        self.assertTrue(moved(), "claimed")
        db.append_log(self.db, NOW, "listed 3 open orders", job_id=job)
        self.assertTrue(moved(), "logged")
        db.finish_job(self.db, job, NOW)
        self.assertTrue(moved(), "finished")
        again = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": ""}, NOW)
        db.claim_job(self.db, NOW)
        moved()
        db.fail_running_jobs(self.db, NOW, "the worker stopped")
        self.assertTrue(moved(), "closed at restart")
        self.assertEqual(db.read_log(self.db, again)[-1].message, "failed: the worker stopped")

    def test_a_line_about_no_job_leaves_the_change_mark_alone(self):
        before = db.last_job_change(self.db)
        db.append_log(self.db, NOW, "watch armed", watch_id=4)
        db.append_log(self.db, NOW, "worker started")
        self.assertEqual(db.last_job_change(self.db), before)


class SnapshotTests(Scratch):
    def test_a_snapshot_reads_back_with_its_orders_and_their_market_names(self):
        expires = NOW + timedelta(days=3)
        orders = [order("0xo1", MARKET_A, Side.BUY, 0.98, 100.0, 12.5, expires),
                  order("0xo2", MARKET_B, Side.SELL, 0.015, 2000.0)]
        snapshot_id = db.insert_snapshot(self.db, NOW, "before the upgrade", orders,
                                         {MARKET_A: "Will A happen?"}, job_id=5)
        snapshot = db.read_snapshot(self.db, snapshot_id)
        self.assertEqual((snapshot.taken_at, snapshot.note, snapshot.source, snapshot.job_id,
                          snapshot.orders), (NOW, "before the upgrade", None, 5, 2))
        stored = db.read_snapshot_orders(self.db, snapshot_id)
        self.assertEqual([(s.snapshot_id, s.market_name, s.order) for s in stored],
                         [(snapshot_id, "Will A happen?", orders[0]),
                          (snapshot_id, None, orders[1])])
        self.assertEqual(len({s.id for s in stored}), 2)

    def test_snapshots_list_newest_first_with_their_order_counts(self):
        old = db.insert_snapshot(self.db, NOW - timedelta(days=30), "", [order()], {},
                                 source="orders_snapshot_old.json")
        empty = db.insert_snapshot(self.db, NOW, "nothing open", [], {})
        mid = db.insert_snapshot(self.db, NOW - timedelta(days=1), "", [order(), order("0xo2")],
                                 {})
        self.assertEqual([(s.id, s.orders, s.source) for s in db.read_snapshots(self.db)],
                         [(empty, 0, None), (mid, 2, None), (old, 1, "orders_snapshot_old.json")])

    def test_an_unknown_snapshot_reads_as_none(self):
        self.assertIsNone(db.read_snapshot(self.db, 99))

    def test_a_source_is_stored_once_and_a_refused_copy_leaves_no_orders_behind(self):
        db.insert_snapshot(self.db, NOW, "", [order()], {}, source="orders_snapshot_a.json")
        self.assertEqual(db.imported_sources(self.db), {"orders_snapshot_a.json"})
        with self.assertRaises(sqlite3.IntegrityError):
            db.insert_snapshot(self.db, NOW, "", [order(), order("0xo2")], {},
                               source="orders_snapshot_a.json")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM snapshot_orders").fetchone()[0], 1)

    def test_snapshots_the_worker_took_have_no_source_and_do_not_collide(self):
        db.insert_snapshot(self.db, NOW, "", [], {})
        db.insert_snapshot(self.db, NOW, "", [], {})
        self.assertEqual(db.imported_sources(self.db), set())
        self.assertEqual(len(db.read_snapshots(self.db)), 2)


class RestoreJobTests(Scratch):
    def test_one_job_among_preview_and_restore_is_open_at_a_time(self):
        preview = db.enqueue_job(self.db, JobKind.PREVIEW, {"snapshot_id": 1}, NOW)
        with self.assertRaisesRegex(db.JobOpen, f"A preview job is already queued .job {preview}."):
            db.enqueue_job(self.db, JobKind.RESTORE, {"snapshot_id": 1}, later(1))
        db.claim_job(self.db, later(2))
        db.finish_job(self.db, preview, later(3))
        restore = db.enqueue_job(self.db, JobKind.RESTORE, {"snapshot_id": 1}, later(4))
        db.claim_job(self.db, later(5))
        with self.assertRaisesRegex(db.JobOpen, f"restore job is already running .job {restore}."):
            db.enqueue_job(self.db, JobKind.PREVIEW, {"snapshot_id": 2}, later(6))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_a_snapshot_job_is_accepted_beside_an_open_restore(self):
        db.enqueue_job(self.db, JobKind.RESTORE, {"snapshot_id": 1}, NOW)
        snapshot = db.enqueue_job(self.db, JobKind.SNAPSHOT, {"note": ""}, NOW)
        self.assertEqual(db.read_job(self.db, snapshot).state, JobState.QUEUED)

    def test_the_latest_restore_job_of_a_snapshot_is_its_newest_preview_or_restore(self):
        self.assertIsNone(db.latest_restore_job(self.db, 1))

        def run(kind, **params):
            job = db.enqueue_job(self.db, kind, params, NOW)
            db.claim_job(self.db, NOW)
            db.finish_job(self.db, job, NOW)
            return job

        preview = run(JobKind.PREVIEW, snapshot_id=1, managed_markets=[])
        self.assertEqual(db.latest_restore_job(self.db, 1).id, preview)
        restore = run(JobKind.RESTORE, snapshot_id=1, managed_markets=[])
        run(JobKind.PREVIEW, snapshot_id=2, managed_markets=[])
        run(JobKind.SNAPSHOT, note="")
        latest = db.latest_restore_job(self.db, 1)
        self.assertEqual((latest.id, latest.kind), (restore, JobKind.RESTORE))
        self.assertIsNone(db.latest_restore_job(self.db, 3))


class RestoreLineTests(Scratch):
    def setUp(self):
        super().setUp()
        self.snapshot = db.insert_snapshot(
            self.db, NOW, "", [order("0xo1", MARKET_A, size=100.0, matched=40.0),
                               order("0xo2", MARKET_B), order("0xo3", MARKET_B)],
            {MARKET_A: "Will A happen?"})
        self.orders = db.read_snapshot_orders(self.db, self.snapshot)

    def line(self, index, action, size=100.0, order_id=None, detail="", job=7):
        return db.RestoreLine(job, self.orders[index], action, size, order_id, detail)

    def test_a_jobs_lines_read_back_as_written_with_their_snapshot_orders(self):
        written = [self.line(0, RestoreAction.PLACED, 60.0, "0xnew", "placed"),
                   self.line(1, RestoreAction.REJECTED, detail="not enough balance / allowance"),
                   self.line(2, RestoreAction.SKIP_LIVE, detail="already open")]
        db.add_restore_lines(self.db, written[:1])
        db.add_restore_lines(self.db, written[1:])
        db.add_restore_lines(self.db, [self.line(0, RestoreAction.SKIP_FARMER, job=8)])
        self.assertEqual(db.read_restore_lines(self.db, 7), written)
        self.assertEqual(db.read_restore_lines(self.db, 7)[0].order.market_name, "Will A happen?")
        self.assertEqual(db.read_restore_lines(self.db, 9), [])

    def test_a_job_holds_one_line_for_a_snapshot_order_and_a_refused_batch_stores_nothing(self):
        db.add_restore_lines(self.db, [self.line(0, RestoreAction.SKIP_EXPIRED)])
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_restore_lines(self.db, [self.line(1, RestoreAction.SKIP_LIVE),
                                           self.line(0, RestoreAction.WOULD_PLACE)])
        self.assertEqual([line.action for line in db.read_restore_lines(self.db, 7)],
                         [RestoreAction.SKIP_EXPIRED])

    def test_a_placed_line_carries_an_order_id_and_no_other_line_does(self):
        for action in RestoreAction:
            if action is not RestoreAction.PLACED:
                with self.subTest(action=action):
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.add_restore_lines(self.db, [self.line(0, action, order_id="0xnew")])
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_restore_lines(self.db, [self.line(0, RestoreAction.PLACED)])

    def test_the_lines_of_a_job_are_counted_by_action(self):
        self.assertEqual(db.count_restore_lines(self.db, 7), {})
        db.add_restore_lines(self.db, [self.line(0, RestoreAction.WOULD_PLACE),
                                       self.line(1, RestoreAction.SKIP_LIVE),
                                       self.line(2, RestoreAction.WOULD_PLACE),
                                       self.line(0, RestoreAction.ERROR, job=8)])
        self.assertEqual(db.count_restore_lines(self.db, 7),
                         {RestoreAction.WOULD_PLACE: 2, RestoreAction.SKIP_LIVE: 1})

    def test_a_restore_line_moves_the_job_change_mark(self):
        before = db.last_job_change(self.db)
        db.add_restore_lines(self.db, [self.line(0, RestoreAction.WOULD_PLACE)])
        self.assertNotEqual(db.last_job_change(self.db), before)


if __name__ == "__main__":
    unittest.main()
