"""The sniper's picker: which outcomes of a market search are a setup worth
watching, and in what order to show them. Pure: the page fetches the search
and the books and hands them here."""

import difflib
import re
from typing import NamedTuple, Optional

from polymarket import books
from polymarket.orders import Side

RESULT_LIMIT = 25


class Candidate(NamedTuple):
    """One outcome of one market the search found. score is its relevance to
    the query, from 0 to 1."""

    market: object
    outcome: str
    token_id: str
    score: float

    @property
    def label(self):
        return f"{self.market.question} -> {self.outcome}"


class Verdict(NamedTuple):
    """Whether an outcome is a setup to watch, and the note the page shows."""

    ready: bool
    note: str


class Ranked(NamedTuple):
    candidate: Candidate
    book: Optional[books.Levels]
    verdict: Verdict


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def score(query, text):
    """How well text matches the query, from 0 to 1. Gamma's search decides
    which markets come back; this only reranks them, so a blend of a substring
    hit, sequence similarity and word overlap is enough. Word overlap is what
    makes an out-of-order query match."""
    query, text = (query or "").strip().lower(), (text or "").lower()
    if not query or not text:
        return 0.0
    if query in text:
        # The shortest title holding the whole query comes first.
        similarity = 0.9 + 0.1 * (len(query) / len(text))
    else:
        similarity = difflib.SequenceMatcher(None, query, text).ratio()
    words = _words(query)
    overlap = len(words & _words(text)) / len(words) if words else 0.0
    return max(similarity, 0.5 * similarity + 0.5 * overlap)


def candidates(query, markets):
    """One Candidate for each outcome that has a token, scored by the best of
    the question, the event title and the slug."""
    found = []
    for market in markets:
        best = max(score(query, market.question), score(query, market.event_title),
                   score(query, market.slug.replace("-", " ")))
        for outcome, token_id in zip(market.outcomes, market.token_ids):
            if token_id:
                found.append(Candidate(market, outcome, token_id, best))
    return found


def status(candidate, book, side, guard, target_tick):
    """Whether the outcome is a setup for a watch on this side with this
    guard: open, accepting orders, still on a tick coarser than the target,
    and quoted near the guard's price on the guard's side. The book's tick
    counts over Gamma's, which lags."""
    market = candidate.market
    if market.closed:
        return Verdict(False, "closed")
    if not market.accepting_orders:
        return Verdict(False, "not accepting orders")
    tick = book.tick if book is not None and book.tick is not None else market.tick
    if tick is not None and tick <= float(target_tick):
        return Verdict(False, f"tick {tick:g} already")
    if guard is None:
        return Verdict(True, "ready, no guard")
    if book is None:
        return Verdict(False, "no book")
    name = "bid" if side is Side.BUY else "ask"
    price = books.best_bid(book) if side is Side.BUY else books.best_ask(book)
    if price is None:
        return Verdict(False, f"no {name}")
    if abs(price - guard.price) > guard.tolerance + 1e-9:
        return Verdict(False, f"{name} at {price * 100:.1f}c")
    return Verdict(True, "ready")


def rank(found, book_of, side, guard, target_tick, limit=RESULT_LIMIT):
    """The candidates as Ranked rows: ready first, then by relevance, then by
    volume. book_of maps a token to its Levels; a token it lacks has none."""
    ranked = [Ranked(c, book_of.get(c.token_id), status(c, book_of.get(c.token_id), side, guard,
                                                          target_tick)) for c in found]
    ranked.sort(key=lambda r: (not r.verdict.ready, -r.candidate.score, -r.candidate.market.volume))
    return ranked[:limit]
