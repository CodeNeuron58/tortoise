"""The Monday schedule: one cron per council, 07:00 UK time.

The cron is ``0 7 * * MON`` in ``Europe/London`` per the plan. The scheduled
workflow computes its own week, so the schedule never needs reinstalling when
a week rolls over — the storage dedup guard protects against double sends.
"""

from __future__ import annotations

from dataclasses import dataclass

from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleSpec,
)
from temporalio.common import WorkflowIDReusePolicy

from .connection import TASK_QUEUE, connect
from .messages import WeeklyWatchInput
from .weekly_watch import WeeklyWatch

SCHEDULE_PREFIX = "tortoise-weekly-"
CRON = "0 7 * * MON"  # every Monday at 07:00
TIME_ZONE = "Europe/London"


@dataclass(slots=True, frozen=True)
class ScheduleInfo:
    id: str
    cron: str
    time_zone: str


def schedule_id(council_slug: str) -> str:
    return f"{SCHEDULE_PREFIX}{council_slug}"


async def install(council_slug: str) -> str:
    """Create (or replace) the weekly schedule for one council."""
    client = await connect()
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            WeeklyWatch.run,
            WeeklyWatchInput(council=council_slug),  # week is computed inside the workflow
            id=f"watch-{council_slug}-scheduled",
            task_queue=TASK_QUEUE,
            # A previous Monday's completed run with the same id must not
            # block this Monday's run; the week dedup guard is the real lock.
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        ),
        spec=ScheduleSpec(cron_strings=[CRON], time_zone_name=TIME_ZONE),
    )
    await client.create_schedule(schedule_id(council_slug), schedule)
    return schedule_id(council_slug)


async def remove(council_slug: str) -> None:
    handle = (await connect()).get_schedule_handle(schedule_id(council_slug))
    await handle.delete()


async def list_schedules() -> list[ScheduleInfo]:
    client = await connect()
    infos: list[ScheduleInfo] = []
    async for schedule in client.list_schedules():
        cron = ", ".join(schedule.spec.cron_strings) if schedule.spec else ""
        time_zone = schedule.spec.time_zone_name if schedule.spec else ""
        infos.append(ScheduleInfo(id=schedule.id, cron=cron, time_zone=time_zone))
    return infos
