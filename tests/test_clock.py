"""Server clock-offset estimation and the precise scheduler."""

from __future__ import annotations

import math
import time

import pytest

from seu_lecture.core.client import ClockSample
from seu_lecture.core.clock import (
    ClockEstimate,
    OffsetBounds,
    Scheduler,
    estimate_offset,
)


class SimulatedServer:
    """Virtual local clock plus a server whose clock runs ``offset`` seconds ahead.

    The server stamps responses with a whole-second ``Date`` header, exactly like HTTP.
    """

    def __init__(self, offset: float, rtt: float = 0.010, start: float = 1_700_000_000.123) -> None:
        self.now, self.offset, self.rtt = start, offset, rtt
        self.probes = 0

    def wall(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def probe(self) -> ClockSample:
        self.probes += 1
        sent = self.now
        arrival = sent + self.rtt / 2 + self.offset  # server-side instant of the request
        self.now += self.rtt
        return ClockSample(sent, self.now, float(math.floor(arrival)))


# ---- estimate_offset ---------------------------------------------------------------------

@pytest.mark.parametrize("offset", [0.3721, -0.3721, 0.0, 1.9, -2.5])
def test_estimate_offset_brackets_the_true_offset(offset: float) -> None:
    server = SimulatedServer(offset)
    est = estimate_offset(server.probe, wall=server.wall, sleep=server.sleep)
    assert est.consistent
    assert est.offset_ms is not None
    assert est.error_ms is not None
    assert abs(est.offset_ms - offset * 1000) <= est.error_ms + 0.01
    assert est.error_ms <= 30
    assert est.rtt_ms == pytest.approx(10.0, abs=0.01)
    assert 2 <= est.samples <= 8


def test_estimate_with_a_single_sample_is_about_half_a_second_wide() -> None:
    server = SimulatedServer(0.3721)
    est = estimate_offset(server.probe, max_samples=1, wall=server.wall, sleep=server.sleep)
    assert est.samples == 1
    assert est.error_ms == pytest.approx(505, abs=1)  # (1 s + rtt) / 2
    assert est.offset_ms is not None
    assert abs(est.offset_ms - 372.1) <= est.error_ms


def test_estimate_stops_probing_once_precise_enough() -> None:
    server = SimulatedServer(0.3721)
    est = estimate_offset(server.probe, target_error_ms=1000, wall=server.wall,
                          sleep=server.sleep)
    assert server.probes == 1
    assert est.samples == 1


def test_estimate_probes_at_most_one_per_second_and_honours_max_samples() -> None:
    server = SimulatedServer(0.3721)
    start = server.now
    est = estimate_offset(server.probe, max_samples=4, target_error_ms=1, wall=server.wall,
                          sleep=server.sleep)
    assert server.probes == est.samples == 4
    assert server.now - start < 4 * 1.1


def test_inconsistent_samples_yield_no_offset() -> None:
    # Two load-balanced backends whose clocks disagree by several seconds.
    samples = iter([
        ClockSample(1000.0, 1000.01, 1000.0),
        ClockSample(2000.0, 2000.01, 2005.0),
    ])
    est = estimate_offset(lambda: next(samples), wall=lambda: 1500.0, sleep=lambda s: None)
    assert est.consistent is False
    assert est.offset_ms is None
    assert est.error_ms is None
    assert est.samples == 2


def test_offset_bounds_intersect() -> None:
    bounds = OffsetBounds()
    bounds.add(ClockSample(1000.0, 1000.0, 1000.0))  # offset in (0, 1)
    assert bounds.width == pytest.approx(1.0)
    bounds.add(ClockSample(1000.6, 1000.6, 1001.0))  # offset in (0.4, 1.4)
    assert (bounds.lo, bounds.hi) == pytest.approx((0.4, 1.0))
    est = bounds.estimate()
    assert est.offset_ms == pytest.approx(700)
    assert est.error_ms == pytest.approx(300)


def test_clock_estimate_to_dict_rounds() -> None:
    est = ClockEstimate(372.14159, 4.96, 10.04, 5)
    assert est.to_dict() == {"offset_ms": 372.1, "error_ms": 5.0, "rtt_ms": 10.0, "samples": 5,
                             "consistent": True}
    assert ClockEstimate(None, None, None, 0).to_dict()["offset_ms"] is None


# ---- Scheduler ----------------------------------------------------------------------------

def test_wait_until_real_clock_is_precise() -> None:
    # Lenient for noisy CI: best of three runs must be under 5 ms late (typically < 0.5 ms).
    late = []
    for _ in range(3):
        target = time.time() + 0.05
        result = Scheduler().wait_until(target)
        late.append(result.late_ms)
        assert result.target_wall == target
        assert result.clock_jumps == []
    assert min(late) >= 0
    assert min(late) < 5


def test_wait_until_past_target_returns_immediately() -> None:
    start = time.perf_counter()
    result = Scheduler().wait_until(time.time() - 1)
    assert time.perf_counter() - start < 0.1
    assert result.late_ms > 900


class FakeClocks:
    """Monotonic clock that ticks 1 us per read; wall clock follows it plus optional steps."""

    def __init__(self, jump_after_first_sleep: float = 0.0) -> None:
        self.mono = 1_000_000
        self.step = 0.0
        self.pending_jump = jump_after_first_sleep
        self.slept: list[float] = []

    def mono_ns(self) -> int:
        self.mono += 1000
        return self.mono

    def wall(self) -> float:
        return 1_700_000_000.0 + self.mono / 1e9 + self.step

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.mono += int(seconds * 1e9)
        if self.pending_jump and len(self.slept) == 1:
            self.step += self.pending_jump  # e.g. NTP step or laptop resume

    def scheduler(self) -> Scheduler:
        return Scheduler(wall=self.wall, mono_ns=self.mono_ns, sleep=self.sleep)


def test_coarse_phase_sleeps_in_chunks_without_jumps() -> None:
    clocks = FakeClocks()
    target = clocks.wall() + 12
    result = clocks.scheduler().wait_until(target)
    assert clocks.slept[:2] == [5.0, 5.0]
    assert result.clock_jumps == []
    assert 0 <= result.late_ms < 0.1
    assert clocks.wall() == pytest.approx(target, abs=1e-3)


def test_clock_jump_is_detected_and_deadline_re_anchored() -> None:
    clocks = FakeClocks(jump_after_first_sleep=3.0)
    target = clocks.wall() + 10
    reported: list[float] = []
    result = clocks.scheduler().wait_until(target, on_jump=reported.append)
    assert len(result.clock_jumps) == 1
    assert result.clock_jumps[0] == pytest.approx(3000, abs=1)
    assert reported == result.clock_jumps
    # Without re-anchoring the wait would end 3 s late in wall time (monotonic deadline);
    # re-anchored, it fires at the wall-clock target.
    assert clocks.wall() == pytest.approx(target, abs=1e-3)


def test_backward_clock_step_is_also_detected() -> None:
    clocks = FakeClocks(jump_after_first_sleep=-2.0)
    target = clocks.wall() + 10
    result = clocks.scheduler().wait_until(target)
    assert len(result.clock_jumps) == 1
    assert result.clock_jumps[0] == pytest.approx(-2000, abs=1)
    assert clocks.wall() == pytest.approx(target, abs=1e-3)


def test_small_drift_below_threshold_is_not_a_jump() -> None:
    clocks = FakeClocks(jump_after_first_sleep=0.1)
    target = clocks.wall() + 10
    result = clocks.scheduler().wait_until(target)
    assert result.clock_jumps == []
