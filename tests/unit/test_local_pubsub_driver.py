"""Tests for LocalPubSubDriver - in-process pub-sub with durable replay."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from af.framework.errors import PlatformReason
from af.messaging.envelopes import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.messaging.pubsub import (
    BroadcastChannelKind,
    BroadcastDispatchReport,
    BroadcastSubscription,
    CheckpointCursor,
    ConsumerGroup,
    ConsumerGroupAssignment,
    InvalidPubSubSchema,
    LocalBroadcastManager,
    LocalPubSubDriver,
    LocalStreamDriver,
    OrderingScope,
    PubSubOperationError,
    PubSubSubscription,
    PubSubTopic,
    ReplayCursor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _topic(name: str = "events.user.created") -> PubSubTopic:
    return PubSubTopic(name)


def _envelope(
    *,
    kind: EnvelopeKind = EnvelopeKind.INTEGRATION_EVENT,
    message_type: str = "events.user.created",
    payload: object | None = None,
    message_id: str | None = None,
) -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=message_id or uuid.uuid4().hex,
            message_type=message_type,
            schema_version=1,
            kind=kind,
            occurred_at=datetime.now(UTC),
        ),
        payload=payload or {"id": uuid.uuid4().hex},
    )


def _subscription(
    *,
    topic: PubSubTopic | None = None,
    subscriber_id: str = "sub-1",
    consumer_group: str | None = None,
    replay_from: ReplayCursor | None = None,
) -> PubSubSubscription:
    return PubSubSubscription(
        topic=topic or _topic(),
        subscriber_id=subscriber_id,
        consumer_group=consumer_group,
        replay_from=replay_from,
    )


# ---------------------------------------------------------------------------
# Test: independent subscribers fan-out
# ---------------------------------------------------------------------------


class TestLocalPubSubFanOut:
    """A single published event is delivered to every independent subscriber."""

    def test_two_subscribers_receive_one_event(self) -> None:
        driver = LocalPubSubDriver()
        topic = _topic()

        handle_a = driver.subscribe(_subscription(subscriber_id="sub-a"))
        handle_b = driver.subscribe(_subscription(subscriber_id="sub-b"))

        envelope = _envelope(message_type=topic.name)
        driver.publish(topic, envelope)

        event_a = driver.next_event(handle_a, timeout=timedelta(seconds=1))
        event_b = driver.next_event(handle_b, timeout=timedelta(seconds=1))

        assert event_a is not None
        assert event_a.metadata.message_id == envelope.metadata.message_id
        assert event_b is not None
        assert event_b.metadata.message_id == envelope.metadata.message_id

    def test_unsubscribe_stops_delivery(self) -> None:
        driver = LocalPubSubDriver()
        topic = _topic()

        handle = driver.subscribe(_subscription(subscriber_id="sub-x"))
        driver.unsubscribe(handle)

        envelope = _envelope(message_type=topic.name)
        driver.publish(topic, envelope)

        result = driver.next_event(handle, timeout=timedelta(milliseconds=100))
        assert result is None


# ---------------------------------------------------------------------------
# Test: consumer group - at-most-one delivery
# ---------------------------------------------------------------------------


class TestLocalPubSubConsumerGroup:
    """A consumer group partitions delivery so each event reaches one member."""

    def test_one_member_receives_the_event(self) -> None:
        driver = LocalPubSubDriver()
        topic = _topic()

        group = ConsumerGroup(name="cg-1", topic=topic)
        driver.create_consumer_group(group)

        driver.assign_consumer(ConsumerGroupAssignment("cg-1", "member-a", 0, 1))

        handle = driver.subscribe(_subscription(subscriber_id="member-a", consumer_group="cg-1"))

        envelope = _envelope(message_type=topic.name)
        driver.publish(topic, envelope)

        event = driver.next_event(handle, timeout=timedelta(seconds=1))
        assert event is not None
        assert event.metadata.message_id == envelope.metadata.message_id


# ---------------------------------------------------------------------------
# Test: replay from beginning
# ---------------------------------------------------------------------------


class TestLocalPubSubReplay:
    """A subscription with replay_from=beginning receives past events."""

    def test_replay_delivers_past_events(self) -> None:
        driver = LocalPubSubDriver()
        topic = _topic()

        envelope1 = _envelope(message_type=topic.name)
        envelope2 = _envelope(message_type=topic.name)
        driver.publish(topic, envelope1)
        driver.publish(topic, envelope2)

        handle = driver.subscribe(
            _subscription(subscriber_id="sub-replay", replay_from=ReplayCursor.BEGINNING)
        )

        e1 = driver.next_event(handle, timeout=timedelta(seconds=1))
        e2 = driver.next_event(handle, timeout=timedelta(seconds=1))

        assert e1 is not None
        assert e1.metadata.message_id == envelope1.metadata.message_id
        assert e2 is not None
        assert e2.metadata.message_id == envelope2.metadata.message_id


# ---------------------------------------------------------------------------
# Test: checkpoint and recovery
# ---------------------------------------------------------------------------


class TestLocalPubSubCheckpoint:
    """Checkpointing allows a subscriber to resume from a saved position."""

    def test_checkpoint_skips_already_consumed(self) -> None:
        driver = LocalPubSubDriver()
        topic = _topic()

        envelope1 = _envelope(message_type=topic.name)
        envelope2 = _envelope(message_type=topic.name)
        driver.publish(topic, envelope1)
        driver.publish(topic, envelope2)

        handle = driver.subscribe(
            _subscription(subscriber_id="sub-cp", replay_from=ReplayCursor.BEGINNING)
        )

        e1 = driver.next_event(handle, timeout=timedelta(seconds=1))
        assert e1 is not None

        driver.checkpoint(
            CheckpointCursor(
                topic=topic,
                subscriber_id="sub-cp",
                sequence=1,
                committed_at=datetime.now(UTC),
            )
        )

        e_next = driver.next_event(handle, timeout=timedelta(seconds=1))
        assert e_next is not None
        assert e_next.metadata.message_id == envelope2.metadata.message_id


# ---------------------------------------------------------------------------
# Test: schema validation
# ---------------------------------------------------------------------------


class TestPubSubSchema:
    """Pub-sub schema rejects invalid identifiers and policy values."""

    def test_invalid_topic_name(self) -> None:
        with pytest.raises(InvalidPubSubSchema):
            PubSubTopic("INVALID NAME!")

    def test_invalid_subscriber_id(self) -> None:
        with pytest.raises(InvalidPubSubSchema):
            PubSubSubscription(topic=_topic(), subscriber_id="")

    def test_missing_auth_token_for_private_channel(self) -> None:
        from af.messaging.pubsub import BroadcastChannelKind, BroadcastSubscription

        with pytest.raises(InvalidPubSubSchema):
            BroadcastSubscription(
                channel="room-1",
                client_id="client-1",
                kind=BroadcastChannelKind.PRIVATE,
            )


# ---------------------------------------------------------------------------
# Test: LocalBroadcastManager - public channel
# ---------------------------------------------------------------------------


class TestLocalBroadcastManagerPublic:
    """Public channels deliver to all registered clients without auth."""

    def test_publish_to_empty_channel(self) -> None:
        mgr = LocalBroadcastManager()
        envelope = _envelope()
        report = mgr.publish(channel="general", envelope=envelope)

        assert report.channel == "general"
        assert report.clients_reached == 0
        assert report.payload_bytes > 0

    def test_publish_reaches_registered_clients(self) -> None:
        mgr = LocalBroadcastManager()
        sub1 = BroadcastSubscription(channel="general", client_id="c1")
        sub2 = BroadcastSubscription(channel="general", client_id="c2")
        mgr.register_client(sub1)
        mgr.register_client(sub2)

        envelope = _envelope()
        report = mgr.publish(channel="general", envelope=envelope)

        assert report.clients_reached == 2

    def test_multiple_channels_are_isolated(self) -> None:
        mgr = LocalBroadcastManager()
        mgr.register_client(BroadcastSubscription(channel="room-a", client_id="c1"))
        mgr.register_client(BroadcastSubscription(channel="room-b", client_id="c2"))

        report_a = mgr.publish(channel="room-a", envelope=_envelope())
        report_b = mgr.publish(channel="room-b", envelope=_envelope())

        assert report_a.clients_reached == 1
        assert report_b.clients_reached == 1


# ---------------------------------------------------------------------------
# Test: LocalBroadcastManager - private channel auth
# ---------------------------------------------------------------------------


class TestLocalBroadcastManagerPrivate:
    """Private channels require a valid auth token."""

    def test_rejects_missing_auth_token(self) -> None:
        mgr = LocalBroadcastManager()
        with pytest.raises(PubSubOperationError):
            mgr.publish(
                channel="secret",
                envelope=_envelope(),
                kind=BroadcastChannelKind.PRIVATE,
            )

    def test_rejects_invalid_auth_token(self) -> None:
        mgr = LocalBroadcastManager()
        mgr.register_auth_token("secret", "real-token")
        with pytest.raises(PubSubOperationError):
            mgr.publish(
                channel="secret",
                envelope=_envelope(),
                kind=BroadcastChannelKind.PRIVATE,
                auth_token="wrong-token",
            )

    def test_rejects_unauthorized_registration_before_mutation(self) -> None:
        mgr = LocalBroadcastManager()
        subscription = BroadcastSubscription(
            channel="secret",
            client_id="client-secret",
            kind=BroadcastChannelKind.PRIVATE,
            auth_token="forged-token",
        )

        with pytest.raises(PubSubOperationError) as captured:
            mgr.register_client(subscription)

        assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED
        mgr.register_auth_token("secret", "real-token")
        report = mgr.publish(
            channel="secret",
            envelope=_envelope(),
            kind=BroadcastChannelKind.PRIVATE,
            auth_token="real-token",
        )
        assert report.clients_reached == 0

    def test_private_publish_excludes_public_subscription_with_same_channel(self) -> None:
        mgr = LocalBroadcastManager()
        mgr.register_client(BroadcastSubscription(channel="secret", client_id="public-client"))
        mgr.register_auth_token("secret", "real-token")

        report = mgr.publish(
            channel="secret",
            envelope=_envelope(),
            kind=BroadcastChannelKind.PRIVATE,
            auth_token="real-token",
        )

        assert report.clients_reached == 0

    def test_accepts_valid_auth_token(self) -> None:
        mgr = LocalBroadcastManager()
        mgr.register_auth_token("secret", "real-token")
        mgr.register_client(
            BroadcastSubscription(
                channel="secret",
                client_id="c1",
                kind=BroadcastChannelKind.PRIVATE,
                auth_token="real-token",
            )
        )

        report = mgr.publish(
            channel="secret",
            envelope=_envelope(),
            kind=BroadcastChannelKind.PRIVATE,
            auth_token="real-token",
        )

        assert report.clients_reached == 1


# ---------------------------------------------------------------------------
# Test: LocalBroadcastManager - presence channel
# ---------------------------------------------------------------------------


class TestLocalBroadcastManagerPresence:
    """Presence channels also require a valid auth token."""

    def test_rejects_missing_auth_token(self) -> None:
        mgr = LocalBroadcastManager()
        with pytest.raises(PubSubOperationError):
            mgr.publish(
                channel="presence-room",
                envelope=_envelope(),
                kind=BroadcastChannelKind.PRESENCE,
            )

    def test_rejects_invalid_auth_token(self) -> None:
        mgr = LocalBroadcastManager()
        mgr.register_auth_token("presence-room", "token-a")
        with pytest.raises(PubSubOperationError):
            mgr.publish(
                channel="presence-room",
                envelope=_envelope(),
                kind=BroadcastChannelKind.PRESENCE,
                auth_token="bad-token",
            )

    def test_rejects_unauthorized_registration_without_disclosure(self) -> None:
        mgr = LocalBroadcastManager()
        subscription = BroadcastSubscription(
            channel="presence-secret",
            client_id="client-secret",
            kind=BroadcastChannelKind.PRESENCE,
            auth_token="token-secret",
        )

        with pytest.raises(PubSubOperationError) as captured:
            mgr.register_client(subscription)

        assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED
        public_error = str(captured.value)
        assert "presence-secret" not in public_error
        assert "client-secret" not in public_error
        assert "token-secret" not in public_error


def test_broadcast_subscription_repr_redacts_auth_token() -> None:
    subscription = BroadcastSubscription(
        channel="secret",
        client_id="client",
        kind=BroadcastChannelKind.PRIVATE,
        auth_token="token-secret",
    )

    assert "token-secret" not in repr(subscription)


# ---------------------------------------------------------------------------
# Test: LocalBroadcastManager - subscription lifecycle
# ---------------------------------------------------------------------------


class TestLocalBroadcastManagerLifecycle:
    """Clients can be registered and unregistered."""

    def test_unregister_removes_client(self) -> None:
        mgr = LocalBroadcastManager()
        sub = BroadcastSubscription(channel="ch", client_id="c1")
        mgr.register_client(sub)

        report = mgr.publish(channel="ch", envelope=_envelope())
        assert report.clients_reached == 1

        removed = mgr.unregister_client(sub)
        assert removed is True

        report2 = mgr.publish(channel="ch", envelope=_envelope())
        assert report2.clients_reached == 0

    def test_unregister_unknown_returns_false(self) -> None:
        mgr = LocalBroadcastManager()
        sub = BroadcastSubscription(channel="ch", client_id="c1")
        result = mgr.unregister_client(sub)
        assert result is False


# ---------------------------------------------------------------------------
# Test: LocalBroadcastManager - dispatch report
# ---------------------------------------------------------------------------


class TestLocalBroadcastDispatchReport:
    """BroadcastDispatchReport carries correct fields."""

    def test_report_fields(self) -> None:
        mgr = LocalBroadcastManager()
        mgr.register_client(BroadcastSubscription(channel="ch", client_id="c1"))
        envelope = _envelope()
        report = mgr.publish(channel="ch", envelope=envelope)

        assert isinstance(report, BroadcastDispatchReport)
        assert report.channel == "ch"
        assert report.clients_reached == 1
        assert report.payload_bytes > 0


# ---------------------------------------------------------------------------
# Test: LocalStreamDriver - key ordering
# ---------------------------------------------------------------------------


class TestLocalStreamDriverKeyOrdering:
    """Messages with the same ordering key are sequenced deterministically."""

    def test_same_key_increments_sequence(self) -> None:
        driver = LocalStreamDriver()
        topic = _topic()

        driver.publish_ordered(topic, _envelope(message_type=topic.name), ordering_key="user-1")
        driver.publish_ordered(topic, _envelope(message_type=topic.name), ordering_key="user-1")

        state = driver._topics[topic.name]
        assert state.sequence == 2

    def test_different_keys_share_topic_sequence(self) -> None:
        driver = LocalStreamDriver()
        topic = _topic()

        driver.publish_ordered(topic, _envelope(message_type=topic.name), ordering_key="user-1")
        driver.publish_ordered(topic, _envelope(message_type=topic.name), ordering_key="user-2")

        state = driver._topics[topic.name]
        assert state.sequence == 2


# ---------------------------------------------------------------------------
# Test: LocalStreamDriver - global ordering
# ---------------------------------------------------------------------------


class TestLocalStreamDriverGlobalOrdering:
    """GLOBAL scope maintains a single cross-topic sequence counter."""

    def test_global_counter_increments(self) -> None:
        driver = LocalStreamDriver()
        topic = _topic()

        driver.publish_ordered(
            topic,
            _envelope(message_type=topic.name),
            ordering_key="k",
            scope=OrderingScope.GLOBAL,
        )
        driver.publish_ordered(
            topic,
            _envelope(message_type=topic.name),
            ordering_key="k",
            scope=OrderingScope.GLOBAL,
        )

        assert driver._global_sequence == 2

    def test_global_counter_shared_across_topics(self) -> None:
        driver = LocalStreamDriver()
        t1 = _topic("events.a")
        t2 = _topic("events.b")

        driver.publish_ordered(
            t1,
            _envelope(message_type=t1.name),
            ordering_key="k",
            scope=OrderingScope.GLOBAL,
        )
        driver.publish_ordered(
            t2,
            _envelope(message_type=t2.name),
            ordering_key="k",
            scope=OrderingScope.GLOBAL,
        )

        assert driver._global_sequence == 2


# ---------------------------------------------------------------------------
# Test: LocalStreamDriver - none scope
# ---------------------------------------------------------------------------


class TestLocalStreamDriverNoneScope:
    """NONE scope delegates to regular publish without ordering guarantees."""

    def test_none_scope_delegates_to_publish(self) -> None:
        driver = LocalStreamDriver()
        topic = _topic()

        handle = driver.subscribe(_subscription(subscriber_id="sub-1"))

        envelope = _envelope(message_type=topic.name)
        driver.publish_ordered(topic, envelope, ordering_key="k", scope=OrderingScope.NONE)

        event = driver.next_event(handle, timeout=timedelta(seconds=1))
        assert event is not None
        assert event.metadata.message_id == envelope.metadata.message_id


# ---------------------------------------------------------------------------
# Test: LocalStreamDriver - fan-out with ordering
# ---------------------------------------------------------------------------


class TestLocalStreamDriverFanOut:
    """Ordered publish still fans out to subscribers."""

    def test_subscriber_receives_ordered_event(self) -> None:
        driver = LocalStreamDriver()
        topic = _topic()

        handle = driver.subscribe(_subscription(subscriber_id="sub-1"))

        envelope = _envelope(message_type=topic.name)
        driver.publish_ordered(topic, envelope, ordering_key="k")

        event = driver.next_event(handle, timeout=timedelta(seconds=1))
        assert event is not None
        assert event.metadata.message_id == envelope.metadata.message_id


# ---------------------------------------------------------------------------
# Test: LocalStreamDriver - extends LocalPubSubDriver
# ---------------------------------------------------------------------------


class TestLocalStreamDriverExtendsPubSub:
    """LocalStreamDriver inherits all LocalPubSubDriver functionality."""

    def test_subscribe_and_publish(self) -> None:
        driver = LocalStreamDriver()
        topic = _topic()

        handle = driver.subscribe(_subscription(subscriber_id="sub-1"))
        envelope = _envelope(message_type=topic.name)
        driver.publish(topic, envelope)

        event = driver.next_event(handle, timeout=timedelta(seconds=1))
        assert event is not None
        assert event.metadata.message_id == envelope.metadata.message_id

    def test_consumer_group_delivery(self) -> None:
        driver = LocalStreamDriver()
        topic = _topic()

        group = ConsumerGroup(name="cg-1", topic=topic)
        driver.create_consumer_group(group)
        driver.assign_consumer(ConsumerGroupAssignment("cg-1", "member-a", 0, 1))

        handle = driver.subscribe(_subscription(subscriber_id="member-a", consumer_group="cg-1"))

        envelope = _envelope(message_type=topic.name)
        driver.publish(topic, envelope)

        event = driver.next_event(handle, timeout=timedelta(seconds=1))
        assert event is not None
        assert event.metadata.message_id == envelope.metadata.message_id
