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
  watches          the console inserts an armed row and later asks, once, for
                   it to be disarmed; the worker writes state and every
                   column after it. Nothing else writes state.
  log             worker. What a job or a watch did, and every change of a
                   job's state.

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
from polymarket.orders import TICKS, LimitOrder, OpenOrder, Side, on_grid

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


class WatchState(Enum):
    """Where a watch is. ARMED and FIRING are open; the rest are final."""

    ARMED = "armed"
    FIRING = "firing"
    FIRED = "fired"
    REHEARSED = "rehearsed"
    REJECTED = "rejected"
    MISSED = "missed"
    FAILED = "failed"
    DISARMED = "disarmed"


OPEN_STATES = (WatchState.ARMED, WatchState.FIRING)


class Reason(Enum):
    """Why a closed watch closed as it did. may_rest marks the two cases where
    an order may be on the book without the row knowing."""

    AT_TARGET = "at_target"
    GUARD_NO_QUOTE = "guard_no_quote"
    GUARD_STALE = "guard_stale"
    GUARD_PRICE = "guard_price"
    VENUE = "venue"
    SIGN = "sign"
    STOPPED = "stopped"
    UNANSWERED = "unanswered"
    INTERRUPTED = "interrupted"

    @property
    def may_rest(self):
        return self in (Reason.UNANSWERED, Reason.INTERRUPTED)


class Guard(NamedTuple):
    """The side's own best price (the bid for a BUY) must be within tolerance
    of price, in a quote no older than max_age_s, when the tick changes."""

    price: float
    tolerance: float
    max_age_s: float


GUARD_DEFAULT = Guard(0.99, 0.005, 5.0)
TARGET_TICK_DEFAULT = "0.001"
# The CLOB refuses an order that expires within a minute.
EXPIRES_MIN_S = 60
INTERRUPTED_LINE = "found firing when the worker started: the order may rest"


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

CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market TEXT NOT NULL,
    label TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ({_values(Side)})),
    price REAL NOT NULL,
    size REAL NOT NULL,
    expires_after_s INTEGER NOT NULL,
    target_tick TEXT NOT NULL,
    guard_price REAL,
    guard_tolerance REAL,
    guard_max_age_s REAL,
    dry_run INTEGER NOT NULL,
    disarm_requested_at TEXT,
    state TEXT NOT NULL DEFAULT 'armed' CHECK (state IN ({_values(WatchState)})),
    reason TEXT CHECK (reason IS NULL OR reason IN ({_values(Reason)})),
    detail TEXT,
    tick TEXT,
    bid REAL,
    ask REAL,
    seen_at TEXT,
    triggered_at TEXT,
    source TEXT,
    send_ms REAL,
    post_ms REAL,
    attempts INTEGER,
    order_id TEXT,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    CHECK ((guard_price IS NULL) = (guard_tolerance IS NULL)
           AND (guard_price IS NULL) = (guard_max_age_s IS NULL)),
    CHECK ((state = 'fired') = (order_id IS NOT NULL)),
    CHECK (NOT (dry_run = 1 AND state = 'fired')),
    CHECK (NOT (dry_run = 0 AND state = 'rehearsed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS watches_one_open_per_token ON watches (token_id)
    WHERE state IN ({_values(OPEN_STATES)});
CREATE TRIGGER IF NOT EXISTS watches_terminal_is_final BEFORE UPDATE OF state ON watches
    WHEN OLD.state NOT IN ({_values(OPEN_STATES)})
BEGIN
    SELECT RAISE(ABORT, 'a closed watch is final');
END;
CREATE TRIGGER IF NOT EXISTS watches_version AFTER UPDATE ON watches
BEGIN
    UPDATE watches SET version = version + 1 WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    job_id INTEGER,
    watch_id INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_job ON log (job_id);
CREATE INDEX IF NOT EXISTS log_watch ON log (watch_id);
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


def _lines(rows):
    return [LogLine(r["id"], _time(r["at"]), r["job_id"], r["watch_id"], r["message"])
            for r in rows]


def read_log(conn, job_id):
    """A job's lines, oldest first."""
    return _lines(conn.execute("SELECT * FROM log WHERE job_id = ? ORDER BY id", (job_id,)))


def read_watch_log(conn, watch_id):
    """A watch's lines, oldest first."""
    return _lines(conn.execute("SELECT * FROM log WHERE watch_id = ? ORDER BY id", (watch_id,)))


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


class NewWatch(NamedTuple):
    """A watch the console may arm. parse_watch is the one way to build it."""

    order: LimitOrder
    market: str
    label: str
    target_tick: str
    guard: Optional[Guard]
    dry_run: bool


class WatchRow(NamedTuple):
    """One watch as stored. The console's columns end at dry_run and
    disarm_requested_at; the worker owns state and everything after it: the
    quote it last published (tick, bid, ask, seen_at), the trigger, the
    timings and the outcome."""

    id: int
    created_at: datetime
    order: LimitOrder
    market: str
    label: str
    target_tick: str
    guard: Optional[Guard]
    dry_run: bool
    disarm_requested_at: Optional[datetime]
    state: WatchState
    reason: Optional[Reason]
    detail: Optional[str]
    tick: Optional[str]
    bid: Optional[float]
    ask: Optional[float]
    seen_at: Optional[datetime]
    triggered_at: Optional[datetime]
    source: Optional[str]
    send_ms: Optional[float]
    post_ms: Optional[float]
    attempts: Optional[int]
    order_id: Optional[str]
    resolved_at: Optional[datetime]

    @property
    def open(self):
        return self.state in OPEN_STATES


class Settlement(NamedTuple):
    """The closing write of a watch: its final state, why, and what was sent."""

    state: WatchState
    reason: Optional[Reason]
    detail: str
    order_id: Optional[str] = None
    attempts: Optional[int] = None


class Sample(NamedTuple):
    """The quote the worker last saw for an open watch."""

    watch_id: int
    tick: Optional[str]
    bid: Optional[float]
    ask: Optional[float]
    seen_at: datetime


def _number(text):
    try:
        return float((text or "").replace(",", "").strip())
    except ValueError:
        return None


def parse_watch(form):
    """(the NewWatch of an arm form, the errors by field): it parses whole or
    not at all. The form's fields are the columns the console writes, with
    guard "1" for a guarded watch and dry_run "1" for a rehearsal."""
    errors = {}

    def required(name):
        value = (form.get(name) or "").strip()
        if not value:
            errors[name] = "Required."
        return value

    token_id, market, label = required("token_id"), required("market"), required("label")
    try:
        side = Side((form.get("side") or "").strip().upper())
    except ValueError:
        errors["side"], side = "BUY or SELL.", None
    target_tick = (form.get("target_tick") or "").strip()
    price = _number(form.get("price"))
    if target_tick not in TICKS:
        errors["target_tick"] = f"One of {', '.join(TICKS)}."
        if price is None:
            errors["price"] = "A price between 0 and 1."
    else:
        tick = float(target_tick)
        if price is None or not tick <= price <= 1 - tick or not on_grid(price, tick):
            errors["price"] = f"A multiple of {target_tick} from {target_tick} to {1 - tick:g}."
    size = _number(form.get("size"))
    if size is None or size <= 0:
        errors["size"] = "A positive number of shares."
    guard = None
    if form.get("guard") == "1":
        guard = Guard(*(_number(form.get(f"guard_{name}"))
                        for name in ("price", "tolerance", "max_age_s")))
        if guard.price is None or not 0 < guard.price < 1:
            errors["guard_price"] = "A price between 0 and 1."
        if guard.tolerance is None or not 0 <= guard.tolerance < 1:
            errors["guard_tolerance"] = "A tolerance from 0 to 1."
        if guard.max_age_s is None or guard.max_age_s <= 0:
            errors["guard_max_age_s"] = "A positive number of seconds."
    expires = _number(form.get("expires_after_s"))
    if expires is None or expires != int(expires) or not (expires == 0 or expires >= EXPIRES_MIN_S):
        errors["expires_after_s"] = (f"0 for good-til-cancelled, or at least {EXPIRES_MIN_S} "
                                     "seconds.")
    if errors:
        return None, errors
    return NewWatch(LimitOrder(token_id, side, price, size, int(expires)), market, label,
                    target_tick, guard, form.get("dry_run") == "1"), {}


def arm_watch(conn, new, now):
    """Insert an armed watch and return its id, or None when its token already
    has an open watch: the unique index refuses the insert."""
    order, guard = new.order, new.guard or Guard(None, None, None)
    try:
        with conn:
            return conn.execute(
                "INSERT INTO watches (created_at, token_id, market, label, side, price, size, "
                "expires_after_s, target_tick, guard_price, guard_tolerance, guard_max_age_s, "
                "dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stamp(now), order.token_id, new.market, new.label, order.side.value, order.price,
                 order.size, order.expires_after_s, new.target_tick, guard.price, guard.tolerance,
                 guard.max_age_s, int(new.dry_run))).lastrowid
    except sqlite3.IntegrityError:
        return None


def request_disarm(conn, watch_id, now):
    """Ask the worker to disarm an open watch; False when it is closed or was
    already asked. The worker answers with `disarmed`, or with the order it
    had already sent."""
    with conn:
        return conn.execute(
            f"UPDATE watches SET disarm_requested_at = ? WHERE id = ? AND state IN "
            f"({_values(OPEN_STATES)}) AND disarm_requested_at IS NULL",
            (stamp(now), watch_id)).rowcount == 1


def _watch(row):
    if row is None:
        return None
    guard = None
    if row["guard_price"] is not None:
        guard = Guard(row["guard_price"], row["guard_tolerance"], row["guard_max_age_s"])
    return WatchRow(
        row["id"], _time(row["created_at"]),
        LimitOrder(row["token_id"], Side(row["side"]), row["price"], row["size"],
                   row["expires_after_s"]),
        row["market"], row["label"], row["target_tick"], guard, bool(row["dry_run"]),
        _time(row["disarm_requested_at"]), WatchState(row["state"]),
        Reason(row["reason"]) if row["reason"] else None, row["detail"], row["tick"], row["bid"],
        row["ask"], _time(row["seen_at"]), _time(row["triggered_at"]), row["source"],
        row["send_ms"], row["post_ms"], row["attempts"], row["order_id"],
        _time(row["resolved_at"]))


def read_watch(conn, watch_id):
    return _watch(conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone())


def watches(conn, limit=200):
    """Every watch, the open ones first and then the newest first."""
    return [_watch(row) for row in conn.execute(
        f"SELECT * FROM watches ORDER BY (state IN ({_values(OPEN_STATES)})) DESC, id DESC "
        "LIMIT ?", (limit,))]


def open_watches(conn):
    """The armed and firing watches, oldest first."""
    return [_watch(row) for row in conn.execute(
        f"SELECT * FROM watches WHERE state IN ({_values(OPEN_STATES)}) ORDER BY id")]


def mark_firing(conn, watch_id, triggered_at, source, tick, bid, ask, seen_at):
    """An armed watch's order is leaving: freeze the trigger and the quote the
    guard judged. A watch that is not armed is left as it is."""
    with conn:
        conn.execute(
            "UPDATE watches SET state = 'firing', triggered_at = ?, source = ?, tick = ?, "
            "bid = ?, ask = ?, seen_at = ? WHERE id = ? AND state = 'armed'",
            (stamp(triggered_at), source, tick, bid, ask, stamp(seen_at), watch_id))


def _close(conn, watch_id, settlement, now, send_ms, post_ms):
    return conn.execute(
        f"UPDATE watches SET state = ?, reason = ?, detail = ?, order_id = ?, attempts = ?, "
        f"send_ms = ?, post_ms = ?, resolved_at = ? WHERE id = ? AND state IN "
        f"({_values(OPEN_STATES)})",
        (settlement.state.value, settlement.reason.value if settlement.reason else None,
         settlement.detail, settlement.order_id, settlement.attempts, send_ms, post_ms,
         stamp(now), watch_id)).rowcount == 1


def close_watch(conn, watch_id, settlement, now, send_ms=None, post_ms=None):
    """Close an open watch with this Settlement; False when it was already
    closed, so a settlement applied twice changes nothing."""
    with conn:
        return _close(conn, watch_id, settlement, now, send_ms, post_ms)


def fail_interrupted(conn, now):
    """Every watch found firing is closed as failed, interrupted, and logged.
    A starting worker calls it: a fire is never resumed and never repeated.
    Returns the ids."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        left = [row["id"] for row in conn.execute("SELECT id FROM watches WHERE state = 'firing'")]
        for watch_id in left:
            _close(conn, watch_id, Settlement(WatchState.FAILED, Reason.INTERRUPTED, ""), now,
                   None, None)
            _log(conn, now, INTERRUPTED_LINE, watch_id=watch_id)
    return left


def publish_samples(conn, samples):
    """Store the quote the worker last saw for each armed watch, in one
    transaction. A watch that is no longer armed keeps what it has."""
    with conn:
        conn.executemany(
            "UPDATE watches SET tick = ?, bid = ?, ask = ?, seen_at = ? "
            "WHERE id = ? AND state = 'armed'",
            [(s.tick, s.bid, s.ask, stamp(s.seen_at), s.watch_id) for s in samples])


def watch_version(conn):
    """A mark that moves whenever a watch is armed, changes, is sampled or
    logs a line: the version column counts every update of a row."""
    return tuple(conn.execute(
        "SELECT (SELECT COALESCE(MAX(id), 0) FROM watches), "
        "(SELECT COALESCE(SUM(version), 0) FROM watches), "
        "(SELECT COALESCE(MAX(id), 0) FROM log WHERE watch_id IS NOT NULL)").fetchone())


def _cents(price):
    return "--" if price is None else f"{price * 100:.1f}c"


def _ago(now, then):
    seconds = int(max(0, (now - then).total_seconds()))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60}s"


def _plural(n, noun):
    return f"{n} {noun}{'' if n == 1 else 's'}"


MAY_REST = "An order may rest: check your open orders."


def describe(row, keyed, now):
    """The sentence the Sniper page shows for a watch. keyed is whether the
    running worker holds a key, or None while no worker runs."""
    state, detail = row.state, row.detail
    if state is WatchState.ARMED:
        if row.disarm_requested_at is not None:
            return "Disarm requested. Waiting for the worker to answer."
        if keyed is None:
            return "Armed. Waiting for a worker."
        if not keyed and not row.dry_run:
            return "Armed. Waiting for a keyed worker: the running worker holds no key."
        if row.seen_at is None:
            return "Armed. The worker has not read the book yet."
        return (f"Armed. Tick {row.tick}, bid {_cents(row.bid)}, ask {_cents(row.ask)}, seen "
                f"{_ago(now, row.seen_at)} ago.")
    if state is WatchState.FIRING:
        return (f"Firing. The order left {_ago(now, row.triggered_at)} ago; waiting for the "
                "exchange's answer.")
    if state is WatchState.FIRED:
        return f"Fired. Order {row.order_id} rests after {_plural(row.attempts, 'attempt')}."
    if state is WatchState.REHEARSED:
        return f"Rehearsed. A dry run: {detail}."
    if state is WatchState.REJECTED:
        return f"Rejected by your guard, nothing was sent: {detail}."
    if state is WatchState.MISSED:
        return f"Missed: the tick was already {detail} when the worker adopted the watch."
    if state is WatchState.DISARMED:
        return "Disarmed."
    return {
        Reason.VENUE: f"The exchange refused the order: {detail}.",
        Reason.UNANSWERED: f"The post got no answer ({detail}). {MAY_REST}",
        Reason.INTERRUPTED: f"The worker stopped while the order was leaving. {MAY_REST}",
        Reason.STOPPED: (f"The worker stopped inside the retry window after "
                         f"{_plural(row.attempts, 'refusal')}: {detail}. No order rests."),
        Reason.SIGN: f"The order could not be signed: {detail}.",
    }[row.reason]
