"""The fake campus behind a real loopback socket (keep-alive, real timing), kept short."""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

from seu_lecture.core.auth import CasLogin, Credentials
from seu_lecture.core.captcha import FixedSolver
from seu_lecture.core.client import CampusClient, Endpoints
from seu_lecture.core.lecture import SHANGHAI, load_lectures, parse_lectures
from seu_lecture.core.reserve import Outcome, ReservationTask, ReserveOptions, TaskResult
from seu_lecture.mock import LECTURE_ID, FakeCampus, MockConfig, MockServer, default_lectures


def run_over_socket(campus: FakeCampus, base: str, open_in: float, *, set_open: bool = False,
                    **options: object) -> tuple[TaskResult, list[dict[str, object]]]:
    """Log in first, then fix the opening time ``open_in`` seconds ahead.

    Server start-up, RSA key generation and login are slow on some CI machines
    (notably Windows); choosing the opening time only after them keeps that
    set-up cost out of the timing assertions.
    """
    config = campus.config
    events: list[dict[str, object]] = []
    opts = {"clock": "local", "offset_ms": 20.0, "captcha_lead_ms": 300.0,
            "min_submit_interval_ms": 100.0, "verify_interval_ms": 10.0, **options}
    with CampusClient(Endpoints.for_base(base)) as client:
        CasLogin(client, Credentials(config.username, config.password), fingerprint="fp").login()
        at = time.time() + open_in
        if set_open:
            config.open_at = at
        task = ReservationTask(
            client, FixedSolver(),
            ReserveOptions(lecture_id=LECTURE_ID, at=datetime.fromtimestamp(at, SHANGHAI), **opts),
            relogin=lambda: None, emit=events.append)
        return task.run(), events


def test_reservation_end_to_end_over_a_real_socket() -> None:
    campus = FakeCampus(MockConfig(open_at=time.time() + 3600))  # closed until set below
    with MockServer(campus) as base:
        result, events = run_over_socket(campus, base, 1.5, set_open=True)
    open_at = campus.config.open_at
    assert open_at is not None
    # Set-up and the task's own preparation are not timing: they must just finish in time.
    prepared = next(e for e in events if e["event"] == "prepared")
    assert prepared["wall"] < open_at - 0.3, "preparation did not finish before opening"
    # Scheduling precision, measured on its own.
    assert result.timings["schedule_late_ms"] < 50
    # Outcome and arrival: after opening, and not much later.
    assert result.outcome is Outcome.CONFIRMED
    assert result.attempts == 1  # the prefetched captcha was ready, the first submit won
    assert LECTURE_ID in campus.reservations
    assert 0 <= campus.submit_times[0] - open_at < 0.25
    assert events[-1]["event"] == "result"


def test_dropped_connection_is_recovered_over_a_real_socket() -> None:
    campus = FakeCampus(MockConfig(submit_fault="disconnect"))
    with MockServer(campus) as base:
        result, events = run_over_socket(campus, base, -1)
    assert result.outcome is Outcome.CONFIRMED
    assert campus.calls["addReservation.do"] == 2
    assert any(e["event"] == "resubmit" for e in events)


def test_server_serves_login_and_lists_over_http() -> None:
    campus = FakeCampus()
    with MockServer(campus) as base, CampusClient(Endpoints.for_base(base)) as client:
        CasLogin(client, Credentials(campus.config.username, campus.config.password), fingerprint="fp").login()
        assert load_lectures(client)[0].id == LECTURE_ID
        assert client.probe_app_clock().server > 0


# ---- mock fidelity ---------------------------------------------------------------------------

@pytest.fixture
def utc_timezone():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset (POSIX)")
def test_mock_open_time_does_not_depend_on_the_machine_timezone(utc_timezone: None) -> None:
    open_at = 1_900_000_000.0
    [first, _] = parse_lectures({"datas": {"hdlbList": default_lectures(open_at)}})
    assert first.reserve_start is not None
    assert first.reserve_start.timestamp() == pytest.approx(open_at, abs=1)
