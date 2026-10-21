"""``seu-lecture benchmark --mock``: latency breakdown against a loopback mock.

What is measured (all in milliseconds):

* ``schedule.*``    - how late the scheduler fires vs. its deadline (local only).
* ``*.rtt_ms``      - client send -> response parsed, per endpoint.
* ``*.server_ms``   - mock server processing time (from ``Server-Timing``).
* ``*.client_overhead_ms`` - rtt minus server time: client stack + loopback.
* ``ocr_ms``        - one ddddocr inference on a warmed model.
* ``critical_path.*`` - fire -> submit reply, with and without captcha prefetch.
* ``connection.*``  - first request on a new connection vs. a warm keep-alive one.
* ``e2e.*``         - full ReservationTask runs (prepare/wait/fire/verify) with the
                      same solver as ``--ocr``; the captcha is prefetched before the
                      opening, so OCR is not on the fire -> reply path.

No request ever leaves 127.0.0.1. Real-network latency to the school is NOT
measured here; see ``doctor`` for a single connectivity sample.
"""

from __future__ import annotations

import platform
import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from seu_lecture.core.auth import CasLogin, Credentials
from seu_lecture.core.captcha import FixedSolver, OcrSolver, Solver
from seu_lecture.core.client import CampusClient, Endpoints
from seu_lecture.core.clock import Scheduler, wall_time
from seu_lecture.core.lecture import SHANGHAI
from seu_lecture.core.metrics import Metrics, ms_since, now_ns, summarize
from seu_lecture.core.reserve import ReservationTask, ReserveOptions, classify_reply
from seu_lecture.mock import LECTURE_ID, FakeCampus, MockConfig, MockServer


def run_mock_benchmark(*, iterations: int = 50, latency_ms: float = 2.0, ocr: str = "real",
                       schedule_samples: int = 50, e2e_runs: int = 3,
                       progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    say = progress or (lambda msg: None)
    config = MockConfig(latency_ms=latency_ms, captcha_answer=None, capacity=10**9)
    campus = FakeCampus(config)
    with MockServer(campus) as base:
        endpoints = Endpoints.for_base(base)
        metrics = Metrics()
        client = CampusClient(endpoints, metrics=metrics)
        CasLogin(client).login(Credentials(config.username, config.password), fingerprint="bm")
        solver: Solver = OcrSolver() if ocr == "real" else FixedSolver()
        say("warming up OCR")
        ocr_warmup = solver.warmup()

        say(f"scheduler precision x{schedule_samples}")
        scheduler = Scheduler()
        late, wall_err = [], []
        for _ in range(schedule_samples):
            target = wall_time() + 0.03 + random.random() * 0.02
            result = scheduler.wait_until(target)
            wall_err.append((wall_time() - target) * 1000)
            late.append(result.late_ms)

        say(f"critical path x{iterations}")
        prefetch, jit, overhead = [], [], []
        for _ in range(iterations):
            campus.reset_reservations()
            t0 = now_ns()
            image = client.captcha_image()
            t_ocr = now_ns()
            code = solver.solve(image)
            metrics.add("ocr_ms", ms_since(t_ocr))
            t_submit = now_ns()
            reply, _ = classify_reply(client.submit(LECTURE_ID, code))
            prefetch.append(ms_since(t_submit))
            jit.append(ms_since(t0))
            rtt, server = metrics.last("submit.rtt_ms"), metrics.last("submit.server_ms")
            if rtt is not None and server is not None:
                overhead.append(rtt - server)
            if reply.value != "success":
                raise RuntimeError(f"mock benchmark got unexpected reply {reply}")
            client.reservations_payload()

        say("cold vs warm connections")
        cold, warm = [], []
        for _ in range(min(iterations, 20)):
            fresh = CampusClient(endpoints)
            fresh.import_cookies(client.export_cookies())
            t = now_ns()
            fresh.reservations_payload(1, 1)
            cold.append(ms_since(t))
            t = now_ns()
            fresh.reservations_payload(1, 1)
            warm.append(ms_since(t))
            fresh.close()

        e2e = _e2e(campus, endpoints, config, solver, e2e_runs, say) if e2e_runs else {}
        client.close()

    series = metrics.summary()
    return {
        "environment": {
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
            "mock_latency_ms": latency_ms, "iterations": iterations, "ocr": ocr,
            "time": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        },
        "ocr_warmup_ms": round(ocr_warmup, 1),
        "schedule": {"late_ms": summarize(late), "wall_error_ms": summarize(wall_err)},
        "requests": {k: v for k, v in series.items() if k.endswith(("rtt_ms", "server_ms"))},
        "submit_client_overhead_ms": summarize(overhead),
        "ocr_ms": series.get("ocr_ms"),
        "critical_path": {"prefetched_captcha_ms": summarize(prefetch),
                          "jit_captcha_ms": summarize(jit)},
        "connection": {"cold_ms": summarize(cold), "warm_ms": summarize(warm)},
        "e2e": e2e,
    }


def _e2e(campus: FakeCampus, endpoints: Endpoints, config: MockConfig, solver: Solver,
         runs: int, say: Callable[[str], None]) -> dict[str, Any]:
    """Full ReservationTask runs with the mock opening ~2.5 s in the future."""
    late, first_reply, verified, server_late = [], [], [], []
    for i in range(runs):
        say(f"end-to-end task {i + 1}/{runs}")
        campus.reset_reservations()
        client = CampusClient(endpoints)
        CasLogin(client).login(Credentials(config.username, config.password), fingerprint="bm")
        open_at = time.time() + 2.5  # after login, so set-up cost never shows up as lateness
        config.open_at = open_at
        options = ReserveOptions(lecture_id=LECTURE_ID, at=datetime.fromtimestamp(open_at, SHANGHAI),
                                 clock="local", offset_ms=20, captcha_lead_ms=1000)
        campus.submit_times.clear()
        result = ReservationTask(client, solver, options, relogin=lambda: None).run()
        client.close()
        if result.outcome.value != "confirmed":
            raise RuntimeError(f"e2e run ended with {result.outcome}: {result.message}")
        late.append(result.timings.get("schedule_late_ms") or 0.0)
        first_reply.append(result.timings.get("fire_to_first_reply_ms") or 0.0)
        verified.append(result.timings.get("fire_to_verified_ms") or 0.0)
        if campus.submit_times:
            server_late.append((campus.submit_times[0] - open_at) * 1000)
    config.open_at = None
    return {
        "runs": runs,
        "schedule_late_ms": summarize(late),
        "fire_to_first_reply_ms": summarize(first_reply),
        "fire_to_verified_ms": summarize(verified),
        "server_receipt_after_open_ms": summarize(server_late),
    }
