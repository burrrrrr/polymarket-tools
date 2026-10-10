import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from farm import reward_screener

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMANDS = {"scan": [], "list": [], "approve": ["some-market", "--p-true", "0.97"],
            "reject": ["some-market"]}


class Start(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def main(self, argv, dotenv=None):
        """(exit code, what it printed, [(the command that ran, the database it was given)])."""
        ran, out = [], io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            os.environ.pop("POLYMARKET_DATA_DIR", None)
            for command in COMMANDS:
                stack.enter_context(mock.patch.object(
                    reward_screener, f"cmd_{command}",
                    lambda args, command=command: ran.append((command, args.db))))
            stack.enter_context(mock.patch.object(reward_screener, "_make_output_safe"))
            self.load_dotenv = stack.enter_context(mock.patch.object(
                reward_screener, "load_dotenv", side_effect=lambda path: os.environ.update(
                    dotenv or {})))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = reward_screener.main(argv)
        return code, out.getvalue(), ran


class DataDirTests(Start):
    def test_with_no_flag_and_no_variable_every_command_opens_the_repos_farm_db(self):
        for command, rest in COMMANDS.items():
            with self.subTest(command=command):
                code, _, ran = self.main([command, *rest])
                self.assertEqual(code, 0)
                self.assertEqual([(name, Path(db)) for name, db in ran],
                                 [(command, Path(ROOT) / "farm.db")])

    def test_the_data_directory_that_env_names_holds_the_farm_db(self):
        for command, rest in COMMANDS.items():
            with self.subTest(command=command):
                _, _, ran = self.main([command, *rest],
                                      dotenv={"POLYMARKET_DATA_DIR": str(self.dir)})
                self.assertEqual(ran, [(command, str(self.dir / "farm.db"))])
                self.load_dotenv.assert_called_once_with(Path(ROOT) / ".env")

    def test_a_db_flag_wins_over_the_data_directory(self):
        _, _, ran = self.main(["list", "--db", "scratch.db"],
                              dotenv={"POLYMARKET_DATA_DIR": str(self.dir / "missing")})
        self.assertEqual(ran, [("list", "scratch.db")])

    def test_a_data_directory_that_does_not_exist_ends_the_run_before_any_command(self):
        missing = self.dir / "missing"
        code, out, ran = self.main(["scan"], dotenv={"POLYMARKET_DATA_DIR": str(missing)})
        self.assertEqual((code, ran), (1, []))
        self.assertIn(f"POLYMARKET_DATA_DIR is not a directory: {missing}", out)
        self.assertFalse(missing.exists())

    def test_flags_with_no_command_are_a_scan(self):
        for argv in ([], ["--top", "5"]):
            with self.subTest(argv=argv):
                _, _, ran = self.main(argv, dotenv={"POLYMARKET_DATA_DIR": str(self.dir)})
                self.assertEqual(ran, [("scan", str(self.dir / "farm.db"))])

    def test_the_help_says_where_the_default_is_without_naming_this_machine(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            reward_screener.build_parser().parse_args(["list", "--help"])
        self.assertIn("POLYMARKET_DATA_DIR", out.getvalue())
        self.assertNotIn(ROOT, out.getvalue())


class ImportTests(unittest.TestCase):
    def test_importing_the_screener_loads_no_env_file(self):
        code = ("import dotenv\n"
                "loads = []\n"
                "dotenv.load_dotenv = lambda *args, **kwargs: loads.append(args)\n"
                "from farm import reward_screener\n"
                "print(len(loads))")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                              text=True, check=True)
        self.assertEqual(done.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
