"""Session expiry and recovery, in the service layer and inside a running reservation."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import Env

from seu_lecture.core.captcha import FixedSolver
from seu_lecture.core.errors import AuthRequired, SessionExpired
from seu_lecture.core.reserve import Outcome
from seu_lecture.mock import LECTURE_ID


class ExpireOnSolve(FixedSolver):
    """Kills every server session when the captcha is solved, i.e. just before the submit."""

    def __init__(self, env: Env, times: int = 1) -> None:
        super().__init__(env.cfg.captcha_answer or "1234")
        self.env = env
        self.times = times

    def solve(self, image: bytes) -> str:
        if self.times > 0:
            self.times -= 1
            self.env.campus.expire_sessions()
        return super().solve(image)


# ---- expiry ----------------------------------------------------------------------

def test_session_ttl_expires_business_calls(make_env: Callable[..., Env]) -> None:
    env = make_env(session_ttl=1)
    client = env.client(login=True)  # the login's own liveness check is call number 1
    with pytest.raises(SessionExpired) as info:
        client.lectures_payload()
    assert info.value.retryable
    assert info.value.exit_code == 3
    assert client.session_alive() is False


def test_expire_sessions_invalidates_logged_in_client(env: Env) -> None:
    client = env.client(login=True)
    assert client.session_alive()
    env.campus.expire_sessions()
    assert client.session_alive() is False


# ---- LectureService.open_session ---------------------------------------------------

def test_open_session_reuses_cached_cookies(env: Env) -> None:
    service = env.service()
    service.login(env.cfg.username, env.cfg.password)
    assert env.campus.calls["casLogin"] == 1

    with service.open_session() as client:
        assert client.session_alive()
    assert env.campus.calls["casLogin"] == 1
    assert env.campus.calls["getChiperKey"] == 1


def test_open_session_relogs_in_when_cache_is_expired(env: Env) -> None:
    service = env.service()
    service.login(env.cfg.username, env.cfg.password)
    before = env.store.get_session(env.cfg.username)
    env.campus.expire_sessions()

    with service.open_session() as client:
        assert client.session_alive()
    assert env.campus.calls["casLogin"] == 2
    assert env.store.get_session(env.cfg.username) != before


def test_open_session_without_cache_logs_in_with_env_password(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEU_LECTURE_PASSWORD", env.cfg.password)
    with env.service().open_session() as client:
        assert client.session_alive()
    assert env.campus.calls["casLogin"] == 1
    assert env.store.get_session(env.cfg.username) is not None


def test_open_session_uses_password_prompt_as_last_resort(env: Env) -> None:
    service = env.service(password_prompt=lambda: env.cfg.password)
    with service.open_session() as client:
        assert client.session_alive()


def test_open_session_without_password_raises_auth_required(env: Env) -> None:
    service = env.service()
    service.login(env.cfg.username, env.cfg.password)
    env.store.passwords.clear()
    env.campus.expire_sessions()
    with pytest.raises(AuthRequired):
        service.open_session()


def test_open_session_without_account_raises_auth_required(env: Env) -> None:
    env.settings.username = None
    with pytest.raises(AuthRequired, match="no account"):
        env.service().open_session()


def test_open_session_closes_client_on_failure(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    service = env.service()
    created = []
    original = service.new_client

    def spy():
        created.append(original())
        return created[-1]

    monkeypatch.setattr(service, "new_client", spy)
    with pytest.raises(AuthRequired):
        service.open_session()
    assert created[0].http.is_closed


def test_auth_status_reflects_session_validity(env: Env) -> None:
    service = env.service()
    assert service.auth_status()["session_valid"] is None  # nothing cached yet
    service.login(env.cfg.username, env.cfg.password)
    status = service.auth_status()
    assert (status["password_saved"], status["session_cached"], status["session_valid"]) == (
        True, True, True)
    assert service.auth_status(online=False)["session_valid"] is None
    env.campus.expire_sessions()
    assert service.auth_status()["session_valid"] is False


def test_logout_clears_password_and_session(env: Env) -> None:
    service = env.service()
    service.login(env.cfg.username, env.cfg.password)
    assert service.logout() == {"username": env.cfg.username, "password_saved": False,
                                "session_cached": False}
    assert env.store.get_password(env.cfg.username) is None
    assert env.store.get_session(env.cfg.username) is None


# ---- relogin inside ReservationTask --------------------------------------------------

def test_task_relogs_in_when_session_expired_before_prepare(env: Env) -> None:
    client = env.client(login=True)
    env.campus.expire_sessions()
    run = env.run(client=client)
    assert run.outcome is Outcome.CONFIRMED
    assert env.relogins == 1
    assert [e["reason"] for e in run.events_of("relogin")] == ["session expired"]


def test_session_expired_at_submit_is_verified_before_any_resubmit(env: Env) -> None:
    # Regression: a redirect at submit used to trigger re-login + immediate resubmit.
    run = env.run(solver=ExpireOnSolve(env))
    assert run.outcome is Outcome.CONFIRMED
    assert env.relogins == 1
    assert [e["reason"] for e in run.events_of("relogin")] == [
        "session expired during verification"]
    names = [e["event"] for e in run.events]
    assert names.index("relogin") < names.index("resubmit")  # list checked before resubmitting
    assert run.result.attempts == 2
    assert env.campus.calls["addReservation.do"] == 2
    assert LECTURE_ID in env.campus.reservations


def test_task_relogs_in_when_session_expires_during_verification(env: Env) -> None:
    campus = env.campus
    original = campus._reserve

    def reserve_then_expire(wid: str) -> None:
        original(wid)
        campus.expire_sessions()

    campus._reserve = reserve_then_expire  # type: ignore[method-assign]
    run = env.run()
    assert run.outcome is Outcome.CONFIRMED
    assert env.relogins == 1
    assert [e["reason"] for e in run.events_of("relogin")] == [
        "session expired during verification"]
    assert campus.calls["addReservation.do"] == 1


def test_task_stops_resubmitting_when_session_keeps_expiring(env: Env) -> None:
    run = env.run(solver=ExpireOnSolve(env, times=99), max_attempts=10)
    assert run.outcome is Outcome.UNCONFIRMED
    assert run.result.exit_code == 11
    assert "resubmit budget used" in run.result.message
    # 1 submit + max_resubmits (1); never one submit per re-login.
    assert env.campus.calls["addReservation.do"] == 2
    assert not env.campus.reservations
