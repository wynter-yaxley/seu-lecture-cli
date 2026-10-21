"""Reservation state machine: prepare -> wait -> fire -> verify.

Safety rules enforced here (not in the UI):

* A server "success" is only reported as ``confirmed`` after the reservation
  shows up in ``queryMyActivityList``.
* After a timeout / dropped connection / unknown reply the task queries the
  reservation list first; it resubmits only if the list proves the lecture is
  not reserved, and only a bounded number of times.
* Every retry path is bounded (attempt cap, minimum spacing, one rate-limit backoff).
* The critical section does no disk I/O and emits no output; events raised
  there are buffered and flushed afterwards.
"""

from __future__ import annotations

import gc
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from seu_lecture.core.captcha import Solver
from seu_lecture.core.client import CampusClient
from seu_lecture.core.clock import ClockEstimate, Scheduler, estimate_offset, wall_time
from seu_lecture.core.errors import (
    CaptchaError,
    ConfigError,
    NetworkError,
    ProtocolError,
    RateLimited,
    SessionExpired,
)
from seu_lecture.core.lecture import (
    SHANGHAI,
    Lecture,
    find_lecture,
    load_lectures,
    reservation_state,
)
from seu_lecture.core.metrics import Metrics, ms_since, now_ns


class Outcome(StrEnum):
    CONFIRMED = "confirmed"
    ALREADY_RESERVED = "already_reserved"
    DRY_RUN = "dry_run"
    FULL = "full"
    CONFLICT = "conflict"
    CLOSED = "closed"
    NOT_OPEN = "not_open"
    RATE_LIMITED = "rate_limited"
    UNCONFIRMED = "unconfirmed"
    FAILED = "failed"
    ABORTED = "aborted"


OUTCOME_EXIT = {
    Outcome.CONFIRMED: 0, Outcome.ALREADY_RESERVED: 0, Outcome.DRY_RUN: 0,
    Outcome.FULL: 10, Outcome.CONFLICT: 10, Outcome.CLOSED: 10, Outcome.NOT_OPEN: 10,
    Outcome.RATE_LIMITED: 10, Outcome.UNCONFIRMED: 11, Outcome.FAILED: 1, Outcome.ABORTED: 130,
}


class Reply(StrEnum):
    SUCCESS = "success"
    CAPTCHA = "captcha_error"
    ALREADY = "already_reserved"
    FULL = "full"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    NOT_OPEN = "not_open"
    CLOSED = "closed"
    INVALID = "invalid_request"
    UNKNOWN = "unknown"


# Message keywords seen in public clients of the SEU lecture system. Failure
# keywords are checked before success flags because some failures return code 0.
_RULES: tuple[tuple[Reply, tuple[str, ...]], ...] = (
    (Reply.CAPTCHA, ("验证码",)),
    # Before ALREADY: "与已预约活动时间上存在冲突" mentions 已预约 but is a conflict.
    (Reply.CONFLICT, ("冲突",)),
    (Reply.ALREADY, ("已预约", "重复预约", "已经预约")),
    (Reply.FULL, ("已满", "余量不足", "人数已达")),
    (Reply.RATE_LIMITED, ("频繁",)),
    (Reply.NOT_OPEN, ("未开始", "未开放", "尚未开始", "尚未开放")),
    (Reply.CLOSED, ("已结束", "已截止", "不可预约")),
    (Reply.INVALID, ("非法参数", "参数错误")),
)


def classify_reply(payload: dict[str, Any]) -> tuple[Reply, str]:
    msg = str(payload.get("msg") or payload.get("message") or payload.get("info") or "")
    for reply, words in _RULES:
        if any(w in msg for w in words):
            return reply, msg
    code, datas = payload.get("code"), payload.get("datas")
    if payload.get("success") is True or (code in (0, "0") and datas in (1, "1", True)):
        return Reply.SUCCESS, msg
    if "成功" in msg and "失败" not in msg and "不成功" not in msg:
        return Reply.SUCCESS, msg
    return Reply.UNKNOWN, msg


@dataclass
class ReserveOptions:
    lecture_id: str
    at: datetime | None = None          # default: the lecture's YYKSSJ
    offset_ms: float = 50.0             # aim server-side arrival this long after `at`
    clock: str = "auto"                 # auto | local | server
    compensate_latency: bool = True     # send RTT/2 early
    prefetch_captcha: bool = True
    captcha_lead_ms: float = 1500.0
    clock_sync_lead_s: float = 60.0
    session_check_lead_s: float = 20.0
    keepalive_s: float = 300.0
    max_attempts: int = 8
    max_resubmits: int = 1              # after verified-absent uncertain submits
    min_submit_interval_ms: float = 200.0
    not_open_window_ms: float = 3000.0
    rate_limit_backoff_s: float = 5.0
    verify_attempts: int = 3
    verify_interval_ms: float = 500.0
    dry_run: bool = False

    def validate(self) -> None:
        if self.clock not in ("auto", "local", "server"):
            raise ConfigError("clock must be auto, local or server")
        if not 1 <= self.max_attempts <= 20:
            raise ConfigError("max_attempts must be between 1 and 20")
        if self.min_submit_interval_ms < 100:
            raise ConfigError("min_submit_interval_ms must be >= 100")
        if not -1000 <= self.offset_ms <= 5000:
            raise ConfigError("offset_ms must be between -1000 and 5000")
        if self.keepalive_s < 30:
            raise ConfigError("keepalive_s must be >= 30")
        if not 1 <= self.verify_attempts <= 10:
            raise ConfigError("verify_attempts must be between 1 and 10")
        if not 0 <= self.rate_limit_backoff_s <= 60:
            raise ConfigError("rate_limit_backoff_s must be between 0 and 60")
        if not 0 <= self.max_resubmits <= 3:
            raise ConfigError("max_resubmits must be between 0 and 3")


@dataclass
class TaskResult:
    outcome: Outcome
    message: str
    lecture: dict[str, Any] | None = None
    attempts: int = 0
    submitted: bool = False
    target_time: str | None = None
    clock: dict[str, Any] | None = None
    timings: dict[str, float | None] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return OUTCOME_EXIT[self.outcome]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "message": self.message,
            "lecture": self.lecture,
            "attempts": self.attempts,
            "submitted": self.submitted,
            "target_time": self.target_time,
            "clock": self.clock,
            "timings": {k: (None if v is None else round(v, 3)) for k, v in self.timings.items()},
            "metrics": self.metrics,
        }


class _Done(Exception):
    def __init__(self, outcome: Outcome, message: str) -> None:
        self.outcome = outcome
        self.message = message


Emit = Callable[[dict[str, Any]], None]

# Upper bound for any single backoff, including a server-requested Retry-After.
MAX_BACKOFF_S = 60.0
# Attempts an earlier (non-final) clock candidate may use; the rest is kept for later ones.
PROBE_ATTEMPTS = 2


class ReservationTask:
    def __init__(self, client: CampusClient, solver: Solver, options: ReserveOptions, *,
                 relogin: Callable[[], None], emit: Emit | None = None,
                 scheduler: Scheduler | None = None,
                 wall: Callable[[], float] = wall_time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        options.validate()
        self.client = client
        self.solver = solver
        self.opt = options
        self.relogin = relogin
        self._emit_fn = emit or (lambda event: None)
        self.scheduler = scheduler or Scheduler(wall=wall, sleep=sleep)
        self.wall = wall
        self.sleep = sleep
        self.metrics: Metrics = client.metrics
        self.lecture: Lecture | None = None
        self.clock = ClockEstimate(None, None, None, 0)
        self.attempts = 0
        self.submitted = False
        self.timings: dict[str, float | None] = {}
        self._critical = False
        self._buffer: list[dict[str, Any]] = []
        self._relogins = 0
        self._code: str | None = None  # prefetched captcha answer
        self._last_reply = "none"
        self._last_submit = 0      # perf_counter_ns of the latest submit (spacing spans candidates)
        self._resubmits = 0        # resubmits after verified-absent uncertain submits (task-wide)
        self._rate_limited = False  # one backoff per task

    # ---- events -----------------------------------------------------------

    def emit(self, event: str, **data: Any) -> None:
        record = {"event": event, "wall": round(self.wall(), 3), **data}
        if self._critical:
            self._buffer.append(record)
        else:
            self._emit_fn(record)

    # ---- public -------------------------------------------------------------

    def run(self) -> TaskResult:
        target: float | None = None
        try:
            target = self._prepare()
            self.emit("phase", phase="wait")
            plan = self._plan(target)
            # Long waits: refine the clock estimate shortly before the first candidate.
            while self.opt.clock != "local" and plan[0] - self.wall() > self.opt.clock_sync_lead_s + 30:
                self._idle_until(plan[0] - self.opt.clock_sync_lead_s)
                plan = self._plan(target)
            if self._probe_budget() == 0:
                plan = plan[-1:]  # no attempt can be spared for probing an earlier candidate
            for i, candidate in enumerate(plan):
                fire_at = self._wait(candidate)
                self._fire(fire_at, final=i == len(plan) - 1)
            raise _Done(Outcome.FAILED, f"attempt budget exhausted (last reply: {self._last_reply})")
        except _Done as done:
            outcome, message = done.outcome, done.message
        except KeyboardInterrupt:
            outcome, message = Outcome.ABORTED, "interrupted by user"
        finally:
            self._critical = False
            for record in self._buffer:
                self._emit_fn(record)
            self._buffer.clear()
        result = TaskResult(
            outcome=outcome,
            message=message,
            lecture=self.lecture.to_dict() if self.lecture else None,
            attempts=self.attempts,
            submitted=self.submitted,
            target_time=(datetime.fromtimestamp(target, SHANGHAI).isoformat(timespec="milliseconds")
                         if target else None),
            clock=self.clock.to_dict(),
            timings=self.timings,
            metrics=self.metrics.summary(),
        )
        self.emit("result", **result.to_dict())
        return result

    # ---- phases -------------------------------------------------------------

    def _session(self) -> None:
        """Validate the session (also keeps the connection warm)."""
        if not self.client.session_alive():
            self._relogin("session expired")

    def _relogin(self, reason: str) -> None:
        if self._relogins >= 3:
            raise _Done(Outcome.FAILED, "session keeps expiring; giving up")
        self._relogins += 1
        self.emit("relogin", reason=reason)
        with self.metrics.timer("relogin_ms"):
            self.relogin()

    def _prepare(self) -> float:
        self.emit("phase", phase="prepare")
        self._session()
        lectures = load_lectures(self.client)
        self.lecture = lecture = find_lecture(lectures, self.opt.lecture_id)
        state = reservation_state(self.client, lecture.id)
        if state:
            raise _Done(Outcome.ALREADY_RESERVED, "lecture is already in my reservations")
        if state is None:
            self.emit("warning", message="my reservations list is longer than the pages read; "
                      "cannot rule out an existing reservation (the server will reject duplicates)")
        at = self.opt.at or lecture.reserve_start
        if at is None:
            raise ConfigError("lecture has no reservation start time; pass --at")
        now = datetime.fromtimestamp(self.wall(), SHANGHAI)
        if lecture.reserve_end and now > lecture.reserve_end:
            raise _Done(Outcome.CLOSED, "reservation window has ended")
        warm_ms = self.solver.warmup()
        self.timings["ocr_warmup_ms"] = warm_ms
        self.emit("prepared", lecture=lecture.to_dict(now), at=at.isoformat(),
                  ocr_warmup_ms=round(warm_ms, 1))
        return at.timestamp()

    def _idle_until(self, t: float) -> None:
        """Wait until wall time ``t`` with low-frequency session keep-alive."""
        while t - self.wall() > self.opt.keepalive_s:
            self.scheduler.wait_until(self.wall() + self.opt.keepalive_s, self._on_jump)
            self._session()
            self.emit("keepalive")
        if t > self.wall():
            self.scheduler.wait_until(t, self._on_jump)

    def _on_jump(self, drift_ms: float) -> None:
        self.emit("clock_jump", drift_ms=round(drift_ms, 1),
                  hint="system sleep or clock adjustment detected; re-anchored")

    def _plan(self, target: float) -> list[float]:
        """Local wall times of the opening moment to aim at (1 or 2 candidates).

        The ``Date`` header may come from a front-end proxy whose clock differs
        from the application server's (on 2026-10-21 every *.seu.edu.cn host
        reported +375 s while campus NTP was correct). ``auto`` therefore never
        bets on one clock when they disagree beyond the estimate's error: it
        probes the earlier candidate with at most ``PROBE_ATTEMPTS`` attempts
        and, on "not open", fires again at the later one with the rest of the
        task's attempt budget.
        """
        # About one probe per second; never let estimation run into the deadline.
        samples = min(8, int(target - self.wall() - 3))
        if self.opt.clock != "local" and samples >= 2:
            self._sync_clock(samples)
        est = self.clock
        plan = [target]
        usable = est.offset_ms is not None and est.error_ms is not None and est.error_ms <= 500
        if self.opt.clock != "local" and usable:
            # Latest moment the server-clock opening can be at, given the estimate's
            # error: never arrive early just because the estimate erred low.
            server = target - (est.offset_ms - est.error_ms) / 1000
            if self.opt.clock == "server":
                plan = [server]
            elif abs(est.offset_ms) > est.error_ms:
                plan = sorted((target, server))
        self.emit("plan", candidates=[datetime.fromtimestamp(c, SHANGHAI).isoformat(timespec="milliseconds")
                                      for c in plan], clock_mode=self.opt.clock)
        return plan

    def _sync_clock(self, samples: int) -> None:
        try:
            self.clock = estimate_offset(self.client.probe_date, max_samples=samples,
                                         wall=self.wall, sleep=self.sleep)
        except (NetworkError, ProtocolError) as exc:
            self.emit("warning", message=f"clock estimation failed: {exc.message}")
            return
        self.emit("clock", **self.clock.to_dict())
        if self.clock.offset_ms is not None and abs(self.clock.offset_ms) > 2000:
            self.emit("warning", message=f"server Date header differs from the local clock by "
                      f"{self.clock.offset_ms / 1000:+.1f} s; check local time sync (NTP)")

    def _rtt_s(self) -> float:
        recent = [v for name in ("mine.rtt_ms", "list.rtt_ms", "captcha.rtt_ms")
                  for v in self.metrics.series.get(name, [])[-5:]]
        return statistics.median(recent) / 1000 if recent else 0.0

    def _fire_time(self, candidate: float) -> float:
        lead = self._rtt_s() / 2 if self.opt.compensate_latency else 0.0
        return candidate + self.opt.offset_ms / 1000 - lead

    def _wait(self, candidate: float) -> float:
        self._code = None
        self._idle_until(self._fire_time(candidate) - self.opt.session_check_lead_s)
        self._session()
        fire_at = self._fire_time(candidate)
        self.emit("armed", fire_at=round(fire_at, 3), rtt_ms=round(self._rtt_s() * 1000, 1))
        self._idle_until(fire_at - max(self.opt.captcha_lead_ms / 1000, 1.0))
        if self.opt.prefetch_captcha and fire_at > self.wall():
            try:
                self._code = self._solve()
            except (CaptchaError, ProtocolError, NetworkError) as exc:
                self.emit("warning", message=f"captcha prefetch failed: {exc.message}")
        else:
            self._session()  # warm the keep-alive connection
        return fire_at

    def _solve(self) -> str:
        last: CaptchaError | None = None
        for _ in range(3):
            image = self.client.captcha_image()
            start = now_ns()
            try:
                code = self.solver.solve(image)
            except CaptchaError as exc:
                last = exc
                continue
            finally:
                self.metrics.add("ocr_ms", ms_since(start))
            return code
        raise last or CaptchaError("captcha could not be solved")

    def _verify(self) -> bool | None:
        """True: reserved. False: a complete list read lacks it. None: unknown.

        Business errors, malformed lists and incomplete pagination are never read
        as "absent", so they can never authorise a resubmit.
        """
        assert self.lecture is not None
        absent = False
        for i in range(self.opt.verify_attempts):
            if i:
                self.sleep(self.opt.verify_interval_ms / 1000)
            try:
                with self.metrics.timer("verify_ms"):
                    state = reservation_state(self.client, self.lecture.id)
            except SessionExpired:
                try:
                    self._relogin("session expired during verification")
                except Exception:
                    break
                continue
            except (NetworkError, ProtocolError):
                continue
            if state:
                return True
            absent = absent or state is False
        return False if absent else None

    def _probe_budget(self) -> int:
        return min(PROBE_ATTEMPTS, self.opt.max_attempts - 1)

    def _backoff(self, retry_after: float | None, message: str) -> None:
        """At most one bounded backoff per task, honouring Retry-After when given."""
        delay = self.opt.rate_limit_backoff_s if retry_after is None else retry_after
        if self._rate_limited or delay > MAX_BACKOFF_S:
            raise _Done(Outcome.RATE_LIMITED, message if delay <= MAX_BACKOFF_S
                        else f"{message}; server asks to wait {delay:.0f} s")
        self._rate_limited = True
        self.emit("backoff", seconds=round(delay, 3), reason=message[:80])
        self.sleep(delay)

    def _fire(self, fire_at: float, *, final: bool = True) -> None:
        """One burst of attempts.

        Returns only for a non-final candidate (not open yet, or its probe budget
        used); every other ending raises ``_Done``. The task-wide ``max_attempts``
        caps all bursts together.
        """
        assert self.lecture is not None
        wid = self.lecture.id
        limit = self.opt.max_attempts if final else self.attempts + self._probe_budget()
        limit = min(limit, self.opt.max_attempts)
        gc_enabled = gc.isenabled()
        gc.disable()
        self._critical = True
        try:
            waited = self.scheduler.wait_until(fire_at)
            fired = now_ns()
            self.timings["schedule_late_ms"] = waited.late_ms
            self.timings.pop("fire_to_first_reply_ms", None)
            self.emit("phase", phase="fire", late_ms=round(waited.late_ms, 3), final=final)
            code = self._code
            not_open_deadline = fired + int(self.opt.not_open_window_ms * 1e6)
            while self.attempts < limit:
                gap = (now_ns() - self._last_submit) / 1e6
                if self._last_submit and gap < self.opt.min_submit_interval_ms:
                    self.sleep((self.opt.min_submit_interval_ms - gap) / 1000)
                if code is None:
                    try:
                        code = self._solve()
                    except SessionExpired:
                        self._relogin("session expired before submit")  # nothing submitted yet
                        continue
                    except RateLimited as exc:
                        self._backoff(exc.retry_after, exc.message)
                        continue
                    except (CaptchaError, ProtocolError, NetworkError) as exc:
                        self.attempts += 1  # count failed captcha rounds toward the cap
                        self._last_reply = "captcha_unavailable"
                        self.emit("attempt", n=self.attempts, reply="captcha_unavailable",
                                  message=exc.message)
                        self._last_submit = now_ns()
                        continue
                if self.opt.dry_run:
                    self.timings["fire_to_ready_ms"] = ms_since(fired)
                    raise _Done(Outcome.DRY_RUN, "dry run: captcha solved, submit skipped")
                self.attempts += 1
                self._last_submit = now_ns()
                try:
                    self.submitted = True
                    payload = self.client.submit(wid, code)
                except RateLimited as exc:  # HTTP 429: rejected before processing
                    self._last_reply = "rate_limited"
                    self.emit("attempt", n=self.attempts, reply="rate_limited", message=exc.message,
                              retry_after=exc.retry_after)
                    self._backoff(exc.retry_after, exc.message)
                    continue
                except NetworkError as exc:
                    self._last_reply = "network_error"
                    self.emit("attempt", n=self.attempts, reply="network_error",
                              message=exc.message, maybe_delivered=exc.maybe_delivered)
                    if not exc.maybe_delivered:
                        continue  # never reached the server: safe to retry
                    self._after_uncertain(exc.message)
                    continue
                except (SessionExpired, ProtocolError) as exc:
                    # Redirect/HTML/empty/broken JSON: the request may have been processed.
                    # Check the list first (re-login happens inside verification); never
                    # re-login and resubmit blindly.
                    self._last_reply = "unreadable_response"
                    self.emit("attempt", n=self.attempts, reply="unreadable_response",
                              message=exc.message)
                    self._after_uncertain(exc.message)
                    continue
                finally:
                    code = None
                self.timings.setdefault("fire_to_first_reply_ms", ms_since(fired))
                reply, msg = classify_reply(payload)
                self._last_reply = reply.value
                self.emit("attempt", n=self.attempts, reply=reply.value, message=msg[:120])
                if reply in (Reply.SUCCESS, Reply.ALREADY):
                    verified = self._verify()
                    self.timings["fire_to_verified_ms"] = ms_since(fired)
                    if verified:
                        raise _Done(Outcome.CONFIRMED if reply is Reply.SUCCESS
                                    else Outcome.ALREADY_RESERVED,
                                    "reservation confirmed in my reservations list")
                    raise _Done(Outcome.UNCONFIRMED,
                                f"server replied '{msg or reply.value}' but the reservation "
                                "is not visible yet; check `seu-lecture reservations`")
                if reply in (Reply.CAPTCHA, Reply.INVALID):
                    continue
                if reply is Reply.NOT_OPEN:
                    if not final:
                        self.emit("candidate_not_open", message="earlier clock candidate not open; "
                                  "waiting for the next candidate")
                        return
                    if now_ns() < not_open_deadline:
                        continue
                    raise _Done(Outcome.NOT_OPEN, msg or "reservation not open yet")
                if reply is Reply.RATE_LIMITED:
                    self._backoff(None, msg or "rate limited")
                    continue
                if reply is Reply.FULL:
                    raise _Done(Outcome.FULL, msg or "lecture is full")
                if reply is Reply.CONFLICT:
                    raise _Done(Outcome.CONFLICT, msg or "time conflict")
                if reply is Reply.CLOSED:
                    raise _Done(Outcome.CLOSED, msg or "reservation closed")
                # Unknown reply: find out what actually happened before anything else.
                verified = self._verify()
                if verified:
                    raise _Done(Outcome.CONFIRMED, "reservation confirmed in my reservations list")
                if verified is None:
                    raise _Done(Outcome.UNCONFIRMED, f"unrecognised reply '{msg[:80]}'")
                raise _Done(Outcome.FAILED, f"unrecognised reply '{msg[:80]}'")
            if not final:
                self.emit("candidate_exhausted", message="probe budget used at the earlier clock "
                          "candidate; moving to the next candidate")
        finally:
            self._critical = False
            if gc_enabled:
                gc.enable()

    def _after_uncertain(self, message: str) -> None:
        """Submit outcome unknown: verify; resubmit only if the list proves absence."""
        verified = self._verify()
        if verified:
            raise _Done(Outcome.CONFIRMED, "reservation confirmed in my reservations list")
        if verified is None:
            raise _Done(Outcome.UNCONFIRMED,
                        f"submit outcome unknown ({message}) and the reservation list "
                        "could not be read completely; NOT resubmitting")
        if self._resubmits >= self.opt.max_resubmits:
            raise _Done(Outcome.UNCONFIRMED,
                        f"submit outcome unknown ({message}); resubmit budget used")
        self._resubmits += 1
        self.emit("resubmit", reason="verified not reserved after uncertain submit")
