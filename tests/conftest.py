"""Shared fixtures: a scenario-driven fake campus wired into clients, services and the CLI.

Nothing here touches the network: HTTP goes through ``httpx.MockTransport`` (or a loopback
``MockServer`` in ``test_mock_server.py``) and credentials live in an in-memory store.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner, Result

from seu_lecture import cli as cli_module
from seu_lecture.api import LectureService
from seu_lecture.config import Settings, load_settings
from seu_lecture.core.auth import CasLogin, Credentials
from seu_lecture.core.captcha import FixedSolver, Solver
from seu_lecture.core.client import CampusClient, Endpoints
from seu_lecture.core.lecture import SHANGHAI
from seu_lecture.core.reserve import Outcome, ReservationTask, ReserveOptions, TaskResult
from seu_lecture.mock import LECTURE_ID, FakeCampus, MockConfig, mock_transport

# Fast-but-valid options: local clock, no latency compensation, short spacing/verification.
FAST_OPTIONS: dict[str, Any] = {
    "lecture_id": LECTURE_ID,
    "clock": "local",
    "offset_ms": 0.0,
    "compensate_latency": False,
    "min_submit_interval_ms": 100.0,
    "verify_interval_ms": 10.0,
    "captcha_lead_ms": 50.0,
    "rate_limit_backoff_s": 0.05,
}


class FakeStore:
    """In-memory stand-in for ``CredentialStore`` (no OS keyring)."""

    def __init__(self) -> None:
        self.passwords: dict[str, str] = {}
        self.sessions: dict[str, list[dict[str, str]]] = {}

    @property
    def available(self) -> bool:
        return True

    @property
    def backend_name(self) -> str | None:
        return "FakeStore"

    def get_password(self, username: str) -> str | None:
        return self.passwords.get(username)

    def set_password(self, username: str, password: str) -> bool:
        self.passwords[username] = password
        return True

    def get_session(self, username: str) -> list[dict[str, str]] | None:
        return self.sessions.get(username)

    def set_session(self, username: str, cookies: list[dict[str, str]]) -> bool:
        self.sessions[username] = cookies
        return True

    def clear(self, username: str) -> None:
        self.passwords.pop(username, None)
        self.sessions.pop(username, None)


class RecordingTransport(httpx.BaseTransport):
    """Wraps another transport and remembers every request that passed through."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.inner.handle_request(request)


@dataclass
class Run:
    """Result of one ``ReservationTask`` run plus the events it emitted."""

    result: TaskResult
    events: list[dict[str, Any]]

    @property
    def outcome(self) -> Outcome:
        return self.result.outcome

    @property
    def kinds(self) -> list[str]:
        return [e["event"] for e in self.events]

    @property
    def replies(self) -> list[str]:
        return [e["reply"] for e in self.events if e["event"] == "attempt"]

    def events_of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == kind]


class Env:
    """One fake campus with helpers to build clients, services and reservation runs."""

    def __init__(self, campus: FakeCampus, settings: Settings, store: FakeStore) -> None:
        self.campus = campus
        self.settings = settings
        self.store = store
        self.relogins = 0
        self._clients: list[CampusClient] = []

    @property
    def cfg(self) -> MockConfig:
        return self.campus.config

    def endpoints(self) -> Endpoints:
        return Endpoints.for_base(self.campus.base)

    def credentials(self, password: str | None = None) -> Credentials:
        return Credentials(self.cfg.username, password or self.cfg.password)

    def client(self, *, login: bool = False, transport: httpx.BaseTransport | None = None,
               **kwargs: Any) -> CampusClient:
        client = CampusClient(self.endpoints(), transport=transport or mock_transport(self.campus),
                              **kwargs)
        self._clients.append(client)
        if login:
            CasLogin(client).login(self.credentials(), fingerprint="fp")
        return client

    def service(self, **kwargs: Any) -> LectureService:
        kwargs.setdefault("solver", FixedSolver())
        return LectureService(self.settings, store=self.store,
                              transport=mock_transport(self.campus), **kwargs)

    def relogin(self, client: CampusClient) -> Callable[[], None]:
        def relogin() -> None:
            self.relogins += 1
            client.http.cookies.clear()
            CasLogin(client).login(self.credentials(), fingerprint="fp")

        return relogin

    def run(self, *, at: float | None = -1.0, solver: Solver | None = None,
            client: CampusClient | None = None,
            sleep: Callable[[float], None] = time.sleep, **options: Any) -> Run:
        """Run a reservation task. ``at`` is seconds from now (negative: already open)."""
        client = client or self.client(login=True)
        events: list[dict[str, Any]] = []
        when = None if at is None else datetime.fromtimestamp(time.time() + at, SHANGHAI)
        task = ReservationTask(
            client, solver or FixedSolver(), ReserveOptions(at=when, **{**FAST_OPTIONS, **options}),
            relogin=self.relogin(client), emit=events.append, sleep=sleep)
        return Run(task.run(), events)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Never read the developer's real config, account or password."""
    for name in ("SEU_LECTURE_PASSWORD", "SEU_LECTURE_USERNAME", "SEU_LECTURE_BASE_URL",
                 "SEU_LECTURE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEU_LECTURE_HOME", str(tmp_path / "home"))


@pytest.fixture
def make_env(tmp_path: Any) -> Iterator[Callable[..., Env]]:
    """Factory: ``make_env(require_sms=True, ...)`` takes ``MockConfig`` fields."""
    envs: list[Env] = []

    def factory(**config: Any) -> Env:
        campus = FakeCampus(MockConfig(**config))
        settings = load_settings(env={
            "SEU_LECTURE_HOME": str(tmp_path / "home"),
            "SEU_LECTURE_BASE_URL": "http://127.0.0.1",
            "SEU_LECTURE_USERNAME": campus.config.username,
        })
        envs.append(Env(campus, settings, FakeStore()))
        return envs[-1]

    yield factory
    for env in envs:
        for client in env._clients:
            client.close()


@pytest.fixture
def env(make_env: Callable[..., Env]) -> Env:
    return make_env()


# ---- CLI ---------------------------------------------------------------------

@pytest.fixture
def cli_env(make_env: Callable[..., Env], monkeypatch: pytest.MonkeyPatch) -> Env:
    """CLI wired to the fake campus (mock transport, in-memory store, fixed captcha).
    No account is configured yet."""
    env = make_env()

    def factory(settings: Settings, **kwargs: Any) -> LectureService:
        return LectureService(settings, store=env.store, transport=mock_transport(env.campus),
                              solver=FixedSolver(), **kwargs)

    monkeypatch.setattr(cli_module, "LectureService", factory)
    monkeypatch.setenv("SEU_LECTURE_BASE_URL", "http://127.0.0.1")
    return env


@pytest.fixture
def account(cli_env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    """``cli_env`` with a configured username and a saved password."""
    monkeypatch.setenv("SEU_LECTURE_USERNAME", cli_env.cfg.username)
    cli_env.store.set_password(cli_env.cfg.username, cli_env.cfg.password)
    return cli_env


@pytest.fixture
def invoke() -> Callable[..., Result]:
    """``invoke("list", "--json", input=...)`` against the Typer app."""
    try:
        runner = CliRunner(mix_stderr=False)  # click < 8.2
    except TypeError:
        runner = CliRunner()

    def run(*args: str, input: str | None = None) -> Result:
        return runner.invoke(cli_module.app, list(args), input=input)

    return run
