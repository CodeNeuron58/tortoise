"""The worker process: registers the workflow and activities and runs forever.

One worker serves every council; each weekly run is a separate workflow
execution, so one slow council never blocks another.
"""

from __future__ import annotations

import logging

from temporalio.worker import Worker

from ..config import load_config
from ..storage import Database
from . import activities
from .connection import TASK_QUEUE, connect
from .weekly_watch import WeeklyWatch


async def run_worker() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    config = load_config()
    database = Database(config.data_dir / "tortoise.db")
    database.init_schema()

    client = await connect()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[WeeklyWatch],
        activities=[
            activities.get_council_settings,
            activities.check_not_already_run,
            activities.crawl_list,
            activities.fetch_application,
            activities.gemma_filter,
            activities.write_brief,
            activities.send_email,
            activities.record_run,
        ],
    )
    logging.getLogger(__name__).info("worker up on task queue %r", TASK_QUEUE)
    await worker.run()
