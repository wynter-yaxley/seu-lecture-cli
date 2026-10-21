"""In-process fake of the SEU CAS + undergraduate lecture endpoints.

Used by the test-suite (through ``httpx.MockTransport``) and by
``seu-lecture benchmark --mock`` (through a real loopback HTTP server, so
socket, keep-alive and parsing costs are measured too).

The fake follows the response shapes documented in ``core/client.py``. It is a
test double, not a specification of the real school system.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from seu_lecture.core.clock import wall_time
from seu_lecture.core.lecture import SHANGHAI

APP = "/gsapp/sys/yddjzxxtjappseu"
API = APP + "/modules/hdyy_bk/"
CAS = "/auth/casback/"
LECTURE_ID = "8f1c2d3e4b5a69788796a5b4c3d2e1f0"


def default_lectures(open_at: float | None) -> list[dict[str, Any]]:
    moment = datetime.fromtimestamp(open_at or time.time() - 60, SHANGHAI)  # school times are UTC+8
    start = moment.strftime("%Y-%m-%d %H:%M:%S")
    return [
        {"WID": LECTURE_ID, "JZMC": "【线下】人文与科学素养讲座：宋词里的东南",
         "JZSJ": "2030-11-01 19:00:00", "HDJSSJ": "2030-11-01 21:00:00",
         "YYKSSJ": start, "YYJSSJ": "2030-11-01 18:00:00", "JZDD": "九龙湖校区焦廷标馆",
         "JZXL_DISPLAY": "人文与科学素养系列讲座-艺术类", "HDZRS": "50"},
        {"WID": "00aa11bb22cc33dd44ee55ff66778899", "JZMC": "【线上】心理健康与自我成长",
         "JZSJ": "2030-11-02 14:00:00", "YYKSSJ": "2030-10-30 12:00:00",
         "YYJSSJ": "2030-11-02 12:00:00", "HDZRS": "200"},
    ]


@dataclass
class MockConfig:
    username: str = "213200000"
    password: str = "correct-horse-battery"
    require_sms: bool = False
    sms_code: str = "246810"
    open_at: float | None = None          # epoch seconds (server clock); None = open
    capacity: int = 50
    reserved: int = 0
    captcha_answer: str | None = "1234"   # None accepts any code
    captcha_failures: int = 0             # first N submits answer 验证码错误
    # normal | read_timeout | disconnect | connect_error | server_error | unknown |
    # success_hidden | rate_limit | conflict | http_429 | html | empty | broken_json;
    # a ``_applied`` suffix records the reservation before answering with the fault.
    submit_fault: str = "normal"
    fault_times: int = 1
    retry_after: str | None = None        # Retry-After header sent with http_429
    # queryMyActivityList faults: missing_list | null_list | business_error
    mine_fault: str = "normal"
    mine_fault_times: int = 1
    extra_reservations: list[str] = field(default_factory=list)  # other lectures I reserved
    send_total: bool = False              # include totalSize in list responses
    list_lag: int = 0                     # reservation visible after N list reads
    latency_ms: float = 0.0
    session_ttl: int | None = None        # business calls before the session expires
    clock_offset_s: float = 0.0           # application clock minus real clock
    date_skew_s: float = 0.0              # extra skew of the Date header only (front proxy)
    datas_shape: str = "dict"             # dict | list
    captcha_key: str = "result"           # result | datas
    lectures: list[dict[str, Any]] | None = None
    lecture_fields_broken: bool = False


@dataclass
class MockResponse:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    fault: str | None = None  # read_timeout | disconnect | connect_error


def _png() -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (90, 32), "white")
    ImageDraw.Draw(img).text((18, 8), "1234", fill="black")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeCampus:
    def __init__(self, config: MockConfig | None = None) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.config = config or MockConfig()
        self.lock = threading.RLock()
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        der = self._key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.public_key = base64.urlsafe_b64encode(der).decode().rstrip("=")
        self.cas_sessions: set[str] = set()
        self.tickets: set[str] = set()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.trusted: set[str] = set()
        self.sms_sent = False
        self.reservations: set[str] = set()
        self.lag_left = 0
        self.calls: dict[str, int] = {}
        self.submit_times: list[float] = []
        self.base = "http://127.0.0.1"
        self._image = "data:image/png;base64," + base64.b64encode(_png()).decode()

    # ---- helpers ------------------------------------------------------------

    def now(self) -> float:
        return wall_time() + self.config.clock_offset_s

    def reset_reservations(self) -> None:
        with self.lock:
            self.reservations.clear()
            self.config.reserved = 0

    def expire_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    def _decrypt(self, value: str) -> str:
        from cryptography.hazmat.primitives.asymmetric import padding

        return self._key.decrypt(base64.b64decode(value), padding.PKCS1v15()).decode()

    @staticmethod
    def _json(data: Any, status: int = 200, **headers: str) -> MockResponse:
        body = json.dumps(data, ensure_ascii=False).encode()
        return MockResponse(status, {"Content-Type": "application/json;charset=UTF-8", **headers},
                            body)

    def _redirect(self, location: str, **headers: str) -> MockResponse:
        return MockResponse(302, {"Location": location, **headers})

    def _login_redirect(self) -> MockResponse:
        service = quote(self.base + APP + "/*default/index.do", safe="")
        return self._redirect(f"{self.base}/auth/dist/#/dist/main/login?service={service}")

    # ---- dispatch -------------------------------------------------------------

    def handle(self, method: str, path: str, query: dict[str, str], headers: dict[str, str],
               body: bytes) -> MockResponse:
        started = time.perf_counter()
        if self.config.latency_ms:
            time.sleep(self.config.latency_ms / 1000)
        with self.lock:
            name = path.rsplit("/", 1)[-1]
            self.calls[name] = self.calls.get(name, 0) + 1
            cookies = _cookies(headers.get("cookie", ""))
            if path.startswith(CAS):
                resp = self._cas(name, cookies, body)
            elif path == APP + "/*default/index.do":
                resp = self._entry(query, cookies)
            elif path.startswith(API) and method == "POST":
                resp = self._business(name, cookies, headers, body)
            else:
                resp = MockResponse(404, {"Content-Type": "text/plain"}, b"not found")
        resp.headers["Date"] = formatdate(self.now() + self.config.date_skew_s, usegmt=True)
        resp.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.3f}"
        return resp

    def _cas(self, name: str, cookies: dict[str, str], body: bytes) -> MockResponse:
        data = json.loads(body or b"{}")
        if name == "needCaptcha":
            return self._json({"code": 200, "success": True, "info": "不需要验证码",
                               "needStage2Validation": False, "redirectUrl": None})
        if name == "getChiperKey":
            sid = cookies.get("CASID") or secrets.token_hex(8)
            self.cas_sessions.add(sid)
            return self._json({"code": 200, "publicKey": self.public_key},
                              **{"Set-Cookie": f"CASID={sid}; Path=/; HttpOnly"})
        if name == "sendStage2Code":
            if data.get("userId") != self.config.username:
                return self._json({"code": 400, "success": False, "info": "用户不存在"})
            self.sms_sent = True
            return self._json({"code": 200, "success": True, "info": "验证码已发送至 138****0000"})
        if name == "casLogin":
            if cookies.get("CASID") not in self.cas_sessions:
                return self._json({"code": 500, "success": False, "info": "会话已失效"})
            try:
                password = self._decrypt(data["password"])
            except (KeyError, ValueError):
                return self._json({"code": 400, "success": False, "info": "参数错误"})
            if data.get("username") != self.config.username or password != self.config.password:
                return self._json({"code": 401, "success": False, "info": "用户名或密码错误"})
            fingerprint = data.get("fingerPrint") or ""
            if self.config.require_sms and fingerprint not in self.trusted:
                code = data.get("mobileVerifyCode")
                if not code:
                    return self._json({"code": 502, "success": False, "info": "非可信设备，需要短信验证",
                                       "needStage2Validation": True})
                if not self.sms_sent or self._decrypt(code) != self.config.sms_code:
                    return self._json({"code": 400, "success": False, "info": "短信验证码错误"})
                self.trusted.add(fingerprint)
            ticket = "ST-" + secrets.token_hex(8)
            self.tickets.add(ticket)
            redirect = quote(f"{data.get('service')}?ticket={ticket}", safe="")
            return self._json({"code": 200, "success": True, "info": "登录成功",
                               "redirectUrl": redirect})
        return MockResponse(404)

    def _entry(self, query: dict[str, str], cookies: dict[str, str]) -> MockResponse:
        ticket = query.get("ticket")
        if ticket:
            if ticket not in self.tickets:
                return self._login_redirect()
            self.tickets.discard(ticket)
            sid = secrets.token_hex(12)
            self.sessions[sid] = {"calls": 0, "captcha": None}
            return self._redirect(self.base + APP + "/*default/index.do",
                                  **{"Set-Cookie": f"JSESSIONID={sid}; Path=/; HttpOnly"})
        if cookies.get("JSESSIONID") in self.sessions:
            return MockResponse(200, {"Content-Type": "text/html"}, b"<html>hdyy</html>")
        return self._login_redirect()

    def _business(self, name: str, cookies: dict[str, str], headers: dict[str, str],
                  body: bytes) -> MockResponse:
        session = self.sessions.get(cookies.get("JSESSIONID", ""))
        if session is None:
            return self._login_redirect()
        session["calls"] += 1
        ttl = self.config.session_ttl
        if ttl is not None and session["calls"] > ttl:
            self.sessions.pop(cookies["JSESSIONID"], None)
            return self._login_redirect()
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        if name == "queryActivityList.do":
            return self._lectures()
        if name == "queryMyActivityList.do":
            return self._mine(form)
        if name == "vcode.do":
            session["captcha"] = self.config.captcha_answer or "*"
            return self._json({"code": "0", self.config.captcha_key: self._image})
        if name == "addReservation.do":
            return self._submit(session, form)
        if name == "appiontCheck.do":
            return self._json({"code": "0", "success": True, "msg": ""})
        return MockResponse(404)

    def _lectures(self) -> MockResponse:
        rows = self.config.lectures or default_lectures(self.config.open_at)
        if self.config.lecture_fields_broken:
            rows = [{"JZMC": 123, "HDZRS": "n/a"}, {"WID": LECTURE_ID, "YYKSSJ": "bad-time"}]
        counts = [{"HD_WID": LECTURE_ID, "YYRS": str(self.config.reserved)}]
        if self.config.datas_shape == "list":
            return self._json({"code": "0", "datas": rows})
        return self._json({"code": "0", "datas": {"hdlbList": rows, "hdrsList": counts}})

    def _mine(self, form: dict[str, str]) -> MockResponse:
        if self.config.mine_fault != "normal" and self.config.mine_fault_times > 0:
            self.config.mine_fault_times -= 1
            if self.config.mine_fault == "business_error":
                return self._json({"code": "1", "msg": "系统异常，请稍后再试"})
            if self.config.mine_fault == "missing_list":
                return self._json({"code": "0", "datas": {}})
            if self.config.mine_fault == "null_list":
                return self._json({"code": "0", "datas": {"wdhdList": None}})
        visible = self.reservations if self.lag_left <= 0 else set()
        self.lag_left -= 1
        wids = sorted(set(self.config.extra_reservations) | visible)
        size = int(form.get("pageSize") or 50)
        index = int(form.get("pageIndex") or 1)
        page = wids[(index - 1) * size:index * size]
        rows = [{"WID": "R" + wid[:8], "HD_WID": wid, "JZMC": "讲座", "FBZT": "1", "SFDK": "0",
                 "SFWY": "0", "JZSJ": "2030-11-01 19:00:00"} for wid in page]
        if self.config.datas_shape == "list":
            return self._json({"code": "0", "datas": rows})
        datas: dict[str, Any] = {"wdhdList": rows}
        if self.config.send_total:
            datas["totalSize"] = len(wids)
        return self._json({"code": "0", "datas": datas})

    def _fault(self) -> str | None:
        if self.config.submit_fault != "normal" and self.config.fault_times > 0:
            self.config.fault_times -= 1
            return self.config.submit_fault
        return None

    def _reserve(self, wid: str) -> None:
        self.reservations.add(wid)
        self.config.reserved += 1
        self.lag_left = self.config.list_lag

    def _submit(self, session: dict[str, Any], form: dict[str, str]) -> MockResponse:
        self.submit_times.append(self.now())
        if "paramJson" in form:
            form = json.loads(form["paramJson"])
        wid, code = form.get("wid", ""), form.get("vcode", "")
        expected, session["captcha"] = session.get("captcha"), None  # single use
        fault = self._fault()
        if fault and fault.endswith("_applied"):
            self._reserve(wid)
            fault = fault.removesuffix("_applied")
        if fault in ("read_timeout", "disconnect", "connect_error"):
            return MockResponse(fault=fault)
        if fault == "http_429":
            headers = {"Content-Type": "text/plain"}
            if self.config.retry_after is not None:
                headers["Retry-After"] = self.config.retry_after
            return MockResponse(429, headers, b"Too Many Requests")
        if fault == "html":
            return MockResponse(200, {"Content-Type": "text/html;charset=UTF-8"},
                                "<html><body>统一身份认证</body></html>".encode())
        if fault == "empty":
            return MockResponse(200, {"Content-Type": "application/json;charset=UTF-8"}, b"")
        if fault == "broken_json":
            return MockResponse(200, {"Content-Type": "application/json;charset=UTF-8"},
                                b'{"code":"0","msg":"\xe9\xa2')
        if fault == "server_error":
            return MockResponse(500, {"Content-Type": "text/plain"}, b"error")
        if fault == "unknown":
            return self._json({"code": "9", "msg": "系统繁忙"})
        if fault == "rate_limit":
            return self._json({"code": "1", "msg": "请求过于频繁，请稍后再试"})
        if self.config.open_at is not None and self.now() < self.config.open_at:
            return self._json({"code": "1", "msg": "预约未开始"})
        if self.config.captcha_failures > 0:
            self.config.captcha_failures -= 1
            return self._json({"code": "1", "msg": "验证码错误"})
        if expected is None or (expected != "*" and code != expected):
            return self._json({"code": "1", "msg": "验证码错误或已失效"})
        if wid in self.reservations:
            return self._json({"code": "1", "msg": "您已预约过该活动"})
        if fault == "conflict":
            return self._json({"code": "1", "msg": "与已预约活动时间上存在冲突"})
        if self.config.reserved >= self.config.capacity:
            return self._json({"code": "1", "msg": "预约人数已满"})
        if fault == "success_hidden":
            return self._json({"code": "0", "msg": "预约成功", "success": True, "datas": 1})
        self._reserve(wid)
        return self._json({"code": "0", "msg": "预约成功", "success": True, "datas": 1})


def _cookies(header: str) -> dict[str, str]:
    result = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            result[k] = v
    return result


# ---- adapters ------------------------------------------------------------

def mock_transport(campus: FakeCampus) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        query = {k: v for k, v in request.url.params.items()}
        resp = campus.handle(request.method, request.url.path, query,
                             {k.lower(): v for k, v in request.headers.items()}, request.content)
        if resp.fault == "read_timeout":
            raise httpx.ReadTimeout("mock read timeout", request=request)
        if resp.fault == "disconnect":
            raise httpx.RemoteProtocolError("mock disconnect", request=request)
        if resp.fault == "connect_error":
            raise httpx.ConnectError("mock connect error", request=request)
        return httpx.Response(resp.status, headers=resp.headers, content=resp.body)

    return httpx.MockTransport(handler)


class MockServer:
    """Loopback HTTP/1.1 server (keep-alive, TCP_NODELAY) around a ``FakeCampus``."""

    def __init__(self, campus: FakeCampus) -> None:
        self.campus = campus
        handler = _make_handler(campus)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        campus.base = self.base_url
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        return self.base_url

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def _make_handler(campus: FakeCampus) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            headers = {k.lower(): v for k, v in self.headers.items()}
            resp = campus.handle(self.command, parts.path, query, headers, body)
            if resp.fault:
                if resp.fault == "read_timeout":
                    time.sleep(30)
                self.close_connection = True
                return
            payload = b"" if self.command == "HEAD" else resp.body
            self.send_response_only(resp.status)  # send_response() would add a second Date
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        do_GET = do_POST = do_HEAD = _serve

    return Handler
