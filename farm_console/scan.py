"""Runs the screener's scan for the console, one at a time, in a background thread."""

import threading
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Optional

import farm_db
import reward_screener


class ScanPhase(Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class ScanJob:
    """The console's scan: replaced whole on every change, never mutated."""

    phase: ScanPhase
    stage: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    funnel: Optional[farm_db.ScanFunnel] = None
    error: Optional[str] = None


IDLE = ScanJob(ScanPhase.IDLE)


class ScanRunner:
    """scan is reward_screener.run_scan or a stand-in with its signature. It
    starts from the fill prior the last scan stored."""

    def __init__(self, db_path, clock, scan=reward_screener.run_scan):
        self.db_path = db_path
        self.clock = clock
        self.scan = scan
        self.job = IDLE
        self._lock = threading.Lock()

    def start(self, config):
        """Start a scan with this ScreenConfig; False when one is already running."""
        with self._lock:
            if self.job.phase is ScanPhase.RUNNING:
                return False
            self.job = ScanJob(ScanPhase.RUNNING, "starting", self.clock())
        threading.Thread(target=self._run, args=(config, self.job.started_at),
                         daemon=True, name="console-scan").start()
        return True

    def _stage(self, stage):
        self.job = replace(self.job, stage=stage)

    def _run(self, config, started_at):
        try:
            with closing(farm_db.open_db(self.db_path)) as conn:
                result = self.scan(config, started_at, reward_screener.load_prior(conn),
                                   rejected=farm_db.condition_ids(conn, "rejected"),
                                   on_stage=self._stage)
                self._stage(f"storing {len(result.assessments):,} assessments")
                reward_screener.store(conn, result, started_at)
            self.job = ScanJob(ScanPhase.DONE, "", started_at, self.clock(), result.funnel)
        except Exception as e:
            self.job = ScanJob(ScanPhase.FAILED, self.job.stage, started_at, self.clock(),
                               error=f"{type(e).__name__}: {e}")
