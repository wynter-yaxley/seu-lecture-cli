"""HTTP client: URL policy, error mapping, response parsing, cookies and submit encoding."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import Env, RecordingTransport

from seu_lecture.core.client import CampusClient, Endpoints, parse_json_response
from seu_lecture.core.clock import parse_http_date
from seu_lecture.core.errors import (
    NetworkError,
    NetworkTimeout,
    ProtocolError,
    SessionExpired,
)
from seu_lecture.core.reserve import Reply, classify_reply
from seu_lecture.mock import LECTURE_ID, FakeCampus, mock_transport


def client_with(handler: Callable[[httpx.Request], httpx.Response],
                endpoints: Endpoints | None = None) -> CampusClient:
    return CampusClient(endpoints or Endpoints.for_base("http://127.0.0.1"),
                        transport=httpx.MockTransport(handler))


# ---- endpoints / URL policy -----------------------------------------------------------------

def test_endpoints_for_base() -> None:
    endpoints = Endpoints.for_base("http://127.0.0.1:8080/")
    assert endpoints.auth == "http://127.0.0.1:8080/auth/casback/"
    assert endpoints.api("vcode.do") == (
        "http://127.0.0.1:8080/gsapp/sys/yddjzxxtjappseu/modules/hdyy_bk/vcode.do")
    assert endpoints.origin == "http://127.0.0.1:8080/"
    assert endpoints.hosts == {"127.0.0.1"}
    assert not endpoints.secure
    assert Endpoints.for_base("https://x.example", module="other").api("a").endswith("/other/a")


def test_default_endpoints_use_the_undergraduate_module() -> None:
    endpoints = Endpoints()
    assert endpoints.secure
    assert "/hdyy_bk/" in endpoints.api("x")
    assert endpoints.hosts == {"auth.seu.edu.cn", "ehall.seu.edu.cn"}


def test_checked_url_policy() -> None:
    client = CampusClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert client.checked_url("https://ehall.seu.edu.cn/x?a=1") == "https://ehall.seu.edu.cn/x?a=1"
    # The school's service string uses http; everything is upgraded to TLS.
    assert client.checked_url("http://ehall.seu.edu.cn/x?a=1#f") == (
        "https://ehall.seu.edu.cn/x?a=1#f")
    with pytest.raises(ProtocolError, match="unexpected host"):
        client.checked_url("https://evil.example/x")
    with pytest.raises(ProtocolError, match="unexpected host"):
        client.checked_url("https://ehall.seu.edu.cn.evil.example/x")
    with pytest.raises(ProtocolError, match="scheme"):
        client.checked_url("ftp://ehall.seu.edu.cn/x")
    client.close()


def test_plain_http_only_for_loopback() -> None:
    local = client_with(lambda r: httpx.Response(200))
    assert local.checked_url("http://127.0.0.1/x") == "http://127.0.0.1/x"
    remote = client_with(lambda r: httpx.Response(200), Endpoints.for_base("http://example.com"))
    with pytest.raises(ProtocolError, match="plain HTTP"):
        remote.checked_url("http://example.com/x")
    local.close()
    remote.close()


def test_invalid_submit_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="submit_format"):
        CampusClient(submit_format="xml")


# ---- transport error mapping -------------------------------------------------------------------

@pytest.mark.parametrize(("raised", "mapped", "delivered"), [
    (httpx.ConnectTimeout, NetworkTimeout, False),
    (httpx.PoolTimeout, NetworkTimeout, False),
    (httpx.ReadTimeout, NetworkTimeout, True),
    (httpx.WriteTimeout, NetworkTimeout, True),
    (httpx.ConnectError, NetworkError, False),
    (httpx.ReadError, NetworkError, True),
    (httpx.RemoteProtocolError, NetworkError, True),
])
def test_transport_errors_carry_the_delivery_flag(raised: type[Exception],
                                                  mapped: type[NetworkError],
                                                  delivered: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised("boom")

    client = client_with(handler)
    with pytest.raises(NetworkError) as info:
        client.request("POST", client.endpoints.api("addReservation.do"))
    assert type(info.value) is mapped
    assert info.value.maybe_delivered is delivered
    assert info.value.retryable
    assert "boom" not in info.value.message  # no raw exception text leaks
    client.close()


# ---- parse_json_response ------------------------------------------------------------------------

@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://auth.seu.edu.cn/x"}),
    httpx.Response(301),
    httpx.Response(401),
    httpx.Response(403),
    httpx.Response(200, headers={"content-type": "text/html"}, text="<html>login</html>"),
    httpx.Response(200, headers={"content-type": "application/json"}, text=" <html>"),
])
def test_redirects_and_login_pages_mean_session_expired(response: httpx.Response) -> None:
    with pytest.raises(SessionExpired):
        parse_json_response(response)


@pytest.mark.parametrize(("status", "delivered"), [(429, False), (500, True), (503, True)])
def test_rate_limit_and_server_errors(status: int, delivered: bool) -> None:
    with pytest.raises(NetworkError) as info:
        parse_json_response(httpx.Response(status))
    assert info.value.maybe_delivered is delivered
    assert info.value.detail["http_status"] == status


@pytest.mark.parametrize("response", [
    httpx.Response(404),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=[1, 2]),
    httpx.Response(200, text=""),
])
def test_unexpected_responses_are_protocol_errors(response: httpx.Response) -> None:
    with pytest.raises(ProtocolError):
        parse_json_response(response)


def test_json_object_is_returned_and_stray_prefix_tolerated() -> None:
    assert parse_json_response(httpx.Response(200, json={"a": 1})) == {"a": 1}
    assert parse_json_response(httpx.Response(200, text='\ufeff{"a": 1}')) == {"a": 1}
    assert parse_json_response(httpx.Response(200, text='xx{"a": "中"}')) == {"a": "中"}


# ---- redirects -------------------------------------------------------------------------------------

def test_follow_stops_on_redirect_loops() -> None:
    client = client_with(lambda r: httpx.Response(302, headers={"location": "/loop"}))
    with pytest.raises(ProtocolError, match="too many redirects"):
        client.follow(client.endpoints.entry)
    client.close()


def test_follow_refuses_redirects_to_foreign_hosts() -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://evil.example/steal"})

    client = client_with(handler)
    with pytest.raises(ProtocolError, match="unexpected host"):
        client.follow(client.endpoints.entry)
    assert len(hits) == 1  # the foreign URL was never requested
    client.close()


def test_follow_requires_a_location_header() -> None:
    client = client_with(lambda r: httpx.Response(302))
    with pytest.raises(ProtocolError, match="Location"):
        client.follow(client.endpoints.entry)
    client.close()


# ---- business calls against the fake campus --------------------------------------------------------------

def test_business_calls_send_xhr_headers_and_cache_buster(env: Env) -> None:
    spy = RecordingTransport(mock_transport(env.campus))
    client = env.client(login=True, transport=spy)
    spy.requests.clear()
    client.lectures_payload(2, 10)
    [request] = spy.requests
    assert request.method == "POST"
    assert request.headers["x-requested-with"] == "XMLHttpRequest"
    assert request.headers["origin"] == "http://127.0.0.1"
    assert request.headers["user-agent"].startswith("Mozilla/5.0")
    assert "_" in request.url.params
    assert parse_qs(request.content.decode()) == {"pageIndex": ["2"], "pageSize": ["10"]}


def test_requests_record_rtt_and_server_timing(env: Env) -> None:
    client = env.client(login=True)
    client.reservations_payload(1, 1)
    assert len(client.metrics.series["mine.rtt_ms"]) >= 1
    assert len(client.metrics.series["mine.server_ms"]) >= 1
    assert client.date_samples  # every response with a Date header is kept for clock sync


@pytest.mark.parametrize(("fmt", "marker"), [("form", b"wid="), ("paramJson", b"paramJson=")])
def test_submit_encodings_are_both_understood(env: Env, fmt: str, marker: bytes) -> None:
    spy = RecordingTransport(mock_transport(env.campus))
    client = env.client(login=True, transport=spy, submit_format=fmt)
    client.captcha_image()
    payload = client.submit(LECTURE_ID, "1234")
    assert classify_reply(payload)[0] is Reply.SUCCESS
    assert LECTURE_ID in env.campus.reservations
    submit = next(r for r in spy.requests if r.url.path.endswith("addReservation.do"))
    assert submit.content.startswith(marker)
    if fmt == "paramJson":
        body = parse_qs(submit.content.decode())["paramJson"][0]
        assert json.loads(body) == {"wid": LECTURE_ID, "vcode": "1234"}


def test_probe_date_returns_server_time(make_env: Callable[..., Env]) -> None:
    env = make_env(clock_offset_s=-100.0)
    sample = env.client().probe_date()
    server = parse_http_date(sample.date)
    assert server is not None
    assert abs(server - (time.time() - 100)) < 2
    assert sample.sent <= sample.received


def test_probe_date_requires_a_date_header() -> None:
    client = client_with(lambda r: httpx.Response(200))
    with pytest.raises(ProtocolError, match="Date"):
        client.probe_date()
    client.close()


# ---- cookies ---------------------------------------------------------------------------------------------------

def test_cookie_export_and_import_restore_the_session(env: Env) -> None:
    cookies = env.client(login=True).export_cookies()
    assert {c["name"] for c in cookies} >= {"JSESSIONID", "CASID"}
    assert all(set(c) == {"name", "value", "domain", "path"} for c in cookies)

    fresh = env.client()
    assert not fresh.session_alive()
    fresh.import_cookies(cookies)
    assert fresh.session_alive()


def test_cookie_export_only_includes_school_hosts(env: Env) -> None:
    client = env.client(login=True)
    client.http.cookies.set("tracker", "1", domain="ads.example.com", path="/")
    assert "tracker" not in {c["name"] for c in client.export_cookies()}


def test_fake_campus_is_not_shared_between_instances() -> None:
    first, second = FakeCampus(), FakeCampus()
    first.reservations.add("x")
    assert not second.reservations
    assert first.public_key != second.public_key


def test_context_manager_closes_the_client(env: Env) -> None:
    with CampusClient(env.endpoints(), transport=mock_transport(env.campus)) as client:
        assert not client.http.is_closed
    assert client.http.is_closed


def test_call_returns_parsed_json_object(env: Env) -> None:
    client = env.client(login=True)
    data: dict[str, Any] = client.lectures_payload()
    assert data["code"] == "0"
    assert "hdlbList" in data["datas"]
