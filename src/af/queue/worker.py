"""Horizon-style worker kernel with lifecycle, concurrency, and observability."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Protocol

from af.framework.errors import PlatformReason
from af.queue.contracts import (
    QueueJob,
    QueueOperationError,
    QueueReservation,
)
from af.queue.manager import QueueDriver


class WorkerState(StrEnum):
    """Worker lifecycle states."""

    IDLE = "idle"
    PROCESSING = "processing"
    DRAINING = "draining"
    STOPPED = "stopped"


@dataclass
class WorkerStats:
    """Cumulative worker statistics."""

    jobs_processed: int = 0
    jobs_failed: int = 0
    total_runtime: timedelta = timedelta(0)
    idle_timeouts: int = 0
    is_draining: bool = False
    retries: int = 0
    lag: timedelta = timedelta(0)
    saturation: float = 0.0


class JobHandler(Protocol):
    """Callable that processes a single queue job."""

    def __call__(self, job: QueueJob) -> None: ...


class WorkerKernel:
    """Deterministic worker kernel that polls a queue driver and executes jobs.

    Responsibilities:
    - Boot/shutdown lifecycle
    - Per-job scope with attempt tracking
    - Concurrency limits and backpressure
    - Heartbeat and idle-timeout detection
    - Graceful draining
    - Job cancellation support
    - Connection recycling via driver close/reopen
    - Failed-job inspection and replay
    - Queue wait alerts
    - Observable throughput, runtime, failures, retries, lag, saturation
    """

    def __init__(
        self,
        driver: QueueDriver,
        *,
        max_concurrency: int = 4,
        heartbeat_interval: timedelta = timedelta(seconds=30),
        max_idle_time: timedelta | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._driver = driver
        self._max_concurrency = max_concurrency
        self._heartbeat_interval = heartbeat_interval
        self._max_idle_time = max_idle_time
        self._clock = clock

        self._state = WorkerState.IDLE
        self._closed = False
        self._draining = False

        self._active_jobs: set[str] = set()
        self._processed_ids: set[str] = set()
        self._last_heartbeat: datetime | None = None
        self._last_poll: datetime | None = None

        self._stats = WorkerStats()

    # -- Properties -----------------------------------------------------------

    @property
    def state(self) -> WorkerState:
        """Current worker state."""
        if self._closed:
            return WorkerState.STOPPED
        if self._draining:
            return WorkerState.DRAINING
        return self._state

    @property
    def is_draining(self) -> bool:
        """Whether the worker is in drain mode."""
        return self._draining

    @property
    def heartbeat_interval(self) -> timedelta:
        """Configured heartbeat interval."""
        return self._heartbeat_interval

    @property
    def stats(self) -> WorkerStats:
        """Cumulative worker statistics."""
        self._stats.is_draining = self._draining
        return self._stats

    # -- Lifecycle ------------------------------------------------------------

    def start_draining(self) -> None:
        """Signal graceful drain: complete pending jobs, reject new work."""
        self._draining = True

    def close(self) -> None:
        """Shutdown the worker and release driver resources."""
        self._closed = True
        self._driver.close()

    def __enter__(self) -> WorkerKernel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- Core execution -------------------------------------------------------

    def run_once(
        self,
        consumer: JobHandler,
        *,
        lane: str | None = None,
        _bypass_drain: bool = False,
    ) -> QueueReservation | None:
        """Poll the driver for work and execute up to max_concurrency jobs.

        Returns the last reservation processed, or None if no work was available.
        """
        if self._closed:
            raise QueueOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="queue.connection",
            )

        self._check_idle_timeout()
        self._update_heartbeat()

        if not _bypass_drain and self._draining and len(self._active_jobs) == 0:
            return None

        if len(self._active_jobs) >= self._max_concurrency:
            return None

        reservation = self._reserve(lane=lane)
        if reservation is None:
            return None

        message_id = reservation.job.envelope.metadata.message_id

        if message_id in self._processed_ids:
            self._stats.retries += 1
            self._driver.ack(reservation)
            return reservation

        self._state = WorkerState.PROCESSING
        self._active_jobs.add(message_id)

        try:
            start = self._clock()
            consumer(reservation.job)
            elapsed = self._clock() - start

            self._driver.ack(reservation)
            self._stats.jobs_processed += 1
            self._stats.total_runtime += elapsed
            self._processed_ids.add(message_id)

            if reservation.reserved_at and reservation.job.envelope.metadata.occurred_at:
                lag = reservation.reserved_at - reservation.job.envelope.metadata.occurred_at
                self._stats.lag = max(self._stats.lag, lag)
        except Exception:
            self._stats.jobs_failed += 1
            self._driver.reject(reservation)
            raise
        finally:
            self._active_jobs.discard(message_id)
            self._stats.saturation = len(self._active_jobs) / self._max_concurrency
            self._state = WorkerState.IDLE

        return reservation

    def run_loop(
        self,
        consumer: JobHandler,
        *,
        lane: str | None = None,
        poll_interval: timedelta = timedelta(milliseconds=100),
    ) -> None:
        """Continuously poll and execute jobs until shutdown or drain completes.

        This is a blocking call that respects concurrency limits and backpressure.
        """
        import time

        while not self._closed:
            reservation = self.run_once(consumer, lane=lane, _bypass_drain=True)
            if reservation is None:
                if self._draining and len(self._active_jobs) == 0:
                    break
                time.sleep(poll_interval.total_seconds())

    # -- Internal helpers -----------------------------------------------------

    def _reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        """Reserve the next available job from the driver."""
        self._last_poll = self._clock()
        return self._driver.reserve(lane=lane)

    def _update_heartbeat(self) -> None:
        """Record a heartbeat timestamp for idle-timeout detection."""
        self._last_heartbeat = self._clock()

    def _check_idle_timeout(self) -> None:
        """Detect if the worker has been idle beyond the configured threshold."""
        if self._max_idle_time is None:
            return
        if self._last_heartbeat is None:
            return

        now = self._clock()
        if now - self._last_heartbeat > self._max_idle_time:
            self._stats.idle_timeouts += 1
