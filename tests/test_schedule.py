"""Timetable: real dates/times, week rules, 调停补课, conflict checks and how they gate reservations."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import pytest
from conftest import Env
from fake_campus import KB

from seu_lecture.core.auth import CasLogin
from seu_lecture.core.errors import ScheduleBlocked, ScheduleUndetermined
from seu_lecture.core.lecture import SHANGHAI
from seu_lecture.core.reserve import Outcome, ReserveOptions
from seu_lecture.core.schedule import Timetable

MakeEnv = Callable[..., Env]
TERM = {"term_start": "2026-09-21"}  # week 5 = 2026-10-19 .. 10-25


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SHANGHAI)


def lecture(wid: str, start: str, end: str | None) -> dict[str, Any]:
    row = {"WID": wid, "JZMC": f"讲座{wid[:2]}", "JZSJ": start, "YYKSSJ": "2026-10-01 12:00:00",
           "HDZRS": "100"}
    if end:
        row["HDJSSJ"] = end
    return row


def timetable(env: Env) -> Timetable:
    client = env.client(login=True)
    return Timetable.open(client, CasLogin(client, env.credentials(), fingerprint="fp"))


# ---- dates, weeks, period times ----------------------------------------------------------------

def test_term_week_and_period_times(make_env: MakeEnv) -> None:
    tt = timetable(make_env(**TERM))
    term = tt.term()
    assert (term.code, term.start, term.weeks) == ("2026-2027-2", date(2026, 9, 21), 18)
    assert tt.week_of(date(2026, 10, 23)) == 5
    assert tt.week_days(5)[0] == date(2026, 10, 19)
    assert tt.week_of(date(2026, 9, 1)) is None    # before the term
    assert tt.week_of(date(2027, 3, 1)) is None    # after week 18
    [soil] = tt.classes_on(date(2026, 10, 19))      # Monday, periods 1-2
    assert (soil.course, soil.start, soil.end) == ("土力学", at("2026-10-19 08:00"), at("2026-10-19 09:35"))
    assert soil.to_dict()["periods"] == "1-2"


def test_even_week_only_course(make_env: MakeEnv) -> None:
    tt = timetable(make_env(**TERM))
    assert tt.classes_on(date(2026, 10, 23)) == []  # Friday of week 5 (odd)
    [math] = tt.classes_on(date(2026, 10, 30))      # Friday of week 6 (even), periods 11-13
    assert (math.start, math.end) == (at("2026-10-30 19:00"), at("2026-10-30 21:25"))


def test_moved_cancelled_and_makeup_classes(make_env: MakeEnv) -> None:
    changes = [
        # 调课: week 4 Monday 1-2 moved to week 3 Friday 3-4 (as seen on the real system)
        {"TKLXDM": "01", "KCH": "C1", "KCM": "土力学", "SKZC": "0001", "SKXQ": 1, "KSJC": 1, "JSJC": 2,
         "XSKZC": "001", "XSKXQ": 5, "XKSJC": 3, "XJSJC": 4, "XJASMC": "教三-301", "XZCMC": "3周"},
        # 停课: week 5 Monday 1-2
        {"TKLXDM": "02", "KCH": "C1", "KCM": "土力学", "SKZC": "00001", "SKXQ": 1, "KSJC": 1, "JSJC": 2},
        # 补课: week 5 Saturday 6-7
        {"TKLXDM": "03", "KCH": "C1", "KCM": "土力学", "XSKZC": "00001", "XSKXQ": 6, "XKSJC": 6, "XJSJC": 7},
    ]
    tt = timetable(make_env(course_changes=changes, **TERM))
    assert tt.classes_on(date(2026, 10, 12)) == []                     # week 4 Monday: moved away
    [moved] = tt.classes_on(date(2026, 10, 9))                        # week 3 Friday
    assert (moved.change, moved.location, moved.start) == ("调课", "教三-301", at("2026-10-09 09:50"))
    assert tt.classes_on(date(2026, 10, 19)) == []                     # week 5 Monday: cancelled
    [makeup] = tt.classes_on(date(2026, 10, 24))                      # week 5 Saturday
    assert (makeup.change, makeup.start, makeup.end) == ("补课", at("2026-10-24 14:00"), at("2026-10-24 15:35"))
    assert len(tt.classes_on(date(2026, 10, 26))) == 1                 # week 6 Monday: unchanged


# ---- conflict judgement ------------------------------------------------------------------------

def test_conflict_free_and_unknown(make_env: MakeEnv) -> None:
    tt = timetable(make_env(**TERM))
    assert tt.check(at("2026-10-23 19:00"), at("2026-10-23 21:30")).status == "free"
    clash = tt.check(at("2026-10-30 19:00"), at("2026-10-30 21:30"))
    assert clash.status == "conflict" and [c.course for c in clash.classes] == ["高等数学"]
    # touching end/start is not an overlap: class ends 09:35
    assert tt.check(at("2026-10-19 09:35"), at("2026-10-19 11:00")).status == "free"
    assert tt.check(at("2026-10-19 09:34"), at("2026-10-19 11:00")).status == "conflict"
    assert tt.check(at("2026-10-19 09:00"), None).status == "unknown"            # no end time
    outside = tt.check(at("2027-03-01 19:00"), at("2027-03-01 21:00"))
    assert outside.status == "unknown" and "教学周" in (outside.reason or "")


def test_period_counts_that_differ_from_the_official_table_are_unknown(make_env: MakeEnv) -> None:
    tt = timetable(make_env(period_split=(4, 4, 4), **TERM))
    assert tt.term().periods is None
    [soil] = tt.classes_on(date(2026, 10, 19))
    assert soil.start is None and soil.to_dict()["start"] is None
    assert tt.check(at("2026-10-19 19:00"), at("2026-10-19 21:00")).status == "unknown"
    assert tt.check(at("2026-10-20 19:00"), at("2026-10-20 21:00")).status == "free"  # no classes that day


def test_timetable_business_error_is_unknown_not_free(make_env: MakeEnv) -> None:
    env = make_env(**TERM)
    tt = timetable(env)
    tt.term()
    env.cfg.timetable_fault = "business_error"
    check = tt.check(at("2026-10-23 19:00"), at("2026-10-23 21:30"))
    assert check.status == "unknown" and "课表查询失败" in (check.reason or "")


def test_timetable_app_needs_portal_and_appshow(make_env: MakeEnv) -> None:
    env = make_env(**TERM)
    client = env.client(login=True)
    direct = CasLogin(client).enter(env.campus.base + KB + "/*default/index.do")
    assert direct.status_code == 403  # like the real app without appShow
    assert timetable(env).term().code == "2026-2027-2"


# ---- service: lecture list filtering and the pre-reservation check -----------------------------

FREE, CLASH, OPEN_END = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture
def scheduled(make_env: MakeEnv) -> Env:
    return make_env(lectures=[
        lecture(FREE, "2026-10-23 19:00:00", "2026-10-23 21:30:00"),
        lecture(CLASH, "2026-10-30 19:00:00", "2026-10-30 21:30:00"),
        lecture(OPEN_END, "2026-10-26 09:00:00", None),
    ], **TERM)


def test_lecture_list_annotates_and_filters(scheduled: Env) -> None:
    service = scheduled.service()
    service.login(scheduled.cfg.username, scheduled.cfg.password)
    statuses = {x["id"]: x["schedule"]["status"] for x in service.lectures(schedule=True)}
    assert statuses == {FREE: "free", CLASH: "conflict", OPEN_END: "unknown"}
    assert [x["id"] for x in service.lectures(free=True)] == [FREE]
    assert "schedule" not in service.lectures()[0]  # no timetable access unless asked


@pytest.mark.parametrize(("wid", "error"), [(CLASH, ScheduleBlocked), (OPEN_END, ScheduleUndetermined)])
def test_reservation_is_refused_unless_free(scheduled: Env, wid: str, error: type[Exception]) -> None:
    service = scheduled.service()
    service.login(scheduled.cfg.username, scheduled.cfg.password)
    with pytest.raises(error) as info:
        service.reserve(ReserveOptions(lecture_id=wid, clock="local"))
    assert info.value.exit_code == 10
    assert scheduled.campus.calls.get("addReservation.do", 0) == 0
    assert service.task_status()["tasks"][0]["state"] == "error"


def test_free_lecture_is_reserved_after_the_recheck(scheduled: Env) -> None:
    service = scheduled.service()
    service.login(scheduled.cfg.username, scheduled.cfg.password)
    events: list[dict[str, Any]] = []
    result = service.reserve(ReserveOptions(lecture_id=FREE, clock="local", min_submit_interval_ms=100),
                             emit=events.append)
    assert result.outcome is Outcome.CONFIRMED
    assert events[0]["event"] == "schedule" and events[0]["status"] == "free"
    assert FREE in scheduled.campus.reservations


def test_ignore_schedule_skips_the_recheck(scheduled: Env) -> None:
    service = scheduled.service()
    service.login(scheduled.cfg.username, scheduled.cfg.password)
    result = service.reserve(ReserveOptions(lecture_id=CLASH, clock="local"), check_schedule=False)
    assert result.outcome is Outcome.CONFIRMED
    assert CLASH in scheduled.campus.reservations


# ---- CLI ------------------------------------------------------------------------------------------

def test_cli_schedule_and_free_json(account: Env, invoke: Any) -> None:
    account.cfg.term_start = TERM["term_start"]
    result = invoke("schedule", "--week", "6", "--json")
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    assert data["week"] == 6 and data["term"]["period_times"] == "official"
    friday = data["days"][4]
    assert friday["date"] == "2026-10-30"
    assert friday["classes"][0]["start"] == "19:00" and friday["classes"][0]["end"] == "21:25"

    result = invoke("list", "--free", "--json")
    assert result.exit_code == 0, result.stdout
    items = json.loads(result.stdout)["data"]
    assert all(x["schedule"]["status"] == "free" for x in items)
