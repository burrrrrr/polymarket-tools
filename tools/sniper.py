"""The sniper: fires each armed watch once, when its token's tick reaches the
watch's target, by posting the watch's order from the thread that saw the tick.

tools.db is the whole interface. The console inserts armed rows and sets
disarm_requested_at; the Sniper writes state and everything after it. Four
lanes, each owning one kind of blocking, and nothing blocks on another lane:

  control  the thread that calls run(). The only SQLite in the sniper: rows
           in, events out, once every CONTROL_TICK_S.
  feed     the market websocket. Fires the first attempt inline.
  poll     /books every POLL_S, which also keeps the order connection warm.
           Fires the first attempt inline.
  slow     adoption (the baseline book, the prewarm, the presign), re-signing,
           and every attempt after the first.

Every shared structure has one writing lane. The hot path, from the tick to
the POST, runs on the lane that saw the tick with no SQLite, no blocking lock,
no wall clock and no thread woken, so nothing can take the interpreter lock
from it before the bytes leave.
"""

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import NamedTuple, Optional, Union

from polymarket import books, orders, ticks
from polymarket.feed import MarketFeed
from polymarket.orders import Posted, Rejected, Unanswered, post, text
from tools import db
from tools.db import Guard, Reason, Sample, Settlement, WatchRow, WatchState

# Process-wide, so the entry point sets it and this module only names the number. The
# feed thread wakes from its socket through the interpreter lock, and a restore signing
# orders on the job thread holds it for the whole interval. Measured on 2026-10-11 with a
# CPU-bound thread standing in for that restore: a parked thread woke after a median
# 15.5 ms and a worst 110 ms at the default 5 ms, against 0.08 ms and 0.35 ms at 0.5 ms.
# The leg from the callback to the POST needs no hand-off and measured 0.03 ms either way.
SWITCH_INTERVAL_S = 0.0005
CONTROL_TICK_S = 0.05
RECONCILE_S = 0.25
PUBLISH_S = 1.0
POLL_S = 0.5
SLOW_TICK_S = 0.05
PRESIGN_S = 0.5
PRESIGN_MAX_AGE_S = 2.0   # a v2 signed order embeds its signing time
RETRY_WINDOW_S = 10.0     # the tick event can arrive before the order endpoint takes the price
RETRY_DELAY_S = 0.25
BOOK_TIMEOUT_S = 5.0
JOIN_RETRY_S = 2.0


class Source(Enum):
    FEED = "feed"
    POLL = "poll"
    BASELINE = "baseline"


class Quote(NamedTuple):
    """Top of book from one observation whose tick was coarser than the target."""

    bid: Optional[float]
    ask: Optional[float]
    tick: Optional[str]
    source: Source
    at: float


class Trigger(NamedTuple):
    """The edge: a routed watch, whose baseline tick was coarse, saw a tick at the target."""

    at: float
    source: Source
    tick: str
    quote: Optional[Quote]


class Presigned(NamedTuple):
    order: object
    tick: str
    at: float


class Rehearsed(NamedTuple):
    """A dry watch reached the point of the POST. signed is False on a keyless worker."""

    signed: bool


class Unsigned(NamedTuple):
    error: str


Attempt = Union[Posted, Rejected, Unanswered, Rehearsed, Unsigned]


class Missed(NamedTuple):
    tick: str


class GuardRefused(NamedTuple):
    reason: Reason
    detail: str


class CutBy(Enum):
    DISARM = "disarm"
    STOP = "stop"


class Cut(NamedTuple):
    """The retry window was cut short. Every attempt so far was a definite no, so no order rests."""

    by: CutBy
    last_reason: str


class Disarmed(NamedTuple):
    """A disarm request won the claim, so nothing was sent. The control lane
    closes it directly, off the outbox."""


class Interrupted(NamedTuple):
    """Found firing when the worker started, so the order may rest. The
    control lane closes it at open(), off the outbox."""


Outcome = Union[Missed, GuardRefused, Posted, Rehearsed, Rejected, Unanswered, Unsigned, Cut,
                Disarmed, Interrupted]


class Prepared(NamedTuple):
    """slow -> control: baseline coarse, token prewarmed, first order presigned. Route it."""

    watch: "Watch"


class Claimed(NamedTuple):
    """hot -> control: the guard passed and a POST is leaving. Write `firing`."""

    watch_id: int
    trigger: Trigger


class Resolved(NamedTuple):
    """any lane -> control: close the row."""

    watch_id: int
    outcome: Outcome
    trigger: Optional[Trigger] = None
    attempts: int = 0
    send_ms: Optional[float] = None
    post_ms: Optional[float] = None


class Note(NamedTuple):
    """any lane -> control: a log line that is not an outcome. watch_id None
    is a line for every routed watch."""

    at: float
    watch_id: Optional[int]
    message: str


Event = Union[Prepared, Claimed, Resolved, Note]


def utc_now():
    return datetime.now(timezone.utc)


def cents(price):
    return "--" if price is None else f"{price * 100:.1f}c"


def reached(tick, target):
    return tick <= target


def newest(*quotes):
    seen = [quote for quote in quotes if quote is not None]
    return max(seen, key=lambda quote: quote.at) if seen else None


def last_tick(*quotes):
    ticked = [quote for quote in quotes if quote is not None and quote.tick is not None]
    return max(ticked, key=lambda quote: quote.at).tick if ticked else None


def fresh(presigned, tick, now):
    if presigned is None or presigned.tick != tick or now - presigned.at > PRESIGN_MAX_AGE_S:
        return None
    return presigned.order


def judge(guard, side, quote, now):
    """The guard, pure: None lets the order go, a GuardRefused says why not."""
    if guard is None:
        return None
    side_name = "bid" if side is orders.Side.BUY else "ask"
    if quote is None:
        return GuardRefused(Reason.GUARD_NO_QUOTE, "no quote was seen before the tick change")
    age = now - quote.at
    if age > guard.max_age_s:
        return GuardRefused(Reason.GUARD_STALE, f"the last quote was {age:.1f}s old, older than "
                                                f"the guard's {guard.max_age_s:g}s")
    reference = quote.bid if side is orders.Side.BUY else quote.ask
    if reference is None:
        return GuardRefused(Reason.GUARD_NO_QUOTE,
                            f"no {side_name} was seen before the tick change")
    if not guard.admits(reference):
        return GuardRefused(Reason.GUARD_PRICE, f"the {side_name} was {cents(reference)}, not "
                                                f"{cents(guard.price)} within "
                                                f"{cents(guard.tolerance)}")
    return None


def retryable(attempt):
    """Whether another POST may change this answer: a plain refusal, and nothing else."""
    return isinstance(attempt, Rejected) and not orders.terminal(attempt)


def settle(outcome, attempts):
    if isinstance(outcome, Missed):
        return Settlement(WatchState.MISSED, Reason.AT_TARGET, outcome.tick, attempts=attempts)
    if isinstance(outcome, GuardRefused):
        return Settlement(WatchState.REJECTED, outcome.reason, outcome.detail, attempts=attempts)
    if isinstance(outcome, Posted):
        return Settlement(WatchState.FIRED, None, "", outcome.order_id, attempts)
    if isinstance(outcome, Rehearsed):
        return Settlement(WatchState.REHEARSED, None,
                          "signed, not sent" if outcome.signed else "not signed (no key), not sent",
                          attempts=attempts)
    if isinstance(outcome, Rejected):
        return Settlement(WatchState.FAILED, Reason.VENUE, outcome.reason, attempts=attempts)
    if isinstance(outcome, Unanswered):
        return Settlement(WatchState.FAILED, Reason.UNANSWERED, outcome.detail, attempts=attempts)
    if isinstance(outcome, Unsigned):
        return Settlement(WatchState.FAILED, Reason.SIGN, outcome.error, attempts=attempts)
    if isinstance(outcome, Cut):
        if outcome.by is CutBy.DISARM:
            return Settlement(WatchState.DISARMED, None, outcome.last_reason, attempts=attempts)
        return Settlement(WatchState.FAILED, Reason.STOPPED, outcome.last_reason,
                          attempts=attempts)
    if isinstance(outcome, Disarmed):
        return Settlement(WatchState.DISARMED, None, "", attempts=attempts)
    if isinstance(outcome, Interrupted):
        return Settlement(WatchState.FAILED, Reason.INTERRUPTED, "", attempts=attempts)
    raise TypeError(f"not an Outcome: {outcome!r}")


def describe(event, watch):
    """The log lines of an event. Lanes record facts; the control lane words them."""
    if isinstance(event, Note):
        return [event.message]
    if isinstance(event, Prepared):
        base = watch.base_quote
        signed = (f"presigned for {watch.target_text}" if watch.presigned is not None
                  else f"not presigned ({watch.presign_error or 'no key'})")
        return [f"adopted at tick {base.tick} (bid {cents(base.bid)}, ask {cents(base.ask)}), "
                f"{signed}"]
    trigger = event.trigger
    if isinstance(event, Claimed):
        quote = trigger.quote
        if watch.guard is None:
            passed = "no guard"
        else:
            side = "bid" if watch.order.side is orders.Side.BUY else "ask"
            price = quote.bid if side == "bid" else quote.ask
            passed = (f"{side} {cents(price)} from the {quote.source.value} "
                      f"{trigger.at - quote.at:.1f} s before")
        return [f"tick {trigger.tick} seen by the {trigger.source.value}; guard passed: {passed}; "
                "posting"]
    outcome, n = event.outcome, event.attempts
    if isinstance(outcome, Missed):
        return [f"missed: the book already showed tick {outcome.tick} at adoption"]
    if isinstance(outcome, GuardRefused):
        return [f"tick {trigger.tick} seen by the {trigger.source.value}; rejected by the guard: "
                f"{outcome.detail}; nothing sent"]
    if isinstance(outcome, Posted):
        return [f"fired: order {outcome.order_id} accepted on attempt {n}"]
    if isinstance(outcome, Rehearsed):
        return [f"rehearsed: a dry run reached the post on attempt {n}, "
                f"{'signed' if outcome.signed else 'not signed'}"]
    if isinstance(outcome, Rejected):
        why = ("no retry can change it" if orders.terminal(outcome)
               else "the retry window closed")
        return [f"attempt {n} refused: {outcome.reason}; giving up, {why}"]
    if isinstance(outcome, Unanswered):
        return [f"attempt {n} got no answer ({outcome.detail}); not retried: the order may rest"]
    if isinstance(outcome, Unsigned):
        return [f"could not sign the order: {outcome.error}"]
    if outcome.by is CutBy.DISARM:
        return [f"disarmed inside the retry window after {n} refusals (last: "
                f"{outcome.last_reason})"]
    return [f"the worker stopped inside the retry window after {n} refusals (last: "
            f"{outcome.last_reason})"]


class LivePoster:
    def __init__(self, client, signer, post):
        self._client, self._signer, self._post = client, signer, post

    def prewarm(self, token_id):
        self._signer.prewarm(token_id)

    def presign(self, order, tick):
        return self._signer.sign(order, tick)

    def attempt(self, order, tick, presigned):
        try:
            signed = presigned if presigned is not None else self._signer.sign(order, tick)
        except Exception as e:
            return Unsigned(f"{type(e).__name__}: {e}")
        return self._post(self._client, signed, order.order_type)


class DryPoster:
    """Signs when it has a Signer, so a rehearsal on a keyed worker costs what a
    fire costs. It holds no client and no post function: it cannot send."""

    def __init__(self, signer):
        self._signer = signer

    def prewarm(self, token_id):
        if self._signer is not None:
            self._signer.prewarm(token_id)

    def presign(self, order, tick):
        return self._signer.sign(order, tick) if self._signer is not None else None

    def attempt(self, order, tick, presigned):
        if self._signer is None:
            return Rehearsed(signed=False)
        try:
            if presigned is None:
                self._signer.sign(order, tick)
        except Exception as e:
            return Unsigned(f"{type(e).__name__}: {e}")
        return Rehearsed(signed=True)


class Watch:
    """One adopted row in memory. Each mutable field is in the tuple of the one
    lane that writes it; every other lane only reads it."""

    CONTROL_WRITES = ("abort",)
    SLOW_WRITES = ("not_before", "base_quote", "presigned", "presign_error")
    FEED_WRITES = ("feed_quote",)
    POLL_WRITES = ("poll_quote",)
    __slots__ = ("id", "token_id", "order", "target", "target_text", "guard", "dry_run", "poster",
                 "_claim", *CONTROL_WRITES, *SLOW_WRITES, *FEED_WRITES, *POLL_WRITES)

    def __init__(self, row, poster):
        self.id = row.id
        self.token_id = row.order.token_id
        self.order = row.order
        self.target_text = row.target_tick
        self.target = float(row.target_tick)
        self.guard = row.guard
        self.dry_run = row.dry_run
        self.poster = poster
        self._claim = threading.Lock()
        self.abort = False
        self.not_before = 0.0
        self.base_quote = None
        self.feed_quote = None
        self.poll_quote = None
        self.presigned = None
        self.presign_error = None

    def claim(self):
        """True for exactly one caller, ever. The winner decides the watch's end: a hot
        lane fires it, the control lane disarms it or leaves it armed for the next start.
        The lock is never released, so nobody ever waits on it."""
        return self._claim.acquire(blocking=False)

    @property
    def claimed(self):
        return self._claim.locked()


class Fire:
    """A claimed watch between its first attempt and its outcome."""

    __slots__ = ("watch", "trigger", "send_ms", "attempts", "last", "deadline", "next_at")

    def __init__(self, watch, trigger, send_ms):
        self.watch, self.trigger, self.send_ms = watch, trigger, send_ms
        self.attempts = 0
        self.last = None
        self.deadline = trigger.at + RETRY_WINDOW_S
        self.next_at = trigger.at


class Sniper:
    """See the module docstring. A keyless Sniper (client None) adopts only dry
    rows, so a live row waits for a keyed worker.

    signer defaults to ticks.Signer(client, clock). feed builds the market
    feed as feed(token_ids, on_update, on_error=...). clock gives the aware
    UTC time the rows record; monotonic the seconds the lanes measure with.
    """

    def __init__(self, db_path, http, stop, client=None, *, signer=None, post=post,
                 feed=MarketFeed, clock=utc_now, monotonic=time.monotonic):
        self._db_path = db_path
        self._http = http
        self._stop = stop
        self._client = client
        self._post = post
        self._clock = clock
        self._monotonic = monotonic
        if signer is None and client is not None:
            signer = ticks.Signer(client, clock)
        self._signer = signer
        self._feed = feed([], self.on_update, on_error=self._feed_error)
        self._conn = None
        self._failure = None
        self._adopted: dict[int, Watch] = {}
        self._armed: dict[str, Watch] = {}
        self._control_inbox: deque[Event] = deque()
        self._slow_adoptions: deque[Watch] = deque()
        self._slow_retries: deque[Fire] = deque()
        self._slow_fires: list[Fire] = []
        self._reconciled = self._published = float("-inf")

    def run(self):
        """Blocks until stop is set. An exception in any lane sets stop and is
        raised here, so a broken sniper takes the worker down."""
        self.open()
        threads = [
            threading.Thread(target=self._feed.run, args=(self._stop,), name="sniper-feed",
                             daemon=True),
            threading.Thread(target=self._lane, args=(self.poll_once, POLL_S), name="sniper-poll",
                             daemon=True),
            threading.Thread(target=self._lane, args=(self.work_once, SLOW_TICK_S),
                             name="sniper-slow", daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            while not self._stop.wait(CONTROL_TICK_S):
                self.tick()
                if self._failure is not None:
                    raise self._failure
        finally:
            self._stop.set()
            self.close(threads)
            if self._failure is not None:
                raise self._failure

    def _lane(self, step, period):
        try:
            while not self._stop.is_set():
                step()
                self._stop.wait(period)
        except Exception as e:
            self._failure = e
            self._stop.set()

    def open(self):
        """Control. Open the connection and close every `firing` row a dead
        worker left as failed, interrupted."""
        self._conn = db.open_db(self._db_path)
        db.close_firing(self._conn, settle(Interrupted(), None), db.INTERRUPTED_LINE,
                        self._clock())

    def tick(self):
        """Control. Drain the outbox, and when due reconcile with the rows and
        publish the quotes."""
        self._drain()
        now = self._monotonic()
        if now - self._reconciled >= RECONCILE_S:
            self._reconciled = now
            self._reconcile()
        if now - self._published >= PUBLISH_S:
            self._published = now
            self._publish(now)

    def on_update(self, update):
        """Feed. One polymarket.feed.Update."""
        watch = self._armed.get(update.token_id)
        if watch is not None:
            self._observe(watch, update.tick, update.bid, update.ask, Source.FEED)

    def poll_once(self):
        """Poll. One /books request for every routed token. A failed request
        is a Note, and the next poll is the retry."""
        tokens = list(self._armed)
        if not tokens:
            return
        try:
            found = books.fetch_books(self._http, tokens, timeout=BOOK_TIMEOUT_S)
        except Exception as e:
            self._control_inbox.append(Note(self._monotonic(), None, f"poll failed: {e}"))
            return
        for token_id, levels in found.items():
            watch = self._armed.get(token_id)
            if watch is not None:
                self._observe(watch, levels.tick, books.best_bid(levels), books.best_ask(levels),
                              Source.POLL)

    def work_once(self):
        """Slow. In this order, so nothing delays a retry that is due: the
        retries, then one adoption, then the presigns that aged."""
        now = self._monotonic()
        while self._slow_retries:
            self._slow_fires.append(self._slow_retries.popleft())
        for fire in list(self._slow_fires):
            if now >= fire.next_at:
                over = self._retry(fire, now)
                if over:
                    self._slow_fires.remove(fire)
        if self._stop.is_set():
            return
        if not self._slow_fires and self._slow_adoptions:
            watch = self._slow_adoptions.popleft()
            if now < watch.not_before:
                self._slow_adoptions.append(watch)
            else:
                self._prepare(watch, now)
        for watch in list(self._armed.values()):
            if not watch.claimed and (watch.presigned is None
                                      or now - watch.presigned.at >= PRESIGN_S):
                self._presign(watch, now)

    def close(self, threads=()):
        """Control, after stop is set. Claims every adopted watch, so no lane
        fires one from here on: a won claim writes nothing and the row stays
        armed for the next start. Joins the lanes, cuts the open fires and
        drains the outbox one last time."""
        if self._conn is None:
            return
        for watch in list(self._adopted.values()):
            watch.claim()
        for thread in threads:
            thread.join()
        self.work_once()
        self._drain()
        self._conn.close()
        self._conn = None

    def _observe(self, watch, tick, bid, ask, source):
        if tick is not None and reached(tick, watch.target):
            self._trigger(watch, tick, source)
        elif bid is not None or ask is not None:
            quote = Quote(bid, ask, text(tick) if tick is not None else None, source,
                          self._monotonic())
            if source is Source.FEED:
                watch.feed_quote = quote
            else:
                watch.poll_quote = quote

    def _trigger(self, watch, tick, source):
        """From the tick to the POST, on the lane that saw the tick: no SQLite,
        no blocking lock, no wall clock, no formatting, no thread woken."""
        if self._stop.is_set() or not watch.claim():
            return
        now = self._monotonic()
        trigger = Trigger(now, source, text(tick),
                          newest(watch.base_quote, watch.feed_quote, watch.poll_quote))
        refused = judge(watch.guard, watch.order.side, trigger.quote, now)
        if refused is not None:
            self._control_inbox.append(Resolved(watch.id, refused, trigger))
            return
        self._control_inbox.append(Claimed(watch.id, trigger))
        sent = self._monotonic()
        attempt = watch.poster.attempt(watch.order, trigger.tick,
                                       fresh(watch.presigned, trigger.tick, now))
        answered = self._monotonic()
        fire = Fire(watch, trigger, (sent - now) * 1000)
        over = self._answer(fire, attempt, (answered - sent) * 1000, answered)
        if not over:
            self._slow_retries.append(fire)

    def _answer(self, fire, attempt, post_ms, now):
        fire.attempts += 1
        if retryable(attempt):
            fire.last = attempt
            fire.next_at = now + RETRY_DELAY_S
            self._control_inbox.append(Note(now, fire.watch.id,
                                     f"attempt {fire.attempts} refused: {attempt.reason}; retrying"))
            return False
        self._control_inbox.append(Resolved(fire.watch.id, attempt, fire.trigger, fire.attempts,
                                     fire.send_ms, post_ms))
        return True

    def _retry(self, fire, now):
        watch = fire.watch
        if watch.abort or self._stop.is_set():
            cut = Cut(CutBy.DISARM if watch.abort else CutBy.STOP, fire.last.reason)
            self._control_inbox.append(Resolved(watch.id, cut, fire.trigger, fire.attempts, fire.send_ms))
            return True
        if now >= fire.deadline:
            self._control_inbox.append(Resolved(watch.id, fire.last, fire.trigger, fire.attempts,
                                         fire.send_ms))
            return True
        attempt = watch.poster.attempt(watch.order, fire.trigger.tick, None)
        answered = self._monotonic()
        return self._answer(fire, attempt, (answered - now) * 1000, answered)

    def _prepare(self, watch, now):
        if watch.claimed:
            return
        try:
            levels = books.fetch_book(self._http, watch.token_id, timeout=BOOK_TIMEOUT_S)
        except Exception as e:
            self._later(watch, now, f"book read failed: {e}")
            return
        if watch.claimed:
            return
        tick = levels.tick
        if tick is not None and reached(tick, watch.target):
            if watch.claim():
                self._control_inbox.append(Resolved(watch.id, Missed(text(tick))))
            return
        watch.base_quote = Quote(books.best_bid(levels), books.best_ask(levels),
                                 text(tick) if tick is not None else None, Source.BASELINE, now)
        try:
            watch.poster.prewarm(watch.token_id)
        except Exception as e:
            self._later(watch, now, f"prewarm failed: {e}")
            return
        self._presign(watch, now)
        self._control_inbox.append(Prepared(watch))

    def _later(self, watch, now, message):
        self._control_inbox.append(Note(now, watch.id, f"{message}; trying again in {JOIN_RETRY_S:g} s"))
        watch.not_before = now + JOIN_RETRY_S
        self._slow_adoptions.append(watch)

    def _presign(self, watch, now):
        try:
            signed = watch.poster.presign(watch.order, watch.target_text)
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            if message != watch.presign_error:
                watch.presign_error = message
                self._control_inbox.append(Note(now, watch.id,
                                         f"presign failed, signing at fire time: {message}"))
            return
        watch.presign_error = None
        watch.presigned = Presigned(signed, watch.target_text, now) if signed is not None else None

    def _feed_error(self, error):
        self._control_inbox.append(Note(self._monotonic(), None,
                                 f"feed: {type(error).__name__}: {error}; reconnecting"))

    def _drain(self):
        routed = set(self._armed)
        while self._control_inbox:
            event = self._control_inbox.popleft()
            now = self._clock()
            if isinstance(event, Note):
                targets = [event.watch_id] if event.watch_id else [w.id for w in self._armed.values()]
                for watch_id in targets:
                    db.append_log(self._conn, now, event.message, watch_id=watch_id)
                continue
            watch = self._adopted.get(event.watch.id if isinstance(event, Prepared)
                                      else event.watch_id)
            if watch is None:
                continue
            if isinstance(event, Prepared):
                if event.watch is watch and not watch.claimed:
                    self._armed[watch.token_id] = watch
                    self._log(now, watch, event)
            elif isinstance(event, Claimed):
                trigger, quote = event.trigger, event.trigger.quote
                db.mark_firing(self._conn, watch.id, self._wall_of(trigger.at, now),
                               trigger.source.value, trigger.tick,
                               quote.bid if quote else None, quote.ask if quote else None,
                               self._wall_of(quote.at, now) if quote else None)
                self._log(now, watch, event)
            else:
                closed = db.close_watch(self._conn, watch.id, settle(event.outcome, event.attempts),
                                        now, event.send_ms, event.post_ms)
                self._forget(watch)
                if closed:
                    self._log(now, watch, event)
        self._route(routed)

    def _log(self, now, watch, event):
        for line in describe(event, watch):
            db.append_log(self._conn, now, line, watch_id=watch.id)

    def _wall_of(self, monotonic_at, wall_now):
        return wall_now - timedelta(seconds=self._monotonic() - monotonic_at)

    def _forget(self, watch):
        self._adopted.pop(watch.id, None)
        if self._armed.get(watch.token_id) is watch:
            del self._armed[watch.token_id]

    def _reconcile(self):
        now, routed = self._clock(), set(self._armed)
        open_ids = set()
        for row in db.open_watches(self._conn):
            open_ids.add(row.id)
            watch = self._adopted.get(row.id)
            if row.disarm_requested_at is not None:
                if watch is None:
                    if row.state is WatchState.ARMED:
                        self._disarm(row.id, now)
                elif watch.claim():
                    self._disarm(watch.id, now)
                    self._forget(watch)
                else:
                    watch.abort = True
            elif (watch is None and row.state is WatchState.ARMED
                  and (row.dry_run or self._client is not None)):
                watch = Watch(row, self._poster(row))
                self._adopted[row.id] = watch
                self._slow_adoptions.append(watch)
        for watch in list(self._adopted.values()):
            if watch.id not in open_ids:
                watch.claim()
                self._forget(watch)
        self._route(routed)

    def _route(self, before):
        if set(self._armed) != before:
            self._feed.set_tokens(list(self._armed))

    def _disarm(self, watch_id, now):
        db.close_watch(self._conn, watch_id, settle(Disarmed(), None), now)
        db.append_log(self._conn, now, "disarmed", watch_id=watch_id)

    def _poster(self, row):
        if row.dry_run:
            return DryPoster(self._signer)
        return LivePoster(self._client, self._signer, self._post)

    def _publish(self, now):
        wall, samples = self._clock(), []
        for watch in list(self._armed.values()):
            quotes = (watch.base_quote, watch.feed_quote, watch.poll_quote)
            quote = newest(*quotes)
            if quote is not None:
                samples.append(Sample(watch.id, last_tick(*quotes), quote.bid, quote.ask,
                                      wall - timedelta(seconds=now - quote.at)))
        if samples:
            db.publish_samples(self._conn, samples)
