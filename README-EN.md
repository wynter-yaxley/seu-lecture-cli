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

## Why

Popular lectures fill up seconds after booking opens. This tool filters out lectures that clash with your classes,
submits at the exact opening moment with a captcha already solved, and only reports success once the lecture shows up
in "my reservations".

You don't need to use the command line yourself: install it once, then just talk to your AI agent
(Hermes, Claude Code, Codex, …). The diagrams below are in Chinese.

![Agent workflow](docs/images/agent-flow.svg)

## Features

- **Timetable-aware filtering**: checks each lecture's actual time span against your classes (odd/even weeks, moved,
  cancelled and make-up classes). Missing information yields `unknown`, never "free".
- **Timed reservation**: submits at the opening measured on the school server's own clock (≈0.1 s), with a confidently
  read captcha prepared beforehand; a wrong read is retried with a fresh captcha at once.
- **Verified results**: success only counts once confirmed in "my reservations"; unclear outcomes are checked first,
  never blindly resubmitted.
- **Resilient**: short network outages, server clock skew, expired sessions, a moved opening time and rate limits are handled.
- **Background tasks**: `reserve --detach` hands the task to its own process (a transient systemd user unit where
  available); closing the chat or restarting the agent does not stop it. Several lectures can wait side by side.
- **Seat watching** (opt-in, `--watch N`): keeps polling a full lecture and submits when a seat frees up.
- **Agent-ready**: bundled Agent Skill, stable JSON (`--json`), documented error and exit codes.
- **Private**: the password is typed by you only and stored in the OS keyring; captchas are recognised locally.

## Quick start with an agent

1. **Install** [uv](https://docs.astral.sh/uv/), then the CLI, on the machine where your agent runs
   (campus network or the school VPN client required):

   ```bash
   uv tool install git+https://github.com/wynter-yaxley/seu-lecture-cli
   uv tool update-shell && seu-lecture --version
   ```

2. **Log in yourself** in that machine's terminal with your SEU card number (一卡通号; add `--insecure-storage` on WSL /
   servers without a keyring, which stores the password in a file readable only by you, like `gh`).
   **Nothing is shown while you type the password** (not even `*`): type it in full and press Enter.

   ```bash
   seu-lecture auth login -u 213xxxxxx
   ```

3. **Give the agent the skill**: copy [`skills/seu-lecture/`](skills/seu-lecture/) into its skills directory
   (Hermes `~/.hermes/skills/<category>/seu-lecture/`, Claude Code `~/.claude/skills/seu-lecture/`), or ask the agent to do it.

Then ask things like *"Which lectures don't clash with my classes?"*, *"Reserve the second one"*, *"Did I get it?"*.
The skill requires the agent to get your explicit consent **for each lecture**, never touch your password, never change
settings on its own and only report success after verification.

On the day: keep the computer on and awake, stay on the campus network, and **don't open the booking captcha in a
browser before the opening** (the school warns against using several devices at once). Attend what you booked.

![Timeline of a reservation task](docs/images/timeline.svg)

## Without an agent

```bash
seu-lecture list --free                     # lectures that do not clash with your classes
seu-lecture show 5f6766 --schedule          # details; a 6-character id prefix is enough
seu-lecture schedule --week 6               # timetable with real dates and times
seu-lecture reserve 5f6766 --dry-run        # full rehearsal without submitting
seu-lecture reserve 5f6766 --yes --detach   # timed reservation in the background
seu-lecture status                          # progress and results of reservation tasks
seu-lecture reservations                    # my reservations
```

Every command takes `--help` and `--json`. Exit codes: 0 ok · 1 failed · 2 usage · 3 auth/SMS · 4 network ·
5 protocol · 6 not found · 7 task running · 10 rejected/refused · 11 unconfirmed · 12 confirmation required · 130 interrupted.

## More

- [How it works](docs/how-it-works.md) (Chinese): conflict check, every step of a reservation task, JSON output,
  configuration, real-system validation, code layout
- [Agent Skill](skills/seu-lecture/SKILL.md) and [reference](skills/seu-lecture/reference.md) (Chinese)
- [Changelog](CHANGELOG.md)

## Development

```bash
uv sync && uv run pytest && uv run ruff check .   # 400+ tests against a mock campus; nothing touches the school
```

Independent implementation; protocol details cross-checked with [Warma10032/fetch_lecture](https://github.com/Warma10032/fetch_lecture)
(MIT; its GPL-3.0 `seu_auth.py` was not copied), [MuZiJin701/seu-lecture-reserve-http](https://github.com/MuZiJin701/seu-lecture-reserve-http),
[candyb0x/SEU-Lecture-Revenge](https://github.com/candyb0x/SEU-Lecture-Revenge) and the timetable app's own front-end code.
Captchas are read with [ddddocr](https://github.com/sml2h3/ddddocr). License: [MIT](LICENSE).
