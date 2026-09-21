"""Which dates to fetch, and why.

Two facts drive all of it:

  1. Search Console data lags 2-3 days. Asking for today returns nothing and
     asking for yesterday returns a number that will change.

  2. Google RESTATES recent days. A day fetched once is not final, so the
     nightly sync re-fetches a trailing window and upserts rather than
     appending only yesterday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

#: Google's own lag. Nothing inside this many days of today is worth asking for.
REPORTING_LAG_DAYS = 3

#: How far back the nightly sync re-fetches. Covers restatement without
#: re-reading sixteen months every night.
RESTATEMENT_WINDOW_DAYS = 5

#: Google keeps 16 months. We keep it forever — this is the one-time grab of
#: history the customer cannot get anywhere else once Google drops it.
BACKFILL_MONTHS = 16

#: Query x page is the largest and least-queried dataset, so it is fetched for
#: a short recent window only.
QUERY_PAGE_WINDOW_DAYS = 90


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"window starts after it ends: {self.start}..{self.end}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_api(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()


def latest_available(today: date) -> date:
    """The most recent day worth asking Google about."""
    return today - timedelta(days=REPORTING_LAG_DAYS)


def backfill_window(today: date, months: int = BACKFILL_MONTHS) -> DateWindow:
    end = latest_available(today)
    # Approximate a month as 30 days deliberately: Google's own limit is
    # "16 months", and asking for a day too few loses history that is about to
    # be deleted, while asking for a day too many simply returns nothing.
    start = end - timedelta(days=months * 30)
    return DateWindow(start, end)


def incremental_window(today: date, days: int = RESTATEMENT_WINDOW_DAYS) -> DateWindow:
    end = latest_available(today)
    return DateWindow(end - timedelta(days=days - 1), end)


def month_chunks(window: DateWindow) -> list[DateWindow]:
    """Split a long window into calendar months.

    Sixteen months of date x query rows in one request would paginate for a
    long time and lose everything if it failed at the end. Per-month chunks
    make a backfill resumable at the month boundary and keep each pagination
    bounded.
    """
    chunks: list[DateWindow] = []
    cursor = window.start

    while cursor <= window.end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        chunk_end = min(next_month - timedelta(days=1), window.end)
        chunks.append(DateWindow(cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)

    return chunks
