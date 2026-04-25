from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date
    days: int

    @property
    def report_date(self) -> str:
        return self.end.isoformat()

    @property
    def start_date(self) -> str:
        return self.start.isoformat()

    @property
    def end_date(self) -> str:
        return self.end.isoformat()


def parse_date(value: str | date | None, *, default: date | None = None) -> date:
    if value is None or value == "":
        if default is None:
            raise ValueError("date is required")
        return default
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return datetime.strptime(text, "%Y-%m-%d").date()


def parse_window(end: str | date | None = None, days: int = 1) -> DateWindow:
    end_date = parse_date(end, default=date.today())
    safe_days = int(days)
    if safe_days < 1:
        raise ValueError(f"days must be >= 1, got {days!r}")
    start_date = end_date - timedelta(days=safe_days - 1)
    return DateWindow(start=start_date, end=end_date, days=safe_days)


def parse_range(start: str | date, end: str | date) -> DateWindow:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if end_date < start_date:
        raise ValueError(f"end date {end_date.isoformat()} is before start date {start_date.isoformat()}")
    return DateWindow(start=start_date, end=end_date, days=(end_date - start_date).days + 1)
