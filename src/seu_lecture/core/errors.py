"""Error types shared by the core and every front end.

Each error carries a stable machine-readable ``code`` and a process ``exit_code``.
Messages must never contain passwords, cookies, tickets or captcha content.
"""

from __future__ import annotations


class SeuLectureError(Exception):
    code = "INTERNAL_ERROR"
    exit_code = 1
    retryable = False

    def __init__(self, message: str = "", **detail: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail:
            data["detail"] = self.detail
        return data


class ConfigError(SeuLectureError):
    code = "CONFIG_ERROR"
    exit_code = 2


class AuthRequired(SeuLectureError):
    code = "AUTH_REQUIRED"
    exit_code = 3


class AuthFailed(SeuLectureError):
    code = "AUTH_FAILED"
    exit_code = 3


class SmsRequired(SeuLectureError):
    """CAS considers this device untrusted and wants an SMS code."""

    code = "SMS_REQUIRED"
    exit_code = 3


class SessionExpired(SeuLectureError):
    code = "SESSION_EXPIRED"
    exit_code = 3
    retryable = True


class NetworkError(SeuLectureError):
    code = "NETWORK_ERROR"
    exit_code = 4
    retryable = True

    def __init__(self, message: str = "", *, maybe_delivered: bool = True, **detail: object):
        super().__init__(message, **detail)
        # False only when the request provably never reached the server
        # (DNS / connect failure). Reservation logic relies on this flag.
        self.maybe_delivered = maybe_delivered


class NetworkTimeout(NetworkError):
    code = "NETWORK_TIMEOUT"


class ProtocolError(SeuLectureError):
    """The school system answered with something this client does not understand."""

    code = "PROTOCOL_ERROR"
    exit_code = 5


class CaptchaError(SeuLectureError):
    code = "CAPTCHA_ERROR"
    exit_code = 5
    retryable = True


class NotFound(SeuLectureError):
    code = "NOT_FOUND"
    exit_code = 6


class TaskRunning(SeuLectureError):
    code = "TASK_RUNNING"
    exit_code = 7


class ConfirmationRequired(SeuLectureError):
    code = "CONFIRMATION_REQUIRED"
    exit_code = 12
