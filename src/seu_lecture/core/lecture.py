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
    other_id: str | None = None  # WID when HD_WID is also present

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


def _rows(payload: dict[str, Any], key: str, *, strict: bool = False,
          ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rows under ``datas[key]`` (or a bare ``datas`` list).

    A business error (``success: false`` or a non-zero ``code``) is never read as an
    empty list. ``strict`` also rejects a missing / null list, which matters for
    "my reservations": an empty answer there would otherwise mean "not reserved".
    """
    code = payload.get("code")
    if payload.get("success") is False or (code is not None and str(code).strip() != "0"):
        msg = str(payload.get("msg") or payload.get("message") or "")[:80]
        raise ProtocolError("school system returned a business error", code=code, msg=msg)
    datas = payload.get("datas")
    if isinstance(datas, dict):
        rows = datas.get(key)
        if rows is None and not strict:
            rows = []
        extra = datas
    elif isinstance(datas, list):  # older / alternative shape
        rows, extra = datas, {}
    else:
        raise ProtocolError("response has no 'datas'", keys=sorted(payload)[:10])
    if not isinstance(rows, list):
        raise ProtocolError(f"'{key}' is missing or not a list")
    return [r for r in rows if isinstance(r, dict)], extra


def _flag(value: Any) -> bool | None:
    text = str(value).strip() if value is not None else ""
    return {"1": True, "0": False}.get(text)


def parse_lectures(payload: dict[str, Any]) -> list[Lecture]:
    """``queryActivityList.do`` -> lectures. Seat counts come from ``hdrsList``."""
    rows, extra = _rows(payload, "hdlbList")
    counts = extra.get("hdrsList")
    booked: dict[str, int | None] = {}
    for item in counts if isinstance(counts, list) else []:
        if isinstance(item, dict) and isinstance(item.get("HD_WID"), str):
            booked[item["HD_WID"]] = _int(item.get("YYRS"))
    # hdrsList only lists lectures someone has booked: absent from a present list = 0.
    default_booked = 0 if isinstance(counts, list) else None
    lectures = []
    for row in rows:
        wid = _str(row.get("WID")) or _str(row.get("HD_WID"))
        if not wid:
            continue  # an id-less row cannot be reserved safely
        reserved = booked.get(wid, _int(row.get("YYRS")) if "YYRS" in row else default_booked)
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

    Public clients disagree on the lecture id field: ``HD_WID`` (userscript) or ``WID``
    ("current format" per fetch_lecture, ``HD_WID`` being legacy). Both are kept and
    either one matching counts.
    """
    rows, _ = _rows(payload, "wdhdList", strict=True)
    result = []
    for row in rows:
        hd_wid, row_wid = _str(row.get("HD_WID")), _str(row.get("WID"))
        wid = hd_wid or row_wid
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
            other_id=row_wid if hd_wid and row_wid != hd_wid else None,
        ))
    return result


def page_total(payload: dict[str, Any]) -> int | None:
    datas = payload.get("datas")
    for source in (payload, datas if isinstance(datas, dict) else {}):
        for key in ("totalSize", "total", "totalCount"):
            if isinstance(source.get(key), int):
                return source[key]
    return None


def _load(fetch: Any, parse: Any, key: Any, page_size: int,
          max_pages: int) -> tuple[list[Any], bool]:
    """Read pages until the data says it is complete. Returns ``(items, complete)``.

    Without a total field a full page means "there may be more"; a short or empty
    page ends the list. A page repeating already-seen ids means the server
    ignores ``pageIndex``: the rest is unreachable, so the list is incomplete.
    Hitting ``max_pages`` first is incomplete too.
    """
    items: list[Any] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        payload = fetch(page, page_size)
        batch = parse(payload)
        ids = {key(x) for x in batch}
        if page > 1 and batch and ids <= seen:
            return items, False
        seen |= ids
        items.extend(batch)
        total = page_total(payload)
        if total is not None:
            if len(items) >= total:
                return items, True
            if not batch:
                return items, False  # total promises more rows than the pages deliver
        elif len(batch) < page_size:
            return items, True
    return items, False


def load_lectures(client: Any, max_pages: int = 5) -> list[Lecture]:
    return _load(client.lectures_payload, parse_lectures, lambda x: x.id, 100, max_pages)[0]


def load_reservations_checked(client: Any, max_pages: int = 5) -> tuple[list[Reservation], bool]:
    return _load(client.reservations_payload, parse_reservations, lambda r: r.lecture_id, 50,
                 max_pages)


def load_reservations(client: Any, max_pages: int = 5) -> list[Reservation]:
    return load_reservations_checked(client, max_pages)[0]


def reservation_state(client: Any, lecture_id: str, max_pages: int = 5) -> bool | None:
    """True: reserved. False: the complete list lacks it. None: list incomplete, can't tell."""
    reservations, complete = load_reservations_checked(client, max_pages)
    if is_reserved(reservations, lecture_id):
        return True
    return False if complete else None


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
    return any(lecture_id in (r.lecture_id, r.other_id) and not r.cancelled for r in reservations)
