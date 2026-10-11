"""
Run the order tools' worker:   python -m tools [--db PATH] [--key-stdin | --no-key]
Import the old snapshot files: python -m tools import-snapshots [DIR] [--db PATH]

The worker is the keyed half of the console's order tools. The console queues
jobs in tools.db and this process runs them, so the key never reaches the
process that serves HTTP. Ctrl+C or a terminate signal stops it once the
running job has finished.

The key comes from the environment or the Bitwarden vault, as polymarket.keys
describes, and the vault's master password is asked for here when the vault is
locked. --key-stdin reads the key itself from one line of standard input
instead, for a machine with no vault: unlock the vault elsewhere and pipe the
key in over SSH. --no-key starts a worker that fails every job that needs one.

--db defaults to tools.db in the data directory (see data_dir.py): the
directory POLYMARKET_DATA_DIR names, or the repo directory. DIR defaults to
the snapshots folder of the same directory.
"""

import argparse
import getpass
import signal
import sys
import threading
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv

import data_dir
from polymarket import client as polymarket_client
from polymarket import http as polymarket_http
from polymarket import keys, region
from tools import db, snapshot, sniper, worker

REPO = Path(__file__).parent.parent
STOP_SIGNALS = ("SIGINT", "SIGTERM", "SIGBREAK")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m tools",
        description="The keyed worker behind the console's order tools. With no command it "
                    "runs until Ctrl+C.")
    parser.add_argument("--db", help="SQLite file shared with the console (default: tools.db "
                                     f"{data_dir.LOCATION_HELP})")
    key = parser.add_mutually_exclusive_group()
    key.add_argument("--key-stdin", action="store_true",
                     help="read the wallet key from one line of standard input, not from the "
                          "environment or Bitwarden")
    key.add_argument("--no-key", action="store_true",
                     help="run without a key: every job that needs one fails")
    commands = parser.add_subparsers(dest="command", metavar="command")
    importer = commands.add_parser(
        "import-snapshots", help="store the old orders_snapshot_*.json files in tools.db, once",
        description="Store each orders_snapshot_*.json file as a snapshot. A file stored "
                    "before is skipped. The files' times are read in this machine's time "
                    "zone. Needs no key.")
    importer.add_argument("directory", nargs="?",
                          help=f"where the files are (default: snapshots {data_dir.LOCATION_HELP})")
    # SUPPRESS keeps a --db given before the command when none follows it.
    importer.add_argument("--db", default=argparse.SUPPRESS, help="as above")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    load_dotenv(REPO / ".env")
    try:
        args.db = data_dir.tools_db_path(args.db)
    except data_dir.NoDataDir as e:
        return fail(f"Not started: {e}.")
    if args.command == "import-snapshots":
        args.directory = data_dir.snapshots_path(args.directory)
        return import_snapshots(args)
    return run_worker(args)


def import_snapshots(args):
    try:
        with closing(db.open_db(args.db)) as conn:
            done = snapshot.import_files(conn, args.directory)
    except (OSError, ValueError) as e:
        return fail(f"Import failed: {e}")
    print(f"Stored {done.files} snapshot file{plural(done.files)} with {done.orders:,} "
          f"order{plural(done.orders)} in {args.db}; {done.skipped} already stored.")
    return 0


def run_worker(args):
    sys.setswitchinterval(sniper.SWITCH_INTERVAL_S)
    http = polymarket_http.persistent_client()
    try:
        client = sniper_client = None
        if not args.no_key:
            clients = connect(args, http)
            if clients is None:
                return 1
            client, sniper_client = clients
        stop = threading.Event()
        for name in STOP_SIGNALS:
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda signum, frame: stop.set())
        print(f"Worker on {args.db}, {'keyed' if client else 'no key'}. Ctrl+C stops it.",
              flush=True)
        try:
            worker.run(args.db, client, http, stop=stop, sniper_client=sniper_client)
        except db.WorkerRunning as e:
            return fail(f"Not started: {e}.")
        print("Worker stopped.", flush=True)
        return 0
    finally:
        http.close()


def connect(args, http):
    """(the jobs' client, the sniper's client), both authenticated, or None
    after saying why there is none. The sniper's client presigns orders at a
    tick its market has not reached, so the jobs never sign through it."""
    verdict = region.check(http)
    print(f"Region: {verdict.message}.", flush=True)
    if not verdict.allowed:
        fail("Not started: a keyed worker would place orders from here. "
             "Start it with --no-key to run without one.")
        return None
    try:
        key = read_key(args.key_stdin)
        polymarket_http.use_for_sdk(http)
        return polymarket_client.connect(key), polymarket_client.connect(key)
    except (keys.KeyUnavailable, polymarket_client.ConfigError) as e:
        fail(f"Not started: {e}.")
        return None


def read_key(from_stdin):
    if not from_stdin:
        return keys.load_private_key(lambda: getpass.getpass("Bitwarden master password: "))
    return keys.read_stdin_key(sys.stdin)


def fail(message):
    print(message, file=sys.stderr, flush=True)
    return 1


def plural(n):
    return "" if n == 1 else "s"


if __name__ == "__main__":
    sys.exit(main())
