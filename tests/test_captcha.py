"""Captcha payload decoding and the OCR wrapper (with a stubbed model)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import pytest
from conftest import Env

from seu_lecture.core.captcha import FixedSolver, OcrSolver, decode_captcha_payload
from seu_lecture.core.errors import CaptchaError, ProtocolError

IMAGE = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
B64 = base64.b64encode(IMAGE).decode()
DATA_URL = "data:image/png;base64," + B64


@pytest.mark.parametrize("payload", [
    {"result": DATA_URL},
    {"datas": DATA_URL},
    {"data": DATA_URL},
    {"code": "0", "datas": {"img": DATA_URL}},          # nested one level
    {"result": B64},                                    # bare base64 without the data: prefix
    {"result": "data:image/png;base64," + "\n".join(
        B64[i:i + 20] for i in range(0, len(B64), 20))},  # line-wrapped
])
def test_decode_accepts_observed_shapes(payload: dict[str, Any]) -> None:
    assert decode_captcha_payload(payload) == IMAGE


def test_decode_prefers_known_keys() -> None:
    other = base64.b64encode(b"other-image").decode()
    payload = {"zzz": "data:image/png;base64," + other, "result": DATA_URL}
    assert decode_captcha_payload(payload) == IMAGE


@pytest.mark.parametrize(("payload", "message"), [
    ({}, "no image"),
    ({"code": "0"}, "no image"),
    ({"result": 123}, "no image"),
    ({"result": "short"}, "no image"),
    ({"result": None}, "no image"),
    ({"a": {"b": {"c": {"d": {"result": DATA_URL}}}}}, "no image"),  # nested too deep
    ({"result": "data:image/png;base64,@@@@"}, "not valid base64"),
    ({"result": "data:image/png;base64,"}, "empty"),
    ({"result": "data:image/png;base64," + "A" * 2_000_004}, "unreasonably large"),
])
def test_decode_rejects_invalid_payloads(payload: dict[str, Any], message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        decode_captcha_payload(payload)


@pytest.mark.parametrize("key", ["result", "datas"])
def test_client_downloads_png_for_both_response_keys(
        make_env: Callable[..., Env], key: str) -> None:
    env = make_env(captcha_key=key)
    image = env.client(login=True).captcha_image()
    assert image.startswith(b"\x89PNG")
    assert env.campus.calls["vcode.do"] == 1


# ---- solvers ------------------------------------------------------------------------

class StubOcr:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def classification(self, image: bytes) -> Any:
        return self.answer


def ocr(answer: Any, **options: Any) -> OcrSolver:
    solver = OcrSolver(**options)
    solver._ocr = StubOcr(answer)  # skip loading the real ddddocr model
    return solver


def test_ocr_strips_non_alphanumerics() -> None:
    assert ocr("a1-2 b\n").solve(b"img") == "a12b"


@pytest.mark.parametrize("answer", ["", "--", b"1234", None])
def test_ocr_rejects_empty_results(answer: Any) -> None:
    with pytest.raises(CaptchaError) as info:
        ocr(answer).solve(b"img")
    assert info.value.retryable


def test_ocr_rejects_wrong_length() -> None:
    solver = ocr("123", length=4)
    with pytest.raises(CaptchaError) as info:
        solver.solve(b"img")
    assert info.value.detail == {"length": 3}
    assert ocr("1234", length=4).solve(b"img") == "1234"


def test_fixed_solver() -> None:
    solver = FixedSolver("9876")
    assert solver.warmup() == 0.0
    assert solver.solve(IMAGE) == "9876"
    assert FixedSolver().solve(IMAGE) == "1234"
