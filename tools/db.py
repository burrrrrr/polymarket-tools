"""
tools.db: the contract between the console and the worker

Two processes share one SQLite file: the web console, which holds no key, and
the worker (python -m tools), which holds the key and runs the order tools.
This module owns the schema and every statement either of them runs. The file
is opened in WAL mode with a busy timeout, and every column has one writer:

  worker_status    worker. A single row about the running process.
  jobs             the console inserts a queued row; the worker moves it to
                   running and then to done or failed.
  snapshots        worker, or the one-time import of the old JSON files.
  snapshot_orders  the same writer as its snapshot.
  restore_lines    worker. What a preview would do with each order of a
                   snapshot, or what a restore did with it.
  log             worker. What a job did, and every change of a job's state.

The worker never opens farm.db and the farmer never opens this file, so
neither waits on the other's write lock.

Times are ISO 8601 UTC strings to the second, as in farm_db.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import NamedTuple, Optional

from farm_db import stamp
from polymarket.orders import OpenOrder, Side

BUSY_TIMEOUT_S = 5
# How often the worker beats, and how long after its last beat it counts as gone.
BEAT_S = 1.0
STALE_AFTER_S = 10.0


class JobKind(Enum):
    SNAPSHOT = "snapshot"
    PREVIEW = "preview"
    RESTORE = "restore"


class JobState(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# A restore places what its own fresh plan finds missing, and the page shows
# one plan at a time, so one job among these is queued or running.
RESTORE_KINDS = (JobKind.PREVIEW, JobKind.RESTORE)
# A restore runs soon after the click that confirmed it, or not at all: the
# worker fails one that waited longer than this for it.
RESTORE_MAX_WAIT = timedelta(minutes=5)


class RestoreAction(Enum):
    """What became of one snapshot order. A plan decides PLACE or a skip. In a
    preview job PLACE means the order would be placed. In a restore job it
    means the CLOB accepted the order: one it refused is REJECTED, and one that
    was not sent or got no answer is ERROR."""

    PLACE = "place"
    SKIP_LIVE = "skip_live"
    SKIP_EXPIRED = "skip_expired"
    SKIP_FARMER = "skip_farmer"
    SKIP_FILLED = "skip_filled"
    REJECTED = "rejected"
    ERROR = "error"


def _values(members):
    return ", ".join(f"'{member.value}'" for member in members)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS worker_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    instance TEXT NOT NULL,
    pid INTEGER,
    keyed INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    last_beat_at TEXT NOT NULL,
    stopped_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    params TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ({_values(JobState)})),
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_open_kind ON jobs (kind)
    WHERE state IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS jobs_open_restore ON jobs ((1))
    WHERE kind IN ({_values(RESTORE_KINDS)}) AND state IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    note TEXT NOT NULL,
    source TEXT UNIQUE,
    job_id INTEGER
);

CREATE TABLE IF NOT EXISTS snapshot_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market TEXT NOT NULL,
    market_name TEXT,
    outcome TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ({_values(Side)})),
    price REAL NOT NULL,
    size REAL NOT NULL,
    size_matched REAL NOT NULL,
    expiration TEXT
);
CREATE INDEX IF NOT EXISTS snapshot_orders_snapshot ON snapshot_orders (snapshot_id);

CREATE TABLE IF NOT EXISTS restore_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    snapshot_order_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ({_values(RestoreAction)})),
    size REAL NOT NULL,
    order_id TEXT CHECK (order_id IS NULL OR action = 'place'),
    detail TEXT NOT NULL,
    UNIQUE (job_id, snapshot_order_id)
);

CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    job_id INTEGER,
    watch_id INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_job ON log (job_id);
"""


def open_db(path):
    """Connect, creating the tables; running it again changes nothing."""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
    conn.executescript(SCHEMA)
    return conn


def _time(text):
    return datetime.fromisoformat(text) if text else None


class WorkerStatus(NamedTuple):
    """instance names one run of the worker; keyed is whether it holds a key."""

    instance: str
    pid: int
    keyed: bool
    started_at: datetime
    last_beat_at: datetime
    stopped_at: Optional[datetime]


class WorkerRunning(Exception):
    """Another worker beats on this database."""


def is_running(status, now):
    """Whether the worker behind this row is alive: not stopped, and beating."""
    return (status is not None and status.stopped_at is None
            and (now - status.last_beat_at).total_seconds() < STALE_AFTER_S)


def read_worker_status(conn):
    row = conn.execute("SELECT * FROM worker_status WHERE id = 1").fetchone()
    if row is None:
        return None
    return WorkerStatus(row["instance"], row["pid"], bool(row["keyed"]), _time(row["started_at"]),
                        _time(row["last_beat_at"]), _time(row["stopped_at"]))


def start_worker(conn, now, pid, keyed):
    """Take the worker row and return this run's instance.

    Raises WorkerRunning while another worker is running. A worker that
    stopped, or whose beats stopped STALE_AFTER_S ago, is replaced.
    """
    instance = uuid.uuid4().hex
    with conn:
        # The write lock comes first, so two workers starting together cannot
        # both find the row free.
        conn.execute("BEGIN IMMEDIATE")
        running = read_worker_status(conn)
        if is_running(running, now):
            raise WorkerRunning(
                f"a worker is already running on this database (pid {running.pid}, last beat "
                f"{stamp(running.last_beat_at)})")
        conn.execute(
            "INSERT OR REPLACE INTO worker_status "
            "(id, instance, pid, keyed, started_at, last_beat_at, stopped_at) "
            "VALUES (1, ?, ?, ?, ?, ?, NULL)",
            (instance, pid, int(keyed), stamp(now), stamp(now)))
    return instance


def beat(conn, instance, now):
    """Record that this worker is alive; False when another worker took its place."""
    with conn:
        return conn.execute(
            "UPDATE worker_status SET last_beat_at = ? WHERE instance = ? AND stopped_at IS NULL",
            (stamp(now), instance)).rowcount == 1


def stop_worker(conn, instance, now):
    with conn:
        conn.execute("UPDATE worker_status SET stopped_at = ? WHERE instance = ?",
                     (stamp(now), instance))


class Job(NamedTuple):
    id: int
    kind: JobKind
    params: dict
    state: JobState
    error: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class JobOpen(Exception):
    """A job of this kind is already queued or running."""


def _job(row):
    if row is None:
        return None
    return Job(row["id"], JobKind(row["kind"]), json.loads(row["params"]), JobState(row["state"]),
               row["error"], _time(row["created_at"]), _time(row["started_at"]),
               _time(row["finished_at"]))


def enqueue_job(conn, kind, params, now):
    """Queue a job for the worker and return its id.

    Raises JobOpen when a job of this kind is still queued or running, and for
    a preview or a restore when either is: the jobs_open_kind and
    jobs_open_restore indexes allow one.
    """
    try:
        with conn:
            return conn.execute(
                "INSERT INTO jobs (kind, params, state, created_at) VALUES (?, ?, 'queued', ?)",
                (kind.value, json.dumps(params), stamp(now))).lastrowid
    except sqlite3.IntegrityError:
        kinds = RESTORE_KINDS if kind in RESTORE_KINDS else (kind,)
        open_job = conn.execute(
            f"SELECT id, kind, state FROM jobs WHERE kind IN ({_values(kinds)}) "
            "AND state IN ('queued', 'running') ORDER BY id").fetchone()
        raise JobOpen(f"A {open_job['kind']} job is already {open_job['state']} "
                      f"(job {open_job['id']}).") from None


def claim_job(conn, now):
    """Move the oldest queued job to running and return it, or None."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET state = 'running', started_at = ? WHERE id = ?",
                     (stamp(now), row["id"]))
        _log(conn, now, "started", row["id"])
    return read_job(conn, row["id"])


def finish_job(conn, job_id, now, error=None):
    """Close a running job as done, or as failed with this error. A job that is
    not running is left as it is."""
    with conn:
        closed = conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = ? "
            "WHERE id = ? AND state = 'running'",
            ((JobState.FAILED if error else JobState.DONE).value, error, stamp(now), job_id))
        if closed.rowcount:
            _log(conn, now, f"failed: {error}" if error else "done", job_id)


def fail_running_jobs(conn, now, reason):
    """Close every running job as failed and return how many there were. A
    starting worker calls it: no job survives the worker that ran it."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        left = [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE state = 'running'")]
        for job_id in left:
            conn.execute(
                "UPDATE jobs SET state = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (reason, stamp(now), job_id))
            _log(conn, now, f"failed: {reason}", job_id)
    return len(left)


def read_job(conn, job_id):
    return _job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def latest_job(conn, kind):
    return _job(conn.execute("SELECT * FROM jobs WHERE kind = ? ORDER BY id DESC LIMIT 1",
                             (kind.value,)).fetchone())


def latest_restore_job(conn, snapshot_id):
    """The newest preview or restore job of a snapshot, or None."""
    return _job(conn.execute(
        f"SELECT * FROM jobs WHERE kind IN ({_values(RESTORE_KINDS)}) "
        "AND json_extract(params, '$.snapshot_id') = ? ORDER BY id DESC LIMIT 1",
        (snapshot_id,)).fetchone())


def last_job_change(conn):
    """A mark that moves whenever a job is queued, changes state, logs a line or
    stores a restore line."""
    return tuple(conn.execute(
        "SELECT (SELECT COALESCE(MAX(id), 0) FROM jobs), "
        "(SELECT COALESCE(MAX(id), 0) FROM log WHERE job_id IS NOT NULL), "
        "(SELECT COALESCE(MAX(id), 0) FROM restore_lines)").fetchone())


class LogLine(NamedTuple):
    id: int
    at: datetime
    job_id: Optional[int]
    watch_id: Optional[int]
    message: str


def _log(conn, now, message, job_id=None, watch_id=None):
    conn.execute("INSERT INTO log (at, job_id, watch_id, message) VALUES (?, ?, ?, ?)",
                 (stamp(now), job_id, watch_id, message))


def append_log(conn, now, message, job_id=None, watch_id=None):
    with conn:
        _log(conn, now, message, job_id, watch_id)


def read_log(conn, job_id):
    """A job's lines, oldest first."""
    rows = conn.execute("SELECT * FROM log WHERE job_id = ? ORDER BY id", (job_id,))
    return [LogLine(r["id"], _time(r["at"]), r["job_id"], r["watch_id"], r["message"])
            for r in rows]


class Snapshot(NamedTuple):
    """The open orders at one moment. source is the file an imported snapshot
    came from, job_id the job that took any other, and orders their number."""

    id: int
    taken_at: datetime
    note: str
    source: Optional[str]
    job_id: Optional[int]
    orders: int


class SnapshotOrder(NamedTuple):
    id: int
    snapshot_id: int
    market_name: Optional[str]
    order: OpenOrder


SNAPSHOTS = """
    SELECT s.*, (SELECT COUNT(*) FROM snapshot_orders o WHERE o.snapshot_id = s.id) AS orders
    FROM snapshots s
"""


def _snapshot(row):
    return Snapshot(row["id"], _time(row["taken_at"]), row["note"], row["source"], row["job_id"],
                    row["orders"])


def insert_snapshot(conn, taken_at, note, orders, names, source=None, job_id=None):
    """Store these OpenOrders as one snapshot and return its id.

    names maps a condition id to its market's name. A source that is already
    stored raises sqlite3.IntegrityError and stores nothing.
    """
    with conn:
        snapshot_id = conn.execute(
            "INSERT INTO snapshots (taken_at, note, source, job_id) VALUES (?, ?, ?, ?)",
            (stamp(taken_at), note, source, job_id)).lastrowid
        conn.executemany(
            "INSERT INTO snapshot_orders (snapshot_id, order_id, token_id, market, market_name, "
            "outcome, side, price, size, size_matched, expiration) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(snapshot_id, o.order_id, o.token_id, o.market, names.get(o.market), o.outcome,
              o.side.value, o.price, o.size, o.size_matched, stamp(o.expiration))
             for o in orders])
    return snapshot_id


def imported_sources(conn):
    """The file names of the snapshots that came from a file."""
    return {row["source"] for row in
            conn.execute("SELECT source FROM snapshots WHERE source IS NOT NULL")}


def read_snapshots(conn):
    """Every snapshot, the newest first."""
    return [_snapshot(row) for row in
            conn.execute(SNAPSHOTS + "ORDER BY s.taken_at DESC, s.id DESC")]


def read_snapshot(conn, snapshot_id):
    row = conn.execute(SNAPSHOTS + "WHERE s.id = ?", (snapshot_id,)).fetchone()
    return _snapshot(row) if row else None


def _snapshot_order(row):
    return SnapshotOrder(row["id"], row["snapshot_id"], row["market_name"], OpenOrder(
        row["order_id"], row["token_id"], row["market"], row["outcome"], Side(row["side"]),
        row["price"], row["size"], row["size_matched"], _time(row["expiration"])))


def read_snapshot_orders(conn, snapshot_id):
    """A snapshot's orders, in the order the CLOB listed them."""
    rows = conn.execute("SELECT * FROM snapshot_orders WHERE snapshot_id = ? ORDER BY id",
                        (snapshot_id,))
    return [_snapshot_order(row) for row in rows]


class RestoreLine(NamedTuple):
    """One snapshot order in a preview or a restore job. size is what was left
    of the order to place. order_id is the new order's id, on a line a restore
    placed and on no other. detail is the plan's reason or the CLOB's."""

    job_id: int
    order: SnapshotOrder
    action: RestoreAction
    size: float
    order_id: Optional[str]
    detail: str


def add_restore_lines(conn, lines):
    """Store these RestoreLines, all or none. A second line for one snapshot
    order in a job raises sqlite3.IntegrityError."""
    with conn:
        conn.executemany(
            "INSERT INTO restore_lines (job_id, snapshot_order_id, action, size, order_id, "
            "detail) VALUES (?, ?, ?, ?, ?, ?)",
            [(line.job_id, line.order.id, line.action.value, line.size, line.order_id,
              line.detail) for line in lines])


def read_restore_lines(conn, job_id):
    """A job's lines with their snapshot orders, in the order they were stored."""
    rows = conn.execute(
        "SELECT o.*, l.job_id, l.action, l.size AS line_size, l.order_id AS new_order_id, "
        "l.detail FROM restore_lines l JOIN snapshot_orders o ON o.id = l.snapshot_order_id "
        "WHERE l.job_id = ? ORDER BY l.id", (job_id,))
    return [RestoreLine(row["job_id"], _snapshot_order(row), RestoreAction(row["action"]),
                        row["line_size"], row["new_order_id"], row["detail"]) for row in rows]


def count_restore_lines(conn, job_id):
    """{RestoreAction: how many of the job's lines}, without the actions it has none of."""
    return {RestoreAction(row["action"]): row["n"] for row in conn.execute(
        "SELECT action, COUNT(*) AS n FROM restore_lines WHERE job_id = ? GROUP BY action",
        (job_id,))}
