import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from farm_console import __main__ as entry_point

ROOT = Path(__file__).resolve().parent


def printed_paths(out):
    """The two database paths the start line names, as paths, since the drive
    letter the console prints has the case of the working directory."""
    farm, tools = re.search(r" reading (.+) and (.+)$", out.strip()).groups()
    return Path(farm), Path(tools)


class Fakes:
    def __init__(self):
        self.farm_db = self.tools_db = self.served = None

    def create_app(self, db_path, *, tools_db, **kwargs):
        self.farm_db, self.tools_db = db_path, tools_db
        return "the app"

    def serve(self, app, **kwargs):
        self.served = (app, kwargs["host"], kwargs["port"])


class StartTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def main(self, argv, dotenv=None):
        fakes = Fakes()
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            os.environ.pop("POLYMARKET_DATA_DIR", None)
            stack.enter_context(mock.patch.object(sys, "argv", ["farm_console", *argv]))
            stack.enter_context(mock.patch.object(entry_point, "create_app", fakes.create_app))
            stack.enter_context(mock.patch("uvicorn.run", fakes.serve))
            self.load_dotenv = stack.enter_context(mock.patch.object(
                entry_point, "load_dotenv", side_effect=lambda path: os.environ.update(
                    dotenv or {})))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            code = entry_point.main()
        return code, out.getvalue(), fakes

    def test_with_no_flag_and_no_variable_the_console_serves_the_repos_databases(self):
        _, out, fakes = self.main(["--port", "8799"])
        self.assertEqual((Path(fakes.farm_db), Path(fakes.tools_db)),
                         (ROOT / "farm.db", ROOT / "tools.db"))
        self.assertEqual(fakes.served, ("the app", "127.0.0.1", 8799))
        self.load_dotenv.assert_called_once_with(ROOT / ".env")
        self.assertEqual(printed_paths(out), (ROOT / "farm.db", ROOT / "tools.db"))

    def test_the_data_directory_that_env_names_holds_both_databases(self):
        _, out, fakes = self.main([], dotenv={"POLYMARKET_DATA_DIR": str(self.dir)})
        self.assertEqual((Path(fakes.farm_db), Path(fakes.tools_db)),
                         (self.dir / "farm.db", self.dir / "tools.db"))
        self.assertEqual(printed_paths(out), (self.dir / "farm.db", self.dir / "tools.db"))

    def test_each_flag_wins_over_the_data_directory_for_its_own_database(self):
        dotenv = {"POLYMARKET_DATA_DIR": str(self.dir)}
        _, _, fakes = self.main(["--db", "scratch.db"], dotenv)
        self.assertEqual((fakes.farm_db, Path(fakes.tools_db)),
                         ("scratch.db", self.dir / "tools.db"))
        _, _, fakes = self.main(["--tools-db", "other.db"], dotenv)
        self.assertEqual((Path(fakes.farm_db), fakes.tools_db),
                         (self.dir / "farm.db", "other.db"))

    def test_a_data_directory_that_does_not_exist_ends_the_start_before_anything_is_served(self):
        missing = self.dir / "missing"
        code, out, fakes = self.main([], dotenv={"POLYMARKET_DATA_DIR": str(missing)})
        self.assertEqual(code, 1)
        self.assertIsNone(fakes.served)
        self.assertIn(f"POLYMARKET_DATA_DIR is not a directory: {missing}", out)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
