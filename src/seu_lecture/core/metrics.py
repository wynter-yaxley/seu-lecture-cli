"""Lightweight latency bookkeeping (no third-party dependencies)."""

from __future__ import annotations

import math
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

now_ns = time.perf_counter_ns


def _wall_clock() -> Callable[[], float]:
    """``time.time`` with sub-millisecond resolution everywhere.

    On Windows, Python < 3.13 reads ``GetSystemTimeAsFileTime`` whose nominal
    resolution is 15.6 ms; that is too coarse for the offset bounds and the
    wall -> monotonic anchoring, so the precise variant is used there.
    """
    if sys.platform == "win32" and sys.version_info < (3, 13):
        try:
            import ctypes

            precise = ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime
            ticks = ctypes.c_uint64()

            def wall() -> float:
                precise(ctypes.byref(ticks))
                return ticks.value / 1e7 - 11_644_473_600  # FILETIME epoch is 1601

            wall()
            return wall
        except (AttributeError, OSError):
            pass
    return time.time


wall_time = _wall_clock()


def ms_since(start_ns: int) -> float:
    return (now_ns() - start_ns) / 1e6


def percentile(values: Iterable[float], q: float) -> float | None:
    """Linear-interpolated percentile, ``q`` in [0, 100]."""
    data = sorted(values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def summarize(values: Iterable[float], digits: int = 3) -> dict[str, float | int | None]:
    data = list(values)

    def r(v: float | None) -> float | None:
        return None if v is None else round(v, digits)

    return {
        "count": len(data),
        "p50": r(percentile(data, 50)),
        "p95": r(percentile(data, 95)),
        "p99": r(percentile(data, 99)),
        "min": r(min(data)) if data else None,
        "max": r(max(data)) if data else None,
        "mean": r(sum(data) / len(data)) if data else None,
    }


_SERVER_TIMING = re.compile(r"dur=(\d+(?:\.\d+)?)(?![\d.])")


def parse_server_timing(header: str | None) -> float | None:
    """Sum ``dur=`` values of a ``Server-Timing`` header (ms). Real SEU servers do not
    send it; the mock server does, which lets benchmarks split network vs server time."""
    if not header:
        return None
    values = [float(v) for v in _SERVER_TIMING.findall(header)]
    return sum(values) if values else None


class Metrics:
    """Named series of millisecond samples. Holds numbers only, never request content."""

    def __init__(self) -> None:
        self.series: dict[str, list[float]] = defaultdict(list)

    def add(self, name: str, ms: float) -> None:
        self.series[name].append(ms)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        start = now_ns()
        try:
            yield
        finally:
            self.add(name, ms_since(start))

    def last(self, name: str) -> float | None:
        values = self.series.get(name)
        return values[-1] if values else None

    def summary(self) -> dict[str, dict[str, float | int | None]]:
        return {name: summarize(values) for name, values in sorted(self.series.items())}
