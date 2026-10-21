"""Lecture / reservation models and tolerant parsing of school responses.

Only fields with public evidence are mapped. Unknown or missing fields become
``None`` instead of being guessed; ``raw`` keeps the original row for ``show --raw``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from seu_lecture.core.errors import NotFound, ProtocolError

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(frozen=True)
class Lecture:
    id: str
    title: str
    lecture_time: str | None          # JZSJ, kept verbatim (format varies)
    start: datetime | None
    end: datetime | None
    reserve_start: datetime | None    # YYKSSJ
    reserve_end: datetime | None      # YYJSSJ
    location: str | None
    category: str | None
    capacity: int | None              # HDZRS
    reserved: int | None              # YYRS (hdrsList)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def remaining(self) -> int | None:
        if self.capacity is None or self.reserved is None:
            return None
        return max(self.capacity - self.reserved, 0)

    def state(self, now: datetime) -> str:
        if self.reserve_start and now < self.reserve_start:
            return "not_open"
        if self.reserve_end and now > self.reserve_end:
            return "closed"
        if self.remaining == 0:
            return "full"
        if self.reserve_start or self.reserve_end:
            return "open"
        return "unknown"

    def to_dict(self, now: datetime | None = None, *, raw: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "lecture_time": self.lecture_time,
            "start": _iso(self.start),
            "end": _iso(self.end),
            "reserve_start": _iso(self.reserve_start),
            "reserve_end": _iso(self.reserve_end),
            "location": self.location,
            "category": self.category,
            "capacity": self.capacity,
            "reserved": self.reserved,
            "remaining": self.remaining,
        }
        if now is not None:
            data["state"] = self.state(now)
        if raw:
            data["raw"] = self.raw
        return data


@dataclass(frozen=True)
class Reservation:
    lecture_id: str
    title: str | None
    lecture_time: str | None
    location: str | None
    checked_in: bool | None    # SFDK
    breached: bool | None      # SFWY
    cancelled: bool | None     # FBZT == -1 (activity cancelled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lecture_id": self.lecture_id,
            "title": self.title,
            "lecture_time": self.lecture_time,
            "location": self.location,
            "checked_in": self.checked_in,
            "breached": self.breached,
            "cancelled": self.cancelled,
        }


def _rows(payload: dict[str, Any], key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    datas = payload.get("datas")
    if isinstance(datas, dict):
        rows = datas.get(key)
        if rows is None:
            rows = []
        extra = datas
    elif isinstance(datas, list):  # older / alternative shape
        rows, extra = datas, {}
    else:
        raise ProtocolError("response has no 'datas'", keys=sorted(payload)[:10])
    if not isinstance(rows, list):
        raise ProtocolError(f"'{key}' is not a list")
    return [r for r in rows if isinstance(r, dict)], extra


def _flag(value: Any) -> bool | None:
    text = str(value).strip() if value is not None else ""
    return {"1": True, "0": False}.get(text)


def parse_lectures(payload: dict[str, Any]) -> list[Lecture]:
    """``queryActivityList.do`` -> lectures. Seat counts come from ``hdrsList``."""
    rows, extra = _rows(payload, "hdlbList")
    booked: dict[str, int | None] = {}
    for item in extra.get("hdrsList") or []:
        if isinstance(item, dict) and isinstance(item.get("HD_WID"), str):
            booked[item["HD_WID"]] = _int(item.get("YYRS"))
    lectures = []
    for row in rows:
        wid = _str(row.get("WID")) or _str(row.get("HD_WID"))
        if not wid:
            continue  # an id-less row cannot be reserved safely
        reserved = booked.get(wid, _int(row.get("YYRS")))
        lectures.append(Lecture(
            id=wid,
            title=_str(row.get("JZMC")) or _str(row.get("HDMC")) or "(untitled)",
            lecture_time=_str(row.get("JZSJ")),
            start=parse_time(row.get("JZSJ")) or parse_time(row.get("HDKSSJ")),
            end=parse_time(row.get("HDJSSJ")),
            reserve_start=parse_time(row.get("YYKSSJ")),
            reserve_end=parse_time(row.get("YYJSSJ")),
            location=_str(row.get("JZDD")),
            category=_str(row.get("JZXL_DISPLAY")),
            capacity=_int(row.get("HDZRS")),
            reserved=reserved,
            raw=row,
        ))
    return lectures


def parse_reservations(payload: dict[str, Any]) -> list[Reservation]:
    """``queryMyActivityList.do`` -> my reservations.

    ``HD_WID`` is the lecture id; ``WID`` may be the reservation record id, so it is
    only used when ``HD_WID`` is absent (matching the upstream GUI behaviour).
    """
    rows, _ = _rows(payload, "wdhdList")
    result = []
    for row in rows:
        wid = _str(row.get("HD_WID")) or _str(row.get("WID"))
        if not wid:
            continue
        fbzt = str(row.get("FBZT", "")).strip()
        result.append(Reservation(
            lecture_id=wid,
            title=_str(row.get("JZMC")) or _str(row.get("HDMC")),
            lecture_time=_str(row.get("JZSJ")),
            location=_str(row.get("JZDD")),
            checked_in=_flag(row.get("SFDK")),
            breached=_flag(row.get("SFWY")),
            cancelled=True if fbzt == "-1" else (False if fbzt else None),
        ))
    return result


def page_total(payload: dict[str, Any]) -> int | None:
    datas = payload.get("datas")
    for source in (payload, datas if isinstance(datas, dict) else {}):
        for key in ("totalSize", "total", "totalCount"):
            if isinstance(source.get(key), int):
                return source[key]
    return None


def _load(fetch: Any, parse: Any, page_size: int, max_pages: int) -> list[Any]:
    items: list[Any] = []
    for page in range(1, max_pages + 1):
        payload = fetch(page, page_size)
        batch = parse(payload)
        items.extend(batch)
        total = page_total(payload)
        if total is None or len(items) >= total or not batch:
            break
    return items


def load_lectures(client: Any, max_pages: int = 5) -> list[Lecture]:
    return _load(client.lectures_payload, parse_lectures, 100, max_pages)


def load_reservations(client: Any, max_pages: int = 5) -> list[Reservation]:
    return _load(client.reservations_payload, parse_reservations, 50, max_pages)


def find_lecture(lectures: list[Lecture], query: str) -> Lecture:
    """Exact id, or a unique id prefix of at least 6 characters."""
    for lecture in lectures:
        if lecture.id == query:
            return lecture
    if len(query) >= 6:
        matches = [x for x in lectures if x.id.startswith(query)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise NotFound("lecture id prefix is ambiguous", matches=len(matches))
    raise NotFound("lecture not found in the current list", id=query)


def filter_lectures(lectures: list[Lecture], *, keyword: str | None = None,
                    available: bool = False, now: datetime | None = None) -> list[Lecture]:
    now = now or datetime.now(SHANGHAI)
    result = lectures
    if keyword:
        k = keyword.lower()
        result = [x for x in result if k in x.title.lower() or k in (x.category or "").lower()]
    if available:
        result = [x for x in result if x.state(now) in ("open", "not_open", "unknown")]
    return result


def is_reserved(reservations: list[Reservation], lecture_id: str) -> bool:
    return any(r.lecture_id == lecture_id and not r.cancelled for r in reservations)
