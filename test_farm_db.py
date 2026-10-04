import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import farm_db
from farm_db import CommandKind
from reward_farmer import FarmConfig

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

# The markets table as the screener created it before this module existed.
PRE_CHANGE_MARKETS = """
CREATE TABLE markets (
    condition_id TEXT PRIMARY KEY,
    slug TEXT,
    question TEXT,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'approved', 'rejected', 'retired')),
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
    baseline_long REAL
)
"""

NEW_TABLES = {"farm_state", "farmer_status", "farm_events", "settings", "commands",
              "rewards_daily", "farm_mids"}


class ScratchDb(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "farm.db")

    def open(self):
        conn = farm_db.open_db(self.path)
        self.addCleanup(conn.close)
        return conn


def schema(path):
    conn = sqlite3.connect(path)
    try:
        tables = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        columns = conn.execute("SELECT * FROM pragma_table_info('markets')").fetchall()
        rows = conn.execute("SELECT * FROM markets ORDER BY condition_id").fetchall()
    finally:
        conn.close()
    return tables, columns, rows


class MigrationTest(ScratchDb):
    def make_old_db(self, create_sql, rows):
        conn = sqlite3.connect(self.path)
        conn.execute(create_sql)
        for row in rows:
            conn.execute(f"INSERT INTO markets ({', '.join(row)}) VALUES "
                         f"({', '.join('?' * len(row))})", tuple(row.values()))
        conn.commit()
        conn.close()

    def test_pre_change_db_gains_columns_and_tables_and_keeps_rows(self):
        self.make_old_db(PRE_CHANGE_MARKETS, [
            {"condition_id": "0xa", "slug": "a", "status": "approved", "p_true": 0.97,
             "naked_budget_usd": 25.0, "baseline_fav": 12.0, "baseline_long": 0.0},
            {"condition_id": "0xb", "slug": "b", "status": "rejected", "note": "risky"},
        ])
        conn = farm_db.open_db(self.path)
        rows = {r["condition_id"]: dict(r) for r in conn.execute("SELECT * FROM markets")}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()

        self.assertLessEqual(NEW_TABLES, tables)
        self.assertEqual(mode, "wal")
        a = rows["0xa"]
        self.assertEqual((a["status"], a["p_true"], a["naked_budget_usd"], a["baseline_fav"]),
                         ("approved", 0.97, 25.0, 12.0))
        self.assertEqual((a["paused"], a["naked_paused"], a["capital_usd"]), (0, 0, None))
        self.assertEqual((rows["0xb"]["status"], rows["0xb"]["note"]), ("rejected", "risky"))

    def test_first_release_db_without_baselines_migrates(self):
        self.make_old_db("CREATE TABLE markets (condition_id TEXT PRIMARY KEY, status TEXT)",
                         [{"condition_id": "0xa", "status": "approved"}])
        row = self.open().execute("SELECT * FROM markets").fetchone()
        self.assertEqual(row["status"], "approved")
        self.assertEqual((row["baseline_fav"], row["paused"], row["capital_usd"]), (None, 0, None))

    def test_migrating_twice_changes_nothing(self):
        self.make_old_db(PRE_CHANGE_MARKETS, [{"condition_id": "0xa", "status": "approved"}])
        farm_db.open_db(self.path).close()
        once = schema(self.path)
        farm_db.open_db(self.path).close()
        self.assertEqual(schema(self.path), once)


class SettingsTest(ScratchDb):
    def test_settings_cover_exactly_the_farm_config(self):
        self.assertEqual(set(farm_db.SETTINGS), set(FarmConfig._fields))

    def test_parse_accepts_numbers_in_range(self):
        self.assertEqual(farm_db.parse_setting("bid_offset", "0.015"), 0.015)
        self.assertEqual(farm_db.parse_setting("cooldown_s", "0"), 0.0)
        self.assertEqual(farm_db.parse_setting("capital", "250"), 250.0)

    def test_parse_rejects_bad_values(self):
        for key, text in (
            ("bid_offset", "abc"),
            ("bid_offset", '"0.01"'),
            ("bid_offset", "true"),
            ("bid_offset", "null"),
            ("bid_offset", "NaN"),
            ("bid_offset", "-0.01"),
            ("bid_offset", "0.6"),
            ("capital", "0"),
            ("min_fav", "1.5"),
            ("drop_window_s", "0"),
            ("no_such_key", "1"),
        ):
            with self.subTest(key=key, text=text), self.assertRaises(ValueError):
                farm_db.parse_setting(key, text)

    def test_put_refuses_a_bad_value_and_load_splits_bad_rows_out(self):
        conn = self.open()
        with self.assertRaises(ValueError):
            farm_db.put_setting(conn, "keep_ratio", 2, NOW)
        farm_db.put_setting(conn, "keep_ratio", 0.5, NOW)
        with conn:
            conn.execute("INSERT INTO settings VALUES ('min_fav', '\"high\"', ?)", (NOW.isoformat(),))
        values, errors = farm_db.load_settings(conn)
        self.assertEqual(values, {"keep_ratio": 0.5})
        self.assertEqual(set(errors), {"min_fav"})


    def test_scan_rows_are_not_farmer_settings(self):
        conn = self.open()
        farm_db.put_scan_params(conn, {"min_fav": 0.92}, NOW)
        funnel = farm_db.ScanFunnel(NOW, 60.0, 10, 5, 2, {"window too short": 5}, {})
        farm_db.store_scan(conn, [], [], NOW, funnel)
        self.assertEqual(farm_db.load_settings(conn), ({}, {}))
        self.assertEqual(farm_db.load_scan_params(conn), {"min_fav": 0.92})
        self.assertEqual(farm_db.load_scan_funnel(conn), funnel)

    def test_put_settings_writes_all_or_nothing(self):
        conn = self.open()
        with self.assertRaises(ValueError):
            farm_db.put_settings(conn, {"bid_offset": 0.02, "keep_ratio": 5}, NOW)
        self.assertEqual(farm_db.load_settings(conn), ({}, {}))
        farm_db.put_settings(conn, {"bid_offset": 0.02, "keep_ratio": 0.5}, NOW)
        self.assertEqual(farm_db.load_settings(conn)[0], {"bid_offset": 0.02, "keep_ratio": 0.5})


def seed(conn, *condition_ids):
    farm_db.store_scan(conn, [
        farm_db.ScanRow(cid, cid, "?", NOW.isoformat(), 1.0, "{}") for cid in condition_ids
    ], condition_ids, NOW)


class ControlsTest(ScratchDb):
    def test_cancel_all_pauses_only_approved_markets(self):
        conn = self.open()
        seed(conn, "0xa", "0xb")
        farm_db.approve_market(conn, "0xa", 0.97, 25.0)
        farm_db.cancel_all(conn)
        paused = dict(conn.execute("SELECT condition_id, paused FROM markets").fetchall())
        self.assertEqual(paused, {"0xa": 1, "0xb": 0})

    def test_approve_without_capital_keeps_the_set_capital(self):
        conn = self.open()
        seed(conn, "0xa")
        farm_db.approve_market(conn, "0xa", 0.97, 25.0, 200.0)
        farm_db.approve_market(conn, "0xa", 0.98, 25.0)
        row = farm_db.find_market(conn, "0xa")
        self.assertEqual((row["p_true"], row["capital_usd"]), (0.98, 200.0))

    def test_set_approval_sets_exact_terms_and_re_approves(self):
        conn = self.open()
        seed(conn, "0xa")
        farm_db.approve_market(conn, "0xa", 0.97, 25.0, 200.0)
        farm_db.retire_market(conn, "0xa", "done")
        farm_db.set_approval(conn, "0xa", 0.98, 10.0, None)
        row = farm_db.find_market(conn, "0xa")
        self.assertEqual((row["status"], row["p_true"], row["naked_budget_usd"],
                          row["capital_usd"]), ("approved", 0.98, 10.0, None))

    def test_cancel_all_counts_the_markets_it_paused(self):
        conn = self.open()
        seed(conn, "0xa", "0xb")
        for cid in ("0xa", "0xb"):
            farm_db.approve_market(conn, cid, 0.97, 25.0)
        farm_db.set_paused(conn, "0xb", True)
        self.assertEqual(farm_db.cancel_all(conn), 1)
        self.assertEqual(farm_db.cancel_all(conn), 0)

    def test_rescan_leaves_the_controls_alone(self):
        conn = self.open()
        seed(conn, "0xa")
        farm_db.approve_market(conn, "0xa", 0.97, 25.0, 200.0)
        farm_db.set_paused(conn, "0xa", True)
        farm_db.set_naked_paused(conn, "0xa", True)
        seed(conn, "0xa")
        row = farm_db.find_market(conn, "0xa")
        self.assertEqual((row["status"], row["paused"], row["naked_paused"], row["capital_usd"]),
                         ("approved", 1, 1, 200.0))

    def test_commands_are_consumed_once_and_stale_ones_expire(self):
        conn = self.open()
        first = farm_db.send_command(conn, CommandKind.STOP, NOW)
        self.assertEqual(farm_db.expire_commands(conn, NOW), 1)
        second = farm_db.send_command(conn, CommandKind.STOP, NOW)
        self.assertEqual([c.id for c in farm_db.pending_commands(conn)], [second])
        farm_db.consume_command(conn, second, "stopping", NOW)
        farm_db.consume_command(conn, second, "again", NOW)
        results = dict(conn.execute("SELECT id, result FROM commands").fetchall())
        self.assertTrue(results[first].startswith("expired"))
        self.assertEqual(results[second], "stopping")
        self.assertEqual(farm_db.pending_commands(conn), [])


class PublishTest(ScratchDb):
    def state(self, cid):
        return farm_db.FarmState(cid, NOW, "QUOTING", "", None, 0.99, 0.99, 1.0, 1.0, 500.0,
                                 0.0, None, {}, {"bids": [], "asks": []}, None, {}, None)

    def status(self):
        return farm_db.FarmerStatus("dry", 1, NOW, NOW, 5.0, False, 1, {"capital": 500.0})

    def test_publish_replaces_farm_state_and_appends_events(self):
        conn = self.open()
        farm_db.publish(conn, self.status(), [self.state("0xa"), self.state("0xb")])
        event = farm_db.Event(NOW, farm_db.EventKind.PLACE, "PLACE", "0xa", "FAV_BID", 0.98,
                              10.0, 0.5, {"k": 1})
        farm_db.publish(conn, self.status(), [self.state("0xb")], [event])
        self.assertEqual([r[0] for r in conn.execute("SELECT condition_id FROM farm_state")],
                         ["0xb"])
        row = conn.execute("SELECT * FROM farm_events").fetchone()
        self.assertEqual((row["kind"], row["slot"], json.loads(row["data"])),
                         ("PLACE", "FAV_BID", {"k": 1}))
        farm_db.publish(conn, self.status(), None)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM farm_state").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM farmer_status").fetchone()[0], 1)

    def test_prune_drops_events_older_than_thirty_days(self):
        conn = self.open()
        old = farm_db.Event(NOW - timedelta(days=31), farm_db.EventKind.START, "old")
        new = farm_db.Event(NOW - timedelta(days=29), farm_db.EventKind.START, "new")
        farm_db.publish(conn, self.status(), None, [old, new])
        self.assertEqual(farm_db.prune_events(conn, NOW), 1)
        self.assertEqual([r[0] for r in conn.execute("SELECT message FROM farm_events")], ["new"])


if __name__ == "__main__":
    unittest.main()
