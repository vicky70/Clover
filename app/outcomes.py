"""Window outcomes, derived purely from a window's events and the server clock.

    schedule_set ─▶ PENDING ──open time──▶ ACTIVE ──close+15m──▶ decide()
                                                      │
       ┌──────────────────┬───────────────────┬───────┴──────┬────────────┬───────────────┐
       ▼                  ▼                   ▼              ▼            ▼               ▼
    PASSED       ATTEMPTED_FAILED          MISSED         PAUSED   SERVER_OUTAGE        VOID
    (all checks   (valid attempt,      (no valid attempt) (declared  (first pass after  (admin)
     pass)         not all pass)              │            before     restart, past due,
                        │ 2nd in a row        ▼            open)      not passed)
                        ▼                 miss alert
                   struggling alert           │ pass received before the next
                   (once per task)            ▼ non-paused window opens
                                         PASSED_LATE (new event, supersedes; track advances)

Rules (v1 Behavior Rules + eng D1, D8):
- Server receipt time (event created_at) decides; client timestamps are ignored.
- A run counts for the window if received in [opens_at, closes_at + GRACE].
- `environment_error` runs never count.
- Valid attempt: a run where a `unique` check passed, or 2 counted runs >= 10 min apart.
- Corrections are new outcome_decided events; the latest one is the window's outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

GRACE = timedelta(minutes=15)
MIN_RUN_GAP = timedelta(minutes=10)

PASSED = "PASSED"
PASSED_LATE = "PASSED_LATE"
ATTEMPTED_FAILED = "ATTEMPTED_FAILED"
MISSED = "MISSED"
PAUSED = "PAUSED"
SERVER_OUTAGE = "SERVER_OUTAGE"
VOID = "VOID"

PASSING = {PASSED, PASSED_LATE}
LATE_UPGRADABLE = {MISSED, ATTEMPTED_FAILED}


@dataclass(frozen=True)
class Window:
    id: int
    opens_at: datetime
    closes_at: datetime
    task_id: str | None
    check_keys: frozenset[str]
    unique_keys: frozenset[str]

    @property
    def deadline(self) -> datetime:
        return self.closes_at + GRACE


def current_outcome(events: Iterable[dict]) -> str | None:
    outcome = None
    for e in events:
        if e["type"] == "outcome_decided":
            outcome = e["payload"]["outcome"]
    return outcome


def _runs(window: Window, events: Iterable[dict]) -> list[dict]:
    """Verify runs for this window's task, excluding environment errors."""
    return [
        e for e in events
        if e["type"] == "verify_attempted"
        and e["payload"].get("task_id") == window.task_id
        and e["payload"].get("status") != "environment_error"
    ]


def _passed_keys(run: dict) -> set[str]:
    return {c["key"] for c in run["payload"].get("checks", []) if c.get("passed")}


def run_passes(window: Window, run: dict) -> bool:
    # The server judges from its own spec: every spec check must be reported passing.
    return bool(window.check_keys) and window.check_keys <= _passed_keys(run)


def is_valid_attempt(window: Window, runs: list[dict]) -> bool:
    if any(window.unique_keys & _passed_keys(r) for r in runs):
        return True
    times = sorted(r["created_at"] for r in runs)
    return bool(times) and times[-1] - times[0] >= MIN_RUN_GAP


def _judge(window: Window, events: list[dict]) -> str:
    runs = [r for r in _runs(window, events) if window.opens_at <= r["created_at"] <= window.deadline]
    if any(run_passes(window, r) for r in runs):
        return PASSED
    if is_valid_attempt(window, runs):
        return ATTEMPTED_FAILED
    return MISSED


def decide(window: Window, events: list[dict], now: datetime, *,
           startup: bool = False, late_until: datetime | None = None) -> str | None:
    """Return the outcome to record for `window` now, or None if nothing changes.

    startup: first scheduler pass after a process start (eng D1). A window that is
        past its deadline with no outcome becomes SERVER_OUTAGE unless it passed.
    late_until: when the next non-paused window opens (None: no next window yet).
        A passing run received after the deadline and before it upgrades a
        MISSED / ATTEMPTED_FAILED window to PASSED_LATE.
    """
    current = current_outcome(events)
    if current is None:
        if any(e["type"] == "pause_declared" and e["created_at"] < window.opens_at for e in events):
            return PAUSED if now >= window.opens_at else None
        if now < window.deadline:
            return None
        outcome = _judge(window, events)
        if startup and outcome != PASSED:
            return SERVER_OUTAGE
        return outcome

    if current in LATE_UPGRADABLE:
        for r in _runs(window, events):
            t = r["created_at"]
            if window.deadline < t <= now and (late_until is None or t < late_until) and run_passes(window, r):
                return PASSED_LATE
    return None


def is_struggling(prior_outcomes_same_task: list[str]) -> bool:
    """Two ATTEMPTED_FAILED in a row on the same task.

    `prior_outcomes_same_task` is the decided outcomes of this task's windows, oldest
    first, ending with the one just decided. PAUSED / SERVER_OUTAGE / VOID windows
    don't break or extend the run; a PASSED_LATE correction does break it.
    """
    relevant = [o for o in prior_outcomes_same_task if o not in (PAUSED, SERVER_OUTAGE, VOID)]
    return len(relevant) >= 2 and relevant[-1] == ATTEMPTED_FAILED and relevant[-2] == ATTEMPTED_FAILED
