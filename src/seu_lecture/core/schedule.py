"""Timetable reading and lecture/class conflict checks.

Every input is official:

* term, teaching week of a date, per-week courses and 调课/停课/补课 come from the
  timetable app (``jwapp/sys/wdkb``): ``dqxnxq.do``, ``dqzc.do``, ``xskcb.do``
  (``SKZC`` = week, so the server applies odd/even-week rules) and ``xsdkkc.do``;
* period clock times come from the 教务处《上课时间安排表》 (in force since the
  2024-2025 autumn term). The app itself only publishes the number of morning /
  afternoon / evening periods (``cxjcs.do``); if those counts ever differ from
  the table, times become unknown instead of being guessed.

A lecture is ``free`` only when its real start/end are known and no class of
that day overlaps them; missing data gives ``unknown``, never ``free``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from seu_lecture.core.client import CampusClient
from seu_lecture.core.errors import AuthFailed, NetworkError, ProtocolError
from seu_lecture.core.lecture import SHANGHAI, parse_time

PERIODS_SOURCE = ("https://jwc.seu.edu.cn/_upload/article/files/c3/32/c3b5a51f41ddab0e8f2e66069bf2/"
                  "0ba0d8c2-fa9e-4e0e-8b4b-333d815d15b8.pdf")
_TABLE = ("08:00-08:45", "08:50-09:35", "09:50-10:35", "10:40-11:25", "11:30-12:15",
          "14:00-14:45", "14:50-15:35", "15:50-16:35", "16:40-17:25", "17:30-18:15",
          "19:00-19:45", "19:50-20:35", "20:40-21:25")
_TABLE_SPLIT = (5, 5, 3)  # morning / afternoon / evening periods in the table
OFFICIAL_PERIODS: dict[int, tuple[time, time]] = {
    i: (time.fromisoformat(span[:5]), time.fromisoformat(span[6:])) for i, span in enumerate(_TABLE, 1)}
CHANGE_KIND = {"01": "调课", "02": "停课"}  # anything else is 补课 (official app's rule)


class ScheduleUnknown(Exception):
    """The timetable cannot tell whether a time is free."""


@dataclass(frozen=True)
class Term:
    code: str                      # XNXQDM, e.g. 2026-2027-2
    name: str
    start: date | None             # first day of week 1
    weeks: int | None              # total weeks
    periods: dict[int, tuple[time, time]] | None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name,
                "start": self.start.isoformat() if self.start else None, "weeks": self.weeks,
                "period_times": "official" if self.periods else "unknown",
                "period_source": PERIODS_SOURCE}


@dataclass(frozen=True)
class ClassSlot:
    day: date
    first: int
    last: int
    course: str
    location: str | None
    weeks: str | None
    start: datetime | None
    end: datetime | None
    change: str | None = None      # 调课 / 补课 when moved or added

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.day.isoformat(), "weekday": self.day.isoweekday(),
                "periods": f"{self.first}-{self.last}",
                "start": self.start.strftime("%H:%M") if self.start else None,
                "end": self.end.strftime("%H:%M") if self.end else None,
                "course": self.course, "location": self.location, "weeks": self.weeks,
                "change": self.change}


@dataclass
class Conflict:
    status: str                    # free | conflict | unknown
    reason: str | None = None
    classes: list[ClassSlot] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason,
                "classes": [c.to_dict() for c in self.classes]}


def _rows(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    datas = payload.get("datas")
    block = datas.get(key) if isinstance(datas, dict) else None
    rows = block.get("rows") if isinstance(block, dict) else None
    ext = block.get("extParams") if isinstance(block, dict) else None
    if not isinstance(rows, list) or (isinstance(ext, dict) and ext.get("code") not in (1, "1", None)):
        raise ProtocolError(f"timetable response for {key} is missing or failed")
    return [r for r in rows if isinstance(r, dict)]


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _in_week(bits: Any, week: int) -> bool:
    return isinstance(bits, str) and 0 < week <= len(bits) and bits[week - 1] == "1"


class Timetable:
    """Read-only timetable access with per-run caching (one request per week / date)."""

    def __init__(self, client: CampusClient) -> None:
        self.client = client
        self._term: Term | None = None
        self._weeks: dict[date, int | None] = {}
        self._week_rows: dict[int, list[dict[str, Any]]] = {}
        self._changes: list[dict[str, Any]] | None = None

    @classmethod
    def open(cls, client: CampusClient, cas: Any) -> Timetable:
        """Enter the timetable app: ehall portal login, then ``appShow`` (each a CAS service)."""
        cas.enter(client.endpoints.portal_login)
        final = cas.enter(client.endpoints.timetable_show)
        if final.status_code != 200:
            raise AuthFailed("could not open the timetable app", http_status=final.status_code)
        return cls(client)

    def term(self) -> Term:
        if self._term is None:
            current = _rows(self.client.timetable_call("/modules/jshkcb/dqxnxq.do"), "dqxnxq")
            if not current or not current[0].get("DM"):
                raise ProtocolError("timetable did not report the current term")
            code = str(current[0]["DM"])
            xn, _, xq = code.rpartition("-")
            info = _rows(self.client.timetable_call("/modules/jshkcb/cxjcs.do", {"XN": xn, "XQ": xq}), "cxjcs")
            row = info[0] if info else {}
            split = (_int(row.get("SFJJ")), _int(row.get("XFJJ")), _int(row.get("WSJJ")))
            start = parse_time(row.get("XQKSRQ"))
            self._term = Term(code=code, name=str(current[0].get("MC") or code),
                              start=start.date() if start else None, weeks=_int(row.get("ZZC")),
                              periods=OFFICIAL_PERIODS if split == _TABLE_SPLIT else None)
        return self._term

    def week_of(self, day: date) -> int | None:
        """Teaching week of ``day`` per the school (``dqzc.do``); None outside the term."""
        if day not in self._weeks:
            xn, _, xq = self.term().code.rpartition("-")
            rows = _rows(self.client.timetable_call("/modules/jshkcb/dqzc.do", {
                "XN": xn, "XQ": xq, "RQ": day.isoformat()}), "dqzc")
            week = _int(rows[0].get("ZC")) if len(rows) == 1 else None
            if week is not None and _int(rows[0].get("XQJ")) not in (None, day.isoweekday()):
                raise ProtocolError("timetable weekday disagrees with the calendar")
            weeks = self.term().weeks
            self._weeks[day] = week if week and week > 0 and (weeks is None or week <= weeks) else None
        return self._weeks[day]

    def _rows_of_week(self, week: int) -> list[dict[str, Any]]:
        if week not in self._week_rows:
            payload = self.client.timetable_call("/modules/xskcb/xskcb.do", {
                "XNXQDM": self.term().code, "*order": "+KSJC", "SKZC": str(week)})
            self._week_rows[week] = _rows(payload, "xskcb")
        return self._week_rows[week]

    def _all_changes(self) -> list[dict[str, Any]]:
        if self._changes is None:
            payload = self.client.timetable_call("/modules/xskcb/xsdkkc.do", {
                "XNXQDM": self.term().code, "*order": "-SQSJ"})
            self._changes = _rows(payload, "xsdkkc")
        return self._changes

    def _slot(self, day: date, first: int, last: int, row: dict[str, Any], *, location: Any,
              weeks: Any, change: str | None = None) -> ClassSlot:
        periods = self.term().periods
        start = end = None
        if periods and first in periods and last in periods and first <= last:
            start = datetime.combine(day, periods[first][0], SHANGHAI)
            end = datetime.combine(day, periods[last][1], SHANGHAI)
        return ClassSlot(day, first, last, str(row.get("KCM") or "(未命名课程)"),
                         location if isinstance(location, str) and location else None,
                         weeks if isinstance(weeks, str) else None, start, end, change)

    def classes_on(self, day: date) -> list[ClassSlot]:
        """Classes really held on ``day``, after 调课/停课/补课."""
        week = self.week_of(day)
        if week is None:
            raise ScheduleUnknown(f"{day.isoformat()} 不在本学期教学周内")
        weekday = day.isoweekday()
        base = [r for r in self._rows_of_week(week) if _int(r.get("SKXQ")) == weekday]
        added: list[ClassSlot] = []
        for change in self._all_changes():
            kind = CHANGE_KIND.get(str(change.get("TKLXDM")), "补课")
            if kind != "补课" and _in_week(change.get("SKZC"), week) and _int(change.get("SKXQ")) == weekday:
                base = [r for r in base if not (r.get("KCH") == change.get("KCH")
                        and _int(r.get("KSJC")) == _int(change.get("KSJC"))
                        and _int(r.get("JSJC")) == _int(change.get("JSJC")))]
            if (kind != "停课" and _in_week(change.get("XSKZC"), week)
                    and _int(change.get("XSKXQ")) == weekday):
                first, last = _int(change.get("XKSJC")), _int(change.get("XJSJC"))
                if first is None or last is None:
                    raise ScheduleUnknown("调补课记录缺少节次")
                added.append(self._slot(day, first, last, change, location=change.get("XJASMC"),
                                        weeks=change.get("XZCMC"), change=kind))
        slots = []
        for row in base:
            first, last = _int(row.get("KSJC")), _int(row.get("JSJC"))
            if first is None or last is None:
                raise ScheduleUnknown("课表记录缺少节次")
            slots.append(self._slot(day, first, last, row, location=row.get("JASMC"), weeks=row.get("ZCMC")))
        return sorted(slots + added, key=lambda s: (s.first, s.last))

    def week_days(self, week: int) -> list[date]:
        start = self.term().start
        if start is None:
            raise ProtocolError("timetable did not report the term start date")
        return [start + timedelta(days=7 * (week - 1) + i) for i in range(7)]

    def check(self, start: datetime | None, end: datetime | None) -> Conflict:
        """Conflict between a lecture's real time span and the classes of those days."""
        if start is None or end is None or end <= start:
            return Conflict("unknown", "讲座缺少有效的举办起止时间")
        clashes: list[ClassSlot] = []
        day = start.astimezone(SHANGHAI).date()
        try:
            while day <= end.astimezone(SHANGHAI).date():
                for slot in self.classes_on(day):
                    if slot.start is None or slot.end is None:
                        return Conflict("unknown", "节次作息时间与官方表不一致，无法换算")
                    if slot.start < end and start < slot.end:
                        clashes.append(slot)
                day += timedelta(days=1)
        except ScheduleUnknown as exc:
            return Conflict("unknown", str(exc))
        except (ProtocolError, NetworkError) as exc:
            return Conflict("unknown", f"课表查询失败：{exc.message}")
        return Conflict("conflict", None, clashes) if clashes else Conflict("free")
