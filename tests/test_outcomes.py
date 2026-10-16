from datetime import datetime, timedelta, timezone

import pytest

from app import outcomes as o
from app.outcomes import Window, decide, is_struggling

OPEN = datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc)  # 19:00 IST
CLOSE = OPEN + timedelta(hours=1)
DEADLINE = CLOSE + o.GRACE
W = Window(
    id=1, opens_at=OPEN, closes_at=CLOSE, task_id="docker-03",
    check_keys=frozenset({"0:container_running", "1:port_published", "2:http_ok"}),
    unique_keys=frozenset({"1:port_published"}),
)


def run(minutes, passed=(), status=None, task="docker-03"):
    passed = set(passed)
    checks = [{"key": k, "passed": k in passed} for k in sorted(W.check_keys)]
    if status is None:
        status = "pass" if passed == W.check_keys else "fail"
    return {"type": "verify_attempted", "created_at": OPEN + timedelta(minutes=minutes),
            "payload": {"task_id": task, "status": status, "checks": checks}}


def decided(outcome):
    return {"type": "outcome_decided", "created_at": DEADLINE, "payload": {"outcome": outcome}}


ALL = W.check_keys


def test_undecided_before_deadline():
    assert decide(W, [run(5, ALL)], DEADLINE - timedelta(seconds=1)) is None


def test_passed_in_window():
    assert decide(W, [run(30, ALL)], DEADLINE) == o.PASSED


def test_pass_received_during_grace_is_passed():
    assert decide(W, [run(70, ALL)], DEADLINE) == o.PASSED


def test_pass_received_after_grace_is_not_in_window():
    assert decide(W, [run(76, ALL)], DEADLINE + timedelta(minutes=2)) == o.MISSED


def test_run_before_window_opens_does_not_count():
    assert decide(W, [run(-5, ALL)], DEADLINE) == o.MISSED


def test_no_runs_is_missed():
    assert decide(W, [], DEADLINE) == o.MISSED


def test_unique_check_passing_is_valid_attempt():
    assert decide(W, [run(10, {"1:port_published"})], DEADLINE) == o.ATTEMPTED_FAILED


def test_leftover_state_passing_non_unique_check_is_not_an_attempt():
    # nginx from docker-02 still running: container_running passes, nothing unique does.
    assert decide(W, [run(10, {"0:container_running"})], DEADLINE) == o.MISSED


def test_two_runs_ten_minutes_apart_is_valid_attempt():
    assert decide(W, [run(5), run(15)], DEADLINE) == o.ATTEMPTED_FAILED


def test_two_runs_under_ten_minutes_apart_is_missed():
    assert decide(W, [run(5), run(14)], DEADLINE) == o.MISSED


def test_environment_error_runs_never_count():
    runs = [run(5, status="environment_error"), run(30, status="environment_error")]
    assert decide(W, runs, DEADLINE) == o.MISSED
    # nor as one of the two spaced runs
    assert decide(W, [run(5, status="environment_error"), run(30)], DEADLINE) == o.MISSED


def test_runs_for_another_task_are_ignored():
    assert decide(W, [run(5, ALL, task="docker-02")], DEADLINE) == o.MISSED


def test_client_claimed_pass_without_all_spec_checks_is_not_pass():
    r = run(10, ALL)
    r["payload"]["checks"] = [{"key": "1:port_published", "passed": True}]
    r["payload"]["status"] = "pass"
    assert decide(W, [r], DEADLINE) == o.ATTEMPTED_FAILED


def test_paused_before_open():
    pause = {"type": "pause_declared", "created_at": OPEN - timedelta(hours=3), "payload": {}}
    assert decide(W, [pause], OPEN - timedelta(minutes=1)) is None
    assert decide(W, [pause], OPEN) == o.PAUSED


def test_pause_after_open_is_ignored():
    pause = {"type": "pause_declared", "created_at": OPEN + timedelta(minutes=1), "payload": {}}
    assert decide(W, [pause], DEADLINE) == o.MISSED


def test_startup_rule_makes_unpassed_window_server_outage():
    assert decide(W, [], DEADLINE + timedelta(hours=2), startup=True) == o.SERVER_OUTAGE
    assert decide(W, [run(10, {"1:port_published"})], DEADLINE, startup=True) == o.SERVER_OUTAGE


def test_startup_rule_keeps_a_recorded_pass():
    assert decide(W, [run(10, ALL)], DEADLINE + timedelta(hours=2), startup=True) == o.PASSED


def test_startup_rule_does_not_touch_open_windows():
    assert decide(W, [], CLOSE, startup=True) is None


def test_already_decided_returns_none():
    assert decide(W, [run(10, ALL), decided(o.PASSED)], DEADLINE + timedelta(hours=1)) is None


@pytest.mark.parametrize("prior", [o.MISSED, o.ATTEMPTED_FAILED])
def test_late_pass_supersedes_missed_and_attempted_failed(prior):
    events = [decided(prior), run(200, ALL)]
    next_open = OPEN + timedelta(days=1)
    assert decide(W, events, OPEN + timedelta(minutes=201), late_until=next_open) == o.PASSED_LATE


def test_late_pass_after_next_window_opens_does_not_count():
    next_open = OPEN + timedelta(days=1)
    events = [decided(o.MISSED), run(24 * 60 + 5, ALL)]
    assert decide(W, events, next_open + timedelta(minutes=10), late_until=next_open) is None


def test_late_failing_run_does_not_upgrade():
    assert decide(W, [decided(o.MISSED), run(200)], OPEN + timedelta(hours=5)) is None


@pytest.mark.parametrize("final", [o.PAUSED, o.SERVER_OUTAGE, o.PASSED, o.PASSED_LATE, o.VOID])
def test_late_pass_does_not_touch_other_outcomes(final):
    assert decide(W, [decided(final), run(200, ALL)], OPEN + timedelta(hours=5)) is None


def test_struggling_rule():
    AF, M = o.ATTEMPTED_FAILED, o.MISSED
    assert not is_struggling([AF])
    assert is_struggling([AF, AF])
    assert is_struggling([AF, o.PAUSED, AF])
    assert not is_struggling([AF, M, AF])
    assert not is_struggling([AF, o.PASSED_LATE, AF])
