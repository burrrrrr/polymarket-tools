"""Tests for what the polymarket package is allowed to load and to call.

A keyless process, the console, imports the read modules, so they must not
bring in the key loader, the client or the SDK. No module of the package may
reach back into the farmer, load .env, or talk to a terminal.
"""

import ast
import json
import os
import subprocess
import sys
import unittest
from glob import glob

ROOT = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.join(ROOT, "polymarket")
SOURCES = sorted(glob(os.path.join(PACKAGE, "*.py")))
MODULES = [f"polymarket.{os.path.basename(path)[:-3]}" for path in SOURCES
           if not path.endswith("__init__.py")]

KEYLESS = ["polymarket.books", "polymarket.gamma", "polymarket.feed", "polymarket.region",
           "polymarket.hosts", "polymarket.http", "polymarket.parse"]
KEYED = ("polymarket.keys", "polymarket.client", "py_clob_client_v2")
OUTSIDE_PREFIXES = ("reward_", "farm_")
OUTSIDE = {"tools", "config", "key_loader", "bitwarden_loader", "dotenv", "tick_watch", "restore",
           "snapshot"}
TERMINAL_CALLS = {"print", "input", "getpass", "exit", "quit", "load_dotenv"}


def loaded_by(modules):
    """The names in sys.modules after a fresh interpreter imports these modules."""
    code = (f"import sys, json, {', '.join(modules)}\n"
            "print(json.dumps(sorted(sys.modules)))")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                          check=True)
    return json.loads(done.stdout)


def called_names(path):
    """The name of every function a source file calls: f for f(...) and for x.f(...)."""
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            yield getattr(target, "id", None) or getattr(target, "attr", None), node.lineno


class ImportTests(unittest.TestCase):
    def test_the_read_modules_load_no_key_loader_no_client_and_no_sdk(self):
        loaded = loaded_by(KEYLESS)
        self.assertTrue(set(KEYLESS) <= set(loaded))
        self.assertEqual([name for name in loaded if name.startswith(KEYED)], [])

    def test_no_module_loads_the_farmer_the_console_the_old_scripts_or_dotenv(self):
        self.assertGreaterEqual(len(MODULES), 9)
        top_level = {name.split(".")[0] for name in loaded_by(MODULES)}
        self.assertEqual(sorted(name for name in top_level
                                if name in OUTSIDE or name.startswith(OUTSIDE_PREFIXES)), [])

    def test_the_package_init_is_empty(self):
        self.assertEqual(os.path.getsize(os.path.join(PACKAGE, "__init__.py")), 0)

    def test_no_module_prints_prompts_exits_or_loads_dotenv(self):
        found = [f"{os.path.basename(path)}:{line} {name}"
                 for path in SOURCES for name, line in called_names(path)
                 if name in TERMINAL_CALLS]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
