"""Tests for python -m tools: the worker's entry point. The region check, the key
loader, the CLOB connection and the worker loop are fakes, so no key is read
and nothing is sent."""

import contextlib
import io
import json
import os
import signal
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from polymarket.client import ConfigError
from polymarket.keys import KeyUnavailable
from polymarket.region import Verdict
from test_polymarket_orders import entry
from tools import __main__ as entry_point
from tools import db

KEY = "0x" + "ab" * 32
ALLOWED = Verdict(True, "CH: new orders permitted")
BLOCKED = Verdict(False, "US: the API rejects new orders from here")


class Fakes:
    """What main reaches for, replaced: each call is noted in `calls`."""

    def __init__(self, verdict=ALLOWED, key=KEY, run_raises=None, connect_raises=None):
        self.calls = []
        self.verdict, self.key = verdict, key
        self.run_raises, self.connect_raises = run_raises, connect_raises
        self.http = SimpleNamespace(close=lambda: self.calls.append("close"))
        self.clients = []
        self.run_args = self.sniper_client = self.stop = None
        self.handlers = {}

    def persistent_client(self):
        return self.http

    def check(self, http):
        self.calls.append("region")
        return self.verdict

    def load_private_key(self, ask_password):
        self.calls.append("key")
        if isinstance(self.key, Exception):
            raise self.key
        self.asked = ask_password()
        return self.key

    def use_for_sdk(self, http):
        self.calls.append("sdk")

    def connect(self, private_key):
        self.calls.append("connect")
        self.connected_with = private_key
        if self.connect_raises:
            raise self.connect_raises
        self.clients.append(object())
        return self.clients[-1]

    def run(self, db_path, client, http, *, stop, sniper_client=None):
        self.calls.append("run")
        self.run_args, self.sniper_client, self.stop = (db_path, client, http), sniper_client, stop
        if self.run_raises:
            raise self.run_raises

    def handle(self, signum, handler):
        self.handlers[signum] = handler

    def set_switch_interval(self, seconds):
        self.calls.append(f"switch {seconds:g}")


class Main(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.db_path = os.path.join(tmp.name, "tools.db")

    def main(self, argv, fakes=None, stdin=""):
        fakes = fakes or Fakes()
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target, fake in (
                    ("polymarket.http.persistent_client", fakes.persistent_client),
                    ("polymarket.http.use_for_sdk", fakes.use_for_sdk),
                    ("polymarket.region.check", fakes.check),
                    ("polymarket.keys.load_private_key", fakes.load_private_key),
                    ("polymarket.client.connect", fakes.connect),
                    ("tools.worker.run", fakes.run),
                    ("signal.signal", fakes.handle),
                    ("sys.setswitchinterval", fakes.set_switch_interval),
                    ("getpass.getpass", lambda prompt: "master password"),
                    ("sys.stdin", io.StringIO(stdin))):
                stack.enter_context(mock.patch(target, fake))
            self.load_dotenv = stack.enter_context(mock.patch.object(entry_point, "load_dotenv"))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            code = entry_point.main(argv)
        return code, out.getvalue(), fakes


class WorkerTests(Main):
    def test_a_keyed_worker_checks_the_region_reads_the_key_and_connects_before_it_runs(self):
        code, out, fakes = self.main(["--db", self.db_path])
        self.assertEqual(code, 0)
        self.assertEqual(fakes.calls, ["switch 0.0005", "region", "key", "sdk", "connect",
                                       "connect", "run", "close"])
        jobs_client, sniper_client = fakes.clients
        self.assertEqual(fakes.run_args, (self.db_path, jobs_client, fakes.http))
        self.assertIs(fakes.sniper_client, sniper_client, "the sniper shares the jobs' client")
        self.assertEqual((fakes.connected_with, fakes.asked), (KEY, "master password"))
        self.load_dotenv.assert_called_once_with(entry_point.REPO / ".env")
        self.assertNotIn(KEY, out)

    def test_a_blocked_place_is_refused_before_the_key_is_read(self):
        code, out, fakes = self.main(["--db", self.db_path], Fakes(verdict=BLOCKED))
        self.assertEqual(code, 1)
        self.assertEqual(fakes.calls, ["switch 0.0005", "region", "close"])
        self.assertIn("US: the API rejects new orders from here", out)
        self.assertFalse(os.path.exists(self.db_path))

    def test_without_a_key_nothing_is_checked_read_or_connected(self):
        code, _, fakes = self.main(["--db", self.db_path, "--no-key"])
        self.assertEqual(code, 0)
        self.assertEqual(fakes.calls, ["switch 0.0005", "run", "close"])
        self.assertEqual(fakes.run_args, (self.db_path, None, fakes.http))
        self.assertIsNone(fakes.sniper_client)

    def test_the_key_can_arrive_as_one_line_of_standard_input(self):
        with mock.patch.dict(os.environ):
            code, out, fakes = self.main(["--db", self.db_path, "--key-stdin"],
                                         stdin=f"{KEY}\nnot read\n")
            self.assertNotIn(KEY, os.environ.values())
        self.assertEqual(code, 0)
        self.assertEqual(fakes.calls, ["switch 0.0005", "region", "sdk", "connect", "connect",
                                       "run", "close"])
        self.assertEqual(fakes.connected_with, KEY)
        self.assertNotIn(KEY, out)

    def test_an_empty_standard_input_is_no_key(self):
        code, out, fakes = self.main(["--db", self.db_path, "--key-stdin"], stdin="\n")
        self.assertEqual(code, 1)
        self.assertEqual(fakes.calls, ["switch 0.0005", "region", "close"])
        self.assertIn("standard input", out)

    def test_the_two_key_flags_exclude_each_other(self):
        with self.assertRaises(SystemExit):
            self.main(["--key-stdin", "--no-key"])

    def test_a_key_that_cannot_be_loaded_ends_the_run_with_its_reason(self):
        fakes = Fakes(key=KeyUnavailable("the Bitwarden CLI is not logged in: run `bw login`"))
        code, out, fakes = self.main(["--db", self.db_path], fakes)
        self.assertEqual(code, 1)
        self.assertEqual(fakes.calls, ["switch 0.0005", "region", "key", "close"])
        self.assertIn("run `bw login`", out)

    def test_an_account_the_environment_does_not_describe_ends_the_run(self):
        fakes = Fakes(connect_raises=ConfigError("POLYMARKET_FUNDER_ADDRESS is not set"))
        code, out, fakes = self.main(["--db", self.db_path], fakes)
        self.assertEqual(code, 1)
        self.assertNotIn("run", fakes.calls)
        self.assertIn("POLYMARKET_FUNDER_ADDRESS is not set", out)

    def test_a_second_worker_ends_with_the_reason_it_was_refused(self):
        fakes = Fakes(run_raises=db.WorkerRunning("a worker is already running (pid 7)"))
        code, out, _ = self.main(["--db", self.db_path, "--no-key"], fakes)
        self.assertEqual(code, 1)
        self.assertIn("a worker is already running (pid 7)", out)

    def test_an_interrupt_or_a_terminate_signal_sets_the_stop_event(self):
        for name in ("SIGINT", "SIGTERM"):
            with self.subTest(signal=name):
                _, _, fakes = self.main(["--db", self.db_path, "--no-key"])
                self.assertFalse(fakes.stop.is_set())
                fakes.handlers[getattr(signal, name)](getattr(signal, name), None)
                self.assertTrue(fakes.stop.is_set())


class ImportTests(Main):
    def setUp(self):
        super().setUp()
        self.files = os.path.join(self.dir, "snapshots")
        os.mkdir(self.files)
        with open(os.path.join(self.files, "orders_snapshot_20260718_221655.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"metadata": {"timestamp": "2026-07-18T22:16:55.620915"},
                       "orders": [entry(id="0xa"), entry(id="0xb")]}, f)

    def stored(self):
        conn = db.open_db(self.db_path)
        self.addCleanup(conn.close)
        return [(s.source, s.orders) for s in db.read_snapshots(conn)]

    def test_the_import_needs_no_key_and_says_what_it_stored(self):
        for argv in (["import-snapshots", self.files, "--db", self.db_path],
                     ["--db", self.db_path, "import-snapshots", self.files]):
            with self.subTest(argv=argv):
                code, out, fakes = self.main(argv)
                self.assertEqual((code, fakes.calls), (0, []))
                self.assertEqual(self.stored(), [("orders_snapshot_20260718_221655.json", 2)])
        self.assertIn("0 snapshot files with 0 orders", out)
        self.assertIn("1 already stored", out)

    def test_a_directory_that_is_missing_ends_the_import_with_the_reason(self):
        code, out, _ = self.main(["import-snapshots", os.path.join(self.dir, "nowhere"),
                                  "--db", self.db_path])
        self.assertEqual(code, 1)
        self.assertIn("nowhere", out)

    def test_the_defaults_are_the_repos_tools_db_and_its_snapshots_directory(self):
        args = entry_point.build_parser().parse_args(["import-snapshots"])
        self.assertEqual((args.db, args.directory),
                         (str(entry_point.REPO / "tools.db"), str(entry_point.REPO / "snapshots")))
        self.assertEqual(entry_point.build_parser().parse_args([]).db,
                         str(entry_point.REPO / "tools.db"))


if __name__ == "__main__":
    unittest.main()
