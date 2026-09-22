"""Cron parsing and evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from forge.domain.schedule import InvalidSchedule, describe, parse


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class TestParsing:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("0 0 * * *", "0 0 * * *"),
            ("@daily", "0 0 * * *"),
            ("@hourly", "0 * * * *"),
            ("  0   3  *  *  *  ", "0 3 * * *"),
        ],
    )
    def test_aliases_and_spacing_normalise(self, expression, expected):
        assert parse(expression).expression == expected

    def test_steps_ranges_and_lists(self):
        schedule = parse("0,30 9-17/4 * * *")
        assert schedule.minutes == frozenset({0, 30})
        assert schedule.hours == frozenset({9, 13, 17})

    def test_a_bare_value_with_a_step_runs_to_the_end_of_the_field(self):
        """`5/10` is 'from 5, every 10' — not just 5."""
        assert parse("5/10 * * * *").minutes == frozenset({5, 15, 25, 35, 45, 55})

    def test_a_bare_value_without_a_step_is_only_itself(self):
        assert parse("5 * * * *").minutes == frozenset({5})

    def test_month_and_weekday_names(self):
        schedule = parse("0 0 * jan-mar mon,fri")
        assert schedule.months == frozenset({1, 2, 3})
        assert schedule.weekdays == frozenset({1, 5})

    def test_seven_is_sunday_as_well_as_zero(self):
        assert parse("0 0 * * 7").weekdays == parse("0 0 * * 0").weekdays

    @pytest.mark.parametrize(
        ("expression", "complaint"),
        [
            ("0 0 * *", "five fields"),
            ("", "required"),
            ("60 * * * *", "minute"),
            ("* 24 * * *", "hour"),
            ("* * 32 * *", "day"),
            ("* * * 13 *", "month"),
            ("* * * * 8", "weekday"),
            ("17-3 * * * *", "backwards"),
            ("nonsense * * * *", "minute"),
            ("*/0 * * * *", "step"),
        ],
    )
    def test_bad_expressions_name_the_field(self, expression, complaint):
        with pytest.raises(InvalidSchedule) as exc:
            parse(expression)
        assert complaint in str(exc.value).lower()


class TestDayOfMonthAndWeekday:
    """The one place cron does not intersect its fields.

    When both day-of-month and day-of-week are restricted, the match is a
    union. Getting this backwards turns a job that should run twice a month
    into one that runs twice a year, and nothing announces it.
    """

    def test_both_restricted_is_a_union_not_an_intersection(self):
        schedule = parse("0 0 13 * fri")
        # Friday the 13th — matches on both counts.
        assert schedule.matches(at("2026-11-13T00:00"))
        # The 13th, a Tuesday. Matches by day-of-month alone.
        assert schedule.matches(at("2026-10-13T00:00"))
        # A Friday that is not the 13th. Matches by weekday alone.
        assert schedule.matches(at("2026-10-16T00:00"))
        # Neither.
        assert not schedule.matches(at("2026-10-14T00:00"))

    def test_only_day_of_month_restricted_ignores_the_weekday(self):
        schedule = parse("0 0 1 * *")
        assert schedule.matches(at("2026-10-01T00:00"))
        assert not schedule.matches(at("2026-10-02T00:00"))

    def test_only_weekday_restricted_ignores_the_day_of_month(self):
        schedule = parse("0 0 * * mon")
        assert schedule.matches(at("2026-10-05T00:00"))
        assert not schedule.matches(at("2026-10-06T00:00"))


class TestDueSlot:
    def test_the_slot_is_the_expression_time_not_the_call_time(self):
        slot = parse("0 3 * * *").due_slot(at("2026-09-22T03:00:41"))
        assert slot == at("2026-09-22T03:00")

    def test_a_worker_that_was_down_catches_up_to_the_missed_slot(self):
        """Late, not lost — the point of claiming a slot rather than firing
        on a timer."""
        slot = parse("0 3 * * *").due_slot(at("2026-09-22T07:12:00"))
        assert slot == at("2026-09-22T03:00")

    def test_only_the_most_recent_missed_slot_is_returned(self):
        """An hourly job whose worker was down for six hours runs once, now —
        not six times against data that has moved past them."""
        slot = parse("0 * * * *").due_slot(at("2026-09-22T09:30:00"))
        assert slot == at("2026-09-22T09:00")

    def test_nothing_is_due_beyond_the_catch_up_horizon(self):
        """A worker down for a week should not decide last Tuesday's job is
        still worth running."""
        assert parse("0 3 * * mon").due_slot(at("2026-09-27T12:00")) is None

    def test_the_horizon_is_configurable(self):
        assert (
            parse("0 3 * * mon").due_slot(
                at("2026-09-27T12:00"), horizon=timedelta(days=14)
            )
            is not None
        )


class TestNextSlot:
    def test_it_finds_the_next_occurrence(self):
        assert parse("0 3 * * *").next_slot(at("2026-09-22T04:00")) == at(
            "2026-09-23T03:00"
        )

    def test_it_is_strictly_after_the_given_time(self):
        """Otherwise 'next run' on a job that just fired shows the run that
        has already happened."""
        assert parse("0 3 * * *").next_slot(at("2026-09-22T03:00")) == at(
            "2026-09-23T03:00"
        )

    def test_it_skips_months_without_stepping_through_every_minute(self):
        assert parse("0 0 29 2 *").next_slot(at("2026-03-01T00:00")) == at(
            "2028-02-29T00:00"
        )

    def test_it_gives_up_rather_than_looping_forever(self):
        assert parse("0 0 30 2 *").next_slot(at("2026-01-01T00:00")) is None


def test_common_schedules_are_described_in_words():
    assert describe(parse("0 3 * * *")) == "every day at 03:00 UTC"
    assert describe(parse("@hourly")) == "every hour, on the hour"
    assert describe(parse("*/15 * * * *")) == "every 15 minutes"
    # Anything unusual keeps its expression rather than risking a wrong
    # paraphrase.
    assert describe(parse("0 0 13 * fri")) == "0 0 13 * fri"
