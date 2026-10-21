"""Lecture / reservation parsing, lookup and state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from conftest import Env
from fake_campus import LECTURE_ID, default_lectures

from seu_lecture.core.errors import NotFound, ProtocolError
from seu_lecture.core.lecture import (
    SHANGHAI,
    Lecture,
    filter_lectures,
    find_lecture,
    is_reserved,
    load_lectures,
    load_reservations,
    load_reservations_checked,
    parse_lectures,
    parse_reservations,
    parse_time,
    reservation_state,
)

WID = "a" * 32
ROW = {
    "WID": WID, "JZMC": " 讲座A ", "JZSJ": "2030-11-01 19:00:00", "HDJSSJ": "2030-11-01 21:00:00",
    "YYKSSJ": "2030-10-30 12:00:00", "YYJSSJ": "2030-11-01 18:00", "JZDD": "九龙湖",
    "JZXL_DISPLAY": "艺术类", "HDZRS": "50",
}
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=SHANGHAI)


def lecture(lecture_id: str = WID, **fields: Any) -> Lecture:
    base: dict[str, Any] = {
        "title": "t", "lecture_time": None, "start": None, "end": None, "reserve_start": None,
        "reserve_end": None, "location": None, "category": None, "capacity": None,
        "reserved": None,
    }
    return Lecture(id=lecture_id, **{**base, **fields})


# ---- parse_lectures ----------------------------------------------------------------

def test_dict_shape_maps_seat_counts_by_hd_wid() -> None:
    other = "b" * 32
    payload = {"datas": {"hdlbList": [ROW, {**ROW, "WID": other}],
                         "hdrsList": [{"HD_WID": WID, "YYRS": "7"}]}}
    first, second = parse_lectures(payload)
    assert (first.id, first.reserved, first.capacity, first.remaining) == (WID, 7, 50, 43)
    assert second.reserved == 0  # hdrsList only lists lectures someone booked
    assert second.remaining == 50
    assert first.title == "讲座A"  # stripped
    assert first.start == datetime(2030, 11, 1, 19, 0, tzinfo=SHANGHAI)
    assert first.reserve_end == datetime(2030, 11, 1, 18, 0, tzinfo=SHANGHAI)
    assert first.to_dict(NOW)["reserve_start"] == "2030-10-30T12:00:00+08:00"
    assert first.to_dict()["remaining"] == 43
    assert "state" not in first.to_dict()
    assert "raw" not in first.to_dict()
    assert first.to_dict(raw=True)["raw"]["WID"] == WID


def test_list_shape_uses_row_level_reserved_count() -> None:
    [parsed] = parse_lectures({"datas": [{**ROW, "YYRS": 12}]})
    assert (parsed.reserved, parsed.remaining) == (12, 38)


def test_remaining_never_negative() -> None:
    [parsed] = parse_lectures({"datas": [{**ROW, "YYRS": 99}]})
    assert parsed.remaining == 0


def test_alternative_field_names_are_accepted() -> None:
    row = {"HD_WID": WID, "HDMC": "活动", "HDKSSJ": "2030/11/01 19:00"}
    [parsed] = parse_lectures({"datas": {"hdlbList": [row]}})
    assert (parsed.id, parsed.title) == (WID, "活动")
    assert parsed.start == datetime(2030, 11, 1, 19, 0, tzinfo=SHANGHAI)


def test_empty_dict_shape_is_an_empty_list() -> None:
    assert parse_lectures({"datas": {}}) == []
    assert parse_lectures({"datas": {"hdlbList": None}}) == []


def test_both_mock_shapes_load_through_the_client(make_env: Any) -> None:
    for shape in ("dict", "list"):
        env: Env = make_env(datas_shape=shape)
        lectures = load_lectures(env.client(login=True))
        assert lectures[0].id == LECTURE_ID
        assert len(lectures) == 2
        assert load_reservations(env.client(login=True)) == []


def test_dict_shape_carries_reserved_count_from_mock(make_env: Any) -> None:
    env: Env = make_env(reserved=12, capacity=50)
    first = load_lectures(env.client(login=True))[0]
    assert (first.reserved, first.capacity, first.remaining) == (12, 50, 38)


def test_broken_fields_skip_rows_without_id_and_null_bad_values(make_env: Any) -> None:
    env: Env = make_env(lecture_fields_broken=True)
    [parsed] = load_lectures(env.client(login=True))  # the id-less row is dropped
    assert parsed.id == LECTURE_ID
    assert parsed.title == "(untitled)"
    assert parsed.reserve_start is None
    assert parsed.capacity is None
    assert parsed.remaining is None
    assert parsed.state(NOW) == "unknown"


def test_non_dict_rows_are_ignored() -> None:
    assert [x.id for x in parse_lectures({"datas": ["junk", 3, None, ROW]})] == [WID]


@pytest.mark.parametrize("payload", [
    {"code": "0"},
    {"datas": "oops"},
    {"datas": None},
    {"datas": {"hdlbList": "not-a-list"}},
])
def test_missing_or_malformed_datas_is_protocol_error(payload: dict[str, Any]) -> None:
    with pytest.raises(ProtocolError) as info:
        parse_lectures(payload)
    assert info.value.exit_code == 5


def test_protocol_error_reports_payload_keys_not_content() -> None:
    with pytest.raises(ProtocolError) as info:
        parse_lectures({"code": "0", "secret": "hunter2"})
    assert info.value.detail == {"keys": ["code", "secret"]}


@pytest.mark.parametrize(("text", "expected"), [
    ("2030-11-01 19:00:00", datetime(2030, 11, 1, 19, 0, tzinfo=SHANGHAI)),
    ("2030-11-01 19:00", datetime(2030, 11, 1, 19, 0, tzinfo=SHANGHAI)),
    ("2030/11/01 19:00:30", datetime(2030, 11, 1, 19, 0, 30, tzinfo=SHANGHAI)),
    ("  2030/11/01 19:00 ", datetime(2030, 11, 1, 19, 0, tzinfo=SHANGHAI)),
    ("bad-time", None),
    ("2030-13-45 00:00:00", None),
    ("", None),
    ("   ", None),
    (None, None),
    (20301101, None),
])
def test_parse_time(text: Any, expected: datetime | None) -> None:
    assert parse_time(text) == expected


# ---- reservations ------------------------------------------------------------------

def test_parse_reservations_prefers_hd_wid_and_reads_flags() -> None:
    payload = {"datas": {"wdhdList": [
        {"WID": "record-1", "HD_WID": "lec-1", "JZMC": "A", "FBZT": "1", "SFDK": "1", "SFWY": "0"},
        {"WID": "lec-2", "HDMC": "B", "FBZT": "-1"},
        {"JZMC": "no id"},
    ]}}
    first, second = parse_reservations(payload)
    assert (first.lecture_id, first.checked_in, first.breached, first.cancelled) == (
        "lec-1", True, False, False)
    assert (second.lecture_id, second.title, second.cancelled) == ("lec-2", "B", True)
    assert second.checked_in is None
    assert first.to_dict()["lecture_id"] == "lec-1"


def test_is_reserved_ignores_cancelled_activities() -> None:
    payload = {"datas": [{"HD_WID": "x", "FBZT": "-1"}, {"HD_WID": "y", "FBZT": "1"}]}
    reservations = parse_reservations(payload)
    assert not is_reserved(reservations, "x")
    assert is_reserved(reservations, "y")
    assert not is_reserved(reservations, "z")


# ---- pagination --------------------------------------------------------------------

class PagedClient:
    def __init__(self, rows: int, *, with_total: bool = True) -> None:
        self.rows, self.with_total, self.calls = rows, with_total, 0

    def lectures_payload(self, page: int, size: int) -> dict[str, Any]:
        self.calls += 1
        batch = [{"WID": f"id-{i:04d}"} for i in range(self.rows)][(page - 1) * size:page * size]
        datas: dict[str, Any] = {"hdlbList": batch}
        if self.with_total:
            datas["totalSize"] = self.rows
        return {"datas": datas}


def test_load_lectures_follows_pages_until_total() -> None:
    client = PagedClient(120)
    assert len(load_lectures(client)) == 120
    assert client.calls == 2


def test_load_lectures_respects_max_pages() -> None:
    assert len(load_lectures(PagedClient(500), max_pages=2)) == 200


def test_load_lectures_without_total_follows_full_pages() -> None:
    # Regression: without a total field only page 1 used to be read.
    client = PagedClient(150, with_total=False)
    assert len(load_lectures(client)) == 150
    assert client.calls == 2  # page 2 is short, so the list is complete


def test_load_without_total_exact_multiple_reads_empty_page() -> None:
    client = PagedClient(200, with_total=False)
    assert len(load_lectures(client)) == 200
    assert client.calls == 3


class PagedReservations:
    def __init__(self, ids: list[str], *, with_total: bool = False, ignore_index: bool = False):
        self.ids, self.with_total, self.ignore_index, self.calls = ids, with_total, ignore_index, 0

    def reservations_payload(self, page: int, size: int) -> dict[str, Any]:
        self.calls += 1
        start = 0 if self.ignore_index else (page - 1) * size
        rows = [{"HD_WID": wid, "FBZT": "1"} for wid in self.ids[start:start + size]]
        datas: dict[str, Any] = {"wdhdList": rows}
        if self.with_total:
            datas["totalSize"] = len(self.ids)
        return {"code": "0", "datas": datas}


def test_reservation_on_a_later_page_without_total_is_found() -> None:
    ids = [f"other-{i:03d}" for i in range(60)] + ["target"]
    assert reservation_state(PagedReservations(ids), "target") is True


def test_reservation_list_cut_off_by_max_pages_is_unknown_not_absent() -> None:
    # Regression: hitting the page limit used to be read as "not reserved".
    ids = [f"other-{i:03d}" for i in range(300)] + ["target"]
    client = PagedReservations(ids)
    assert load_reservations_checked(client, max_pages=5)[1] is False
    assert reservation_state(client, "target", max_pages=5) is None
    assert reservation_state(PagedReservations(ids[:40]), "target") is False


def test_total_larger_than_delivered_rows_is_incomplete() -> None:
    class Short(PagedReservations):
        def reservations_payload(self, page: int, size: int) -> dict[str, Any]:
            payload = super().reservations_payload(page, size)
            payload["datas"]["totalSize"] = 999
            return payload

    assert reservation_state(Short(["a", "b"]), "target") is None


def test_server_ignoring_page_index_is_incomplete_not_complete() -> None:
    # Regression: a repeated page used to count as "list complete", so a lecture
    # beyond page 1 could be judged absent.
    client = PagedReservations([f"x{i}" for i in range(50)] + ["target"], ignore_index=True)
    reservations, complete = load_reservations_checked(client)
    assert len(reservations) == 50 and not complete and client.calls == 2
    assert reservation_state(client, "target") is None


# ---- find_lecture ------------------------------------------------------------------

@pytest.fixture
def catalog() -> list[Lecture]:
    return [lecture("abcdef0001"), lecture("abcdef0002"), lecture("zzzzzz9999"), lecture("ab")]


def test_find_lecture_exact_id(catalog: list[Lecture]) -> None:
    assert find_lecture(catalog, "abcdef0002").id == "abcdef0002"
    assert find_lecture(catalog, "ab").id == "ab"  # exact match works below 6 chars


def test_find_lecture_unique_prefix(catalog: list[Lecture]) -> None:
    assert find_lecture(catalog, "zzzzzz").id == "zzzzzz9999"
    assert find_lecture(catalog, "zzzzzz99").id == "zzzzzz9999"


def test_find_lecture_ambiguous_prefix(catalog: list[Lecture]) -> None:
    with pytest.raises(NotFound, match="ambiguous") as info:
        find_lecture(catalog, "abcdef")
    assert info.value.detail == {"matches": 2}
    assert info.value.exit_code == 6


@pytest.mark.parametrize("query", ["zzzzz", "unknown-id", "", "abcdef9"])
def test_find_lecture_not_found(catalog: list[Lecture], query: str) -> None:
    with pytest.raises(NotFound, match="not found"):
        find_lecture(catalog, query)


# ---- state / filtering ---------------------------------------------------------------

@pytest.mark.parametrize(("fields", "state"), [
    ({"reserve_start": datetime(2030, 6, 1, tzinfo=SHANGHAI)}, "not_open"),
    ({"reserve_end": datetime(2029, 6, 1, tzinfo=SHANGHAI)}, "closed"),
    ({"reserve_start": datetime(2029, 6, 1, tzinfo=SHANGHAI), "capacity": 5, "reserved": 5}, "full"),
    ({"reserve_start": datetime(2029, 6, 1, tzinfo=SHANGHAI), "capacity": 5, "reserved": 1}, "open"),
    ({"reserve_end": datetime(2031, 6, 1, tzinfo=SHANGHAI)}, "open"),
    ({}, "unknown"),
    ({"capacity": 5, "reserved": 5}, "full"),
    # closed wins over full, not_open wins over everything
    ({"reserve_end": datetime(2029, 6, 1, tzinfo=SHANGHAI), "capacity": 5, "reserved": 5}, "closed"),
    ({"reserve_start": datetime(2030, 6, 1, tzinfo=SHANGHAI), "capacity": 5, "reserved": 5},
     "not_open"),
])
def test_lecture_state(fields: dict[str, Any], state: str) -> None:
    assert lecture(**fields).state(NOW) == state


def test_filter_by_keyword_and_availability() -> None:
    items = [
        lecture("1", title="Poetry night", category="Arts"),
        lecture("2", title="Physics", category="Science", capacity=1, reserved=1,
                reserve_start=datetime(2029, 1, 1, tzinfo=SHANGHAI)),
        lecture("3", title="Old talk", reserve_end=datetime(2029, 1, 1, tzinfo=SHANGHAI)),
    ]
    assert [x.id for x in filter_lectures(items, keyword="poet", now=NOW)] == ["1"]
    assert [x.id for x in filter_lectures(items, keyword="SCIENCE", now=NOW)] == ["2"]
    assert [x.id for x in filter_lectures(items, available=True, now=NOW)] == ["1"]


def test_mock_default_lectures_are_parseable() -> None:
    lectures = parse_lectures({"datas": {"hdlbList": default_lectures(None)}})
    assert lectures[0].id == LECTURE_ID
    assert all(x.reserve_start is not None for x in lectures)
