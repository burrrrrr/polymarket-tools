"""Runs Claude screens for the console: a FIFO of markets, taken an event batch
at a time by at most WORKERS workers."""

import threading
from collections import deque
from contextlib import closing

from farm import farm_db, triage

WORKERS = 2


class TriageRunner:
    """fetch_rules reads a market's remote.Rules; run is triage.run_claude and
    fetch triage.fetch_page, or stand-ins with their signatures. Each market
    of a batch gets an ok or an error row, so a failure shows on the market's
    panel and never reaches a request."""

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
        return self._enqueue([condition_id]) == 1

    def enqueue_top(self, conn, n):
        """Queue the n best-ranked queued candidates with no ok row and no job
        yet; how many were queued."""
        done = farm_db.triaged_ids(conn)
        busy = self.pending() | self.running()
        ids = [c for c in farm_db.candidate_queue(conn) if c not in done and c not in busy]
        return self._enqueue(ids[:n])

    def _enqueue(self, condition_ids):
        """Queue the markets neither waiting nor running; how many. They are
        queued before a worker looks, so it finds an event's markets together."""
        with self._lock:
            new = [c for c in condition_ids if c not in self._queue and c not in self._running]
            self._queue.extend(new)
            for _ in range(min(len(new), WORKERS - self._workers)):
                self._workers += 1
                threading.Thread(target=self._work, daemon=True, name="console-triage").start()
            return len(new)

    def _take(self):
        """Move the first queued market and the queued markets of its event, up
        to a batch, to running; their ids. The caller holds the lock. A market
        the database cannot place goes alone."""
        ids = [self._queue[0]]
        try:
            with closing(farm_db.open_db(self.db_path)) as conn:
                batches = triage.read_batches(conn, list(self._queue))
            if batches and batches[0][0].condition_id == ids[0]:
                ids = [m.condition_id for m in batches[0]]
        except Exception:
            pass
        for condition_id in ids:
            self._queue.remove(condition_id)
        self._running.update(ids)
        return ids

    def _work(self):
        """Take batches until the queue is empty, then exit."""
        while True:
            with self._lock:
                if not self._queue:
                    self._workers -= 1
                    return
                ids = self._take()
            try:
                with closing(farm_db.open_db(self.db_path)) as conn:
                    triage.screen_and_store(conn, ids, self.clock(), self.fetch_rules,
                                            self.run, self.fetch)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._running.difference_update(ids)
