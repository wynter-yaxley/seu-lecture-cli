<div align="center">

# seu-lecture-cli

**Unofficial CLI for Southeast University (SEU) undergraduate humanities lecture reservations**

Timetable-aware lecture filtering · timed reservation · verified results · agent-friendly

[![CI](https://github.com/wynter-yaxley/seu-lecture-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/wynter-yaxley/seu-lecture-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

[简体中文](README.md) | English

</div>

> [!WARNING]
> **Unofficial**, not affiliated with SEU. Follow the university's rules and only reserve lectures you will attend,
> with your own account. Success is not guaranteed; the school system may change at any time.

## What it does

- **Timetable-aware filtering**: reads "我的课表" and checks each lecture's **actual time span** against your classes
  (odd/even weeks, moved, cancelled and make-up classes). Missing information yields `unknown`, never "free".
- **Timed reservation**: a long-running process logs in, warms the connection, has a confidently read captcha ready
  before the opening and submits at the opening measured on the lecture app's own clock (one request on the critical path).
- **Seat watching**: if the lecture is full (or not open yet), it keeps polling the seat count for 60 minutes by default
  and submits as soon as someone cancels.
- **Verified results**: success only counts once the lecture shows up in "my reservations"; unclear outcomes are
  checked before anything is resubmitted.
- **Stable JSON** (`--json`) with error codes and exit codes; credentials only in the OS keyring; TLS always verified.

## Install & use

Python 3.11+, [uv](https://docs.astral.sh/uv/), campus network (or the school VPN client).

```bash
uv tool install git+https://github.com/wynter-yaxley/seu-lecture-cli
seu-lecture auth login -u 213xxxxxx     # password prompt; SMS code on a new device
seu-lecture list --free                 # lectures that do not clash with your classes
seu-lecture schedule --week 6           # timetable with real dates and times
seu-lecture reserve 5f6766 --dry-run    # full rehearsal without submitting
seu-lecture reserve 5f6766              # timed reservation (asks first); start a few minutes early
seu-lecture reservations
```

Commands: `auth login|status|logout`, `list` (`-k`, `--available`, `--schedule`, `--free`), `show <id>`
(`--schedule`, `--raw`), `schedule` (`--week`, `--date`), `reservations`, `reserve <id>` (`--at`, `--dry-run`,
`--yes`, `--ignore-schedule`, `--watch`, `--offset-ms`, `--clock auto|local`, `--max-attempts`, `--no-prefetch`), `status`.

## Conflict check

All inputs are official: term / week of a date / per-week classes / class changes from the timetable app
(`dqxnxq.do`, `cxjcs.do`, `dqzc.do`, `xskcb.do`, `xsdkkc.do`); period clock times from the Academic Affairs Office
[timetable](https://jwc.seu.edu.cn/_upload/article/files/c3/32/c3b5a51f41ddab0e8f2e66069bf2/0ba0d8c2-fa9e-4e0e-8b4b-333d815d15b8.pdf)
(since autumn 2024, 13 periods), cross-checked against the per-term period counts the app reports; a mismatch
makes times unknown. Results: `free`, `conflict` (with the overlapping classes) or `unknown` (with a reason).
`reserve` re-checks before submitting and refuses `conflict` (`SCHEDULE_CONFLICT`) and `unknown`
(`SCHEDULE_UNKNOWN`), exit code 10, unless `--ignore-schedule`.

## Speed and safety

- **Timing**: the app clock (`xtdqsj`) is sampled a minute before the opening (error ≈ ±50 ms). The submit is sent for
  the *latest* possible opening moment + 10 ms with no network lead, so it never arrives early (an early arrival costs
  a whole "not open" round trip); the price is at most ~2× the error plus one network trip (≈0.1 s in practice).
- **Captcha**: real captchas are 4-digit 70×30 images from a pool of a few hundred. Both bundled ddddocr models are
  decoded over digits only; a read counts as confident when both agree and every digit is clear (55 of 60 real
  samples, ~23 ms each). From 3 s before the opening, captchas are fetched (~50 ms each, up to 6) until one is confident.
- **Burst**: wrong captcha → new captcha at once; "not open" retried for 3 s; ≥100 ms between submits; `--max-attempts` per burst.
- **Watch**: after a full / not-open / failed burst, the seat count is polled every 2 s and a new burst fires when a
  seat frees up, until success, the reservation deadline, the lecture start or `--watch` minutes.
- **Verification**: success only counts once the lecture is in "my reservations". Timeouts, disconnects and unreadable
  replies trigger a list check; a resubmit happens only if a *completely read* list proves absence, otherwise the
  result is `unconfirmed` (exit 11). HTTP 429 / "too frequent": `Retry-After` is honoured (≤60 s).

Exit codes: 0 ok · 1 failed/internal · 2 usage · 3 auth/SMS · 4 network · 5 protocol · 6 not found · 7 task running ·
10 rejected/refused · 11 unconfirmed · 12 confirmation required · 130 interrupted.

**Using it from an AI agent?** Put [AGENT_GUIDE.md](AGENT_GUIDE.md) (Chinese) in the agent's context: it defines the
rules the agent must follow (explicit per-lecture consent before `reserve --yes`, no credential handling, no polling),
the standard workflow and how to handle every outcome.

## Validation status (2026-10-21)

- ✅ Real system, read-only: login (saved password → CAS) and TGT reuse; lecture list with real start/end times;
  timetable (term, weeks, per-week classes, a real moved class) and conflict filtering; captcha download and OCR
  (60 samples, 55 confident reads, consistent with manual reading); app clock `xtdqsj` ≈ local clock (−17 ± 49 ms)
  while every `*.seu.edu.cn` `Date` header was ~375 s fast (not used); full `--dry-run` rehearsals (no submit).
- ⏳ **A real reservation has not been submitted or verified yet**; `addReservation.do` encoding and reply texts are unverified.
  Seat watching is verified against the mock only.
- Off-campus WebVPN gateway is not supported.

## Development

```bash
uv sync && uv run pytest && uv run ruff check .   # tests use a mock campus only
```

Independent implementation; protocol details cross-checked with [Warma10032/fetch_lecture](https://github.com/Warma10032/fetch_lecture)
(MIT; its GPL-3.0 `seu_auth.py` was not copied), [MuZiJin701/seu-lecture-reserve-http](https://github.com/MuZiJin701/seu-lecture-reserve-http),
[candyb0x/SEU-Lecture-Revenge](https://github.com/candyb0x/SEU-Lecture-Revenge) and the timetable app's own front-end code. License: [MIT](LICENSE).
