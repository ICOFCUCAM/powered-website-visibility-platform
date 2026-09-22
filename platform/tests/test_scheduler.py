"""Turning schedules into claimed slots."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forge.adapters.containers import command_argv
from forge.engine import scheduler
from tests import fakes


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.fixture
def queue(monkeypatch):
    """Record what the sweep would enqueue, without a database."""
    claimed: list[tuple] = []
    finished: list[dict] = []
    taken: set[tuple] = set()

    async def claim_slot(
        process_id, slot, deployment_id, *, status="pending", detail=None
    ):
        key = (process_id, slot)
        if key in taken:
            # What the unique constraint on (process_id, scheduled_for) does.
            return None
        taken.add(key)
        claimed.append((process_id, slot, deployment_id))
        finished.append({"status": status, "detail": detail})
        return fakes.job_run(process_id=process_id, scheduled_for=slot)

    async def finish_run(run_id, **kwargs):
        finished.append(kwargs)
        return fakes.job_run()

    monkeypatch.setattr("forge.engine.scheduler.process_repo.claim_slot", claim_slot)
    monkeypatch.setattr("forge.engine.scheduler.process_repo.finish_run", finish_run)
    return {"claimed": claimed, "finished": finished}


def with_processes(monkeypatch, processes, project):
    async def listed():
        return processes

    async def get(_id):
        return project

    monkeypatch.setattr("forge.engine.scheduler.process_repo.list_scheduled", listed)
    monkeypatch.setattr("forge.engine.scheduler.project_repo.get", get)


class TestSweep:
    async def test_a_due_slot_is_enqueued_at_the_schedules_minute(
        self, monkeypatch, queue
    ):
        """The slot is the time the expression named, not the time the sweep
        happened to run — which is what makes the run identifiable."""
        live = fakes.deployment()
        with_processes(
            monkeypatch,
            [fakes.cron(schedule="0 3 * * *")],
            fakes.project(production_deployment_id=live.id),
        )
        assert await scheduler.sweep(now=at("2026-09-22T03:00:47")) == 1
        _, slot, deployment_id = queue["claimed"][0]
        assert slot == at("2026-09-22T03:00")
        assert deployment_id == live.id

    async def test_sweeping_repeatedly_enqueues_a_slot_once(self, monkeypatch, queue):
        """The sweep runs every 20 seconds; a minute-resolution schedule would
        otherwise fire three times."""
        with_processes(
            monkeypatch,
            [fakes.cron(schedule="0 3 * * *")],
            fakes.project(production_deployment_id=fakes.deployment().id),
        )
        first = await scheduler.sweep(now=at("2026-09-22T03:00:05"))
        second = await scheduler.sweep(now=at("2026-09-22T03:00:25"))
        third = await scheduler.sweep(now=at("2026-09-22T03:00:45"))
        assert (first, second, third) == (1, 0, 0)
        assert len(queue["claimed"]) == 1

    async def test_nothing_is_enqueued_when_the_last_slot_is_long_past(
        self, monkeypatch, queue
    ):
        """A monthly job swept on the 23rd owes nothing: its slot was three
        weeks ago, well beyond the catch-up horizon, so it is not resurrected.

        Note the flip side, which is deliberate rather than accidental: a
        *daily* job swept at noon still owes its 03:00 slot, because nine
        hours is inside the horizon. That is the whole point of claiming a
        slot rather than firing on a timer — a worker that was down at 03:00
        runs the job late instead of skipping the day. The sweep is safe to
        repeat because the slot, once claimed, is taken.
        """
        with_processes(
            monkeypatch,
            [fakes.cron(schedule="0 3 1 * *")],
            fakes.project(production_deployment_id=fakes.deployment().id),
        )
        assert await scheduler.sweep(now=at("2026-09-23T12:00:00")) == 0

    async def test_a_missed_slot_is_still_claimed_when_the_worker_returns(
        self, monkeypatch, queue
    ):
        """Late, not lost."""
        with_processes(
            monkeypatch,
            [fakes.cron(schedule="0 3 * * *")],
            fakes.project(production_deployment_id=fakes.deployment().id),
        )
        assert await scheduler.sweep(now=at("2026-09-22T06:40:00")) == 1
        assert queue["claimed"][0][1] == at("2026-09-22T03:00")

    async def test_a_job_with_no_production_deployment_is_recorded_as_skipped(
        self, monkeypatch, queue
    ):
        """Recorded rather than dropped: a silent gap in the history looks
        like the scheduler is broken."""
        with_processes(
            monkeypatch,
            [fakes.cron(schedule="0 3 * * *")],
            fakes.project(production_deployment_id=None),
        )
        await scheduler.sweep(now=at("2026-09-22T03:00:00"))
        # Written in its final state, not inserted pending and then finished —
        # which would leave a window for the job loop to claim it first.
        assert queue["finished"][0]["status"] == "skipped"
        assert "production" in queue["finished"][0]["detail"]

    async def test_one_unreadable_schedule_does_not_stop_the_others(
        self, monkeypatch, queue
    ):
        """A typo in one project's cron must not stop every other project's
        jobs from being queued."""
        with_processes(
            monkeypatch,
            [
                fakes.cron(name="broken", schedule="not a schedule"),
                fakes.cron(name="fine", schedule="0 3 * * *"),
            ],
            fakes.project(production_deployment_id=fakes.deployment().id),
        )
        assert await scheduler.sweep(now=at("2026-09-22T03:00:00")) == 1

    async def test_a_disabled_job_is_never_swept(self, monkeypatch, queue):
        """`list_scheduled` filters on `enabled` in SQL, so pausing is a
        property of the query and not of a check the sweep might forget."""
        with_processes(monkeypatch, [], fakes.project(production_deployment_id=None))
        assert await scheduler.sweep(now=at("2026-09-22T03:00:00")) == 0


class TestCommandArgv:
    def test_a_simple_command_is_passed_straight_to_the_image(self):
        """No shell. A distroless image has no /bin/sh, so wrapping every
        command in one would make cron impossible on exactly the images this
        platform builds for Go."""
        assert command_argv("node cleanup.js") == ["node", "cleanup.js"]

    def test_quotes_are_respected(self):
        assert command_argv("python -c 'print(1)'") == ["python", "-c", "print(1)"]

    def test_a_command_needing_a_shell_gets_one_explicitly(self):
        assert command_argv("rake db:clean && rake report") == [
            "/bin/sh",
            "-c",
            "exec rake db:clean && rake report",
        ]

    def test_no_command_means_the_images_own_entrypoint(self):
        assert command_argv(None) == []
        assert command_argv("") == []
