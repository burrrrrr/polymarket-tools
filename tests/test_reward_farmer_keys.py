import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from farm import reward_farmer
from polymarket.client import ConfigError
from polymarket.keys import KeyUnavailable
from polymarket.region import Verdict, verdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "0x" + "ab" * 32
ALLOWED = Verdict(True, "CH: new orders permitted")
BLOCKED = Verdict(False, "US: the API rejects new orders from here")


class Fakes:
    def __init__(self, verdict=ALLOWED, key=KEY, connect_raises=None):
        self.calls = []
        self.verdict, self.key, self.connect_raises = verdict, key, connect_raises
        self.client = object()
        self.asked = self.connected_with = self.ran_with = self.ran_on = None

    def check(self, http):
        self.calls.append("region")
        return self.verdict

    def load_private_key(self, ask_password):
        self.calls.append("key")
        if isinstance(self.key, Exception):
            raise self.key
        self.asked = ask_password()
        return self.key

    def connect(self, private_key):
        self.calls.append("connect")
        self.connected_with = private_key
        if self.connect_raises:
            raise self.connect_raises
        return self.client

    def getpass(self, prompt):
        self.calls.append("getpass")
        return "master password"

    def run(self, args, client=None):
        self.calls.append("run")
        self.ran_with = (args.live, client)
        self.ran_on = args.db


class Start(unittest.TestCase):
    def main(self, argv, fakes=None, stdin="", dotenv=None):
        fakes = fakes or Fakes()
        out = io.StringIO()
        self.stdin = io.StringIO(stdin)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            os.environ.pop("POLYMARKET_DATA_DIR", None)
            for target, fake in (
                    ("polymarket.region.check", fakes.check),
                    ("polymarket.keys.load_private_key", fakes.load_private_key),
                    ("polymarket.client.connect", fakes.connect),
                    ("getpass.getpass", fakes.getpass),
                    ("sys.stdin", self.stdin)):
                stack.enter_context(mock.patch(target, fake))
            stack.enter_context(mock.patch.object(reward_farmer, "run", fakes.run))
            stack.enter_context(mock.patch.object(reward_farmer, "_make_output_safe"))
            self.load_dotenv = stack.enter_context(mock.patch.object(
                reward_farmer, "load_dotenv", side_effect=lambda path: os.environ.update(
                    dotenv or {})))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            code = reward_farmer.main(argv)
        return code, out.getvalue(), fakes


class StartTests(Start):
    def test_a_live_start_checks_the_region_reads_the_key_and_connects_before_the_loop(self):
        code, out, fakes = self.main(["--live"])
        self.assertEqual(code, 0)
        self.assertEqual(fakes.calls, ["region", "key", "getpass", "connect", "run"])
        self.assertIn("Region: CH: new orders permitted.", out)
        self.assertEqual((fakes.connected_with, fakes.asked), (KEY, "master password"))
        self.assertEqual(fakes.ran_with, (True, fakes.client))
        self.load_dotenv.assert_called_once_with(Path(ROOT) / ".env")
        self.assertNotIn(KEY, out)

    def test_a_blocked_place_is_refused_before_the_key_is_read(self):
        for flags in (["--live"], ["--live", "--key-stdin"]):
            with self.subTest(flags=flags):
                code, out, fakes = self.main(flags, Fakes(verdict=BLOCKED), stdin=f"{KEY}\n")
                self.assertEqual(code, 1)
                self.assertEqual(fakes.calls, ["region"])
                self.assertIn("US: the API rejects new orders from here", out)
                self.assertIn("without --live", out)
                self.assertEqual(self.stdin.read(), f"{KEY}\n")

    def test_a_region_that_cannot_be_read_does_not_stop_a_live_start(self):
        code, out, fakes = self.main(["--live"], Fakes(verdict=verdict(None, None)))
        self.assertEqual(code, 0)
        self.assertEqual(fakes.calls, ["region", "key", "getpass", "connect", "run"])
        self.assertIn("Region: region unreadable, not checked.", out)

    def test_a_dry_run_checks_no_region_and_asks_for_nothing(self):
        for flags in ([], ["--key-stdin"]):
            with self.subTest(flags=flags):
                code, _, fakes = self.main(flags, stdin=f"{KEY}\n")
                self.assertEqual(code, 0)
                self.assertEqual(fakes.calls, ["run"])
                self.assertEqual(fakes.ran_with, (False, None))
                self.assertEqual(self.stdin.read(), f"{KEY}\n")

    def test_the_key_can_arrive_as_one_line_of_standard_input(self):
        with mock.patch.dict(os.environ):
            code, out, fakes = self.main(["--live", "--key-stdin"], stdin=f"{KEY}\nnot read\n")
            self.assertNotIn(KEY, os.environ.values())
        self.assertEqual(code, 0)
        self.assertEqual(fakes.calls, ["region", "connect", "run"])
        self.assertEqual(fakes.connected_with, KEY)
        self.assertEqual(self.stdin.read(), "not read\n")
        self.assertNotIn(KEY, out)

    def test_an_empty_standard_input_is_no_key(self):
        code, out, fakes = self.main(["--live", "--key-stdin"], stdin="\n")
        self.assertEqual(code, 1)
        self.assertEqual(fakes.calls, ["region"])
        self.assertIn("standard input", out)

    def test_a_key_that_cannot_be_loaded_ends_the_start_with_its_reason(self):
        fakes = Fakes(key=KeyUnavailable("the Bitwarden CLI is not logged in: run `bw login`"))
        code, out, fakes = self.main(["--live"], fakes)
        self.assertEqual(code, 1)
        self.assertEqual(fakes.calls, ["region", "key"])
        self.assertIn("run `bw login`", out)
        self.assertNotIn("Traceback", out)

    def test_an_account_the_environment_does_not_describe_ends_the_start(self):
        fakes = Fakes(connect_raises=ConfigError("POLYMARKET_FUNDER_ADDRESS is not set"))
        code, out, fakes = self.main(["--live"], fakes)
        self.assertEqual(code, 1)
        self.assertNotIn("run", fakes.calls)
        self.assertIn("POLYMARKET_FUNDER_ADDRESS is not set", out)
        self.assertNotIn("Traceback", out)


class DataDirTests(Start):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_with_no_flag_and_no_variable_the_farmer_opens_the_repos_farm_db(self):
        for flags in ([], ["--live"]):
            with self.subTest(flags=flags):
                _, _, fakes = self.main(flags)
                self.assertEqual(Path(fakes.ran_on), Path(ROOT) / "farm.db")

    def test_the_data_directory_that_env_names_holds_the_farm_db(self):
        code, _, fakes = self.main([], dotenv={"POLYMARKET_DATA_DIR": str(self.dir)})
        self.assertEqual(code, 0)
        self.assertEqual(Path(fakes.ran_on), self.dir / "farm.db")

    def test_a_db_flag_wins_over_the_data_directory(self):
        _, _, fakes = self.main(["--db", "scratch.db"],
                                dotenv={"POLYMARKET_DATA_DIR": str(self.dir / "missing")})
        self.assertEqual(fakes.ran_on, "scratch.db")

    def test_a_data_directory_that_does_not_exist_ends_the_start_before_anything_is_read(self):
        missing = self.dir / "missing"
        code, out, fakes = self.main(["--live"], dotenv={"POLYMARKET_DATA_DIR": str(missing)})
        self.assertEqual(code, 1)
        self.assertEqual(fakes.calls, [])
        self.assertIn(f"POLYMARKET_DATA_DIR is not a directory: {missing}", out)
        self.assertFalse(missing.exists())


class ImportTests(unittest.TestCase):
    def test_importing_the_farmer_loads_no_env_file_no_keys_and_no_client(self):
        code = ("import dotenv, sys\n"
                "loads = []\n"
                "dotenv.load_dotenv = lambda *args, **kwargs: loads.append(args)\n"
                "from farm import reward_farmer\n"
                "print(len(loads), sorted(name for name in sys.modules if name in "
                "('polymarket.keys', 'polymarket.client')))")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                              text=True, check=True)
        self.assertEqual(done.stdout.strip(), "0 []")


if __name__ == "__main__":
    unittest.main()
