"""
Run the farm console: python -m farm_console --db farm.db --port 8787

It binds 127.0.0.1 only, never a public interface. The console has no login,
and it controls real orders through farm.db (pausing, retiring, settings,
stopping the farmer), so anyone who could reach the port could move money.
Reach it from another machine through an SSH tunnel, not by changing the bind
address. farm_console.security guards it against other web pages in the same
browser.
"""

import argparse
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from farm_console.app import DEFAULT_PORT, create_app

HOST = "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(prog="python -m farm_console",
                                     description="Local web console for the reward farmer.")
    parser.add_argument("--db", default="farm.db", help="SQLite file (default: farm.db)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port (default: {DEFAULT_PORT})")
    args = parser.parse_args()
    load_dotenv(Path(__file__).parent.parent / ".env")
    app = create_app(args.db, port=args.port, funder=os.getenv("POLYMARKET_FUNDER_ADDRESS"))
    print(f"Farm console on http://{HOST}:{args.port} reading {args.db}", flush=True)
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning",
                timeout_graceful_shutdown=1)


if __name__ == "__main__":
    main()
