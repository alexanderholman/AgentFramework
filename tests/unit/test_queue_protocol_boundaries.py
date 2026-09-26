"""Failure-path contracts shared by the injected broker protocol adapters."""

from datetime import timedelta
from typing import Any

import pytest
from tests.unit.test_queue_beanstalkd import Client as BeanstalkClient
from tests.unit.test_queue_beanstalkd import Job, _job
from tests.unit.test_queue_sqs import Client as SQSClient
from tests.unit.test_queue_sqs import _driver as sqs_driver

from af.framework.managers import ConnectionRef
from af.queue import QueueFeature, QueueOperationError, QueueReservationLost
from af.queue.beanstalkd import BeanstalkdQueueDriver
from af.queue.beanstalkd_manager import beanstalkd_queue_registration
from af.queue.sqs import SQSQueueDriver
from af.queue.sqs_manager import sqs_queue_registration


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"Messages": "invalid"},
        {"Messages": [1]},
        {"Messages": [{"Body": "x"}]},
        {"Messages": [{}, {}]},
    ],
)
def test_sqs_malformed_broker_responses_fail_closed(response: object) -> None:
    class Malformed(SQSClient):
        def receive_message(self, **kwargs: object) -> Any:
            return response

    with pytest.raises(QueueOperationError):
        sqs_driver(Malformed()).reserve()


@pytest.mark.parametrize(
    "attributes,attempt,lane",
    [
        (None, 1, "default"),
        ({}, 1, "default"),
        ({"ApproximateReceiveCount": "0"}, 1, "default"),
        ({"ApproximateReceiveCount": "1"}, 1, "wrong"),
        ({"ApproximateReceiveCount": "3"}, 3, "default"),
    ],
)
def test_sqs_invalid_or_exhausted_delivery_goes_to_failed_queue(
    attributes: object, attempt: int, lane: str
) -> None:
    client = SQSClient()
    client.deliver(_job(max_attempts=2, lane=lane), attempt)
    client.messages[0]["Attributes"] = attributes
    driver = sqs_driver(client)
    assert driver.reserve() is None
    assert [name for name, _ in client.calls[-2:]] == ["send", "delete"]


def test_sqs_idle_poll_nonadvancing_renewal_and_closed_driver() -> None:
    client = SQSClient()
    driver = sqs_driver(client)
    assert driver.supports(QueueFeature.RESERVATION)
    assert not driver.supports(QueueFeature.PRIORITY)
    assert driver.reserve() is None
    client.deliver(_job())
    reservation = driver.reserve()
    assert reservation is not None
    with pytest.raises(QueueReservationLost):
        driver.renew(reservation)
    with pytest.raises(TypeError):
        driver.ack(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        driver.dispatch(object())  # type: ignore[arg-type]
    driver.close()
    driver.close()
    with pytest.raises(QueueOperationError):
        driver.reserve()


@pytest.mark.parametrize(
    "queues,dead",
    [
        ({}, {"default": "https://sqs.test/dead"}),
        ({"default": "http://sqs.test/main"}, {"default": "https://sqs.test/dead"}),
        ({"default": "https://sqs.test/main.fifo"}, {"default": "https://sqs.test/dead"}),
        (
            {"a": "https://sqs.test/main", "b": "https://sqs.test/main"},
            {"a": "https://sqs.test/dead"},
        ),
        ({"default": "https://sqs.test/main"}, {"other": "https://sqs.test/dead"}),
        ({"default": "https://sqs.test/main"}, {"default": "https://sqs.test/main"}),
    ],
)
def test_sqs_rejects_ambiguous_or_unsafe_lane_topology(
    queues: dict[str, str], dead: dict[str, str]
) -> None:
    with pytest.raises(ValueError):
        SQSQueueDriver(client=SQSClient(), queues=queues, dead_letter_queues=dead)


def test_sqs_unacknowledged_send_fails_instead_of_inventing_receipt() -> None:
    class Unacknowledged(SQSClient):
        def send_message(self, **kwargs: object) -> dict[str, object]:
            return {}

    with pytest.raises(QueueOperationError):
        sqs_driver(Unacknowledged()).dispatch(_job())


@pytest.mark.parametrize(
    "stats", [None, {}, {"reserves": 0, "ttr": 30}, {"reserves": 4, "ttr": 30}]
)
def test_beanstalk_invalid_or_exhausted_job_is_buried(stats: object) -> None:
    from af.queue.codec import QueueJobCodec

    class InvalidStats(BeanstalkClient):
        def stats_job(self, job: object) -> Any:
            return stats

    client = InvalidStats()
    client.next_job = Job(1, QueueJobCodec().encode(_job(max_attempts=2)))
    driver = BeanstalkdQueueDriver(client=client, tubes={"default": "jobs"})
    assert driver.reserve() is None
    assert client.calls[-1][0] == "bury"


def test_beanstalk_failed_settlement_stays_visible_to_caller_and_is_sanitized() -> None:
    from af.queue.codec import QueueJobCodec

    class FailedDelete(BeanstalkClient):
        def delete(self, job: object) -> None:
            raise RuntimeError("secret broker details")

    client = FailedDelete()
    client.next_job = Job(1, QueueJobCodec().encode(_job()))
    driver = BeanstalkdQueueDriver(client=client, tubes={"default": "jobs"})
    assert driver.supports(QueueFeature.PRIORITY)
    reservation = driver.reserve()
    assert reservation is not None
    with pytest.raises(QueueOperationError) as error:
        driver.ack(reservation)
    assert "secret" not in str(error.value)
    with pytest.raises(TypeError):
        driver.dispatch(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        driver.ack(object())  # type: ignore[arg-type]
    driver.close()
    driver.close()
    with pytest.raises(QueueOperationError):
        driver.reserve()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tubes": {}},
        {"tubes": {"a": "same", "b": "same"}},
        {"reservation_lease": 0},
        {"empty_errors": ("invalid",)},
    ],
)
def test_beanstalk_invalid_connection_configuration_is_rejected(kwargs: dict[str, Any]) -> None:
    config: dict[str, Any] = {"tubes": {"default": "jobs"}, **kwargs}
    with pytest.raises(ValueError):
        BeanstalkdQueueDriver(client=BeanstalkClient(), **config)


def test_beanstalk_rejects_unbounded_delay() -> None:
    with pytest.raises(ValueError):
        BeanstalkdQueueDriver(client=BeanstalkClient(), tubes={"default": "jobs"}).dispatch(
            _job(delay=timedelta(days=100000))
        )


@pytest.mark.parametrize("kind", ["sqs", "beanstalkd"])
def test_broker_registration_rejects_config_and_disposes_failed_construction(kind: str) -> None:
    client: Any = SQSClient() if kind == "sqs" else BeanstalkClient()
    registration = (sqs_queue_registration if kind == "sqs" else beanstalkd_queue_registration)(
        client_factory=lambda: client, verifier=lambda *_: None
    )  # type: ignore[arg-type]
    ref = ConnectionRef("queue", "primary", kind)
    with pytest.raises(ValueError):
        registration.factory(ref, {"driver": kind, "bad": "option"})
    config: dict[str, object] = (
        {"driver": kind, "queues": {}, "dead_letter_queues": {}}
        if kind == "sqs"
        else {"driver": kind, "tubes": {}}
    )
    with pytest.raises(ValueError):
        registration.factory(ref, config)
    if kind == "sqs":
        assert client.closed
    else:
        assert client.calls[-1] == ("close",)
