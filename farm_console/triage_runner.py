"""Runs Claude screens for the console: a FIFO of markets, at most WORKERS at a time."""

import threading
from collections import deque
from contextlib import closing

import farm_db
import triage

WORKERS = 2


class TriageRunner:
    """fetch_rules reads a market's remote.Rules; run is triage.run_claude and
    fetch triage.fetch_page, or stand-ins with their signatures. Each job
    stores an ok or an error row, so a failure shows on the market's panel and
    never reaches a request."""

    def __init__(self, db_path, clock, fetch_rules, run=triage.run_claude,
                 fetch=triage.fetch_page):
        self.db_path = db_path
        self.clock = clock
        self.fetch_rules = fetch_rules
        self.run = run
        self.fetch = fetch
        self._queue = deque()
        self._running = set()
        self._workers = 0
        self._lock = threading.Lock()

    def pending(self):
        """The condition ids waiting their turn."""
        with self._lock:
            return frozenset(self._queue)

    def running(self):
        with self._lock:
            return frozenset(self._running)

    def enqueue(self, condition_id):
        """Queue a market unless it is already waiting or running; whether it was."""
        with self._lock:
            if condition_id in self._queue or condition_id in self._running:
                return False
            self._queue.append(condition_id)
            if self._workers < WORKERS:
                self._workers += 1
                threading.Thread(target=self._work, daemon=True, name="console-triage").start()
            return True

    def enqueue_top(self, conn, n):
        """Queue the n best-ranked queued candidates with no ok row of this
        prompt version and no job yet; how many were queued."""
        done = farm_db.triaged_ids(conn, triage.PROMPT_VERSION)
        busy = self.pending() | self.running()
        ids = [c for c in farm_db.candidate_queue(conn) if c not in done and c not in busy]
        return sum(self.enqueue(c) for c in ids[:n])

    def _work(self):
        """Take jobs until the queue is empty, then exit."""
        while True:
            with self._lock:
                if not self._queue:
                    self._workers -= 1
                    return
                condition_id = self._queue.popleft()
                self._running.add(condition_id)
            try:
                with closing(farm_db.open_db(self.db_path)) as conn:
                    triage.screen_and_store(conn, condition_id, self.clock(),
                                            self.fetch_rules, self.run, self.fetch)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._running.discard(condition_id)
