import json
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import httpx

from polymarket import gamma
from polymarket.orders import open_orders, parse_order
from tools import db

FILE_PATTERN = "orders_snapshot_*.json"


class Taken(NamedTuple):
    """names_error is why the orders were stored without market names, or None."""

    snapshot_id: int
    orders: int
    names_error: Optional[str]


class Imported(NamedTuple):
    """files and orders count what this run stored; skipped files were stored before."""

    files: int
    orders: int
    skipped: int


def take(client, http, conn, now, note, job_id=None, farmer_markets=None):
    """Store the account's open orders as one snapshot taken at `now`.

    farmer_markets holds the condition ids the farmer manages now, so that a
    restore can leave the farmer's orders to the farmer whatever it manages by
    then. None stores no record of it.

    Raises when the listing fails or cannot be read, and then stores nothing.
    The market names come from Gamma. They are for display only, so when Gamma
    cannot be read the orders are stored without them.
    """
    orders = open_orders(client)
    names, names_error = {}, None
    try:
        names = gamma.market_names(http, [o.market for o in orders])
    except (httpx.HTTPError, ValueError) as e:
        names_error = f"{type(e).__name__}: {e}"
    snapshot_id = db.insert_snapshot(conn, now, note, orders, names, job_id=job_id,
                                     farmer_markets=farmer_markets)
    return Taken(snapshot_id, len(orders), names_error)


def import_files(conn, directory, local_zone=None):
    """Store each orders_snapshot_*.json of the directory as a snapshot, once.

    A file already stored, by name, is skipped, so running this again changes
    nothing. The files hold naive local times: each is read in local_zone, by
    default the zone of the machine this runs on, and stored as UTC. So run the
    import on the machine that took the snapshots, or name its zone.

    Raises FileNotFoundError when the directory is missing, and ValueError
    naming the file when one is not a snapshot. That file stores nothing, and
    the files before it stay stored.
    """
    files = sorted(Path(directory).iterdir())
    known = db.imported_sources(conn)
    stored = orders = skipped = 0
    for path in files:
        if not path.match(FILE_PATTERN):
            continue
        if path.name in known:
            skipped += 1
            continue
        taken_at, entries = _read_file(path, local_zone)
        db.insert_snapshot(
            conn, taken_at, "", [o for o, _ in entries],
            {o.market: name for o, name in entries if name}, source=path.name)
        stored += 1
        orders += len(entries)
    return Imported(stored, orders, skipped)


def _read_file(path, local_zone):
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        taken = datetime.fromisoformat(saved["metadata"]["timestamp"])
        entries = [(parse_order(entry), entry.get("market_name")) for entry in saved["orders"]]
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{path.name} is not an order snapshot: {e!r}") from None
    if taken.tzinfo is None:
        # astimezone() reads a naive time in the machine's own zone.
        taken = taken.replace(tzinfo=local_zone) if local_zone else taken.astimezone()
    return taken.astimezone(timezone.utc), entries
