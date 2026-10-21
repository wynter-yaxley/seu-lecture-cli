"""Fair local comparison: httpx.Client vs requests.Session (dev-only script).

Both clients hit the same loopback mock server, reuse one keep-alive
connection, send the same form POST and parse the same JSON. Order is
interleaved per round to spread out any warm-up / CPU frequency effects.

    uv run python benchmarks/compare_http_clients.py --rounds 2000
"""

from __future__ import annotations

import argparse
import json
import platform
import time

import httpx
import requests

from seu_lecture.core.metrics import summarize
from seu_lecture.mock import API, FakeCampus, MockConfig, MockServer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=2000)
    args = parser.parse_args()

    campus = FakeCampus(MockConfig())
    with MockServer(campus) as base:
        # Authenticated business session shared by both clients.
        sid = "bench"
        campus.sessions[sid] = {"calls": 0, "captcha": None}
        url = base + API + "queryActivityList.do"
        form = {"pageIndex": "1", "pageSize": "100"}
        cookies = {"JSESSIONID": sid}

        hx = httpx.Client(cookies=cookies, timeout=10)
        rq = requests.Session()
        rq.cookies.update(cookies)

        def run_httpx() -> float:
            t = time.perf_counter_ns()
            hx.post(url, data=form).json()
            return (time.perf_counter_ns() - t) / 1e6

        def run_requests() -> float:
            t = time.perf_counter_ns()
            rq.post(url, data=form, timeout=10).json()
            return (time.perf_counter_ns() - t) / 1e6

        for _ in range(50):  # warm-up
            run_httpx()
            run_requests()
        results: dict[str, list[float]] = {"httpx": [], "requests": []}
        for i in range(args.rounds):
            order = (("httpx", run_httpx), ("requests", run_requests))
            for name, fn in order if i % 2 == 0 else reversed(order):
                results[name].append(fn())
        hx.close()
        rq.close()

    print(json.dumps({
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "httpx": httpx.__version__,
        "requests": requests.__version__,
        "rounds": args.rounds,
        "ms": {name: summarize(values) for name, values in results.items()},
    }, indent=2))


if __name__ == "__main__":
    main()
