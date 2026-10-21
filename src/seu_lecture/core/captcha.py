"""Captcha image extraction and OCR.

ddddocr is imported lazily and warmed up during the preparation phase, so the
critical request path never pays model-loading cost.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
from typing import Any, Protocol

from seu_lecture.core.errors import CaptchaError, ProtocolError
from seu_lecture.core.metrics import ms_since, now_ns

MAX_IMAGE_BYTES = 1_000_000
_IMAGE_KEYS = ("result", "datas", "data", "img_b64", "img")


def _find_image(value: Any, depth: int = 0) -> str | None:
    if depth > 3:
        return None
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return value
        if len(value) > 64 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value):
            return value
        return None
    if isinstance(value, dict):
        ordered = [k for k in _IMAGE_KEYS if k in value] + [k for k in value if k not in _IMAGE_KEYS]
        for key in ordered:
            if found := _find_image(value[key], depth + 1):
                return found
    return None


def decode_captcha_payload(payload: dict[str, Any]) -> bytes:
    """Extract image bytes from a ``vcode.do`` response.

    Observed shapes: ``{"result": "data:image/...;base64,..."}`` (undergraduate
    userscript) and ``{"datas": "data:image/..."}`` (graduate module). Both are
    accepted; anything else is a protocol error rather than a guess.
    """
    source = _find_image(payload)
    if not source:
        raise ProtocolError("captcha response contains no image")
    if "," in source:
        source = source.split(",", 1)[1]
    if len(source) > MAX_IMAGE_BYTES * 2:
        raise ProtocolError("captcha image is unreasonably large")
    try:
        image = base64.b64decode("".join(source.split()), validate=True)
    except (binascii.Error, ValueError):
        raise ProtocolError("captcha image is not valid base64") from None
    if not image:
        raise ProtocolError("captcha image is empty")
    return image


class Solver(Protocol):
    def warmup(self) -> float: ...

    def solve(self, image: bytes) -> str: ...


class OcrSolver:
    """ddddocr wrapper. ``length`` rejects obviously wrong reads before a submit is spent.

    Real captchas (2026-10) are 70x30 JPEGs with 4 digits; ddddocr 1.6.1 reads
    about half of them right (it confuses 1 and 7 with high confidence), so the
    reservation loop simply fetches a new captcha after a captcha error.
    ``set_ranges`` is deliberately not used: in 1.6.1 it makes every result empty.
    """

    def __init__(self, *, length: int | None = None) -> None:
        self.length = length
        self._ocr: Any = None

    def warmup(self) -> float:
        """Load the model and run one inference. Returns elapsed milliseconds."""
        start = now_ns()
        if self._ocr is None:
            try:
                import ddddocr
            except ImportError as exc:  # pragma: no cover - dependency is mandatory
                raise CaptchaError("ddddocr is not installed") from exc
            try:
                self._ocr = ddddocr.DdddOcr(show_ad=False)
            except TypeError:  # newer releases dropped show_ad
                self._ocr = ddddocr.DdddOcr()
        self._ocr.classification(_blank_png())
        return ms_since(start)

    def solve(self, image: bytes) -> str:
        if self._ocr is None:
            self.warmup()
        raw = self._ocr.classification(image)
        text = re.sub(r"[^0-9A-Za-z]", "", raw if isinstance(raw, str) else "")
        if not text or (self.length and len(text) != self.length):
            raise CaptchaError("OCR result rejected", length=len(text))
        return text


class FixedSolver:
    """Returns a constant answer. Used by tests and the mock benchmark."""

    def __init__(self, answer: str = "1234") -> None:
        self.answer = answer

    def warmup(self) -> float:
        return 0.0

    def solve(self, image: bytes) -> str:
        return self.answer


def _blank_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (100, 36), "white").save(buf, format="PNG")
    return buf.getvalue()
