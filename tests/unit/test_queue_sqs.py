from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import QueueJob, QueueReservationLost, UnsupportedQueueFeature
from af.queue.codec import QueueJobCodec
from af.queue.sqs import SQSQueueDriver
from af.testing import FakeClock

_NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _job(**options: Any) -> QueueJob:
    return QueueJob(
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id="sqs-job",
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW,
            ),
            payload={"secret": "private"},
        ),
        **options,
    )


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.messages: list[dict[str, object]] = []
        self.fail_send = False
        self.closed = False

    def send_message(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(("send", kwargs))
        if self.fail_send:
            raise RuntimeError("secret account failure")
        return {"MessageId": "broker-id"}

    def receive_message(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(("receive", kwargs))
        messages, self.messages = self.messages, []
        return {"Messages": messages}

    def delete_message(self, **kwargs: object) -> object:
        self.calls.append(("delete", kwargs))
        return {}

    def change_message_visibility(self, **kwargs: object) -> object:
        self.calls.append(("visibility", kwargs))
        return {}

    def close(self) -> None:
        self.closed = True

    def deliver(self, job: QueueJob, attempt: int = 1) -> None:
        self.messages = [
            {
                "Body": QueueJobCodec().encode(job).decode(),
                "ReceiptHandle": "receipt-secret",
                "Attributes": {"ApproximateReceiveCount": str(attempt)},
            }
        ]


def _driver(client: Client, clock: FakeClock | None = None) -> SQSQueueDriver:
    return SQSQueueDriver(
        client=client,
        queues={"default": "https://sqs.test/primary"},
        dead_letter_queues={"default": "https://sqs.test/failed"},
        clock=clock or FakeClock(_NOW),
        visibility_timeout=30,
    )


def test_sqs_dispatch_and_reserve_ack_keep_broker_handles_private() -> None:
    client = Client()
    driver = _driver(client)
    job = _job(delay=timedelta(milliseconds=1001), max_attempts=3)
    receipt = driver.dispatch(job)
    assert receipt.message_id == "sqs-job"
    assert client.calls[0][1]["DelaySeconds"] == 2
    client.deliver(job)
    reservation = driver.reserve()
    assert reservation is not None
    assert reservation.job == job
    assert "receipt-secret" not in repr(reservation)
    driver.ack(reservation)
    assert client.calls[-1] == (
        "delete",
        {
            "QueueUrl": "https://sqs.test/primary",
            "ReceiptHandle": "receipt-secret",
        },
    )
    with pytest.raises(QueueReservationLost):
        driver.ack(reservation)
    driver.close()
    assert client.closed


def test_sqs_renewal_rotates_handle_and_rejects_wrong_owner_and_expiry() -> None:
    client, clock = Client(), FakeClock(_NOW)
    driver = _driver(client, clock)
    client.deliver(_job(max_attempts=3))
    first = driver.reserve()
    assert first is not None
    with pytest.raises(QueueReservationLost):
        _driver(client, clock).ack(first)
    clock.advance(timedelta(seconds=10))
    renewed = driver.renew(first)
    assert renewed.reservation_id != first.reservation_id
    with pytest.raises(QueueReservationLost):
        driver.ack(first)
    clock.advance(timedelta(seconds=30))
    with pytest.raises(QueueReservationLost):
        driver.ack(renewed)


def test_sqs_retry_exhaustion_and_poison_send_before_deleting() -> None:
    client = Client()
    driver = _driver(client)
    client.deliver(_job(max_attempts=2), attempt=1)
    first = driver.reserve()
    assert first is not None
    driver.retry(first, available_at=_NOW + timedelta(seconds=3))
    assert client.calls[-1][1]["VisibilityTimeout"] == 3
    client.deliver(_job(max_attempts=2), attempt=2)
    second = driver.reserve()
    assert second is not None
    driver.retry(second)
    assert [name for name, _ in client.calls[-2:]] == ["send", "delete"]
    assert client.calls[-2][1]["QueueUrl"] == "https://sqs.test/failed"
    client.messages = [{"Body": "bad-json", "ReceiptHandle": "poison", "Attributes": {}}]
    assert driver.reserve() is None
    assert [name for name, _ in client.calls[-2:]] == ["send", "delete"]


def test_sqs_dead_letter_failure_never_deletes_original() -> None:
    from af.queue import QueueOperationError

    client = Client()
    driver = _driver(client)
    client.deliver(_job())
    reservation = driver.reserve()
    assert reservation is not None
    client.fail_send = True
    with pytest.raises(QueueOperationError) as error:
        driver.reject(reservation)
    assert "secret" not in str(error.value)
    assert not any(name == "delete" for name, _ in client.calls)


@pytest.mark.parametrize(
    "options",
    [
        {"priority": 1},
        {"timeout": timedelta(seconds=1)},
        {"unique_key": "unique"},
        {"fifo_group": "group"},
        {"requires_transaction": True},
    ],
)
def test_sqs_rejects_unimplemented_semantics(options: dict[str, object]) -> None:
    with pytest.raises(UnsupportedQueueFeature):
        _driver(Client()).dispatch(_job(**options))


def test_sqs_manager_requires_deployment_evidence_and_binds_connection() -> None:
    from af.framework.managers import (
        CapabilityBinding,
        CapabilityEvidence,
        CapabilitySet,
        ConnectionRef,
        HealthStatus,
    )
    from af.queue import QueueDriver, QueueFeature, QueueManager
    from af.queue.sqs_manager import sqs_queue_registration

    client = Client()

    def verify(
        ref: ConnectionRef, driver: QueueDriver, binding: CapabilityBinding
    ) -> CapabilityEvidence:
        assert ref.connection_name == "primary"
        assert isinstance(driver, SQSQueueDriver)
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
                "driver": "sqs",
                "queues": {"default": "https://sqs.test/primary"},
                "dead_letter_queues": {"default": "https://sqs.test/failed"},
                "visibility_timeout": 30,
            }
        },
        default="primary",
        deployment_profile="test",
        policy_version="test",
        clock=lambda: _NOW,
    )
    manager.install(
        sqs_queue_registration(client_factory=lambda: client, verifier=verify, clock=lambda: _NOW)
    )
    manager.connection(requires=(QueueFeature.RESERVATION,))
    client.deliver(_job())
    reservation = manager.reserve()
    assert reservation is not None
    assert reservation.connection_name == "primary"
    manager.ack(reservation)
    manager.close()


@pytest.mark.parametrize("visibility", [0, -1, 43201, True])
def test_sqs_rejects_invalid_visibility(visibility: int) -> None:
    with pytest.raises(ValueError):
        SQSQueueDriver(
            client=Client(),
            queues={"default": "https://sqs.test/main"},
            dead_letter_queues={"default": "https://sqs.test/dead"},
            visibility_timeout=visibility,
        )


def test_sqs_rejects_out_of_range_delay_and_retry_and_unknown_lane() -> None:
    client = Client()
    driver = _driver(client)
    with pytest.raises(ValueError):
        driver.dispatch(_job(delay=timedelta(seconds=901)))
    with pytest.raises(ValueError):
        driver.dispatch(_job(backoff=timedelta(days=1)))
    with pytest.raises(ValueError):
        driver.reserve(lane="unconfigured")
    client.deliver(_job(max_attempts=3))
    reserved = driver.reserve()
    assert reserved is not None
    with pytest.raises(ValueError):
        driver.retry(reserved, available_at=_NOW + timedelta(days=1))
    driver.release(reserved)
    assert client.calls[-1][1]["VisibilityTimeout"] == 0
