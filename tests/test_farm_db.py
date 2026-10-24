import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from farm import farm_books, farm_db
from farm.farm_db import CommandKind
from farm.reward_farmer import FarmConfig

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

# The triage table as it was while each row carried a prompt version.
VERSIONED_TRIAGE = """
CREATE TABLE triage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    at TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    p_true REAL,
    p_low REAL,
    p_high REAL,
    book_mid REAL,
    result TEXT,
    error TEXT,
    duration_s REAL,
    cost_usd REAL,
    verdict TEXT
);
CREATE INDEX triage_market ON triage (condition_id, prompt_version, at);
"""

NEW_TABLES = {"farm_state", "farmer_status", "farm_events", "settings", "commands",
              "rewards_daily", "farm_mids", "fav_buys"}


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
        self.assertEqual((a["paused"], a["naked_paused"], a["capital_usd"], a["sole_bid"]),
                         (0, 0, None, 0))
        self.assertEqual((rows["0xb"]["status"], rows["0xb"]["note"]), ("rejected", "risky"))

    def test_first_release_db_without_baselines_migrates(self):
        self.make_old_db("CREATE TABLE markets (condition_id TEXT PRIMARY KEY, status TEXT)",
                         [{"condition_id": "0xa", "status": "approved"}])
        row = self.open().execute("SELECT * FROM markets").fetchone()
        self.assertEqual(row["status"], "approved")
        self.assertEqual((row["baseline_fav"], row["paused"], row["capital_usd"]), (None, 0, None))

    def test_a_migrated_file_opens_while_another_process_writes(self):
        farm_db.open_db(self.path).close()
        writer = sqlite3.connect(self.path)
        writer.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(farm_db, "BUSY_TIMEOUT_S", 0.2):
                conn = farm_db.open_db(self.path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0], 0)
            conn.close()
        finally:
            writer.rollback()
            writer.close()

    def test_a_status_table_from_before_free_cash_gains_its_columns(self):
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE farmer_status (id INTEGER PRIMARY KEY CHECK (id = 1), "
                     "mode TEXT NOT NULL, pid INTEGER, started_at TEXT, last_cycle_at TEXT, "
                     "interval_s REAL, heartbeat INTEGER, managed INTEGER, config TEXT, "
                     "stopped_at TEXT)")
        conn.execute("INSERT INTO farmer_status VALUES (1, 'live', 7, ?, ?, 5.0, 0, 3, '{}', "
                     "NULL)", (NOW.isoformat(), NOW.isoformat()))
        conn.commit()
        conn.close()
        status = farm_db.read_farmer_status(self.open())
        self.assertEqual((status.mode, status.managed, status.free_cash_usd,
                          status.effective_hurdle), ("live", 3, None, None))

    def test_migrating_twice_changes_nothing(self):
        self.make_old_db(PRE_CHANGE_MARKETS, [{"condition_id": "0xa", "status": "approved"}])
        farm_db.open_db(self.path).close()
        once = schema(self.path)
        farm_db.open_db(self.path).close()
        self.assertEqual(schema(self.path), once)


class SettingsTest(ScratchDb):
    def test_settings_cover_exactly_the_farm_config(self):
        self.assertEqual(set(farm_db.SETTINGS), set(FarmConfig._fields))

    def test_opening_deletes_a_retired_setting_and_nothing_else(self):
        conn = self.open()
        farm_db.put_setting(conn, "keep_ratio", 0.5, NOW)
        with conn:
            for key in ("fav_target_score", "depth_haircut", "fill_lookback_days",
                        "sweep_prior_weight", "stuck_share"):
                conn.execute("INSERT INTO settings VALUES (?, '0.45', ?)",
                             (key, NOW.isoformat()))
        for _ in range(2):
            reopened = self.open()
            self.assertEqual(farm_db.load_settings(reopened), ({"keep_ratio": 0.5}, {}))

    def test_parse_accepts_numbers_in_range(self):
        self.assertEqual(farm_db.parse_setting("max_lead", "0.015"), 0.015)
        self.assertEqual(farm_db.parse_setting("cooldown_s", "0"), 0.0)
        self.assertEqual(farm_db.parse_setting("capital", "250"), 250.0)
        # A buffer of 0 turns the cash scaling of the hurdle off.
        self.assertEqual(farm_db.parse_setting("cash_buffer", "0"), 0.0)
        self.assertEqual(farm_db.parse_setting("cash_buffer", "15000"), 15000.0)

    def test_parse_rejects_bad_values(self):
        for key, text in (
            ("max_lead", "abc"),
            ("max_lead", '"0.01"'),
            ("max_lead", "true"),
            ("max_lead", "null"),
            ("max_lead", "NaN"),
            ("max_lead", "-0.01"),
            ("max_lead", "0.6"),
            ("min_low_score", "0"),
            ("min_low_score", "1.5"),
            ("capital", "0"),
            ("min_fav", "1.5"),
            ("drop_window_s", "0"),
            ("hurdle_rate", "-0.1"),
            ("hurdle_rate", "10.5"),
            ("cash_buffer", "-1"),
            ("depth_haircut", "0.5"),
            ("stuck_share", "0.25"),
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
        prior = {"v": 1, "rates": [0.1], "markets": 30}
        farm_db.store_scan(conn, [], [], NOW, funnel, prior=prior)
        self.assertEqual(farm_db.load_settings(conn), ({}, {}))
        self.assertEqual(farm_db.load_scan_params(conn), {"min_fav": 0.92})
        self.assertEqual(farm_db.load_scan_funnel(conn), funnel)
        self.assertEqual(farm_db.load_scan_json(conn, farm_db.SCAN_FILL_PRIOR_KEY), prior)

    def test_triage_auto_is_not_a_farmer_setting_and_defaults_to_off(self):
        conn = self.open()
        self.assertEqual(farm_db.load_triage_auto(conn), 0)
        farm_db.put_triage_auto(conn, 20, NOW)
        self.assertEqual((farm_db.load_triage_auto(conn), farm_db.load_settings(conn)),
                         (20, ({}, {})))
        for bad in (-1, 101, 2.5, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                farm_db.put_triage_auto(conn, bad, NOW)
        with conn:
            conn.execute("UPDATE settings SET value = '\"lots\"' WHERE key = ?",
                         (farm_db.TRIAGE_AUTO_KEY,))
        self.assertEqual(farm_db.load_triage_auto(conn), 0)

    def test_a_scan_without_a_prior_keeps_the_stored_one(self):
        conn = self.open()
        farm_db.store_scan(conn, [], [], NOW, prior={"markets": 30})
        farm_db.store_scan(conn, [], [], NOW)
        self.assertEqual(farm_db.load_scan_json(conn, farm_db.SCAN_FILL_PRIOR_KEY),
                         {"markets": 30})

    def test_the_exit_prior_is_kept_apart_from_the_fill_prior(self):
        conn = self.open()
        farm_db.store_scan(conn, [], [], NOW, prior={"markets": 30})
        farm_db.store_scan(conn, [], [], NOW, exit_prior={"markets": 25})
        farm_db.store_scan(conn, [], [], NOW, prior={"markets": 31})
        self.assertEqual([farm_db.load_scan_json(conn, key) for key in
                          (farm_db.SCAN_FILL_PRIOR_KEY, farm_db.SCAN_EXIT_PRIOR_KEY)],
                         [{"markets": 31}, {"markets": 25}])
        self.assertEqual(farm_db.load_settings(conn), ({}, {}))

    def test_put_settings_writes_all_or_nothing(self):
        conn = self.open()
        with self.assertRaises(ValueError):
            farm_db.put_settings(conn, {"max_lead": 0.02, "keep_ratio": 5}, NOW)
        self.assertEqual(farm_db.load_settings(conn), ({}, {}))
        farm_db.put_settings(conn, {"max_lead": 0.02, "keep_ratio": 0.5}, NOW)
        self.assertEqual(farm_db.load_settings(conn)[0], {"max_lead": 0.02, "keep_ratio": 0.5})


def seed(conn, *condition_ids):
    farm_db.store_scan(conn, [
        farm_db.ScanRow(cid, cid, "?", NOW.isoformat(), 1.0, "{}") for cid in condition_ids
    ], condition_ids, NOW)


class StaleCandidateTest(ScratchDb):
    """A candidate the latest scan dropped must leave the review queue."""

    def scan(self, conn, at, *condition_ids):
        farm_db.store_scan(conn, [
            farm_db.ScanRow(cid, cid, "?", at.isoformat(), 1.0, "{}") for cid in condition_ids
        ], condition_ids, at)

    def setUp(self):
        super().setUp()
        self.conn = self.open()
        self.scan(self.conn, NOW, "0xstale", "0xkept", "0xapproved")
        farm_db.approve_market(self.conn, "0xapproved", 0.97, 25.0, now=NOW)
        self.scan(self.conn, NOW + timedelta(hours=1), "0xkept")

    def test_queue_holds_only_candidates_from_the_latest_scan(self):
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xkept"])

    def test_search_drops_stale_candidates_but_keeps_decided_markets(self):
        rows, total = farm_db.search_markets(
            self.conn, statuses=None, mode=None, text=None, min_days=None,
            buffer_days=7, now=NOW, limit=50, offset=0)
        self.assertEqual(({r.condition_id for r in rows}, total),
                         ({"0xkept", "0xapproved"}, 2))


class RewardsActiveTest(ScratchDb):
    """A candidate whose rewards ended leaves the queue; decided markets stay."""

    def setUp(self):
        super().setUp()
        self.conn = self.open()
        farm_db.store_scan(self.conn, [
            farm_db.ScanRow(cid, cid, "?", NOW.isoformat(), rank, "{}")
            for cid, rank in (("0xa", 4.0), ("0xb", 3.0), ("0xc", 3.0), ("0xd", 1.0),
                              ("0xapproved", 2.0))
        ], ["0xa", "0xb", "0xc", "0xd", "0xapproved"], NOW)
        farm_db.approve_market(self.conn, "0xapproved", 0.97, 25.0, now=NOW)

    def search(self):
        rows, _ = farm_db.search_markets(
            self.conn, statuses=None, mode=None, text=None, min_days=None,
            buffer_days=7, now=NOW, limit=50, offset=0)
        return {r.condition_id for r in rows}

    def test_ended_rewards_drop_a_candidate_and_returning_ones_restore_it(self):
        farm_db.set_rewards_active(self.conn, "0xb", False)
        farm_db.set_rewards_active(self.conn, "0xapproved", False)
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xa", "0xc", "0xd"])
        self.assertEqual(self.search(), {"0xa", "0xc", "0xd", "0xapproved"})
        farm_db.set_rewards_active(self.conn, "0xb", True)
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xa", "0xc", "0xb", "0xd"])

    def test_the_paying_set_decides_every_candidate_and_no_decided_market(self):
        farm_db.reject_market(self.conn, "0xd", "", farm_db.RejectReason.OTHER, NOW)
        farm_db.set_rewards_active(self.conn, "0xc", False)
        farm_db.set_candidates_rewards_active(self.conn, {"0xc"})
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xc"])
        farm_db.set_candidates_rewards_active(self.conn, {"0xa", "0xb", "0xd", "0xelsewhere"})
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xa", "0xb"])
        farm_db.retire_market(self.conn, "0xapproved", "")
        farm_db.set_candidates_rewards_active(self.conn, set())
        self.assertEqual(
            dict(self.conn.execute("SELECT condition_id, rewards_active FROM markets")),
            {"0xa": 0, "0xb": 0, "0xc": 0, "0xd": 1, "0xapproved": 1})

    def test_queue_after_a_market_keeps_its_place_once_it_has_left(self):
        self.assertEqual(farm_db.candidate_queue(self.conn, after="0xc"), ["0xb", "0xd"])
        farm_db.set_rewards_active(self.conn, "0xc", False)
        self.assertEqual(farm_db.candidate_queue(self.conn, after="0xc"), ["0xb", "0xd"])


class ConditionIdsTest(ScratchDb):
    def test_ids_with_the_status_only(self):
        conn = self.open()
        seed(conn, "0xa", "0xb", "0xc")
        farm_db.reject_market(conn, "0xa", "", farm_db.RejectReason.OTHER, NOW)
        farm_db.reject_market(conn, "0xc", "", farm_db.RejectReason.OTHER, NOW)
        farm_db.approve_market(conn, "0xb", 0.97, 25.0, now=NOW)
        self.assertEqual(farm_db.condition_ids(conn, "rejected"), frozenset({"0xa", "0xc"}))
        self.assertEqual(farm_db.condition_ids(conn, "candidate"), frozenset())


class ControlsTest(ScratchDb):
    def test_cancel_all_pauses_only_approved_markets(self):
        conn = self.open()
        seed(conn, "0xa", "0xb")
        farm_db.approve_market(conn, "0xa", 0.97, 25.0, now=NOW)
        farm_db.cancel_all(conn)
        paused = dict(conn.execute("SELECT condition_id, paused FROM markets").fetchall())
        self.assertEqual(paused, {"0xa": 1, "0xb": 0})

    def test_approve_without_capital_keeps_the_set_capital(self):
        conn = self.open()
        seed(conn, "0xa")
        farm_db.approve_market(conn, "0xa", 0.97, 25.0, 200.0, now=NOW)
        farm_db.approve_market(conn, "0xa", 0.98, 25.0, now=NOW)
        row = farm_db.find_market(conn, "0xa")
        self.assertEqual((row["p_true"], row["capital_usd"]), (0.98, 200.0))

    def test_set_approval_sets_exact_terms_and_re_approves(self):
        conn = self.open()
        seed(conn, "0xa")
        farm_db.approve_market(conn, "0xa", 0.97, 25.0, 200.0, now=NOW)
        farm_db.retire_market(conn, "0xa", "done")
        farm_db.set_approval(conn, "0xa", 0.98, 10.0, None, now=NOW)
        row = farm_db.find_market(conn, "0xa")
        self.assertEqual((row["status"], row["p_true"], row["naked_budget_usd"],
                          row["capital_usd"]), ("approved", 0.98, 10.0, None))

    def test_candidate_queue_filters_by_planned_mode(self):
        conn = self.open()
        farm_db.store_scan(conn, [
            farm_db.ScanRow(cid, cid, cid, None, rank, json.dumps({"plan": {"mode": mode}}))
            for cid, rank, mode in (("0xs", 4.0, "sole"), ("0xa", 3.0, "floor"),
                                    ("0xb", 2.0, "budget"), ("0xc", 1.0, "floor"),
                                    ("0xt", 0.5, "sole"))],
            ["0xs", "0xa", "0xb", "0xc", "0xt"], NOW)
        self.assertEqual(farm_db.candidate_queue(conn), ["0xa", "0xb", "0xc"])
        self.assertEqual(farm_db.candidate_queue(conn, "floor"), ["0xa", "0xc"])
        self.assertEqual(farm_db.candidate_queue(conn, "budget"), ["0xb"])
        self.assertEqual(farm_db.candidate_queue(conn, "sole"), ["0xs", "0xt"])
        self.assertEqual(farm_db.candidate_queue(conn, "sole", after="0xs"), ["0xt"])
        self.assertEqual(farm_db.candidate_queue(conn, after="0xa"), ["0xb", "0xc"])

    def test_search_lists_sole_candidates_only_under_their_own_mode(self):
        conn = self.open()
        farm_db.store_scan(conn, [
            farm_db.ScanRow(cid, cid, cid, None, rank, metrics)
            for cid, rank, metrics in (("0xs", 4.0, '{"plan": {"mode": "sole"}}'),
                                       ("0xt", 3.0, '{"plan": {"mode": "sole"}}'),
                                       ("0xa", 2.0, '{"plan": {"mode": "floor"}}'),
                                       ("0xbare", 1.0, "{}"))],
            ["0xs", "0xt", "0xa", "0xbare"], NOW)
        farm_db.approve_market(conn, "0xt", 0.99, 10.0, now=NOW)

        def search(mode):
            rows, total = farm_db.search_markets(
                conn, statuses=(), mode=mode, text="", min_days=None, buffer_days=7, now=NOW,
                limit=50, offset=0)
            self.assertEqual(total, len(rows))
            return [r.condition_id for r in rows]

        # Decided, a sole market lists like any other.
        self.assertEqual(search(None), ["0xt", "0xa", "0xbare"])
        self.assertEqual(search("sole"), ["0xs", "0xt"])

    def test_set_approval_paused_sets_the_flag_and_otherwise_keeps_it(self):
        conn = self.open()
        seed(conn, "0xa", "0xb")
        farm_db.set_approval(conn, "0xa", 0.98, 10.0, None, paused=True, now=NOW)
        farm_db.set_approval(conn, "0xa", 0.98, 10.0, None, now=NOW)
        farm_db.set_approval(conn, "0xb", 0.98, 10.0, None, now=NOW)
        self.assertEqual([farm_db.find_market(conn, c)["paused"] for c in ("0xa", "0xb")], [1, 0])

    def test_cancel_all_counts_the_markets_it_paused(self):
        conn = self.open()
        seed(conn, "0xa", "0xb")
        for cid in ("0xa", "0xb"):
            farm_db.approve_market(conn, cid, 0.97, 25.0, now=NOW)
        farm_db.set_paused(conn, "0xb", True)
        self.assertEqual(farm_db.cancel_all(conn), 1)
        self.assertEqual(farm_db.cancel_all(conn), 0)

    def test_rescan_leaves_the_controls_alone(self):
        conn = self.open()
        seed(conn, "0xa")
        farm_db.approve_market(conn, "0xa", 0.97, 25.0, 200.0, now=NOW)
        farm_db.set_paused(conn, "0xa", True)
        farm_db.set_naked_paused(conn, "0xa", True)
        farm_db.set_sole_bid(conn, "0xa", True)
        seed(conn, "0xa")
        row = farm_db.find_market(conn, "0xa")
        self.assertEqual((row["status"], row["paused"], row["naked_paused"], row["capital_usd"],
                          row["sole_bid"]), ("approved", 1, 1, 200.0, 1))

    def test_sole_bid_is_allowed_and_stopped_for_one_market(self):
        conn = self.open()
        seed(conn, "0xa", "0xb")

        def allowed():
            return {m.condition_id: m.sole_bid
                    for m in farm_db.read_markets(conn, statuses=("candidate",))}

        self.assertEqual(allowed(), {"0xa": False, "0xb": False})
        farm_db.set_sole_bid(conn, "0xa", True)
        self.assertEqual(allowed(), {"0xa": True, "0xb": False})
        farm_db.set_sole_bid(conn, "0xa", False)
        self.assertEqual(allowed(), {"0xa": False, "0xb": False})

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

    def test_an_end_cooldown_command_names_its_market(self):
        conn = self.open()
        sent = farm_db.send_command(conn, CommandKind.END_COOLDOWN, NOW, {"condition_id": "0xa"})
        self.assertEqual(farm_db.pending_commands(conn),
                         [farm_db.Command(sent, "end_cooldown", {"condition_id": "0xa"})])


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

    def test_the_status_carries_free_cash_and_the_hurdle_it_set(self):
        conn = self.open()
        farm_db.publish(conn, self.status()._replace(free_cash_usd=6600.0,
                                                     effective_hurdle=2.25), None)
        read = farm_db.read_farmer_status(conn)
        self.assertEqual((read.free_cash_usd, read.effective_hurdle), (6600.0, 2.25))
        farm_db.publish(conn, self.status(), None)
        read = farm_db.read_farmer_status(conn)
        self.assertEqual((read.free_cash_usd, read.effective_hurdle), (None, None))

    def test_prune_drops_events_older_than_thirty_days(self):
        conn = self.open()
        old = farm_db.Event(NOW - timedelta(days=31), farm_db.EventKind.START, "old")
        new = farm_db.Event(NOW - timedelta(days=29), farm_db.EventKind.START, "new")
        farm_db.publish(conn, self.status(), None, [old, new])
        self.assertEqual(farm_db.prune_events(conn, NOW), 1)
        self.assertEqual([r[0] for r in conn.execute("SELECT message FROM farm_events")], ["new"])


class BuysTest(ScratchDb):
    TAPE = {"v": 1, "read_at": NOW.isoformat(), "first_at": NOW.timestamp() - 86400,
            "sweeps": [[NOW.timestamp() - 60, [[0.97, 25.0], [0.965, 300.0]]]]}

    def test_a_saved_tape_reads_back_and_a_newer_one_replaces_it(self):
        conn = self.open()
        farm_db.save_buys(conn, "0xa", NOW, self.TAPE)
        farm_db.save_buys(conn, "0xb", NOW, {**self.TAPE, "sweeps": []})
        newer = {**self.TAPE, "read_at": (NOW + timedelta(hours=1)).isoformat()}
        farm_db.save_buys(conn, "0xa", NOW + timedelta(hours=1), newer)
        self.assertEqual(farm_db.read_buys(conn), {"0xa": newer,
                                                   "0xb": {**self.TAPE, "sweeps": []}})
        self.assertEqual([r[0] for r in conn.execute("SELECT read_at FROM fav_buys ORDER BY 1")],
                         [farm_db.stamp(NOW), farm_db.stamp(NOW + timedelta(hours=1))])

    def test_a_database_with_no_tape_saved_reads_as_empty(self):
        self.assertEqual(farm_db.read_buys(self.open()), {})


class BookSamplesTest(ScratchDb):
    """farm_books.db, the favorite books the farmer records beside farm.db."""

    BIDS = ((0.985, 1000.0), (0.95, 20.0), (0.93, 500.0))
    ASKS = ((0.995, 1000.0), (0.999, 300760.54))
    OURS = (("FAV_BID", 0.985, 507.61), ("LONG_BID", 0.001, 675.82))

    def sample(self, at=NOW, cid="0xa"):
        return farm_books.sample(cid, at, 0.99, self.BIDS, self.ASKS, self.OURS)

    def books(self):
        conn = farm_books.open_books(farm_books.books_path(self.path))
        self.addCleanup(conn.close)
        return conn

    def recorder(self, path=None):
        warn = mock.Mock()
        recorder = farm_books.BookRecorder(path or farm_books.books_path(self.path),
                                           threading.Event(), warn)
        self.addCleanup(recorder.close)
        return recorder, warn

    def test_the_file_sits_beside_farm_db(self):
        self.assertEqual(farm_books.books_path(self.path),
                         os.path.join(os.path.dirname(self.path), "farm_books.db"))

    def test_a_sample_reads_back_with_the_levels_near_the_mid_and_our_orders(self):
        conn = self.books()
        farm_books.record(conn, [self.sample(), self.sample(cid="0xb")])
        [read] = farm_books.read_books(conn, "0xa")
        # The 93c bid is 6c under the 99c mid, past the 5c depth.
        self.assertEqual(read, farm_books.BookSample(
            "0xa", NOW, 0.99, self.BIDS[:2], self.ASKS, self.OURS))
        self.assertEqual(len(farm_books.read_books(conn)), 2)

    def test_prune_drops_samples_older_than_the_kept_days(self):
        conn = self.books()
        kept = farm_books.BOOK_KEPT_DAYS
        farm_books.record(conn, [self.sample(NOW - timedelta(days=kept + 1)),
                                 self.sample(NOW - timedelta(days=kept - 1))])
        self.assertEqual(farm_books.prune(conn, NOW), 1)
        self.assertEqual([s.at for s in farm_books.read_books(conn, "0xa")],
                         [NOW - timedelta(days=kept - 1)])

    def test_the_recorder_writes_what_was_put_and_prunes_once_it_opens(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        conn = self.books()
        farm_books.record(conn, [self.sample(now - timedelta(days=farm_books.BOOK_KEPT_DAYS + 1))])
        recorder, warn = self.recorder()
        recorder.put([self.sample(now)])
        recorder.put([self.sample(now + timedelta(minutes=5))])
        recorder.drain()
        self.assertEqual([s.at for s in farm_books.read_books(conn, "0xa")],
                         [now, now + timedelta(minutes=5)])
        warn.assert_not_called()

    def test_a_write_that_fails_warns_and_drops_the_batch(self):
        recorder, warn = self.recorder(os.path.join(self.path, "no", "such", "farm_books.db"))
        recorder.put([self.sample()])
        recorder.drain()
        warn.assert_called_once()
        self.assertRegex(warn.call_args.args[0], r"^Warning: recording books to .* failed")
        recorder.drain()
        warn.assert_called_once()

    def test_a_full_queue_drops_the_batch_with_a_warning(self):
        recorder, warn = self.recorder()
        for _ in range(farm_books.QUEUE_BATCHES + 1):
            recorder.put([self.sample()])
        warn.assert_called_once()
        recorder.put([])
        warn.assert_called_once()


FR, US = "2027-french-presidential-election", "2028-us-presidential-election"


def topical(cid, rank, at=NOW, event=None, tags=None, book_mid=0.95):
    """A scan row whose market is in event (a slug) and carries tags; None
    leaves the field out, as scans before they were recorded did."""
    market = {"condition_id": cid}
    if event is not None:
        market.update(event_slug=event, event_title=event.replace("-", " ").title())
    if tags is not None:
        market["tags"] = tags
    return farm_db.ScanRow(cid, cid, f"{cid}?", at.isoformat(), rank,
                           json.dumps({"market": market, "book_mid": book_mid}))


class DecisionsTest(ScratchDb):
    def setUp(self):
        super().setUp()
        self.conn = self.open()
        farm_db.store_scan(self.conn, [
            topical("0xa", 3.0, event=FR, tags=["Politics", "French Election"]),
            topical("0xb", 2.0, event=FR, tags=["Politics"]),
            topical("0xc", 1.0, event=US, tags=["Elections"]),
        ], ["0xa", "0xb", "0xc"], NOW)

    def test_reject_logs_its_reason_and_note_with_the_metrics_of_that_moment(self):
        farm_db.reject_market(self.conn, "0xa", "vague rules",
                              farm_db.RejectReason.RESOLUTION_RISK, NOW)
        later = NOW + timedelta(hours=1)
        farm_db.store_scan(self.conn, [topical("0xa", 3.0, later, FR, [], book_mid=0.5)],
                           ["0xa"], later)
        [d] = farm_db.read_decisions(self.conn)
        self.assertEqual((d.condition_id, d.at, d.decision, d.reason, d.note),
                         ("0xa", NOW, "rejected", farm_db.RejectReason.RESOLUTION_RISK,
                          "vague rules"))
        self.assertEqual((d.metrics["book_mid"], d.metrics["market"]["tags"]),
                         (0.95, ["Politics", "French Election"]))
        self.assertEqual(farm_db.find_market(self.conn, "0xa")["status"], "rejected")

    def test_approving_logs_only_the_move_to_approved(self):
        farm_db.approve_market(self.conn, "0xa", 0.97, 25.0, now=NOW)
        farm_db.approve_market(self.conn, "0xa", 0.98, 25.0, now=NOW + timedelta(hours=1))
        farm_db.set_approval(self.conn, "0xa", 0.99, 10.0, None, now=NOW + timedelta(hours=2))
        farm_db.retire_market(self.conn, "0xa", "done")
        farm_db.set_approval(self.conn, "0xa", 0.96, 10.0, None, now=NOW + timedelta(hours=3))
        self.assertEqual([(d.decision, d.p_true, d.at) for d in farm_db.read_decisions(self.conn)],
                         [("approved", 0.96, NOW + timedelta(hours=3)),
                          ("approved", 0.97, NOW)])

    def test_an_approval_that_follows_the_market_logs_no_p_true(self):
        farm_db.approve_market(self.conn, "0xa", 0.97, 25.0, now=NOW)
        farm_db.retire_market(self.conn, "0xa", "done")
        farm_db.set_approval(self.conn, "0xa", None, 10.0, None, now=NOW + timedelta(hours=1))
        self.assertIsNone(farm_db.find_market(self.conn, "0xa")["p_true"])
        farm_db.reject_market(self.conn, "0xa", None, farm_db.RejectReason.OTHER,
                              NOW + timedelta(hours=2))
        self.assertEqual([(d.decision, d.p_true) for d in farm_db.read_decisions(self.conn)],
                         [("rejected", None), ("approved", None), ("approved", 0.97)])

    def test_unreject_moves_only_a_rejected_market_back_to_the_queue(self):
        farm_db.reject_market(self.conn, "0xa", None, farm_db.RejectReason.OTHER, NOW)
        farm_db.approve_market(self.conn, "0xb", 0.97, 25.0, now=NOW)
        self.assertEqual([farm_db.unreject_market(self.conn, cid, NOW + timedelta(hours=1))
                          for cid in ("0xa", "0xb", "0xc")], [True, False, False])
        self.assertEqual([farm_db.find_market(self.conn, c)["status"]
                          for c in ("0xa", "0xb", "0xc")], ["candidate", "approved", "candidate"])
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xa", "0xc"])
        self.assertEqual([(d.condition_id, d.decision) for d in farm_db.read_decisions(self.conn)],
                         [("0xa", "unrejected"), ("0xb", "approved"), ("0xa", "rejected")])

    def test_decisions_filter_by_market_event_or_tag_as_they_were_decided(self):
        for i, cid in enumerate(("0xa", "0xb", "0xc")):
            farm_db.reject_market(self.conn, cid, None, farm_db.RejectReason.UNFAMILIAR_TOPIC,
                                  NOW + timedelta(minutes=i))

        def ids(**filters):
            return [d.condition_id for d in farm_db.read_decisions(self.conn, **filters)]

        self.assertEqual(ids(), ["0xc", "0xb", "0xa"])
        self.assertEqual(ids(event_slug=FR), ["0xb", "0xa"])
        self.assertEqual(ids(tags=["French Election", "Elections"]), ["0xc", "0xa"])
        self.assertEqual(ids(tags=["French"]), [])
        self.assertEqual(ids(condition_ids=["0xc"], event_slug=FR), ["0xc", "0xb", "0xa"])
        self.assertEqual(ids(event_slug=FR, limit=1), ["0xb"])


class MuteTest(ScratchDb):
    """FR holds 0xa, 0xb and the rejected 0xdone; 0xold and 0xbare predate
    recorded events and tags."""

    def setUp(self):
        super().setUp()
        self.conn = self.open()
        rows = [topical("0xa", 6.0, event=FR, tags=["Politics", "French Election"]),
                topical("0xb", 5.0, event=FR, tags=["Politics"]),
                topical("0xc", 4.0, event=US, tags=["French Election"]),
                topical("0xd", 3.0, event=US, tags=["Politics"]),
                topical("0xbare", 2.0),
                farm_db.ScanRow("0xold", "0xold", "?", NOW.isoformat(), 1.0, "{}"),
                topical("0xdone", 0.5, event=FR, tags=["French Election"])]
        farm_db.store_scan(self.conn, rows, [r.condition_id for r in rows], NOW)
        farm_db.reject_market(self.conn, "0xdone", None, farm_db.RejectReason.OTHER, NOW)

    def search(self):
        rows, total = farm_db.search_markets(
            self.conn, statuses=None, mode=None, text=None, min_days=None,
            buffer_days=7, now=NOW, limit=50, offset=0)
        self.assertEqual(len(rows), total)
        return [r.condition_id for r in rows]

    def test_muting_an_event_hides_its_candidates_but_not_a_decided_one_until_unmuted(self):
        farm_db.mute(self.conn, farm_db.MuteKind.EVENT, FR, "French election", NOW)
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xc", "0xd", "0xbare", "0xold"])
        self.assertEqual(self.search(), ["0xc", "0xd", "0xbare", "0xold", "0xdone"])
        self.assertEqual(farm_db.find_market(self.conn, "0xa")["status"], "candidate")
        farm_db.unmute(self.conn, farm_db.MuteKind.EVENT, FR)
        self.assertEqual(farm_db.candidate_queue(self.conn),
                         ["0xa", "0xb", "0xc", "0xd", "0xbare", "0xold"])

    def test_muting_a_tag_hides_the_candidates_carrying_exactly_that_tag(self):
        farm_db.mute(self.conn, farm_db.MuteKind.TAG, "French", "French", NOW)
        self.assertEqual(len(farm_db.candidate_queue(self.conn)), 6)
        farm_db.mute(self.conn, farm_db.MuteKind.TAG, "French Election", "French Election", NOW)
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xb", "0xd", "0xbare", "0xold"])
        self.assertEqual(self.search(), ["0xb", "0xd", "0xbare", "0xold", "0xdone"])

    def test_rows_without_an_event_or_tags_are_never_hidden(self):
        farm_db.mute(self.conn, farm_db.MuteKind.EVENT, FR, None, NOW)
        farm_db.mute(self.conn, farm_db.MuteKind.EVENT, US, None, NOW)
        farm_db.mute(self.conn, farm_db.MuteKind.TAG, "Politics", None, NOW)
        self.assertEqual(farm_db.candidate_queue(self.conn), ["0xbare", "0xold"])

    def test_muting_again_keeps_one_mute_and_each_counts_the_candidates_it_hides(self):
        farm_db.mute(self.conn, farm_db.MuteKind.EVENT, FR, "old label", NOW)
        farm_db.mute(self.conn, farm_db.MuteKind.EVENT, FR, "French election",
                     NOW + timedelta(hours=1))
        farm_db.mute(self.conn, farm_db.MuteKind.TAG, "Politics", "Politics",
                     NOW + timedelta(hours=2))
        self.assertEqual(farm_db.read_mutes(self.conn), [
            farm_db.Mute(farm_db.MuteKind.TAG, "Politics", "Politics", NOW + timedelta(hours=2), 3),
            farm_db.Mute(farm_db.MuteKind.EVENT, FR, "French election", NOW, 2),
        ])


class TriageTest(ScratchDb):
    RESULT = {"verdict": "near_impossible", "why": [], "runs": [],
              "brief": {"searches": [], "state": "", "facts": []}}

    def test_store_and_read_the_latest_row(self):
        conn = self.open()
        self.assertIsNone(farm_db.latest_triage(conn, "0xa"))
        farm_db.store_triage(conn, "0xa", NOW, "sonnet", result=self.RESULT, book_mid=0.94,
                             duration_s=55.0, cost_usd=0.16)
        farm_db.store_triage(conn, "0xa", NOW + timedelta(minutes=1), "sonnet",
                             error="TriageError: timed out", duration_s=240.0)
        farm_db.store_triage(conn, "0xb", NOW + timedelta(minutes=2), "sonnet",
                             result={**self.RESULT, "verdict": "needs_human"})
        latest = farm_db.latest_triage(conn, "0xa")
        self.assertEqual((latest.status, latest.error, latest.verdict, latest.result),
                         ("error", "TriageError: timed out", None, None))
        ok, = farm_db.read_triage(conn, ("0xa",), status="ok")
        self.assertEqual((ok.at, ok.model, ok.verdict, ok.book_mid, ok.result, ok.duration_s,
                          ok.cost_usd),
                         (NOW, "sonnet", "near_impossible", 0.94, self.RESULT, 55.0, 0.16))
        self.assertEqual(farm_db.latest_triage(conn, "0xb").verdict, "needs_human")

    def versioned_db(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(VERSIONED_TRIAGE)
        conn.execute("INSERT INTO triage (condition_id, at, prompt_version, model, status, "
                     "verdict, result) VALUES ('0xa', ?, 5, 'sonnet', 'ok', 'quiet', '{}')",
                     (NOW.isoformat(),))
        conn.commit()
        conn.close()

    def test_opening_drops_a_triage_table_that_carries_prompt_versions(self):
        self.versioned_db()
        conn = self.open()
        self.assertEqual(farm_db.read_triage(conn), [])
        self.assertEqual([r["name"] for r in conn.execute("PRAGMA table_info(triage)")],
                         ["id", "condition_id", "at", "model", "status", "book_mid", "result",
                          "error", "duration_s", "cost_usd", "verdict"])
        indexed = [r["name"] for r in conn.execute("PRAGMA index_info(triage_market)")]
        self.assertEqual(indexed, ["condition_id", "at"])

    def test_opening_again_keeps_the_rows_stored_since_the_drop(self):
        self.versioned_db()
        farm_db.store_triage(self.open(), "0xa", NOW, "sonnet", result=self.RESULT)
        for _ in range(2):
            kept, = farm_db.read_triage(self.open())
            self.assertEqual((kept.condition_id, kept.result), ("0xa", self.RESULT))

    def test_triaged_ids_counts_only_ok_rows(self):
        conn = self.open()
        farm_db.store_triage(conn, "0xa", NOW, "sonnet", result=self.RESULT)
        farm_db.store_triage(conn, "0xb", NOW, "sonnet", error="boom")
        farm_db.store_triage(conn, "0xc", NOW, "sonnet", error="boom")
        farm_db.store_triage(conn, "0xc", NOW + timedelta(minutes=1), "sonnet",
                             result=self.RESULT)
        self.assertEqual(farm_db.triaged_ids(conn), {"0xa", "0xc"})

    def test_a_row_has_a_result_or_an_error(self):
        conn = self.open()
        for kw in ({}, {"result": self.RESULT, "error": "boom"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                farm_db.store_triage(conn, "0xa", NOW, "sonnet", **kw)
        self.assertEqual(farm_db.read_triage(conn), [])


class RewardTotalsTest(ScratchDb):
    def test_sums_every_day_per_market_and_a_refetched_day_counts_once(self):
        conn = self.open()
        self.assertEqual(farm_db.read_reward_totals(conn), {})
        farm_db.store_rewards(conn, "2026-08-01", {"0xa": 2.0, "0xb": 1.0}, NOW)
        farm_db.store_rewards(conn, "2026-10-03", {"0xa": 0.5}, NOW)
        farm_db.store_rewards(conn, "2026-10-04", {"0xa": 0.25}, NOW)
        farm_db.store_rewards(conn, "2026-10-04", {"0xa": 0.75}, NOW)
        self.assertEqual(farm_db.read_reward_totals(conn), {"0xa": 3.25, "0xb": 1.0})


if __name__ == "__main__":
    unittest.main()
