"""Front-end-neutral business API.

The CLI is a thin layer over this module, and a future MCP adapter can call the
same methods directly (returning plain dicts / dataclasses, raising
``SeuLectureError``) instead of shelling out to the CLI.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import socket
import ssl
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from seu_lecture import __version__
from seu_lecture.config import (
    Settings,
    load_account,
    pid_alive,
    read_json,
    save_account,
    task_lock,
    write_json,
)
from seu_lecture.core.auth import CasLogin, Credentials, CredentialStore, new_fingerprint
from seu_lecture.core.captcha import OcrSolver, Solver
from seu_lecture.core.client import CampusClient, Endpoints
from seu_lecture.core.clock import estimate_offset
from seu_lecture.core.errors import (
    AuthFailed,
    AuthRequired,
    NetworkError,
    ProtocolError,
    SeuLectureError,
    SmsRequired,
)
from seu_lecture.core.lecture import (
    SHANGHAI,
    filter_lectures,
    find_lecture,
    load_lectures,
    load_reservations,
)
from seu_lecture.core.reserve import ReservationTask, ReserveOptions, TaskResult

PasswordPrompt = Callable[[], str | None]
SmsPrompt = Callable[[str], str | None]
_TERMINAL = {"finished", "error"}


class LectureService:
    def __init__(self, settings: Settings, *, store: CredentialStore | None = None,
                 transport: httpx.BaseTransport | None = None,
                 password_prompt: PasswordPrompt | None = None,
                 sms_prompt: SmsPrompt | None = None,
                 solver: Solver | None = None) -> None:
        self.settings = settings
        self.store = store or CredentialStore()
        self.transport = transport
        self.password_prompt = password_prompt
        self.sms_prompt = sms_prompt
        self.solver = solver

    # ---- plumbing -----------------------------------------------------------

    def endpoints(self) -> Endpoints:
        s = self.settings
        if s.base_url:
            return Endpoints.for_base(s.base_url, module=s.module)
        return Endpoints(module=s.module)

    def new_client(self) -> CampusClient:
        s = self.settings
        return CampusClient(self.endpoints(), connect_timeout=s.connect_timeout,
                            read_timeout=s.read_timeout, submit_format=s.submit_format,
                            transport=self.transport)

    def _username(self) -> str:
        if not self.settings.username:
            raise AuthRequired("no account configured")
        return self.settings.username

    def _fingerprint(self) -> str:
        account = load_account(self.settings)
        if not account.get("fingerprint"):
            account = save_account(self.settings, fingerprint=new_fingerprint())
        return account["fingerprint"]

    def _password(self, username: str, *, prompt: bool = True) -> str | None:
        return (os.environ.get("SEU_LECTURE_PASSWORD")
                or self.store.get_password(username)
                or (self.password_prompt() if prompt and self.password_prompt else None))

    def _login(self, client: CampusClient, creds: Credentials,
               sms_code: str | None = None) -> None:
        client.http.cookies.clear()
        CasLogin(client).login(creds, fingerprint=self._fingerprint(), sms_code=sms_code)
        self.store.set_session(creds.username, client.export_cookies())

    def open_session(self, password: str | None = None) -> CampusClient:
        """Logged-in client: cached keyring session first, password login otherwise."""
        username = self._username()
        client = self.new_client()
        try:
            cached = self.store.get_session(username)
            if cached:
                client.import_cookies(cached)
                if client.session_alive():
                    return client
            password = password or self._password(username)
            if not password:
                raise AuthRequired("session expired and no password available")
            self._login(client, Credentials(username, password))
            return client
        except BaseException:
            client.close()
            raise

    # ---- auth ---------------------------------------------------------------

    def login(self, username: str, password: str, *, sms_code: str | None = None,
              save_password: bool = True) -> dict[str, Any]:
        creds = Credentials(username, password)
        save_account(self.settings, username=username)
        self.settings.username = username
        with self.new_client() as client:
            try:
                self._login(client, creds, sms_code)
            except SmsRequired:
                if sms_code:
                    raise AuthFailed("SMS code was not accepted") from None
                info = CasLogin(client).send_sms(username)
                code = self.sms_prompt(info) if self.sms_prompt else None
                if not code:
                    raise SmsRequired("SMS code sent; rerun with --sms-code <code>",
                                      sms_sent=True) from None
                self._login(client, creds, code.strip())
        saved = save_password and self.store.set_password(username, password)
        return {"username": username, "logged_in": True, "password_saved": bool(saved),
                "session_cached": self.store.get_session(username) is not None,
                "keyring": self.store.backend_name}

    def logout(self) -> dict[str, Any]:
        username = self._username()
        self.store.clear(username)
        return {"username": username, "password_saved": False, "session_cached": False}

    def auth_status(self, *, online: bool = True) -> dict[str, Any]:
        username = self.settings.username
        data: dict[str, Any] = {"username": username, "keyring": self.store.backend_name,
                                "password_saved": False, "session_cached": False,
                                "session_valid": None}
        if not username:
            return data
        data["password_saved"] = self.store.get_password(username) is not None
        cached = self.store.get_session(username)
        data["session_cached"] = cached is not None
        if online and cached:
            with self.new_client() as client:
                client.import_cookies(cached)
                data["session_valid"] = client.session_alive()
        return data

    # ---- queries ------------------------------------------------------------

    def lectures(self, *, keyword: str | None = None, available: bool = False,
                 ) -> list[dict[str, Any]]:
        now = datetime.now(SHANGHAI)
        with self.open_session() as client:
            items = load_lectures(client)
        return [x.to_dict(now) for x in filter_lectures(items, keyword=keyword,
                                                        available=available, now=now)]

    def lecture(self, lecture_id: str, *, raw: bool = False) -> dict[str, Any]:
        with self.open_session() as client:
            item = find_lecture(load_lectures(client), lecture_id)
        return item.to_dict(datetime.now(SHANGHAI), raw=raw)

    def reservations(self) -> list[dict[str, Any]]:
        with self.open_session() as client:
            return [r.to_dict() for r in load_reservations(client)]

    # ---- reservation task -----------------------------------------------------

    def reserve(self, options: ReserveOptions,
                emit: Callable[[dict[str, Any]], None] | None = None) -> TaskResult:
        status: dict[str, Any] = {"state": "running", "pid": os.getpid(),
                                  "lecture_id": options.lecture_id, "dry_run": options.dry_run,
                                  "started_at": datetime.now(SHANGHAI).isoformat()}

        def on_event(record: dict[str, Any]) -> None:
            # Called outside the critical section only (events are buffered there).
            kind = record.get("event")
            if kind == "phase":
                status["phase"] = record.get("phase")
            elif kind == "prepared":
                status["lecture"] = record.get("lecture")
                status["target_time"] = record.get("at")
            elif kind == "result":
                status.update(state="finished", result=record)
            status["last_event"] = kind
            status["updated_at"] = datetime.now(SHANGHAI).isoformat()
            write_json(self.settings.status_file, status)
            if emit:
                emit(record)

        with task_lock(self.settings.lock_file):
            write_json(self.settings.status_file, status)
            try:
                username = self._username()
                password = self._password(username)
                client = self.open_session(password)
                creds = Credentials(username, password or "")

                def relogin() -> None:
                    if not creds.password:
                        raise AuthRequired("session expired and no password available")
                    self._login(client, creds)

                solver = self.solver or OcrSolver(length=self.settings.captcha_length,
                                                  digits=self.settings.captcha_digits)
                with client:
                    task = ReservationTask(client, solver, options, relogin=relogin,
                                           emit=on_event)
                    return task.run()
            except SeuLectureError as exc:
                status.update(state="error", error=exc.to_dict())
                write_json(self.settings.status_file, status)
                raise
            except BaseException as exc:
                status.update(state="error", error={"code": type(exc).__name__})
                write_json(self.settings.status_file, status)
                raise

    def task_status(self) -> dict[str, Any]:
        status = read_json(self.settings.status_file)
        if not status:
            return {"state": "idle"}
        if status.get("state") not in _TERMINAL and not pid_alive(int(status.get("pid") or 0)):
            status["state"] = "stale"  # process died without writing a result
        return status

    # ---- diagnostics ------------------------------------------------------------

    def doctor(self, *, online: bool = True, ocr: bool = False) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def check(name: str, status: str, detail: Any = None) -> None:
            checks.append({"name": name, "status": status, "detail": detail})

        check("python", "pass" if sys.version_info >= (3, 11) else "fail",
              f"{platform.python_version()} on {platform.system()} {platform.machine()}")
        for module in ("httpx", "typer", "ddddocr", "keyring", "cryptography"):
            found = importlib.util.find_spec(module) is not None
            check(f"dependency.{module}", "pass" if found else "fail")
        if ocr:
            try:
                ms = OcrSolver().warmup()
                check("ocr.warmup", "pass", f"{ms:.0f} ms (cold import + model load + 1 inference)")
            except Exception as exc:
                check("ocr.warmup", "fail", type(exc).__name__)
        check("keyring", "pass" if self.store.available else "warn",
              self.store.backend_name or "no secure backend: password/session will not be saved")
        try:
            self.settings.state_dir.mkdir(parents=True, exist_ok=True)
            probe = self.settings.state_dir / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            check("state_dir", "pass", str(self.settings.state_dir))
        except OSError as exc:
            check("state_dir", "fail", f"{self.settings.state_dir}: {exc.strerror}")
        check("account", "pass" if self.settings.username else "warn",
              "configured" if self.settings.username else "run `seu-lecture auth login`")
        if online:
            self._doctor_network(check)
        worst = "fail" if any(c["status"] == "fail" for c in checks) else (
            "warn" if any(c["status"] == "warn" for c in checks) else "pass")
        return {"version": __version__, "status": worst, "checks": checks}

    def _doctor_network(self, check: Callable[..., None]) -> None:
        endpoints = self.endpoints()
        for url in (endpoints.auth, endpoints.app):
            parts = urlsplit(url)
            host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
            start = time.perf_counter()
            try:
                with socket.create_connection((host, port), timeout=5) as sock:
                    tcp_ms = (time.perf_counter() - start) * 1000
                    detail = f"tcp {tcp_ms:.1f} ms"
                    if parts.scheme == "https":
                        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
                            detail += f", {tls.version()} {(time.perf_counter() - start) * 1000:.1f} ms"
                check(f"network.{host}", "pass", detail)
            except ssl.SSLError as exc:
                check(f"network.{host}", "fail", f"TLS error: {exc.reason}")
            except OSError as exc:
                check(f"network.{host}", "fail", f"{type(exc).__name__}: not reachable")
        with self.new_client() as client:
            try:
                resp = client.request("GET", endpoints.entry)
                location = resp.headers.get("location", "")
                host = urlsplit(location).hostname or ""
                if "vpn" in host:
                    check("campus_access", "fail", "redirected to the VPN gateway: connect to the "
                          "campus network or the school VPN client first")
                else:
                    check("campus_access", "pass", f"HTTP {resp.status_code}"
                          + (f" -> {host}" if host else ""))
            except (NetworkError, ProtocolError) as exc:
                check("campus_access", "fail", exc.message)
            try:
                est = estimate_offset(client.probe_date, max_samples=3, target_error_ms=500)
                skew = est.offset_ms
                detail: dict[str, Any] = est.to_dict()
                if skew is not None and abs(skew) >= 1000:
                    detail["note"] = ("server Date header disagrees with the local clock; if your clock "
                                      "is NTP-synced this is the school's front-end proxy clock. "
                                      "reserve --clock auto covers both candidates")
                check("clock_offset", "pass" if skew is not None and abs(skew) < 1000 else "warn", detail)
            except (NetworkError, ProtocolError) as exc:
                check("clock_offset", "warn", exc.message)
        if self.settings.username:
            status = self.auth_status(online=True)
            valid = status.get("session_valid")
            check("session", "pass" if valid else "warn",
                  "valid" if valid else ("expired" if valid is False else "no cached session"))
