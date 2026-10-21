"""Settings loading, state files, the single-task lock and task status."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import Env

from seu_lecture.config import (
    load_account,
    load_settings,
    pid_alive,
    read_json,
    save_account,
    task_lock,
    write_json,
)
from seu_lecture.core.errors import AuthRequired, ConfigError, TaskRunning
from seu_lecture.core.lecture import SHANGHAI
from seu_lecture.core.reserve import Outcome, ReserveOptions
from seu_lecture.mock import LECTURE_ID


@pytest.fixture
def home(tmp_path: Path) -> dict[str, str]:
    return {"SEU_LECTURE_HOME": str(tmp_path / "home")}


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


# ---- load_settings -------------------------------------------------------------------

def test_defaults_without_a_config_file(tmp_path: Path, home: dict[str, str]) -> None:
    settings = load_settings(env=home)
    assert settings.config_dir == settings.state_dir == tmp_path / "home"
    assert settings.reserve == {}
    assert settings.base_url is None
    assert settings.username is None
    assert (settings.connect_timeout, settings.read_timeout) == (5.0, 10.0)
    assert settings.submit_format == "form"
    assert settings.status_file.parent == settings.state_dir


def test_toml_sections_are_loaded(tmp_path: Path, home: dict[str, str]) -> None:
    path = write_config(tmp_path, """
[network]
connect_timeout = 2
read_timeout = 4.5

[protocol]
module = "hdyy_bk"
submit_format = "paramJson"

[captcha]
length = 4
digits = true

[reserve]
offset_ms = 75
clock = "local"
max_attempts = 4
compensate_latency = false
verify_interval_ms = 20
""")
    settings = load_settings(path, env=home)
    assert (settings.connect_timeout, settings.read_timeout) == (2.0, 4.5)
    assert settings.submit_format == "paramJson"
    assert (settings.captcha_length, settings.captcha_digits) == (4, True)
    assert settings.reserve == {"offset_ms": 75.0, "clock": "local", "max_attempts": 4,
                                "compensate_latency": False, "verify_interval_ms": 20.0}
    options = ReserveOptions(lecture_id="x", **settings.reserve)
    options.validate()
    assert options.offset_ms == 75.0
    assert options.compensate_latency is False


def test_config_path_from_environment(tmp_path: Path, home: dict[str, str]) -> None:
    path = write_config(tmp_path, "[reserve]\nmax_attempts = 3\n")
    settings = load_settings(env={**home, "SEU_LECTURE_CONFIG": str(path)})
    assert settings.reserve == {"max_attempts": 3}


def test_unknown_reserve_key_is_a_config_error(tmp_path: Path, home: dict[str, str]) -> None:
    path = write_config(tmp_path, "[reserve]\nturbo = true\n")
    with pytest.raises(ConfigError, match="turbo") as info:
        load_settings(path, env=home)
    assert info.value.exit_code == 2


@pytest.mark.parametrize("body", [
    "[reserve]\nmax_attempts = \"many\"\n",
    "[reserve]\noffset_ms = \"soon\"\n",
    "[reserve]\nmax_attempts = [1]\n",
    "[protocol]\nsubmit_format = \"xml\"\n",
    "[reserve\nbroken toml",
])
def test_invalid_config_content_is_a_config_error(tmp_path: Path, home: dict[str, str],
                                                  body: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write_config(tmp_path, body), env=home)


def test_explicit_missing_config_file_is_an_error(tmp_path: Path, home: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml", env=home)


@pytest.mark.parametrize("url", [
    "https://ehall.example.edu.cn",
    "http://127.0.0.1",
    "http://127.0.0.1:8080",
    "http://localhost:9000/x",
    "http://[::1]:8000",
])
def test_acceptable_base_urls(home: dict[str, str], url: str) -> None:
    assert load_settings(env={**home, "SEU_LECTURE_BASE_URL": url}).base_url == url


@pytest.mark.parametrize("url", [
    "http://example.com",
    "http://ehall.seu.edu.cn",
    "http://127.0.0.1.evil.example",
    "ftp://127.0.0.1",
    "ws://localhost",
    "127.0.0.1:8000",
])
def test_base_url_must_be_https_or_loopback_http(home: dict[str, str], url: str) -> None:
    with pytest.raises(ConfigError, match="base_url"):
        load_settings(env={**home, "SEU_LECTURE_BASE_URL": url})


def test_base_url_from_toml_and_env_override(tmp_path: Path, home: dict[str, str]) -> None:
    path = write_config(tmp_path, 'base_url = "http://127.0.0.1:1"\n')
    assert load_settings(path, env=home).base_url == "http://127.0.0.1:1"
    overridden = load_settings(path, env={**home, "SEU_LECTURE_BASE_URL": "https://x.example"})
    assert overridden.base_url == "https://x.example"
    bad = write_config(tmp_path, 'base_url = "http://example.com"\n')
    with pytest.raises(ConfigError):
        load_settings(bad, env=home)


def test_username_from_env_or_saved_account(home: dict[str, str]) -> None:
    settings = load_settings(env=home)
    assert settings.username is None
    save_account(settings, username="saved-user")
    assert load_settings(env=home).username == "saved-user"
    assert load_settings(env={**home, "SEU_LECTURE_USERNAME": "env-user"}).username == "env-user"


# ---- JSON state files ------------------------------------------------------------------------

def test_json_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "dir" / "state.json"
    write_json(path, {"title": "讲座", "n": 1})
    assert read_json(path) == {"title": "讲座", "n": 1}
    assert "讲座" in path.read_text(encoding="utf-8")  # not \u-escaped
    assert not list(path.parent.glob("*.tmp"))


def test_read_json_tolerates_missing_and_corrupt_files(tmp_path: Path) -> None:
    assert read_json(tmp_path / "missing.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert read_json(bad) == {}
    bad.write_text("[1, 2]", encoding="utf-8")
    assert read_json(bad) == {}


def test_save_account_merges_fields(home: dict[str, str]) -> None:
    settings = load_settings(env=home)
    save_account(settings, username="u")
    save_account(settings, fingerprint="abc")
    assert load_account(settings) == {"username": "u", "fingerprint": "abc"}


def test_pid_alive(dead_pid: int) -> None:
    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(-5)
    assert not pid_alive(dead_pid)


# ---- task_lock -------------------------------------------------------------------------------

def test_task_lock_prevents_a_second_holder(tmp_path: Path) -> None:
    lock = tmp_path / "state" / "reserve.lock"
    with task_lock(lock):
        assert lock.read_text() == str(os.getpid())
        with pytest.raises(TaskRunning) as info, task_lock(lock):
            pytest.fail("second acquire must not succeed")
        assert info.value.detail == {"pid": os.getpid()}
        assert info.value.exit_code == 7
        assert lock.exists()  # the failed attempt must not steal or delete the live lock
    assert not lock.exists()
    with task_lock(lock):  # released lock can be taken again
        pass


def test_task_lock_released_when_the_body_raises(tmp_path: Path) -> None:
    lock = tmp_path / "reserve.lock"
    with pytest.raises(RuntimeError), task_lock(lock):
        raise RuntimeError
    assert not lock.exists()


def test_task_lock_removes_stale_lock_of_dead_process(tmp_path: Path, dead_pid: int) -> None:
    lock = tmp_path / "reserve.lock"
    lock.write_text(str(dead_pid))
    with task_lock(lock):
        assert lock.read_text() == str(os.getpid())
    assert not lock.exists()


@pytest.mark.parametrize("content", ["", "garbage", "0"])
def test_task_lock_treats_old_unreadable_lock_as_stale(tmp_path: Path, content: str) -> None:
    lock = tmp_path / "reserve.lock"
    lock.write_text(content)
    old = lock.stat().st_mtime - 60
    os.utime(lock, (old, old))
    with task_lock(lock):
        assert lock.read_text() == str(os.getpid())


def test_task_lock_respects_fresh_empty_lock(tmp_path: Path) -> None:
    # Another process may be between creating the lock and writing its pid.
    lock = tmp_path / "reserve.lock"
    lock.write_text("")
    with pytest.raises(TaskRunning), task_lock(lock):
        pass
    assert lock.exists()


# ---- LectureService status and locking -------------------------------------------------------

def test_task_status_idle(env: Env) -> None:
    assert env.service().task_status() == {"state": "idle"}


def test_task_status_running_and_stale(env: Env, dead_pid: int) -> None:
    service = env.service()
    write_json(env.settings.status_file, {"state": "running", "pid": os.getpid(), "phase": "wait"})
    assert service.task_status()["state"] == "running"
    write_json(env.settings.status_file, {"state": "running", "pid": dead_pid})
    assert service.task_status()["state"] == "stale"
    write_json(env.settings.status_file, {"state": "running"})  # no pid at all
    assert service.task_status()["state"] == "stale"


@pytest.mark.parametrize("state", ["finished", "error"])
def test_terminal_task_states_are_never_stale(env: Env, dead_pid: int, state: str) -> None:
    write_json(env.settings.status_file, {"state": state, "pid": dead_pid})
    assert env.service().task_status()["state"] == state


def test_reserve_records_status_and_releases_lock(env: Env) -> None:
    service = env.service()
    service.login(env.cfg.username, env.cfg.password)
    events: list[dict[str, object]] = []
    options = ReserveOptions(lecture_id=LECTURE_ID, at=datetime.now(SHANGHAI) - timedelta(seconds=1),
                             clock="local", offset_ms=0, compensate_latency=False)
    result = service.reserve(options, emit=events.append)
    assert result.outcome is Outcome.CONFIRMED
    status = service.task_status()
    assert status["state"] == "finished"
    assert status["result"]["outcome"] == "confirmed"
    assert status["lecture_id"] == LECTURE_ID
    assert events[-1]["event"] == "result"
    assert not env.settings.lock_file.exists()


def test_reserve_refuses_while_another_task_holds_the_lock(env: Env) -> None:
    service = env.service()
    service.login(env.cfg.username, env.cfg.password)
    with task_lock(env.settings.lock_file), pytest.raises(TaskRunning):
        service.reserve(ReserveOptions(lecture_id=LECTURE_ID))
    assert env.campus.calls.get("addReservation.do", 0) == 0


def test_reserve_error_is_recorded_in_status(env: Env) -> None:
    env.settings.username = None
    service = env.service()
    with pytest.raises(AuthRequired):
        service.reserve(ReserveOptions(lecture_id=LECTURE_ID))
    status = service.task_status()
    assert status["state"] == "error"
    assert status["error"]["code"] == "AUTH_REQUIRED"
    assert not env.settings.lock_file.exists()
