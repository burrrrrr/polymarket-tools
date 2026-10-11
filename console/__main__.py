"""
Run the console: python -m console [--db PATH] [--tools-db PATH] [--port 8787]

--db defaults to farm.db, the production database, and --tools-db to tools.db,
the database the console shares with the order tools' worker (python -m
tools). Both are looked for in the data directory (see data_dir.py): the
directory POLYMARKET_DATA_DIR names, or the repo directory, wherever the
console is started from.

It binds 127.0.0.1 only, never a public interface. The console has no login,
and it controls real orders through farm.db (pausing, retiring, settings,
stopping the farmer), so anyone who could reach the port could move money.
Reach it from another machine through an SSH tunnel, not by changing the bind
address. console.security guards it against other web pages in the same
browser.
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

import data_dir
from console.app import DEFAULT_PORT, create_app

HOST = "127.0.0.1"
REPO = Path(__file__).parent.parent


def main():
    parser = argparse.ArgumentParser(prog="python -m console",
                                     description="Local web console for the reward farmer and the order tools.")
    parser.add_argument("--db", help="SQLite file, the production database (default: farm.db "
                                     f"{data_dir.LOCATION_HELP})")
    parser.add_argument("--tools-db", help="SQLite file shared with the worker (default: "
                                           f"tools.db {data_dir.LOCATION_HELP})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port (default: {DEFAULT_PORT})")
    args = parser.parse_args()
    load_dotenv(REPO / ".env")
    try:
        farm_db, tools_db = data_dir.farm_db_path(args.db), data_dir.tools_db_path(args.tools_db)
    except data_dir.NoDataDir as e:
        print(f"Not started: {e}.", file=sys.stderr, flush=True)
        return 1
    app = create_app(farm_db, port=args.port, funder=os.getenv("POLYMARKET_FUNDER_ADDRESS"),
                     tools_db=tools_db)
    print(f"Console on http://{HOST}:{args.port} reading {farm_db} and {tools_db}",
          flush=True)
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning",
                timeout_graceful_shutdown=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
