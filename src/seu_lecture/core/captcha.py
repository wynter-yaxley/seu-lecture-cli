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


# A read at least this confident is submitted; below it, a fresh captcha is cheaper
# (~50 ms) than a wasted submit.
CONFIDENT = 0.8
DIGITS = "0123456789"


class Solver(Protocol):
    def warmup(self) -> float: ...

    def solve(self, image: bytes) -> str: ...

    def read(self, image: bytes) -> tuple[str, float]: ...


class OcrSolver:
    """Both bundled ddddocr models, decoded over the captcha alphabet only.

    Real captchas (sampled 2026-10) are 70x30 JPEGs with 4 digits, drawn from a
    pool of a few hundred images. ddddocr's own decoding picks from thousands of
    characters (``set_ranges`` is broken in 1.6.1), so the raw model output is
    decoded here over digits only. On 60 real samples the two models agreed on
    56, mostly with high confidence. ``read`` returns ``(text, confidence)``;
    the reservation task replaces doubtful reads with a fresh captcha.
    """

    def __init__(self, *, length: int | None = 4, charset: str = DIGITS) -> None:
        self.length = length
        self.charset = charset
        self._engines: list[Any] = []

    def warmup(self) -> float:
        """Load the models and run one inference. Returns elapsed milliseconds."""
        start = now_ns()
        if not self._engines:
            try:
                import ddddocr
            except ImportError as exc:  # pragma: no cover - dependency is mandatory
                raise CaptchaError("ddddocr is not installed") from exc
            for beta in (False, True):
                try:
                    ocr = ddddocr.DdddOcr(show_ad=False, beta=beta)
                except TypeError:  # newer releases dropped show_ad
                    ocr = ddddocr.DdddOcr(beta=beta)
                self._engines.append(ocr)
        self.read(_blank_png(), check=False)
        return ms_since(start)

    def solve(self, image: bytes) -> str:
        return self.read(image)[0]

    def read(self, image: bytes, *, check: bool = True) -> tuple[str, float]:
        if not self._engines:
            self.warmup()
        from PIL import Image

        try:
            picture = Image.open(io.BytesIO(image))
            picture.load()
        except Exception as exc:
            raise CaptchaError("captcha image cannot be decoded") from exc
        reads = [self._decode(ocr, picture) for ocr in self._engines]
        fits = [r for r in reads if r[0] and (not self.length or len(r[0]) == self.length)]
        if not fits:
            if check:
                raise CaptchaError("OCR result rejected", length=len(reads[0][0]))
            return reads[0]
        text, conf = max(fits, key=lambda r: r[1])
        if len(fits) == len(reads) and len({r[0] for r in fits}) == 1:
            return text, conf
        return text, conf / 2  # the models disagree

    def _decode(self, ocr: Any, picture: Any) -> tuple[str, float]:
        """Greedy CTC decode restricted to ``charset``; confidence = weakest character."""
        import numpy as np

        engine = getattr(ocr, "ocr_engine", None)
        if engine is None:  # unknown ddddocr internals: plain text, neutral confidence
            raw = ocr.classification(picture)
            return "".join(c for c in str(raw) if c in self.charset), 0.5
        chars = engine.charset_manager.get_charset()
        allowed = np.array([0] + [i for i, c in enumerate(chars) if c and c in self.charset])
        tensor = engine._preprocess_image(picture, False)
        out = engine.session.run(None, {engine.session.get_inputs()[0].name: tensor})[0]
        logits = (out[:, 0, :] if out.shape[1] == 1 else out[0])[:, allowed]
        prob = np.exp(logits - logits.max(axis=1, keepdims=True))
        prob /= prob.sum(axis=1, keepdims=True)
        text, confs, prev = "", [], 0
        for t, k in enumerate(prob.argmax(axis=1)):
            if k and k != prev:
                text += chars[allowed[k]]
                confs.append(float(prob[t, k]))
            elif k and k == prev:
                confs[-1] = max(confs[-1], float(prob[t, k]))
            prev = k
        return text, min(confs, default=0.0)


class FixedSolver:
    """Returns a constant answer. Used by tests and the mock benchmark."""

    def __init__(self, answer: str = "1234") -> None:
        self.answer = answer

    def warmup(self) -> float:
        return 0.0

    def solve(self, image: bytes) -> str:
        return self.answer

    def read(self, image: bytes) -> tuple[str, float]:
        return self.solve(image), 1.0


def _blank_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (100, 36), "white").save(buf, format="PNG")
    return buf.getvalue()
