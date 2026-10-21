"""HTTP client plus the protocol adapters for the lecture and timetable apps.

Everything that depends on undocumented school behaviour (paths, field names,
response shapes) lives here, in ``lecture.py`` or in ``schedule.py``.

Observed on the real system (2026-10):

* Lectures: undergraduate module ``.../yddjzxxtjappseu/modules/hdyy_bk/``
  (``modules/hdyy/`` is the graduate module). List responses carry
  ``datas.xtdqsj``, the application server's own clock (second resolution).
* Timetable: ``/jwapp/sys/wdkb``; it is only reachable after the ehall portal
  login and ``appShow?appId=...``, each behind its own CAS service.
* An unauthenticated call answers ``302`` to ``auth.seu.edu.cn/dist/``.
* The HTTP ``Date`` header comes from a front-end proxy that ran ~375 s fast,
  so it is never used as a clock.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from seu_lecture.core.captcha import decode_captcha_payload
from seu_lecture.core.clock import ClockSample, wall_time
from seu_lecture.core.errors import (
    NetworkError,
    NetworkTimeout,
    ProtocolError,
    RateLimited,
    SessionExpired,
)

# CAS answers 403 to non-browser user agents.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
SUBMIT_FORMATS = ("both", "form", "paramJson")
REDIRECTS = {301, 302, 303, 307, 308}
# Public ehall portal id of the "我的课表" app (portal search, 2026-10).
TIMETABLE_APP_ID = "4770397878132218"


@dataclass(frozen=True)
class Endpoints:
    auth: str = "https://auth.seu.edu.cn/auth/casback/"
    login_page: str = "https://auth.seu.edu.cn/dist/"
    ehall: str = "https://ehall.seu.edu.cn/"
    module: str = "hdyy_bk"

    @classmethod
    def for_base(cls, base: str, module: str = "hdyy_bk") -> Endpoints:
        """Point every endpoint at one origin (used by the mock server)."""
        base = base.rstrip("/")
        return cls(auth=base + "/auth/casback/", login_page=base + "/auth/dist/",
                   ehall=base + "/", module=module)

    @property
    def app(self) -> str:
        return self.ehall + "gsapp/sys/yddjzxxtjappseu"

    @property
    def entry(self) -> str:
        return self.app + "/*default/index.do"

    def api(self, name: str) -> str:
        return f"{self.app}/modules/{self.module}/{name}"

    @property
    def portal_login(self) -> str:
        return self.ehall + "login?service=" + quote(self.ehall + "new/index.html", safe="")

    @property
    def timetable_show(self) -> str:
        return self.ehall + "appShow?appId=" + TIMETABLE_APP_ID

    @property
    def timetable(self) -> str:
        return self.ehall + "jwapp/sys/wdkb"

    @property
    def hosts(self) -> set[str]:
        return {urlsplit(u).hostname or "" for u in (self.auth, self.ehall)}

    @property
    def secure(self) -> bool:
        return self.ehall.startswith("https://")


class CampusClient:
    """One persistent ``httpx.Client`` (keep-alive, cookies, TLS verification on)."""

    def __init__(
        self,
        endpoints: Endpoints | None = None,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        submit_format: str = "both",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoints = endpoints or Endpoints()
        if submit_format not in SUBMIT_FORMATS:
            raise ValueError("submit_format must be 'both', 'form' or 'paramJson'")
        self.submit_format = submit_format
        # Raw answers kept for the task log: the first real submit must leave evidence.
        self.last_submit: dict[str, Any] | None = None
        self.last_mine: dict[str, Any] | None = None
        self.http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout, pool=connect_timeout),
            # keepalive_expiry defaults to 5 s in httpx; a longer expiry lets the
            # connection warmed shortly before the deadline survive until it.
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=120),
            follow_redirects=False,
            verify=True,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> CampusClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- transport -------------------------------------------------------

    def checked_url(self, url: str) -> str:
        """Allow only the configured school hosts; upgrade http to https."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if host not in self.endpoints.hosts:
            raise ProtocolError("unexpected host in URL", host=host)
        if parts.scheme == "http":
            if self.endpoints.secure:
                return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
            if host not in LOOPBACK:
                raise ProtocolError("plain HTTP is only allowed for a local mock server")
        elif parts.scheme != "https":
            raise ProtocolError("unsupported URL scheme")
        return url

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        url = self.checked_url(url)
        try:
            response = self.http.request(method, url, **kwargs)
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            raise NetworkTimeout("connect timeout", maybe_delivered=False) from None
        except httpx.TimeoutException:
            raise NetworkTimeout("timed out waiting for the server", maybe_delivered=True) from None
        except httpx.ConnectError:
            raise NetworkError("connection failed", maybe_delivered=False) from None
        except httpx.TransportError as exc:
            raise NetworkError(f"connection interrupted ({type(exc).__name__})",
                               maybe_delivered=True) from None
        return response

    def post_json(self, url: str, data: dict[str, Any] | None = None, *,
                  referer: str | None = None) -> dict[str, Any]:
        return parse_json_response(self._post(url, data, referer))

    def _post(self, url: str, data: dict[str, Any] | None, referer: str | None) -> httpx.Response:
        headers = {"Accept": "application/json, text/javascript, */*; q=0.01",
                   "X-Requested-With": "XMLHttpRequest", "Origin": self.endpoints.ehall.rstrip("/")}
        if referer:
            headers["Referer"] = referer
        # ``_`` mirrors the browser's jQuery cache buster.
        return self.request("POST", url, params={"_": str(int(time.time() * 1000))}, data=data,
                            headers=headers)

    # ---- lecture app (hdyy_bk) -------------------------------------------

    def call(self, name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.post_json(self.endpoints.api(name), data, referer=self.endpoints.entry)

    def lectures_payload(self, page_index: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self.call("queryActivityList.do", {"pageIndex": page_index, "pageSize": page_size})

    def reservations_payload(self, page_index: int = 1, page_size: int = 50) -> dict[str, Any]:
        response = self._post(self.endpoints.api("queryMyActivityList.do"), {
            "pageIndex": page_index, "pageSize": page_size, "sortField": "", "sortOrder": ""},
            self.endpoints.entry)
        self.last_mine = snapshot(response)
        return parse_json_response(response)

    def captcha_image(self) -> bytes:
        return decode_captcha_payload(self.call("vcode.do"))

    def submit(self, wid: str, code: str) -> dict[str, Any]:
        """The only write operation in the whole client.

        Public clients of the undergraduate module disagree on the encoding: plain form
        fields ``wid`` / ``vcode`` (fetch_lecture's Python client and GUI) versus one
        ``paramJson`` field (its v2.8 userscript). ``both`` sends all three fields in one
        request, so either reading works and there is still only a single submit.
        """
        fields = {"wid": wid, "vcode": code}
        packed = {"paramJson": json.dumps(fields, separators=(",", ":"))}
        data = {"form": fields, "paramJson": packed}.get(self.submit_format, {**fields, **packed})
        self.last_submit = None
        try:
            response = self._post(self.endpoints.api("addReservation.do"), data, self.endpoints.entry)
        except NetworkError as exc:
            self.last_submit = {"error": exc.code, "message": exc.message}
            raise
        self.last_submit = snapshot(response)
        return parse_json_response(response)

    def session_alive(self) -> bool:
        try:
            self.reservations_payload(1, 1)
        except SessionExpired:
            return False
        return True

    def probe_app_clock(self) -> ClockSample:
        """One light call returning the lecture application's clock (``xtdqsj``)."""
        sent = wall_time()
        payload = self.reservations_payload(1, 1)
        received = wall_time()
        datas = payload.get("datas")
        server = parse_server_time(datas.get("xtdqsj") if isinstance(datas, dict) else None)
        if server is None:
            raise ProtocolError("response carries no application time (xtdqsj)")
        return ClockSample(sent, received, server)

    # ---- timetable app (jwapp/sys/wdkb) ----------------------------------

    def timetable_call(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        base = self.endpoints.timetable
        return self.post_json(base + path, data, referer=base + "/*default/index.do")

    # ---- cookie persistence (only ever written to the OS keyring) --------

    def export_cookies(self) -> list[dict[str, str]]:
        """CAS cookies only (the TGT): enough to get a ticket for any app without the
        password, and small enough for Windows Credential Manager (2560-byte limit)."""
        auth = urlsplit(self.endpoints.auth)
        return [
            {"name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path}
            for c in self.http.cookies.jar
            if c.domain.lstrip(".") == auth.hostname and auth.path.startswith(c.path)
        ]

    def import_cookies(self, cookies: list[dict[str, str]]) -> None:
        for c in cookies:
            self.http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])


def parse_server_time(value: Any) -> float | None:
    """``'2026-10-21 15:37:22'`` in Beijing time -> epoch seconds."""
    from seu_lecture.core.lecture import parse_time

    moment = parse_time(value)
    return moment.timestamp() if moment else None


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """``Retry-After`` as seconds from now (delta-seconds or HTTP-date); None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        return None
    return max(0.0, moment.timestamp() - (time.time() if now is None else now))


def snapshot(response: httpx.Response, limit: int = 4000) -> dict[str, Any]:
    """What the server actually answered, for diagnosing unknown replies (no cookies)."""
    headers = response.headers
    return {"http_status": response.status_code, "content_type": headers.get("content-type"),
            "location": headers.get("location"), "retry_after": headers.get("retry-after"),
            "body": response.text[:limit]}


def parse_json_response(response: httpx.Response) -> dict[str, Any]:
    status = response.status_code
    if status in REDIRECTS or status in (401, 403):
        raise SessionExpired("school session expired or not logged in", http_status=status)
    if status == 429:
        raise RateLimited("rate limited by server (HTTP 429)", http_status=status,
                          retry_after=parse_retry_after(response.headers.get("retry-after")))
    if status >= 500:
        raise NetworkError(f"server error (HTTP {status})", maybe_delivered=True, http_status=status)
    if status != 200:
        raise ProtocolError(f"unexpected HTTP {status}", http_status=status)
    text = response.text
    if "html" in response.headers.get("content-type", "") or text.lstrip().startswith("<"):
        raise SessionExpired("received an HTML page instead of JSON (session expired?)")
    try:
        payload = json.loads(text)
    except ValueError:
        start = text.find("{")  # tolerate stray bytes before the JSON body
        try:
            payload = json.loads(text[start:]) if start > 0 else None
        except ValueError:
            payload = None
        if payload is None:
            raise ProtocolError("response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ProtocolError("JSON response is not an object")
    return payload

