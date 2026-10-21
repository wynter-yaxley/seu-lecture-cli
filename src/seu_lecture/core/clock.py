"""Server clock-offset estimation and precise, low-CPU scheduling.

Clock offset
    The HTTP ``Date`` header only has 1-second resolution. A single sample says
    the server clock was in ``[D, D+1)`` at some instant inside our local
    ``[sent, received]`` window, i.e. ``offset in (D - received, D + 1 - sent)``.
    Intersecting several samples whose send time is aimed at a predicted
    second boundary narrows the interval to roughly the RTT. The result always
    carries an explicit error bound and is discarded if samples contradict
    each other (e.g. load-balanced backends with different clocks).

Scheduling
    The wall-clock target is converted to a ``perf_counter_ns`` deadline. Long
    waits sleep in chunks and re-anchor if wall and monotonic clocks diverge
    (suspend/resume, NTP step). The last few milliseconds use short sleeps and
    a final <=1.5 ms spin, so CPU stays idle almost all the time.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from seu_lecture.core.client import DateSample
from seu_lecture.core.metrics import wall_time


def parse_http_date(value: str) -> float | None:
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    # A naive result means no usable zone (e.g. merged duplicate headers).
    return moment.timestamp() if moment.tzinfo is not None else None


@dataclass
class ClockEstimate:
    offset_ms: float | None       # server - local; None when unknown
    error_ms: float | None        # half width of the feasible interval
    rtt_ms: float | None
    samples: int
    consistent: bool = True

    def to_dict(self) -> dict[str, object]:
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 1)

        return {"offset_ms": r(self.offset_ms), "error_ms": r(self.error_ms),
                "rtt_ms": r(self.rtt_ms), "samples": self.samples,
                "consistent": self.consistent}


class OffsetBounds:
    def __init__(self) -> None:
        self.lo = -math.inf
        self.hi = math.inf
        self.rtts: list[float] = []
        self.count = 0
        self.consistent = True

    def add(self, sample: DateSample) -> None:
        server = parse_http_date(sample.date)
        if server is None:
            return
        self.count += 1
        self.rtts.append((sample.received - sample.sent) * 1000)
        lo, hi = server - sample.received, server + 1 - sample.sent
        new_lo, new_hi = max(self.lo, lo), min(self.hi, hi)
        if new_lo > new_hi:
            self.consistent = False
            return
        self.lo, self.hi = new_lo, new_hi

    @property
    def width(self) -> float:
        return self.hi - self.lo

    def estimate(self) -> ClockEstimate:
        rtt = statistics.median(self.rtts) if self.rtts else None
        if not self.consistent or not math.isfinite(self.width):
            return ClockEstimate(None, None, rtt, self.count, self.consistent)
        return ClockEstimate((self.lo + self.hi) / 2 * 1000, self.width / 2 * 1000, rtt,
                             self.count, True)


def estimate_offset(probe: Callable[[], DateSample], *, max_samples: int = 8,
                    target_error_ms: float = 25.0,
                    wall: Callable[[], float] = wall_time,
                    sleep: Callable[[float], None] = time.sleep) -> ClockEstimate:
    """Probe at most ``max_samples`` times (about one per second)."""
    bounds = OffsetBounds()
    bounds.add(probe())
    while bounds.count < max_samples and bounds.consistent:
        est = bounds.estimate()
        if est.offset_ms is None or est.error_ms is None or est.error_ms <= target_error_ms:
            break
        rtt = (est.rtt_ms or 0) / 1000
        offset = est.offset_ms / 1000
        # Aim the expected server-side instant at the next whole server second.
        boundary = math.floor(wall() + offset) + 1
        send_at = boundary - offset - rtt / 2
        while send_at - wall() < 0.05:
            boundary += 1
            send_at += 1
        sleep(max(0.0, send_at - wall()))
        bounds.add(probe())
    return bounds.estimate()


@dataclass
class WaitResult:
    target_wall: float
    late_ms: float                 # >0 means fired after the deadline
    clock_jumps: list[float] = field(default_factory=list)  # wall-monotonic divergence, ms


class Scheduler:
    """Hybrid sleep/spin waiter. All clocks are injectable for tests."""

    def __init__(self, *, wall: Callable[[], float] = wall_time,
                 mono_ns: Callable[[], int] = time.perf_counter_ns,
                 sleep: Callable[[float], None] = time.sleep,
                 chunk_s: float = 5.0, jump_threshold_ms: float = 250.0,
                 spin_ns: int = 1_500_000) -> None:
        self.wall = wall
        self.mono_ns = mono_ns
        self.sleep = sleep
        self.chunk_s = chunk_s
        self.jump_threshold_ms = jump_threshold_ms
        self.spin_ns = spin_ns

    def wait_until(self, target_wall: float,
                   on_jump: Callable[[float], None] | None = None) -> WaitResult:
        jumps: list[float] = []
        ref_wall, ref_mono = self.wall(), self.mono_ns()
        deadline = ref_mono + int((target_wall - ref_wall) * 1e9)
        # Coarse phase: chunked sleeps, watching for suspend / clock steps.
        while True:
            remaining = deadline - self.mono_ns()
            if remaining <= 2_000_000_000:
                break
            self.sleep(min(self.chunk_s, remaining / 1e9 - 1.5))
            now_wall, now_mono = self.wall(), self.mono_ns()
            drift_ms = ((now_wall - ref_wall) - (now_mono - ref_mono) / 1e9) * 1000
            if abs(drift_ms) > self.jump_threshold_ms:
                jumps.append(drift_ms)
                if on_jump:
                    on_jump(drift_ms)
                deadline = now_mono + int((target_wall - now_wall) * 1e9)
            ref_wall, ref_mono = now_wall, now_mono
        # Fine phase: monotonic clock only.
        while True:
            remaining = deadline - self.mono_ns()
            if remaining <= self.spin_ns:
                break
            # Sleep most of the remainder; OS timers may overshoot by ~1 ms.
            self.sleep(max((remaining - self.spin_ns) / 1e9 * 0.8, 0.0002))
        while self.mono_ns() < deadline:
            pass
        late_ms = (self.mono_ns() - deadline) / 1e6
        return WaitResult(target_wall, late_ms, jumps)
