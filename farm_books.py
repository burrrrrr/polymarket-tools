"""
farm_books.db: the favorite books of the markets the farmer quotes, kept for
a backtest of how much rests ahead of a favorite ask.

A file of its own beside farm.db, written by the farmer alone, so bulky
research data never bloats the control surface the other processes share.
Every BOOK_SAMPLE_S each quoting market gets one row: the book mid (without
our orders), the favorite levels within BOOK_RECORD_DEPTH of it as the CLOB
showed them, and our resting orders as (slot, price, size) in their own
token's prices, so a backtest can place itself in the queue. Live, the
levels include our orders; in a dry run they do not, and `ours` holds the
simulated ones. The levels are zlib-compressed JSON, [bids, asks] of
[price, size] best first: at a 5c depth a row holds about 28 levels in about
250 bytes, some 17 MB a day at 237 markets, where plain JSON would take
about 46 MB. Rows older than
BOOK_KEPT_DAYS are pruned when the recorder opens the file.

The farmer never writes here itself. Each cycle hands its samples to a
BookRecorder, whose own thread writes them, so a slow or failing disk never
holds up a cycle: a failed write warns and drops its batch, and a batch
that finds the queue full is dropped too.
"""

import json
import os
import queue
import sqlite3
import threading
import zlib
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from farm_db import stamp

FILE_NAME = "farm_books.db"
BOOK_SAMPLE_S = 300
BOOK_RECORD_DEPTH = 0.05
BOOK_KEPT_DAYS = 45
# About eight hours of samples, should the writer stall.
QUEUE_BATCHES = 100
WRITE_TICK_S = 1.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY,
    condition_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS books (
    market INTEGER NOT NULL REFERENCES markets(id),
    at TEXT NOT NULL,
    mid REAL NOT NULL,
    levels BLOB NOT NULL,
    ours TEXT NOT NULL,
    PRIMARY KEY (market, at)
) WITHOUT ROWID;
"""


class BookSample(NamedTuple):
    """One market's favorite book at one time. bids and asks are
    ((price, size), ...) best first within BOOK_RECORD_DEPTH of mid; ours is
    ((slot name, price, size), ...)."""

    condition_id: str
    at: datetime
    mid: float
    bids: tuple
    asks: tuple
    ours: tuple


def books_path(db_path):
    """farm_books.db in the directory of the farm.db at db_path."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), FILE_NAME)


def sample(condition_id, at, mid, bids, asks, ours):
    """The BookSample of a book, its levels cut to BOOK_RECORD_DEPTH of mid."""
    def near(levels):
        return tuple((p, s) for p, s in levels if abs(p - mid) <= BOOK_RECORD_DEPTH + 1e-9)

    return BookSample(condition_id, at, mid, near(bids), near(asks), tuple(ours))


def open_books(path):
    conn = sqlite3.connect(path, timeout=1)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def _compact(value):
    return json.dumps(value, separators=(",", ":"))


def record(conn, samples):
    with conn:
        conn.executemany("INSERT OR IGNORE INTO markets (condition_id) VALUES (?)",
                         [(s.condition_id,) for s in samples])
        conn.executemany(
            "INSERT OR REPLACE INTO books VALUES "
            "((SELECT id FROM markets WHERE condition_id = ?), ?, ?, ?, ?)",
            [(s.condition_id, stamp(s.at), s.mid,
              zlib.compress(_compact([s.bids, s.asks]).encode()), _compact(s.ours))
             for s in samples],
        )


def prune(conn, now):
    with conn:
        return conn.execute("DELETE FROM books WHERE at < ?",
                            (stamp(now - timedelta(days=BOOK_KEPT_DAYS)),)).rowcount


def read_books(conn, condition_id=None, since=None):
    """The samples of one market, or all, from `since` on, oldest first."""
    sql = ("SELECT condition_id, at, mid, levels, ours FROM books "
           "JOIN markets ON markets.id = books.market WHERE 1")
    params = []
    if condition_id is not None:
        sql += " AND condition_id = ?"
        params.append(condition_id)
    if since is not None:
        sql += " AND at >= ?"
        params.append(stamp(since))
    samples = []
    for cid, at, mid, levels, ours in conn.execute(sql + " ORDER BY at, condition_id", params):
        bids, asks = json.loads(zlib.decompress(levels))
        samples.append(BookSample(cid, datetime.fromisoformat(at), mid,
                                  tuple(map(tuple, bids)), tuple(map(tuple, asks)),
                                  tuple(map(tuple, json.loads(ours)))))
    return samples


class BookRecorder:
    """Writes the batches of BookSamples put to it into the file at path, on
    a thread of its own once started (drain writes on the caller's). The
    connection belongs to the thread that first drains, which prunes the
    file as it opens it."""

    def __init__(self, path, stop, warn):
        self.path = path
        self._stop = stop
        self._warn = warn
        self._queue = queue.Queue(QUEUE_BATCHES)
        self._conn = None
        self._thread = None

    def put(self, samples):
        """Hand over a batch without waiting."""
        if not samples:
            return
        try:
            self._queue.put_nowait(list(samples))
        except queue.Full:
            self._warn(f"Warning: book recorder queue full; dropping {len(samples)} samples")

    def drain(self):
        """Write every batch queued so far, dropping one that fails."""
        while True:
            try:
                samples = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if self._conn is None:
                    self._conn = open_books(self.path)
                    prune(self._conn, datetime.now(timezone.utc))
                record(self._conn, samples)
            except Exception as e:
                self._warn(f"Warning: recording books to {self.path} failed ({e}); "
                           f"dropped {len(samples)} samples")

    def run(self):
        try:
            while not self._stop.wait(WRITE_TICK_S):
                self.drain()
            self.drain()
        finally:
            self.close()

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def join(self, timeout):
        if self._thread is not None:
            self._thread.join(timeout)

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
