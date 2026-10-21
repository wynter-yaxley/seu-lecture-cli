"""Output contract: JSON envelope, JSONL events, exit-code table and human rendering."""

from __future__ import annotations

import json
from typing import Any

import pytest

from seu_lecture.core import errors
from seu_lecture.core.reserve import OUTCOME_EXIT
from seu_lecture.output import EXIT_CODES, HINTS, SCHEMA_VERSION, Output, render_event


def stdout_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out
    assert out.endswith("\n")
    [line] = out.splitlines()
    return json.loads(line)


def test_success_envelope_is_compact_utf8_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert Output(True).success("list", [{"title": "讲座"}]) == 0
    out = capsys.readouterr().out
    assert out == '{"schema_version":"1","ok":true,"command":"list","data":[{"title":"讲座"}]}\n'
    assert "\x1b" not in out


def test_failure_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    code = Output(True).failure("list", errors.NotFound("gone", id="x"))
    assert code == 6
    assert stdout_json(capsys) == {
        "schema_version": SCHEMA_VERSION, "ok": False, "command": "list", "exit_code": 6,
        "error": {"code": "NOT_FOUND", "message": "gone", "retryable": False,
                  "detail": {"id": "x"}}}


def test_streamed_failure_is_an_error_event(capsys: pytest.CaptureFixture[str]) -> None:
    Output(True).failure("reserve", errors.ConfirmationRequired("pass --yes"), stream=True)
    body = stdout_json(capsys)
    assert body["event"] == "error"
    assert body["ok"] is False
    assert "ts" in body
    assert body["exit_code"] == 12


def test_event_line_has_schema_version_and_shanghai_timestamp(
        capsys: pytest.CaptureFixture[str]) -> None:
    Output(True).event({"event": "attempt", "wall": 1_700_000_000.5, "n": 1})
    assert stdout_json(capsys) == {
        "schema_version": "1", "ts": "2023-11-15T06:13:20.500+08:00", "event": "attempt",
        "wall": 1_700_000_000.5, "n": 1}


def test_json_mode_keeps_stdout_clean_of_diagnostics(
        capsys: pytest.CaptureFixture[str]) -> None:
    Output(True).info("hello")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_human_mode_writes_diagnostics_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    out = Output(False)
    out.info("hello")
    out.failure("list", errors.AuthRequired("no account configured"))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello" in captured.err
    assert "AUTH_REQUIRED" in captured.err
    assert "no account configured" in captured.err


def test_human_success_without_renderer_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    Output(False).success("x", {"a": 1})
    assert json.loads(capsys.readouterr().out) == {"a": 1}


def test_every_exit_code_in_use_is_documented() -> None:
    classes = [errors.ConfigError, errors.AuthRequired, errors.AuthFailed, errors.SmsRequired,
               errors.SessionExpired, errors.NetworkError, errors.ProtocolError,
               errors.CaptchaError, errors.NotFound, errors.TaskRunning,
               errors.ConfirmationRequired, errors.SeuLectureError]
    used = {cls.exit_code for cls in classes} | set(OUTCOME_EXIT.values())
    assert used <= set(EXIT_CODES)


def test_hints_exist_only_for_real_error_codes() -> None:
    codes = {cls.code for cls in vars(errors).values()
             if isinstance(cls, type) and issubclass(cls, errors.SeuLectureError)}
    assert set(HINTS) <= codes


@pytest.mark.parametrize("event", [
    {"event": "phase", "phase": "prepare"},
    {"event": "phase", "phase": "wait"},
    {"event": "phase", "phase": "fire", "late_ms": 0.2},
    {"event": "prepared", "lecture": {"title": "t", "remaining": 3}, "at": "x"},
    {"event": "clock", "offset_ms": 1, "error_ms": 2, "rtt_ms": 3, "applied_offset_ms": 0},
    {"event": "armed", "fire_at": 1_700_000_000.0, "rtt_ms": 3},
    {"event": "attempt", "n": 1, "reply": "full", "message": "m"},
    {"event": "warning", "message": "careful"},
    {"event": "clock_jump", "hint": "jumped"},
    {"event": "relogin", "reason": "expired"},
    {"event": "resubmit"},
    {"event": "keepalive"},
])
def test_render_event_returns_a_line(event: dict[str, Any]) -> None:
    assert render_event(event)


def test_render_event_ignores_unknown_events() -> None:
    assert render_event({"event": "result"}) is None
    assert render_event({"event": "something-new"}) is None


def test_human_error_with_square_brackets_does_not_crash(
        capsys: pytest.CaptureFixture[str]) -> None:
    Output(False).failure("login", errors.AuthFailed("rejected [/oops] by server"))
    assert "[/oops]" in capsys.readouterr().err
