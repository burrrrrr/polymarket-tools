"""What the Review page fetches on demand: a market's resolution rules from
Gamma, the funder's holdings in it, its Gamma market record and live book, and
the total reward rates, all read the way the farmer reads them."""

import threading
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

import httpx

import reward_farmer
import reward_screener
from polymarket import books
from polymarket.gamma import request_markets
from polymarket.parse import parse_time

RULES_TTL = timedelta(minutes=10)
# The Gamma market record holds the reward terms and tick, which rarely change;
# the book is cached just long enough that several open tabs share one fetch.
INFO_TTL = timedelta(minutes=5)
BOOK_TTL = timedelta(seconds=2)
TOTAL_RATES_TTL = timedelta(seconds=reward_farmer.TOTAL_RATES_S)
TIMEOUT_S = 15


class Rules(NamedTuple):
    description: str
    resolution_source: Optional[str]
    end_date: Optional[datetime]


def fetch_rules(condition_id):
    """The market's Rules; Gamma lists a closed market only when asked for closed ones."""
    with httpx.Client() as http:
        for closed in (None, True):
            entries = request_markets(http, [condition_id], closed, TIMEOUT_S, limit=False,
                                      retries=0)
            entry = next((e for e in entries
                          if isinstance(e, dict) and e.get("conditionId") == condition_id), None)
            if entry is not None:
                return Rules(entry.get("description") or "",
                             entry.get("resolutionSource") or None,
                             parse_time(entry.get("endDate")))
    raise LookupError("Gamma has no record of this market")


def _market_info(http, condition_id):
    infos = reward_farmer.fetch_market_infos(http, [condition_id], timeout=TIMEOUT_S)
    info = infos.get(condition_id)
    if info is None:
        raise LookupError("Gamma has no record of this market")
    return info


def fetch_position(user, condition_id, fav_token_id):
    """The wallet's reward_farmer.Position in this market, from the same data API
    positions and Gamma market record the farmer uses."""
    if not user:
        raise LookupError("POLYMARKET_FUNDER_ADDRESS is not set in .env")
    with httpx.Client() as http:
        info = _market_info(http, condition_id)
        holdings = reward_farmer.fetch_holdings(http, user, timeout=TIMEOUT_S)
    longs = [t for t in info.token_ids if t != fav_token_id]
    fav = holdings.get(fav_token_id, (0.0, 0.0))
    long = holdings.get(longs[0], (0.0, 0.0)) if len(longs) == 1 else (0.0, 0.0)
    return reward_farmer.Position(fav[0], fav[1], long[0], long[1])


def fetch_info(condition_id):
    """The market's reward_farmer.MarketInfo from its Gamma record."""
    with httpx.Client() as http:
        return _market_info(http, condition_id)


def fetch_book(token_id):
    """The token's live book as polymarket.books.Levels."""
    with httpx.Client() as http:
        return books.fetch_book(http, token_id, timeout=TIMEOUT_S)


def fetch_total_rates():
    """{condition_id: total daily rate} of every market with native rewards,
    sponsored pools included."""
    with httpx.Client() as http:
        return reward_screener.fetch_total_rates(http)


def in_background(work):
    threading.Thread(target=work, daemon=True, name="console-total-rates").start()


class TotalRates:
    """The fetch_total_rates map, which takes about ten seconds to read.

    get() never waits on it. It returns the last map, None until the first
    fetch lands, and starts a refresh through spawn when there is no map yet or
    the last refresh started ttl ago. A failed or empty fetch keeps the old
    map, as in the farmer. Each map that lands is passed to on_rates.
    """

    def __init__(self, fetch, ttl, clock, spawn=in_background, on_rates=lambda rates: None):
        self.fetch = fetch
        self.ttl = ttl
        self.clock = clock
        self.spawn = spawn
        self.on_rates = on_rates
        self.rates = None
        self._started = None
        self._running = False
        self._lock = threading.Lock()

    def get(self):
        if self._claim():
            self.spawn(self._fetch)
        return self.rates

    def refresh(self):
        """Fetch the map in this thread, unless it is fresh or already being fetched."""
        if self._claim():
            self._fetch()

    def _claim(self):
        with self._lock:
            due = not self._running and (self._started is None
                                         or self.clock() - self._started >= self.ttl)
            if due:
                self._running, self._started = True, self.clock()
            return due

    def _fetch(self):
        try:
            rates = self.fetch()
            if rates:
                self.rates = rates
                self.on_rates(rates)
        except Exception:
            pass
        finally:
            self._running = False


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
