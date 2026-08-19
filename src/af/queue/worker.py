"""Synchronous queue worker lifecycle with explicit reliability boundaries."""

from __future__ import annotations

import inspect
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Condition, Event, RLock, Thread, get_ident
from types import TracebackType
from typing import Protocol, cast

from af.framework.context import CancellationToken
from af.framework.errors import PlatformReason
from af.observability.signals import (
    CapabilityState,
    DegradationState,
    HealthState,
    LivenessState,
    ReadinessState,
    TelemetryKind,
    TelemetryPublisher,
    TelemetrySignal,
)
from af.queue.contracts import (
    QueueJob,
    QueueOperationError,
    QueueReservation,
    QueueReservationLost,
)
from af.queue.manager import QueueDriver, QueueFeature


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


class CooperativeJobHandler(Protocol):
    """Synchronous handler that accepts one worker-owned cancellation token."""

    cooperative_cancellation: bool

    def __call__(self, job: QueueJob) -> None: ...

    def invoke(
        self,
        job: QueueJob,
        /,
        *,
        cancellation: CancellationToken,
    ) -> None: ...


class _RenewalDriver(Protocol):
    def renew(self, reservation: QueueReservation, /) -> QueueReservation: ...


class IdempotencyStore(Protocol):
    """Post-success duplicate-suppression boundary for retry-safe handlers."""

    def claim(self, message_id: str, /) -> AbstractContextManager[IdempotencyClaim]: ...


class IdempotencyClaim(Protocol):
    """Claim held while one delivery checks and records post-scope success."""

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


class WorkerAdmissionGate(Protocol):
    """Fail-closed host-resource admission before a queue reservation is acquired."""

    def __call__(
        self,
        *,
        lane: str | None,
        active_jobs: int,
        max_concurrency: int,
    ) -> bool | WorkerAdmissionPermit: ...


class WorkerAdmissionPermit(Protocol):
    """One acquired host-resource capability held for a delivery lifecycle."""

    def release(self) -> None: ...


class WorkerConnectionRecycler(Protocol):
    """Post-settlement cleanup for worker-owned backend connections."""

    def recycle(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _AdmittedDelivery:
    reservation: QueueReservation
    permit: WorkerAdmissionPermit | None = None


class _SettledJobFailure(Exception):
    """Handler exception that was already settled (retried/rejected)."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


class _AcknowledgementFailure(Exception):
    """Infrastructure acknowledgement failure that must propagate."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


class _CooperativeCancellation(BaseException):
    """Expected worker-owned cancellation after cooperative execution exits."""


@dataclass(slots=True)
class _DeliveryObservation:
    """Low-cardinality terminal state for one admitted delivery."""

    outcome: str = "settlement_failed"
    reason: str = "execution_failure"
    settlement: str = "failed"
    phase: str = "execution"
    attempt_bucket: str = "first"
    renewal_count_bucket: str = "none"
    renewal_outcome: str = "not_required"


class _RenewalCoordinator:
    """Serialize lease replacement before the delivery's terminal settlement."""

    def __init__(
        self,
        driver: QueueDriver,
        reservation: QueueReservation,
        *,
        clock: Callable[[], datetime],
        minimum_interval: timedelta,
        maximum_interval: timedelta,
    ) -> None:
        self._driver = driver
        self._current = reservation
        self._clock = clock
        self._minimum_interval = minimum_interval
        self._maximum_interval = maximum_interval
        self._stop = Event()
        self._ready = Event()
        self._lock = RLock()
        self._failure: BaseException | None = None
        self._renewals = 0
        self._thread: Thread | None = None
        if reservation.expires_at is None:
            return
        if not callable(getattr(driver, "renew", None)):
            raise QueueOperationError(
                reason=PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="queue.worker.renewal",
            )
        supports = getattr(driver, "supports", None)
        if callable(supports) and not supports(QueueFeature.RESERVATION_RENEWAL):
            raise QueueOperationError(
                reason=PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="queue.worker.renewal",
            )
        remaining = reservation.expires_at - clock()
        if remaining <= minimum_interval * 2:
            raise QueueOperationError(
                reason=PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="queue.worker.renewal",
            )
        self._thread = Thread(target=self._run, name="af-queue-renewal", daemon=True)
        self._thread.start()
        self._ready.wait()
        with self._lock:
            if self._failure is not None:
                raise self._failure

    @property
    def required(self) -> bool:
        return self._thread is not None

    @property
    def renewal_count(self) -> int:
        with self._lock:
            return self._renewals

    def finish(self) -> QueueReservation:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join()
        with self._lock:
            if self._failure is not None:
                raise self._failure
            return self._current

    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    expires_at = self._current.expires_at
                assert expires_at is not None
                remaining = expires_at - self._clock()
                if remaining <= self._minimum_interval * 2:
                    raise QueueOperationError(
                        reason=PlatformReason.CAPABILITY_UNAVAILABLE,
                        stage="queue.worker.renewal",
                    )
                delay = min(self._maximum_interval, remaining / 2)
                self._ready.set()
                if self._stop.wait(timeout=delay.total_seconds()):
                    return
                with self._lock:
                    if self._stop.is_set():
                        return
                    renewer = cast(_RenewalDriver, self._driver)
                    renewed = renewer.renew(self._current)
                    if not self._valid_renewal(self._current, renewed):
                        raise QueueReservationLost(stage="queue.worker.renewal")
                    self._current = renewed
                    self._renewals += 1
        except BaseException as error:
            with self._lock:
                self._failure = error
            self._ready.set()

    def _valid_renewal(self, original: QueueReservation, renewed: object) -> bool:
        return (
            isinstance(renewed, QueueReservation)
            and original.expires_at is not None
            and renewed.expires_at is not None
            and renewed.expires_at > original.expires_at
            and renewed.expires_at - self._clock() > self._minimum_interval * 2
            and original.reservation_id is not None
            and renewed.reservation_id is not None
            and renewed.reservation_id != original.reservation_id
            and renewed.job == original.job
            and renewed.attempt == original.attempt
            and renewed.reserved_at == original.reserved_at
            and renewed.fencing_token == original.fencing_token
            and renewed.owner_id == original.owner_id
        )


def _duration_bucket(duration: timedelta) -> str:
    if duration < timedelta(milliseconds=10):
        return "lt_10ms"
    if duration < timedelta(milliseconds=100):
        return "lt_100ms"
    if duration < timedelta(seconds=1):
        return "lt_1s"
    if duration < timedelta(seconds=10):
        return "lt_10s"
    if duration < timedelta(minutes=1):
        return "lt_1m"
    if duration < timedelta(minutes=5):
        return "lt_5m"
    return "gte_5m"


def _attempt_bucket(reservation: QueueReservation) -> str:
    if reservation.attempt == 1:
        return "first"
    return "retry"


def _renewal_count_bucket(count: int) -> str:
    if count == 0:
        return "none"
    if count == 1:
        return "one"
    return "multiple"


def _saturation_bucket(saturation: float) -> str:
    if saturation < 0.5:
        return "lt_50pct"
    if saturation < 0.8:
        return "lt_80pct"
    if saturation < 1.0:
        return "lt_100pct"
    return "full"


def _validate_handler(consumer: object) -> None:
    if not callable(consumer):
        raise TypeError("sync queue worker handler must be callable")
    if inspect.iscoroutinefunction(consumer) or inspect.isasyncgenfunction(consumer):
        raise TypeError("sync queue worker returned an asynchronous result")
    call = inspect.getattr_static(consumer, "__call__", None)
    if inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call):
        raise TypeError("sync queue worker returned an asynchronous result")


def _resolve_cooperative_handler(consumer: object) -> CooperativeJobHandler | None:
    if getattr(consumer, "cooperative_cancellation", None) is not True:
        return None
    invoke = getattr(consumer, "invoke", None)
    if not callable(invoke):
        raise TypeError("cooperative queue handler requires callable invoke")
    if inspect.iscoroutinefunction(invoke) or inspect.isasyncgenfunction(invoke):
        raise TypeError("sync queue worker returned an asynchronous result")
    try:
        inspect.signature(invoke).bind(object(), cancellation=CancellationToken())
    except (TypeError, ValueError) as error:
        raise TypeError("invalid cooperative signature") from error
    return cast(CooperativeJobHandler, consumer)


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

    Driver operations are synchronous, operationally bounded trust boundaries. A close
    timeout bounds waiting for admitted work but cannot preempt a blocked driver call,
    including the driver's own ``close`` operation.

    Durable exactly-once business effects are deliberately not claimed. An injected
    ``IdempotencyStore`` records only after the job scope exits successfully and before the
    queue acknowledgement. If recording fails, the delivery is retried and handler effects
    may repeat. Handlers requiring stronger guarantees must provide their own transactional
    inbox/outbox or idempotent business operation.
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
        admission_gate: WorkerAdmissionGate | None = None,
        connection_recycler: WorkerConnectionRecycler | None = None,
        driver_close: Callable[[], object] | None = None,
        telemetry: TelemetryPublisher | None = None,
        queue_wait_alert_after: timedelta | None = None,
        renewal_min_interval: timedelta = timedelta(milliseconds=100),
        renewal_max_interval: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("worker maximum concurrency must be positive")
        if heartbeat_interval <= timedelta(0):
            raise ValueError("worker heartbeat interval must be positive")
        if max_idle_time is not None and max_idle_time <= timedelta(0):
            raise ValueError("worker maximum idle time must be positive")
        for method in ("reserve", "ack", "reject", "release", "retry", "close"):
            if not callable(getattr(driver, method, None)):
                raise TypeError(f"worker driver requires callable {method}")
        if idempotency is not None and not callable(getattr(idempotency, "claim", None)):
            raise TypeError("worker idempotency store requires callable claim")
        if scope_factory is not None and not callable(scope_factory):
            raise TypeError("worker scope factory must be callable")
        if admission_gate is not None and not callable(admission_gate):
            raise TypeError("worker resource admission gate must be callable")
        if connection_recycler is not None:
            recycle = getattr(connection_recycler, "recycle", None)
            if not callable(recycle):
                raise TypeError("worker connection recycler requires callable recycle")
            if inspect.iscoroutinefunction(recycle) or inspect.isasyncgenfunction(recycle):
                raise TypeError("worker connection recycler must be synchronous")
        if driver_close is not None:
            if not callable(driver_close):
                raise TypeError("worker driver close boundary must be callable")
            if inspect.iscoroutinefunction(driver_close) or inspect.isasyncgenfunction(
                driver_close
            ):
                raise TypeError("worker driver close boundary must be synchronous")
        if telemetry is not None and not isinstance(telemetry, TelemetryPublisher):
            raise TypeError("worker telemetry publisher is invalid")
        if queue_wait_alert_after is not None and queue_wait_alert_after <= timedelta(0):
            raise ValueError("worker queue wait alert threshold must be positive")
        if renewal_min_interval <= timedelta(0):
            raise ValueError("worker renewal minimum interval must be positive")
        if renewal_max_interval < renewal_min_interval:
            raise ValueError("worker renewal maximum interval must not precede its minimum")
        if not callable(clock):
            raise TypeError("worker clock must be callable")

        self._driver = driver
        self._max_concurrency = max_concurrency
        self._heartbeat_interval = heartbeat_interval
        self._max_idle_time = max_idle_time
        self._idempotency = idempotency
        self._scope_factory = scope_factory or (lambda job: nullcontext())
        self._admission_gate = admission_gate
        self._connection_recycler = connection_recycler
        self._driver_close = driver.close if driver_close is None else driver_close
        self._telemetry = telemetry
        self._queue_wait_alert_after = queue_wait_alert_after
        self._renewal_min_interval = renewal_min_interval
        self._renewal_max_interval = renewal_max_interval
        self._clock = clock

        self._state = WorkerState.IDLE
        self._closed = False
        self._closing = False
        self._draining = False
        self._active_jobs: set[str] = set()
        self._active_cancellations: dict[str, CancellationToken] = {}
        self._active_threads: set[int] = set()
        self._last_heartbeat: datetime | None = None
        self._last_activity: datetime | None = None
        self._queue_wait_alert_active = False
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
    def max_concurrency(self) -> int:
        """Maximum number of deliveries admitted concurrently."""
        return self._max_concurrency

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

    def cancel_active(self) -> None:
        """Stop intake, then ask active cooperative handlers to exit."""
        with self._condition:
            self._draining = True
            for cancellation in self._active_cancellations.values():
                cancellation.cancel()
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
            result = self._driver_close()
            if inspect.iscoroutine(result):
                result.close()
                raise TypeError("worker driver close boundary returned an asynchronous result")
            if inspect.isawaitable(result) or inspect.isasyncgen(result):
                raise TypeError("worker driver close boundary returned an asynchronous result")
            if result is not None:
                raise TypeError("worker driver close boundary must return None")
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
        """Admit and synchronously execute at most one job.

        A settled handler failure is re-raised to direct callers. Infrastructure and
        settlement failures are always propagated.
        """
        _validate_handler(consumer)
        try:
            return self._run_once(consumer, lane=lane)
        except _SettledJobFailure as failure:
            raise failure.error.with_traceback(failure.error.__traceback__) from None
        except _AcknowledgementFailure as failure:
            raise failure.error.with_traceback(failure.error.__traceback__) from None

    def _run_once(
        self,
        consumer: JobHandler,
        *,
        lane: str | None = None,
    ) -> QueueReservation | None:
        cooperative_handler = _resolve_cooperative_handler(consumer)
        admitted = self._admit(lane=lane)
        if admitted is None:
            return None
        reservation = admitted.reservation
        permit = admitted.permit

        message_id = reservation.job.envelope.metadata.message_id
        with self._condition:
            cancellation = self._active_cancellations[message_id]
        started_monotonic = time.monotonic()
        lag = max(
            timedelta(0),
            reservation.reserved_at - reservation.job.envelope.metadata.occurred_at,
        )
        saturation = len(self._active_jobs) / self._max_concurrency
        observation = _DeliveryObservation(attempt_bucket=_attempt_bucket(reservation))
        try:
            try:
                renewal = _RenewalCoordinator(
                    self._driver,
                    reservation,
                    clock=self._clock,
                    minimum_interval=self._renewal_min_interval,
                    maximum_interval=self._renewal_max_interval,
                )
            except BaseException as renewal_error:
                observation.phase = "renewal"
                observation.reason = "renewal_failure"
                observation.renewal_outcome = "failed"
                if not isinstance(renewal_error, QueueReservationLost):
                    try:
                        self._driver.release(reservation)
                        observation.outcome = "released"
                        observation.settlement = "release"
                    except BaseException as release_error:
                        raise BaseExceptionGroup(
                            "queue renewal admission and release both failed",
                            (renewal_error, release_error),
                        ) from None
                raise _AcknowledgementFailure(renewal_error) from renewal_error
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
                            if cooperative_handler is not None and cancellation.cancelled:
                                result = None
                            elif cooperative_handler is not None:
                                cooperative_invoke = cast(
                                    Callable[..., object],
                                    cooperative_handler.invoke,
                                )
                                result = cooperative_invoke(
                                    reservation.job,
                                    cancellation=cancellation,
                                )
                            else:
                                sync_consumer = cast(Callable[[QueueJob], object], consumer)
                                result = sync_consumer(reservation.job)
                            _validate_sync_result(result)
                        with self._condition:
                            cooperative_cancelled = (
                                cooperative_handler is not None and cancellation.cancelled
                            )
                            self._active_cancellations.pop(message_id, None)
                        if cooperative_cancelled:
                            raise _CooperativeCancellation
                        claim.record()
            except _CooperativeCancellation as cooperative_cancellation:
                self._increment("cancellations")
                try:
                    reservation = renewal.finish()
                    self._observe_renewal(observation, renewal)
                except BaseException as renewal_error:
                    self._observe_renewal_failure(observation, renewal, renewal_error)
                    raise BaseExceptionGroup(
                        "queue cancellation and renewal both failed",
                        (cooperative_cancellation, renewal_error),
                    ) from None
                observation.reason = "cancellation"
                self._release_cancelled(reservation, cooperative_cancellation)
                observation.outcome = "released"
                observation.settlement = "release"
                return reservation
            except Exception as error:
                with self._condition:
                    self._active_cancellations.pop(message_id, None)
                self._increment("jobs_failed")
                try:
                    reservation = renewal.finish()
                    self._observe_renewal(observation, renewal)
                except BaseException as renewal_error:
                    self._observe_renewal_failure(observation, renewal, renewal_error)
                    raise BaseExceptionGroup(
                        "queue handler and renewal both failed",
                        (error, renewal_error),
                    ) from None
                if reservation.attempt >= reservation.job.max_attempts:
                    observation.attempt_bucket = "exhausted"
                settlement = self._settle_failure(reservation, error)
                observation.outcome = "retried" if settlement == "retry" else "rejected"
                observation.settlement = settlement
                raise _SettledJobFailure(error) from error
            except BaseException as cancellation:
                with self._condition:
                    self._active_cancellations.pop(message_id, None)
                self._increment("cancellations")
                try:
                    reservation = renewal.finish()
                    self._observe_renewal(observation, renewal)
                except BaseException as renewal_error:
                    self._observe_renewal_failure(observation, renewal, renewal_error)
                    raise BaseExceptionGroup(
                        "queue cancellation and renewal both failed",
                        (cancellation, renewal_error),
                    ) from None
                observation.reason = "cancellation"
                self._release_cancelled(reservation, cancellation)
                observation.outcome = "released"
                observation.settlement = "release"
                raise

            try:
                reservation = renewal.finish()
                self._observe_renewal(observation, renewal)
            except BaseException as renewal_error:
                self._increment("jobs_failed")
                self._observe_renewal_failure(observation, renewal, renewal_error)
                raise _AcknowledgementFailure(renewal_error) from renewal_error
            observation.phase = "ack"
            observation.reason = "acknowledgement_failure"
            try:
                self._driver.ack(reservation)
            except BaseException as acknowledgement_error:
                observation.reason = (
                    "acknowledgement_failure"
                    if isinstance(acknowledgement_error, Exception)
                    else "cancellation"
                )
                self._increment(
                    "jobs_failed"
                    if isinstance(acknowledgement_error, Exception)
                    else "cancellations"
                )
                if isinstance(acknowledgement_error, QueueReservationLost):
                    observation.reason = "reservation_lost"
                    raise _AcknowledgementFailure(acknowledgement_error) from acknowledgement_error
                self._release_uncertain(reservation, acknowledgement_error)
                observation.outcome = "released"
                observation.settlement = "release"
                if isinstance(acknowledgement_error, Exception):
                    raise _AcknowledgementFailure(acknowledgement_error) from acknowledgement_error
                raise
            observation.outcome = "duplicate" if duplicate else "processed"
            observation.reason = "duplicate" if duplicate else "success"
            observation.settlement = "ack"
            self._increment("duplicates" if duplicate else "jobs_processed")
            return reservation
        finally:
            active_failure = sys.exception()
            finalization_failure: BaseException | None = None
            try:
                self._finalize_delivery_resources(permit, active_failure=active_failure)
            except BaseException as error:
                finalization_failure = error
            finished_at = self._clock()
            elapsed = timedelta(seconds=time.monotonic() - started_monotonic)
            with self._condition:
                self._stats = replace(
                    self._stats,
                    total_runtime=self._stats.total_runtime + elapsed,
                )
                self._active_cancellations.pop(message_id, None)
                self._active_jobs.discard(message_id)
                self._active_threads.discard(get_ident())
                self._last_activity = finished_at
                self._state = WorkerState.PROCESSING if self._active_jobs else WorkerState.IDLE
                self._condition.notify_all()
            # Optional telemetry construction cannot change queue settlement semantics.
            with suppress(Exception):
                self._emit_delivery_telemetry(
                    reservation,
                    observation=observation,
                    observed_at=finished_at,
                    runtime=elapsed,
                    lag=lag,
                    saturation=saturation,
                )
            if finalization_failure is not None:
                raise finalization_failure.with_traceback(finalization_failure.__traceback__)

    def run_loop(
        self,
        consumer: JobHandler,
        *,
        lane: str | None = None,
        poll_interval: timedelta = timedelta(milliseconds=100),
        stop_event: Event | None = None,
    ) -> None:
        """Poll serially until shutdown or drain begins.

        Ordinary consumer failures that were successfully settled allow the loop
        to continue. Infrastructure acknowledgement failures propagate immediately.
        """
        if poll_interval <= timedelta(0):
            raise ValueError("worker poll interval must be positive")
        _validate_handler(consumer)
        heartbeat_stop = Event()
        heartbeat_thread: Thread | None = None
        if self._telemetry is not None:
            heartbeat_thread = Thread(
                target=self._run_heartbeat,
                args=(heartbeat_stop,),
                name="af-queue-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                with self._condition:
                    if self._closed or self._draining:
                        return
                try:
                    reservation = self._run_once(consumer, lane=lane)
                except _AcknowledgementFailure as failure:
                    raise failure.error.with_traceback(failure.error.__traceback__) from None
                except _SettledJobFailure:
                    continue
                if reservation is None:
                    if stop_event is None:
                        time.sleep(poll_interval.total_seconds())
                    elif stop_event.wait(timeout=poll_interval.total_seconds()):
                        return
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join()
                with suppress(Exception):
                    self._emit_heartbeat(stopped=True)

    def _admit(self, *, lane: str | None) -> _AdmittedDelivery | None:
        active_jobs = 0
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
            active_jobs = len(self._active_jobs)

        admission_gate = self._admission_gate
        permit: WorkerAdmissionPermit | None = None
        if admission_gate is not None:
            decision = admission_gate(
                lane=lane,
                active_jobs=active_jobs,
                max_concurrency=self._max_concurrency,
            )
            if type(decision) is bool:
                if not decision:
                    return None
            else:
                release = getattr(decision, "release", None)
                if not callable(release):
                    raise TypeError("worker admission permit requires callable release")
                if inspect.iscoroutinefunction(release) or inspect.isasyncgenfunction(release):
                    raise TypeError("worker admission permit must be synchronous")
                permit = decision

        with self._condition:
            if self._is_closed():
                error = QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="queue.worker.admission",
                )
                self._release_admission_permit(permit, active_failure=error)
                raise error
            if self._draining or len(self._active_jobs) >= self._max_concurrency:
                self._release_admission_permit(permit)
                return None
            try:
                reservation = self._driver.reserve(lane=lane)
            except BaseException as error:
                self._release_admission_permit(permit, active_failure=error)
                raise
            if reservation is None:
                self._release_admission_permit(permit)
                return None
            message_id = reservation.job.envelope.metadata.message_id
            if message_id in self._active_jobs:
                try:
                    self._driver.retry(reservation)
                except BaseException as error:
                    self._release_admission_permit(permit, active_failure=error)
                    raise
                self._release_admission_permit(permit)
                self._stats = replace(self._stats, retries=self._stats.retries + 1)
                return None

            self._active_jobs.add(message_id)
            self._active_cancellations[message_id] = CancellationToken()
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
            return _AdmittedDelivery(reservation=reservation, permit=permit)

    @staticmethod
    def _release_admission_permit(
        permit: WorkerAdmissionPermit | None,
        *,
        active_failure: BaseException | None = None,
    ) -> None:
        if permit is None:
            return
        try:
            release = cast(Callable[[], object], permit.release)
            result = release()
            if inspect.iscoroutine(result):
                result.close()
                raise TypeError("worker admission permit returned an asynchronous result")
            if inspect.isawaitable(result) or inspect.isasyncgen(result):
                raise TypeError("worker admission permit returned an asynchronous result")
            if result is not None:
                raise TypeError("worker admission permit must return None")
        except BaseException as release_failure:
            if active_failure is not None:
                raise BaseExceptionGroup(
                    "queue delivery and resource permit release both failed",
                    (active_failure, release_failure),
                ) from None
            raise

    def _finalize_delivery_resources(
        self,
        permit: WorkerAdmissionPermit | None,
        *,
        active_failure: BaseException | None,
    ) -> None:
        cleanup_failures: list[BaseException] = []
        recycler = self._connection_recycler
        if recycler is not None:
            try:
                recycle = cast(Callable[[], object], recycler.recycle)
                result = recycle()
                if inspect.iscoroutine(result):
                    result.close()
                    raise TypeError("worker connection recycler returned an asynchronous result")
                if inspect.isawaitable(result) or inspect.isasyncgen(result):
                    raise TypeError("worker connection recycler returned an asynchronous result")
                if result is not None:
                    raise TypeError("worker connection recycler must return None")
            except BaseException as recycle_failure:
                cleanup_failures.append(recycle_failure)
        try:
            self._release_admission_permit(permit)
        except BaseException as release_failure:
            cleanup_failures.append(release_failure)
        if not cleanup_failures:
            return
        exposed_failure = active_failure
        if isinstance(active_failure, (_SettledJobFailure, _AcknowledgementFailure)):
            exposed_failure = active_failure.error
        failures = (
            tuple(cleanup_failures)
            if exposed_failure is None
            else (exposed_failure, *cleanup_failures)
        )
        if len(failures) == 1:
            failure = failures[0]
            raise failure.with_traceback(failure.__traceback__)
        raise BaseExceptionGroup(
            "queue delivery and resource cleanup failed",
            failures,
        ) from None

    def _settle_failure(self, reservation: QueueReservation, error: Exception) -> str:
        try:
            if reservation.attempt < reservation.job.max_attempts:
                self._driver.retry(reservation)
                self._increment("retries")
                return "retry"
            else:
                self._driver.reject(reservation)
                return "reject"
        except Exception as settlement_error:
            raise settlement_error from error

    def _emit_delivery_telemetry(
        self,
        reservation: QueueReservation,
        *,
        observation: _DeliveryObservation,
        observed_at: datetime,
        runtime: timedelta,
        lag: timedelta,
        saturation: float,
    ) -> None:
        publisher = self._telemetry
        if publisher is None:
            return
        context = reservation.job.envelope.metadata.telemetry
        self._publish_telemetry(
            TelemetrySignal.create(
                kind=TelemetryKind.METRIC,
                name="queue.worker.delivery",
                observed_at=observed_at,
                context=context,
                attributes={
                    "attempt_bucket": observation.attempt_bucket,
                    "lag_bucket": _duration_bucket(lag),
                    "outcome": observation.outcome,
                    "phase": observation.phase,
                    "reason": observation.reason,
                    "renewal_count_bucket": observation.renewal_count_bucket,
                    "renewal_outcome": observation.renewal_outcome,
                    "runtime_bucket": _duration_bucket(runtime),
                    "saturation_bucket": _saturation_bucket(saturation),
                    "settlement": observation.settlement,
                },
            )
        )
        threshold = self._queue_wait_alert_after
        if threshold is None:
            return
        exceeded = lag > threshold
        with self._condition:
            if exceeded == self._queue_wait_alert_active:
                return
            self._queue_wait_alert_active = exceeded
        self._publish_telemetry(
            TelemetrySignal.create(
                kind=TelemetryKind.LOG,
                name="queue.worker.wait_alert",
                observed_at=observed_at,
                context=context,
                attributes={
                    "lag_bucket": _duration_bucket(lag),
                    "status": "exceeded" if exceeded else "recovered",
                    "threshold_bucket": _duration_bucket(threshold),
                },
            )
        )

    def _publish_telemetry(self, signal: TelemetrySignal) -> None:
        try:
            assert self._telemetry is not None
            self._telemetry.publish(signal)
        except Exception:
            # Optional telemetry evidence cannot change queue settlement semantics.
            return

    def _run_heartbeat(self, stop: Event) -> None:
        with suppress(Exception):
            self._emit_heartbeat(stopped=False)
        while not stop.wait(timeout=self._heartbeat_interval.total_seconds()):
            with suppress(Exception):
                self._emit_heartbeat(stopped=False)

    def _emit_heartbeat(self, *, stopped: bool) -> None:
        observed_at = self._clock()
        with self._condition:
            active_jobs = len(self._active_jobs)
            saturation = active_jobs / self._max_concurrency
            if stopped:
                state = WorkerState.STOPPED
                readiness = ReadinessState.NOT_READY
            elif self._draining:
                state = WorkerState.DRAINING
                readiness = ReadinessState.DRAINING
            elif active_jobs >= self._max_concurrency:
                state = WorkerState.PROCESSING
                readiness = ReadinessState.SATURATED
            elif active_jobs:
                state = WorkerState.PROCESSING
                readiness = ReadinessState.BUSY
            else:
                state = WorkerState.IDLE
                readiness = ReadinessState.READY
        self._publish_telemetry(
            TelemetrySignal.health(
                name="queue.worker.heartbeat",
                observed_at=observed_at,
                attributes={
                    "active_jobs_bucket": (
                        "zero" if active_jobs == 0 else "one" if active_jobs == 1 else "multiple"
                    ),
                    "saturation_bucket": _saturation_bucket(saturation),
                    "state": state.value,
                },
                state=HealthState(
                    liveness=(LivenessState.STOPPED if stopped else LivenessState.LIVE),
                    readiness=readiness,
                    degradation=DegradationState.HEALTHY,
                    capability=CapabilityState.AVAILABLE,
                    reason_code="worker_stopped" if stopped else "worker_alive",
                    fresh_until=(
                        observed_at if stopped else observed_at + (self._heartbeat_interval * 2)
                    ),
                ),
            )
        )

    def _release_cancelled(
        self,
        reservation: QueueReservation,
        cancellation: BaseException,
    ) -> None:
        try:
            self._driver.release(reservation)
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
            self._driver.release(reservation)
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

    @staticmethod
    def _observe_renewal(
        observation: _DeliveryObservation,
        renewal: _RenewalCoordinator,
    ) -> None:
        count = renewal.renewal_count
        if not renewal.required:
            return
        observation.renewal_count_bucket = _renewal_count_bucket(count)
        observation.renewal_outcome = "succeeded" if count else "not_needed"

    @staticmethod
    def _observe_renewal_failure(
        observation: _DeliveryObservation,
        renewal: _RenewalCoordinator,
        error: BaseException,
    ) -> None:
        observation.phase = "renewal"
        observation.reason = (
            "reservation_lost" if isinstance(error, QueueReservationLost) else "renewal_failure"
        )
        observation.outcome = "settlement_failed"
        observation.settlement = "none"
        count = renewal.renewal_count
        observation.renewal_count_bucket = _renewal_count_bucket(count)
        observation.renewal_outcome = (
            "lost" if isinstance(error, QueueReservationLost) else "failed"
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
