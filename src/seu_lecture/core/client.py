"""HTTP client plus the protocol adapter for the undergraduate lecture module.

Everything that depends on undocumented school behaviour (paths, field names,
response shapes) lives here or in ``lecture.py`` so it can be adjusted in one place.

Protocol notes (cross-checked against public projects, see README):

* Undergraduate module: ``.../yddjzxxtjappseu/modules/hdyy_bk/``.
  ``modules/hdyy/`` is the *graduate* module and is deliberately not used.
* An unauthenticated business call answers ``302`` to ``auth.seu.edu.cn``.
* ``addReservation.do`` is posted as form fields ``wid`` + ``vcode`` (what the
  upstream GUI verified). A browser userscript used ``paramJson={...}`` instead;
  ``submit_format="paramJson"`` switches to that encoding without code changes.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from seu_lecture.core.captcha import decode_captcha_payload
from seu_lecture.core.errors import (
    NetworkError,
    NetworkTimeout,
    ProtocolError,
    RateLimited,
    SessionExpired,
)
from seu_lecture.core.metrics import Metrics, ms_since, now_ns, parse_server_timing, wall_time

# CAS answers 403 to non-browser user agents.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
REDIRECTS = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class Endpoints:
    auth: str = "https://auth.seu.edu.cn/auth/casback/"
    app: str = "https://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu"
    # The exact service string CAS issues tickets for (the school uses http here).
    service: str = "http://ehall.seu.edu.cn/gsapp/sys/yddjzxxtjappseu/*default/index.do"
    module: str = "hdyy_bk"

    @classmethod
    def for_base(cls, base: str, module: str = "hdyy_bk") -> Endpoints:
        """Point every endpoint at one origin (used by the mock server)."""
        base = base.rstrip("/")
        app = base + "/gsapp/sys/yddjzxxtjappseu"
        return cls(auth=base + "/auth/casback/", app=app, service=app + "/*default/index.do",
                   module=module)

    @property
    def entry(self) -> str:
        return self.app + "/*default/index.do"

    @property
    def origin(self) -> str:
        parts = urlsplit(self.app)
        return f"{parts.scheme}://{parts.netloc}/"

    def api(self, name: str) -> str:
        return f"{self.app}/modules/{self.module}/{name}"

    @property
    def hosts(self) -> set[str]:
        return {urlsplit(u).hostname or "" for u in (self.auth, self.app, self.service)}

    @property
    def secure(self) -> bool:
        return self.app.startswith("https://")


@dataclass
class DateSample:
    """Local wall-clock send/receive times bracketing a server ``Date`` header."""

    sent: float
    received: float
    date: str


class CampusClient:
    """One persistent ``httpx.Client`` (keep-alive, cookies, TLS verification on)."""

    def __init__(
        self,
        endpoints: Endpoints | None = None,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        submit_format: str = "form",
        transport: httpx.BaseTransport | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.endpoints = endpoints or Endpoints()
        if submit_format not in ("form", "paramJson"):
            raise ValueError("submit_format must be 'form' or 'paramJson'")
        self.submit_format = submit_format
        self.metrics = metrics or Metrics()
        self.date_samples: deque[DateSample] = deque(maxlen=32)
        self.http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout, pool=connect_timeout),
            # keepalive_expiry defaults to 5 s in httpx; a longer expiry lets the
            # connection warmed shortly before the deadline survive until it.
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4,
                                keepalive_expiry=120),
            follow_redirects=False,
            verify=True,
            transport=transport,
        )
        self._xhr_headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.endpoints.origin.rstrip("/"),
            "Referer": self.endpoints.entry,
        }

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

    def request(self, method: str, url: str, *, metric: str | None = None,
                **kwargs: Any) -> httpx.Response:
        url = self.checked_url(url)
        sent = wall_time()
        start = now_ns()
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
        elapsed = ms_since(start)
        if metric:
            self.metrics.add(f"{metric}.rtt_ms", elapsed)
            server = parse_server_timing(response.headers.get("server-timing"))
            if server is not None:
                self.metrics.add(f"{metric}.server_ms", server)
        if dates := response.headers.get_list("date"):
            self.date_samples.append(DateSample(sent, wall_time(), dates[0]))
        return response

    def follow(self, url: str, *, max_hops: int = 10) -> httpx.Response:
        """GET with manual redirects so every hop passes ``checked_url``."""
        for _ in range(max_hops):
            response = self.request("GET", url)
            if response.status_code not in REDIRECTS:
                return response
            location = response.headers.get("location")
            if not location:
                raise ProtocolError("redirect without Location header")
            url = urljoin(str(response.url), location)
        raise ProtocolError("too many redirects")

    def probe_date(self) -> DateSample:
        """One cheap HEAD request used for clock-offset estimation."""
        before = len(self.date_samples)
        self.request("HEAD", self.endpoints.origin, metric="clock_probe")
        if len(self.date_samples) == before:
            raise ProtocolError("server did not send a Date header")
        return self.date_samples[-1]

    # ---- business API (hdyy_bk) -----------------------------------------

    def call(self, name: str, *, data: dict[str, Any] | None = None,
             method: str = "POST", metric: str | None = None) -> dict[str, Any]:
        # ``_`` mirrors the browser's jQuery cache buster.
        params = {"_": str(int(time.time() * 1000))}
        response = self.request(method, self.endpoints.api(name), params=params, data=data,
                                headers=self._xhr_headers, metric=metric)
        return parse_json_response(response)

    def lectures_payload(self, page_index: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self.call("queryActivityList.do",
                         data={"pageIndex": page_index, "pageSize": page_size}, metric="list")

    def reservations_payload(self, page_index: int = 1, page_size: int = 50) -> dict[str, Any]:
        return self.call("queryMyActivityList.do", metric="mine", data={
            "pageIndex": page_index, "pageSize": page_size, "sortField": "", "sortOrder": ""})

    def captcha_image(self) -> bytes:
        return decode_captcha_payload(self.call("vcode.do", metric="captcha"))

    def submit(self, wid: str, code: str) -> dict[str, Any]:
        """The only write operation in the whole client."""
        if self.submit_format == "paramJson":
            data = {"paramJson": json.dumps({"wid": wid, "vcode": code}, separators=(",", ":"))}
        else:
            data = {"wid": wid, "vcode": code}
        return self.call("addReservation.do", data=data, metric="submit")

    def session_alive(self) -> bool:
        try:
            self.reservations_payload(1, 1)
        except SessionExpired:
            return False
        return True

    # ---- cookie persistence (only ever written to the OS keyring) --------

    def export_cookies(self) -> list[dict[str, str]]:
        hosts = self.endpoints.hosts
        return [
            {"name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path}
            for c in self.http.cookies.jar
            if c.domain.lstrip(".") in hosts or any(h.endswith(c.domain) for h in hosts)
        ]

    def import_cookies(self, cookies: list[dict[str, str]]) -> None:
        for c in cookies:
            self.http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])


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


def parse_json_response(response: httpx.Response) -> dict[str, Any]:
    status = response.status_code
    if status in REDIRECTS or status in (401, 403):
        raise SessionExpired("school session expired or not logged in", http_status=status)
    if status == 429:
        raise RateLimited("rate limited by server (HTTP 429)", http_status=status,
                          retry_after=parse_retry_after(response.headers.get("retry-after")))
    if status >= 500:
        raise NetworkError(f"server error (HTTP {status})", maybe_delivered=True,
                           http_status=status)
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
