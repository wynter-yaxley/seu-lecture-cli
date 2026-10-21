"""Settings (TOML + environment), per-user paths and small state files.

Secrets never go into these files: the password and session cookies are kept in
the OS keyring only (see ``core/auth.py``). ``account.json`` holds the username
and the random device fingerprint; ``status.json`` holds the latest task status.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from seu_lecture.core.errors import ConfigError, TaskRunning

APP = "seu-lecture"
_RESERVE_KEYS = {
    "offset_ms": float, "clock": str, "compensate_latency": bool, "prefetch_captcha": bool,
    "captcha_lead_ms": float, "captcha_tries": int, "clock_sync_lead_s": float,
    "session_check_lead_s": float, "watch_minutes": float, "watch_interval_s": float,
    "keepalive_s": float, "max_attempts": int, "max_resubmits": int,
    "min_submit_interval_ms": float, "not_open_window_ms": float,
    "rate_limit_backoff_s": float, "verify_attempts": int, "verify_interval_ms": float,
}


def _default_dirs(env: Mapping[str, str]) -> tuple[Path, Path]:
    if home := env.get("SEU_LECTURE_HOME"):
        return Path(home), Path(home)
    user = Path.home()
    if sys.platform == "win32":
        roaming = Path(env.get("APPDATA") or user / "AppData" / "Roaming")
        local = Path(env.get("LOCALAPPDATA") or user / "AppData" / "Local")
        return roaming / APP, local / APP
    if sys.platform == "darwin":
        base = user / "Library" / "Application Support" / APP
        return base, base
    config = Path(env.get("XDG_CONFIG_HOME") or user / ".config")
    state = Path(env.get("XDG_STATE_HOME") or user / ".local" / "state")
    return config / APP, state / APP


@dataclass
class Settings:
    config_dir: Path
    state_dir: Path
    config_file: Path
    base_url: str | None = None
    module: str = "hdyy_bk"
    submit_format: str = "form"
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    captcha_length: int | None = 4  # 0 / None: accept any length
    username: str | None = None
    reserve: dict[str, Any] = field(default_factory=dict)

    @property
    def account_file(self) -> Path:
        return self.state_dir / "account.json"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "reserve.lock"


def load_settings(config_file: Path | None = None,
                  env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    config_dir, state_dir = _default_dirs(env)
    path = config_file or Path(env.get("SEU_LECTURE_CONFIG") or config_dir / "config.toml")
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read config file: {exc}", path=str(path)) from None
    elif config_file is not None:
        raise ConfigError("config file not found", path=str(path))

    s = Settings(config_dir=config_dir, state_dir=state_dir, config_file=path)
    net, proto = data.get("network", {}), data.get("protocol", {})
    cap, res = data.get("captcha", {}), data.get("reserve", {})
    s.connect_timeout = float(net.get("connect_timeout", s.connect_timeout))
    s.read_timeout = float(net.get("read_timeout", s.read_timeout))
    s.module = str(proto.get("module", s.module))
    s.submit_format = str(proto.get("submit_format", s.submit_format))
    s.captcha_length = cap.get("length", s.captcha_length)
    for key, value in res.items():
        if key not in _RESERVE_KEYS:
            raise ConfigError(f"unknown [reserve] option: {key}")
        try:
            s.reserve[key] = _RESERVE_KEYS[key](value)
        except (TypeError, ValueError):
            raise ConfigError(f"invalid value for [reserve] {key}") from None
    if s.submit_format not in ("form", "paramJson"):
        raise ConfigError("protocol.submit_format must be 'form' or 'paramJson'")

    s.base_url = env.get("SEU_LECTURE_BASE_URL") or data.get("base_url")
    if s.base_url:
        parts = urlsplit(s.base_url)
        local = parts.hostname in ("127.0.0.1", "localhost", "::1")
        if not (parts.scheme == "https" or (parts.scheme == "http" and local)):
            raise ConfigError("base_url must be https, or http on localhost (mock server)")
    s.username = env.get("SEU_LECTURE_USERNAME") or load_account(s).get("username")
    return s


# ---- small JSON state files ---------------------------------------------

def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_account(settings: Settings) -> dict[str, Any]:
    return read_json(settings.account_file)


def save_account(settings: Settings, **fields: Any) -> dict[str, Any]:
    account = {**load_account(settings), **fields}
    write_json(settings.account_file, account)
    return account


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would *terminate* the process on Windows.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def task_lock(path: Path) -> Iterator[None]:
    """One reservation task per user at a time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
                pid = int(path.read_text().strip() or 0)
            except OSError:
                pid, age = 0, 0.0
            except ValueError:
                pid = 0
            # An empty, fresh lock is another process between create and pid write.
            if pid_alive(pid) or (pid == 0 and age < 10):
                raise TaskRunning("another reservation task is running", pid=pid) from None
            path.unlink(missing_ok=True)  # stale lock from a crashed run
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        break
    else:
        raise TaskRunning("could not acquire the task lock")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
