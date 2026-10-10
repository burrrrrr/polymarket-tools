"""The data directory: where the farmer, the console and the worker keep their
databases.

One directory holds farm.db, which the farmer and the console share, and
tools.db, which the console and the worker share. It is the environment
variable POLYMARKET_DATA_DIR, which .env may set, and the repo directory
without it. A relative value is taken from the repo directory, so the three
processes agree wherever each is started. The directory is never made here: a
mistyped one would start every process on empty databases.

The variable is read when a path is asked for, so an entry point asks after it
has loaded .env. A path given on the command line is used as it is.
"""

import os
from pathlib import Path

VARIABLE = "POLYMARKET_DATA_DIR"
REPO = Path(__file__).parent
LOCATION_HELP = f"in {VARIABLE}, or in the repo directory when that is not set"


class NoDataDir(Exception):
    """POLYMARKET_DATA_DIR names no directory."""


def farm_db_path(explicit=None):
    """The farm.db to open: explicit when a path was given, or the data directory's."""
    return explicit or _in_data_dir("farm.db")


def tools_db_path(explicit=None):
    """The tools.db to open: explicit when a path was given, or the data directory's."""
    return explicit or _in_data_dir("tools.db")


def _in_data_dir(name):
    directory = REPO / (os.getenv(VARIABLE) or "")
    if not directory.is_dir():
        raise NoDataDir(f"{VARIABLE} is not a directory: {directory}")
    return str(directory / name)
