"""CAS login: success, failures, SMS second factor and password encryption."""

from __future__ import annotations

import base64
from collections.abc import Callable

import httpx
import pytest
from conftest import Env, RecordingTransport
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from seu_lecture.core.auth import CasLogin, encrypt_password
from seu_lecture.core.errors import AuthFailed, ProtocolError, SmsRequired
from seu_lecture.mock import mock_transport


def test_login_success(env: Env) -> None:
    client = env.client()
    CasLogin(client).login(env.credentials(), fingerprint="fp")
    assert client.session_alive()
    assert env.campus.calls["casLogin"] == 1
    assert "JSESSIONID" in {c["name"] for c in client.export_cookies()}


def test_wrong_password_raises_auth_failed(env: Env) -> None:
    client = env.client()
    with pytest.raises(AuthFailed) as info:
        CasLogin(client).login(env.credentials("not-the-password"), fingerprint="fp")
    assert info.value.code == "AUTH_FAILED"
    assert info.value.exit_code == 3
    assert "not-the-password" not in info.value.message
    assert not client.session_alive()


def test_unknown_user_raises_auth_failed(env: Env) -> None:
    client = env.client()
    creds = env.credentials()
    creds.username = "someone-else"
    with pytest.raises(AuthFailed):
        CasLogin(client).login(creds, fingerprint="fp")


def test_password_is_never_sent_in_clear(env: Env) -> None:
    spy = RecordingTransport(mock_transport(env.campus))
    client = env.client(transport=spy)
    CasLogin(client).login(env.credentials(), fingerprint="fp")
    assert spy.requests
    for request in spy.requests:
        assert env.cfg.password.encode() not in request.content
        assert env.cfg.password not in str(request.url)


# ---- SMS second factor ---------------------------------------------------------

def test_untrusted_device_raises_sms_required(make_env: Callable[..., Env]) -> None:
    env = make_env(require_sms=True)
    with pytest.raises(SmsRequired) as info:
        CasLogin(env.client()).login(env.credentials(), fingerprint="fp")
    assert info.value.exit_code == 3
    assert not env.campus.sms_sent


def test_service_login_sends_sms_and_uses_prompted_code(make_env: Callable[..., Env]) -> None:
    env = make_env(require_sms=True)
    prompts: list[str] = []

    def prompt(info: str) -> str:
        prompts.append(info)
        return f" {env.cfg.sms_code} "  # whitespace must be stripped

    data = env.service(sms_prompt=prompt).login(env.cfg.username, env.cfg.password)
    assert data["logged_in"] is True
    assert data["password_saved"] is True
    assert data["session_cached"] is True
    assert len(prompts) == 1
    assert env.campus.sms_sent
    assert len(env.campus.trusted) == 1


def test_device_is_trusted_after_sms_login(make_env: Callable[..., Env]) -> None:
    env = make_env(require_sms=True)
    env.service(sms_prompt=lambda _: env.cfg.sms_code).login(env.cfg.username, env.cfg.password)
    sent = env.campus.calls["sendStage2Code"]

    # A fresh service shares account.json (hence the fingerprint) but has no SMS prompt.
    env.service().login(env.cfg.username, env.cfg.password)
    assert env.campus.calls["sendStage2Code"] == sent
    assert env.campus.calls["casLogin"] == 3  # rejected, accepted with SMS, accepted trusted


def test_non_interactive_login_reports_sms_sent(make_env: Callable[..., Env]) -> None:
    env = make_env(require_sms=True)
    service = env.service()
    with pytest.raises(SmsRequired) as info:
        service.login(env.cfg.username, env.cfg.password)
    assert info.value.detail == {"sms_sent": True}
    assert env.campus.sms_sent
    assert env.store.get_password(env.cfg.username) is None

    data = service.login(env.cfg.username, env.cfg.password, sms_code=env.cfg.sms_code)
    assert data["logged_in"] is True
    assert env.store.get_password(env.cfg.username) == env.cfg.password


def test_wrong_sms_code_is_rejected(make_env: Callable[..., Env]) -> None:
    env = make_env(require_sms=True)
    service = env.service()
    with pytest.raises(SmsRequired):
        service.login(env.cfg.username, env.cfg.password)
    with pytest.raises(AuthFailed):
        service.login(env.cfg.username, env.cfg.password, sms_code="000000")
    assert not env.campus.trusted


def test_service_login_failure_saves_nothing(env: Env) -> None:
    with pytest.raises(AuthFailed):
        env.service().login(env.cfg.username, "wrong")
    assert env.store.get_password(env.cfg.username) is None
    assert env.store.get_session(env.cfg.username) is None


def test_login_can_skip_saving_password(env: Env) -> None:
    data = env.service().login(env.cfg.username, env.cfg.password, save_password=False)
    assert data["password_saved"] is False
    assert data["session_cached"] is True
    assert env.store.get_password(env.cfg.username) is None


# ---- password encryption -------------------------------------------------------

def test_rsa_round_trip(env: Env) -> None:
    secret = "p@ss w0rd/密码+="
    encrypted = encrypt_password(env.campus.public_key, secret)
    assert secret not in encrypted
    assert env.campus._decrypt(encrypted) == secret


def test_rsa_encryption_is_randomised(env: Env) -> None:
    first = encrypt_password(env.campus.public_key, "same")
    second = encrypt_password(env.campus.public_key, "same")
    assert first != second
    assert env.campus._decrypt(first) == env.campus._decrypt(second) == "same"


def test_invalid_public_key_is_protocol_error() -> None:
    with pytest.raises(ProtocolError):
        encrypt_password("not-a-key", "x")


def test_non_rsa_public_key_is_protocol_error() -> None:
    der = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    key = base64.urlsafe_b64encode(der).decode().rstrip("=")
    with pytest.raises(ProtocolError, match="not RSA"):
        encrypt_password(key, "x")


def test_missing_public_key_is_protocol_error(env: Env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("needCaptcha"):
            return httpx.Response(200, json={"code": 200, "success": True})
        return httpx.Response(200, json={"code": 200})  # getChiperKey without publicKey

    client = env.client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProtocolError, match="public key"):
        CasLogin(client).login(env.credentials(), fingerprint="fp")
