"""Run the test suite, one process a module.

    python run_tests.py                  every test_*.py beside this file
    python run_tests.py test_farm_db     the modules named

The modules share nothing: each test works in its own temporary directory.
Side by side the suite takes about as long as its slowest module. A module
that fails prints its unittest output, and the exit status is then 1.
"""

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from glob import glob

ROOT = os.path.dirname(os.path.abspath(__file__))


def run(module):
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-m", "unittest", module], cwd=ROOT,
                          capture_output=True, text=True)
    return module, done, time.monotonic() - started


def main(modules):
    modules = modules or sorted(
        os.path.basename(path)[:-3] for path in glob(os.path.join(ROOT, "test_*.py")))
    started = time.monotonic()
    with ThreadPoolExecutor(min(len(modules), os.cpu_count() or 4)) as pool:
        results = list(pool.map(run, modules))
    tests = 0
    for module, done, seconds in results:
        ran = re.search(r"^Ran (\d+) tests?", done.stderr, re.MULTILINE)
        tests += int(ran.group(1)) if ran else 0
        verdict = "ok" if done.returncode == 0 else "FAILED"
        print(f"{verdict:6} {module:28} {ran.group(1) if ran else '?':>4} tests {seconds:6.1f}s")
    failed = [(module, done) for module, done, _ in results if done.returncode != 0]
    for module, done in failed:
        print(f"\n===== {module}\n{done.stdout}{done.stderr}")
    print(f"\n{tests} tests in {time.monotonic() - started:.1f}s: "
          + (f"{len(failed)} of {len(modules)} modules FAILED" if failed else "OK"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
