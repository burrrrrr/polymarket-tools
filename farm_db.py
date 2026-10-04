"""
farm.db: the contract between the screener, the farmer, and the console

Three processes share one SQLite file: the screener CLI, the farmer daemon,
and the web console (with the screener's approve and reject standing in for
it on the command line). This module owns the schema and every statement any
of them runs, so the contract lives in one place.

Two processes write at the same time, so the file is opened in WAL mode with
a busy timeout, and every column has exactly one writer process:

  markets         scan columns (slug, question, end_date, rewards_active,
                  rank_score, metrics, first_seen, last_scanned): screener.
                  Decisions (status, p_true, naked_budget_usd, note, paused,
                  naked_paused, capital_usd): console or CLI.
                  baseline_fav, baseline_long: farmer, live only.
                  One exception: the live farmer retires a market by moving
                  status and note from 'approved' to 'retired' in a single
                  conditional UPDATE, so it never overwrites a human decision.
  farm_state      farmer. One row per managed market, replaced every cycle.
  farmer_status   farmer. A single row about the running process.
  farm_events     farmer. Changes only (places, cancels, fills, phase
                  transitions, ...), never per-cycle holds.
  farm_mids       farmer. One midpoint sample per managed market per minute,
                  in dry run too, kept 7 days, for the console's mid chart.
  rewards_daily   farmer, live only.
  settings        console or CLI. Overrides of the farmer's command-line
                  config, read every cycle.
  commands        the console inserts rows; the farmer only stamps
                  consumed_at and result.

Controls a human can repeat or a crash can interrupt (pause, resume, retire,
cancel-all) are level-triggered columns on markets, not commands: the farmer
reads the level every cycle and converges to it, however many times it was
set and whether or not the farmer was running. Only stopping the farmer is
an edge, and it is a command.

Times are ISO 8601 UTC strings to the second, so they sort as text.
"""

import json
import math
import sqlite3
from datetime import datetime, timedelta
from enum import Enum
from typing import NamedTuple, Optional

STATUSES = ("candidate", "approved", "rejected", "retired")

BUSY_TIMEOUT_S = 5
EVENTS_KEPT_DAYS = 30
MIDS_KEPT_DAYS = 7

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    slug TEXT,
    question TEXT,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
    p_true REAL,
    naked_budget_usd REAL,
    note TEXT,
    end_date TEXT,
    rewards_active INTEGER,
    rank_score REAL,
    metrics TEXT,
    first_seen TEXT,
    last_scanned TEXT,
    baseline_fav REAL,
    baseline_long REAL,
    paused INTEGER NOT NULL DEFAULT 0,
    naked_paused INTEGER NOT NULL DEFAULT 0,
    capital_usd REAL
);

CREATE TABLE IF NOT EXISTS farm_state (
    condition_id TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    phase TEXT NOT NULL,
    phase_reason TEXT,
    phase_until TEXT,
    book_mid REAL,
    mid REAL,
    q1 REAL,
    q2 REAL,
    capital_usd REAL,
    naked_used_usd REAL,
    position TEXT,
    slots TEXT,
    book TEXT,
    naked_paused_until TEXT,
    backoff TEXT,
    scoring_pct TEXT
);

CREATE TABLE IF NOT EXISTS farmer_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL CHECK (mode IN ('dry', 'live')),
    pid INTEGER,
    started_at TEXT,
    last_cycle_at TEXT,
    interval_s REAL,
    heartbeat INTEGER,
    managed INTEGER,
    config TEXT,
    stopped_at TEXT
);

CREATE TABLE IF NOT EXISTS farm_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    condition_id TEXT,
    kind TEXT NOT NULL,
    slot TEXT,
    price REAL,
    size REAL,
    score REAL,
    message TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS farm_events_at ON farm_events (at);

CREATE TABLE IF NOT EXISTS farm_mids (
    condition_id TEXT NOT NULL,
    at TEXT NOT NULL,
    mid REAL,
    book_mid REAL,
    fav_bid REAL,
    long_bid REAL,
    PRIMARY KEY (condition_id, at)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    consumed_at TEXT,
    result TEXT
);

CREATE TABLE IF NOT EXISTS rewards_daily (
    date TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    earnings_usd REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, condition_id)
);
"""

# markets columns added after the first release, so open_db can bring an
# older database up to date. CREATE TABLE IF NOT EXISTS covers new tables.
ADDED_MARKET_COLUMNS = (
    ("baseline_fav", "REAL"),
    ("baseline_long", "REAL"),
    ("paused", "INTEGER NOT NULL DEFAULT 0"),
    ("naked_paused", "INTEGER NOT NULL DEFAULT 0"),
    ("capital_usd", "REAL"),
)


def stamp(dt):
    return dt.isoformat(timespec="seconds") if dt is not None else None


def open_db(path):
    """Connect, migrating the file in place; running it again changes nothing."""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
    conn.executescript(SCHEMA)
    present = {row["name"] for row in conn.execute("PRAGMA table_info(markets)")}
    with conn:
        for name, kind in ADDED_MARKET_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE markets ADD COLUMN {name} {kind}")
    return conn


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------

class ScanRow(NamedTuple):
    condition_id: str
    slug: str
    question: str
    end_date: str
    rank_score: float
    metrics: str


def store_scan(conn, rows, reward_ids, now):
    """Upsert this scan's rows and flag markets that left the reward set.

    Human decisions (status, p_true, naked_budget_usd, note, paused,
    naked_paused, capital_usd) are never touched, so rescanning is always
    safe. Retiring is the farmer's call.
    """
    at = stamp(now)
    with conn:
        conn.executemany(
            """
            INSERT INTO markets (condition_id, slug, question, end_date,
                rewards_active, rank_score, metrics, first_seen, last_scanned)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT (condition_id) DO UPDATE SET
                slug = excluded.slug,
                question = excluded.question,
                end_date = excluded.end_date,
                rewards_active = 1,
                rank_score = excluded.rank_score,
                metrics = excluded.metrics,
                last_scanned = excluded.last_scanned
            """,
            [(r.condition_id, r.slug, r.question, r.end_date, r.rank_score,
              r.metrics, at, at) for r in rows],
        )
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS reward_ids (id TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM reward_ids")
        conn.executemany(
            "INSERT OR IGNORE INTO reward_ids VALUES (?)",
            [(c,) for c in reward_ids if c],
        )
        conn.execute(
            "UPDATE markets SET rewards_active = "
            "(condition_id IN (SELECT id FROM reward_ids))"
        )


def find_market(conn, key):
    return conn.execute(
        "SELECT * FROM markets WHERE condition_id = ? OR slug = ?", (key, key)
    ).fetchone()


def latest_scan(conn):
    return conn.execute("SELECT MAX(last_scanned) FROM markets").fetchone()[0]


def list_markets(conn, status, limit):
    """Markets by rank; status 'all' lists every status."""
    where, params = ("", ()) if status == "all" else (" WHERE status = ?", (status,))
    return conn.execute(
        f"SELECT * FROM markets{where} ORDER BY rank_score DESC LIMIT ?",
        params + (limit,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Console and CLI: decisions and level-triggered controls
# ---------------------------------------------------------------------------

def approve_market(conn, condition_id, p_true, budget, capital_usd=None):
    """Approve with the human's terms; capital_usd None keeps the current one."""
    with conn:
        conn.execute(
            "UPDATE markets SET status = 'approved', p_true = ?, naked_budget_usd = ?, "
            "capital_usd = COALESCE(?, capital_usd) WHERE condition_id = ?",
            (p_true, budget, capital_usd, condition_id),
        )


def reject_market(conn, condition_id, note):
    with conn:
        conn.execute(
            "UPDATE markets SET status = 'rejected', note = COALESCE(?, note) "
            "WHERE condition_id = ?",
            (note, condition_id),
        )


def retire_market(conn, condition_id, note):
    with conn:
        conn.execute(
            "UPDATE markets SET status = 'retired', note = COALESCE(?, note) "
            "WHERE condition_id = ?",
            (note, condition_id),
        )


def set_paused(conn, condition_id, paused):
    with conn:
        conn.execute("UPDATE markets SET paused = ? WHERE condition_id = ?",
                     (int(paused), condition_id))


def set_naked_paused(conn, condition_id, paused):
    with conn:
        conn.execute("UPDATE markets SET naked_paused = ? WHERE condition_id = ?",
                     (int(paused), condition_id))


def set_capital(conn, condition_id, capital_usd):
    """Per-market capital; None falls back to the farmer's configured capital."""
    if capital_usd is not None:
        capital_usd = parse_setting("capital", json.dumps(capital_usd))
    with conn:
        conn.execute("UPDATE markets SET capital_usd = ? WHERE condition_id = ?",
                     (capital_usd, condition_id))


def cancel_all(conn):
    """Pause every approved market, which takes all their orders down."""
    with conn:
        conn.execute("UPDATE markets SET paused = 1 WHERE status = 'approved'")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Bound(NamedTuple):
    low: float
    high: float = math.inf
    low_open: bool = False

    def admits(self, value):
        above = value > self.low if self.low_open else value >= self.low
        return above and value <= self.high


# The farmer config fields the console may override, with their valid ranges.
# Keys are FarmConfig field names.
SETTINGS = {
    "capital": Bound(0, low_open=True),
    "bid_offset": Bound(0, 0.5),
    "ask_offset": Bound(0, 0.5),
    "min_capture": Bound(0, 0.5),
    "ev_margin": Bound(0, 0.5),
    "min_fav": Bound(0, 1, low_open=True),
    "min_low_score": Bound(0, 1, low_open=True),
    "keep_ratio": Bound(0, 1),
    "drop_threshold": Bound(0, 1, low_open=True),
    "drop_window_s": Bound(0, low_open=True),
    "cooldown_s": Bound(0),
    "naked_cooldown_s": Bound(0),
    "expiry_buffer_days": Bound(0),
}


def parse_setting(key, text):
    """A settings value (JSON text) as a float, or ValueError saying why not."""
    bound = SETTINGS.get(key)
    if bound is None:
        raise ValueError(f"unknown setting {key!r}")
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: {text!r} is not JSON") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key}: {text!r} is not a number")
    if not math.isfinite(value) or not bound.admits(value):
        raise ValueError(f"{key}: {value!r} is out of range")
    return float(value)


def put_setting(conn, key, value, now):
    parse_setting(key, json.dumps(value))
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value), stamp(now)),
        )


def delete_setting(conn, key):
    with conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def load_settings(conn):
    """({key: value} of the valid rows, {key: error} of the rest)."""
    values, errors = {}, {}
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            values[row["key"]] = parse_setting(row["key"], row["value"])
        except ValueError as e:
            errors[row["key"]] = str(e)
    return values, errors


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

class CommandKind(Enum):
    STOP = "stop"


class Command(NamedTuple):
    id: int
    kind: str
    payload: Optional[dict]


def send_command(conn, kind, now, payload=None):
    with conn:
        cursor = conn.execute(
            "INSERT INTO commands (at, kind, payload) VALUES (?, ?, ?)",
            (stamp(now), kind.value, json.dumps(payload) if payload is not None else None),
        )
    return cursor.lastrowid


def pending_commands(conn):
    """Unconsumed commands, oldest first; the farmer stamps each with consume_command."""
    rows = conn.execute(
        "SELECT id, kind, payload FROM commands WHERE consumed_at IS NULL ORDER BY id"
    ).fetchall()
    return [Command(r["id"], r["kind"], json.loads(r["payload"]) if r["payload"] else None)
            for r in rows]


def consume_command(conn, command_id, result, now):
    with conn:
        conn.execute(
            "UPDATE commands SET consumed_at = ?, result = ? "
            "WHERE id = ? AND consumed_at IS NULL",
            (stamp(now), result, command_id),
        )


def expire_commands(conn, now):
    """Consume commands sent while no farmer ran, so a stale stop is not obeyed."""
    with conn:
        return conn.execute(
            "UPDATE commands SET consumed_at = ?, "
            "result = 'expired: sent before this farmer started' "
            "WHERE consumed_at IS NULL",
            (stamp(now),),
        ).rowcount


# ---------------------------------------------------------------------------
# Farmer
# ---------------------------------------------------------------------------

def load_approved(conn):
    rows = conn.execute("SELECT * FROM markets WHERE status = 'approved'").fetchall()
    return {row["condition_id"]: row for row in rows}


def mark_retired(conn, condition_id, reason):
    """Retire an approved row; a row the human already rejected keeps its status."""
    with conn:
        conn.execute(
            "UPDATE markets SET status = 'retired', note = ? "
            "WHERE condition_id = ? AND status = 'approved'",
            (f"farmer: {reason}", condition_id),
        )


def save_baseline(conn, condition_id, fav, long):
    with conn:
        conn.execute(
            "UPDATE markets SET baseline_fav = ?, baseline_long = ? WHERE condition_id = ?",
            (fav, long, condition_id),
        )


class EventKind(Enum):
    PLACE = "PLACE"
    CANCEL = "CANCEL"
    FILL = "FILL"
    PHASE = "PHASE"
    ALERT = "ALERT"
    REJECT = "REJECT"
    BACKOFF = "BACKOFF"
    RETIRE = "RETIRE"
    START = "START"
    STOP = "STOP"
    SETTINGS = "SETTINGS"


class Event(NamedTuple):
    at: datetime
    kind: EventKind
    message: str
    condition_id: Optional[str] = None
    slot: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    score: Optional[float] = None
    data: Optional[dict] = None


class FarmState(NamedTuple):
    """One managed market's farm_state row; the dict fields are stored as JSON."""

    condition_id: str
    updated_at: datetime
    phase: str
    phase_reason: str
    phase_until: Optional[datetime]
    book_mid: Optional[float]
    mid: Optional[float]
    q1: float
    q2: float
    capital_usd: float
    naked_used_usd: float
    position: Optional[dict]
    slots: dict
    book: dict
    naked_paused_until: Optional[datetime]
    backoff: dict
    scoring_pct: Optional[float]


class MidSample(NamedTuple):
    """A market's midpoints and the bids it rests on after one cycle.

    fav_bid and long_bid are each slot's own price (the longshot bid in
    longshot terms), or None when the slot rests nothing.
    """

    condition_id: str
    at: datetime
    mid: Optional[float]
    book_mid: Optional[float]
    fav_bid: Optional[float]
    long_bid: Optional[float]


class FarmerStatus(NamedTuple):
    mode: str
    pid: int
    started_at: datetime
    last_cycle_at: datetime
    interval_s: float
    heartbeat: bool
    managed: int
    config: dict
    stopped_at: Optional[datetime] = None


def _json(value):
    return json.dumps(value) if value is not None else None


def publish(conn, status, states=None, events=(), mids=()):
    """Write one cycle's observability in one transaction.

    states None leaves farm_state alone; a list replaces it, deleting the rows
    of markets the farmer no longer manages.
    """
    with conn:
        if states is not None:
            conn.executemany(
                "INSERT OR REPLACE INTO farm_state VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(s.condition_id, stamp(s.updated_at), s.phase, s.phase_reason,
                  stamp(s.phase_until), s.book_mid, s.mid, s.q1, s.q2, s.capital_usd,
                  s.naked_used_usd, _json(s.position), _json(s.slots), _json(s.book),
                  stamp(s.naked_paused_until), _json(s.backoff), _json(s.scoring_pct))
                 for s in states],
            )
            keep = [s.condition_id for s in states]
            conn.execute(
                f"DELETE FROM farm_state WHERE condition_id NOT IN "
                f"({', '.join('?' * len(keep))})",
                keep,
            )
        conn.executemany(
            "INSERT INTO farm_events (at, condition_id, kind, slot, price, size, "
            "score, message, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(stamp(e.at), e.condition_id, e.kind.value, e.slot, e.price, e.size,
              e.score, e.message, _json(e.data)) for e in events],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO farm_mids VALUES (?, ?, ?, ?, ?, ?)",
            [(m.condition_id, stamp(m.at), m.mid, m.book_mid, m.fav_bid, m.long_bid)
             for m in mids],
        )
        conn.execute(
            "INSERT OR REPLACE INTO farmer_status (id, mode, pid, started_at, "
            "last_cycle_at, interval_s, heartbeat, managed, config, stopped_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (status.mode, status.pid, stamp(status.started_at),
             stamp(status.last_cycle_at), status.interval_s, int(status.heartbeat),
             status.managed, json.dumps(status.config), stamp(status.stopped_at)),
        )


def prune_events(conn, now):
    with conn:
        return conn.execute(
            "DELETE FROM farm_events WHERE at < ?",
            (stamp(now - timedelta(days=EVENTS_KEPT_DAYS)),),
        ).rowcount


def prune_mids(conn, now):
    with conn:
        return conn.execute(
            "DELETE FROM farm_mids WHERE at < ?",
            (stamp(now - timedelta(days=MIDS_KEPT_DAYS)),),
        ).rowcount


def store_rewards(conn, date, earnings, now):
    """Upsert one day's {condition_id: earnings_usd}."""
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO rewards_daily (date, condition_id, earnings_usd, "
            "fetched_at) VALUES (?, ?, ?, ?)",
            [(date, cid, usd, stamp(now)) for cid, usd in earnings.items()],
        )
