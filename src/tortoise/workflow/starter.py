"""Starting a WeeklyWatch run by hand — the demo path and failure-test path."""

from __future__ import annotations

from datetime import datetime, timezone

from temporalio.common import WorkflowIDReusePolicy

from ..models import RunNumbers
from .connection import TASK_QUEUE, connect
from .messages import WeeklyWatchInput
from .weekly_watch import WeekAlreadyRunError, WeeklyWatch


def iso_week_for_today() -> str:
    """This week's ISO label, for manual runs and readable workflow ids."""
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


async def start_and_wait(
    council: str,
    week: str | None = None,
    local_browser: bool = True,
) -> RunNumbers:
    """Start one run and wait for its Lazy Payoff numbers.

    The workflow id carries the week, and the reject-duplicate policy makes a
    second run of the same week fail fast at the client (failure test 5: the
    second run is rejected by its id; one email).
    """
    week = week or iso_week_for_today()
    client = await connect()
    handle = await client.start_workflow(
        WeeklyWatch.run,
        WeeklyWatchInput(council=council, week=week, local_browser=local_browser),
        id=f"watch-{council}-{week}",
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    try:
        return await handle.result()
    except WeekAlreadyRunError:
        raise
