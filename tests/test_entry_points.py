import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOTENV_HOST = "https://from-dotenv.invalid"
FAKE_DOTENV = (
    "import os, sys, dotenv\n"
    "dotenv.load_dotenv = lambda *args, **kwargs: "
    f"os.environ.update(POLYMARKET_HOST={DOTENV_HOST!r})\n")
STARTS = {
    "farmer": ("from farm import reward_farmer\n"
               "reward_farmer.run = lambda *args, **kwargs: None\n"
               "reward_farmer.main(['--db', os.path.join(sys.argv[1], 'farm.db')])\n"),
    "console": ("import uvicorn\n"
                "from farm_console import __main__ as console\n"
                "uvicorn.run = console.create_app = lambda *args, **kwargs: None\n"
                "sys.argv[1:] = ['--db', os.path.join(sys.argv[1], 'farm.db')]\n"
                "console.main()\n"),
    "worker": ("from tools import __main__ as worker\n"
               "worker.main(['import-snapshots', sys.argv[1], "
               "'--db', os.path.join(sys.argv[1], 'tools.db')])\n"),
    "screener": ("from farm import reward_screener\n"
                 "reward_screener.cmd_list = lambda args: None\n"
                 "scratch = sys.argv[1]\n"
                 "sys.argv[1:] = ['list', '--db', os.path.join(scratch, 'farm.db')]\n"
                 "reward_screener.main()\n"),
    "triage_eval": ("from analysis import triage_eval\n"
                    "triage_eval.main(['hindsight', '--db', os.path.join(sys.argv[1], 'farm.db'), "
                    "'--out', os.path.join(sys.argv[1], 'out.csv')])\n"),
}
REPORT = ("from polymarket import hosts\n"
          "print(hosts.CLOB_HOST, os.environ.get('POLYMARKET_HOST'))\n")


class DotenvTests(unittest.TestCase):
    def test_a_host_that_only_env_names_moves_no_entry_point(self):
        environment = {name: value for name, value in os.environ.items()
                       if name != "POLYMARKET_HOST"}
        for name, start in STARTS.items():
            with self.subTest(entry_point=name), tempfile.TemporaryDirectory() as scratch:
                done = subprocess.run(
                    [sys.executable, "-c", FAKE_DOTENV + start + REPORT, scratch], cwd=ROOT,
                    env=environment, capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.splitlines()[-1],
                                 f"https://clob.polymarket.com {DOTENV_HOST}")


if __name__ == "__main__":
    unittest.main()
