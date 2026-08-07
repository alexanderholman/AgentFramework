"""Worker kernel – safe long-lived queue worker with supervision and metrics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from af.queue import QueueDriver, QueueJob, QueueReservation


class WorkerState(StrEnum):
    """Worker lifecycle states."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


class CancellationPolicy(StrEnum):
    """How cancellation affects pending and in-flight jobs."""

    immediate = "immediate"
    drain = "drain"


class WaitAlertLevel(StrEnum):
    """Severity level for queue wait alerts."""

    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class JobTag:
    """Tag attached to every job for traceability."""

    correlation_id: str
    tenant_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    capability_id: str | None = None


@dataclass
class WorkerMetrics:
    """Accumulated worker observability counters."""

    jobs_processed: int = 0
    jobs_failed: int = 0
    retries: int = 0
    active_workers: int = 0
    max_concurrency: int = 1
    runtime: timedelta = field(default_factory=lambda: timedelta(0))
    _started_at: datetime | None = None

    def mark_start(self, now: datetime) -> None:
        self._started_at = now

    def tick(self, now: datetime) -> None:
        if self._started_at is not None:
            self.runtime = now - self._started_at


@dataclass
class BatchResult:
    """Outcome of a batch processing run."""

    jobs_processed: int = 0
    jobs_failed: int = 0


@dataclass
class FailedJob:
    """Record of a failed job for inspection and replay."""

    message_id: str
    error_message: str
    attempt: int
    failed_at: datetime


@dataclass
class HeartbeatConfig:
    """Configuration for heartbeat tracking."""

    interval: timedelta


@dataclass
class WaitAlertConfig:
    """Configuration for queue wait alerts."""

    warning_threshold: timedelta
    critical_threshold: timedelta
    on_alert: Callable[[WaitAlertLevel], None]


class OptimizationProvider(ABC):
    """Backend-specific optimization hints for workers."""

    @property
    @abstractmethod
    def recommended_concurrency(self) -> int:
        """Recommended concurrency level for the backend."""


class WorkerKernel:
    """Core worker loop that reserves, executes, and settles jobs."""

    def __init__(
        self,
        driver: QueueDriver,
        handler: Callable[[QueueJob], Any],
        *,
        max_concurrency: int = 1,
        max_retries: int = 0,
        clock: Callable[[datetime], datetime] | Callable[[], datetime] | None = None,
        cancellation: CancellationPolicy | None = None,
        heartbeat: HeartbeatConfig | None = None,
        wait_alert: WaitAlertConfig | None = None,
        provider: OptimizationProvider | None = None,
    ) -> None:
        self._driver = driver
        self._handler = handler
        self._max_concurrency = max_concurrency
        self._max_retries = max_retries
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state: WorkerState = WorkerState.IDLE
        self._metrics = WorkerMetrics(max_concurrency=max_concurrency)
        self._seen: set[str] = set()
        self._retry_counts: dict[str, int] = {}
        self._cancellation = cancellation
        self._heartbeat = heartbeat
        self._wait_alert = wait_alert
        self._provider = provider
        self._last_heartbeat: datetime | None = None
        self._failed_jobs: list[FailedJob] = []
        self._cancelled = False

    # -- Properties --

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def metrics(self) -> WorkerMetrics:
        return self._metrics

    @property
    def provider(self) -> OptimizationProvider | None:
        return self._provider

    @property
    def last_heartbeat(self) -> datetime | None:
        return self._last_heartbeat

    @property
    def failed_jobs(self) -> list[FailedJob]:
        return self._failed_jobs

    @property
    def is_stale(self) -> bool:
        if self._last_heartbeat is None or self._heartbeat is None:
            return False
        elapsed = self._now() - self._last_heartbeat
        return elapsed >= self._heartbeat.interval

    # -- Lifecycle --

    def boot(self) -> None:
        if self._state is WorkerState.RUNNING:
            return
        now = self._now()
        self._state = WorkerState.RUNNING
        self._metrics.mark_start(now)
        self._cancelled = False

    def shutdown(self) -> None:
        if self._state is WorkerState.STOPPED:
            return
        self._state = WorkerState.STOPPED
        self._metrics.tick(self._now())

    def recycle(self) -> None:
        """Close driver connection for recycling."""
        self._driver.close()

    # -- Cancellation --

    def cancel(self) -> None:
        """Cancel pending jobs and clear idempotency set."""
        if self._cancellation != CancellationPolicy.immediate:
            return
        self._cancelled = True
        self._seen.clear()

    # -- Processing --

    def process_one(self) -> bool:
        """Process a single job. Returns True if a job was processed."""
        if self._state != WorkerState.RUNNING:
            return False

        if self._cancelled:
            return False

        reservation = self._driver.reserve()
        if reservation is None:
            self._state = WorkerState.IDLE
            return False

        result = self._execute(reservation)

        # Update heartbeat
        if self._heartbeat is not None:
            self._last_heartbeat = self._now()

        # Check wait alert
        if self._wait_alert is not None:
            self._check_wait_alert(reservation.job)

        return result

    def process_batch(self, max_jobs: int = 10) -> BatchResult:
        """Process up to max_jobs respecting concurrency limits."""
        if self._state != WorkerState.RUNNING:
            return BatchResult()

        limit = min(max_jobs, self._max_concurrency)
        result = BatchResult()

        for _ in range(limit):
            reservation = self._driver.reserve()
            if reservation is None:
                break

            if self._execute(reservation):
                result.jobs_processed += 1
            else:
                result.jobs_failed += 1

        return result

    # -- Tagging --

    def build_tag(self, job: QueueJob) -> JobTag:
        """Build a JobTag from job metadata."""
        meta = job.envelope.metadata
        return JobTag(
            correlation_id=meta.message_id,
            tenant_id=getattr(meta, "tenant_id", None),
            agent_id=getattr(meta, "agent_id", None),
            task_id=getattr(meta, "task_id", None),
            capability_id=getattr(meta, "capability_id", None),
        )

    # -- Internal --

    def _execute(self, reservation: QueueReservation) -> bool:
        job = reservation.job
        mid = job.envelope.metadata.message_id
        now = self._now()

        # Idempotency: skip already-seen jobs
        if mid in self._seen:
            self._driver.ack(reservation)
            return True

        try:
            self._handler(job)
            self._driver.ack(reservation)
            self._seen.add(mid)
            self._metrics.jobs_processed += 1
            self._metrics.tick(now)
            return True
        except Exception as exc:
            self._metrics.jobs_failed += 1
            self._metrics.tick(now)
            self._failed_jobs.append(
                FailedJob(
                    message_id=mid,
                    error_message=str(exc),
                    attempt=reservation.attempt,
                    failed_at=now,
                )
            )
            self._handle_failure(reservation)
            return False

    def _handle_failure(self, reservation: QueueReservation) -> None:
        mid = reservation.job.envelope.metadata.message_id
        attempts = self._retry_counts.get(mid, 0)

        if attempts < self._max_retries:
            self._retry_counts[mid] = attempts + 1
            self._metrics.retries += 1
            self._driver.retry(reservation)
        else:
            self._driver.reject(reservation)

    def _check_wait_alert(self, job: QueueJob) -> None:
        if self._wait_alert is None:
            return
        now = self._now()
        enqueued_at = job.envelope.metadata.occurred_at
        wait_time = now - enqueued_at
        if wait_time >= self._wait_alert.critical_threshold:
            self._wait_alert.on_alert(WaitAlertLevel.CRITICAL)
        elif wait_time >= self._wait_alert.warning_threshold:
            self._wait_alert.on_alert(WaitAlertLevel.WARNING)

    def _now(self) -> datetime:
        clock = self._clock
        if callable(clock):
            import inspect

            sig = inspect.signature(clock)
            if len(sig.parameters) == 0:
                return clock()
            return clock(datetime.now(UTC))
        raise TypeError("clock must be callable")


class WorkerSupervisor:
    """Supervises multiple WorkerKernel instances."""

    def __init__(
        self,
        driver: QueueDriver,
        handler: Callable[[QueueJob], Any],
        *,
        num_workers: int = 1,
        max_concurrency: int = 1,
        max_retries: int = 0,
        clock: Callable[[datetime], datetime] | Callable[[], datetime] | None = None,
    ) -> None:
        self._driver = driver
        self._handler = handler
        self._num_workers = num_workers
        self._max_concurrency = max_concurrency
        self._max_retries = max_retries
        self._clock = clock
        self._kernels: list[WorkerKernel] = [
            WorkerKernel(
                driver,
                handler,
                max_concurrency=max_concurrency,
                max_retries=max_retries,
                clock=clock,
            )
            for _ in range(num_workers)
        ]

    # -- Properties --

    @property
    def kernels(self) -> list[WorkerKernel]:
        return self._kernels

    @property
    def metrics(self) -> WorkerMetrics:
        """Aggregated metrics across all kernels."""
        agg = WorkerMetrics()
        for k in self._kernels:
            agg.jobs_processed += k.metrics.jobs_processed
            agg.jobs_failed += k.metrics.jobs_failed
            agg.retries += k.metrics.retries
        return agg

    # -- Lifecycle --

    def boot(self) -> None:
        for k in self._kernels:
            k.boot()

    def shutdown(self) -> None:
        for k in self._kernels:
            k.shutdown()

    def recycle(self) -> None:
        """Recycle all connections."""
        for k in self._kernels:
            k.recycle()

    # -- Processing --

    def process(self) -> None:
        """Distribute jobs across kernels in round-robin fashion."""
        idx = 0
        while True:
            kernel = self._kernels[idx % len(self._kernels)]
            if not kernel.process_one():
                # Reboot kernel if it's no longer running
                if kernel.state is not WorkerState.RUNNING:
                    kernel.boot()
                # Try next kernel
                idx += 1
                if idx > len(self._kernels) * 2:
                    break
                continue
            idx += 1
