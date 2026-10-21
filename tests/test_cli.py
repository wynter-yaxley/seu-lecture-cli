"""CLI contract: JSON envelopes, JSONL event streams, exit codes, no ANSI in machine mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from conftest import Env
from typer.testing import Result

from seu_lecture import __version__
from seu_lecture.api import LectureService
from seu_lecture.core.lecture import SHANGHAI
from seu_lecture.mock import LECTURE_ID

Invoke = Callable[..., Result]


def lines(result: Result) -> list[dict[str, Any]]:
    """Every non-empty stdout line parsed as JSON (fails if any line is not JSON)."""
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def envelope(result: Result) -> dict[str, Any]:
    [body] = lines(result)
    assert body["schema_version"] == "1"
    return body


def past(seconds: float = 5.0) -> str:
    return datetime.fromtimestamp(time.time() - seconds, SHANGHAI).isoformat()


def assert_clean_machine_output(result: Result) -> None:
    assert "\x1b" not in result.stdout, "ANSI escape in --json stdout"
    assert "\x1b" not in result.stderr


# ---- basics ---------------------------------------------------------------------------------

def test_version(invoke: Invoke) -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"seu-lecture {__version__}"


def test_help_lists_commands_and_exit_codes(invoke: Invoke) -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("list", "schedule", "reserve", "status", "auth"):
        assert command in result.stdout
    assert "退出码" in result.stdout


def test_usage_error_exits_2(invoke: Invoke) -> None:
    assert invoke("no-such-command").exit_code == 2
    assert invoke("list", "--no-such-flag").exit_code == 2


# ---- read-only commands -----------------------------------------------------------------------

def test_list_json_envelope(account: Env, invoke: Invoke) -> None:
    result = invoke("list", "--json")
    assert result.exit_code == 0
    assert_clean_machine_output(result)
    body = envelope(result)
    assert body["ok"] is True
    assert body["command"] == "list"
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 2
    first = body["data"][0]
    assert first["id"] == LECTURE_ID
    assert first["capacity"] == 50
    assert first["state"] in {"open", "not_open", "full", "closed", "unknown"}
    assert set(first) >= {"title", "lecture_time", "reserve_start", "remaining", "location"}


def test_list_json_keyword_filter(account: Env, invoke: Invoke) -> None:
    body = envelope(invoke("list", "--json", "--keyword", "心理"))
    assert [x["id"] for x in body["data"]] == ["00aa11bb22cc33dd44ee55ff66778899"]


def test_list_human_output_is_a_table(account: Env, invoke: Invoke) -> None:
    result = invoke("list")
    assert result.exit_code == 0
    assert "讲座列表" in result.stdout
    assert LECTURE_ID[:10] in result.stdout


def test_show_by_prefix_and_not_found(account: Env, invoke: Invoke) -> None:
    body = envelope(invoke("show", LECTURE_ID[:8], "--json"))
    assert body["data"]["id"] == LECTURE_ID
    assert "raw" not in body["data"]
    assert envelope(invoke("show", LECTURE_ID, "--raw", "--json"))["data"]["raw"]["WID"] == LECTURE_ID

    missing = invoke("show", "deadbeefdeadbeef", "--json")
    assert missing.exit_code == 6
    assert envelope(missing)["error"]["code"] == "NOT_FOUND"


def test_reservations_json(account: Env, invoke: Invoke) -> None:
    assert envelope(invoke("reservations", "--json"))["data"] == []
    account.campus.reservations.add(LECTURE_ID)
    [mine] = envelope(invoke("reservations", "--json"))["data"]
    assert mine["lecture_id"] == LECTURE_ID
    assert mine["cancelled"] is False


def test_status_json_is_idle_initially(account: Env, invoke: Invoke) -> None:
    result = invoke("status", "--json")
    assert result.exit_code == 0
    body = envelope(result)
    assert body["ok"] is True
    assert body["command"] == "status"
    assert body["data"] == {"state": "idle"}





# ---- error envelopes ------------------------------------------------------------------------------

def test_error_envelope_without_account(cli_env: Env, invoke: Invoke) -> None:
    result = invoke("list", "--json")
    assert result.exit_code == 3
    assert_clean_machine_output(result)
    body = envelope(result)
    assert body["ok"] is False
    assert body["command"] == "list"
    assert body["error"]["code"] == "AUTH_REQUIRED"
    assert body["error"]["retryable"] is False
    assert body["exit_code"] == 3
    assert "data" not in body


def test_error_in_human_mode_goes_to_stderr(cli_env: Env, invoke: Invoke) -> None:
    result = invoke("list")
    assert result.exit_code == 3
    assert result.stdout == ""
    assert "AUTH_REQUIRED" in result.stderr


def test_config_error_exits_2(cli_env: Env, invoke: Invoke, tmp_path: Any) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[reserve]\nturbo = 1\n", encoding="utf-8")
    result = invoke("status", "--json", "--config", str(bad))
    assert result.exit_code == 2
    assert envelope(result)["error"]["code"] == "CONFIG_ERROR"


def test_unexpected_exception_becomes_internal_error(
        account: Env, invoke: Invoke, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: LectureService, **kwargs: Any) -> None:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(LectureService, "lectures", boom)
    result = invoke("list", "--json")
    assert result.exit_code == 1
    error = envelope(result)["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert error["message"] == "unexpected RuntimeError"
    assert "secret internal detail" not in result.stdout
    assert "Traceback" not in result.stdout


def test_keyboard_interrupt_exits_130(
        account: Env, invoke: Invoke, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(self: LectureService, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(LectureService, "lectures", interrupt)
    result = invoke("list", "--json")
    assert result.exit_code == 130
    assert envelope(result)["error"]["code"] == "INTERRUPTED"


# ---- auth -----------------------------------------------------------------------------------------------

def test_auth_login_json_with_password_from_stdin(cli_env: Env, invoke: Invoke) -> None:
    cfg = cli_env.cfg
    result = invoke("auth", "login", "--json", "-u", cfg.username, "--password-stdin",
                    input=cfg.password + "\n")
    assert result.exit_code == 0
    data = envelope(result)["data"]
    assert (data["username"], data["logged_in"], data["password_saved"]) == (
        cfg.username, True, True)
    assert cfg.password not in result.stdout
    assert cli_env.store.get_password(cfg.username) == cfg.password


def test_auth_login_wrong_password_exits_3(cli_env: Env, invoke: Invoke) -> None:
    result = invoke("auth", "login", "--json", "-u", cli_env.cfg.username, "--password-stdin",
                    input="wrong\n")
    assert result.exit_code == 3
    assert envelope(result)["error"]["code"] == "AUTH_FAILED"
    assert "wrong" not in result.stdout.replace("AUTH_FAILED", "")  # password is not echoed


def test_auth_login_without_username_non_interactive(cli_env: Env, invoke: Invoke) -> None:
    result = invoke("auth", "login", "--json", "--password-stdin", input="x\n")
    assert result.exit_code == 2
    assert envelope(result)["error"]["code"] == "CONFIG_ERROR"


def test_auth_login_sms_flow_non_interactive(cli_env: Env, invoke: Invoke) -> None:
    cfg = cli_env.cfg
    cfg.require_sms = True
    first = invoke("auth", "login", "--json", "-u", cfg.username, "--password-stdin",
                   input=cfg.password + "\n")
    assert first.exit_code == 3
    error = envelope(first)["error"]
    assert error["code"] == "SMS_REQUIRED"
    assert error["detail"] == {"sms_sent": True}

    second = invoke("auth", "login", "--json", "-u", cfg.username, "--password-stdin",
                    "--sms-code", cfg.sms_code, input=cfg.password + "\n")
    assert second.exit_code == 0
    assert envelope(second)["data"]["logged_in"] is True


def test_auth_status_and_logout(account: Env, invoke: Invoke) -> None:
    data = envelope(invoke("auth", "status", "--json", "--offline"))["data"]
    assert data["username"] == account.cfg.username
    assert data["password_saved"] is True
    assert data["session_cached"] is False

    assert envelope(invoke("auth", "logout", "--json"))["data"]["password_saved"] is False
    assert account.store.get_password(account.cfg.username) is None


# ---- reserve ----------------------------------------------------------------------------------------------

def test_reserve_json_without_yes_requires_confirmation(account: Env, invoke: Invoke) -> None:
    result = invoke("reserve", LECTURE_ID, "--json")
    assert result.exit_code == 12
    assert_clean_machine_output(result)
    body = envelope(result)
    assert body["ok"] is False
    assert body["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert body["event"] == "error"  # reserve streams: even the failure is a JSONL event
    assert account.campus.calls.get("addReservation.do", 0) == 0
    assert not account.campus.reservations


def test_reserve_json_yes_streams_valid_json_events(account: Env, invoke: Invoke) -> None:
    result = invoke("reserve", LECTURE_ID, "--json", "--yes", "--at", past())
    assert result.exit_code == 0, result.stdout
    assert_clean_machine_output(result)
    events = lines(result)  # raises if any line is not valid JSON
    assert len(events) >= 4
    for event in events:
        assert event["schema_version"] == "1"
        assert "event" in event
        assert "ts" in event
    assert events[-1]["event"] == "result"
    assert events[-1]["outcome"] == "confirmed"
    assert [e["event"] for e in events].count("result") == 1
    assert LECTURE_ID in account.campus.reservations


def test_reserve_json_full_lecture_exits_10(account: Env, invoke: Invoke) -> None:
    account.cfg.capacity = 1
    account.cfg.reserved = 1
    result = invoke("reserve", LECTURE_ID, "--json", "--yes", "--at", past())
    assert result.exit_code == 10
    assert lines(result)[-1]["outcome"] == "full"


def test_reserve_dry_run_needs_no_confirmation_and_does_not_submit(
        account: Env, invoke: Invoke) -> None:
    result = invoke("reserve", LECTURE_ID, "--json", "--dry-run", "--at", past())
    assert result.exit_code == 0
    assert lines(result)[-1]["outcome"] == "dry_run"
    assert account.campus.calls.get("addReservation.do", 0) == 0


def test_reserve_then_status_reports_finished(account: Env, invoke: Invoke) -> None:
    assert invoke("reserve", LECTURE_ID, "--json", "--yes", "--at", past()).exit_code == 0
    data = envelope(invoke("status", "--json"))["data"]
    assert data["state"] == "finished"
    assert data["result"]["outcome"] == "confirmed"
    assert data["lecture_id"] == LECTURE_ID


def test_reserve_already_reserved_exits_0(account: Env, invoke: Invoke) -> None:
    account.campus.reservations.add(LECTURE_ID)
    result = invoke("reserve", LECTURE_ID, "--json", "--yes", "--at", past())
    assert result.exit_code == 0
    assert lines(result)[-1]["outcome"] == "already_reserved"


def test_reserve_unknown_lecture_streams_error_event(account: Env, invoke: Invoke) -> None:
    result = invoke("reserve", "0123456789abcdef", "--json", "--yes", "--at", past())
    assert result.exit_code == 6
    last = lines(result)[-1]
    assert (last["event"], last["ok"], last["error"]["code"]) == ("error", False, "NOT_FOUND")
    assert_clean_machine_output(result)


def test_reserve_bad_at_and_bad_options_exit_2(account: Env, invoke: Invoke) -> None:
    bad_at = invoke("reserve", LECTURE_ID, "--json", "--yes", "--at", "tomorrow-ish")
    assert bad_at.exit_code == 2
    assert lines(bad_at)[-1]["error"]["code"] == "CONFIG_ERROR"
    bad_attempts = invoke("reserve", LECTURE_ID, "--json", "--yes", "--max-attempts", "99")
    assert bad_attempts.exit_code == 2


def test_reserve_honours_reserve_section_of_the_config(
        account: Env, invoke: Invoke, tmp_path: Any) -> None:
    config = tmp_path / "c.toml"
    config.write_text("[reserve]\nmax_attempts = 1\nclock = \"local\"\n", encoding="utf-8")
    account.cfg.captcha_failures = 5
    result = invoke("reserve", LECTURE_ID, "--json", "--yes", "--at", past(), "--config", str(config))
    assert result.exit_code == 1  # one attempt, captcha rejected, budget exhausted
    assert lines(result)[-1]["attempts"] == 1


def test_reserve_human_mode_prints_outcome_on_stdout(account: Env, invoke: Invoke) -> None:
    result = invoke("reserve", LECTURE_ID, "--yes", "--at", past())
    assert result.exit_code == 0
    assert "confirmed" in result.stdout
    assert "{" not in result.stdout  # no JSON in human mode


def test_help_works_on_a_non_utf8_pipe() -> None:
    # Regression (Windows CI): cp1252 stdout could not encode the Chinese help.
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run([sys.executable, "-m", "seu_lecture", "schedule", "--help"],
                            capture_output=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "课表" in result.stdout.decode("utf-8")
