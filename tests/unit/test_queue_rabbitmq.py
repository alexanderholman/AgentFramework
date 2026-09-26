"""RabbitMQ quorum protocol tests without a live broker."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from tests.unit.test_queue_sqs import _job

from af.queue import QueueReservationLost, UnsupportedQueueFeature
from af.queue.codec import QueueJobCodec
from af.queue.rabbitmq import RabbitMQQueueDriver


class Channel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.delivery: tuple[object, object, object] = (None, None, None)
        self.publish_error = False

    def confirm_delivery(self) -> None:
        self.calls.append(("confirm", {}))

    def queue_declare(self, **kwargs: object) -> None:
        self.calls.append(("declare", kwargs))

    def basic_publish(self, **kwargs: object) -> None:
        self.calls.append(("publish", kwargs))
        if self.publish_error:
            raise RuntimeError("secret broker failure")

    def basic_get(self, **kwargs: object) -> tuple[object, object, object]:
        self.calls.append(("get", kwargs))
        delivery, self.delivery = self.delivery, (None, None, None)
        return delivery

    def basic_ack(self, **kwargs: object) -> None:
        self.calls.append(("ack", kwargs))

    def basic_nack(self, **kwargs: object) -> None:
        self.calls.append(("nack", kwargs))

    def basic_reject(self, **kwargs: object) -> None:
        self.calls.append(("reject", kwargs))


class Connection:
    def __init__(self) -> None:
        self.channel_instance = Channel()
        self.closed = False

    def channel(self) -> Channel:
        return self.channel_instance

    def close(self) -> None:
        self.closed = True

    def process_data_events(self, time_limit: float = 0) -> None:
        pass


def _driver(connection: Connection) -> RabbitMQQueueDriver:
    return RabbitMQQueueDriver(
        connection_factory=lambda: connection,
        properties_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        queues={"default": "af.jobs"},
        dead_letter_queues={"default": "af.failed"},
        clock=lambda: datetime(2026, 9, 26, tzinfo=UTC),
    )


def _deliver(channel: Channel, attempt: int = 1, body: bytes | None = None) -> None:
    channel.delivery = (
        SimpleNamespace(delivery_tag=7, redelivered=attempt > 1),
        SimpleNamespace(headers={"x-delivery-count": attempt - 1}),
        QueueJobCodec().encode(_job(max_attempts=2)) if body is None else body,
    )


def test_rabbitmq_confirms_durable_quorum_dispatch_and_owned_ack() -> None:
    connection = Connection()
    with _driver(connection) as driver:
        channel = connection.channel_instance
        driver.dispatch(_job(max_attempts=2))
        assert channel.calls[-1][1]["mandatory"] is True
        assert channel.calls[-1][1]["properties"].delivery_mode == 2
        _deliver(channel)
        reserved = driver.reserve()
        assert reserved is not None and reserved.expires_at is None
        driver.ack(reserved)
        assert channel.calls[-1] == ("ack", {"delivery_tag": 7})
        with pytest.raises(QueueReservationLost):
            driver.ack(reserved)
    assert connection.closed


def test_rabbitmq_retry_uses_failure_reject_and_release_uses_nack() -> None:
    connection = Connection()
    with _driver(connection) as driver:
        channel = connection.channel_instance
        _deliver(channel)
        first = driver.reserve()
        assert first is not None
        driver.retry(first)
        assert channel.calls[-1] == ("reject", {"delivery_tag": 7, "requeue": True})
        _deliver(channel, attempt=2)
        last = driver.reserve()
        assert last is not None
        driver.retry(last)
        assert [name for name, _ in channel.calls[-2:]] == ["publish", "ack"]
        _deliver(channel)
        released = driver.reserve()
        assert released is not None
        driver.release(released)
        assert channel.calls[-1] == (
            "nack",
            {"delivery_tag": 7, "multiple": False, "requeue": True},
        )


def test_rabbitmq_poison_and_failed_dead_letter_send_never_ack_source() -> None:
    from af.queue import QueueOperationError

    connection = Connection()
    with _driver(connection) as driver:
        channel = connection.channel_instance
        _deliver(channel, body=b"invalid")
        assert driver.reserve() is None
        assert [name for name, _ in channel.calls[-2:]] == ["publish", "ack"]
        _deliver(channel)
        reserved = driver.reserve()
        assert reserved is not None
        channel.publish_error = True
        with pytest.raises(QueueOperationError):
            driver.reject(reserved)
        assert channel.calls[-1][0] == "publish"


@pytest.mark.parametrize(
    "options",
    [{"priority": 1}, {"unique_key": "x"}, {"fifo_group": "group"}, {"requires_transaction": True}],
)
def test_rabbitmq_rejects_unverified_job_semantics(options: dict[str, Any]) -> None:
    with _driver(Connection()) as driver, pytest.raises(UnsupportedQueueFeature):
        driver.dispatch(_job(**options))


def test_rabbitmq_processes_heartbeats_on_its_own_io_thread() -> None:
    from threading import Event, get_ident

    observed = Event()
    threads: list[int] = []

    class HeartbeatConnection(Connection):
        def process_data_events(self, time_limit: float = 0) -> None:
            threads.append(get_ident())
            observed.set()

    connection = HeartbeatConnection()

    def factory() -> Connection:
        threads.append(get_ident())
        return connection

    with RabbitMQQueueDriver(
        connection_factory=factory,
        properties_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        queues={"default": "jobs"},
        dead_letter_queues={"default": "failed"},
        heartbeat_interval=0.001,
    ) as driver:
        assert observed.wait(1)
        assert len(set(threads)) == 1 and threads[0] != get_ident()
        assert driver.reserve() is None


def test_rabbitmq_heartbeat_failure_stops_new_intake() -> None:
    from af.queue import QueueOperationError

    class FailedHeartbeat(Connection):
        def process_data_events(self, time_limit: float = 0) -> None:
            raise RuntimeError("private heartbeat failure")

    with RabbitMQQueueDriver(
        connection_factory=FailedHeartbeat,
        properties_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        queues={"default": "jobs"},
        dead_letter_queues={"default": "failed"},
        heartbeat_interval=0.001,
    ) as driver:
        driver._heartbeat.join(timeout=1)
        with pytest.raises(QueueOperationError) as error:
            driver.reserve()
        assert "private" not in str(error.value)


def test_rabbitmq_channel_setup_failure_closes_owned_connection() -> None:
    from af.queue import QueueOperationError

    connection = Connection()

    def fail(**kwargs: object) -> None:
        raise RuntimeError("declare failed")

    connection.channel_instance.queue_declare = fail  # type: ignore[method-assign]
    with pytest.raises(QueueOperationError):
        _driver(connection)
    assert connection.closed


@pytest.mark.parametrize("delivery", ["invalid", (None,), (object(), None, b"body")])
def test_rabbitmq_rejects_malformed_protocol_responses(delivery: Any) -> None:
    from af.queue import QueueOperationError

    connection = Connection()
    connection.channel_instance.delivery = delivery
    with _driver(connection) as driver, pytest.raises(QueueOperationError):
        driver.reserve()


@pytest.mark.parametrize(
    "headers,attempt",
    [("invalid", 2), ({}, 2), ({"x-delivery-count": -1}, 1), ({"x-delivery-count": 3}, 4)],
)
def test_rabbitmq_invalid_or_exhausted_deliveries_go_to_failed_queue(
    headers: object, attempt: int
) -> None:
    connection = Connection()
    channel = connection.channel_instance
    _deliver(channel, attempt)
    method, _, body = channel.delivery
    channel.delivery = (method, SimpleNamespace(headers=headers), body)
    with _driver(connection) as driver:
        assert driver.reserve() is None
        assert [name for name, _ in channel.calls[-2:]] == ["publish", "ack"]


def test_rabbitmq_bounds_attempts_retries_and_shutdown() -> None:
    from datetime import timedelta

    from af.queue import QueueFeature, QueueOperationError

    connection = Connection()
    driver = _driver(connection)
    assert driver.supports(QueueFeature.RESERVATION)
    assert driver.features == {QueueFeature.RESERVATION, QueueFeature.RETRY}
    with pytest.raises(ValueError):
        driver.dispatch(_job(max_attempts=21))
    with pytest.raises(ValueError):
        driver.reserve(lane="missing")
    with pytest.raises(TypeError):
        driver.dispatch(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        driver.ack(object())  # type: ignore[arg-type]
    _deliver(connection.channel_instance)
    reservation = driver.reserve()
    assert reservation is not None
    with pytest.raises(UnsupportedQueueFeature):
        driver.retry(
            reservation, available_at=datetime(2026, 9, 26, tzinfo=UTC) + timedelta(seconds=1)
        )
    driver.close()
    driver.close()
    with pytest.raises(QueueOperationError):
        driver.reserve()


def test_rabbitmq_registration_routes_through_verified_manager() -> None:
    from datetime import timedelta

    from af.framework.managers import (
        CapabilityBinding,
        CapabilityEvidence,
        CapabilitySet,
        ConnectionRef,
        HealthStatus,
    )
    from af.queue import QueueDriver, QueueManager
    from af.queue.rabbitmq_manager import rabbitmq_queue_registration

    now = datetime(2026, 9, 26, tzinfo=UTC)
    connection = Connection()

    def verify(
        ref: ConnectionRef, driver: QueueDriver, binding: CapabilityBinding
    ) -> CapabilityEvidence:
        assert isinstance(driver, RabbitMQQueueDriver)
        return CapabilityEvidence(
            binding=binding,
            capabilities=CapabilitySet(feature.value for feature in driver.features),
            health=HealthStatus.HEALTHY,
            observed_at=now,
            expires_at=now + timedelta(minutes=1),
            evidence_digest="a" * 64,
        )

    registration = rabbitmq_queue_registration(
        connection_factory=lambda: connection,
        properties_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        verifier=verify,
        clock=lambda: now,
    )
    manager = QueueManager(
        connections={
            "primary": {
                "driver": "rabbitmq",
                "queues": {"default": "jobs"},
                "dead_letter_queues": {"default": "failed"},
            }
        },
        default="primary",
        deployment_profile="test",
        policy_version="test",
        clock=lambda: now,
    )
    manager.install(registration)
    manager.dispatch(_job())
    _deliver(connection.channel_instance)
    reserved = manager.reserve()
    assert reserved is not None
    manager.ack(reserved)
    manager.close()
    with pytest.raises(ValueError):
        registration.factory(ConnectionRef("queue", "bad", "rabbitmq"), {})
