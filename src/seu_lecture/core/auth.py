"""SEU CAS login (auth.seu.edu.cn) and secure credential storage.

Independent implementation of the public CAS protocol; no third-party code copied.
Flow: needCaptcha -> getChiperKey -> casLogin (RSA PKCS#1 v1.5 password) ->
follow the ticket redirect into ehall -> verify a business call works.
An untrusted device triggers SMS second-factor (sendStage2Code + mobileVerifyCode).
"""

from __future__ import annotations

import base64
import contextlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from seu_lecture.core.client import CampusClient, parse_json_response
from seu_lecture.core.errors import AuthFailed, ProtocolError, SessionExpired, SmsRequired

KEYRING_SERVICE = "seu-lecture-cli"
_INSECURE_BACKENDS = ("fail", "null", "plaintext", "keyrings.alt", "chainer")


def new_fingerprint() -> str:
    """Stable random device id; CAS remembers it as trusted after an SMS login."""
    return secrets.token_hex(16)


def encrypt_password(public_key: str, text: str) -> str:
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        der = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
        key = load_der_public_key(der)
    except (ValueError, TypeError):
        raise ProtocolError("CAS public key is invalid") from None
    if not isinstance(key, rsa.RSAPublicKey):
        raise ProtocolError("CAS public key is not RSA")
    return base64.b64encode(key.encrypt(text.encode(), padding.PKCS1v15())).decode()


@dataclass
class Credentials:
    username: str
    password: str = field(repr=False)


class CasLogin:
    def __init__(self, client: CampusClient) -> None:
        self.client = client

    def _post(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.request("POST", self.client.endpoints.auth + name, json=body,
                                       metric="cas")
        return parse_json_response(response)

    def login(self, creds: Credentials, *, fingerprint: str | None,
              sms_code: str | None = None) -> None:
        auth = self.client.endpoints.auth
        need = parse_json_response(self.client.request("GET", auth + "needCaptcha", metric="cas"))
        if need.get("success") is False or ("code" in need and need.get("code") != 200):
            raise AuthFailed("CAS requires an image captcha for this login; "
                             "log in once in a browser, then retry", code=need.get("code"))
        key = self._post("getChiperKey", {}).get("publicKey")
        if not isinstance(key, str) or not key:
            raise ProtocolError("CAS did not return a public key")
        body: dict[str, Any] = {
            "captcha": "",
            "loginType": "account",
            "mobilePhoneNum": "",
            "mobileVerifyCode": encrypt_password(key, sms_code) if sms_code else "",
            "password": encrypt_password(key, creds.password),
            "rememberMe": False,
            "service": self.client.endpoints.service,
            "username": creds.username,
            "wxBinded": False,
            "fingerPrint": fingerprint,
        }
        result = self._post("casLogin", body)
        info = str(result.get("info") or "")
        redirect = result.get("redirectUrl")
        ok = result.get("success") is True or result.get("code") == 200
        if not ok or not isinstance(redirect, str) or not redirect:
            if (result.get("needStage2Validation") is True or result.get("code") == 502
                    or "非可信设备" in info):
                raise SmsRequired("SMS verification required for this device")
            raise AuthFailed(info[:120] or "CAS login rejected", code=result.get("code"))
        self._enter_app(redirect)

    def _enter_app(self, redirect: str) -> None:
        if "://" not in redirect:
            redirect = unquote(redirect)
        final = self.client.follow(redirect)
        host = urlsplit(str(final.url)).hostname
        if final.status_code != 200 or host not in (urlsplit(self.client.endpoints.app).hostname,):
            raise AuthFailed("could not enter the lecture system after CAS login",
                             http_status=final.status_code)
        if not self.client.session_alive():
            raise SessionExpired("lecture system did not accept the CAS ticket")

    def send_sms(self, username: str) -> str:
        result = self._post("sendStage2Code", {"userId": username})
        if result.get("success") is not True and result.get("code") != 200:
            raise AuthFailed(str(result.get("info") or "failed to send SMS code")[:120])
        return str(result.get("info") or "SMS code sent")


class CredentialStore:
    """Password and session cookies live only in the OS keyring
    (Windows Credential Manager / macOS Keychain / Secret Service).
    Without a secure backend nothing is persisted."""

    def __init__(self) -> None:
        self._backend: Any = None
        self._checked = False

    def backend(self) -> Any:
        if not self._checked:
            self._checked = True
            try:
                import keyring

                backend = keyring.get_keyring()
                name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
                if not any(bad in name for bad in _INSECURE_BACKENDS):
                    self._backend = backend
                elif "chainer" in name and getattr(backend, "backends", None):
                    secure = [b for b in backend.backends
                              if not any(bad in type(b).__module__.lower()
                                         for bad in _INSECURE_BACKENDS)]
                    self._backend = secure[0] if secure else None
            except Exception:
                self._backend = None
        return self._backend

    @property
    def available(self) -> bool:
        return self.backend() is not None

    @property
    def backend_name(self) -> str | None:
        b = self.backend()
        return type(b).__name__ if b is not None else None

    def _get(self, key: str) -> str | None:
        b = self.backend()
        if b is None:
            return None
        try:
            return b.get_password(KEYRING_SERVICE, key)
        except Exception:
            return None

    def _set(self, key: str, value: str) -> bool:
        b = self.backend()
        if b is None:
            return False
        try:
            b.set_password(KEYRING_SERVICE, key, value)
            return True
        except Exception:  # e.g. Windows credential blob size limit
            return False

    def _delete(self, key: str) -> None:
        b = self.backend()
        if b is not None:
            with contextlib.suppress(Exception):
                b.delete_password(KEYRING_SERVICE, key)

    def get_password(self, username: str) -> str | None:
        return self._get(f"password:{username}")

    def set_password(self, username: str, password: str) -> bool:
        return self._set(f"password:{username}", password)

    def get_session(self, username: str) -> list[dict[str, str]] | None:
        raw = self._get(f"session:{username}")
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    def set_session(self, username: str, cookies: list[dict[str, str]]) -> bool:
        return self._set(f"session:{username}", json.dumps(cookies, separators=(",", ":")))

    def clear(self, username: str) -> None:
        self._delete(f"password:{username}")
        self._delete(f"session:{username}")
