#!/usr/bin/env python3
"""
Polymarket Tick Size Watcher

Watches one or more markets and places a limit order on each the moment its
own minimum tick size shrinks (by default 0.01 -> 0.001). Polymarket caps
prices at `1 - tick`, so a market on a 0.01 tick cannot trade above $0.99;
when it moves to a 0.001 tick, $0.999 (99.9c) becomes legal for the first
time. The point of this tool is to be first in the queue at that price.

Run it with no arguments for an interactive picker: fuzzy-search markets,
choose an outcome, size the order, and either add another market or confirm
the batch and start watching. Each market in a batch fires independently the
moment its own tick shrinks, while the rest keep watching. Once the batch ends
- every order fired, timed out, or dropped with Ctrl+C - it returns to the
picker for the next batch, until you quit with 'q'. Every prompt has a CLI
equivalent for unattended use.

Detection runs on two independent paths at once, whichever fires first wins.
Each path is a single connection covering the whole batch:

  1. WSS market channel (`tick_size_change` events, plus the tick carried on
     every `book` message). This is the low-latency path.
  2. A REST poll of /books, which returns the live tick size *and* the top of
     the book for every market in one request, bypassing the SDK's in-process
     tick cache.

Both paths also track the best bid/ask continuously. That feeds the entry-price
guard: the market must be sitting at 99c right up to the moment the tick
changes, or the order is rejected. The price used is always the last one
observed *before* the change, never a fresh read afterwards - the instant a
tick opens up, competing bots move the book, and a post-change read would
reject a perfectly good setup.

Latency matters here, so every network lookup `create_order` would otherwise
perform at fire time - tick size, neg risk, exchange version - is pre-warmed
at startup. When the event lands, only the single order POST hits the network.

The connection that POST rides on is pre-warmed too, and deliberately kept
alive: httpx reaps idle keep-alive connections after 5s, so a quiet watch
would otherwise pay a fresh TCP + TLS handshake (~300ms) at the worst possible
moment. See `install_persistent_http_client`.

Orders are pre-signed, but never once and held: a v2 signed order embeds its
signing timestamp in the EIP-712 payload, so a background thread re-signs on a
short interval and anything stale is signed inline instead. See
`PresignedOrder`.

Usage:
  python tick_watch.py                                           # interactive
  python tick_watch.py --slug will-x-happen --size 100            # unattended
  python tick_watch.py --slug will-x --slug will-y --size 100     # two markets
"""

import argparse
import difflib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from typing import NamedTuple, Optional

import httpx

import py_clob_client_v2.http_helpers.helpers as clob_http
from py_clob_client_v2.clob_types import OrderArgsV2, OrderType

from config import load_config, create_client
from key_loader import cleanup_key_source

GAMMA_HOST = os.getenv("POLYMARKET_GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
WSS_HOST = os.getenv(
    "POLYMARKET_WSS_HOST", "wss://ws-subscriptions-clob.polymarket.com"
)
WSS_MARKET_URL = f"{WSS_HOST}/ws/market"

# Tick sizes the CLOB accepts, as the API spells them.
VALID_TICKS = ("0.1", "0.01", "0.001", "0.0001")

# How many search hits to show at once.
RESULT_LIMIT = 12

# Most tokens /books accepts in one request; 501 is refused as too large.
BOOKS_REQUEST_LIMIT = 500


def _make_output_safe():
    """Replace unencodable characters instead of crashing on them.

    Market questions carry degree signs, accents, and the occasional emoji.
    A stock Windows console is cp1252, where printing one of those raises
    UnicodeEncodeError - which would otherwise kill the watcher mid-run.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def log(message):
    """Print with a millisecond timestamp; detection timing is the point here."""
    stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{stamp}] {message}", flush=True)


def cents(price):
    """Format a dollar price as cents for display."""
    if price is None:
        return "  --  "
    return f"{float(price) * 100:.1f}c"


# ---------------------------------------------------------------------------
# Region preflight
# ---------------------------------------------------------------------------

# The geoblock is enforced on the IP that submits the order, so a VPN that is
# not actually carrying this process's traffic turns every attempt into a 403.
# Far cheaper to learn that at startup than at the instant a tick changes.
#
# Two independent signals, because either alone gives false clearances:
#
#   1. A live probe of the hosts themselves. A blocked request is answered by
#      the edge with 451 before it reaches the API, and that happens for IPs
#      the venue simply distrusts - a shared VPN exit in an unrestricted
#      country gets a 451 that no published list predicts.
#   2. The country and region tables below, which catch the close-only
#      jurisdictions: reads succeed there, and only order submission is
#      rejected, so probing alone would clear them.
#
# Source of truth for the tables:
# https://docs.polymarket.com/developers/CLOB/geoblock
# They are a snapshot. An unrecognised region is reported and allowed, never
# blocked, so a change to the published list cannot silently stop a run.

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
TRACE_URL = f"{CLOB_HOST}/cdn-cgi/trace"

# Countries where the API rejects NEW orders - the docs' "close-only" list plus
# the fully blocked jurisdictions. This tool only ever opens a position, so
# close-only is a hard stop rather than a partial restriction.
API_BLOCKED_COUNTRIES = frozenset({
    "AU", "BE", "BI", "BR", "BY", "CD", "CF", "CU", "DE", "ET", "FR", "GB",
    "IQ", "IR", "IT", "KP", "LB", "LY", "MM", "NI", "NZ", "PL", "RU", "SD",
    "SG", "SK", "SO", "SS", "SY", "TH", "TW", "UM", "US", "VE", "YE", "ZW",
})

# Countries blocked only in part, keyed to the subdivisions that are blocked.
API_BLOCKED_REGIONS = {
    "CA": frozenset({"AB", "BC", "ON", "QC"}),
    "UA": frozenset({"09", "14", "43"}),  # Luhansk, Donetsk, Crimea
}

# Blocked on the website but explicitly NOT on the API. polymarket.com's own
# /api/geoblock reports blocked=true for these, which is exactly why its
# verdict is ignored below and only its country/region reading is used.
FRONTEND_ONLY_COUNTRIES = frozenset({"IE", "JP", "MT", "NL"})

# Substring identifying a geoblock rejection in an order response.
GEOBLOCK_MARKER = "restricted in your region"


def read_region(timeout=6.0):
    """Return (ip, country, region) as Polymarket's own geo lookup sees us.

    Falls back to the CDN edge's country when the geoblock endpoint cannot be
    reached; that is the same edge the CLOB sits behind, so it is a fair second
    opinion. Returns (None, None, None) when neither answers.
    """
    try:
        data = httpx.get(GEOBLOCK_URL, timeout=timeout).raise_for_status().json()
        # data["blocked"] is deliberately unused: it encodes the website's
        # rules, which block several regions whose API access is unrestricted.
        return (
            data.get("ip"),
            (data.get("country") or "").upper(),
            str(data.get("region") or "").upper(),
        )
    except Exception:
        pass

    try:
        text = httpx.get(TRACE_URL, timeout=timeout).raise_for_status().text
        fields = dict(
            line.split("=", 1) for line in text.splitlines() if "=" in line
        )
        return fields.get("ip"), fields.get("loc", "").upper(), ""
    except Exception:
        return None, None, None


def probe_host(url, params=None, timeout=6.0):
    """Return the status the edge answers with, or None if it never answered.

    Only the status matters: a geoblocked request is rejected at the edge with
    451 before the API sees it, whatever the body says.
    """
    try:
        return httpx.get(url, params=params, timeout=timeout).status_code
    except Exception:
        return None


def region_verdict(country, region):
    """Classify a country/region pair as 'ok', 'blocked', or 'unknown'."""
    if not country:
        return "unknown"
    if country in API_BLOCKED_COUNTRIES:
        return "blocked"
    partial = API_BLOCKED_REGIONS.get(country)
    if partial:
        if not region:
            return "unknown"
        if region in partial:
            return "blocked"
    return "ok"


def preflight_region():
    """Report the egress region; return True when new orders are permitted.

    Detection and reporting only - the caller decides whether a blocked egress
    is fatal, since a dry run has nothing to submit.
    """
    ip, country, region = read_region()
    where = (f"{country}-{region}" if region else country) if country else "unknown"

    # The order path is what matters, so its host decides the verdict.
    clob_status = probe_host(f"{CLOB_HOST}/time")
    if clob_status == 451:
        log(f"Region check: {where} via {ip} - CLOB refuses this IP (451)")
        log(
            "That is an edge-level block on the exit address itself, not a "
            "country rule; another server in the same country usually clears it."
        )
        return False

    if country:
        verdict = region_verdict(country, region)
        if verdict == "blocked":
            log(f"Region check: {where} via {ip} - API REJECTS NEW ORDERS from here")
            return False
        if verdict == "unknown":
            log(f"Region check: {where} via {ip} - region unrecognised, continuing")
        else:
            note = (
                " (website-blocked, API permitted)"
                if country in FRONTEND_ONLY_COUNTRIES
                else ""
            )
            log(f"Region check: {where} via {ip} - new orders permitted{note}")
    elif clob_status is None:
        log("Region check: hosts unreachable, continuing unchecked")
    else:
        log(f"Region check: CLOB reachable, region unreadable via {ip}")

    # Market lookup lives on a different host with its own rules. Losing it
    # costs the picker and --slug but not --token-id, so it is only a warning.
    if probe_host(f"{GAMMA_HOST}/markets", params={"limit": 1}) == 451:
        log(
            "Warning: the market lookup host refuses this IP (451); the picker "
            "and --slug will fail, --token-id will still work"
        )

    return True


def is_geoblocked_error(exc):
    """True when an order was rejected because of the submitting IP.

    A geoblock is not the venue lagging behind a tick change, so retrying it
    only burns the retry window a genuine race needs.
    """
    status = getattr(exc, "status_code", None)
    if status == 451:
        return True
    if status is not None and status != 403:
        return False
    message = getattr(exc, "error_msg", None)
    if message is None:
        message = exc
    return GEOBLOCK_MARKER in str(message).lower()


def human_volume(value):
    """Compact volume formatting for the results table."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= limit:
            return f"${value / limit:.1f}{suffix}"
    return f"${value:.0f}"


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

class Aborted(Exception):
    """An interactive prompt ended without an answer.

    `no_input` separates "there was never a terminal to answer from" - which is
    a usage error worth a non-zero exit - from a deliberate quit, which is not.
    """

    def __init__(self, reason="cancelled", no_input=False):
        super().__init__(reason)
        self.reason = reason
        self.no_input = no_input


def ask(text, default=None):
    """Read a line, treating EOF/Ctrl-C as an abort rather than a crash."""
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        answer = input(f"{text}{suffix}: ").strip()
    except EOFError:
        # Redirected or closed stdin. Windows shells can report a redirected
        # handle as a tty, so this is the reliable signal that nobody is there.
        raise Aborted("stdin is not available for prompting", no_input=True)
    except KeyboardInterrupt:
        print()
        raise Aborted("interrupted")
    return answer or (str(default) if default is not None else "")


def ask_yes_no(text, default=False):
    """Yes/no prompt. Anything unrecognized re-asks rather than guessing."""
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{text} [{hint}]").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_float(text, default=None, minimum=None, maximum=None):
    """Read a number, re-asking until it parses and fits the bounds."""
    while True:
        raw = ask(text, default)
        if not raw:
            print("  A value is required.")
            continue
        try:
            value = float(raw)
        except ValueError:
            print(f"  '{raw}' is not a number.")
            continue
        if minimum is not None and value < minimum:
            print(f"  Must be at least {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"  Must be at most {maximum}.")
            continue
        return value


# ---------------------------------------------------------------------------
# Market search
# ---------------------------------------------------------------------------

def _decode_gamma_list(value):
    """Gamma returns some list fields as JSON-encoded strings. Normalize both."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def fuzzy_score(query, text):
    """Score how well `text` matches `query` (0..1).

    Gamma's own search decides which markets come back; this only reranks them,
    so a cheap blend of substring hit, sequence similarity, and word overlap is
    plenty. Word overlap is what makes out-of-order queries ("rate fed") match.
    """
    query = (query or "").strip().lower()
    text = (text or "").lower()
    if not query or not text:
        return 0.0

    if query in text:
        # Prefer the shortest title containing the whole query.
        similarity = 0.9 + 0.1 * (len(query) / max(len(text), 1))
    else:
        similarity = difflib.SequenceMatcher(None, query, text).ratio()

    query_words = _words(query)
    overlap = (
        len(query_words & _words(text)) / len(query_words) if query_words else 0.0
    )
    return max(similarity, 0.5 * similarity + 0.5 * overlap)


class Criteria:
    """The rules a market must satisfy to be worth watching."""

    def __init__(self, args):
        self.required_price = args.require_price
        self.target_tick = args.target_tick
        self.tolerance = args.price_tolerance
        self.side = args.side
        self.enforce_price = not args.no_price_check

    @property
    def source_name(self):
        return "best bid" if self.side == "BUY" else "best ask"


class Candidate:
    """One tradeable outcome of one market - what the user actually picks."""

    def __init__(self, market, event_title=""):
        self.question = market.get("question") or market.get("title") or ""
        self.slug = market.get("slug") or ""
        self.condition_id = market.get("conditionId") or ""
        self.event_title = event_title
        self.tick_size = str(market.get("orderPriceMinTickSize") or "")
        self.volume = market.get("volume24hr") or market.get("volume") or 0
        self.accepting_orders = bool(market.get("acceptingOrders"))
        self.closed = bool(market.get("closed"))
        try:
            self.min_order_size = float(market.get("orderMinSize") or 0)
        except (TypeError, ValueError):
            self.min_order_size = 0.0

        self.token_id = ""
        self.outcome = ""
        self.outcome_index = -1
        self.price: Optional[float] = None
        self.score = 0.0
        # Authoritative top of book, filled in by enrich_with_books. Gamma's
        # outcomePrices is a mid/last price and disagrees with the book often
        # enough to matter here (observed: Gamma 0.99 against a real best bid
        # of 0.98), so it is only ever a display fallback.
        self.book = None

    @property
    def label(self):
        return f"{self.question} -> {self.outcome}" if self.outcome else self.question

    def short_label(self, width=58):
        """Label trimmed to `width`, shortening the question so the outcome
        always survives - the outcome is what distinguishes two rows of the
        same market."""
        if not self.outcome:
            question = self.question
            return question if len(question) <= width else question[:width - 3] + "..."
        suffix = f" -> {self.outcome}"
        room = max(width - len(suffix), 12)
        question = self.question
        if len(question) > room:
            question = question[:room - 3] + "..."
        return question + suffix

    def effective_price(self, side):
        """The price to judge this outcome by: the real book when we have it."""
        if self.book is not None:
            return self.book.reference(side)
        return self.price

    @property
    def effective_tick(self):
        if self.book is not None and self.book.tick:
            return self.book.tick
        return self.tick_size

    def status(self, criteria):
        """Why this outcome is or isn't a valid setup to watch.

        Returns (ready, note). `ready` gates selection in interactive mode; the
        note is what gets shown in the results table.
        """
        if self.closed:
            return False, "closed"
        if not self.accepting_orders:
            return False, "not accepting"
        if not self.token_id:
            return False, "no token"
        tick = self.effective_tick
        if tick and float(tick) <= float(criteria.target_tick):
            return False, f"tick {tick} already"
        if not criteria.enforce_price:
            return True, "READY (no check)"
        price = self.effective_price(criteria.side)
        if price is None:
            return False, f"no {criteria.source_name}"
        if not price_matches(price, criteria.required_price, criteria.tolerance):
            return False, f"at {cents(price)}"
        return True, "READY"


def expand_market(market, event_title=""):
    """Turn one Gamma market into one Candidate per outcome."""
    token_ids = _decode_gamma_list(market.get("clobTokenIds"))
    outcomes = _decode_gamma_list(market.get("outcomes"))
    prices = _decode_gamma_list(market.get("outcomePrices"))

    candidates = []
    for index, token_id in enumerate(token_ids):
        candidate = Candidate(market, event_title)
        candidate.token_id = str(token_id)
        candidate.outcome_index = index
        candidate.outcome = (
            str(outcomes[index]) if index < len(outcomes) else str(index)
        )
        if index < len(prices):
            try:
                candidate.price = float(prices[index])
            except (TypeError, ValueError):
                candidate.price = None
        candidates.append(candidate)
    return candidates


def search_markets(query, timeout=20, limit_per_type=20):
    """Fuzzy-search open markets via Gamma, ranked best-first.

    `events_status=active` is essential: without it the search happily returns
    markets that resolved years ago, which are never valid targets here.
    """
    try:
        resp = httpx.get(
            f"{GAMMA_HOST}/public-search",
            params={
                "q": query,
                "limit_per_type": str(limit_per_type),
                "events_status": "active",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  Search failed: {e}")
        return []

    candidates = []
    for event in payload.get("events") or []:
        event_title = event.get("title") or ""
        for market in event.get("markets") or []:
            if market.get("closed"):
                continue
            for candidate in expand_market(market, event_title):
                candidate.score = max(
                    fuzzy_score(query, candidate.question),
                    fuzzy_score(query, event_title),
                    fuzzy_score(query, candidate.slug.replace("-", " ")),
                )
                candidates.append(candidate)
    return candidates


def rank_candidates(candidates, criteria):
    """Sort ready setups first, then by search relevance, then by volume."""

    def key(candidate):
        ready, _ = candidate.status(criteria)
        try:
            volume = float(candidate.volume or 0)
        except (TypeError, ValueError):
            volume = 0.0
        return (0 if ready else 1, -candidate.score, -volume)

    return sorted(candidates, key=key)


def print_results(candidates, criteria):
    """Render the numbered pick list."""
    header = "Bid" if criteria.side == "BUY" else "Ask"
    print(
        f"\n  #  {header:>7}  {'Tick':>6}  {'24h Vol':>8}  "
        f"{'Status':<18}  Market -> Outcome"
    )
    print("  " + "-" * 102)
    for i, candidate in enumerate(candidates, 1):
        _, note = candidate.status(criteria)
        price = candidate.effective_price(criteria.side)
        # '~' marks a Gamma mid price we could not confirm against the book.
        mark = " " if candidate.book is not None else "~"
        print(
            f"  {i:>2}  {mark}{cents(price):>6}  {candidate.effective_tick or '-':>6}  "
            f"{human_volume(candidate.volume):>8}  {note:<18}  {candidate.short_label()}"
        )
    print()


def run_search(query, criteria):
    """Search, price hits off the real book, and rank them.

    With the price guard on, only READY setups are listed, so every hit is
    priced: Gamma's mid can sit a cent away from the real bid, and filtering
    on it would hide genuine 99c markets. With --no-price-check everything is
    listed, and only the top hits by Gamma price are confirmed on the book.
    """
    print(f"  Searching for '{query}'...")
    found = search_markets(query)
    if not found:
        print("  No open markets matched. Try different words.")
        return []

    if criteria.enforce_price:
        enrich_with_books(found)
        ready = [c for c in found if c.status(criteria)[0]]
        ranked = rank_candidates(ready, criteria)[:RESULT_LIMIT]
        print(
            f"  {len(found)} outcome(s) matched; {len(ready)} at "
            f"{cents(criteria.required_price)} and not yet on a "
            f"{criteria.target_tick} tick."
        )
        if not ready:
            print("  Try different words, or re-run with --no-price-check to list them all.")
        return ranked

    # Rank on Gamma prices first so the book fetch only covers rows we show,
    # then re-rank once the authoritative prices are in.
    shortlist = rank_candidates(found, criteria)[: RESULT_LIMIT * 2]
    enrich_with_books(shortlist)
    ranked = rank_candidates(shortlist, criteria)[:RESULT_LIMIT]
    print(f"  {len(found)} outcome(s) matched; showing {len(ranked)}.")
    return ranked


def parse_picks(choice, count):
    """Parse '3' or '1, 4,7' into distinct 0-based row indexes.

    Returns None if any entry is not a row number, so a typo re-asks rather
    than silently adding the markets that did parse.
    """
    parts = [part for part in re.split(r"[,\s]+", choice) if part]
    if not parts or not all(p.isdigit() and 1 <= int(p) <= count for p in parts):
        return None
    return list(dict.fromkeys(int(part) - 1 for part in parts))


def select_candidates_interactive(args, criteria, query=None, batch=None):
    """Search-and-pick loop. Several rows can be picked at once, e.g. '1,3,5'.

    Returns a non-empty list of Candidates, or None when the user presses 'q'
    with markets already in `batch` - that ends adding rather than quitting,
    so the batch built so far is not thrown away. 'q' with an empty batch
    raises Aborted.
    """
    batch = batch or {}
    if batch:
        print(f"\n{len(batch)} market(s) in this batch so far.")
        quit_hint = "'q' to finish adding"
    else:
        print(
            f"\nLooking for a market on a {args.from_tick} tick with its "
            f"{criteria.source_name} at {cents(args.require_price)}, so a "
            f"${args.price} order becomes legal once the tick shrinks to "
            f"{args.target_tick}."
        )
        quit_hint = "'q' to quit"

    results = []
    while True:
        if not results:
            if query is None:
                query = ask(f"\nSearch markets (or {quit_hint})")
            if query.lower() in ("q", "quit", "exit"):
                if batch:
                    return None
                raise Aborted("no market selected")
            if not query:
                query = None
                continue

            results = run_search(query, criteria)
            query = None
            if not results:
                continue

        print_results(results, criteria)
        choice = ask(
            f"Pick a number, or several like 1,3 ('s' to search again, {quit_hint})"
        )

        if choice.lower() in ("q", "quit", "exit"):
            if batch:
                return None
            raise Aborted("no market selected")
        if choice.lower() in ("s", "search", ""):
            results = []
            continue
        picks = parse_picks(choice, len(results))
        if picks is None:
            print(
                f"  Enter numbers from 1 to {len(results)}, separated by commas."
            )
            continue

        chosen = []
        for candidate in (results[i] for i in picks):
            if candidate.token_id in batch:
                print(f"\n  '{candidate.label}' is already in this batch.")
                continue
            # Only reachable under --no-price-check; otherwise every row is READY.
            ready, note = candidate.status(criteria)
            if not ready:
                print(f"\n  '{candidate.label}' is not a valid setup: {note}.")
                if not ask_yes_no("  Watch it anyway?", default=False):
                    continue
            chosen.append(candidate)
        if chosen:
            return chosen


# ---------------------------------------------------------------------------
# Direct (non-interactive) market resolution
# ---------------------------------------------------------------------------

def _pick_outcome_index(outcomes, outcome):
    """Resolve an outcome selector (name or index) against a list of outcomes."""
    if outcome.isdigit():
        index = int(outcome)
        if index >= len(outcomes):
            raise ValueError(
                f"outcome index {index} out of range; market has "
                f"{len(outcomes)} outcome(s): {outcomes}"
            )
        return index

    wanted = outcome.strip().lower()
    for i, name in enumerate(outcomes):
        if str(name).strip().lower() == wanted:
            return i

    raise ValueError(f"outcome '{outcome}' not found; market outcomes are: {outcomes}")


def _candidate_from_gamma(market, outcome, event_title=""):
    """Choose one outcome of a Gamma market, by name or index."""
    candidates = expand_market(market, event_title)
    if not candidates:
        raise ValueError("market has no CLOB token ids")
    index = _pick_outcome_index([c.outcome for c in candidates], outcome)
    return candidates[index]


def resolve_by_slug(slug, outcome, timeout=20):
    resp = httpx.get(f"{GAMMA_HOST}/markets", params={"slug": slug}, timeout=timeout)
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        raise ValueError(f"no market found for slug '{slug}'")
    return _candidate_from_gamma(markets[0], outcome)


def resolve_by_condition(condition_id, outcome, timeout=20):
    resp = httpx.get(
        f"{GAMMA_HOST}/markets",
        params={"condition_ids": condition_id, "limit": "1"},
        timeout=timeout,
    )
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        raise ValueError(f"no market found for condition id '{condition_id}'")
    return _candidate_from_gamma(markets[0], outcome)


def resolve_by_token(token_id, timeout=20):
    """Build a Candidate from a bare token id.

    /markets-by-token returns only the condition id and the two token ids, so
    the question, outcomes, and order minimums have to come from a second
    lookup keyed on that condition id.
    """
    resp = httpx.get(f"{CLOB_HOST}/markets-by-token/{token_id}", timeout=timeout)
    resp.raise_for_status()
    condition_id = (resp.json() or {}).get("condition_id") or ""

    if condition_id:
        try:
            gamma = httpx.get(
                f"{GAMMA_HOST}/markets",
                params={"condition_ids": condition_id, "limit": "1"},
                timeout=timeout,
            )
            gamma.raise_for_status()
            markets = gamma.json()
            if markets:
                for candidate in expand_market(markets[0]):
                    if str(candidate.token_id) == str(token_id):
                        return candidate
        except Exception as e:
            log(f"Warning: could not resolve market details for token ({e})")

    # Fall back to a bare candidate; the book fetch still validates it.
    candidate = Candidate(
        {
            "question": f"Token {str(token_id)[:20]}...",
            "conditionId": condition_id,
            "acceptingOrders": True,
        }
    )
    candidate.token_id = str(token_id)
    return candidate


# ---------------------------------------------------------------------------
# Book reads / price checks
# ---------------------------------------------------------------------------

class Book:
    """Top of book plus the tick size, as of one observation."""

    __slots__ = ("tick", "bid", "ask", "at")

    def __init__(self, tick=None, bid=None, ask=None, at=None):
        self.tick = str(tick) if tick is not None else None
        self.bid = bid
        self.ask = ask
        self.at = at if at is not None else time.monotonic()

    def reference(self, side):
        """The price a resting order on `side` has to beat."""
        return self.bid if side == "BUY" else self.ask

    def age(self):
        return time.monotonic() - self.at


def _best(levels, pick):
    prices = []
    for level in levels or []:
        try:
            prices.append(float(level["price"]))
        except (KeyError, TypeError, ValueError):
            continue
    return pick(prices) if prices else None


def install_persistent_http_client():
    """Stop the SDK's pooled connection to the CLOB from expiring mid-watch.

    Every SDK request, the order POST included, goes through one module-level
    `httpx.Client`, and httpx reaps idle keep-alive connections after 5s. A
    watch that sits quiet for longer than that - which is every watch - makes
    the order POST reconnect from scratch: ~150ms of TCP plus ~150ms of TLS
    on top of the round trip, paid at the one moment latency matters.

    Returns the shared client so the poll loop can ride the same connection
    and keep it hot against upstream idle timeouts this cannot control.
    """
    clob_http._http_client = httpx.Client(
        http2=True,
        limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=None),
    )
    return clob_http._http_client


def _book_from(data):
    """Parse one /book or /books entry: the tick rides alongside the levels."""
    return Book(
        tick=data.get("tick_size"),
        bid=_best(data.get("bids"), max),
        ask=_best(data.get("asks"), min),
    )


def fetch_book(http, token_id, host=None, timeout=5):
    """Read one token's live book: tick size and top of book in one request."""
    resp = http.get(
        f"{host or CLOB_HOST}/book", params={"token_id": token_id}, timeout=timeout
    )
    resp.raise_for_status()
    return _book_from(resp.json())


def fetch_books(http, token_ids, host=None, timeout=5):
    """Read many tokens' live books in one request. Returns {token_id: Book}.

    /books carries `tick_size` alongside the levels, so one round trip covers
    both the tick and the price for a whole batch. Reading it directly also
    sidesteps `ClobClient.get_tick_size`, which memoizes per token and never
    expires - a watcher polling through the SDK would read its own stale
    startup value forever.

    A token the venue does not know is simply absent from the result. Raises
    on transport or HTTP errors; the caller decides whether that is fatal.
    """
    resp = http.post(
        f"{host or CLOB_HOST}/books",
        json=[{"token_id": t} for t in token_ids],
        timeout=timeout,
    )
    resp.raise_for_status()
    books = {}
    for entry in resp.json() or []:
        if not isinstance(entry, dict):
            continue
        asset_id = entry.get("asset_id") or entry.get("token_id")
        if asset_id:
            books[str(asset_id)] = _book_from(entry)
    return books


def fetch_books_bulk(token_ids, host=None, timeout=15):
    """Price the pick list off the real order book, never raising.

    /books rejects a request for more than BOOKS_REQUEST_LIMIT tokens, and a
    broad search returns several times that, so the list goes out in chunks
    fetched concurrently. Gamma's mid price is only a display fallback: a
    chunk that fails leaves its rows on Gamma prices instead.
    """
    unique = list(dict.fromkeys(t for t in token_ids if t))
    chunks = [
        unique[i:i + BOOKS_REQUEST_LIMIT]
        for i in range(0, len(unique), BOOKS_REQUEST_LIMIT)
    ]
    if not chunks:
        return {}

    def fetch(http, chunk):
        try:
            return fetch_books(http, chunk, host=host, timeout=timeout)
        except Exception as e:
            print(f"  Warning: could not price the book ({e}); showing Gamma prices")
            return {}

    books = {}
    with httpx.Client() as http, ThreadPoolExecutor(len(chunks)) as pool:
        for result in pool.map(lambda chunk: fetch(http, chunk), chunks):
            books.update(result)
    return books


def enrich_with_books(candidates, host=None):
    """Attach real top-of-book data to each candidate, in one request."""
    books = fetch_books_bulk([c.token_id for c in candidates], host=host)
    for candidate in candidates:
        book = books.get(str(candidate.token_id))
        if book is not None:
            candidate.book = book
    return candidates


def price_matches(price, required, tolerance):
    """True when `price` is within `tolerance` of `required`."""
    if price is None:
        return False
    try:
        return abs(float(price) - float(required)) <= float(tolerance)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def tick_reached(tick, target_tick):
    """True when `tick` is at or finer than the tick we're waiting for."""
    try:
        return float(tick) <= float(target_tick)
    except (TypeError, ValueError):
        return False


class TickDetector:
    """One token's detection state, shared by the WSS and polling threads.

    `trigger` is set exactly once; the first thread to see the target tick
    wins. The price observed *before* that moment is captured alongside it,
    which is what the entry-price guard checks.

    `stop` belongs to the whole batch, so a single `set()` on timeout or
    Ctrl+C tears down every thread of every market at once.
    """

    def __init__(self, target_tick, stop):
        self.target_tick = str(target_tick)
        self.trigger = threading.Event()
        self.stop = stop
        self._lock = threading.Lock()
        self.source = None
        self.observed = None
        self.previous = None
        self.detected_at = None
        self.last_tick = None
        # Most recent top-of-book seen by either thread, and a snapshot of it
        # taken at the instant the tick change fired.
        self.book = None
        self.book_at_trigger = None

    def record_price(self, bid, ask, tick=None):
        """Store the latest top of book.

        Callers must record a message's prices *after* passing its tick to
        `observe`, so a book update that carries the new tick cannot overwrite
        the pre-change price the guard needs to inspect.
        """
        if bid is None and ask is None:
            return
        with self._lock:
            self.book = Book(tick=tick, bid=bid, ask=ask)

    def observe(self, tick, source, previous=None):
        """Record a tick reading; fire the trigger if it's the one we want."""
        if tick is None:
            return False
        tick = str(tick)
        # A `book` message carries the new tick but no old one, so fall back to
        # the last tick we saw rather than reporting the change as 'None -> x'.
        prior = self.last_tick
        self.last_tick = tick

        if not tick_reached(tick, self.target_tick):
            return False

        with self._lock:
            if self.trigger.is_set():
                return False
            self.source = source
            self.observed = tick
            self.previous = previous or prior
            self.detected_at = time.time()
            self.book_at_trigger = self.book
            self.trigger.set()
        return True


class Result(Enum):
    """How one market's watch ended, and the exit status it contributes."""

    NOT_FIRED = ("no tick change", 0)
    PLACED = ("placed", 0)
    FAILED = ("FAILED", 1)
    REJECTED = ("rejected by the entry-price guard", 3)

    def __init__(self, label, status):
        self.label = label
        self.status = status


class Watch:
    """One market in a batch: what to fire when its tick shrinks, and what did.

    A batch is a dict of these keyed by token id. Both detection paths route
    every message through that dict, so a batch of one and a batch of ten run
    exactly the same code.
    """

    def __init__(self, candidate, size, book, detector):
        self.candidate = candidate
        self.size = size
        # The startup book that passed validation.
        self.book = book
        self.detector = detector
        self.presigned: Optional["PresignedOrder"] = None
        self.tag = candidate.short_label(30)
        self.result = Result.NOT_FIRED
        self.guard_reason = None

    @property
    def token_id(self):
        return self.candidate.token_id

    def order_label(self, args):
        return f"{args.side} {self.size:g} @ ${args.price}"


class Skipped(NamedTuple):
    """Why a market was left out of the batch, and the exit status it implies."""

    reason: str
    status: int


def _ws_payload(message):
    """Unwrap a market-channel message.

    The live server sends flat snake_case events (`event_type`, `asset_id`,
    `tick_size`); the docs describe a newer envelope of
    `{topic, type, payload}` with camelCase fields. Both are handled so a
    server-side format switch does not silently stop detection.
    """
    if "event_type" in message:
        return message.get("event_type"), message
    if "type" in message:
        payload = message.get("payload")
        return message.get("type"), payload if isinstance(payload, dict) else message
    return None, message


def _field(payload, *names):
    for name in names:
        if payload.get(name) is not None:
            return payload.get(name)
    return None


def _ws_book_updates(event_type, payload):
    """Yield (asset_id, tick, old_tick, bid, ask) for every asset a message touches.

    `book` and `tick_size_change` name one asset; a market's `price_change`
    covers both complementary tokens at once. Routing each entry by its own
    asset id matters: the other side of a market pinned at 99c sits at 1c,
    and letting it reach the wrong detector would fail the price guard on the
    wrong book. An entry with no asset id cannot be routed and is dropped.
    """
    asset = _field(payload, "asset_id", "tokenId")

    if event_type == "tick_size_change":
        if asset:
            yield (
                str(asset),
                _field(payload, "new_tick_size", "newTickSize"),
                _field(payload, "old_tick_size", "oldTickSize"),
                None,
                None,
            )

    elif event_type == "book":
        if asset:
            yield (
                str(asset),
                _field(payload, "tick_size", "tickSize"),
                None,
                _best(payload.get("bids"), max),
                _best(payload.get("asks"), min),
            )

    elif event_type == "price_change":
        for change in _field(payload, "price_changes", "priceChanges") or []:
            if not isinstance(change, dict):
                continue
            change_asset = _field(change, "asset_id", "tokenId") or asset
            if not change_asset:
                continue
            bid = _field(change, "best_bid", "bestBid")
            ask = _field(change, "best_ask", "bestAsk")
            yield (
                str(change_asset),
                None,
                None,
                float(bid) if bid is not None else None,
                float(ask) if ask is not None else None,
            )


def _iter_ws_messages(raw):
    """Yield dict messages from a frame that may hold one object or a list.

    Keepalive replies arrive as the bare string 'PONG', so non-JSON frames are
    skipped rather than treated as errors.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if isinstance(payload, dict):
        yield payload
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item


def _untriggered(watches):
    """The watches still waiting for their tick change."""
    return {
        token_id: watch
        for token_id, watch in watches.items()
        if not watch.detector.trigger.is_set()
    }


def ws_watch(watches, stop, ping_interval=10.0, reconnect_delay=2.0):
    """Subscribe once for the whole batch; route each update to its market.

    Runs until the batch stops or every market has seen its tick change.
    """
    try:
        from websockets.sync.client import connect
    except ImportError:
        log(
            "websockets not installed - WSS path disabled, running on the REST "
            "poll only. Install with: pip install websockets"
        )
        return

    subscribe = json.dumps({"assets_ids": list(watches), "type": "market"})

    def done():
        return stop.is_set() or not _untriggered(watches)

    while not done():
        try:
            with connect(WSS_MARKET_URL, open_timeout=10, close_timeout=1) as ws:
                ws.send(subscribe)
                log(f"WSS subscribed to market channel for {len(watches)} token(s)")
                last_ping = time.monotonic()

                while not done():
                    # The server drops idle connections; a periodic PING keeps
                    # the socket alive through quiet stretches.
                    if time.monotonic() - last_ping >= ping_interval:
                        ws.send("PING")
                        last_ping = time.monotonic()

                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue

                    for message in _iter_ws_messages(raw):
                        event_type, payload = _ws_payload(message)
                        for asset, tick, old_tick, bid, ask in _ws_book_updates(
                            event_type, payload
                        ):
                            watch = watches.get(asset)
                            if watch is None:
                                continue
                            if event_type == "tick_size_change" and tick:
                                log(
                                    f"[{watch.tag}] WSS tick_size_change: "
                                    f"{old_tick} -> {tick}"
                                )
                            # Tick first, price second: this message's own
                            # prices must not overwrite the pre-change book
                            # the guard inspects.
                            watch.detector.observe(tick, "wss", previous=old_tick)
                            watch.detector.record_price(bid, ask, tick=tick)
        except Exception as e:
            if done():
                return
            log(f"WSS error ({e}); reconnecting in {reconnect_delay}s")
            stop.wait(reconnect_delay)


def poll_watch(watches, stop, interval, host=None, log_every=60.0):
    """Poll /books for every untriggered market, tracking price as it goes.

    Runs alongside the WSS watcher rather than as a fallback: a socket that is
    connected but silently stale looks identical to a quiet market, and this is
    the only path that can tell the difference. It also guarantees the price
    the entry guard reads is never older than one poll interval.

    One request covers the whole batch, and it goes out on the same pooled
    connection as the orders, so the keep-alive is exercised every interval
    and is never idle long enough for the venue, a proxy or a VPN's NAT table
    to drop it out from under an order POST.
    """
    previous = {}
    last_status = 0.0
    http = clob_http._http_client
    while not stop.is_set():
        live = _untriggered(watches)
        if not live:
            return
        try:
            books = fetch_books(http, list(live), host=host)
        except Exception as e:
            log(f"Poll error ({e}); retrying")
            stop.wait(interval)
            continue

        for token_id, watch in live.items():
            book = books.get(token_id)
            if book is None:
                continue
            prior = previous.get(token_id)
            if prior is not None and book.tick != prior:
                log(f"[{watch.tag}] Poll saw tick change: {prior} -> {book.tick}")
            # Tick first, price second - see record_price.
            watch.detector.observe(book.tick, "poll", previous=prior)
            watch.detector.record_price(book.bid, book.ask, tick=book.tick)
            previous[token_id] = book.tick

        now = time.monotonic()
        if now - last_status >= log_every:
            for token_id, watch in _untriggered(live).items():
                book = books.get(token_id)
                if book is None:
                    log(f"[{watch.tag}] Still watching - no book in poll response")
                else:
                    log(
                        f"[{watch.tag}] Still watching - tick {book.tick}, "
                        f"bid {cents(book.bid)}, ask {cents(book.ask)}"
                    )
            last_status = now

        stop.wait(interval)


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def set_cached_tick_size(client, token_id, tick):
    """Overwrite the client's memoized tick size for this token.

    Needed because `create_order` validates the price against the cached tick
    and refuses an explicit `tick_size` option finer than what it has cached.
    Without this, the first order at $0.999 is rejected locally by the SDK
    using the stale 0.01 value it primed at startup.
    """
    client._ClobClient__tick_sizes[token_id] = str(tick)


def prewarm_client(client, token_ids):
    """Resolve everything create_order/post_order would fetch at fire time.

    Neg risk is per token; the exchange version is shared by every order.
    Order matters: neg-risk resolution can route through the market-info
    endpoint, which rewrites the cached tick size as a side effect, so it must
    happen before the caller pins the tick it wants.
    """
    for token_id in token_ids:
        try:
            neg_risk = client.get_neg_risk(token_id)
            log(f"Pre-warmed neg_risk={neg_risk} for token {token_id[:16]}...")
        except Exception as e:
            log(
                f"Warning: neg_risk pre-warm failed for token {token_id[:16]}... "
                f"({e}); will resolve at fire time"
            )

    try:
        version = client.get_version()
        client._ClobClient__cached_version = version
        log(f"Pre-warmed exchange version={version}")
    except Exception as e:
        log(f"Warning: version pre-warm failed ({e}); will resolve at fire time")


def check_entry_price(detector, args, side):
    """Verify the market was still at the required price when the tick changed.

    Deliberately reads the book captured *before* the change. The instant a
    tick opens up, competing orders reprice the book, so a fresh read here
    would reject a setup that was valid a millisecond earlier.

    Returns (ok, message).
    """
    if args.no_price_check:
        return True, "price check disabled"

    book = detector.book_at_trigger
    if book is None:
        return False, (
            "no pre-change price was observed, so the entry price could not be "
            "verified"
        )

    age = book.age()
    if age > args.price_max_age:
        return False, (
            f"last observed price is {age:.1f}s old (limit {args.price_max_age}s), "
            f"too stale to verify the entry price"
        )

    price = book.reference(side)
    source = "best bid" if side == "BUY" else "best ask"
    if price is None:
        return False, f"no {source} was observed before the tick change"

    if not price_matches(price, args.require_price, args.price_tolerance):
        return False, (
            f"{source} was {cents(price)}, not {cents(args.require_price)} "
            f"(+/- {args.price_tolerance})"
        )

    return True, f"{source} {cents(price)} confirmed {age * 1000:.0f}ms before change"


def order_type_for(args):
    """GTD once an expiry was asked for, GTC otherwise."""
    return OrderType.GTD if args.expiration_seconds > 0 else OrderType.GTC


def sign_order(client, args, token_id, tick, size):
    """Build and sign the configured order for `tick`. No network calls.

    Everything `create_order` would otherwise fetch is already cached by
    `prewarm_client`, so this is pure local signing - a few milliseconds.
    """
    set_cached_tick_size(client, token_id, tick)

    expiration = 0
    if args.expiration_seconds > 0:
        expiration = int(time.time()) + args.expiration_seconds

    order_args = OrderArgsV2(
        token_id=token_id,
        price=args.price,
        size=size,
        side=args.side,
        expiration=expiration,
    )
    return client.create_order(order_args)


class PresignedOrder:
    """A signed order kept fresh in the background so fire time is one POST.

    Signing is only a few milliseconds, but it is milliseconds spent after the
    tick change is already known. It cannot simply be done once at startup: a
    v2 signed order carries its signing timestamp inside the EIP-712 payload,
    so a payload held for the length of a watch risks server rejection - and a
    rejection costs a whole round trip, dwarfing what pre-signing saves.

    So `presign_watch` re-signs on a short interval and `take` hands back only
    a recent payload, leaving the caller to sign inline otherwise.
    """

    def __init__(self, max_age=2.0):
        self.max_age = max_age
        self._lock = threading.Lock()
        self._order = None
        self._tick = None
        self._signed_at = 0.0

    def put(self, order, tick):
        with self._lock:
            self._order = order
            self._tick = str(tick)
            self._signed_at = time.monotonic()

    def take(self, tick):
        """Return a usable signed order for `tick`, or None to sign inline.

        The tick has to match: maker/taker amounts are rounded per tick size,
        so an order pre-signed for 0.001 is simply the wrong order if the
        market jumped straight to something finer.
        """
        with self._lock:
            if self._order is None or self._tick != str(tick):
                return None
            if time.monotonic() - self._signed_at > self.max_age:
                return None
            return self._order


def presign_watch(watches, stop, client, args, tick, interval=0.5):
    """Keep every untriggered market's `presigned` stocked with a fresh order.

    Purely local work on a cached client, so this costs a few milliseconds of
    CPU per market per interval and touches the network never. One thread
    walks the whole batch rather than one per market, since every extra
    CPU-busy thread is another contender for the GIL a fire thread needs.

    A market drops out of the walk the moment its trigger fires, so signing
    cannot race its fire path for the client's tick cache; a payload built
    mid-race is rejected by `take` on the tick check. A market whose signing
    fails is left to sign at fire time, and the others carry on.
    """
    failed = set()
    while not stop.is_set():
        live = [
            watch
            for token_id, watch in _untriggered(watches).items()
            if token_id not in failed
        ]
        if not live:
            return
        for watch in live:
            if watch.detector.trigger.is_set():
                continue
            try:
                order = sign_order(client, args, watch.token_id, tick, watch.size)
                watch.presigned.put(order, tick)
            except Exception as e:
                log(f"[{watch.tag}] Pre-sign failed ({e}); will sign at fire time")
                failed.add(watch.token_id)
        stop.wait(interval)


def place_order(client, args, token_id, tick, size, signed_order=None):
    """Post the limit order, signing it first unless one was handed over."""
    order = signed_order if signed_order is not None else sign_order(
        client, args, token_id, tick, size
    )
    return client.post_order(
        order, order_type_for(args), post_only=args.post_only
    )


def fire_order(client, args, token_id, tick, size, market_name, presigned=None):
    """Place the order, retrying briefly while the venue catches up.

    The WSS event can land before the order endpoint accepts the finer price,
    so an early rejection is expected rather than fatal. Retries are bounded by
    wall clock so a genuinely bad order (no balance, wrong side) fails fast
    instead of hammering the API.

    Only the first attempt may use a pre-signed payload. Once the venue has
    rejected something, its age or its contents are both suspects, so every
    retry signs fresh.
    """
    order_type = order_type_for(args)

    label = (
        f"{args.side} {size:g} @ ${args.price} "
        f"({order_type}{', post-only' if args.post_only else ''})"
    )

    if args.dry_run:
        log(f"DRY RUN - would place {label} on {market_name}")
        return True

    deadline = time.monotonic() + args.retry_window
    attempt = 0
    signed_order = presigned.take(tick) if presigned else None
    while True:
        attempt += 1
        try:
            response = place_order(
                client, args, token_id, tick, size, signed_order=signed_order
            )
            order_id = response.get("id") if isinstance(response, dict) else None
            log(f"ORDER PLACED: {label} on {market_name} (ID: {order_id})")
            if isinstance(response, dict):
                log(f"Response: {json.dumps(response)}")
            return True
        except Exception as e:
            if is_geoblocked_error(e):
                log(
                    f"Order REJECTED after {attempt} attempt(s) - region "
                    f"blocked: {e}"
                )
                log(
                    "Not retrying: the submitting IP is blocked for new orders. "
                    "Check the VPN is still carrying this process."
                )
                return False
            if time.monotonic() >= deadline:
                log(f"Order FAILED after {attempt} attempt(s): {e}")
                return False
            log(f"Attempt {attempt} failed ({e}); retrying in {args.retry_delay}s")
            signed_order = None
            time.sleep(args.retry_delay)


def fire_watch(watch, client, args, session):
    """Wait for one market's tick change, then guard and fire its order.

    Returns without firing if the batch stops first. Once the trigger is seen,
    a later batch stop does not interrupt the fire: the main thread joins it,
    and fire_order's own retry window bounds how long that takes.
    """
    detector = watch.detector
    while not detector.trigger.wait(0.5):
        if detector.stop.is_set():
            return

    candidate = watch.candidate
    log(
        f"[{watch.tag}] TICK CHANGE DETECTED via {detector.source}: "
        f"{detector.previous or watch.book.tick} -> {detector.observed}"
    )

    ok, reason = check_entry_price(detector, args, args.side)
    watch.guard_reason = reason
    if not ok:
        log(f"[{watch.tag}] ORDER REJECTED by the entry-price guard: {reason}")
        watch.result = Result.REJECTED
        return

    log(f"[{watch.tag}] Entry-price guard passed: {reason}")
    placed = fire_order(
        client,
        args,
        watch.token_id,
        detector.observed,
        watch.size,
        candidate.question,
        presigned=watch.presigned,
    )
    watch.result = Result.PLACED if placed else Result.FAILED
    session.orders.append((candidate.label, watch.order_label(args), placed))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _tagged(flag):
    """argparse type that tags each value with the flag that named it.

    --token-id, --slug and --condition-id all append to one list, so a mixed
    command line keeps its order and resolves through one table.
    """
    return lambda value: (flag, value)


# How each market-naming flag resolves to a Candidate, given --outcome.
RESOLVERS = {
    "--token-id": lambda value, _outcome: resolve_by_token(value),
    "--slug": resolve_by_slug,
    "--condition-id": resolve_by_condition,
}


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Watch one or more markets' minimum tick size and place a limit "
            "order on each the moment its own tick shrinks (0.01 -> 0.001 by "
            "default). Run with no arguments to pick markets interactively."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive: search, pick, size, add more or confirm, watch - then back to
  # search for the next batch, until 'q'
  python tick_watch.py

  # Unattended: buy 100 shares at 99.9c when the tick opens up
  python tick_watch.py --slug will-x-happen --outcome YES --size 100 --yes

  # Two markets at once; each fires on its own tick change
  python tick_watch.py --slug will-x-happen --slug will-y-happen --size 100 --yes

  # Verify wiring without sending anything
  python tick_watch.py --slug will-x-happen --size 250 --dry-run
        """,
    )

    for flag, help_text in (
        ("--token-id", "CLOB token (asset) id to watch; repeatable"),
        ("--condition-id", "Market condition id (0x...); repeatable"),
        ("--slug", "Market slug; repeatable"),
    ):
        parser.add_argument(
            flag,
            dest="targets",
            action="append",
            type=_tagged(flag),
            metavar=flag.lstrip("-").replace("-", "_").upper(),
            help=help_text,
        )
    parser.add_argument(
        "--search",
        help=(
            "Search term; skips straight to the pick list (not combinable "
            "with --token-id/--condition-id/--slug)"
        ),
    )

    parser.add_argument(
        "--outcome",
        default="YES",
        help=(
            "Outcome name or index, applied to every --condition-id/--slug "
            "(default: YES)"
        ),
    )
    parser.add_argument(
        "--size",
        type=float,
        help=(
            "Order size in shares, per market (prompted for each market if "
            "omitted)"
        ),
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0.999,
        help="Limit price in dollars, i.e. 99.9c (default: 0.999)",
    )
    parser.add_argument(
        "--side",
        choices=["BUY", "SELL"],
        default="BUY",
        help="Order side (default: BUY)",
    )
    parser.add_argument(
        "--require-price",
        type=float,
        default=0.99,
        help=(
            "Price the market must be at before the tick change, or the order "
            "is rejected (default: 0.99)"
        ),
    )
    parser.add_argument(
        "--price-tolerance",
        type=float,
        default=0.005,
        help="Allowed deviation from --require-price (default: 0.005)",
    )
    parser.add_argument(
        "--price-max-age",
        type=float,
        default=5.0,
        help=(
            "Reject the order if the last price seen before the tick change is "
            "older than this many seconds (default: 5)"
        ),
    )
    parser.add_argument(
        "--no-price-check",
        action="store_true",
        help="Skip the entry-price guard entirely (not recommended)",
    )
    parser.add_argument(
        "--target-tick",
        default="0.001",
        choices=VALID_TICKS,
        help="Tick size to wait for (default: 0.001)",
    )
    parser.add_argument(
        "--from-tick",
        default="0.01",
        choices=VALID_TICKS,
        help="Tick size expected at startup (default: 0.01)",
    )
    parser.add_argument(
        "--fire-if-already",
        action="store_true",
        help=(
            "Place the order even if the market is already at the target tick "
            "when the watcher starts (default: refuse, since no change occurred)"
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Seconds between /book polls (default: 0.5)",
    )
    parser.add_argument(
        "--no-wss",
        action="store_true",
        help="Disable the WSS path and detect via polling only",
    )
    parser.add_argument(
        "--no-presign",
        action="store_true",
        help=(
            "Sign the order at fire time instead of keeping a freshly signed "
            "one ready (slower by a few ms; use if the venue ever rejects a "
            "pre-signed payload's timestamp)"
        ),
    )
    parser.add_argument(
        "--post-only",
        action="store_true",
        help="Reject the order rather than let it cross the spread",
    )
    parser.add_argument(
        "--expiration-seconds",
        type=int,
        default=0,
        help="Place a GTD order expiring this many seconds after the fire (default: GTC)",
    )
    parser.add_argument(
        "--retry-window",
        type=float,
        default=10.0,
        help=(
            "Seconds to keep retrying a rejected order, for when the tick "
            "change has not propagated to the order endpoint yet (default: 10)"
        ),
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=0.25,
        help="Seconds between order retries (default: 0.25)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help=(
            "Give up on markets still waiting after this many seconds, "
            "counted for the whole batch (default: 0, watch forever)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Watch and report, but never submit an order",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Exit after one batch instead of returning to the market picker "
            "(runs with --slug/--condition-id/--token-id always stop after one)"
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (for unattended runs)",
    )
    parser.add_argument(
        "--skip-region-check",
        action="store_true",
        help="Do not look up the egress region at startup",
    )
    parser.add_argument(
        "--allow-blocked-region",
        action="store_true",
        help="Arm the watch even when the region blocks new orders",
    )
    return parser


def validate_args(args):
    """Reject argument combinations that cannot produce a valid order."""
    if args.search and args.targets:
        return (
            "--search picks markets interactively, so it cannot be combined "
            "with --token-id/--condition-id/--slug"
        )

    if args.size is not None and args.size <= 0:
        return "--size must be greater than 0"

    tick = float(args.target_tick)
    from_tick = float(args.from_tick)

    if tick >= from_tick:
        return (
            f"--target-tick {args.target_tick} is not finer than --from-tick "
            f"{args.from_tick}; there is no tick shrink to wait for"
        )

    if not (tick <= args.price <= 1 - tick):
        return (
            f"--price {args.price} is outside the legal range for tick "
            f"{args.target_tick} ({tick} to {1 - tick})"
        )

    # A price legal under the *current* tick needs no tick change to be placed,
    # which means this watcher is the wrong tool for the job.
    if from_tick <= args.price <= 1 - from_tick:
        return (
            f"--price {args.price} is already legal at tick {args.from_tick}, so "
            f"no tick change is needed to place it. Use restore.py or place it "
            f"directly instead."
        )

    if not args.no_price_check:
        if not (from_tick <= args.require_price <= 1 - from_tick):
            return (
                f"--require-price {args.require_price} is not a legal price at "
                f"tick {args.from_tick} ({from_tick} to {1 - from_tick})"
            )
        if args.price_tolerance < 0:
            return "--price-tolerance cannot be negative"

    return None


def prepare_watch(args, criteria, session, candidate, stop):
    """Validate one market and size its order.

    Returns a Watch ready to arm, or Skipped with the reason and the status it
    implies; the full explanation is printed here either way. All of it runs
    before any credential load, so a rejected setup never triggers a vault
    unlock. Aborted from the size prompt propagates: whether that drops one
    market or the whole run is the caller's call.
    """
    if not candidate.token_id:
        print("Error: resolved market has no CLOB token id")
        return Skipped("no CLOB token id", 1)

    try:
        with httpx.Client() as http:
            book = fetch_book(http, candidate.token_id)
    except Exception as e:
        print(f"Error reading order book: {e}")
        return Skipped(f"order book unreadable ({e})", 1)

    print(f"\nSelected: {candidate.label}")
    log(f"Book: tick {book.tick}, bid {cents(book.bid)}, ask {cents(book.ask)}")

    if tick_reached(book.tick, args.target_tick):
        if not args.fire_if_already:
            print(
                f"\nMarket is already at tick {book.tick}; there is no change "
                f"to wait for. Re-run with --fire-if-already to order anyway."
            )
            return Skipped(f"already at tick {book.tick}", 0)
        log("Already at target tick and --fire-if-already set")
    elif book.tick != args.from_tick:
        log(
            f"Warning: market is at tick {book.tick}, not the expected "
            f"{args.from_tick}; still watching for {args.target_tick}"
        )

    # The entry-price guard, enforced up front so a market that is not at
    # 99c is rejected before anything else happens.
    entry_price = book.reference(args.side)
    source = criteria.source_name
    if criteria.enforce_price and not price_matches(
        entry_price, args.require_price, args.price_tolerance
    ):
        print(
            f"\nRejected: {source} is {cents(entry_price)}, not "
            f"{cents(args.require_price)} (+/- {args.price_tolerance}).\n"
            f"A ${args.price} order only makes sense on a market pinned at "
            f"{cents(args.require_price)}. Pick another market, adjust "
            f"--require-price, or pass --no-price-check to override."
        )
        return Skipped(
            f"{source} is {cents(entry_price)}, not {cents(args.require_price)}", 3
        )

    # Size: prompt when it was not given, offering the last answer as the
    # default, and check it against the market minimum either way.
    minimum = candidate.min_order_size or 0
    if session.fixed_size is None:
        if not sys.stdin.isatty():
            print("Error: --size is required when stdin is not a terminal")
            return Skipped("--size is required when stdin is not a terminal", 2)
        hint = f"Order size in shares (market minimum {minimum:g})"
        default = f"{session.last_size:g}" if session.last_size else None
        size = ask_float(hint, default=default, minimum=max(minimum, 0.000001))
    elif minimum and session.fixed_size < minimum:
        print(
            f"Error: --size {session.fixed_size:g} is below this market's "
            f"minimum of {minimum:g}"
        )
        return Skipped(
            f"--size {session.fixed_size:g} is below the minimum of {minimum:g}", 2
        )
    else:
        size = session.fixed_size
    session.last_size = size

    return Watch(candidate, size, book, TickDetector(args.target_tick, stop))


def build_batch_named(args, criteria, session, stop):
    """Resolve every market named on the command line, then validate each.

    A resolve failure is fatal before anything is watched: it is almost always
    a typo, and quietly watching the rest would hide it. A market that resolves
    but fails setup is skipped with its reason and the rest are still watched;
    the returned status records the skip so the run exits non-zero.

    Returns (watches, status).
    """
    candidates = []
    for flag, value in args.targets:
        try:
            candidates.append(RESOLVERS[flag](value, args.outcome))
        except Exception as e:
            print(f"Error resolving {flag} {value}: {e}")
            return {}, 1

    watches = {}
    skipped = []
    for candidate in candidates:
        if candidate.token_id and candidate.token_id in watches:
            print(f"\nNote: '{candidate.label}' is named twice; watching it once.")
            continue
        prepared = prepare_watch(args, criteria, session, candidate, stop)
        if isinstance(prepared, Skipped):
            skipped.append((candidate, prepared))
        else:
            watches[candidate.token_id] = prepared

    if skipped:
        print(f"\nSkipped {len(skipped)} market(s):")
        for candidate, skip in skipped:
            print(f"  {candidate.short_label()}: {skip.reason}")
    return watches, max((skip.status for _, skip in skipped), default=0)


def build_batch_interactive(args, criteria, session, stop, query=None):
    """Pick and size markets, one or several per pick, until the user stops.

    A market that fails setup, or whose size prompt is cancelled, is left out
    and the rest of its pick carries on; if a pick adds nothing, the picker
    comes back with the batch intact. Returns the batch, or {} when it was
    cancelled at the "add another" prompt. Aborted escapes only from the
    picker itself (the user is done) or when there is no terminal.
    """
    watches = {}
    while True:
        candidates = select_candidates_interactive(
            args, criteria, query, batch=watches
        )
        query = None
        if candidates is None:
            return watches

        added = 0
        for candidate in candidates:
            try:
                prepared = prepare_watch(args, criteria, session, candidate, stop)
            except Aborted as abort:
                if abort.no_input:
                    raise
                print(f"\n{abort.reason.capitalize()}; market not added.")
                continue
            if isinstance(prepared, Skipped):
                continue
            watches[candidate.token_id] = prepared
            added += 1
        if not added:
            continue

        try:
            if not ask_yes_no("\nAdd another market?", default=False):
                return watches
        except Aborted as abort:
            if abort.no_input:
                raise
            print(f"\n{abort.reason.capitalize()}; batch dropped.")
            return {}


def confirm_plan(args, watches):
    """Show the whole batch's plan and, unless --yes, ask once before watching."""
    rule = "=" * 62
    count = len(watches)
    print("\n" + rule)
    print(f"PLAN ({count} market{'' if count == 1 else 's'})")
    print(rule)
    for watch in watches.values():
        candidate, book = watch.candidate, watch.book
        print(f"  Market      : {candidate.question}")
        if candidate.event_title and candidate.event_title != candidate.question:
            print(f"  Event       : {candidate.event_title}")
        if candidate.outcome:
            print(f"  Outcome     : {candidate.outcome}")
        print(f"  Token id    : {candidate.token_id}")
        print(
            f"  Book now    : tick {book.tick}, bid {cents(book.bid)}, "
            f"ask {cents(book.ask)}"
        )
        print(f"  Wait for    : tick {book.tick} -> {args.target_tick}")
        print(
            f"  Then place  : {watch.order_label(args)} "
            f"(notional ${watch.size * args.price:,.2f})"
        )
        print("  " + "-" * 60)
    if args.no_price_check:
        print("  Price guard : DISABLED")
    else:
        print(
            f"  Price guard : {'best bid' if args.side == 'BUY' else 'best ask'} must "
            f"be {cents(args.require_price)} (+/- {args.price_tolerance}) at the change"
        )
    order_kind = "GTD" if args.expiration_seconds > 0 else "GTC"
    print(f"  Order type  : {order_kind}{', post-only' if args.post_only else ''}")
    total = sum(watch.size for watch in watches.values()) * args.price
    print(f"  Notional    : ${total:,.2f} if every market fires")
    if args.dry_run:
        print("  Mode        : DRY RUN (no order will be submitted)")
    print(rule)

    if args.yes or args.dry_run:
        return True
    return ask_yes_no("\nStart watching?", default=False)


class Session:
    """What carries over from one batch to the next.

    Credentials are loaded on the first confirmed batch and reused by every
    later one, so looping through markets never re-unlocks the vault.
    """

    def __init__(self, looping, fixed_size=None):
        self.looping = looping
        self.fixed_size = fixed_size
        self.last_size = None
        self.config = None
        self.client = None
        # (market label, order label, placed) for each order attempted.
        self.orders = []

    def connect(self):
        if self.client is None:
            self.config = load_config()
            # Must precede the first SDK call, so every connection the client
            # opens belongs to the pool that outlives the watch.
            install_persistent_http_client()
            try:
                self.client = create_client(self.config)
            except Exception as e:
                print(f"Error connecting to Polymarket: {e}")
                sys.exit(1)
            log("Connected to Polymarket CLOB")
        return self.client


def _wait_for(threads, deadline=None):
    """Join `threads`, returning False if `deadline` passes first.

    Joins in short slices: an untimed join can hold off Ctrl+C on Windows.
    """
    for thread in threads:
        while thread.is_alive():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            thread.join(0.25)
    return True


def watch_batch(args, session, watches, stop):
    """Arm every market and wait until each has fired or the batch ends.

    Each market fires on its own thread the moment its own tick shrinks; the
    rest keep watching. A timeout or Ctrl+C stops the markets still waiting,
    but never one whose order is already in flight. Returns the worst exit
    status among the batch's markets.
    """
    for watch in watches.values():
        # Seed the guard with the book we already validated, so a market that
        # goes quiet the moment watching starts is still verifiable.
        watch.detector.record_price(
            watch.book.bid, watch.book.ask, tick=watch.book.tick
        )

    client = session.connect()
    prewarm_client(client, list(watches))

    presign = not args.no_presign and not args.dry_run
    for watch in watches.values():
        # prepare_watch only lets a market already at the target through
        # under --fire-if-already.
        if tick_reached(watch.book.tick, args.target_tick):
            watch.detector.observe(watch.book.tick, "startup")
        if presign:
            watch.presigned = PresignedOrder()

    pending = _untriggered(watches)
    helpers = []
    if pending:
        if not args.no_wss:
            helpers.append(
                threading.Thread(target=ws_watch, args=(pending, stop), daemon=True)
            )
        helpers.append(
            threading.Thread(
                target=poll_watch,
                args=(pending, stop, args.poll_interval),
                kwargs={"host": session.config["host"]},
                daemon=True,
            )
        )
        if presign:
            helpers.append(
                threading.Thread(
                    target=presign_watch,
                    args=(pending, stop, client, args, args.target_tick),
                    daemon=True,
                )
            )
    # Not daemons: an order already in flight is finished even if the main
    # thread is on its way out.
    firers = [
        threading.Thread(target=fire_watch, args=(watch, client, args, session))
        for watch in watches.values()
    ]
    for thread in helpers + firers:
        thread.start()

    if pending:
        hint = (
            "Ctrl+C to drop this batch and search again"
            if session.looping
            else "Ctrl+C to stop"
        )
        log(f"Watching {len(pending)} market(s)... ({hint})")

    deadline = time.monotonic() + args.timeout if args.timeout > 0 else None
    interrupted = False
    try:
        finished = _wait_for(firers, deadline)
    except KeyboardInterrupt:
        finished, interrupted = False, True
    finally:
        stop.set()

    if interrupted:
        print("\nInterrupted; dropping the markets still waiting.")
    elif not finished:
        print(f"\nTimed out after {args.timeout}s.")
    _wait_for(firers)
    for thread in helpers:
        thread.join(timeout=2)

    print_batch_summary(args, watches)
    return max(watch.result.status for watch in watches.values())


def print_batch_summary(args, watches):
    """Report each market that fired, then list the ones that never did."""
    fired = [w for w in watches.values() if w.result is not Result.NOT_FIRED]
    waiting = [w for w in watches.values() if w.result is Result.NOT_FIRED]

    print("\n" + "=" * 62)
    print("SUMMARY")
    print("=" * 62)
    for watch in fired:
        candidate, detector = watch.candidate, watch.detector
        print(f"Market: {candidate.question}")
        if candidate.outcome:
            print(f"Outcome: {candidate.outcome}")
        print(f"Tick size: {detector.previous or watch.book.tick} -> {detector.observed}")
        print(f"Detected via: {detector.source}")
        print(f"Entry price: {watch.guard_reason}")
        print(f"Order: {watch.order_label(args)}")
        print(f"Result: {watch.result.label}")
        print()
    if waiting:
        print(f"No tick change ({len(waiting)}):")
        for watch in waiting:
            last = watch.detector.last_tick or watch.book.tick
            print(f"  {watch.candidate.short_label()} (last seen tick {last})")


def run_round(args, criteria, session, query=None):
    """Build one batch of markets, watch it, and fire at most one order each.

    Returns the status a one-shot run would exit with: 0 when every order was
    placed or the batch ended cleanly, otherwise the worst failure among its
    markets and any it had to skip. Aborted escapes only from the market
    picker, where it means the user is done altogether.
    """
    # Every thread of every market in this batch watches this one event.
    stop = threading.Event()
    status = 0
    try:
        if args.targets:
            watches, status = build_batch_named(args, criteria, session, stop)
        elif not sys.stdin.isatty():
            print(
                "Error: no market specified and stdin is not a terminal; pass "
                "--slug, --condition-id, or --token-id"
            )
            return 1
        else:
            watches = build_batch_interactive(args, criteria, session, stop, query)

        if not watches:
            return status

        try:
            if not confirm_plan(args, watches):
                print("Cancelled.")
                return status
        except Aborted as abort:
            print(f"\n{abort.reason.capitalize()}.")
            return 2 if abort.no_input else status

        return max(status, watch_batch(args, session, watches, stop))
    finally:
        stop.set()


def print_session_summary(session):
    """List every order the session attempted, once it ends."""
    if not session.orders:
        return
    placed = sum(1 for _, _, ok in session.orders if ok)
    print("\n" + "=" * 62)
    print(f"SESSION: {placed} of {len(session.orders)} order(s) placed")
    print("=" * 62)
    for market, order, ok in session.orders:
        print(f"  {'placed' if ok else 'FAILED':<7} {order}  {market}")


def main():
    _make_output_safe()
    # A fire thread woken by Event.set() still has to win the GIL from whichever
    # thread is CPU-busy. At the default 5ms switch interval that wake took
    # 31ms median (217ms max) on Windows/Python 3.11; at 0.5ms, 0.06ms median.
    sys.setswitchinterval(0.0005)
    args = build_parser().parse_args()

    print("Polymarket Tick Size Watcher")
    if args.dry_run:
        print("(DRY RUN MODE - no orders will be submitted)")
    print("=" * 62)

    error = validate_args(args)
    if error:
        print(f"Error: {error}")
        sys.exit(2)

    # Before any credential load: a blocked IP makes the whole run pointless,
    # and there is no reason to unlock a vault to discover that.
    if not args.skip_region_check and not preflight_region():
        if args.allow_blocked_region:
            log("Continuing anyway (--allow-blocked-region)")
        elif args.dry_run:
            log("Continuing anyway (dry run submits nothing)")
        else:
            print(
                "\nAn order fired from here would be rejected on submission, so "
                "the watch would burn the tick change it is waiting for.\n"
                "  - Confirm the VPN is carrying THIS process: per-app split "
                "tunnelling matches the interpreter's exact path, so a venv or "
                "a second install needs its own rule.\n"
                "  - Exits whose API access is unrestricted: Ireland, Japan, "
                "Malta, Netherlands.\n"
                "  - Full list: "
                "https://docs.polymarket.com/developers/CLOB/geoblock\n"
                "Re-run with --allow-blocked-region to override this check."
            )
            sys.exit(2)

    # Markets named on the command line are one specific batch; once their
    # ticks have changed there is nothing left to wait for, so only the picker
    # loops.
    session = Session(
        looping=not (args.targets or args.once), fixed_size=args.size
    )
    criteria = Criteria(args)
    query = args.search
    status = 0

    try:
        while True:
            try:
                status = run_round(args, criteria, session, query)
            except Aborted as abort:
                print(f"\n{abort.reason.capitalize()}.")
                if abort.no_input:
                    print(
                        "Pass --slug, --condition-id, or --token-id (plus --size) "
                        "to run without prompts."
                    )
                    status = 2
                else:
                    status = 0
                break
            except KeyboardInterrupt:
                if not session.looping:
                    print("\nInterrupted; shutting down.")
                    status = 0
                    break
                print("\nCancelled.")

            # --search only seeds the first batch.
            query = None
            if not session.looping:
                break
            print("\n" + "=" * 62)
            print("Back to market search ('q' at the prompt to quit).")
    finally:
        if session.looping:
            print_session_summary(session)
        # Only meaningful once credentials were actually loaded.
        if session.client is not None and cleanup_key_source():
            print("Key source secured.")

    # A looping session only ends at the picker, so a failure in an earlier
    # batch never becomes the exit status - it was reported when it happened.
    if status:
        sys.exit(status)


if __name__ == "__main__":
    main()

