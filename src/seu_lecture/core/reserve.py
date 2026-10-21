"""Reservation state machine: prepare -> wait -> fire -> verify -> watch.

Speed: the opening is timed on the lecture app's own clock, a confidently read
captcha is ready before it, and the critical path is a single submit request.
Persistence: if the burst ends with the lecture full (or not open yet), the task
keeps polling the seat count and fires again as soon as a seat frees up.

Safety rules enforced here (not in the UI):

* A server "success" is only reported as ``confirmed`` after the reservation
  shows up in ``queryMyActivityList``.
* After a timeout / dropped connection / unknown reply the task queries the
  reservation list first; it resubmits only if the list proves the lecture is
  not reserved, and only a bounded number of times.
* Every retry path is bounded (attempt cap per burst, minimum spacing, rate-limit
  backoff, watch deadline).
* The critical section does no disk I/O and emits no output; events raised
  there are buffered and flushed afterwards.
"""

from __future__ import annotations

import contextlib
import gc
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from seu_lecture.core.captcha import CONFIDENT, Solver
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
    offset_ms: float = 10.0             # margin after the latest possible opening moment
    clock: str = "auto"                 # auto (app clock xtdqsj) | local
    compensate_latency: bool = False    # send RTT/2 early (may arrive early: RTT includes server time)
    prefetch_captcha: bool = True
    captcha_lead_ms: float = 3000.0     # start picking a confident captcha this early
    captcha_tries: int = 6              # captchas fetched to find a confident read before the opening
    clock_sync_lead_s: float = 60.0
    session_check_lead_s: float = 20.0
    keepalive_s: float = 300.0
    max_attempts: int = 8               # submits per burst
    max_resubmits: int = 1              # after verified-absent uncertain submits
    min_submit_interval_ms: float = 100.0
    not_open_window_ms: float = 3000.0
    rate_limit_backoff_s: float = 5.0
    verify_attempts: int = 3
    verify_interval_ms: float = 500.0
    watch_minutes: float = 60.0         # keep trying this long after a full / not-open burst
    watch_interval_s: float = 2.0       # seat-count polling interval while watching
    dry_run: bool = False

    def validate(self) -> None:
        if self.clock not in ("auto", "local"):
            raise ConfigError("clock must be auto or local")
        if not 1 <= self.max_attempts <= 20:
            raise ConfigError("max_attempts must be between 1 and 20")
        if not 1 <= self.captcha_tries <= 20:
            raise ConfigError("captcha_tries must be between 1 and 20")
        if self.min_submit_interval_ms < 50:
            raise ConfigError("min_submit_interval_ms must be >= 50")
        if not 0 <= self.watch_minutes <= 24 * 60:
            raise ConfigError("watch_minutes must be between 0 and 1440")
        if self.watch_interval_s < 1:
            raise ConfigError("watch_interval_s must be >= 1")
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
    def __init__(self, outcome: Outcome, message: str, *, watchable: bool = False) -> None:
        self.outcome = outcome
        self.message = message
        self.watchable = watchable  # nothing ambiguous happened: watching may still win a seat


Emit = Callable[[dict[str, Any]], None]
T = TypeVar("T")

# Upper bound for any single backoff, including a server-requested Retry-After.
MAX_BACKOFF_S = 60.0


def patient(call: Callable[[], T], *, tries: int = 4, pause_s: float = 3.0,
            sleep: Callable[[float], None] = time.sleep) -> T:
    """Retry a read-only call through transient network trouble (the school servers
    occasionally stall for 10+ s). Only used before the opening, where time is cheap."""
    for attempt in range(tries):
        try:
            return call()
        except NetworkError as exc:
            if attempt == tries - 1:
                raise
            wait = exc.retry_after if isinstance(exc, RateLimited) and exc.retry_after else pause_s
            sleep(min(wait, MAX_BACKOFF_S))
    raise AssertionError("unreachable")


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
        self._fetched = 0              # captchas fetched by the latest _solve
        self._last_reply = "none"
        self._last_submit = 0      # perf_counter_ns of the latest submit
        self._resubmits = 0        # resubmits after verified-absent uncertain submits
        self._uncertain = False    # some submit had an unknown outcome
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
            fire_at = self._wait(self._opening(target))
            try:
                self._fire(fire_at, self.opt.max_attempts, self.opt.not_open_window_ms)
            except _Done as done:
                if not done.watchable:
                    raise
                self._watch(done)
        except _Done as done:
            outcome, message = done.outcome, done.message
        except KeyboardInterrupt:
            outcome, message = Outcome.ABORTED, "interrupted by user"
        finally:
            self._flush()
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

    def _flush(self) -> None:
        self._critical = False
        for record in self._buffer:
            self._emit_fn(record)
        self._buffer.clear()

    # ---- phases -------------------------------------------------------------

    def _session(self) -> None:
        """Validate the session (also keeps the connection warm)."""
        if not self.client.session_alive():
            self._relogin("session expired")

    def _keepalive(self) -> None:
        """Session check while waiting: a network hiccup is only a warning."""
        try:
            self._session()
        except NetworkError as exc:
            self.emit("warning", message=f"keep-alive failed: {exc.message}")

    def _relogin(self, reason: str) -> None:
        if self._relogins >= 3:
            raise _Done(Outcome.FAILED, "session keeps expiring; giving up")
        self._relogins += 1
        self.emit("relogin", reason=reason)
        with self.metrics.timer("relogin_ms"):
            self.relogin()

    def _prepare(self) -> float:
        self.emit("phase", phase="prepare")
        patient(self._session, sleep=self.sleep)
        lectures = patient(lambda: load_lectures(self.client), sleep=self.sleep)
        self.lecture = lecture = find_lecture(lectures, self.opt.lecture_id)
        state = patient(lambda: reservation_state(self.client, lecture.id), sleep=self.sleep)
        if state:
            raise _Done(Outcome.ALREADY_RESERVED, "lecture is already in my reservations")
        if state is None:
            self.emit("warning", message="my reservations list could not be read completely; "
                      "cannot rule out an existing reservation (the server rejects duplicates)")
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
            self._keepalive()
            self.emit("keepalive")
        if t > self.wall():
            self.scheduler.wait_until(t, self._on_jump)

    def _on_jump(self, drift_ms: float) -> None:
        self.emit("clock_jump", drift_ms=round(drift_ms, 1),
                  hint="system sleep or clock adjustment detected; re-anchored")

    def _opening(self, target: float) -> float:
        """Local wall time of the opening, corrected by the app clock (``xtdqsj``).

        The estimate is taken shortly before the opening and applied at the late
        edge of its error interval: arriving early costs a whole "not open" round
        trip, arriving up to 2 x error late costs only that much.
        """
        if self.opt.clock == "local":
            return target
        if target - self.wall() > self.opt.clock_sync_lead_s + 30:
            self._idle_until(target - self.opt.clock_sync_lead_s)
        samples = min(8, int(target - self.wall() - 3))  # ~1 probe/s, never past the deadline
        if samples < 2:
            return target
        try:
            self.clock = est = estimate_offset(self.client.probe_app_clock, max_samples=samples,
                                               wall=self.wall, sleep=self.sleep)
        except (NetworkError, ProtocolError) as exc:
            self.emit("warning", message=f"app clock estimation failed: {exc.message}")
            return target
        self.emit("clock", **est.to_dict())
        if est.offset_ms is None or est.error_ms is None or est.error_ms > 500:
            return target
        if abs(est.offset_ms) > 2000:
            self.emit("warning", message=f"local clock differs from the lecture app by "
                      f"{est.offset_ms / 1000:+.1f} s; check the system time (NTP)")
        return target - (est.offset_ms - est.error_ms) / 1000

    def _rtt_s(self) -> float:
        """Fastest recent round trip (it includes server time, so half of it overestimates one way)."""
        recent = [v for name in ("mine.rtt_ms", "list.rtt_ms", "captcha.rtt_ms")
                  for v in self.metrics.series.get(name, [])[-5:]]
        return min(recent) / 1000 if recent else 0.0

    def _wait(self, opening: float) -> float:
        def fire_time() -> float:
            lead = self._rtt_s() / 2 if self.opt.compensate_latency else 0.0
            return opening + self.opt.offset_ms / 1000 - lead

        self._idle_until(fire_time() - self.opt.session_check_lead_s)
        self._keepalive()
        fire_at = fire_time()
        self.emit("armed", fire_at=round(fire_at, 3), rtt_ms=round(self._rtt_s() * 1000, 1))
        self._idle_until(fire_at - max(self.opt.captcha_lead_ms / 1000, 1.0))
        if self.opt.prefetch_captcha and fire_at > self.wall():
            try:
                self._code = self._solve(self.opt.captcha_tries, until=fire_at - 0.3)
                self.emit("captcha_ready", fetched=self._fetched)
            except RateLimited as exc:
                self._backoff(exc.retry_after, f"captcha prefetch: {exc.message}")
            except (CaptchaError, ProtocolError, NetworkError) as exc:
                self.emit("warning", message=f"captcha prefetch failed: {exc.message}")
        else:
            self._keepalive()  # warm the keep-alive connection
        return fire_at

    def _solve(self, tries: int = 3, until: float | None = None) -> str:
        """Fetch captchas until one reads confidently; returns the latest read.

        Only the most recently fetched captcha is valid server-side, so an earlier,
        better read is worthless once a new image has been requested.
        """
        code: str | None = None
        self._fetched = 0
        for _ in range(tries):
            if self._fetched and until is not None and self.wall() >= until:
                break
            image = self.client.captcha_image()
            self._fetched += 1
            start = now_ns()
            try:
                code, confidence = self.solver.read(image)
            except CaptchaError:
                code, confidence = None, 0.0
            finally:
                self.metrics.add("ocr_ms", ms_since(start))
            if code and confidence >= CONFIDENT:
                return code
        if code is None:
            raise CaptchaError("captcha could not be read")
        return code

    def _verify(self) -> bool | None:
        """True: reserved. False: a complete list read lacks it. None: unknown.

        Business errors, malformed lists and incomplete pagination are never read
        as "absent", so they can never authorise a resubmit. A 429 here waits the
        requested time (bounded) instead of hammering the list.
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
            except RateLimited as exc:
                delay = self.opt.rate_limit_backoff_s if exc.retry_after is None else exc.retry_after
                if delay > MAX_BACKOFF_S:
                    break
                self.emit("backoff", seconds=round(delay, 3), reason="verification rate limited")
                self.sleep(delay)
                continue
            except (NetworkError, ProtocolError):
                continue
            if state:
                return True
            absent = absent or state is False
        return False if absent else None

    def _backoff(self, retry_after: float | None, message: str) -> None:
        """At most one bounded backoff per task, honouring Retry-After when given."""
        delay = self.opt.rate_limit_backoff_s if retry_after is None else retry_after
        if self._rate_limited or delay > MAX_BACKOFF_S:
            raise _Done(Outcome.RATE_LIMITED, message if delay <= MAX_BACKOFF_S
                        else f"{message}; server asks to wait {delay:.0f} s", watchable=True)
        self._rate_limited = True
        self.emit("backoff", seconds=round(delay, 3), reason=message[:80])
        self.sleep(delay)

    def _exhausted(self) -> None:
        """Attempt budget used: keep the meaning of the last state, never a blanket 'failed'."""
        if self._last_reply == "rate_limited":
            raise _Done(Outcome.RATE_LIMITED, "attempt budget exhausted while rate limited",
                        watchable=True)
        if self._uncertain:
            verified = self._verify()
            if verified:
                raise _Done(Outcome.CONFIRMED, "reservation confirmed in my reservations list")
            if verified is None:
                raise _Done(Outcome.UNCONFIRMED, "attempt budget exhausted after a submit with "
                            "unknown outcome; check `seu-lecture reservations`")
            raise _Done(Outcome.FAILED, "attempt budget exhausted; verified not in my reservations",
                        watchable=True)
        raise _Done(Outcome.FAILED, f"attempt budget exhausted (last reply: {self._last_reply})",
                    watchable=True)

    def _watch(self, last: _Done) -> None:
        """Keep polling the seat count after a full / not-open burst; fire when a seat frees."""
        assert self.lecture is not None
        deadline = self.wall() + self.opt.watch_minutes * 60
        for limit in (self.lecture.reserve_end, self.lecture.start):
            if limit is not None:
                deadline = min(deadline, limit.timestamp())
        if self.opt.dry_run or deadline <= self.wall():
            raise last
        self.emit("phase", phase="watch", reason=last.outcome.value,
                  until=datetime.fromtimestamp(deadline, SHANGHAI).isoformat(timespec="seconds"))
        seats: int | None = -1
        failures = 0  # consecutive bursts that failed for another reason than full / not open
        while True:
            failures = failures + 1 if last.outcome in (Outcome.FAILED, Outcome.RATE_LIMITED) else 0
            if failures > 3:
                raise last
            if last.outcome is Outcome.RATE_LIMITED:
                pause, self._rate_limited = MAX_BACKOFF_S, False
            elif last.outcome is Outcome.NOT_OPEN or seats is None:
                pause = max(10.0, self.opt.watch_interval_s)
            else:
                pause = self.opt.watch_interval_s
            if self.wall() + pause >= deadline:
                raise last
            self.sleep(pause)
            try:
                lecture = find_lecture(load_lectures(self.client), self.lecture.id)
            except SessionExpired:
                with contextlib.suppress(NetworkError):  # retried on the next round
                    self._relogin("session expired while watching")
                continue
            except RateLimited:
                last = _Done(Outcome.RATE_LIMITED, "rate limited while watching", watchable=True)
                continue
            except (NetworkError, ProtocolError):
                continue
            self._relogins = 0
            if lecture.remaining != seats:
                seats = lecture.remaining
                self.emit("watch", remaining=seats)
            opens = lecture.reserve_start.timestamp() if lecture.reserve_start else None
            if last.outcome is Outcome.FULL and seats == 0:
                continue
            if opens and opens > self.wall() + 1:  # opening was moved: wait for the new time
                if opens >= deadline:
                    raise last
                self._idle_until(opens)
            try:
                self._fire(self.wall(), min(3, self.opt.max_attempts), 0.0)
            except _Done as done:
                if not done.watchable:
                    raise
                last = done

    def _fire(self, fire_at: float, budget: int, not_open_window_ms: float) -> None:
        """One burst of up to ``budget`` submits; always ends by raising ``_Done``."""
        assert self.lecture is not None
        wid = self.lecture.id
        gc_enabled = gc.isenabled()
        gc.disable()
        self._critical = True
        try:
            waited = self.scheduler.wait_until(fire_at)
            fired = now_ns()
            self.timings.setdefault("schedule_late_ms", waited.late_ms)
            self.emit("phase", phase="fire", late_ms=round(waited.late_ms, 3))
            code, self._code = self._code, None
            not_open_deadline = fired + int(not_open_window_ms * 1e6)
            limit = self.attempts + budget
            while self.attempts < limit:
                if code is None:
                    try:
                        code = self._solve()
                    except SessionExpired:
                        self._relogin("session expired before submit")  # nothing submitted yet
                        continue
                    except RateLimited as exc:
                        self._last_reply = "rate_limited"
                        self._backoff(exc.retry_after, f"captcha: {exc.message}")
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
                # Spacing applies between submits; the next captcha is fetched inside it.
                gap = (now_ns() - self._last_submit) / 1e6
                if self._last_submit and gap < self.opt.min_submit_interval_ms:
                    self.sleep((self.opt.min_submit_interval_ms - gap) / 1000)
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
                    if exc.maybe_delivered:
                        self._after_uncertain(exc.message)
                    continue  # not delivered: safe to retry
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
                    if now_ns() < not_open_deadline:
                        continue
                    raise _Done(Outcome.NOT_OPEN, msg or "reservation not open yet", watchable=True)
                if reply is Reply.RATE_LIMITED:
                    self._backoff(None, msg or "rate limited")
                    continue
                if reply is Reply.FULL:
                    raise _Done(Outcome.FULL, msg or "lecture is full", watchable=True)
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
            self._exhausted()
        finally:
            self._flush()
            if gc_enabled:
                gc.enable()

    def _after_uncertain(self, message: str) -> None:
        """Submit outcome unknown: verify; resubmit only if the list proves absence."""
        self._uncertain = True
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
