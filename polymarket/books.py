"""Order books from the CLOB: one token's bid and ask levels and its tick."""

from typing import NamedTuple, Optional

from polymarket.hosts import CLOB_HOST
from polymarket.http import in_chunks
from polymarket.parse import as_float

# The most tokens /books accepts in one request; 501 is refused as too large.
BOOKS_REQUEST_LIMIT = 500


class Levels(NamedTuple):
    """One token's book: bids high to low, asks low to high, (price, size)."""

    bids: list
    asks: list
    tick: Optional[float] = None


def parse_levels(entry):
    """The Levels in one /book response or one /books entry."""
    def side(raw, descending):
        levels = []
        for level in raw or []:
            price, size = as_float(level.get("price")), as_float(level.get("size"))
            if price is not None and size is not None and size > 0:
                levels.append((price, size))
        return sorted(levels, reverse=descending)

    return Levels(
        bids=side(entry.get("bids"), descending=True),
        asks=side(entry.get("asks"), descending=False),
        tick=as_float(entry.get("tick_size")),
    )


def best_bid(levels):
    return levels.bids[0][0] if levels.bids else None


def best_ask(levels):
    return levels.asks[0][0] if levels.asks else None


def fetch_book(http, token_id, timeout=20):
    """One token's live Levels. Raises when the read fails."""
    resp = http.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=timeout)
    resp.raise_for_status()
    return parse_levels(resp.json())


def fetch_books(http, token_ids, timeout=20):
    """{token_id: Levels}, from one /books request for each BOOKS_REQUEST_LIMIT tokens.

    The tick comes with the levels, so this never reads the SDK's tick cache,
    which does not expire. A token the CLOB does not know is absent from the
    result. Raises when any request fails.
    """
    def fetch(chunk):
        resp = http.post(f"{CLOB_HOST}/books", json=[{"token_id": t} for t in chunk],
                         timeout=timeout)
        resp.raise_for_status()
        return resp.json() or []

    books = {}
    for entries in in_chunks(fetch, token_ids, BOOKS_REQUEST_LIMIT):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("asset_id"):
                books[str(entry["asset_id"])] = parse_levels(entry)
    return books
