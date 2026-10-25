"""Zaman: veritabanında UTC saklanır; lisans bitiş günü gibi takvim hesapları Europe/Istanbul'a göredir."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

TR = ZoneInfo("Europe/Istanbul")


class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_tr(self) -> datetime:
        return self.now().astimezone(TR)

    def today_tr(self) -> date:
        return self.now_tr().date()


class FixedClock(Clock):
    """Testler için: `clock.at = datetime(...)` ile ayarlanır, `clock.advance(seconds=...)` ile ilerler."""

    def __init__(self, at: datetime):
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


def end_of_day_tr(day: date) -> datetime:
    """Bitiş günü dahil: o günün son anı (TR), UTC olarak."""
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), TR).astimezone(UTC) - timedelta(seconds=1)
