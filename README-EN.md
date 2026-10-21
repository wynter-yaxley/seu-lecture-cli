<div align="center">

# seu-lecture-cli

**Unofficial CLI for Southeast University (SEU) undergraduate humanities lecture reservations**

Low latency · verified results · agent-friendly

[![CI](https://github.com/wynter-yaxley/seu-lecture-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/wynter-yaxley/seu-lecture-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

[简体中文](README.md) | English

</div>

> [!WARNING]
> This is an **unofficial** personal open-source tool with no affiliation to Southeast University.
> Follow the university's rules, and only reserve lectures **you will attend** with **your own account**.
> Success is never guaranteed and the school system may change at any time. Use at your own risk.

## Features

| | |
|---|---|
| 🎯 **Undergraduate system** | Talks to the undergraduate module `modules/hdyy_bk/`, never the graduate module `modules/hdyy/` |
| ⏱️ **Precise timing** | A long-running, pre-warmed process: login, connection, OCR model and captcha are ready before the opening time, so only one request is on the critical path |
| 🕐 **Clock calibration** | Estimates server clock skew from HTTP `Date` second transitions, always with an explicit error bound (no false millisecond precision) |
| ✅ **Verified results** | A "success" reply only counts after the lecture appears in *My reservations*; when the outcome is unclear it checks first and never blindly resubmits |
| 🤖 **Agent-friendly** | `--json` gives stable JSON / JSONL with a schema version, error codes and exit codes |
| 🔐 **Credential safety** | Password and session live only in the OS keyring; TLS verification is always on |
| 🧪 **Reproducible tests** | Built-in mock campus system covering login, SMS 2FA, full lectures, wrong captchas, timeouts and ambiguous replies |

## Install

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are recommended.

```bash
uv tool install git+https://github.com/wynter-yaxley/seu-lecture-cli
# or from source
git clone https://github.com/wynter-yaxley/seu-lecture-cli && cd seu-lecture-cli
uv sync && uv run seu-lecture --help
```

The school system is reachable from the **campus network** (or through the school VPN client). Captcha OCR uses [ddddocr](https://github.com/sml2h3/ddddocr) (~150 MB of dependencies incl. onnxruntime/OpenCV).

## Quick start

```bash
seu-lecture doctor                         # environment, keyring, campus reachability, clock skew
seu-lecture auth login -u 213xxxxxx        # prompts for password (and SMS code on new devices)
seu-lecture list --available               # lectures you can still reserve
seu-lecture show 8f1c2d                    # details (unique id prefix >= 6 chars works)
seu-lecture reserve 8f1c2d --dry-run       # full rehearsal without submitting
seu-lecture reserve 8f1c2d                 # fires at the lecture's reservation start (asks first)
seu-lecture reserve 8f1c2d --at 2026-10-22T19:00:00+08:00
seu-lecture reservations                   # my reservations
seu-lecture status                         # latest / running task
```

`reserve` is a long-running process: start it a few minutes before the opening time and leave it running.

## Commands

| Command | Purpose | Writes to school system |
|---|---|---|
| `auth login` / `auth status` / `auth logout` | Login (SMS 2FA supported), status, clear stored secrets | no |
| `list` | Lectures (`-k` keyword, `--available`) | no |
| `show <id>` | Lecture details (`--raw` for original fields) | no |
| `reservations` | My reservations | no |
| `reserve <id>` | Timed reservation task | **yes** (confirmation or `--yes`) |
| `status` | Task status | no |
| `doctor` | Diagnostics (`--offline`, `--ocr`) | no |
| `benchmark --mock` | Local p50/p95/p99 benchmark | no (127.0.0.1 only) |

`reserve` options: `--at` (ISO 8601, defaults to the lecture's reservation start; naive times are Beijing time), `--offset-ms` (target arrival after opening, default 50), `--clock auto|local|server` (auto covers both clock candidates when they disagree), `--no-prefetch`, `--max-attempts` (default 8), `--dry-run`, `--yes`.

## How a reservation task runs

1. **Prepare** – validate session, load the lecture, check it is not already reserved, warm up OCR.
2. **Wait** – low-frequency keep-alive; estimate clock skew at T-60 s; re-check the session at T-20 s; prefetch and solve the captcha at T-1.5 s (also warms the connection).
3. **Fire** – hybrid sleep/spin on `perf_counter_ns()`, send only `addReservation`. No disk I/O, no UI updates, GC paused; events are buffered.
4. **Verify** – success means "visible in My reservations". Timeouts, disconnects, 5xx, HTML / empty / broken-JSON replies and unknown replies trigger a list check first (re-login happens inside verification; never re-login-and-resubmit). Resubmission happens only if a *completely read* list proves the lecture is absent (bounded). A missing/null list, a business error or pagination cut off at the page limit is never treated as "absent"; the task stops with `unconfirmed`.

**Two clocks, no bet.** Which clock decides "open" cannot be observed from outside – the `Date` header may come from a front-end proxy. On 2026-10-21 every `*.seu.edu.cn` host (ehall, auth, www, jwc, mirrors …) returned a `Date` **~6 min 15 s fast**, while the local clock, Baidu/Cloudflare, campus NTP (`ntp.seu.edu.cn`) and public NTP all agreed within 20 ms. In `auto` mode, when the two clocks disagree beyond the estimate's error, the task probes the earlier candidate with at most 2 attempts and, on "not open", fires again at the later one with the rest of the budget; `--max-attempts` caps all candidates together. The server-clock candidate is placed at the late edge of the estimate's error interval, so an estimation error never makes it arrive early. A `candidate_not_open` event in the stream tells you the application server uses the correct time.

Retries are bounded: wrong captcha → new captcha; "not open yet" → retried within a 3 s window; "too frequent" or HTTP 429 → one backoff honouring `Retry-After` (bounded to 60 s; longer requests end with `rate_limited`); all with a 200 ms minimum spacing and `--max-attempts`.

## Agent / script integration

With `--json`, stdout carries only UTF-8 JSON (no ANSI, no logs). Regular commands print one envelope:

```json
{"schema_version":"1","ok":true,"command":"list","data":[...]}
{"schema_version":"1","ok":false,"command":"list","error":{"code":"AUTH_REQUIRED","message":"...","retryable":false},"exit_code":3}
```

`reserve --json` streams JSONL events (`phase`, `prepared`, `clock`, `armed`, `attempt`, `relogin`, `warning`, ...); the last line is always `result` (with `outcome`) or `error`.

Outcomes: `confirmed`, `already_reserved`, `dry_run`, `full`, `conflict`, `closed`, `not_open`, `rate_limited`, `unconfirmed`, `failed`, `aborted`.

Exit codes: `0` success · `1` internal/failed · `2` usage/config · `3` auth/SMS required · `4` network · `5` protocol · `6` not found · `7` task already running · `10` rejected (full/conflict/closed/not open/rate limited) · `11` unconfirmed · `12` confirmation required · `130` interrupted.

Agents query, create tasks (only after explicit user authorization, via `--yes`) and read `status --json`; precise timing is done by the separate `reserve` process. Non-interactive login: `--password-stdin`; on `SMS_REQUIRED` the code has been sent – rerun with `--sms-code`. Business logic lives in `seu_lecture.api.LectureService`, so a future MCP adapter can call it directly.

## Configuration

Optional TOML at `%APPDATA%\seu-lecture\config.toml` (Windows), `~/Library/Application Support/seu-lecture/config.toml` (macOS) or `~/.config/seu-lecture/config.toml` (Linux); see the Chinese README for a full example (`[network]`, `[captcha]`, `[reserve]`, `[protocol]`). Environment: `SEU_LECTURE_USERNAME`, `SEU_LECTURE_PASSWORD`, `SEU_LECTURE_HOME`. State files contain no password or cookies.

## Performance

WSL2 (Linux 6.6) · Python 3.11.15 · httpx 0.28.1; mock server on 127.0.0.1 with 2 ms simulated processing;
`seu-lecture benchmark --mock -n 200 --ocr real --e2e 20`. Milliseconds. End-to-end rows are full `reserve` tasks using the same solver as `--ocr`; the captcha is prefetched, so OCR is not on the fire → reply path. **Local mock only** – this measures client overhead and timing precision, not real-network reservation speed.

| Category | Metric | p50 | p95 | p99 | n |
|---|---|---:|---:|---:|---:|
| Local scheduling | trigger error vs wall clock | 0.014 | 0.026 | 0.046 | 200 |
| Server | `addReservation` processing (`Server-Timing`) | 2.178 | 2.293 | 2.354 | 200 |
| Network + server | `addReservation` round trip | 3.342 | 3.779 | 4.055 | 200 |
| Client overhead | round trip − server (incl. loopback + mock HTTP stack) | 1.140 | 1.546 | 1.884 | 200 |
| OCR | one ddddocr inference (warm) | 5.127 | 6.243 | 6.719 | 200 |
| Critical path | fire → reply, **prefetched captcha** | 3.439 | 3.897 | 4.182 | 200 |
| Critical path | fire → reply, captcha fetched at fire time | 11.751 | 13.165 | 13.644 | 200 |
| End to end | full task: fire → first reply | 3.939 | 4.406 | 4.629 | 20 |
| End to end | full task: fire → verified in My reservations | 7.270 | 8.320 | 8.360 | 20 |
| End to end | server receipt − opening (target +20 ms) | 21.856 | 22.255 | 22.356 | 20 |

Takeaways: captcha prefetch removes one round trip plus ~5 ms OCR from the critical path; scheduling error is below 0.1 ms; OCR cold start is ~1.4 s, so it must happen during preparation. A single real read-only probe from campus (n=1, not a percentile): ehall TCP 15 ms / TLS done 24 ms, auth TCP 22 ms / TLS done 39 ms – a fresh HTTPS connection costs far more than on loopback, hence connection warm-up. Fair client comparison (same mock, keep-alive, 2000 interleaved rounds): httpx p50/p95/p99 = 0.748/0.996/1.128 ms vs requests 0.832/1.037/1.209 ms; httpx was chosen for MockTransport, granular timeouts and configurable keep-alive expiry, not speed. No measured benefit justifies concurrency or async, so the client stays single-threaded and synchronous.

## Validation status and limitations

- ✅ Mock-based tests: login, SMS 2FA, queries, reservation, verification, failure handling; CI on Windows and Linux.
- ✅ Real system, unauthenticated read-only probes only: reachability, TLS verification, redirect to CAS.
- ⏳ **Not yet validated against the real system:** CAS login with a real account, lecture/reservation field names, `vcode.do` format and OCR accuracy, `addReservation.do` encoding and reply texts, whether a prefetched captcha stays valid (`--no-prefetch` disables prefetching). **No real reservation has ever been submitted.**
- Off-campus WebVPN gateway login is not supported; use the campus network or the VPN client.
- If CAS requires an image captcha (e.g. after failed logins), log in once in a browser.
- Without a secure keyring (e.g. headless Linux/WSL) nothing is persisted; use the prompt or `SEU_LECTURE_PASSWORD`.

## Security

No `verify=False`; only `auth.seu.edu.cn` and `ehall.seu.edu.cn` are allowed and `http://` redirects are upgraded to `https://`. The password is RSA-encrypted with the CAS key before sending and stored only in the OS keyring. Output never includes passwords, cookies, tickets or captcha content. Reservation is the only write and requires confirmation or `--yes`. One task at a time (lock file).

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run seu-lecture benchmark --mock
uv run python benchmarks/compare_http_clients.py
```

## Acknowledgements

Independent implementation; no code was copied. Protocol details were cross-checked against public projects: [Warma10032/fetch_lecture](https://github.com/Warma10032/fetch_lecture) (MIT, undergraduate `hdyy_bk` endpoints; its `seu_auth.py` by Golevka2001 is GPL-3.0 and was **not** copied), [MuZiJin701/seu-lecture-reserve-http](https://github.com/MuZiJin701/seu-lecture-reserve-http) and [seu_lecture_reserve](https://github.com/MuZiJin701/seu_lecture_reserve) (CAS codes, graduate-module semantics), [candyb0x/SEU-Lecture-Revenge](https://github.com/candyb0x/SEU-Lecture-Revenge) (reply texts), and CLI conventions from [cli/cli](https://github.com/cli/cli) and [astral-sh/uv](https://github.com/astral-sh/uv).

## License

[MIT](LICENSE)
