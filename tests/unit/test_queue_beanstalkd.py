from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import QueueJob, QueueReservationLost
from af.queue.beanstalkd import BeanstalkdQueueDriver
from af.queue.codec import QueueJobCodec
from af.testing import FakeClock

_NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _job(**options: Any) -> QueueJob:
    return QueueJob(
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id="beanstalk-job",
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW,
            ),
            payload={"work": "execute"},
        ),
        **options,
    )


@dataclass
class Job:
    id: int
    body: bytes


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.next_job: Job | None = None
        self.attempt = 1
        self.watched = {"default"}

    def use(self, tube: str) -> None:
        self.calls.append(("use", tube))

    def watch(self, tube: str) -> int:
        self.calls.append(("watch", tube))
        self.watched.add(tube)
        return 2

    def ignore(self, tube: str) -> int:
        self.calls.append(("ignore", tube))
        self.watched.remove(tube)
        return 1

    def watching(self) -> list[str]:
        return sorted(self.watched)

    def put(self, body: bytes, *, priority: int, delay: int, ttr: int) -> int:
        self.calls.append(("put", priority, delay, ttr, body))
        return 1

    def reserve(self, timeout: int) -> Job | None:
        self.calls.append(("reserve", timeout))
        job, self.next_job = self.next_job, None
        return job

    def stats_job(self, job: object) -> dict[str, object]:
        return {"reserves": self.attempt, "ttr": 30}

    def delete(self, job: object) -> None:
        self.calls.append(("delete", job))

    def release(self, job: object, *, priority: int, delay: int) -> None:
        self.calls.append(("release", priority, delay))

    def bury(self, job: object, *, priority: int) -> None:
        self.calls.append(("bury", job, priority))

    def touch(self, job: object) -> None:
        self.calls.append(("touch", job))

    def close(self) -> None:
        self.calls.append(("close",))


def _driver(client: Client, clock: FakeClock | None = None) -> BeanstalkdQueueDriver:
    return BeanstalkdQueueDriver(
        client=client,
        tubes={"default": "af-jobs", "critical": "af-critical"},
        clock=clock or FakeClock(_NOW),
        reservation_lease=30,
    )


def test_beanstalkd_maps_lanes_delay_priority_and_ack() -> None:
    client = Client()
    driver = _driver(client)
    job = _job(lane="critical", delay=timedelta(milliseconds=1500), priority=20)
    driver.dispatch(job)
    assert client.calls[0] == ("use", "af-critical")
    assert client.calls[1][1:4] == (2**31 - 1 - 20, 2, 30)
    client.next_job = Job(1, QueueJobCodec().encode(job))
    reservation = driver.reserve(lane="critical")
    assert reservation is not None
    assert reservation.job == job
    assert ("watch", "af-critical") in client.calls
    assert ("ignore", "default") in client.calls
    driver.ack(reservation)
    with pytest.raises(QueueReservationLost):
        driver.ack(reservation)


def test_beanstalkd_renewal_rotates_handle_and_bounds_retries() -> None:
    client, clock = Client(), FakeClock(_NOW)
    driver = _driver(client, clock)
    job = _job(max_attempts=2, backoff=timedelta(seconds=3))
    client.next_job = Job(1, QueueJobCodec().encode(job))
    first = driver.reserve()
    assert first is not None
    clock.advance(timedelta(seconds=10))
    renewed = driver.renew(first)
    with pytest.raises(QueueReservationLost):
        driver.ack(first)
    driver.retry(renewed)
    assert client.calls[-1] == ("release", 2**31 - 1, 3)
    client.attempt = 2
    client.next_job = Job(1, QueueJobCodec().encode(job))
    last = driver.reserve()
    assert last is not None
    driver.retry(last)
    assert client.calls[-1][0] == "bury"


def test_beanstalkd_poison_is_buried_and_lane_mismatch_never_runs() -> None:
    client = Client()
    driver = _driver(client)
    for body in (b"invalid", QueueJobCodec().encode(_job(lane="critical"))):
        client.next_job = Job(1, body)
        assert driver.reserve() is None
        assert client.calls[-1][0] == "bury"
    assert driver.reserve() is None


def test_beanstalkd_expired_and_foreign_reservations_cannot_settle() -> None:
    client, clock = Client(), FakeClock(_NOW)
    driver = _driver(client, clock)
    client.next_job = Job(1, QueueJobCodec().encode(_job()))
    reserved = driver.reserve()
    assert reserved is not None
    with pytest.raises(QueueReservationLost):
        _driver(Client()).ack(reserved)
    clock.advance(timedelta(seconds=30))
    with pytest.raises(QueueReservationLost):
        driver.release(reserved)


def test_beanstalkd_empty_sdk_exception_is_an_idle_poll() -> None:
    class EmptyResponse(Exception):
        pass

    class EmptyClient(Client):
        def reserve(self, timeout: int) -> Job | None:
            raise EmptyResponse()

    driver = BeanstalkdQueueDriver(
        client=EmptyClient(), tubes={"default": "jobs"}, empty_errors=(EmptyResponse,)
    )
    assert driver.reserve() is None
    driver.close()


def test_beanstalkd_registration_binds_runtime_evidence_and_closes_client() -> None:
    from af.framework.managers import (
        CapabilityBinding,
        CapabilityEvidence,
        CapabilitySet,
        ConnectionRef,
        HealthStatus,
    )
    from af.queue import QueueDriver, QueueManager
    from af.queue.beanstalkd_manager import beanstalkd_queue_registration

    client = Client()

    def verify(
        ref: ConnectionRef, driver: QueueDriver, binding: CapabilityBinding
    ) -> CapabilityEvidence:
        assert isinstance(driver, BeanstalkdQueueDriver)
        return CapabilityEvidence(
            binding=binding,
            capabilities=CapabilitySet(feature.value for feature in driver.features),
            health=HealthStatus.HEALTHY,
            observed_at=_NOW,
            expires_at=_NOW + timedelta(minutes=1),
            evidence_digest="a" * 64,
        )

    manager = QueueManager(
        connections={
            "primary": {
                "driver": "beanstalkd",
                "tubes": {"default": "jobs"},
            }
        },
        default="primary",
        deployment_profile="test",
        policy_version="test",
        clock=lambda: _NOW,
    )
    manager.install(
        beanstalkd_queue_registration(
            client_factory=lambda: client, verifier=verify, clock=lambda: _NOW
        )
    )
    manager.dispatch(_job())
    client.next_job = Job(1, QueueJobCodec().encode(_job()))
    reserved = manager.reserve()
    assert reserved is not None
    manager.ack(reserved)
    manager.close()
    assert client.calls[-1] == ("close",)


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": timedelta(seconds=1)},
        {"unique_key": "unique"},
        {"fifo_group": "group"},
        {"requires_transaction": True},
    ],
)
def test_beanstalkd_rejects_unsupported_requests(options: dict[str, object]) -> None:
    from af.queue import UnsupportedQueueFeature

    with pytest.raises(UnsupportedQueueFeature):
        _driver(Client()).dispatch(_job(**options))


def test_beanstalkd_release_unknown_lane_and_bounded_priority() -> None:
    client = Client()
    driver = _driver(client)
    with pytest.raises(ValueError):
        driver.dispatch(_job(priority=2**31))
    with pytest.raises(ValueError):
        driver.reserve(lane="missing")
    client.next_job = Job(1, QueueJobCodec().encode(_job()))
    reserved = driver.reserve()
    assert reserved is not None
    driver.release(reserved)
    assert client.calls[-1] == ("release", 2**31 - 1, 0)
