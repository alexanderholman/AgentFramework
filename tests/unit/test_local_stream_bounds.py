"""Volatile local streams still enforce bounded, explicit replay contracts."""

from datetime import UTC, datetime, timedelta

import pytest

from af.messaging.envelopes import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.messaging.pubsub import (
    BackpressurePolicy,
    LocalPubSubDriver,
    LocalStreamDriver,
    PubSubOperationError,
    PubSubSubscription,
    PubSubTopic,
    ReplayCursor,
)

TOPIC = PubSubTopic("events")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def event(identifier: str, time: datetime = NOW) -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=identifier,
            message_type="event",
            schema_version=1,
            kind=EnvelopeKind.INTEGRATION_EVENT,
            occurred_at=time,
        ),
        payload={"id": identifier},
    )


@pytest.mark.parametrize("driver_class", [LocalPubSubDriver, LocalStreamDriver])
def test_local_since_sequence_and_time_are_not_silently_ignored(
    driver_class: type[LocalPubSubDriver],
) -> None:
    driver = driver_class()
    driver.publish(TOPIC, event("one"))
    driver.publish(TOPIC, event("two", NOW + timedelta(days=1)))
    subscription = PubSubSubscription(
        TOPIC, "sequence", replay_from=ReplayCursor.SINCE_SEQUENCE, since_sequence=1
    )
    assert driver.replay(subscription).events_replayed == 1
    assert driver.next_event(driver.subscribe(subscription)) == event(
        "two", NOW + timedelta(days=1)
    )
    timed = PubSubSubscription(
        TOPIC, "time", replay_from=ReplayCursor.SINCE_TIME, since_time=NOW + timedelta(days=1)
    )
    assert driver.next_event(driver.subscribe(timed)) == event("two", NOW + timedelta(days=1))


def test_local_buffer_backpressure_is_atomic() -> None:
    driver = LocalPubSubDriver()
    handle = driver.subscribe(PubSubSubscription(TOPIC, "one", max_pending=1))
    driver.publish(TOPIC, event("one"))
    with pytest.raises(PubSubOperationError):
        driver.publish(TOPIC, event("two"))
    assert driver.next_event(handle) == event("one")
    assert driver.replay(PubSubSubscription(TOPIC, "observer")).events_replayed == 1


@pytest.mark.parametrize(
    "policy,expected",
    [(BackpressurePolicy.DROP_OLDEST, "two"), (BackpressurePolicy.DROP_NEWEST, "one")],
)
def test_local_explicit_drop_policy_is_bounded(policy: BackpressurePolicy, expected: str) -> None:
    driver = LocalStreamDriver()
    handle = driver.subscribe(PubSubSubscription(TOPIC, "one", max_pending=1, backpressure=policy))
    driver.publish_ordered(TOPIC, event("one"), ordering_key="key")
    driver.publish_ordered(TOPIC, event("two"), ordering_key="key")
    assert driver.next_event(handle) == event(expected)
    assert driver.next_event(handle) is None


def test_local_history_is_bounded_and_replay_does_not_overallocate() -> None:
    driver = LocalPubSubDriver(max_events=2)
    driver.publish(TOPIC, event("one"))
    driver.publish(TOPIC, event("two"))
    with pytest.raises(PubSubOperationError):
        driver.publish(TOPIC, event("three"))
    with pytest.raises(PubSubOperationError):
        driver.subscribe(
            PubSubSubscription(TOPIC, "tiny", replay_from=ReplayCursor.BEGINNING, max_pending=1)
        )
