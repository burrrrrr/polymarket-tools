import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import data_dir

ROOT = Path(__file__).resolve().parent.parent


class PathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patched = mock.patch.dict(os.environ)
        patched.start()
        self.addCleanup(patched.stop)
        os.environ.pop(data_dir.VARIABLE, None)

    def paths(self):
        return Path(data_dir.farm_db_path()), Path(data_dir.tools_db_path())

    def test_the_variable_is_polymarket_data_dir(self):
        self.assertEqual(data_dir.VARIABLE, "POLYMARKET_DATA_DIR")

    def test_without_the_variable_both_databases_are_in_the_repo_directory(self):
        self.assertEqual(self.paths(), (ROOT / "farm.db", ROOT / "tools.db"))

    def test_a_variable_left_empty_is_no_variable(self):
        os.environ[data_dir.VARIABLE] = ""
        self.assertEqual(self.paths(), (ROOT / "farm.db", ROOT / "tools.db"))

    def test_the_variable_names_the_directory_of_both_when_a_path_is_asked_for(self):
        os.environ[data_dir.VARIABLE] = str(self.dir)
        self.assertEqual(self.paths(), (self.dir / "farm.db", self.dir / "tools.db"))

    def test_a_relative_directory_is_taken_from_the_repo_and_not_from_where_it_is_run(self):
        os.environ[data_dir.VARIABLE] = "farm_console"
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.dir)
        self.assertEqual(self.paths(), (ROOT / "farm_console" / "farm.db",
                                        ROOT / "farm_console" / "tools.db"))

    def test_the_old_snapshot_files_are_in_snapshots_in_the_same_directory(self):
        self.assertEqual(Path(data_dir.snapshots_path()), ROOT / "snapshots")
        os.environ[data_dir.VARIABLE] = str(self.dir)
        self.assertEqual(Path(data_dir.snapshots_path()), self.dir / "snapshots")

    def test_a_path_given_outright_wins_and_comes_back_as_given(self):
        os.environ[data_dir.VARIABLE] = str(self.dir / "missing")
        self.assertEqual(data_dir.farm_db_path("scratch.db"), "scratch.db")
        self.assertEqual(data_dir.tools_db_path("other.db"), "other.db")
        self.assertEqual(data_dir.snapshots_path("old"), "old")

    def test_a_directory_that_does_not_exist_is_refused_by_name_and_not_made(self):
        missing = self.dir / "missing"
        os.environ[data_dir.VARIABLE] = str(missing)
        for resolve in (data_dir.farm_db_path, data_dir.tools_db_path, data_dir.snapshots_path):
            with self.subTest(resolve=resolve.__name__):
                with self.assertRaises(data_dir.NoDataDir) as raised:
                    resolve()
                self.assertIn("POLYMARKET_DATA_DIR", str(raised.exception))
                self.assertIn(str(missing), str(raised.exception))
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
