"""Regression tests for the independent review of f596b72.

Each test reproduces a defect found in review against the mock campus; on the
reviewed code it fails (crash, blind resubmit, wrong outcome or exhausted budget).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from email.utils import formatdate
from typing import Any

import pytest
from conftest import FAST_OPTIONS, Env, Run

from seu_lecture.core.captcha import FixedSolver
from seu_lecture.core.client import parse_retry_after
from seu_lecture.core.clock import ClockEstimate
from seu_lecture.core.errors import ProtocolError
from seu_lecture.core.lecture import SHANGHAI, parse_lectures, parse_reservations
from seu_lecture.core.reserve import MAX_BACKOFF_S, Outcome, ReservationTask, ReserveOptions

MakeEnv = Callable[..., Env]


def submits(env: Env) -> int:
    return env.campus.calls.get("addReservation.do", 0)


def break_list_after_first_submit(env: Env, fault: str) -> None:
    """Let the preparation read the list normally, then corrupt every later read."""
    campus, original = env.campus, env.campus._submit

    def submit(session: dict[str, Any], form: dict[str, str]) -> Any:
        response = original(session, form)
        campus.config.mine_fault, campus.config.mine_fault_times = fault, 99
        return response

    campus._submit = submit  # type: ignore[method-assign]


# ---- 1. unreadable submit reply: verify first, never re-login and resubmit blindly ---------

@pytest.mark.parametrize("fault", ["html", "empty", "broken_json"])
def test_unreadable_reply_not_applied_is_verified_then_resubmitted_once(
        make_env: MakeEnv, fault: str) -> None:
    env = make_env(submit_fault=fault)
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies[0] == "unreadable_response"
    assert run.kinds.index("resubmit") > run.kinds.index("attempt")
    assert submits(env) == 2
    assert env.relogins == 0  # the session was fine; no re-login was needed


@pytest.mark.parametrize("fault", ["html_applied", "empty_applied", "broken_json_applied"])
def test_unreadable_reply_that_was_applied_is_confirmed_without_resubmit(
        make_env: MakeEnv, fault: str) -> None:
    env = make_env(submit_fault=fault)
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert submits(env) == 1


@pytest.mark.parametrize("fault", ["html", "empty", "broken_json"])
def test_unreadable_reply_with_unreadable_list_is_unconfirmed(make_env: MakeEnv,
                                                              fault: str) -> None:
    env = make_env(submit_fault=fault)
    break_list_after_first_submit(env, "business_error")
    run = env.run()
    assert run.outcome is Outcome.UNCONFIRMED
    assert run.result.exit_code == 11
    assert submits(env) == 1


# ---- 2. "my reservations" errors are not an empty list ------------------------------------

@pytest.mark.parametrize("payload", [
    {"code": "0", "datas": {}},                       # wdhdList missing
    {"code": "0", "datas": {"wdhdList": None}},       # wdhdList null
    {"code": "1", "msg": "系统异常"},                # business error without datas
    {"code": "1", "msg": "系统异常", "datas": {"wdhdList": []}},
    {"success": False, "datas": {"wdhdList": []}},
])
def test_malformed_reservation_list_is_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ProtocolError):
        parse_reservations(payload)


def test_lecture_list_business_error_is_not_an_empty_list() -> None:
    with pytest.raises(ProtocolError):
        parse_lectures({"code": "1", "msg": "系统异常", "datas": {"hdlbList": []}})


@pytest.mark.parametrize("fault", ["missing_list", "null_list", "business_error"])
def test_broken_reservation_list_never_authorises_a_resubmit(make_env: MakeEnv,
                                                             fault: str) -> None:
    env = make_env(submit_fault="read_timeout")  # outcome unknown, not applied
    break_list_after_first_submit(env, fault)
    run = env.run()
    assert run.outcome is Outcome.UNCONFIRMED
    assert "resubmit" not in run.kinds
    assert submits(env) == 1


# ---- 3. pagination without total --------------------------------------------------------

OTHERS = [f"0{i:031x}" for i in range(300)]  # sort before LECTURE_ID in the mock's list


def test_applied_reservation_on_page_two_without_total_is_found(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="read_timeout_applied", extra_reservations=OTHERS[:60])
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert submits(env) == 1  # found on page 2; no duplicate submit


def test_reservation_list_beyond_page_limit_is_unknown(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="read_timeout", extra_reservations=OTHERS)
    run = env.run()
    assert any("cannot rule out" in e["message"] for e in run.events_of("warning"))
    assert run.outcome is Outcome.UNCONFIRMED
    assert submits(env) == 1


# ---- 4. HTTP 429 + Retry-After --------------------------------------------------------------

class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        time.sleep(min(seconds, 0.02))


def test_retry_after_parsing() -> None:
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after(formatdate(1_000_010, usegmt=True), now=1_000_000) == 10.0
    assert parse_retry_after(formatdate(999_000, usegmt=True), now=1_000_000) == 0.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_429_honours_retry_after_then_succeeds(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="http_429", retry_after="2")
    sleeps = Sleeps()
    run = env.run(sleep=sleeps)
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies[0] == "rate_limited"
    assert [e["seconds"] for e in run.events_of("backoff")] == [2.0]
    assert 2.0 in sleeps.calls
    assert submits(env) == 2


def test_429_without_retry_after_uses_configured_backoff(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="http_429")
    run = env.run(sleep=Sleeps())
    assert run.outcome is Outcome.CONFIRMED
    assert [e["seconds"] for e in run.events_of("backoff")] == [FAST_OPTIONS["rate_limit_backoff_s"]]


def test_repeated_429_returns_rate_limited(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="http_429", fault_times=5, retry_after="1")
    run = env.run(sleep=Sleeps())
    assert run.outcome is Outcome.RATE_LIMITED
    assert run.result.exit_code == 10
    assert submits(env) == 2  # exactly one backoff, then stop


def test_retry_after_beyond_bound_stops_instead_of_waiting(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="http_429", retry_after=str(int(MAX_BACKOFF_S) + 60))
    sleeps = Sleeps()
    run = env.run(sleep=sleeps)
    assert run.outcome is Outcome.RATE_LIMITED
    assert all(s < MAX_BACKOFF_S for s in sleeps.calls)
    assert submits(env) == 1


# ---- 5. auto clock with a skew inside the not-open window -----------------------------------

def run_with_skew(env: Env, *, skew_s: float, open_in: float, app_uses_date_clock: bool = False,
                  estimate: tuple[float, float] | None = None, **options: Any) -> Run:
    """Date header says the server is ``skew_s`` ahead of local time.

    The application opens at local ``at`` (it uses correct time) unless
    ``app_uses_date_clock``, in which case it opens ``skew_s`` earlier.
    """
    client = env.client(login=True)
    at = time.time() + open_in
    env.campus.config.open_at = at - skew_s if app_uses_date_clock else at
    events: list[dict[str, Any]] = []
    opts = {**FAST_OPTIONS, "clock": "auto", "min_submit_interval_ms": 200.0,
            "not_open_window_ms": 3000.0, "max_attempts": 8, "offset_ms": 30.0, **options}
    task = ReservationTask(client, FixedSolver(), ReserveOptions(
        at=datetime.fromtimestamp(at, SHANGHAI), **opts), relogin=env.relogin(client),
        emit=events.append)
    # Too close to the deadline to re-estimate, so this estimate is used as-is.
    offset_ms, error_ms = estimate or (skew_s * 1000, 20.0)
    task.clock = ClockEstimate(offset_ms, error_ms, 1.0, 6)
    return Run(task.run(), events)


def test_skew_of_2_5_s_keeps_attempts_for_the_later_candidate(make_env: MakeEnv) -> None:
    # Regression: one early candidate burned all 8 attempts on "not open" in 1.6 s.
    env = make_env()
    run = run_with_skew(env, skew_s=2.5, open_in=2.9)
    assert run.outcome is Outcome.CONFIRMED
    assert len(run.events_of("plan")[0]["candidates"]) == 2
    assert run.replies == ["not_open", "success"]
    assert run.result.attempts <= 8


def test_attempt_cap_spans_all_candidates(make_env: MakeEnv) -> None:
    # The earlier candidate is open but every captcha fails: the probe may use only
    # PROBE_ATTEMPTS, the later candidate the rest, and the total stays at max_attempts.
    env = make_env(captcha_failures=99)
    run = run_with_skew(env, skew_s=1.2, open_in=2.5, app_uses_date_clock=True, max_attempts=3)
    assert run.outcome is Outcome.FAILED
    assert "candidate_exhausted" in run.kinds
    assert submits(env) == 3  # 2 probe attempts + 1 at the later candidate, never 3 + 3
    assert run.result.attempts == 3


def test_early_candidate_success_when_application_uses_date_clock(make_env: MakeEnv) -> None:
    env = make_env()
    run = run_with_skew(env, skew_s=2.5, open_in=2.9, app_uses_date_clock=True)
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["success"]


def test_single_attempt_budget_skips_the_probe(make_env: MakeEnv) -> None:
    env = make_env()
    run = run_with_skew(env, skew_s=1.2, open_in=2.5, max_attempts=1)
    assert run.outcome is Outcome.CONFIRMED
    assert submits(env) == 1
    assert env.campus.submit_times[0] >= env.campus.config.open_at  # fired at the later one


def test_early_candidate_aims_at_late_edge_of_the_estimate_error(make_env: MakeEnv) -> None:
    # Regression (seen on Windows): true skew 2.5 s, estimate 2.563 +/- 0.063 s. Aiming at the
    # midpoint fired 63 ms early (> offset_ms), got "not open" and skipped to the later
    # candidate 2.5 s too late. The late edge of the error interval is never early.
    env = make_env()
    run = run_with_skew(env, skew_s=2.5, open_in=2.9, app_uses_date_clock=True,
                        estimate=(2563.0, 63.0))
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["success"]
    assert "candidate_not_open" not in run.kinds
