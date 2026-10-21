"""Clock-candidate planning: the Date header may not be the application's clock."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest

from seu_lecture.core.auth import CasLogin, Credentials
from seu_lecture.core.captcha import FixedSolver
from seu_lecture.core.client import CampusClient, Endpoints
from seu_lecture.core.clock import ClockEstimate
from seu_lecture.core.lecture import SHANGHAI
from seu_lecture.core.reserve import Outcome, ReservationTask, ReserveOptions
from seu_lecture.mock import LECTURE_ID, FakeCampus, MockConfig, MockServer, mock_transport


def _task(clock: str, estimate: ClockEstimate) -> ReservationTask:
    client = CampusClient(Endpoints.for_base("http://127.0.0.1"), transport=mock_transport(FakeCampus()))
    task = ReservationTask(client, FixedSolver(), ReserveOptions(lecture_id=LECTURE_ID, clock=clock),
                           relogin=lambda: None)
    task.clock = estimate
    return task


@pytest.mark.parametrize(("clock", "offset_ms", "error_ms", "expected"), [
    ("local", 375_000, 30, [0]),
    ("auto", 375_000, 30, [-375, 0]),        # disagree by more than the retry window: both
    ("auto", -375_000, 30, [0, 375]),
    ("auto", 1_000, 30, [-1]),               # within the window: earlier one only
    ("auto", -1_000, 30, [0]),
    ("auto", 20, 30, [0]),                   # skew not distinguishable from noise
    ("auto", 375_000, 800, [0]),             # estimate too uncertain
    ("server", 375_000, 30, [-375]),
])
def test_plan_candidates(clock: str, offset_ms: float, error_ms: float, expected: list[float]) -> None:
    task = _task(clock, ClockEstimate(offset_ms, error_ms, 5.0, 6))
    target = time.time() + 2.5  # too close to re-estimate, so the preset estimate is used
    plan = task._plan(target)
    assert [round(c - target, 3) for c in plan] == expected


@pytest.mark.parametrize(("app_offset", "extra_submits"), [(0.0, 1), (4.0, 0)])
def test_front_proxy_date_skew_end_to_end(app_offset: float, extra_submits: int) -> None:
    """Date header +4 s. Whichever clock the application uses, the task succeeds."""
    config = MockConfig(clock_offset_s=app_offset, date_skew_s=4.0 - app_offset)
    campus = FakeCampus(config)
    with MockServer(campus) as base:
        open_at = time.time() + 8  # in application-clock terms
        config.open_at = open_at
        client = CampusClient(Endpoints.for_base(base))
        CasLogin(client).login(Credentials(config.username, config.password), fingerprint="fp")
        events: list[dict[str, Any]] = []
        options = ReserveOptions(lecture_id=LECTURE_ID, at=datetime.fromtimestamp(open_at, SHANGHAI),
                                 not_open_window_ms=1000, captcha_lead_ms=500)
        result = ReservationTask(client, FixedSolver("1234"), options, relogin=lambda: None,
                                 emit=events.append).run()
        client.close()
    assert result.outcome is Outcome.CONFIRMED
    assert len(campus.submit_times) == 1 + extra_submits
    plan = next(e for e in events if e["event"] == "plan")
    assert len(plan["candidates"]) == 2
    assert any(e["event"] == "candidate_not_open" for e in events) == bool(extra_submits)
    # The accepted submit reached the server shortly after the application-clock opening.
    assert 0 <= campus.submit_times[-1] - open_at < 0.5
