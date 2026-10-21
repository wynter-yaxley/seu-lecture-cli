"""Latency bookkeeping helpers and the shared error types."""

from __future__ import annotations

import pytest

from seu_lecture.core import errors
from seu_lecture.core.metrics import (
    Metrics,
    percentile,
    summarize,
)

# ---- percentile / summarize --------------------------------------------------------------

def test_percentile_edge_cases() -> None:
    assert percentile([], 50) is None
    assert percentile([7.0], 99) == 7.0
    assert percentile([3, 1, 2], 0) == 1
    assert percentile([3, 1, 2], 100) == 3


def test_percentile_interpolates_linearly() -> None:
    assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert percentile(range(1, 101), 95) == pytest.approx(95.05)
    assert percentile(range(1, 101), 99) == pytest.approx(99.01)
    assert percentile([10, 20], 25) == pytest.approx(12.5)




def test_summarize() -> None:
    summary = summarize([1.0, 2.0, 3.0, 4.0])
    assert summary == {"count": 4, "p50": 2.5, "p95": 3.85, "p99": 3.97, "min": 1.0, "max": 4.0,
                       "mean": 2.5}






# ---- Metrics ----------------------------------------------------------------------------------

def test_metrics_series_and_summary() -> None:
    metrics = Metrics()
    assert metrics.last("x") is None
    for value in (3.0, 1.0, 2.0):
        metrics.add("b", value)
    metrics.add("a", 5.0)
    assert metrics.last("b") == 2.0
    summary = metrics.summary()
    assert list(summary) == ["a", "b"]  # sorted by name
    assert summary["b"]["p50"] == 2.0
    assert summary["a"]["count"] == 1


def test_metrics_timer_records_even_when_the_block_raises() -> None:
    metrics = Metrics()
    with metrics.timer("ok"):
        pass
    with pytest.raises(RuntimeError), metrics.timer("boom"):
        raise RuntimeError
    assert len(metrics.series["ok"]) == len(metrics.series["boom"]) == 1
    assert metrics.last("boom") is not None
    assert metrics.last("boom") >= 0




# ---- errors -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("cls", "code", "exit_code"), [
    (errors.SeuLectureError, "INTERNAL_ERROR", 1),
    (errors.ConfigError, "CONFIG_ERROR", 2),
    (errors.AuthRequired, "AUTH_REQUIRED", 3),
    (errors.AuthFailed, "AUTH_FAILED", 3),
    (errors.SmsRequired, "SMS_REQUIRED", 3),
    (errors.SessionExpired, "SESSION_EXPIRED", 3),
    (errors.NetworkError, "NETWORK_ERROR", 4),
    (errors.NetworkTimeout, "NETWORK_TIMEOUT", 4),
    (errors.ProtocolError, "PROTOCOL_ERROR", 5),
    (errors.CaptchaError, "CAPTCHA_ERROR", 5),
    (errors.NotFound, "NOT_FOUND", 6),
    (errors.TaskRunning, "TASK_RUNNING", 7),
    (errors.ConfirmationRequired, "CONFIRMATION_REQUIRED", 12),
])
def test_error_codes_and_exit_codes(cls: type[errors.SeuLectureError], code: str,
                                    exit_code: int) -> None:
    err = cls()
    assert (err.code, err.exit_code) == (code, exit_code)
    assert err.message == code  # empty message falls back to the code
    assert isinstance(err, errors.SeuLectureError)


def test_error_to_dict() -> None:
    plain = errors.NotFound("gone").to_dict()
    assert plain == {"code": "NOT_FOUND", "message": "gone", "retryable": False}
    detailed = errors.NotFound("gone", id="x").to_dict()
    assert detailed["detail"] == {"id": "x"}
    assert errors.SessionExpired().to_dict()["retryable"] is True


def test_network_error_delivery_flag() -> None:
    assert errors.NetworkError("x").maybe_delivered is True
    never_sent = errors.NetworkTimeout("x", maybe_delivered=False, http_status=0)
    assert never_sent.maybe_delivered is False
    assert never_sent.detail == {"http_status": 0}
    assert isinstance(never_sent, errors.NetworkError)
    assert never_sent.retryable
