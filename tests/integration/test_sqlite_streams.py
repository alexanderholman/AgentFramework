"""Durable stream behavior against real SQLite files and independent owners."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.infrastructure.sqlite_streams import SQLiteStreamDriver
from af.messaging.envelopes import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.messaging.pubsub import (
    BackpressurePolicy,
    CheckpointCursor,
    ConsumerGroup,
    ConsumerGroupAssignment,
    InvalidPubSubSchema,
    PubSubOperationError,
    PubSubSubscription,
    PubSubSubscriptionHandle,
    PubSubTopic,
    ReplayCursor,
    SlowConsumerPolicy,
    UnsupportedPubSubFeature,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TOPIC = PubSubTopic("events.test")


def event(identifier: str, *, time: datetime = NOW) -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=identifier,
            message_type=TOPIC.name,
            schema_version=1,
            kind=EnvelopeKind.INTEGRATION_EVENT,
            occurred_at=time,
        ),
        payload={"id": identifier},
    )


def subscription(name: str = "reader", **kwargs: object) -> PubSubSubscription:
    return PubSubSubscription(TOPIC, name, replay_from=ReplayCursor.BEGINNING, **kwargs)  # type: ignore[arg-type]


def test_checkpoint_survives_restart_and_unacked_is_redelivered(tmp_path: Path) -> None:
    clock = [NOW]
    first = SQLiteStreamDriver(tmp_path / "stream.db", clock=lambda: clock[0])
    handle = first.subscribe(subscription())
    first.publish(TOPIC, event("first"))
    first.publish(TOPIC, event("second"))
    delivery = first.next_delivery(handle)
    assert delivery is not None and delivery.envelope.metadata.message_id == "first"
    first.checkpoint(
        CheckpointCursor(TOPIC, "reader", delivery.sequence, NOW, delivery.ownership_token)
    )
    second = first.next_delivery(handle)
    assert second is not None and second.envelope.metadata.message_id == "second"
    restarted = SQLiteStreamDriver(tmp_path / "stream.db", clock=lambda: clock[0])
    recovered = restarted.subscribe(subscription())
    assert recovered == handle
    assert restarted.next_event(recovered) is None
    clock[0] += timedelta(seconds=31)
    redelivered = restarted.next_delivery(recovered)
    assert redelivered is not None and redelivered.sequence == second.sequence
    assert redelivered.ownership_token != second.ownership_token
    with pytest.raises(PubSubOperationError):
        first.checkpoint(
            CheckpointCursor(TOPIC, "reader", second.sequence, clock[0], second.ownership_token)
        )
    restarted.checkpoint(
        CheckpointCursor(TOPIC, "reader", second.sequence, clock[0], redelivered.ownership_token)
    )
    assert restarted.next_event(recovered) is None


def test_checkpoint_cannot_skip_undelivered_records(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db")
    handle = driver.subscribe(subscription())
    driver.publish(TOPIC, event("first"))
    driver.publish(TOPIC, event("second"))
    with pytest.raises(PubSubOperationError):
        driver.checkpoint(CheckpointCursor(TOPIC, "reader", 2, NOW))
    assert driver.next_event(handle) is not None
    with pytest.raises(PubSubOperationError):
        driver.checkpoint(CheckpointCursor(TOPIC, "reader", 2, NOW))
    assert driver.next_event(handle) is None


def test_expired_receipt_from_same_owner_cannot_ack_redelivery(tmp_path: Path) -> None:
    clock = [NOW]
    driver = SQLiteStreamDriver(tmp_path / "stream.db", clock=lambda: clock[0])
    handle = driver.subscribe(subscription())
    driver.publish(TOPIC, event("one"))
    first = driver.next_delivery(handle)
    assert first is not None
    clock[0] += timedelta(seconds=31)
    second = driver.next_delivery(handle)
    assert second is not None and first.sequence == second.sequence
    with pytest.raises(PubSubOperationError):
        driver.checkpoint(
            CheckpointCursor(TOPIC, "reader", first.sequence, clock[0], first.ownership_token)
        )
    driver.checkpoint(
        CheckpointCursor(TOPIC, "reader", second.sequence, clock[0], second.ownership_token)
    )


def test_sequence_time_key_replay_and_read_only_report(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db")
    driver.publish_ordered(TOPIC, event("old"), ordering_key="a")
    driver.publish_ordered(TOPIC, event("new", time=NOW + timedelta(days=1)), ordering_key="b")
    driver.publish_ordered(TOPIC, event("last", time=NOW + timedelta(days=2)), ordering_key="a")
    filtered = PubSubSubscription(
        TOPIC,
        "reader",
        ordering_key="a",
        replay_from=ReplayCursor.SINCE_SEQUENCE,
        since_sequence=1,
    )
    report = driver.replay(filtered)
    assert (report.events_replayed, report.from_sequence, report.to_sequence) == (1, 3, 3)
    assert driver.next_event(driver.subscribe(filtered)) == event(
        "last", time=NOW + timedelta(days=2)
    )
    timed = PubSubSubscription(
        TOPIC,
        "timed",
        replay_from=ReplayCursor.SINCE_TIME,
        since_time=NOW + timedelta(days=1),
    )
    assert driver.replay(timed).events_replayed == 2
    assert driver.next_event(driver.subscribe(timed)) == event("new", time=NOW + timedelta(days=1))


def test_group_partition_ownership_and_expired_owner_fencing(tmp_path: Path) -> None:
    clock = [NOW]
    driver = SQLiteStreamDriver(tmp_path / "stream.db", clock=lambda: clock[0])
    driver.create_consumer_group(ConsumerGroup("workers", TOPIC, partitions=2))
    a = driver.subscribe(subscription("a", consumer_group="workers"))
    b = driver.subscribe(subscription("b", consumer_group="workers"))
    driver.assign_consumer(ConsumerGroupAssignment("workers", "a", 0, 2))
    driver.assign_consumer(ConsumerGroupAssignment("workers", "b", 1, 2))
    for index in range(12):
        driver.publish_ordered(TOPIC, event(f"item-{index}"), ordering_key=f"key-{index}")
    da, db = driver.next_delivery(a), driver.next_delivery(b)
    assert da is not None and db is not None
    assert da.partition == 0 and db.partition == 1 and da.sequence != db.sequence
    with pytest.raises(PubSubOperationError):
        driver.assign_consumer(ConsumerGroupAssignment("workers", "b", 0, 2))
    clock[0] += timedelta(seconds=31)
    driver.assign_consumer(ConsumerGroupAssignment("workers", "b", 0, 2))
    with pytest.raises(PubSubOperationError):
        driver.checkpoint(CheckpointCursor(TOPIC, "a", da.sequence, clock[0], da.ownership_token))
    recovered = driver.next_delivery(b)
    assert recovered is not None and recovered.sequence == da.sequence
    driver.checkpoint(
        CheckpointCursor(TOPIC, "b", recovered.sequence, clock[0], recovered.ownership_token)
    )


def test_backpressure_is_atomic_and_history_requires_safe_pruning(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db", max_events=2)
    handle = driver.subscribe(subscription(max_pending=1))
    driver.publish(TOPIC, event("one"))
    with pytest.raises(PubSubOperationError):
        driver.publish(TOPIC, event("two"))
    assert driver.replay(subscription("observer")).events_replayed == 1
    with pytest.raises(PubSubOperationError):
        driver.prune(TOPIC, through_sequence=1)
    delivery = driver.next_delivery(handle)
    assert delivery is not None
    driver.checkpoint(
        CheckpointCursor(TOPIC, "reader", delivery.sequence, NOW, delivery.ownership_token)
    )
    assert driver.prune(TOPIC, through_sequence=1) == 1
    with pytest.raises(PubSubOperationError):
        driver.subscribe(subscription("late"))
    driver.publish(TOPIC, event("two"))
    assert driver.next_event(handle) == event("two")


def test_duplicate_publish_is_idempotent_but_conflict_is_rejected(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db", max_events=1)
    driver.publish(TOPIC, event("one"))
    driver.publish(TOPIC, event("one"))
    assert driver.replay(subscription()).events_replayed == 1
    with pytest.raises(PubSubOperationError):
        driver.publish(TOPIC, event("one", time=NOW + timedelta(days=1)))
    with pytest.raises(PubSubOperationError):
        driver.publish(TOPIC, event("two"))


def test_persistent_subscription_config_cannot_silently_change(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db")
    handle = driver.subscribe(subscription())
    with pytest.raises(PubSubOperationError):
        driver.subscribe(PubSubSubscription(TOPIC, "reader"))
    driver.unsubscribe(handle)
    driver.publish(TOPIC, event("one"))
    assert driver.next_event(handle) is None
    assert driver.next_event(driver.subscribe(subscription())) == event("one")


def test_unsupported_loss_policy_and_bounded_replay_are_explicit(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db")
    with pytest.raises(UnsupportedPubSubFeature):
        driver.subscribe(subscription(backpressure=BackpressurePolicy.DROP_OLDEST))
    driver.publish(TOPIC, event("one"))
    driver.publish(TOPIC, event("two"))
    with pytest.raises(PubSubOperationError):
        driver.subscribe(subscription(max_pending=1))
    with pytest.raises(PubSubOperationError):
        driver.replay(subscription(max_pending=1))


def test_block_waits_for_ack_and_times_out_without_losing_event(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db", publish_timeout=timedelta(seconds=1))
    handle = driver.subscribe(subscription(max_pending=1, backpressure=BackpressurePolicy.BLOCK))
    driver.publish(TOPIC, event("one"))
    delivery = driver.next_delivery(handle)
    assert delivery is not None
    with ThreadPoolExecutor() as pool:
        waiting = pool.submit(driver.publish, TOPIC, event("two"))
        driver.checkpoint(
            CheckpointCursor(TOPIC, "reader", delivery.sequence, NOW, delivery.ownership_token)
        )
        waiting.result(timeout=2)
    bounded = SQLiteStreamDriver(tmp_path / "stream.db", publish_timeout=timedelta(0))
    with pytest.raises(PubSubOperationError, match="operation failed"):
        bounded.publish(TOPIC, event("three"))
    assert driver.replay(subscription("observe")).events_replayed == 2


def test_latest_subscription_waits_for_future_publish(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db")
    driver.publish(TOPIC, event("old"))
    handle = driver.subscribe(PubSubSubscription(TOPIC, "latest"))
    assert driver.next_event(handle, timeout=timedelta(milliseconds=1)) is None
    with ThreadPoolExecutor() as pool:
        waiting = pool.submit(driver.next_event, handle, timeout=timedelta(seconds=1))
        driver.publish(TOPIC, event("new"))
        assert waiting.result(timeout=2) == event("new")


def test_partition_prune_allows_acknowledged_history_and_new_group_member(tmp_path: Path) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db")
    group = ConsumerGroup("workers", TOPIC, partitions=2)
    driver.create_consumer_group(group)
    driver.create_consumer_group(group)
    handle = driver.subscribe(subscription("a", consumer_group="workers"))
    for partition in range(2):
        assignment = ConsumerGroupAssignment("workers", "a", partition, 2)
        driver.assign_consumer(assignment)
        driver.assign_consumer(assignment)
    driver.publish(TOPIC, event("one"))
    delivery = driver.next_delivery(handle)
    assert delivery is not None
    with pytest.raises(PubSubOperationError):
        driver.prune(TOPIC, through_sequence=delivery.sequence)
    driver.checkpoint(
        CheckpointCursor(TOPIC, "a", delivery.sequence, NOW, delivery.ownership_token)
    )
    assert driver.prune(TOPIC, through_sequence=delivery.sequence) == 1
    driver.subscribe(subscription("b", consumer_group="workers"))
    with pytest.raises(PubSubOperationError):
        driver.create_consumer_group(ConsumerGroup("late", TOPIC))


def test_filtered_subscription_backlog_does_not_block_other_keys_or_older_events(
    tmp_path: Path,
) -> None:
    driver = SQLiteStreamDriver(tmp_path / "stream.db")
    driver.subscribe(
        PubSubSubscription(
            TOPIC,
            "filtered",
            ordering_key="a",
            replay_from=ReplayCursor.SINCE_TIME,
            since_time=NOW,
            max_pending=1,
        )
    )
    driver.publish_ordered(TOPIC, event("one"), ordering_key="a")
    driver.publish_ordered(TOPIC, event("other"), ordering_key="b")
    driver.publish_ordered(TOPIC, event("old", time=NOW - timedelta(days=1)), ordering_key="a")
    assert driver.replay(subscription()).events_replayed == 3


def test_invalid_boundaries_unknown_handles_and_group_conflicts(tmp_path: Path) -> None:
    for kwargs in (
        {"max_events": 0},
        {"visibility_timeout": timedelta(0)},
        {"publish_timeout": timedelta(days=1)},
    ):
        with pytest.raises(InvalidPubSubSchema):
            SQLiteStreamDriver(tmp_path / "invalid.db", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(InvalidPubSubSchema):
        SQLiteStreamDriver(Path(":memory:"))
    driver = SQLiteStreamDriver(tmp_path / "stream.db", max_subscriptions=1)
    handle = driver.subscribe(subscription())
    with pytest.raises(PubSubOperationError):
        driver.subscribe(subscription("second"))
    with pytest.raises(PubSubOperationError):
        driver.next_event(PubSubSubscriptionHandle("unknown", TOPIC, "reader"))
    with pytest.raises(InvalidPubSubSchema):
        driver.next_event(handle, timeout=timedelta(seconds=31))
    with pytest.raises(InvalidPubSubSchema):
        driver.prune(TOPIC, through_sequence=-1)
    with pytest.raises(UnsupportedPubSubFeature):
        driver.subscribe(subscription(slow_consumer=SlowConsumerPolicy.KILL))
    with pytest.raises(UnsupportedPubSubFeature):
        driver.subscribe(subscription(consumer_group="workers", ordering_key="a"))
    driver.create_consumer_group(ConsumerGroup("workers", TOPIC))
    with pytest.raises(PubSubOperationError):
        driver.create_consumer_group(ConsumerGroup("workers", TOPIC, partitions=2))
    with pytest.raises(PubSubOperationError):
        driver.create_consumer_group(ConsumerGroup("another", TOPIC))
    with pytest.raises(InvalidPubSubSchema):
        driver.create_consumer_group(ConsumerGroup("huge", TOPIC, partitions=1025))
    with pytest.raises(PubSubOperationError):
        driver.assign_consumer(ConsumerGroupAssignment("unknown", "a", 0, 1))
    with pytest.raises(PubSubOperationError):
        driver.checkpoint(CheckpointCursor(TOPIC, "unknown", 0, NOW))


def test_storage_corruption_clock_and_byte_budget_are_explicit(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(PubSubOperationError):
        SQLiteStreamDriver(corrupt)
    driver = SQLiteStreamDriver(tmp_path / "stream.db", max_bytes=1)
    with pytest.raises(PubSubOperationError):
        driver.publish(TOPIC, event("large"))
    clockless = SQLiteStreamDriver(tmp_path / "clock.db", clock=lambda: datetime(2026, 1, 1))
    with pytest.raises(InvalidPubSubSchema):
        clockless.next_event(clockless.subscribe(subscription()))
    with pytest.raises(PubSubOperationError):
        clockless.subscribe(subscription("missing", consumer_group="absent"))


def test_application_stream_api_recovers_persisted_checkpoint_after_restart(tmp_path: Path) -> None:
    from af.bootstrap import create_application
    from af.config import Settings

    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    first = create_application(settings=settings)
    try:
        handle = first.streams.subscribe(subscription())
        first.streams.publish(TOPIC, event("committed"))
        delivered = first.streams.next_delivery(handle)
        assert delivered is not None
        first.streams.checkpoint(
            CheckpointCursor(
                TOPIC,
                "reader",
                delivered.sequence,
                NOW,
                delivered.ownership_token,
            )
        )
        first.streams.publish(TOPIC, event("pending"))
    finally:
        first.shutdown()
    restarted = create_application(settings=settings)
    try:
        handle = restarted.streams.subscribe(subscription())
        assert restarted.streams.next_event(handle) == event("pending")
    finally:
        restarted.shutdown()
