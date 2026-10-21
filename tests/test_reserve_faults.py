"""Network faults and uncertain outcomes: the task must verify before it ever resubmits."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import Env

from seu_lecture.core.reserve import Outcome
from seu_lecture.mock import LECTURE_ID

MakeEnv = Callable[..., Env]


# ---- network faults ----------------------------------------------------------------------

def test_read_timeout_not_applied_is_verified_then_resubmitted_once(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="read_timeout")
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["network_error", "success"]
    assert [e["reason"] for e in run.events_of("resubmit")] == [
        "verified not reserved after uncertain submit"]
    assert run.events_of("attempt")[0]["maybe_delivered"] is True
    assert env.campus.calls["addReservation.do"] == 2
    assert env.campus.reservations == {LECTURE_ID}


def test_read_timeout_applied_is_found_by_verification_and_not_resubmitted(
        make_env: MakeEnv) -> None:
    env = make_env(submit_fault="read_timeout_applied")
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert run.result.attempts == 1
    assert "resubmit" not in run.kinds
    assert env.campus.calls["addReservation.do"] == 1


def test_disconnect_is_treated_like_an_uncertain_submit(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="disconnect")
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["network_error", "success"]
    assert "resubmit" in run.kinds
    assert env.campus.calls["addReservation.do"] == 2


def test_connect_error_is_retried_immediately_without_verification(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="connect_error")
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["network_error", "success"]
    assert run.events_of("attempt")[0]["maybe_delivered"] is False  # provably never sent
    assert "resubmit" not in run.kinds
    assert env.campus.calls["addReservation.do"] == 2


def test_server_error_goes_through_the_verify_path(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="server_error")
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert run.replies == ["network_error", "success"]
    assert run.events_of("attempt")[0]["maybe_delivered"] is True
    assert "resubmit" in run.kinds
    assert env.campus.calls["addReservation.do"] == 2


def test_resubmits_are_bounded(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="read_timeout", fault_times=2)
    run = env.run()
    assert run.outcome is Outcome.UNCONFIRMED
    assert run.result.exit_code == 11
    assert "resubmit budget used" in run.result.message
    assert env.campus.calls["addReservation.do"] == 2
    assert not env.campus.reservations


def test_max_resubmits_zero_never_resubmits(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="disconnect")
    run = env.run(max_resubmits=0)
    assert run.outcome is Outcome.UNCONFIRMED
    assert env.campus.calls["addReservation.do"] == 1


def test_repeated_connect_errors_are_capped_by_max_attempts(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="connect_error", fault_times=99)
    run = env.run(max_attempts=3)
    assert run.outcome is Outcome.FAILED
    assert run.result.attempts == 3
    assert env.campus.calls["addReservation.do"] == 3


# ---- uncertain replies -----------------------------------------------------------------------

def test_unrecognised_reply_is_verified_and_reported_failed(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="unknown")
    run = env.run()
    assert run.outcome is Outcome.FAILED
    assert run.result.exit_code == 1
    assert run.replies == ["unknown"]
    assert "系统繁忙" in run.result.message
    assert env.campus.calls["addReservation.do"] == 1  # no blind resubmit
    assert not env.campus.reservations


def test_success_reply_without_visible_reservation_is_unconfirmed(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="success_hidden")
    run = env.run()
    assert run.outcome is Outcome.UNCONFIRMED
    assert run.result.exit_code == 11
    assert run.replies == ["success"]
    assert "not visible" in run.result.message
    assert env.campus.calls["addReservation.do"] == 1  # never resubmits
    assert not env.campus.reservations


def test_hidden_success_is_verified_several_times(make_env: MakeEnv) -> None:
    env = make_env(submit_fault="success_hidden")
    client = env.client(login=True)
    before = env.campus.calls.get("queryMyActivityList.do", 0)
    run = env.run(client=client, verify_attempts=4)
    # prepare + session checks use the list too, so only a lower bound is meaningful
    assert env.campus.calls["queryMyActivityList.do"] - before >= 4
    assert run.outcome is Outcome.UNCONFIRMED


@pytest.mark.parametrize("lag", [1, 2])
def test_lagging_list_is_still_confirmed_by_verify_retries(make_env: MakeEnv, lag: int) -> None:
    env = make_env(list_lag=lag)
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert env.campus.calls["addReservation.do"] == 1


def test_list_lag_beyond_verify_budget_is_unconfirmed(make_env: MakeEnv) -> None:
    env = make_env(list_lag=3)
    run = env.run()  # default verify_attempts == 3, so the third read is still empty
    assert run.outcome is Outcome.UNCONFIRMED
    assert env.campus.calls["addReservation.do"] == 1
    assert env.campus.reservations == {LECTURE_ID}  # it was applied; we just could not see it


def test_more_verify_attempts_rescue_a_long_lag(make_env: MakeEnv) -> None:
    env = make_env(list_lag=3)
    assert env.run(verify_attempts=5).outcome is Outcome.CONFIRMED
