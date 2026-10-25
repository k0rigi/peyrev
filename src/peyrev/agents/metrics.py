"""İnsan görevlerinin performansı: tek sorguyla okunan görev satırları ve durum geçişi olaylarından hesap.

Ayrı sayaç tutulmaz. Dönem görevin oluşturma zamanına göredir (başlangıç dahil, bitiş hariç); bekleme süresi yalnız
kayıtlı geçişlerden ölçülebiliyorsa yazılır, uydurulmaz.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from itertools import groupby

from ..common import ApiError

UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$")
TERMINAL = ("succeeded", "failed", "cancelled")


def period(start, end) -> tuple[datetime, datetime]:
    values = []
    for value in (start, end):
        if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
            raise ApiError("Başlangıç ve bitiş ISO UTC tarih/saat biçiminde olmalı.", 400, "TARIH_ARALIGI")
        raw = value[:-1] if value.endswith("Z") else value[:-6]
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f" if "." in raw else "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            raise ApiError("Başlangıç veya bitiş tarihi geçersiz.", 400, "TARIH_ARALIGI") from None
        values.append(parsed.replace(tzinfo=UTC))
    if not timedelta(0) < values[1] - values[0] <= timedelta(days=366):
        raise ApiError("Tarih aralığı sıfırdan büyük ve en fazla 366 gün olmalı.", 400, "TARIH_ARALIGI")
    return values[0], values[1]


def _empty() -> dict:
    return {"total": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "terminal": 0,
            "total_seconds": 0.0, "duration_missing_count": 0}


def _waiting_seconds(task: dict, events: list[dict], now: datetime) -> float | None:
    changes = []
    for event in events:
        detail = event["olay_ayrinti"]
        if detail is None:
            continue
        if (not isinstance(detail, dict) or type(detail.get("version")) is not int
                or not isinstance(detail.get("before"), str) or not isinstance(detail.get("after"), str)
                or not isinstance(event["olay_zamani"], datetime)):
            return None
        changes.append((detail["version"], event["olay_zamani"], detail["before"], detail["after"]))
    changes.sort(key=lambda change: change[0])
    started, seconds, observed, previous_version = None, 0.0, False, None
    for version, timestamp, before, after in changes:
        if previous_version is not None and version <= previous_version:
            return None
        previous_version = version
        if timestamp > now:
            continue
        if before == "waiting_user":
            if started is None or timestamp < started:
                return None
            seconds += (timestamp - started).total_seconds()
            started = None
        if after == "waiting_user":
            if started is not None:
                return None
            started, observed = timestamp, True
    if started is not None:
        if task["durum"] != "waiting_user":
            return None
        seconds += max(0.0, (now - started).total_seconds())
    return seconds if observed else None


def _finish(value: dict) -> dict:
    result = dict(value)
    terminal = result["terminal"]
    result["success_rate"] = result["succeeded"] / terminal if terminal else 0.0
    result["average_seconds"] = result["total_seconds"] / terminal if terminal else 0.0
    return result


def calculate(rows, start: datetime, end: datetime, now: datetime) -> dict:
    """Rows are tasks ordered by id, each joined with its status_changed events (or one row without an event)."""
    users: dict[int, dict] = {}
    totals = _empty()
    for _task_id, joined in groupby(rows, key=lambda row: row["id"]):
        events = list(joined)
        task = events[0]
        user = users.setdefault(task["atanan_id"], _empty())
        counts = _empty()
        counts["total"] = 1
        if task["durum"] in TERMINAL:
            counts[task["durum"]] = counts["terminal"] = 1
            begun, finished = task["baslama"], task["bitis"]
            if not isinstance(begun, datetime) or not isinstance(finished, datetime) or finished < begun:
                counts["duration_missing_count"] = 1
            else:
                counts["total_seconds"] = (finished - begun).total_seconds()
        waiting = _waiting_seconds(task, events, now)
        if waiting is not None:
            counts["waiting_user_seconds"] = waiting
        for key, value in counts.items():
            user[key] = user.get(key, 0) + value
            totals[key] = totals.get(key, 0) + value
    return {"start": start.isoformat(), "end": end.isoformat(), "as_of": now.astimezone(UTC).isoformat(),
            "range_basis": "created_at", "totals": _finish(totals),
            "users": [dict(_finish(users[user_id]), user_id=user_id) for user_id in sorted(users)]}
