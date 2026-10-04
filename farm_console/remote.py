"""What the Review page fetches on demand: a market's resolution rules from
Gamma, and the funder's holdings in it, read the way the farmer reads them."""

from datetime import datetime, timedelta
from typing import NamedTuple, Optional

import httpx

import reward_farmer
from reward_screener import GAMMA_HOST, parse_time

RULES_TTL = timedelta(minutes=10)
TIMEOUT_S = 15


class Rules(NamedTuple):
    description: str
    resolution_source: Optional[str]
    end_date: Optional[datetime]


def fetch_rules(condition_id):
    with httpx.Client() as http:
        resp = http.get(f"{GAMMA_HOST}/markets", params={"condition_ids": condition_id},
                        timeout=TIMEOUT_S)
        resp.raise_for_status()
        entries = resp.json() or []
    entry = next((e for e in entries
                  if isinstance(e, dict) and e.get("conditionId") == condition_id), None)
    if entry is None:
        raise LookupError("Gamma has no record of this market")
    return Rules(entry.get("description") or "", entry.get("resolutionSource") or None,
                 parse_time(entry.get("endDate")))


def fetch_position(user, condition_id, fav_token_id):
    """The wallet's reward_farmer.Position in this market, from the same data API
    positions and CLOB market record the farmer uses."""
    if not user:
        raise LookupError("POLYMARKET_FUNDER_ADDRESS is not set in .env")
    with httpx.Client() as http:
        info = reward_farmer.fetch_market_info(http, condition_id, timeout=TIMEOUT_S)
        holdings = reward_farmer.fetch_holdings(http, user, timeout=TIMEOUT_S)
    longs = [t for t in info.token_ids if t != fav_token_id]
    fav = holdings.get(fav_token_id, (0.0, 0.0))
    long = holdings.get(longs[0], (0.0, 0.0)) if len(longs) == 1 else (0.0, 0.0)
    return reward_farmer.Position(fav[0], fav[1], long[0], long[1])


class TtlCache:
    """fetch(key) results kept for ttl; a fetch that raises is not kept."""

    def __init__(self, fetch, ttl, clock):
        self.fetch = fetch
        self.ttl = ttl
        self.clock = clock
        self._entries = {}

    def get(self, key):
        now = self.clock()
        hit = self._entries.get(key)
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        value = self.fetch(key)
        self._entries[key] = (now, value)
        return value
