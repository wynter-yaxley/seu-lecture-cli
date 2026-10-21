"""Reservation outcomes: happy path, rejections, captcha handling, duplicates, rate limits."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import pytest
from conftest import Env
from fake_campus import LECTURE_ID, FakeCampus

from seu_lecture.core.captcha import FixedSolver
from seu_lecture.core.errors import CaptchaError, ConfigError, NotFound
from seu_lecture.core.reserve import Outcome

MakeEnv = Callable[..., Env]


# ---- success --------------------------------------------------------------------------

def test_success_is_confirmed_after_verification(env: Env) -> None:
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert run.result.exit_code == 0
    assert run.result.attempts == 1
    assert run.result.submitted
    assert run.replies == ["success"]
    assert LECTURE_ID in env.campus.reservations
    assert "fire_to_verified_ms" in run.result.timings
    assert run.result.lecture is not None
    assert run.result.lecture["id"] == LECTURE_ID
    json.dumps(run.result.to_dict())


def test_event_stream_order_and_prefetched_captcha(env: Env) -> None:
    run = env.run(at=0.25)
    assert run.outcome is Outcome.CONFIRMED
    order = [run.kinds.index(kind) for kind in ("prepared", "armed", "attempt", "result")]
    assert order == sorted(order)
    assert run.kinds[0] == "phase"
    assert [e["phase"] for e in run.events_of("phase")] == ["prepare", "wait", "fire"]
    assert run.events[-1]["outcome"] == "confirmed"
    assert env.campus.calls["vcode.do"] == 1  # fetched before the deadline, not after it


def test_events_from_the_critical_section_are_flushed_in_order(env: Env) -> None:
    run = env.run()
    assert run.kinds.index("armed") < run.kinds.index("attempt") < run.kinds.index("result")
    assert run.events_of("phase")[-1]["phase"] == "fire"
    assert run.events_of("phase")[-1]["late_ms"] >= 0


def test_dry_run_solves_captcha_but_never_submits(env: Env) -> None:
    run = env.run(dry_run=True)
    assert run.outcome is Outcome.DRY_RUN
    assert run.result.exit_code == 0
    assert not run.result.submitted
    assert env.campus.calls.get("addReservation.do", 0) == 0
    assert env.campus.calls["vcode.do"] == 1
    assert not env.campus.reservations


# ---- not open yet ---------------------------------------------------------------------

def test_not_open_then_opens_within_window(make_env: MakeEnv) -> None:
    open_at = time.time() + 0.45
    env = make_env(open_at=open_at)
    run = env.run(at=0.1, max_attempts=10)  # fire ~350 ms before the server opens
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies[0] == "not_open"
    assert run.replies[-1] == "success"
    assert run.result.attempts >= 2
    assert env.campus.submit_times[0] < open_at
    assert env.campus.submit_times[-1] >= open_at


def test_not_open_beyond_window_gives_up(make_env: MakeEnv) -> None:
    env = make_env(open_at=time.time() + 60)
    run = env.run(at=0.05, not_open_window_ms=250.0, max_attempts=10)
    assert run.outcome is Outcome.NOT_OPEN
    assert run.result.exit_code == 10
    assert run.result.attempts >= 2
    assert set(run.replies) == {"not_open"}
    assert not env.campus.reservations


# ---- rejections --------------------------------------------------------------------------

def test_full_lecture_exits_10(make_env: MakeEnv) -> None:
    env = make_env(capacity=1, reserved=1)
    run = env.run()
    assert run.outcome is Outcome.FULL
    assert run.result.exit_code == 10
    assert run.replies == ["full"]
    assert env.campus.calls["addReservation.do"] == 1
    assert not env.campus.reservations


def test_watch_wins_a_seat_freed_after_the_lecture_filled(make_env: MakeEnv) -> None:
    env = make_env(capacity=1, reserved=1, free_seat_after_lists=2)
    run = env.run(watch_minutes=1.0, watch_interval_s=1.0, sleep=lambda s: None)
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["full", "success"]
    assert [e["remaining"] for e in run.events_of("watch")] == [0, 1]
    assert env.campus.calls["addReservation.do"] == 2  # no submit while the count says full


class DoubtfulFirst(FixedSolver):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def read(self, image: bytes) -> tuple[str, float]:
        self.reads += 1
        return ("0000", 0.3) if self.reads == 1 else ("1234", 0.95)


def test_network_trouble_before_the_opening_is_retried(make_env: MakeEnv) -> None:
    env = make_env()
    client = env.client(login=True)
    env.cfg.mine_fault, env.cfg.mine_fault_times = "http_429", 2
    run = env.run(client=client, sleep=lambda s: None)
    assert run.outcome is Outcome.CONFIRMED


def test_doubtful_captcha_is_replaced_before_submitting(env: Env) -> None:
    run = env.run(solver=DoubtfulFirst())
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["success"]
    assert env.campus.calls["vcode.do"] == 2


def test_time_conflict_reply_exits_10(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env.campus, "_submit", lambda session, form: FakeCampus._json(
        {"code": "1", "msg": "时间上存在冲突"}))
    run = env.run()
    assert run.outcome is Outcome.CONFLICT
    assert run.result.exit_code == 10
    assert "冲突" in run.result.message
    assert env.campus.calls["addReservation.do"] == 1
    assert not env.campus.reservations


def test_time_conflict_from_the_mock_exits_10(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="conflict")
    run = env.run()
    assert run.outcome is Outcome.CONFLICT
    assert run.result.exit_code == 10


def test_already_reserved_before_start_never_submits(env: Env) -> None:
    env.campus.reservations.add(LECTURE_ID)
    run = env.run()
    assert run.outcome is Outcome.ALREADY_RESERVED
    assert run.result.exit_code == 0
    assert not run.result.submitted
    assert run.result.attempts == 0
    assert env.campus.calls.get("addReservation.do", 0) == 0
    assert env.campus.calls.get("vcode.do", 0) == 0


def test_closed_window_is_reported_without_submitting(make_env: MakeEnv) -> None:
    env = make_env(lectures=[{"WID": LECTURE_ID, "JZMC": "x", "HDZRS": "5",
                              "YYKSSJ": "2020-01-01 00:00:00", "YYJSSJ": "2020-01-02 00:00:00"}])
    run = env.run()
    assert run.outcome is Outcome.CLOSED
    assert run.result.exit_code == 10
    assert env.campus.calls.get("addReservation.do", 0) == 0


def test_unknown_lecture_raises_not_found(env: Env) -> None:
    with pytest.raises(NotFound):
        env.run(lecture_id="ffffffffffffffff")
    assert env.campus.calls.get("addReservation.do", 0) == 0


def test_lecture_without_start_time_needs_explicit_at(make_env: MakeEnv) -> None:
    env = make_env(lectures=[{"WID": LECTURE_ID, "JZMC": "x"}])
    with pytest.raises(ConfigError, match="--at"):
        env.run(at=None)
    assert env.run(at=-1).outcome is Outcome.CONFIRMED


# ---- captcha -------------------------------------------------------------------------------

def test_captcha_failures_are_retried_until_success(make_env: MakeEnv) -> None:
    env = make_env(captcha_failures=2)
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["captcha_error", "captcha_error", "success"]
    assert run.result.attempts == 3
    assert env.campus.calls["addReservation.do"] == 3


def test_wrong_captcha_answer_is_capped_by_max_attempts(env: Env) -> None:
    run = env.run(solver=FixedSolver("0000"), max_attempts=3)
    assert run.outcome is Outcome.FAILED
    assert run.result.exit_code == 1
    assert run.replies == ["captcha_error"] * 3
    assert run.result.attempts == 3
    assert env.campus.calls["addReservation.do"] == 3
    assert not env.campus.reservations


def test_captcha_attempts_are_spaced_by_the_minimum_interval(make_env: MakeEnv) -> None:
    env = make_env(captcha_failures=2)
    env.run(min_submit_interval_ms=150.0)
    first, _, last = env.campus.submit_times
    assert last - first >= 0.28  # two gaps of at least 150 ms (minus timer slack)


class UnreadableCaptcha(FixedSolver):
    def solve(self, image: bytes) -> str:
        raise CaptchaError("unreadable")


def test_unreadable_captcha_counts_towards_the_attempt_cap(env: Env) -> None:
    run = env.run(solver=UnreadableCaptcha(), max_attempts=2)
    assert run.outcome is Outcome.FAILED
    assert not run.result.submitted
    assert run.replies == ["captcha_unavailable"] * 2
    assert env.campus.calls.get("addReservation.do", 0) == 0
    assert env.campus.calls["vcode.do"] == 6  # three images per attempt


def test_prefetch_failure_only_warns(env: Env) -> None:
    run = env.run(at=0.2, solver=UnreadableCaptcha(), max_attempts=1)
    assert run.outcome is Outcome.FAILED
    assert "warning" in run.kinds


# ---- duplicates ------------------------------------------------------------------------------

class ReserveElsewhere(FixedSolver):
    """Another device reserves the lecture while this task is solving the captcha."""

    def __init__(self, env: Env) -> None:
        super().__init__("1234")
        self.env = env

    def solve(self, image: bytes) -> str:
        self.env.campus.reservations.add(LECTURE_ID)
        return super().solve(image)


def test_duplicate_reply_is_verified_as_already_reserved(env: Env) -> None:
    run = env.run(solver=ReserveElsewhere(env))
    assert run.outcome is Outcome.ALREADY_RESERVED
    assert run.result.exit_code == 0
    assert run.replies == ["already_reserved"]
    assert env.campus.calls["addReservation.do"] == 1


def test_second_submit_answers_already_reserved_when_list_lags(make_env: MakeEnv) -> None:
    # The first submit is applied but its reply is lost, and the list stays empty long enough
    # for verification to conclude "absent"; the allowed resubmit then hits "already reserved".
    env = make_env(submit_fault="read_timeout_applied", list_lag=5)
    run = env.run()
    assert run.outcome is Outcome.ALREADY_RESERVED
    assert run.replies == ["network_error", "already_reserved"]
    assert "resubmit" in run.kinds
    assert env.campus.calls["addReservation.do"] == 2
    assert LECTURE_ID in env.campus.reservations


# ---- rate limiting ------------------------------------------------------------------------------

def test_rate_limit_once_backs_off_then_succeeds(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="rate_limit", fault_times=1)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        time.sleep(seconds)

    run = env.run(sleep=sleep, rate_limit_backoff_s=0.05)
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["rate_limited", "success"]
    assert 0.05 in sleeps
    assert env.campus.calls["addReservation.do"] == 2


def test_repeated_rate_limit_gives_up(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="rate_limit", fault_times=2)
    run = env.run()
    assert run.outcome is Outcome.RATE_LIMITED
    assert run.result.exit_code == 10
    assert env.campus.calls["addReservation.do"] == 2
    assert not env.campus.reservations


def test_raw_answers_are_logged_even_when_unreadable(make_env: MakeEnv) -> None:
    run = make_env().run()
    [attempt] = run.events_of("attempt")
    assert attempt["raw"]["http_status"] == 200
    assert "预约成功" in attempt["raw"]["body"]
    [mine] = run.events_of("verify_raw")
    assert LECTURE_ID in mine["raw"]["body"]  # the real list shape after a submit
    html = make_env(submit_fault="html").run()
    first = html.events_of("attempt")[0]
    assert first["reply"] == "unreadable_response"
    assert "html" in first["raw"]["content_type"]
    assert "统一身份认证" in first["raw"]["body"]


def test_not_open_replies_do_not_burn_the_budget(make_env: MakeEnv) -> None:
    env = make_env(open_at=time.time() + 0.8)
    run = env.run(at=0.0, max_attempts=1, not_open_pause_ms=100.0)  # fires before the server opens
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies[-1] == "success" and "not_open" in run.replies


def test_final_check_follows_a_moved_opening(make_env: MakeEnv) -> None:
    env = make_env(open_at=float(int(time.time()) + 4))  # whole seconds, like YYKSSJ
    moved: list[float] = []

    def sleep(seconds: float) -> None:
        if not moved:  # while the task waits, the school moves the opening 2 s later
            moved.append(env.cfg.open_at + 2)
            env.cfg.open_at = moved[0]
        time.sleep(seconds)

    run = env.run(at=None, final_check_lead_s=1.0, sleep=sleep)
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["success"]  # fired at the new time: no "not open"
    assert run.events_of("final_check")[0]["appoint_check"]["http_status"] == 200
    assert len(run.events_of("retarget")) == 1


class BrokenAfterSubmit(FixedSolver):
    """A bug striking after the first submit must still end in a verified result."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def read(self, image: bytes) -> tuple[str, float]:
        self.reads += 1
        if self.reads > 1:
            raise ZeroDivisionError("simulated bug")
        return "0000", 1.0  # wrong captcha: forces a second read


def test_a_bug_after_a_submit_still_ends_in_a_verified_result(env: Env) -> None:
    run = env.run(solver=BrokenAfterSubmit())
    assert run.outcome is Outcome.FAILED  # verified not reserved
    assert "ZeroDivisionError" in run.result.message
    warning = run.events_of("warning")[-1]
    assert "simulated bug" in warning["traceback"]
    assert run.events[-1]["event"] == "result"
