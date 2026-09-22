"""Cron expressions, parsed and evaluated.

Written here rather than taken from a library because it is a small, entirely
pure problem and the platform already keeps its decision-making in the domain
layer — which means the scheduler's behaviour can be tested without a clock, a
database or a container.

Five fields, Vixie semantics, always UTC. A schedule that means different
things in March and October is not a property anyone wants in a job that
reconciles billing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: field name -> (low, high), in the order the five fields appear.
FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),
)

#: Names accepted in the month and weekday fields, as cron has always allowed.
MONTHS = {
    name: index
    for index, name in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        start=1,
    )
}
WEEKDAYS = {
    name: index
    for index, name in enumerate(
        ["sun", "mon", "tue", "wed", "thu", "fri", "sat"], start=0
    )
}

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

#: How far back `due_slot` will look for a missed run. A worker that was down
#: for a week should not wake up and decide that yesterday's 03:00 job is
#: still worth running; the data it would have processed has moved on.
MAX_CATCHUP = timedelta(hours=25)

#: How far ahead `next_slot` will search before giving up. Longer than a year
#: on purpose: `0 0 29 2 *` is a legitimate schedule whose next occurrence can
#: be almost four years away, and a shorter window would report it as "never".
MAX_LOOKAHEAD = timedelta(days=366 * 5)


class InvalidSchedule(ValueError):
    """The expression is not a schedule. The message says which field."""


@dataclass(frozen=True, slots=True)
class CronSchedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]

    #: Whether the day-of-month and day-of-week fields were restricted.
    #:
    #: These two are the one place cron does not intersect its fields. When
    #: both are restricted the match is a UNION: `0 0 13 * fri` is "the 13th,
    #: and every Friday" — not "Friday the 13th". Getting this wrong makes a
    #: job that should run twice a month run twice a year, silently.
    day_restricted: bool
    weekday_restricted: bool

    expression: str

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minutes:
            return False
        if when.hour not in self.hours:
            return False
        if when.month not in self.months:
            return False

        # Python's weekday() is Monday=0; cron is Sunday=0.
        weekday = (when.weekday() + 1) % 7
        day_ok = when.day in self.days
        weekday_ok = weekday in self.weekdays

        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok

    def due_slot(
        self, now: datetime, *, horizon: timedelta = MAX_CATCHUP
    ) -> datetime | None:
        """The most recent minute at or before `now` that this schedule names.

        Deliberately the *most recent* rather than every missed one. A worker
        that was down overnight should run the nightly job once, now, not
        replay eight hours of hourly slots against data that has already moved
        past them — the sibling project's scheduler made the same choice for
        the same reason.

        Returns None if nothing is due within `horizon`.
        """
        cursor = now.astimezone(UTC).replace(second=0, microsecond=0)
        earliest = cursor - horizon
        while cursor >= earliest:
            if self.matches(cursor):
                return cursor
            cursor -= timedelta(minutes=1)
        return None

    def next_slot(
        self, after: datetime, *, limit: timedelta = MAX_LOOKAHEAD
    ) -> datetime | None:
        """The first minute strictly after `after` that this schedule names.

        Only for display — "next run in 4 hours" on the dashboard. The
        scheduler itself never needs it, because it asks what is due rather
        than predicting what will be.
        """
        cursor = after.astimezone(UTC).replace(second=0, microsecond=0)
        cursor += timedelta(minutes=1)
        deadline = cursor + limit
        while cursor <= deadline:
            if self.matches(cursor):
                return cursor
            # Skip a whole day when the date can never match, rather than
            # stepping 1,440 times through it.
            if not self._date_could_match(cursor):
                cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            cursor += timedelta(minutes=1)
        return None

    def _date_could_match(self, when: datetime) -> bool:
        if when.month not in self.months:
            return False
        weekday = (when.weekday() + 1) % 7
        day_ok = when.day in self.days
        weekday_ok = weekday in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok


def parse(expression: str) -> CronSchedule:
    """Parse a five-field expression, or one of the `@daily` aliases."""
    raw = expression.strip().lower()
    if not raw:
        raise InvalidSchedule("A schedule is required")
    raw = ALIASES.get(raw, raw)

    parts = raw.split()
    if len(parts) != 5:
        raise InvalidSchedule(
            f"A cron expression has five fields "
            f"(minute hour day month weekday); got {len(parts)} in {expression!r}"
        )

    parsed: list[frozenset[int]] = []
    for part, (name, low, high) in zip(parts, FIELDS, strict=True):
        parsed.append(_field(part, name, low, high))

    return CronSchedule(
        minutes=parsed[0],
        hours=parsed[1],
        days=parsed[2],
        months=parsed[3],
        weekdays=parsed[4],
        day_restricted=parts[2] != "*",
        weekday_restricted=parts[4] != "*",
        expression=" ".join(parts),
    )


_STEP = re.compile(r"^(?P<range>[^/]+)(?:/(?P<step>\d+))?$")


def _field(part: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for chunk in part.split(","):
        values |= _chunk(chunk, name, low, high)
    if not values:
        raise InvalidSchedule(f"The {name} field {part!r} matches nothing")
    return frozenset(values)


def _chunk(chunk: str, name: str, low: int, high: int) -> set[int]:
    match = _STEP.match(chunk)
    if not match:
        raise InvalidSchedule(f"{chunk!r} is not valid in the {name} field")

    body = match.group("range")
    step_text = match.group("step")
    step = int(step_text) if step_text else 1
    if step < 1:
        raise InvalidSchedule(f"A step of {step} is not valid in the {name} field")

    if body == "*":
        start, end = low, high
    elif "-" in body.lstrip("-"):
        start_text, _, end_text = body.partition("-")
        start = _value(start_text, name, low, high)
        end = _value(end_text, name, low, high)
        if start > end:
            raise InvalidSchedule(f"{body!r} runs backwards in the {name} field")
    else:
        start = _value(body, name, low, high)
        # `5/10` means "from 5, every 10" — a bare value with a step is open
        # ended, where a bare value alone is just itself.
        end = high if step_text else start

    return set(range(start, end + 1, step))


def _value(text: str, name: str, low: int, high: int) -> int:
    token = text.strip()
    if name == "month" and token in MONTHS:
        return MONTHS[token]
    if name == "weekday" and token in WEEKDAYS:
        return WEEKDAYS[token]
    if not token.isdigit():
        raise InvalidSchedule(f"{text!r} is not a number in the {name} field")

    value = int(token)
    # Cron has always accepted 7 for Sunday alongside 0.
    if name == "weekday" and value == 7:
        return 0
    if not low <= value <= high:
        raise InvalidSchedule(
            f"{value} is out of range in the {name} field ({low}-{high})"
        )
    return value


def describe(schedule: CronSchedule) -> str:
    """A short human rendering, for the dashboard.

    Only the handful of shapes people actually write get a sentence; anything
    else keeps its expression, which is more honest than a paraphrase that
    might be wrong.
    """
    expression = schedule.expression
    if expression == "0 * * * *":
        return "every hour, on the hour"
    if re.fullmatch(r"\d+ \* \* \* \*", expression):
        return f"every hour, at {schedule.expression.split()[0]} past"
    if re.fullmatch(r"\d+ \d+ \* \* \*", expression):
        minute, hour = expression.split()[:2]
        return f"every day at {int(hour):02d}:{int(minute):02d} UTC"
    if re.fullmatch(r"\*/\d+ \* \* \* \*", expression):
        return f"every {expression.split()[0][2:]} minutes"
    return expression
