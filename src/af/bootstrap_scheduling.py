"""Reference local scheduling composition over the real queue and task use cases."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from af.application.scheduling import ScheduledJobHandler, ScheduledTaskContext, SchedulerService
from af.application.tasks import TaskController
from af.cache.database import DatabaseCacheDriver
from af.config import Settings
from af.framework.scheduling import ScheduleError
from af.infrastructure.database import Database
from af.infrastructure.scheduling import (
    CacheSchedulerLocks,
    DurableSchedulerQueue,
    SQLiteScheduleHistory,
    load_schedule_definitions,
)
from af.infrastructure.sqlite import SQLiteDriver
from af.interfaces.scheduling import SchedulingCommands
from af.queue.database_manager import database_queue_registration
from af.queue.manager import QueueFeature, QueueManager
from af.queue.worker import WorkerKernel


class SchedulerRuntime:
    def __init__(self, settings: Settings, database: Database, tasks: TaskController) -> None:
        url = database.engine.url
        if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
            raise ValueError("reference scheduler requires a persistent SQLite database")
        path = Path(str(url.database)).expanduser().resolve()
        definitions = (
            load_schedule_definitions(Path(settings.schedules_path))
            if settings.schedules_path is not None
            else ()
        )
        if any(item.task_id != "task.create" for item in definitions):
            raise ScheduleError("schedule_task_not_installed")
        history = SQLiteScheduleHistory(path)
        self.cache = DatabaseCacheDriver(url=str(url))
        self.queues = QueueManager(
            connections={"primary": {"driver": "database"}},
            default="primary",
            deployment_profile="local",
            policy_version="af-schedules-v1",
        )
        self.queues.install(
            database_queue_registration(database_factory=lambda: SQLiteDriver(path))
        )
        locks = CacheSchedulerLocks(self.cache)
        self.service = SchedulerService(
            definitions,
            history,
            locks,
            DurableSchedulerQueue(self.queues),
            instance_id="local-" + uuid4().hex,
        )

        def create_task(arguments: Mapping[str, str], context: ScheduledTaskContext) -> None:
            if set(arguments) != {"prompt"} or context.cancellation.cancelled:
                raise ScheduleError("schedule_task_invalid_or_cancelled")
            tasks.create(arguments["prompt"])

        self.handler = ScheduledJobHandler(
            definitions, history, locks, {"task.create": create_task}
        )
        self.commands = SchedulingCommands(self.service, history, work_once=self.work_once)

    def work_once(self) -> str | None:
        worker = WorkerKernel(
            self.queues.connection(
                requires=(QueueFeature.RESERVATION, QueueFeature.RESERVATION_RENEWAL)
            ),
            max_concurrency=1,
            driver_close=lambda: None,
        )
        try:
            reservation = worker.run_once(self.handler, lane="scheduler")
            return None if reservation is None else reservation.job.envelope.metadata.message_id
        finally:
            worker.close(timeout=timedelta(seconds=30))

    def close(self) -> None:
        try:
            self.service.shutdown()
        finally:
            try:
                self.queues.close()
            finally:
                self.cache.close()
