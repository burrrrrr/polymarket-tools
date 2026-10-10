"""Tests for what the tools package is allowed to load and call.

The console, which holds no key, reads tools.db, so tools.db must not bring in
the key loader, the client or the SDK. In tools/ only the entry point reads a
key or talks to a terminal.
"""

import ast
import os
import unittest
from glob import glob

from test_polymarket_imports import KEYED, ROOT, TERMINAL_CALLS, called_names, loaded_by

KEYED_BY_THIS_BUILD = ("polymarket.keys", "polymarket.client", "tools.worker", "tools.snapshot",
                       "tools.__main__")
LIBRARY = sorted(path for path in glob(os.path.join(ROOT, "tools", "*.py"))
                 if not path.endswith("__main__.py"))


class ImportTests(unittest.TestCase):
    def test_the_rows_the_console_reads_load_no_key_loader_no_client_and_no_sdk(self):
        loaded = loaded_by(["tools.db"])
        self.assertEqual([name for name in loaded
                          if name.startswith(KEYED) or name in KEYED_BY_THIS_BUILD], [])

    def test_the_worker_is_handed_its_client_and_loads_no_key_loader(self):
        loaded = loaded_by(["tools.worker", "tools.snapshot"])
        self.assertEqual([name for name in loaded if name.startswith(KEYED)], [])

    def test_only_the_entry_point_prints_prompts_reads_standard_input_or_exits(self):
        self.assertGreaterEqual(len(LIBRARY), 4)
        found = [f"{os.path.basename(path)}:{line} {name}"
                 for path in LIBRARY for name, line in called_names(path)
                 if name in TERMINAL_CALLS]
        for path in LIBRARY:
            with open(path, encoding="utf-8") as source:
                found += [f"{os.path.basename(path)}:{node.lineno} stdin"
                          for node in ast.walk(ast.parse(source.read(), path))
                          if isinstance(node, ast.Attribute) and node.attr == "stdin"]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
