"""Captcha payload decoding and the OCR wrapper (with a stubbed model)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import pytest
from conftest import Env

from seu_lecture.core.captcha import FixedSolver, OcrSolver, _blank_png, decode_captcha_payload
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

PICTURE = _blank_png()  # decodable image; the scripted engines ignore it


class Scripted(OcrSolver):
    """Skips the real models: each engine \"reads\" a preset (text, confidence)."""

    def __init__(self, *reads: tuple[str, float], **options: Any) -> None:
        super().__init__(**options)
        self._engines = list(reads)

    def _decode(self, ocr: Any, picture: Any) -> tuple[str, float]:
        return ocr


def test_ocr_models_agreeing_keep_their_confidence() -> None:
    assert Scripted(("4495", 0.9), ("4495", 0.99)).read(PICTURE) == ("4495", 0.99)


def test_ocr_disagreement_halves_confidence_and_prefers_the_surer_model() -> None:
    assert Scripted(("2982", 0.81), ("2282", 0.65)).read(PICTURE) == ("2982", 0.405)
    assert Scripted(("740", 1.0), ("7407", 0.69)).read(PICTURE) == ("7407", 0.345)


def test_ocr_rejects_wrong_length() -> None:
    with pytest.raises(CaptchaError) as info:
        Scripted(("123", 1.0), ("12", 1.0)).solve(PICTURE)
    assert info.value.retryable
    assert Scripted(("123", 1.0), ("12", 1.0), length=None).solve(PICTURE) == "123"


def test_fixed_solver() -> None:
    solver = FixedSolver("9876")
    assert solver.warmup() == 0.0
    assert solver.solve(IMAGE) == "9876"
    assert FixedSolver().solve(IMAGE) == "1234"
