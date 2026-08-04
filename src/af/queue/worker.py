"""Synchronous queue worker lifecycle with explicit reliability boundaries."""

from __future__ import annotations

import inspect
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Condition, RLock, get_ident
from types import TracebackType
from typing import Protocol, cast

from af.framework.errors import PlatformReason
from af.queue.contracts import QueueJob, QueueOperationError, QueueReservation
from af.queue.manager import QueueDriver


class WorkerState(StrEnum):
    """Worker lifecycle states."""

    IDLE = "idle"
    PROCESSING = "processing"
    DRAINING = "draining"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class WorkerStats:
    """Immutable cumulative worker statistics snapshot."""

    jobs_processed: int = 0
    jobs_failed: int = 0
    total_runtime: timedelta = timedelta(0)
    idle_timeouts: int = 0
    is_draining: bool = False
    retries: int = 0
    cancellations: int = 0
    duplicates: int = 0
    lag: timedelta = timedelta(0)
    saturation: float = 0.0


class JobHandler(Protocol):
    """Callable that processes one queue job."""

    def __call__(self, job: QueueJob) -> None: ...


class IdempotencyStore(Protocol):
    """Deduplication boundary shared with a handler's authoritative state."""

    def claim(self, message_id: str, /) -> AbstractContextManager[IdempotencyClaim]: ...


class IdempotencyClaim(Protocol):
    """Atomic claim held across one handler transaction."""

    @property
    def already_processed(self) -> bool: ...

    def record(self) -> None: ...


class _InMemoryClaim:
    def __init__(self, store: InMemoryIdempotencyStore, message_id: str) -> None:
        self._store = store
        self._message_id = message_id

    @property
    def already_processed(self) -> bool:
        return self._message_id in self._store._entries

    def record(self) -> None:
        self._store._entries.pop(self._message_id, None)
        self._store._entries[self._message_id] = None
        while len(self._store._entries) > self._store._capacity:
            self._store._entries.popitem(last=False)


class _UntrackedClaim:
    already_processed = False

    def record(self) -> None:
        return


class InMemoryIdempotencyStore:
    """Bounded process-local deduplication for explicitly ephemeral workers."""

    def __init__(self, *, capacity: int = 10_000) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("worker idempotency capacity must be positive")
        self._capacity = capacity
        self._entries: OrderedDict[str, None] = OrderedDict()
        self._locks = tuple(RLock() for _ in range(64))

    @contextmanager
    def claim(self, message_id: str, /):  # type: ignore[no-untyped-def]
        lock = self._locks[hash(message_id) % len(self._locks)]
        with lock:
            yield _InMemoryClaim(self, message_id)


JobScopeFactory = Callable[[QueueJob], AbstractContextManager[None]]


def _validate_sync_result(result: object) -> None:
    if inspect.iscoroutine(result):
        result.close()
        raise TypeError("sync queue worker returned an asynchronous result")
    if inspect.isawaitable(result) or inspect.isasyncgen(result):
        raise TypeError("sync queue worker returned an asynchronous result")
    if result is not None:
        raise TypeError("sync queue worker handler must return None")


class WorkerKernel:
    """Reserve and execute queue jobs with bounded synchronous lifecycle control.

    ``run_once`` may be called concurrently by multiple host threads. Admission is
    serialized and limited by ``max_concurrency`` while handlers execute outside the
    admission lock. ``start_draining`` stops all new reservations; work already admitted
    is allowed to settle before ``close`` releases the driver.

    Durable exactly-once business effects are deliberately not claimed. Deployments that
    need deduplication must inject an ``IdempotencyStore`` whose ``record`` operation shares
    the handler's authoritative transaction or durable intent boundary.
    """

    def __init__(
        self,
        driver: QueueDriver,
        *,
        max_concurrency: int = 4,
        heartbeat_interval: timedelta = timedelta(seconds=30),
        max_idle_time: timedelta | None = None,
        idempotency: IdempotencyStore | None = None,
        scope_factory: JobScopeFactory | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("worker maximum concurrency must be positive")
        if heartbeat_interval <= timedelta(0):
            raise ValueError("worker heartbeat interval must be positive")
        if max_idle_time is not None and max_idle_time <= timedelta(0):
            raise ValueError("worker maximum idle time must be positive")
        for method in ("reserve", "ack", "reject", "retry", "close"):
            if not callable(getattr(driver, method, None)):
                raise TypeError(f"worker driver requires callable {method}")
        if idempotency is not None and not callable(getattr(idempotency, "claim", None)):
            raise TypeError("worker idempotency store requires callable claim")
        if scope_factory is not None and not callable(scope_factory):
            raise TypeError("worker scope factory must be callable")
        if not callable(clock):
            raise TypeError("worker clock must be callable")

        self._driver = driver
        self._max_concurrency = max_concurrency
        self._heartbeat_interval = heartbeat_interval
        self._max_idle_time = max_idle_time
        self._idempotency = idempotency
        self._scope_factory = scope_factory or (lambda job: nullcontext())
        self._clock = clock

        self._state = WorkerState.IDLE
        self._closed = False
        self._closing = False
        self._draining = False
        self._active_jobs: set[str] = set()
        self._active_threads: set[int] = set()
        self._last_heartbeat: datetime | None = None
        self._last_activity: datetime | None = None
        self._stats = WorkerStats()
        self._condition = Condition(RLock())

    @property
    def state(self) -> WorkerState:
        """Return the current lifecycle state."""
        with self._condition:
            if self._closed:
                return WorkerState.STOPPED
            if self._draining:
                return WorkerState.DRAINING
            return self._state

    @property
    def is_draining(self) -> bool:
        """Whether admission has stopped for graceful shutdown."""
        with self._condition:
            return self._draining

    @property
    def heartbeat_interval(self) -> timedelta:
        """Configured minimum heartbeat interval."""
        return self._heartbeat_interval

    @property
    def stats(self) -> WorkerStats:
        """Return an immutable point-in-time statistics snapshot."""
        with self._condition:
            return replace(self._stats, is_draining=self._draining)

    def start_draining(self) -> None:
        """Stop reserving new work while admitted jobs finish or settle."""
        with self._condition:
            self._draining = True
            self._condition.notify_all()

    def close(self, *, timeout: timedelta | None = None) -> None:
        """Wait for admitted jobs, then close the queue driver exactly once."""
        if timeout is not None and timeout <= timedelta(0):
            raise ValueError("worker shutdown timeout must be positive")
        deadline = time.monotonic() + timeout.total_seconds() if timeout is not None else None
        with self._condition:
            if self._closed:
                return
            if get_ident() in self._active_threads:
                raise QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="queue.worker.self-shutdown",
                )
            self._draining = True
            while self._closing:
                self._condition.wait()
                if self._is_closed():
                    return
            while self._active_jobs:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise QueueOperationError(
                        reason=PlatformReason.SHUTDOWN_FAILED,
                        stage="queue.worker.shutdown",
                    )
                self._condition.wait(timeout=remaining)
            self._closing = True
        try:
            self._driver.close()
        except BaseException:
            with self._condition:
                self._closing = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._closed = True
            self._closing = False
            self._state = WorkerState.STOPPED
            self._condition.notify_all()

    def __enter__(self) -> WorkerKernel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def run_once(
        self,
        consumer: JobHandler,
        *,
        lane: str | None = None,
    ) -> QueueReservation | None:
        """Admit and synchronously execute at most one job."""
        reservation = self._admit(lane=lane)
        if reservation is None:
            return None

        message_id = reservation.job.envelope.metadata.message_id
        started = self._clock()
        try:
            try:
                claim_context = (
                    self._idempotency.claim(message_id)
                    if self._idempotency is not None
                    else nullcontext(_UntrackedClaim())
                )
                with claim_context as claim:
                    duplicate = claim.already_processed
                    if not duplicate:
                        with self._scope_factory(reservation.job):
                            sync_consumer = cast(Callable[[QueueJob], object], consumer)
                            result = sync_consumer(reservation.job)
                            _validate_sync_result(result)
                            claim.record()
            except Exception as error:
                self._increment("jobs_failed")
                self._settle_failure(reservation, error)
                raise
            except BaseException as cancellation:
                self._increment("cancellations")
                self._release_cancelled(reservation, cancellation)
                raise

            try:
                self._driver.ack(reservation)
            except BaseException as acknowledgement_error:
                self._release_uncertain(reservation, acknowledgement_error)
                raise
            self._increment("duplicates" if duplicate else "jobs_processed")
            return reservation
        finally:
            elapsed = self._clock() - started
            with self._condition:
                self._stats = replace(
                    self._stats,
                    total_runtime=self._stats.total_runtime + elapsed,
                )
                self._active_jobs.discard(message_id)
                self._active_threads.discard(get_ident())
                self._last_activity = self._clock()
                self._state = WorkerState.PROCESSING if self._active_jobs else WorkerState.IDLE
                self._condition.notify_all()

    def run_loop(
        self,
        consumer: JobHandler,
        *,
        lane: str | None = None,
        poll_interval: timedelta = timedelta(milliseconds=100),
    ) -> None:
        """Poll serially until shutdown or drain begins."""
        if poll_interval <= timedelta(0):
            raise ValueError("worker poll interval must be positive")
        while True:
            with self._condition:
                if self._closed or self._draining:
                    return
            if self.run_once(consumer, lane=lane) is None:
                time.sleep(poll_interval.total_seconds())

    def _admit(self, *, lane: str | None) -> QueueReservation | None:
        with self._condition:
            if self._closed:
                raise QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="queue.worker.admission",
                )
            now = self._clock()
            self._check_idle_timeout(now)
            self._update_heartbeat(now)
            if self._draining or len(self._active_jobs) >= self._max_concurrency:
                return None

            reservation = self._driver.reserve(lane=lane)
            if reservation is None:
                return None
            message_id = reservation.job.envelope.metadata.message_id
            if message_id in self._active_jobs:
                self._driver.retry(reservation)
                self._stats = replace(self._stats, retries=self._stats.retries + 1)
                return None

            self._active_jobs.add(message_id)
            self._active_threads.add(get_ident())
            self._state = WorkerState.PROCESSING
            saturation = len(self._active_jobs) / self._max_concurrency
            occurred_at = reservation.job.envelope.metadata.occurred_at
            lag = max(timedelta(0), reservation.reserved_at - occurred_at)
            self._stats = replace(
                self._stats,
                lag=max(self._stats.lag, lag),
                saturation=max(self._stats.saturation, saturation),
            )
            return reservation

    def _settle_failure(self, reservation: QueueReservation, error: Exception) -> None:
        try:
            if reservation.attempt < reservation.job.max_attempts:
                self._driver.retry(reservation)
                self._increment("retries")
            else:
                self._driver.reject(reservation)
        except Exception as settlement_error:
            raise settlement_error from error

    def _release_cancelled(
        self,
        reservation: QueueReservation,
        cancellation: BaseException,
    ) -> None:
        try:
            self._driver.retry(reservation)
        except Exception as settlement_error:
            raise BaseExceptionGroup(
                "queue cancellation and release both failed",
                (cancellation, settlement_error),
            ) from None

    def _release_uncertain(
        self,
        reservation: QueueReservation,
        acknowledgement_error: BaseException,
    ) -> None:
        try:
            self._driver.retry(reservation)
        except Exception as settlement_error:
            raise BaseExceptionGroup(
                "queue acknowledgement and release both failed",
                (acknowledgement_error, settlement_error),
            ) from None

    def _increment(self, field: str) -> None:
        with self._condition:
            self._stats = replace(
                self._stats,
                **{field: getattr(self._stats, field) + 1},
            )

    def _is_closed(self) -> bool:
        return self._closed

    def _update_heartbeat(self, now: datetime) -> None:
        if self._last_heartbeat is None or now - self._last_heartbeat >= self._heartbeat_interval:
            self._last_heartbeat = now

    def _check_idle_timeout(self, now: datetime) -> None:
        if self._max_idle_time is None or self._active_jobs:
            return
        if self._last_activity is None:
            self._last_activity = now
            return
        if now - self._last_activity > self._max_idle_time:
            self._stats = replace(
                self._stats,
                idle_timeouts=self._stats.idle_timeouts + 1,
            )
            self._last_activity = now
