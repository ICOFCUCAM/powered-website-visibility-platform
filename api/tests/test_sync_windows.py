"""Date windows for Search Console sync."""

from __future__ import annotations

from datetime import date

import pytest

from api.hub.services.sync.windows import (
    DateWindow,
    backfill_window,
    incremental_window,
    latest_available,
    month_chunks,
)

TODAY = date(2026, 9, 21)


def test_never_asks_for_data_google_does_not_have_yet():
    """Search Console lags 2-3 days. Asking for today returns nothing and
    asking for yesterday returns a number that will change."""
    assert latest_available(TODAY) == date(2026, 9, 18)


def test_the_nightly_sync_re_fetches_a_trailing_window():
    """Google restates recent days, so yesterday-only would freeze the first
    number it ever saw for each day."""
    window = incremental_window(TODAY)
    assert (window.start, window.end) == (date(2026, 9, 14), date(2026, 9, 18))
    assert window.days == 5


def test_the_backfill_reaches_back_sixteen_months():
    window = backfill_window(TODAY)
    assert window.end == date(2026, 9, 18)
    assert window.start < date(2025, 6, 1)
    assert window.days > 400


def test_a_long_window_is_split_into_calendar_months():
    chunks = month_chunks(DateWindow(date(2026, 1, 15), date(2026, 4, 3)))
    assert [(c.start, c.end) for c in chunks] == [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 31)),
        (date(2026, 4, 1), date(2026, 4, 3)),
    ]


def test_chunks_cover_the_window_exactly_with_no_gaps_or_overlap():
    window = backfill_window(TODAY)
    chunks = month_chunks(window)

    assert chunks[0].start == window.start
    assert chunks[-1].end == window.end
    assert sum(c.days for c in chunks) == window.days
    for earlier, later in zip(chunks[:-1], chunks[1:], strict=True):
        assert (later.start - earlier.end).days == 1


def test_a_year_boundary_does_not_break_chunking():
    chunks = month_chunks(DateWindow(date(2025, 12, 20), date(2026, 1, 5)))
    assert [(c.start, c.end) for c in chunks] == [
        (date(2025, 12, 20), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 5)),
    ]


def test_a_backwards_window_is_rejected_rather_than_silently_empty():
    with pytest.raises(ValueError):
        DateWindow(date(2026, 2, 1), date(2026, 1, 1))
