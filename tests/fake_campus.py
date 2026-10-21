"""In-process fake of SEU CAS, the lecture app, the ehall portal and the timetable app.

Used by the test-suite through ``httpx.MockTransport`` and, for socket-level
tests, through a real loopback HTTP server (``MockServer``).

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
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from seu_lecture.core.client import TIMETABLE_APP_ID
from seu_lecture.core.clock import wall_time
from seu_lecture.core.lecture import SHANGHAI

APP = "/gsapp/sys/yddjzxxtjappseu"
API = APP + "/modules/hdyy_bk/"
CAS = "/auth/casback/"
KB = "/jwapp/sys/wdkb"
LECTURE_ID = "8f1c2d3e4b5a69788796a5b4c3d2e1f0"


def default_courses() -> list[dict[str, Any]]:
    """Mon 1-2 every week; Fri evening 11-13 on even weeks only."""
    return [
        {"KCM": "土力学", "KCH": "C1", "SKXQ": "1", "KSJC": "1", "JSJC": "2", "SKZC": "1" * 16,
         "ZCMC": "1-16周", "JASMC": "教一-101"},
        {"KCM": "高等数学", "KCH": "C2", "SKXQ": "5", "KSJC": "11", "JSJC": "13", "SKZC": "01" * 8,
         "ZCMC": "2-16周(双)", "JASMC": "教二-202"},
    ]


def this_monday() -> date:
    today = datetime.now(SHANGHAI).date()
    return today - timedelta(days=today.weekday())


def default_term_start() -> str:
    """Week 5 is the current week, so default data never expires."""
    return (this_monday() - timedelta(weeks=4)).isoformat()


def default_lectures(open_at: float | None, capacity: int = 50) -> list[dict[str, Any]]:
    moment = datetime.fromtimestamp(open_at or time.time() - 60, SHANGHAI)  # school times are UTC+8
    start = moment.strftime("%Y-%m-%d %H:%M:%S")
    saturday = this_monday() + timedelta(days=12)  # next week's Saturday: no classes
    return [
        {"WID": LECTURE_ID, "JZMC": "【线下】人文与科学素养讲座：宋词里的东南",
         "JZSJ": f"{saturday} 19:00:00", "HDJSSJ": f"{saturday} 21:00:00",
         "YYKSSJ": start, "YYJSSJ": f"{saturday} 18:00:00", "JZDD": "九龙湖校区焦廷标馆",
         "JZXL_DISPLAY": "人文与科学素养系列讲座-艺术类", "HDZRS": str(capacity)},
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
    # queryMyActivityList faults: missing_list | null_list | business_error | http_429
    mine_fault: str = "normal"
    mine_fault_times: int = 1
    captcha_fault_times: int = 0          # vcode.do answers HTTP 429 this many times
    free_seat_after_lists: int | None = None  # someone cancels after N lecture-list reads
    extra_reservations: list[str] = field(default_factory=list)  # other lectures I reserved
    send_total: bool = False              # include totalSize in list responses
    list_lag: int = 0                     # reservation visible after N list reads
    latency_ms: float = 0.0
    session_ttl: int | None = None        # business calls before the session expires
    clock_offset_s: float = 0.0           # application clock (xtdqsj) minus real clock
    datas_shape: str = "dict"             # dict | list
    captcha_key: str = "result"           # result | datas
    lectures: list[dict[str, Any]] | None = None
    lecture_fields_broken: bool = False
    # timetable app
    term_code: str = "2026-2027-2"
    term_start: str = field(default_factory=default_term_start)  # Monday of week 1
    term_weeks: int = 18
    period_split: tuple[int, int, int] = (5, 5, 3)
    courses: list[dict[str, Any]] = field(default_factory=default_courses)
    course_changes: list[dict[str, Any]] = field(default_factory=list)  # xsdkkc rows
    timetable_fault: str = "normal"       # business_error


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
        self.tgts: set[str] = set()
        self.tickets: dict[str, str] = {}     # ticket -> service it was issued for
        self.sessions: dict[str, dict[str, Any]] = {}
        self.portals: set[str] = set()        # ehall portal sessions
        self.shown: set[str] = set()          # portal sessions that opened the timetable via appShow
        self.kb_sessions: set[str] = set()
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

    def _login_redirect(self, service: str | None = None) -> MockResponse:
        service = quote(service or self.base + APP + "/*default/index.do", safe="")
        return self._redirect(f"{self.base}/auth/dist/#/dist/main/login?service={service}")

    def _take_ticket(self, query: dict[str, str], service: str) -> bool:
        ticket = query.get("ticket", "")
        return self.tickets.pop(ticket, None) == service

    # ---- dispatch -------------------------------------------------------------

    def handle(self, method: str, path: str, query: dict[str, str], headers: dict[str, str],
               body: bytes) -> MockResponse:
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
            elif path in ("/login", "/new/index.html", "/appShow"):
                resp = self._portal(path, query, cookies)
            elif path.startswith(KB):
                resp = self._timetable(path, query, cookies, body)
            else:
                resp = MockResponse(404, {"Content-Type": "text/plain"}, b"not found")
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
                              **{"Set-Cookie": f"CASID={sid}; Path=/auth; HttpOnly"})
        if name == "verifyTgt":
            if cookies.get("TGT") not in self.tgts:
                return self._json({"code": 500, "success": False, "info": "TGT无效"})
            ticket = "ST-" + secrets.token_hex(8)
            self.tickets[ticket] = data.get("service", "")
            return self._json({"code": 201, "success": True, "info": "CasLoginByCookieRequest Success",
                               "redirectUrl": _with_ticket(data.get("service", ""), ticket)})
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
            self.tickets[ticket] = data.get("service", "")
            tgt = "TGT-" + secrets.token_hex(8)
            self.tgts.add(tgt)
            redirect = quote(_with_ticket(data.get("service", ""), ticket), safe="")
            return self._json({"code": 200, "success": True, "info": "登录成功", "redirectUrl": redirect},
                              **{"Set-Cookie": f"TGT={tgt}; Path=/auth; HttpOnly"})
        return MockResponse(404)

    def _entry(self, query: dict[str, str], cookies: dict[str, str]) -> MockResponse:
        if query.get("ticket"):
            if not self._take_ticket(query, self.base + APP + "/*default/index.do"):
                return self._login_redirect()
            sid = secrets.token_hex(12)
            self.sessions[sid] = {"calls": 0, "captcha": None}
            return self._redirect(self.base + APP + "/*default/index.do",
                                  **{"Set-Cookie": f"JSESSIONID={sid}; Path=/gsapp; HttpOnly"})
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
            if self.config.captcha_fault_times > 0:
                self.config.captcha_fault_times -= 1
                return self._too_many()
            session["captcha"] = self.config.captcha_answer or "*"
            return self._json({"code": "0", self.config.captcha_key: self._image})
        if name == "addReservation.do":
            return self._submit(session, form)
        if name == "appiontCheck.do":
            return self._json({"code": "0", "success": True, "msg": ""})
        return MockResponse(404)

    # ---- ehall portal + timetable app ----------------------------------------

    def _portal(self, path: str, query: dict[str, str], cookies: dict[str, str]) -> MockResponse:
        portal = cookies.get("PORTAL", "")
        if path == "/new/index.html":
            return MockResponse(200, {"Content-Type": "text/html"}, b"<html>portal</html>")
        if path == "/appShow":
            if portal not in self.portals:
                return self._redirect(self.base + "/login?service=" + quote(self.base + "/new/index.html", safe=""))
            if query.get("appId") != TIMETABLE_APP_ID:
                return MockResponse(404)
            self.shown.add(portal)
            return self._redirect(self.base + KB + "/*default/index.do")
        target = query.get("service", "")
        own = self.base + "/login?service=" + quote(target, safe="")
        if query.get("ticket"):
            if not self._take_ticket(query, own):
                return self._login_redirect(own)
            sid = secrets.token_hex(8)
            self.portals.add(sid)
            return self._redirect(target, **{"Set-Cookie": f"PORTAL={sid}; Path=/; HttpOnly"})
        if portal in self.portals:
            return self._redirect(target)
        return self._login_redirect(own)

    def _timetable(self, path: str, query: dict[str, str], cookies: dict[str, str],
                   body: bytes) -> MockResponse:
        entry = self.base + KB + "/*default/index.do"
        if path == KB + "/*default/index.do":
            if query.get("ticket"):
                if not self._take_ticket(query, entry):
                    return self._login_redirect(entry)
                sid = secrets.token_hex(8)
                self.kb_sessions.add(sid)
                return self._redirect(entry, **{"Set-Cookie": f"GS_SESSIONID={sid}; Path=/jwapp; HttpOnly"})
            if cookies.get("GS_SESSIONID") not in self.kb_sessions:
                return self._login_redirect(entry)
            # Like the real app: only reachable after the portal opened it via appShow.
            if cookies.get("PORTAL") not in self.shown:
                return MockResponse(403, {"Content-Type": "text/html"}, b"<html>403</html>")
            return MockResponse(200, {"Content-Type": "text/html"}, b"<html>wdkb</html>")
        if cookies.get("GS_SESSIONID") not in self.kb_sessions:
            return self._login_redirect(entry)
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        key = path.rsplit("/", 1)[-1].removesuffix(".do")
        if self.config.timetable_fault == "business_error":
            return self._json({"datas": {key: {"rows": [], "extParams": {"code": 0, "msg": "查询失败"}}}})
        c = self.config
        start = date.fromisoformat(c.term_start)
        xn, _, xq = c.term_code.rpartition("-")
        if key == "dqxnxq":
            rows: list[dict[str, Any]] = [{"DM": c.term_code, "MC": "测试学期", "XNDM": xn, "XQDM": xq}]
        elif key == "cxjcs":
            morning, afternoon, evening = c.period_split
            rows = [{"XN": xn, "XQ": xq, "SFJJ": morning, "XFJJ": afternoon, "WSJJ": evening,
                     "XQKSRQ": f"{c.term_start} 00:00:00", "ZZC": c.term_weeks}]
        elif key == "dqzc":
            day = date.fromisoformat(form["RQ"])
            rows = [{"ZC": (day - start).days // 7 + 1, "XQJ": day.isoweekday(), "RQ": f"{day} 00:00:00"}]
        elif key == "xskcb":
            week = int(form.get("SKZC") or 0)
            rows = [r for r in c.courses if not week or (len(r["SKZC"]) >= week and r["SKZC"][week - 1] == "1")]
        elif key == "xsdkkc":
            rows = list(c.course_changes)
        else:
            return MockResponse(404)
        return self._json({"datas": {key: {"rows": rows, "extParams": {"code": 1}}}, "code": "0"})

    def app_time(self) -> str:
        return datetime.fromtimestamp(self.now(), SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")

    def _lectures(self) -> MockResponse:
        rows = self.config.lectures or default_lectures(self.config.open_at, self.config.capacity)
        if self.config.free_seat_after_lists is not None:
            self.config.free_seat_after_lists -= 1
            if self.config.free_seat_after_lists < 0:
                self.config.free_seat_after_lists = None
                self.config.reserved -= 1
        if self.config.lecture_fields_broken:
            rows = [{"JZMC": 123, "HDZRS": "n/a"}, {"WID": LECTURE_ID, "YYKSSJ": "bad-time"}]
        counts = [{"HD_WID": LECTURE_ID, "YYRS": str(self.config.reserved)}]
        if self.config.datas_shape == "list":
            return self._json({"code": "0", "datas": rows})
        return self._json({"code": "0", "datas": {"hdlbList": rows, "hdrsList": counts,
                                                  "xtdqsj": self.app_time()}})

    def _mine(self, form: dict[str, str]) -> MockResponse:
        if self.config.mine_fault != "normal" and self.config.mine_fault_times > 0:
            self.config.mine_fault_times -= 1
            if self.config.mine_fault == "business_error":
                return self._json({"code": "1", "msg": "系统异常，请稍后再试"})
            if self.config.mine_fault == "missing_list":
                return self._json({"code": "0", "datas": {}})
            if self.config.mine_fault == "null_list":
                return self._json({"code": "0", "datas": {"wdhdList": None}})
            if self.config.mine_fault == "http_429":
                return self._too_many()
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
        datas: dict[str, Any] = {"wdhdList": rows, "xtdqsj": self.app_time()}
        if self.config.send_total:
            datas["totalSize"] = len(wids)
        return self._json({"code": "0", "datas": datas})

    def _too_many(self) -> MockResponse:
        headers = {"Content-Type": "text/plain"}
        if self.config.retry_after is not None:
            headers["Retry-After"] = self.config.retry_after
        return MockResponse(429, headers, b"Too Many Requests")

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
        if "paramJson" in form and "wid" not in form:  # the real server ignores paramJson
            return self._json({"code": -1, "msg": "非法参数", "datas": None})
        wid, code = form.get("wid", ""), form.get("vcode", "")
        expected, session["captcha"] = session.get("captcha"), None  # single use
        fault = self._fault()
        if fault and fault.endswith("_applied"):
            self._reserve(wid)
            fault = fault.removesuffix("_applied")
        if fault in ("read_timeout", "disconnect", "connect_error"):
            return MockResponse(fault=fault)
        if fault == "http_429":
            return self._too_many()
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
            return self._json({"code": -1, "msg": "验证码错误，请重试！注意不要同时使用多台设备进行预约操作。",
                               "datas": None})
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


def _with_ticket(service: str, ticket: str) -> str:
    return f"{service}{'&' if '?' in service else '?'}ticket={ticket}"


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
