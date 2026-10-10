import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import scoring_probe
from polymarket.client import ConfigError
from polymarket.keys import KeyUnavailable

ROOT = os.path.dirname(os.path.abspath(__file__))
KEY = "0x" + "ab" * 32


class Fakes:
    def __init__(self, key=KEY, connect_raises=None):
        self.calls = []
        self.key, self.connect_raises = key, connect_raises
        self.client = mock.Mock()
        self.asked = self.connected_with = None

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


class StartTests(unittest.TestCase):
    def main(self, argv, fakes=None):
        fakes = fakes or Fakes()
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target, fake in (
                    ("polymarket.keys.load_private_key", fakes.load_private_key),
                    ("polymarket.client.connect", fakes.connect),
                    ("getpass.getpass", lambda prompt: "master password")):
                stack.enter_context(mock.patch(target, fake))
            self.load_dotenv = stack.enter_context(mock.patch.object(scoring_probe, "load_dotenv"))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            code = scoring_probe.main(argv)
        return code, out.getvalue(), fakes

    def test_the_probe_reads_the_key_and_connects_before_it_polls(self):
        code, out, fakes = self.main(["--minutes", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(fakes.calls, ["key", "connect"])
        self.assertEqual((fakes.connected_with, fakes.asked), (KEY, "master password"))
        self.load_dotenv.assert_called_once_with(Path(ROOT) / ".env")
        self.assertIn("Orders placed during the run: 0", out)
        self.assertNotIn(KEY, out)

    def test_a_key_that_cannot_be_loaded_ends_the_probe_with_its_reason(self):
        fakes = Fakes(key=KeyUnavailable("the Bitwarden CLI is not logged in: run `bw login`"))
        code, out, fakes = self.main(["--minutes", "0"], fakes)
        self.assertEqual(code, 1)
        self.assertEqual(fakes.calls, ["key"])
        self.assertIn("run `bw login`", out)
        self.assertNotIn("Traceback", out)

    def test_an_account_the_environment_does_not_describe_ends_the_probe(self):
        fakes = Fakes(connect_raises=ConfigError("POLYMARKET_FUNDER_ADDRESS is not set"))
        code, out, fakes = self.main(["--minutes", "0"], fakes)
        self.assertEqual(code, 1)
        self.assertIn("POLYMARKET_FUNDER_ADDRESS is not set", out)
        self.assertNotIn("Orders placed during the run", out)


class ImportTests(unittest.TestCase):
    def test_importing_the_probe_loads_no_env_file(self):
        code = ("import dotenv\n"
                "loads = []\n"
                "dotenv.load_dotenv = lambda *args, **kwargs: loads.append(args)\n"
                "import scoring_probe\n"
                "print(len(loads))")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                              text=True, check=True)
        self.assertEqual(done.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
