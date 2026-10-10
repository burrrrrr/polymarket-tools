import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from test_polymarket_gamma import Gamma, cid, client
from test_polymarket_orders import Clob, entry
from test_tools_restore import ACCEPT, MARKET_B, MARKET_C, Venue, order
from tools import db, worker
from tools.db import JobKind, JobState, RestoreAction
from tools.worker import Handler

START = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
HANG_TIMEOUT_S = 10


def at(seconds):
    return START + timedelta(seconds=seconds)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class Stop:
    def __init__(self, clock, beats, settle_jobs=True, on_wait=lambda n: None):
        self.clock, self.beats, self.settle_jobs, self.on_wait = clock, beats, settle_jobs, on_wait
        self.waits = 0
        self.flag = False

    def wait(self, timeout):
        self.waits += 1
        self.on_wait(self.waits)
        if self.settle_jobs:
            for thread in threading.enumerate():
                if thread.name == worker.JOB_THREAD:
                    thread.join(HANG_TIMEOUT_S)
        self.clock.now += timedelta(seconds=timeout)
        return self.is_set()

    def set(self):
        self.flag = True

    def is_set(self):
        return self.flag or self.waits >= self.beats


class FakeSniper:
    built = []
    failure = None

    def __init__(self, db_path, http, stop, client=None, *, clock):
        self.db_path, self.http, self.stop, self.client, self.clock = db_path, http, stop, client, clock
        self.ran_on = None
        self.returned_at = None
        FakeSniper.built.append(self)

    def run(self):
        self.ran_on = threading.current_thread().name
        if FakeSniper.failure is not None:
            raise FakeSniper.failure
        released = threading.Event()
        while not self.stop.is_set():
            released.wait(0.01)
        self.returned_at = self.clock()


class Scratch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "tools.db")
        self.db = db.open_db(self.path)
        self.addCleanup(self.db.close)
        self.clock = Clock()
        self.gamma = Gamma(open_ids=[cid(1)])
        FakeSniper.built, FakeSniper.failure = [], None

    def run_worker(self, clob, beats=1, **kwargs):
        stop = kwargs.pop("stop", None) or Stop(self.clock, beats)
        worker.run(self.path, clob, client(self.gamma), stop=stop, clock=self.clock, poll_s=1.0,
                   pid=41, **{"sniper": FakeSniper, **kwargs})

    def queue(self, kind=JobKind.SNAPSHOT, **params):
        return db.enqueue_job(self.db, kind, params or {"note": "before the upgrade"},
                              self.clock())

    def messages(self, job_id):
        return [line.message for line in db.read_log(self.db, job_id)]


class LoopTests(Scratch):
    def test_the_worker_beats_once_a_poll_and_writes_when_it_stopped(self):
        self.run_worker(Clob([]), beats=3)
        status = db.read_worker_status(self.db)
        self.assertEqual((status.pid, status.keyed, status.started_at, status.last_beat_at,
                          status.stopped_at), (41, True, at(0), at(2), at(3)))
        self.assertFalse(db.is_running(status, at(3)))

    def test_it_writes_when_it_stopped_even_if_the_loop_raises(self):
        def broken(n):
            raise RuntimeError("disk full")

        with self.assertRaisesRegex(RuntimeError, "disk full"):
            self.run_worker(Clob([]), stop=Stop(self.clock, 5, on_wait=broken))
        self.assertEqual(db.read_worker_status(self.db).stopped_at, at(0))

    def test_a_queued_snapshot_job_becomes_a_snapshot(self):
        job = self.queue(note="before the upgrade")
        self.run_worker(Clob([entry(id="0xa", market=cid(1)), entry(id="0xb", market=cid(1))]))
        done = db.read_job(self.db, job)
        self.assertEqual((done.state, done.error), (JobState.DONE, None))
        (taken,) = db.read_snapshots(self.db)
        self.assertEqual((taken.note, taken.orders, taken.job_id), ("before the upgrade", 2, job))
        self.assertEqual(self.messages(job), [
            "started", f"stored snapshot {taken.id} with 2 open orders", "done"])

    def test_a_job_queued_while_the_worker_runs_is_claimed_on_the_next_beat(self):
        queued = []
        stop = Stop(self.clock, 3, on_wait=lambda n: n == 1 and queued.append(self.queue()))
        self.run_worker(Clob([entry()]), stop=stop)
        self.assertEqual(db.read_job(self.db, queued[0]).state, JobState.DONE)
        self.assertEqual(db.read_job(self.db, queued[0]).started_at, at(1))

    def test_a_failed_listing_fails_the_job_and_stores_no_snapshot(self):
        job = self.queue()
        self.run_worker(Clob(ConnectionError("timed out")), beats=3)
        failed = db.read_job(self.db, job)
        self.assertEqual((failed.state, failed.error),
                         (JobState.FAILED, "ConnectionError: timed out"))
        self.assertEqual(db.read_snapshots(self.db), [])
        self.assertEqual(self.messages(job), ["started", "failed: ConnectionError: timed out"])
        self.assertEqual(db.read_worker_status(self.db).last_beat_at, at(2))

    def test_names_that_could_not_be_read_are_logged_and_the_job_is_done(self):
        self.gamma.fail_when = lambda ids: True
        job = self.queue()
        self.run_worker(Clob([entry(market=cid(1))]))
        self.assertEqual(db.read_job(self.db, job).state, JobState.DONE)
        self.assertIn("stored without market names", self.messages(job)[1])

    def test_a_worker_without_a_key_fails_a_job_that_needs_one_and_keeps_running(self):
        job = self.queue()
        self.run_worker(None, beats=3)
        failed = db.read_job(self.db, job)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertIn("no key", failed.error)
        self.assertIn("--no-key", failed.error)
        status = db.read_worker_status(self.db)
        self.assertEqual((status.keyed, status.last_beat_at, status.stopped_at),
                         (False, at(2), at(3)))

    def test_a_job_the_worker_has_no_handler_for_fails_and_the_next_one_runs(self):
        unknown = self.queue(JobKind.PREVIEW, snapshot_id=1)
        snapshot = self.queue()
        self.run_worker(Clob([entry()]), beats=2,
                        handlers={JobKind.SNAPSHOT: worker.HANDLERS[JobKind.SNAPSHOT]})
        failed = db.read_job(self.db, unknown)
        self.assertEqual((failed.state, failed.error),
                         (JobState.FAILED, "this worker cannot run a preview job"))
        self.assertEqual(db.read_job(self.db, snapshot).state, JobState.DONE)

    def test_a_job_whose_params_the_handler_cannot_use_fails_without_stopping_the_worker(self):
        job = self.queue(wrong="shape")
        self.run_worker(Clob([entry()]), beats=2)
        self.assertEqual((db.read_job(self.db, job).state, db.read_job(self.db, job).error),
                         (JobState.FAILED, "KeyError: 'note'"))
        self.assertEqual(db.read_worker_status(self.db).last_beat_at, at(1))

    def test_the_heartbeat_goes_on_while_a_job_runs(self):
        released, beat_seen = threading.Event(), []

        def slow(run):
            if not released.wait(HANG_TIMEOUT_S):
                raise TimeoutError("the loop did not beat while the job ran")
            beat_seen.append(db.read_worker_status(run.conn).last_beat_at)

        job = self.queue()
        stop = Stop(self.clock, 4, settle_jobs=False,
                    on_wait=lambda n: n == 3 and released.set())
        self.run_worker(Clob([]), stop=stop, handlers={JobKind.SNAPSHOT: Handler(slow, True)})
        done = db.read_job(self.db, job)
        self.assertEqual((done.state, done.started_at), (JobState.DONE, at(0)))
        self.assertGreaterEqual(beat_seen[0], at(2))

    def test_a_stopping_worker_lets_its_running_job_finish_first(self):
        order = []

        def handler(run):
            order.append(db.read_worker_status(run.conn).stopped_at)

        job = self.queue()
        self.run_worker(Clob([]), stop=Stop(self.clock, 1, settle_jobs=False),
                        handlers={JobKind.SNAPSHOT: Handler(handler, True)})
        self.assertEqual(order, [None])
        self.assertEqual(db.read_job(self.db, job).state, JobState.DONE)
        self.assertEqual(db.read_worker_status(self.db).stopped_at, at(1))

    def test_a_stopping_worker_tells_its_running_job_to_stop(self):
        told = []

        def handler(run):
            told.append(run.stop.wait(HANG_TIMEOUT_S))

        self.queue()
        self.run_worker(Clob([]), stop=Stop(self.clock, 1, settle_jobs=False),
                        handlers={JobKind.SNAPSHOT: Handler(handler, True)})
        self.assertEqual(told, [True])

    def test_a_job_is_not_told_to_stop_while_the_worker_runs(self):
        told = []
        self.queue()
        self.run_worker(Clob([]), beats=2, handlers={
            JobKind.SNAPSHOT: Handler(lambda run: told.append(run.stop.is_set()), True)})
        self.assertEqual(told, [False])


class RestoreJobTests(Scratch):
    def snapshot(self, *orders):
        return db.insert_snapshot(self.db, at(-3600), "", list(orders), {})

    def queue_restore(self, kind, snapshot_id, managed=()):
        return self.queue(kind, snapshot_id=snapshot_id, managed_markets=list(managed))

    def lines(self, job):
        return [(line.action, line.size, line.order_id)
                for line in db.read_restore_lines(self.db, job)]

    def test_a_preview_job_stores_the_plan_and_never_posts(self):
        venue = Venue(listing=[order("0xlive")])
        job = self.queue_restore(JobKind.PREVIEW, self.snapshot(
            order("0xa"), order("0xb"), order("0xc", MARKET_B)), managed=[MARKET_B])
        self.run_worker(venue)
        self.assertEqual((db.read_job(self.db, job).state, db.read_job(self.db, job).error),
                         (JobState.DONE, None))
        self.assertEqual(self.lines(job), [(RestoreAction.SKIP_LIVE, 100.0, None),
                                           (RestoreAction.WOULD_PLACE, 100.0, None),
                                           (RestoreAction.SKIP_FARMER, 100.0, None)])
        self.assertEqual((venue.signed, venue.posts), ([], []), "a preview signed or posted")
        self.assertEqual(self.messages(job),
                         ["started", "planned 3 orders: 1 to place, 2 skipped", "done"])

    def test_a_restore_job_places_what_is_missing_and_stores_what_became_of_each_order(self):
        venue = Venue(listing=[order("0xlive")],
                      answer=lambda signed: "not enough balance / allowance"
                      if signed.price == 0.5 else ACCEPT)
        job = self.queue_restore(JobKind.RESTORE, self.snapshot(
            order("0xa"), order("0xb"), order("0xc", MARKET_B, price=0.5)))
        self.run_worker(venue)
        self.assertEqual(db.read_job(self.db, job).state, JobState.DONE)
        self.assertEqual(self.lines(job), [(RestoreAction.SKIP_LIVE, 100.0, None),
                                           (RestoreAction.PLACED, 100.0, "0xnew1"),
                                           (RestoreAction.REJECTED, 100.0, None)])
        self.assertEqual([post_only for *_, post_only in venue.posts], [True, True])
        self.assertEqual(self.messages(job), [
            "started", "went through 3 orders: 1 placed, 1 rejected, 1 skipped", "done"])

    def test_a_restore_whose_listing_fails_fails_the_job_and_places_nothing(self):
        venue = Venue(listing=ConnectionError("timed out"))
        job = self.queue_restore(JobKind.RESTORE, self.snapshot(order("0xa"), order("0xb")))
        self.run_worker(venue, beats=2)
        failed = db.read_job(self.db, job)
        self.assertEqual((failed.state, failed.error),
                         (JobState.FAILED, "ConnectionError: timed out"))
        self.assertEqual((venue.posts, self.lines(job)), ([], []))

    def test_a_stopping_worker_ends_a_restore_between_orders_and_keeps_its_lines(self):
        venue, runs, posted_two = Venue(), [], threading.Event()

        def hold_the_second_post():
            if len(venue.posts) == 2:
                posted_two.set()
                runs[0].stop.wait(HANG_TIMEOUT_S)

        def handler(run):
            runs.append(run)
            worker.HANDLERS[JobKind.RESTORE].run(run)

        def loop_waits(n):
            if not posted_two.wait(HANG_TIMEOUT_S):
                raise TimeoutError("the restore never reached its second order")

        venue.after_post = hold_the_second_post
        job = self.queue_restore(JobKind.RESTORE, self.snapshot(
            order("0xa"), order("0xb", MARKET_B), order("0xc", MARKET_C), order("0xd", price=0.4)))
        self.run_worker(venue, stop=Stop(self.clock, 1, settle_jobs=False, on_wait=loop_waits),
                        handlers={JobKind.RESTORE: Handler(handler, True)})
        failed = db.read_job(self.db, job)
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertIn("the worker stopped after 2 of 4 orders", failed.error)
        self.assertEqual(self.lines(job), [(RestoreAction.PLACED, 100.0, "0xnew1"),
                                           (RestoreAction.PLACED, 100.0, "0xnew2")])
        self.assertEqual(len(venue.posts), 2)

    def test_a_preview_needs_a_key(self):
        job = self.queue_restore(JobKind.PREVIEW, self.snapshot(order("0xa")))
        self.run_worker(None)
        self.assertIn("no key", db.read_job(self.db, job).error)


class RecoveryTests(Scratch):
    def test_a_job_left_running_is_closed_as_failed_when_a_worker_starts(self):
        left = self.queue()
        db.claim_job(self.db, at(-60))
        self.run_worker(Clob([entry()]))
        closed = db.read_job(self.db, left)
        self.assertEqual((closed.state, closed.error, closed.finished_at),
                         (JobState.FAILED, worker.INTERRUPTED, at(0)))
        self.assertEqual(db.read_snapshots(self.db), [])
        self.run_worker(Clob([entry()]))
        self.assertEqual(db.read_job(self.db, left), closed)
        self.assertEqual(self.messages(left), ["started", f"failed: {worker.INTERRUPTED}"])

    def test_a_second_worker_is_refused_and_leaves_the_first_ones_jobs_alone(self):
        first = db.start_worker(self.db, at(0), pid=7, keyed=True)
        running = self.queue()
        db.claim_job(self.db, at(0))
        queued = self.queue(JobKind.PREVIEW, snapshot_id=1)
        self.clock.now = at(db.STALE_AFTER_S - 1)
        with self.assertRaisesRegex(db.WorkerRunning, "pid 7"):
            self.run_worker(Clob([entry()]), beats=3)
        self.assertEqual([db.read_job(self.db, j).state for j in (running, queued)],
                         [JobState.RUNNING, JobState.QUEUED])
        status = db.read_worker_status(self.db)
        self.assertEqual((status.instance, status.pid, status.stopped_at), (first, 7, None))

    def test_a_worker_starts_once_the_last_ones_beats_have_stopped(self):
        db.start_worker(self.db, at(0), pid=7, keyed=True)
        self.clock.now = at(db.STALE_AFTER_S)
        self.run_worker(Clob([]))
        self.assertEqual(db.read_worker_status(self.db).pid, 41)

    def test_a_worker_that_another_replaced_stops_claiming_and_leaves_its_row(self):
        def replaced(n):
            if n == 1:
                db.start_worker(self.db, self.clock() + timedelta(seconds=db.STALE_AFTER_S),
                                pid=99, keyed=True)
                self.queue()

        with self.assertRaises(db.WorkerRunning):
            self.run_worker(Clob([entry()]), stop=Stop(self.clock, 5, on_wait=replaced))
        status = db.read_worker_status(self.db)
        self.assertEqual((status.pid, status.stopped_at), (99, None))
        self.assertEqual(db.latest_job(self.db, JobKind.SNAPSHOT).state, JobState.QUEUED)


class SniperTests(Scratch):
    def test_the_sniper_runs_on_its_own_thread_with_its_own_client_until_the_worker_stops(self):
        sniper_client = object()
        self.run_worker(Clob([]), beats=2, sniper_client=sniper_client)
        (snipe,) = FakeSniper.built
        self.assertEqual((snipe.db_path, snipe.ran_on), (self.path, worker.SNIPER_THREAD))
        self.assertIs(snipe.client, sniper_client)
        self.assertIsNot(snipe.client, self.db)
        self.assertEqual(snipe.returned_at, at(2), "the sniper outlived the loop")
        self.assertEqual(db.read_worker_status(self.db).stopped_at, at(2))
        self.assertEqual([t.name for t in threading.enumerate() if t.name == worker.SNIPER_THREAD],
                         [])

    def test_a_keyless_worker_runs_a_keyless_sniper(self):
        self.run_worker(None)
        self.assertIsNone(FakeSniper.built[0].client)

    def test_a_replaced_worker_stops_its_sniper_too(self):
        def replaced(n):
            if n == 1:
                db.start_worker(self.db, self.clock() + timedelta(seconds=db.STALE_AFTER_S),
                                pid=99, keyed=True)

        with self.assertRaises(db.WorkerRunning):
            self.run_worker(Clob([]), stop=Stop(self.clock, 5, on_wait=replaced))
        self.assertIsNotNone(FakeSniper.built[0].returned_at)

    def test_a_sniper_that_dies_takes_the_worker_down_with_its_error(self):
        FakeSniper.failure = RuntimeError("feed thread bug")
        with self.assertRaisesRegex(RuntimeError, "feed thread bug"):
            self.run_worker(Clob([]), beats=50)
        self.assertEqual(db.read_worker_status(self.db).stopped_at, self.clock())
        self.assertLess(db.read_worker_status(self.db).last_beat_at, at(50))


class JobThreadTests(Scratch):
    def test_one_job_runs_at_a_time_and_the_oldest_first(self):
        entered, release, ran = threading.Event(), threading.Event(), []

        def held(run):
            ran.append(run.job.id)
            entered.set()
            if not release.wait(HANG_TIMEOUT_S):
                raise TimeoutError("never released")

        handlers = {kind: Handler(held, True) for kind in JobKind}
        jobs = worker.JobThread(self.path, Clob([]), client(self.gamma), self.clock, handlers)
        first, second = self.queue(), self.queue(JobKind.PREVIEW, snapshot_id=1)
        jobs.tick(self.db)
        self.assertTrue(entered.wait(HANG_TIMEOUT_S))
        jobs.tick(self.db)
        self.assertEqual([db.read_job(self.db, j).state for j in (first, second)],
                         [JobState.RUNNING, JobState.QUEUED])
        release.set()
        jobs.join()
        jobs.tick(self.db)
        jobs.join()
        self.assertEqual(ran, [first, second])
        self.assertEqual([db.read_job(self.db, j).state for j in (first, second)],
                         [JobState.DONE, JobState.DONE])


if __name__ == "__main__":
    unittest.main()
