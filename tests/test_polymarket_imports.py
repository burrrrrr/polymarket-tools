import ast
import json
import os
import subprocess
import sys
import unittest
from glob import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "polymarket")
SOURCES = sorted(glob(os.path.join(PACKAGE, "*.py")))
MODULES = [f"polymarket.{os.path.basename(path)[:-3]}" for path in SOURCES
           if not path.endswith("__init__.py")]

KEYLESS = ["polymarket.books", "polymarket.gamma", "polymarket.feed", "polymarket.region",
           "polymarket.hosts", "polymarket.http", "polymarket.parse", "polymarket.orders"]
KEYED = ("polymarket.keys", "polymarket.client", "py_clob_client_v2")
OUTSIDE = {"farm", "analysis", "farm_console", "tools", "data_dir", "dotenv"}
TERMINAL_CALLS = {"print", "input", "getpass", "exit", "quit", "load_dotenv"}


def loaded_by(modules):
    code = (f"import sys, json, {', '.join(modules)}\n"
            "print(json.dumps(sorted(sys.modules)))")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                          check=True)
    return json.loads(done.stdout)


def called_names(path):
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            yield getattr(target, "id", None) or getattr(target, "attr", None), node.lineno


class ImportTests(unittest.TestCase):
    def test_the_read_modules_load_no_keys_no_client_and_no_sdk(self):
        loaded = loaded_by(KEYLESS)
        self.assertTrue(set(KEYLESS) <= set(loaded))
        self.assertEqual([name for name in loaded if name.startswith(KEYED)], [])

    def test_no_module_loads_the_farmer_the_console_the_worker_or_dotenv(self):
        self.assertGreaterEqual(len(MODULES), 9)
        top_level = {name.split(".")[0] for name in loaded_by(MODULES)}
        self.assertEqual(sorted(top_level & OUTSIDE), [])

    def test_the_package_init_is_empty(self):
        self.assertEqual(os.path.getsize(os.path.join(PACKAGE, "__init__.py")), 0)

    def test_no_module_prints_prompts_exits_or_loads_dotenv(self):
        found = [f"{os.path.basename(path)}:{line} {name}"
                 for path in SOURCES for name, line in called_names(path)
                 if name in TERMINAL_CALLS]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
