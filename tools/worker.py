import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from farm import farm_db
from tools import db, restore, snapshot
from tools.db import JobKind
from tools.sniper import Sniper

JOB_THREAD = "tools-job"
SNIPER_THREAD = "tools-sniper"
INTERRUPTED = "the worker stopped while this job was running"


def utc_now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class JobRun:
    """What a handler works with: the claimed job, the worker's client and HTTP
    client, a connection that belongs to the job's thread, stop, an event that
    is set once the worker is stopping or was replaced, and the path of the
    farmer's farm.db. A handler that places orders checks stop before each one."""

    job: db.Job
    client: Any
    http: Any
    conn: Any
    clock: Callable
    stop: Any
    farm_path: Optional[str]

    def log(self, message):
        db.append_log(self.conn, self.clock(), message, job_id=self.job.id)


class Handler(NamedTuple):
    """How the worker runs one kind of job. run(JobRun) returns when the job is
    done and raises when it failed."""

    run: Callable
    needs_key: bool


def stored_line(taken, orders="open orders"):
    return (f"stored snapshot {taken.snapshot_id} with {taken.orders} {orders}"
            + (f", stored without market names ({taken.names_error})" if taken.names_error
               else ""))


def take_snapshot(run):
    run.log(stored_line(snapshot.take(
        run.client, run.http, run.conn, run.clock(), run.job.params["note"], job_id=run.job.id,
        farmer_ids=set(run.job.params["farmer_order_ids"]))))


def farmer_order_ids(path):
    """The ids of the farmer's open orders, read from its farm.db without
    writing it."""
    if path is None or not os.path.exists(path):
        where = f"at {path}" if path else "in a data directory: the directory is missing"
        raise JobRefused(f"no farm.db {where}, and an hourly snapshot reads the farmer's "
                         f"orders from it")
    with closing(sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)) as farm:
        farm.row_factory = sqlite3.Row
        return frozenset(farm_db.farmer_orders(farm))


def take_hourly_snapshot(run):
    taken = snapshot.take_hourly(run.client, run.http, run.conn, run.clock(),
                                 farmer_order_ids(run.farm_path), run.job.id)
    if isinstance(taken, snapshot.Unchanged):
        run.log(f"unchanged since snapshot {taken.snapshot_id}")
    else:
        run.log(stored_line(taken, "open orders that are not the farmer's"))
    pruned = db.prune_hourly_snapshots(run.conn, run.clock() - db.HOURLY_KEEP)
    if pruned:
        run.log(f"pruned {pruned} hourly snapshot{'' if pruned == 1 else 's'} older than "
                f"{db.HOURLY_KEEP.days} days")


def queue_hourly_snapshot(conn, now):
    """Queue an hourly snapshot job when the console turned them on and the
    last one was queued HOURLY_EVERY ago or more. The jobs table says when that
    was, so a restarted worker keeps the same hour."""
    if not db.hourly_snapshots(conn):
        return
    last = db.latest_job(conn, JobKind.HOURLY_SNAPSHOT)
    if last is not None and now - last.created_at < db.HOURLY_EVERY:
        return
    try:
        db.enqueue_job(conn, JobKind.HOURLY_SNAPSHOT, {}, now)
    except db.JobOpen:
        pass


def preview_restore(run):
    lines = restore.preview(run.client, run.conn, run.job, run.clock())
    run.log(f"planned {len(lines)} orders: {restore.summary(lines, preview=True)}")


def place_restore(run):
    lines = restore.place(run.client, run.conn, run.job, run.clock, run.stop)
    run.log(f"went through {len(lines)} orders: {restore.summary(lines)}")


HANDLERS = {JobKind.SNAPSHOT: Handler(take_snapshot, needs_key=True),
            JobKind.PREVIEW: Handler(preview_restore, needs_key=True),
            JobKind.RESTORE: Handler(place_restore, needs_key=True),
            JobKind.HOURLY_SNAPSHOT: Handler(take_hourly_snapshot, needs_key=True)}


class JobRefused(Exception):
    """This worker cannot run the job. str() is the job's error."""


class JobThread:
    """Runs the queued jobs, oldest first and one at a time, off the loop's
    thread, so a long job does not hold up the heartbeat."""

    def __init__(self, db_path, client, http, clock, handlers=HANDLERS, farm_path=None):
        self.db_path = db_path
        self.farm_path = farm_path
        self.client = client
        self.http = http
        self.clock = clock
        self.handlers = handlers
        self._thread = None
        self._stop = threading.Event()

    def tick(self, conn):
        if self._thread is not None and self._thread.is_alive():
            return
        job = db.claim_job(conn, self.clock())
        if job is not None:
            self._thread = threading.Thread(target=self._run, args=(job,), name=JOB_THREAD)
            self._thread.start()

    def join(self):
        if self._thread is not None:
            self._thread.join()

    def close(self):
        """Tell the running job to stop, and wait for it. A job that does not
        look at its stop signal runs to its end."""
        self._stop.set()
        self.join()

    def _run(self, job):
        with closing(db.open_db(self.db_path)) as conn:
            try:
                self._handle(job, conn)
                error = None
            except (JobRefused, restore.Stopped) as e:
                error = str(e)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            db.finish_job(conn, job.id, self.clock(), error)

    def _handle(self, job, conn):
        handler = self.handlers.get(job.kind)
        if handler is None:
            raise JobRefused(f"this worker cannot run a {job.kind.value} job")
        if handler.needs_key and self.client is None:
            raise JobRefused(f"this worker holds no key, and a {job.kind.value} job needs one: "
                             f"start the worker again without --no-key")
        handler.run(JobRun(job, self.client, self.http, conn, self.clock, self._stop,
                           self.farm_path))


def run(db_path, client, http, *, stop, farm_path=None, clock=utc_now, poll_s=db.BEAT_S,
        pid=None, handlers=HANDLERS, sniper_client=None, sniper=Sniper):
    """Run the worker on db_path until stop is set.

    client is the authenticated CLOB client, or None for a worker with no key,
    which fails every job that needs one. sniper_client is the sniper's own
    client, never the jobs': presigning leaves a tick the market has not
    reached in its cache. http reads the public APIs. farm_path is the farmer's
    farm.db, which an hourly snapshot reads. Each poll_s the loop beats,
    queues an hourly snapshot when one is due, and starts the next queued job;
    stop.wait(poll_s) is its only wait.
    The sniper runs on its own thread from the start, and a sniper that dies
    stops the worker, which raises its error. A stopping worker tells its
    running job and the sniper to stop, waits for them, then writes when it
    stopped: a snapshot runs to its end, and a restore ends before its next
    order.

    Raises db.WorkerRunning, touching nothing, while another worker beats on
    this database, and also when another worker took this one's place. A job
    the last worker left running is closed as failed before the first claim.
    """
    with closing(db.open_db(db_path)) as conn:
        instance = db.start_worker(conn, clock(), os.getpid() if pid is None else pid,
                                   keyed=client is not None)
        jobs = JobThread(db_path, client, http, clock, handlers, farm_path)
        snipe = sniper(db_path, http, stop, client=sniper_client, clock=clock)
        failure = []

        def run_sniper():
            try:
                snipe.run()
            except BaseException as e:
                failure.append(e)
                stop.set()

        sniping = threading.Thread(target=run_sniper, name=SNIPER_THREAD, daemon=True)
        try:
            db.fail_running_jobs(conn, clock(), INTERRUPTED)
            sniping.start()
            while True:
                if not db.beat(conn, instance, clock()):
                    raise db.WorkerRunning("another worker took this worker's place")
                queue_hourly_snapshot(conn, clock())
                jobs.tick(conn)
                if stop.wait(poll_s):
                    break
        finally:
            stop.set()
            jobs.close()
            if sniping.is_alive():
                sniping.join()
            db.stop_worker(conn, instance, clock())
    if failure:
        raise failure[0]
