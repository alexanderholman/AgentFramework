"""Reusable conformance cases for queue drivers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from af.messaging.envelopes import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue.contracts import QueueJob, QueueOperationError, UnsupportedQueueFeature
from af.queue.manager import QueueDriver, QueueFeature
from af.testing.conformance import ConformanceCase, DriverConformanceSuite

__all__ = ("QueueDriverConformanceSuite", "queue_driver_cases")

_NOW = datetime(2026, 8, 1, 15, tzinfo=UTC)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _job(
    message_id: str,
    *,
    lane: str = "default",
    delay: timedelta | None = None,
    priority: int | None = None,
    max_attempts: int = 1,
    backoff: timedelta | None = None,
    timeout: timedelta | None = None,
    unique_key: str | None = None,
    fifo_group: str | None = None,
    requires_transaction: bool = False,
) -> QueueJob:
    return QueueJob(
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW,
            ),
            payload={"message_id": message_id, "secret": "queue-payload"},
        ),
        lane=lane,
        delay=delay,
        priority=priority,
        max_attempts=max_attempts,
        backoff=backoff,
        timeout=timeout,
        unique_key=unique_key,
        fifo_group=fifo_group,
        requires_transaction=requires_transaction,
    )


def _assert_enqueue_reserve_ack(driver: QueueDriver) -> None:
    receipt = driver.dispatch(_job("job-ack"))
    reservation = driver.reserve()
    _check(receipt.message_id == "job-ack", "queue driver returned the wrong dispatch receipt")
    _check(reservation is not None, "queue driver did not reserve an enqueued job")
    if reservation is None:
        return
    _check(
        reservation.job.envelope.metadata.message_id == "job-ack",
        "queue driver reserved the wrong job",
    )
    driver.ack(reservation)
    _check(driver.reserve() is None, "queue driver left an acknowledged job visible")


def _assert_lane_isolation(driver: QueueDriver) -> None:
    driver.dispatch(_job("job-default"))
    driver.dispatch(_job("job-critical", lane="critical"))
    reservation = driver.reserve(lane="critical")
    _check(reservation is not None, "queue driver did not route lane-specific work")
    if reservation is None:
        return
    _check(reservation.job.lane == "critical", "queue driver reserved the wrong lane")
    driver.ack(reservation)
    default = driver.reserve(lane="default")
    _check(default is not None, "queue driver lost the default lane job")
    if default is not None:
        driver.ack(default)


def _assert_retry_redelivery(driver: QueueDriver) -> None:
    driver.dispatch(_job("job-retry", max_attempts=3, backoff=timedelta(seconds=5)))
    first = driver.reserve()
    _check(first is not None, "queue driver did not reserve a retryable job")
    if first is None:
        return
    driver.retry(first, available_at=first.reserved_at)
    second = driver.reserve()
    _check(second is not None, "queue driver did not redeliver a retried job")
    if second is None:
        return
    _check(second.attempt == 2, "queue driver did not increment the retry attempt")
    driver.ack(second)


def _assert_retry_exhaustion(driver: QueueDriver) -> None:
    driver.dispatch(_job("job-exhausted", max_attempts=2, backoff=timedelta(seconds=1)))
    first = driver.reserve()
    _check(first is not None, "queue driver did not reserve a bounded retry job")
    if first is None:
        return
    driver.retry(first, available_at=first.reserved_at)
    second = driver.reserve()
    _check(second is not None, "queue driver did not redeliver the final retry")
    if second is None:
        return
    driver.retry(second, available_at=second.reserved_at)
    _check(driver.reserve() is None, "queue driver did not dead-letter the exhausted job")


def _supports(driver: QueueDriver, feature: QueueFeature) -> bool:
    supports = getattr(driver, "supports", None)
    return callable(supports) and supports(feature) is True


def _assert_delay(driver: QueueDriver) -> None:
    if not _supports(driver, QueueFeature.DELAY):
        return
    job = _job("job-delayed", lane="delayed", delay=timedelta(days=1))
    receipt = driver.dispatch(job)
    _check(receipt.message_id == "job-delayed", "queue delay returned the wrong receipt")
    _check(driver.reserve(lane="delayed") is None, "queue delay made future work eligible")


def _assert_priority(driver: QueueDriver) -> None:
    if not _supports(driver, QueueFeature.PRIORITY):
        return
    driver.dispatch(_job("job-low", lane="priority", priority=-1))
    driver.dispatch(_job("job-high", lane="priority", priority=10))
    for expected in ("job-high", "job-low"):
        reservation = driver.reserve(lane="priority")
        _check(reservation is not None, "queue priority lost accepted work")
        if reservation is not None:
            _check(
                reservation.job.envelope.metadata.message_id == expected,
                "queue priority reserved the wrong job",
            )
            driver.ack(reservation)


def _assert_unsupported_features(driver: QueueDriver) -> None:
    for feature, job in (
        (QueueFeature.DELAY, _job("job-delay", delay=timedelta(seconds=1))),
        (QueueFeature.PRIORITY, _job("job-priority", priority=10)),
        (QueueFeature.TIMEOUT, _job("job-timeout", timeout=timedelta(seconds=30))),
        (QueueFeature.UNIQUENESS, _job("job-unique", unique_key="job-unique")),
        (QueueFeature.FIFO, _job("job-fifo", fifo_group="orders")),
        (QueueFeature.TRANSACTIONAL_DISPATCH, _job("job-transaction", requires_transaction=True)),
    ):
        if _supports(driver, feature):
            continue
        captured = None
        try:
            driver.dispatch(job)
        except UnsupportedQueueFeature as error:
            captured = error
        _check(captured is not None, "queue driver silently accepted an unsupported feature")


def _assert_shutdown(driver: QueueDriver) -> None:
    driver.close()
    driver.close()
    captured: QueueOperationError | None = None
    try:
        driver.dispatch(_job("job-after-close"))
    except QueueOperationError as error:
        captured = error
    _check(captured is not None, "closed queue driver accepted a new dispatch")


def queue_driver_cases() -> tuple[ConformanceCase[QueueDriver], ...]:
    """Return the shared, driver-neutral queue conformance cases."""

    return (
        ConformanceCase("enqueue-reserve-ack", _assert_enqueue_reserve_ack),
        ConformanceCase("lane-isolation", _assert_lane_isolation),
        ConformanceCase("retry-redelivery", _assert_retry_redelivery),
        ConformanceCase("retry-exhaustion", _assert_retry_exhaustion),
        ConformanceCase("unsupported-features", _assert_unsupported_features),
        ConformanceCase("delay", _assert_delay),
        ConformanceCase("priority", _assert_priority),
        ConformanceCase("shutdown", _assert_shutdown),
    )


class QueueDriverConformanceSuite(DriverConformanceSuite[QueueDriver]):
    """Certify one queue driver against the shared AF contract."""

    def __init__(self, name: str = "queue-driver") -> None:
        super().__init__(name, queue_driver_cases())
