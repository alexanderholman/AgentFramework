from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from threading import Lock
from typing import Callable

from af.queue.contracts import QueueJob, QueueReservation
from af.queue.manager import QueueDriver
from af.testing.fakes import FakeClock


class WorkerState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True)
class WorkerStats:
    processed: int = 0
    failed: int = 0
    in_flight: int = 0
    retries: int = 0


Handler = Callable[[QueueJob], None]


class WorkerKernel:
    """Deterministic queue worker kernel with tick-driven processing.

    The kernel reserves jobs from a QueueDriver, executes them through a
    handler, and acknowledges or rejects the reservation.  Processing is
    driven by explicit `tick()` calls so that tests and supervisors can
    control the event loop without real threads or sleeps.
    """

    def __init__(
        self,
        driver: QueueDriver,
        handler: Handler | None = None,
        *,
        clock: FakeClock | Callable[[], object] | None = None,
        poll_interval: timedelta = timedelta(seconds=1),
        max_concurrency: int = 10,
    ) -> None:
        self._driver = driver
        self._handler: Handler | None = handler
        self._clock = clock
        self._poll_interval = poll_interval
        self._max_concurrency = max_concurrency

        self._state: WorkerState = WorkerState.NEW
        self._lock = Lock()
        self._processed = 0
        self._failed = 0
        self._retries = 0
        self._in_flight: set[str] = set()
        self._shut_down = False

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    @property
    def state(self) -> WorkerState:
        return self._state

    def boot(self) -> None:
        with self._lock:
            if self._shut_down:
                raise RuntimeError("worker is already shut down")
            self._state = WorkerState.RUNNING

    def shutdown(self) -> None:
        with self._lock:
            self._shut_down = True
            self._state = WorkerState.STOPPED

    def tick(self) -> None:
        """Process up to max_concurrency jobs (synchronously)."""
        with self._lock:
            if self._state != WorkerState.RUNNING:
                return

        attempted: set[str] = set()
        for _ in range(self._max_concurrency):
            reservation = self._driver.reserve(self._poll_interval)
            if reservation is None:
                break
            job_id = reservation.job.envelope.metadata.message_id
            if job_id in attempted:
                self._driver.reject(reservation, requeue=False)
                break
            attempted.add(job_id)
            self._process_reservation(reservation)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def stats(self) -> WorkerStats:
        with self._lock:
            return WorkerStats(
                processed=self._processed,
                failed=self._failed,
                in_flight=len(self._in_flight),
                retries=self._retries,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_reservation(self, reservation: QueueReservation) -> None:
        job_id = reservation.job.envelope.metadata.message_id

        with self._lock:
            self._in_flight.add(job_id)

        try:
            if self._handler is not None:
                self._handler(reservation.job)
            self._driver.ack(reservation)
            with self._lock:
                self._processed += 1
        except Exception:
            self._driver.reject(reservation, requeue=True)
            with self._lock:
                self._failed += 1
        finally:
            with self._lock:
                self._in_flight.discard(job_id)
