import json
from datetime import datetime
from typing import NamedTuple, Optional

from polymarket.hosts import GAMMA_HOST
from polymarket.http import RATE_LIMIT_RETRIES, get_with_retry, in_chunks
from polymarket.parse import as_float, parse_time

# Most condition ids Gamma /markets resolves in one request.
GAMMA_REQUEST_LIMIT = 100
SEARCH_LIMIT_PER_TYPE = 20


class Market(NamedTuple):
    """One market as the search lists it. outcomes and token_ids run in parallel."""

    condition_id: str
    question: str
    slug: str
    event_title: str
    outcomes: tuple
    token_ids: tuple
    end_date: Optional[datetime]
    volume: float
    tick: Optional[float]
    closed: bool
    accepting_orders: bool
    min_size: float


def decode_list(value):
    """A Gamma list field, which arrives as a list or as a list encoded in a JSON
    string; anything else is an empty list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def request_markets(http, condition_ids, closed=None, timeout=20, *, limit=True,
                    retries=RATE_LIMIT_RETRIES):
    """The entries of one /markets request for these ids, at most
    GAMMA_REQUEST_LIMIT of them, as Gamma sent them. Raises when the request
    fails.

    closed is as in fetch_markets. limit=False sends no limit, for a request
    that names one market. A 429 is waited out up to retries times.
    """
    params = [("condition_ids", c) for c in condition_ids]
    if closed is not None:
        params.append(("closed", "true" if closed else "false"))
    if limit:
        # Without a limit Gamma caps a response at 20 markets.
        params.append(("limit", str(len(condition_ids))))
    return get_with_retry(http, f"{GAMMA_HOST}/markets", params, timeout, retries).json() or []


def fetch_markets(http, condition_ids, closed=None, timeout=20):
    """Gamma's entry for each of these markets, GAMMA_REQUEST_LIMIT ids a request.

    Gamma leaves closed markets out unless closed is True, and then returns
    closed markets alone. A market Gamma does not return is absent. Raises when
    any request fails.
    """
    def fetch(chunk):
        return [e for e in request_markets(http, chunk, closed, timeout) if isinstance(e, dict)]

    return [e for entries in in_chunks(fetch, condition_ids, GAMMA_REQUEST_LIMIT) for e in entries]


def market_names(http, condition_ids):
    """{condition_id: question} for each market Gamma knows, open or closed."""
    def names(entries):
        named = ((e.get("conditionId"), e.get("question") or e.get("title")) for e in entries)
        return {condition_id: name for condition_id, name in named if condition_id and name}

    wanted = list(dict.fromkeys(c for c in condition_ids if c))
    found = names(fetch_markets(http, wanted))
    missing = [c for c in wanted if c not in found]
    if missing:
        found.update(names(fetch_markets(http, missing, closed=True)))
    return found


def search(http, query, timeout=20):
    """The open markets of the active events matching query, in Gamma's order.

    Raises when the search fails.
    """
    # Without events_status the search returns markets that resolved years ago.
    params = {"q": query, "limit_per_type": str(SEARCH_LIMIT_PER_TYPE), "events_status": "active"}
    payload = get_with_retry(http, f"{GAMMA_HOST}/public-search", params, timeout).json() or {}
    return [
        _market(entry, event.get("title") or "")
        for event in payload.get("events") or []
        for entry in event.get("markets") or []
        if not entry.get("closed")
    ]


def _market(entry, event_title):
    return Market(
        condition_id=entry.get("conditionId") or "",
        question=entry.get("question") or entry.get("title") or "",
        slug=entry.get("slug") or "",
        event_title=event_title,
        outcomes=tuple(str(o) for o in decode_list(entry.get("outcomes"))),
        token_ids=tuple(str(t) for t in decode_list(entry.get("clobTokenIds"))),
        end_date=parse_time(entry.get("endDate")),
        volume=as_float(entry.get("volume24hr")) or as_float(entry.get("volume")) or 0.0,
        tick=as_float(entry.get("orderPriceMinTickSize")),
        closed=bool(entry.get("closed")),
        accepting_orders=bool(entry.get("acceptingOrders")),
        min_size=as_float(entry.get("orderMinSize")) or 0.0,
    )
