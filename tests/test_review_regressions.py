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
from fake_campus import LECTURE_ID

from seu_lecture.core import reserve as reserve_module
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


# ---- 5. clock: the lecture app's own time (xtdqsj), not the Date header -------------------

def run_at(env: Env, open_in: float, **options: Any) -> Run:
    """Reservation opening ``open_in`` s from now on the *app* clock (``clock_offset_s``)."""
    client = env.client(login=True)
    at = time.time() + env.cfg.clock_offset_s + open_in
    env.campus.config.open_at = at
    events: list[dict[str, Any]] = []
    task = ReservationTask(client, FixedSolver(), ReserveOptions(
        at=datetime.fromtimestamp(at, SHANGHAI), **{**FAST_OPTIONS, "offset_ms": 30.0, **options}),
        relogin=env.relogin(client), emit=events.append)
    return Run(task.run(), events)


def test_auto_clock_follows_the_app_clock(make_env: MakeEnv) -> None:
    # The app clock runs 2.5 s ahead of ours: the opening comes 2.5 s earlier locally.
    env = make_env(clock_offset_s=2.5)
    run = run_at(env, 5.5, clock="auto")
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["success"]  # no "not open", no late arrival
    clock = run.events_of("clock")[0]
    assert abs(clock["offset_ms"] - 2500) <= clock["error_ms"] + 50
    assert 0 <= env.campus.submit_times[0] - env.campus.config.open_at < 0.5


def test_opening_uses_the_late_edge_of_the_estimate(make_env: MakeEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env()
    task = ReservationTask(env.client(login=True), FixedSolver(), ReserveOptions(lecture_id=LECTURE_ID),
                           relogin=lambda: None)
    monkeypatch.setattr(reserve_module, "estimate_offset", lambda *a, **k: ClockEstimate(2563.0, 63.0, 5.0, 6))
    target = time.time() + 10
    assert task._opening(target) == pytest.approx(target - 2.5)  # never early by the 63 ms error
    monkeypatch.setattr(reserve_module, "estimate_offset", lambda *a, **k: ClockEstimate(40.0, 63.0, 5.0, 6))
    assert task._opening(target) == pytest.approx(target + 0.023)  # small skew: still the late edge


# ---- 6. 429 during captcha prefetch and verification (review follow-up) -----------------------

def test_429_on_captcha_prefetch_backs_off_then_reserves(make_env: MakeEnv) -> None:
    env = make_env(captcha_fault_times=1, retry_after="1")
    sleeps = Sleeps()
    run = env.run(at=1.5, sleep=sleeps)
    assert run.outcome is Outcome.CONFIRMED
    backoff = run.events_of("backoff")
    assert [b["seconds"] for b in backoff] == [1.0] and "prefetch" in backoff[0]["reason"]
    assert 1.0 in sleeps.calls


def test_429_during_verification_waits_retry_after(make_env: MakeEnv) -> None:
    env = make_env(retry_after="2")
    campus, original = env.campus, env.campus._submit

    def submit(session: dict[str, Any], form: dict[str, str]) -> Any:
        response = original(session, form)
        campus.config.mine_fault, campus.config.mine_fault_times = "http_429", 1
        return response

    campus._submit = submit  # type: ignore[method-assign]
    sleeps = Sleeps()
    run = env.run(sleep=sleeps)
    assert run.outcome is Outcome.CONFIRMED
    assert 2.0 in sleeps.calls  # waited as asked instead of re-reading immediately


def test_retry_after_beyond_bound_in_verification_is_unconfirmed(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="read_timeout", retry_after=str(int(MAX_BACKOFF_S) + 1))
    break_list_after_first_submit(env, "http_429")
    run = env.run(sleep=Sleeps())
    assert run.outcome is Outcome.UNCONFIRMED
    assert submits(env) == 1


# ---- 7. exhausted attempt budget keeps rate_limited / unconfirmed (review follow-up) ------------

def test_budget_exhausted_while_rate_limited_is_rate_limited(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="http_429", retry_after="0")
    run = env.run(sleep=Sleeps(), max_attempts=1)
    assert run.outcome is Outcome.RATE_LIMITED
    assert run.result.exit_code == 10


def test_budget_exhausted_after_unknown_outcome_is_unconfirmed(make_env: MakeEnv) -> None:
    # Submit 1 times out (outcome unknown, list says absent -> one resubmit); submit 2 gets a
    # captcha error and the budget is gone; the list is now unreadable -> unconfirmed, not failed.
    env = make_env(submit_fault="read_timeout", captcha_failures=1)
    campus, original, count = env.campus, env.campus._submit, [0]

    def submit(session: dict[str, Any], form: dict[str, str]) -> Any:
        response = original(session, form)
        count[0] += 1
        if count[0] == 2:
            campus.config.mine_fault, campus.config.mine_fault_times = "business_error", 99
        return response

    campus._submit = submit  # type: ignore[method-assign]
    run = env.run(max_attempts=2)
    assert run.outcome is Outcome.UNCONFIRMED
    assert run.result.exit_code == 11
    assert submits(env) == 2


def test_budget_exhausted_after_unknown_outcome_but_verified_absent_is_failed(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="read_timeout", captcha_failures=1)
    run = env.run(max_attempts=2)
    assert run.outcome is Outcome.FAILED
    assert "verified not in my reservations" in run.result.message
