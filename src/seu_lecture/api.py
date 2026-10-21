"""Front-end-neutral business API.

The CLI is a thin layer over this module; other front ends can call the same
methods (plain dicts / dataclasses, ``SeuLectureError`` on failure).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx

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
from seu_lecture.core.errors import (
    AuthFailed,
    AuthRequired,
    NetworkError,
    ScheduleBlocked,
    ScheduleUndetermined,
    SeuLectureError,
    SmsRequired,
)
from seu_lecture.core.lecture import SHANGHAI, Lecture, filter_lectures, find_lecture, load_lectures, load_reservations
from seu_lecture.core.reserve import ReservationTask, ReserveOptions, TaskResult, patient
from seu_lecture.core.schedule import Conflict, Timetable

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
        self.store = store or CredentialStore(settings.credentials_file)
        self.transport = transport
        self.password_prompt = password_prompt
        self.sms_prompt = sms_prompt
        self.solver = solver

    # ---- plumbing -----------------------------------------------------------

    def new_client(self) -> CampusClient:
        s = self.settings
        endpoints = Endpoints.for_base(s.base_url) if s.base_url else Endpoints()
        return CampusClient(endpoints, connect_timeout=s.connect_timeout, read_timeout=s.read_timeout,
                            submit_format=s.submit_format, transport=self.transport)

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

    def _cas(self, client: CampusClient, creds: Credentials | None, sms_code: str | None = None) -> CasLogin:
        return CasLogin(client, creds, fingerprint=self._fingerprint(), sms_code=sms_code)

    def open_session(self, password: str | None = None) -> CampusClient:
        """Logged-in lecture client. A cached CAS session (TGT) avoids the password."""
        username = self._username()
        client = self.new_client()
        try:
            cached = self.store.get_session(username)
            if cached:
                client.import_cookies(cached)
                try:
                    self._cas(client, None).login()
                    return client
                except AuthRequired:
                    client.http.cookies.clear()  # TGT expired
            password = password or self._password(username)
            self._cas(client, Credentials(username, password) if password else None).login()
            self.store.set_session(username, client.export_cookies())
            return client
        except BaseException:
            client.close()
            raise

    def _timetable(self, client: CampusClient) -> Timetable:
        username = self._username()
        password = self._password(username, prompt=False)
        return Timetable.open(client, self._cas(client, Credentials(username, password) if password else None))

    # ---- auth ---------------------------------------------------------------

    def login(self, username: str, password: str, *, sms_code: str | None = None,
              save_password: bool = True, insecure_storage: bool = False) -> dict[str, Any]:
        creds = Credentials(username, password)
        if insecure_storage:
            self.store.use_file()
        save_account(self.settings, username=username)
        self.settings.username = username
        with self.new_client() as client:
            try:
                self._cas(client, creds, sms_code).login()
            except SmsRequired:
                if sms_code:
                    raise AuthFailed("SMS code was not accepted") from None
                info = CasLogin(client).send_sms(username)
                code = self.sms_prompt(info) if self.sms_prompt else None
                if not code:
                    raise SmsRequired("SMS code sent; rerun with --sms-code <code>", sms_sent=True) from None
                self._cas(client, creds, code.strip()).login()
            self.store.set_session(username, client.export_cookies())
        saved = save_password and self.store.set_password(username, password)
        return {"username": username, "logged_in": True, "password_saved": bool(saved),
                "session_cached": self.store.get_session(username) is not None,
                "storage": self.store.backend_name}

    def logout(self) -> dict[str, Any]:
        username = self._username()
        self.store.clear(username)
        return {"username": username, "password_saved": False, "session_cached": False}

    def auth_status(self, *, online: bool = True) -> dict[str, Any]:
        username = self.settings.username
        data: dict[str, Any] = {"username": username, "storage": self.store.backend_name,
                                "password_saved": False, "session_cached": False, "session_valid": None}
        if not username:
            return data
        data["password_saved"] = self.store.get_password(username) is not None
        cached = self.store.get_session(username)
        data["session_cached"] = cached is not None
        if online and cached:
            with self.new_client() as client:
                client.import_cookies(cached)
                try:
                    self._cas(client, None).login()
                    data["session_valid"] = True
                except AuthRequired:
                    data["session_valid"] = False
        return data

    # ---- queries ------------------------------------------------------------

    def lectures(self, *, keyword: str | None = None, available: bool = False,
                 schedule: bool = False, free: bool = False) -> list[dict[str, Any]]:
        """Lectures; ``schedule`` adds a timetable check, ``free`` keeps only conflict-free ones."""
        now = datetime.now(SHANGHAI)
        with self.open_session() as client:
            items = filter_lectures(load_lectures(client), keyword=keyword, available=available, now=now)
            checks = None
            if schedule or free:
                timetable = self._timetable(client)
                checks = [timetable.check(x.start, x.end) for x in items]
        result = []
        for i, item in enumerate(items):
            data = item.to_dict(now)
            if checks is not None:
                if free and checks[i].status != "free":
                    continue
                data["schedule"] = checks[i].to_dict()
            result.append(data)
        return result

    def lecture(self, lecture_id: str, *, raw: bool = False, schedule: bool = False) -> dict[str, Any]:
        with self.open_session() as client:
            item = find_lecture(load_lectures(client), lecture_id)
            data = item.to_dict(datetime.now(SHANGHAI), raw=raw)
            if schedule:
                data["schedule"] = self._timetable(client).check(item.start, item.end).to_dict()
        return data

    def reservations(self) -> list[dict[str, Any]]:
        with self.open_session() as client:
            return [r.to_dict() for r in load_reservations(client)]

    def schedule(self, *, week: int | None = None, day: date | None = None) -> dict[str, Any]:
        """Classes of one teaching week with real dates and times (default: this week)."""
        with self.open_session() as client:
            timetable = self._timetable(client)
            term = timetable.term()
            if week is None:
                week = timetable.week_of(day or datetime.now(SHANGHAI).date())
                if week is None:
                    raise SeuLectureError("the date is outside the current term's teaching weeks")
            days = [{"date": d.isoformat(), "weekday": d.isoweekday(),
                     "classes": [c.to_dict() for c in timetable.classes_on(d)]}
                    for d in timetable.week_days(week)]
        return {"term": term.to_dict(), "week": week, "days": days}

    # ---- reservation task -----------------------------------------------------

    def check_schedule(self, client: CampusClient, lecture: Lecture) -> Conflict:
        """Pre-reservation timetable check; only ``free`` may proceed."""
        for _ in range(3):  # a stalled timetable server must not block the reservation
            try:
                conflict = patient(lambda: self._timetable(client)).check(lecture.start, lecture.end)
            except NetworkError as exc:
                conflict = Conflict("unknown", f"课表查询失败：{exc.message}")
            if not (conflict.reason or "").startswith("课表查询失败"):
                break
            time.sleep(3)
        if conflict.status == "conflict":
            names = "、".join(f"{c.course}({c.start:%H:%M}-{c.end:%H:%M})" for c in conflict.classes
                             if c.start and c.end)
            raise ScheduleBlocked(f"lecture overlaps classes: {names}", schedule=conflict.to_dict())
        if conflict.status != "free":
            raise ScheduleUndetermined(f"cannot tell whether the lecture is free: {conflict.reason}",
                                  schedule=conflict.to_dict())
        return conflict

    def reserve(self, options: ReserveOptions, emit: Callable[[dict[str, Any]], None] | None = None,
                *, check_schedule: bool = True) -> TaskResult:
        """Run one reservation task. Tasks for different lectures may run side by side;
        a second task for the same lecture fails with ``TASK_RUNNING``."""
        username = self._username()
        password = self._password(username)
        client = self.open_session(password)
        creds = Credentials(username, password) if password else None
        with client:
            lecture = find_lecture(patient(lambda: load_lectures(client)), options.lecture_id)
            options.lecture_id = lecture.id
            status_file = self.settings.tasks_dir / f"{lecture.id}.json"
            status: dict[str, Any] = {"state": "running", "pid": os.getpid(), "lecture_id": lecture.id,
                                      "title": lecture.title, "dry_run": options.dry_run,
                                      "started_at": datetime.now(SHANGHAI).isoformat()}

            def on_event(record: dict[str, Any]) -> None:
                # Called outside the critical section only (events are buffered there).
                kind = record.get("event")
                if kind == "phase":
                    status["phase"] = record.get("phase")
                elif kind == "prepared":
                    status["target_time"] = record.get("at")
                elif kind == "result":
                    status.update(state="finished", result=record)
                status["last_event"] = kind
                status["updated_at"] = datetime.now(SHANGHAI).isoformat()
                write_json(status_file, status)
                if emit:
                    emit(record)

            with task_lock(self.settings.tasks_dir / f"{lecture.id}.lock"):
                write_json(status_file, status)
                try:
                    if check_schedule:
                        on_event({"event": "schedule", "wall": datetime.now(SHANGHAI).timestamp(),
                                  **self.check_schedule(client, lecture).to_dict()})
                    solver = self.solver or OcrSolver(length=self.settings.captcha_length or None)
                    task = ReservationTask(client, solver, options, emit=on_event,
                                           relogin=lambda: self._cas(client, creds).login())
                    return task.run()
                except SeuLectureError as exc:
                    status.update(state="error", error=exc.to_dict())
                    write_json(status_file, status)
                    raise
                except BaseException as exc:
                    status.update(state="error", error={"code": type(exc).__name__})
                    write_json(status_file, status)
                    raise

    def task_status(self, lecture_id: str | None = None) -> dict[str, Any]:
        """All reservation tasks (newest first), optionally filtered by lecture id prefix.
        Finished tasks older than a week are forgotten."""
        tasks = []
        week_ago = time.time() - 7 * 86400
        for log in self.settings.tasks_dir.glob("*.log"):
            if log.stat().st_mtime < week_ago:
                log.unlink(missing_ok=True)
        for path in sorted(self.settings.tasks_dir.glob("*.json")):
            status = read_json(path)
            if not status or (lecture_id and not path.stem.startswith(lecture_id.lower())):
                continue
            if status.get("state") in _TERMINAL and path.stat().st_mtime < week_ago:
                path.unlink(missing_ok=True)
                continue
            if status.get("state") not in _TERMINAL and not pid_alive(int(status.get("pid") or 0)):
                status["state"] = "stale"  # process died without writing a result
            tasks.append(status)
        tasks.sort(key=lambda t: str(t.get("started_at") or ""), reverse=True)
        return {"tasks": tasks}
