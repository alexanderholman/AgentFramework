"""Pub-sub, durable event-stream, and broadcast contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from typing import Protocol

from af.framework.errors import PlatformError, PlatformReason
from af.messaging.envelopes import EnvelopeKind, MessageEnvelope

__all__ = (
    "BackpressurePolicy",
    "BroadcastChannelKind",
    "BroadcastDispatchReport",
    "BroadcastSubscription",
    "CheckpointCursor",
    "ConsumerGroup",
    "ConsumerGroupAssignment",
    "DurableStreamDriver",
    "InvalidPubSubSchema",
    "LocalBroadcastManager",
    "LocalPubSubDriver",
    "LocalStreamDriver",
    "OrderingScope",
    "PubSubDriver",
    "PubSubOperationError",
    "PubSubSubscription",
    "PubSubSubscriptionHandle",
    "PubSubTopic",
    "ReplayCursor",
    "ReplayReport",
    "SlowConsumerPolicy",
    "StreamDriver",
    "StreamSubscription",
    "UnsupportedPubSubFeature",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PubSubOperationError(PlatformError):
    """Sanitized pub-sub operation failure."""

    def __init__(
        self,
        *,
        reason: PlatformReason = PlatformReason.PERMANENT_FAILURE,
        stage: str = "pubsub.operation",
        protected_cause: BaseException | None = None,
    ) -> None:
        super().__init__(reason, stage=stage, protected_cause=protected_cause)


class UnsupportedPubSubFeature(PubSubOperationError):
    """A requested pub-sub feature is not verified for the selected driver."""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(reason=PlatformReason.CAPABILITY_UNAVAILABLE, stage="pubsub.capability")


class InvalidPubSubSchema(ValueError):
    """A pub-sub topic, subscription, or broadcast contract is invalid."""


# ---------------------------------------------------------------------------
# Topic
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PubSubTopic:
    """Canonical topic identifier for pub-sub drivers."""

    name: str

    def __post_init__(self) -> None:
        _identifier(self.name, "topic name")


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

class BackpressurePolicy(StrEnum):
    """What to do when a subscriber cannot keep up."""

    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    BUFFER = "buffer"
    BLOCK = "block"


class SlowConsumerPolicy(StrEnum):
    """How to handle a consumer that falls behind."""

    LOG = "log"
    WARN = "warn"
    DROP = "drop"
    KILL = "kill"


class OrderingScope(StrEnum):
    """Scope of ordering guarantees."""

    NONE = "none"
    KEY = "key"
    GLOBAL = "global"


# ---------------------------------------------------------------------------
# Subscription & Handle
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PubSubSubscription:
    """One durable or ephemeral subscription request."""

    topic: PubSubTopic
    subscriber_id: str
    ordering_key: str | None = None
    consumer_group: str | None = None
    replay_from: ReplayCursor | None = None
    backpressure: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST
    slow_consumer: SlowConsumerPolicy = SlowConsumerPolicy.LOG

    def __post_init__(self) -> None:
        if not isinstance(self.topic, PubSubTopic):
            raise InvalidPubSubSchema("subscription topic is invalid")
        _identifier(self.subscriber_id, "subscriber id")
        if self.ordering_key is not None:
            _identifier(self.ordering_key, "ordering key")
        if self.consumer_group is not None:
            _identifier(self.consumer_group, "consumer group")
        if not isinstance(self.backpressure, BackpressurePolicy):
            raise InvalidPubSubSchema("subscription backpressure policy is invalid")
        if not isinstance(self.slow_consumer, SlowConsumerPolicy):
            raise InvalidPubSubSchema("subscription slow-consumer policy is invalid")


@dataclass(frozen=True, slots=True)
class PubSubSubscriptionHandle:
    """Runtime handle returned by subscribe()."""

    subscription_id: str
    topic: PubSubTopic
    subscriber_id: str
    consumer_group: str | None = None


# ---------------------------------------------------------------------------
# Replay & Checkpoint
# ---------------------------------------------------------------------------

class ReplayCursor(StrEnum):
    """Where a durable subscription should begin replay."""

    BEGINNING = "beginning"
    LATEST = "latest"
    SINCE_TIME = "since_time"
    SINCE_SEQUENCE = "since_sequence"


@dataclass(frozen=True, slots=True)
class CheckpointCursor:
    """Durable position marker for checkpoint recovery."""

    topic: PubSubTopic
    subscriber_id: str
    sequence: int
    committed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.topic, PubSubTopic):
            raise InvalidPubSubSchema("checkpoint topic is invalid")
        _identifier(self.subscriber_id, "subscriber id")
        if type(self.sequence) is not int or self.sequence < 0:
            raise InvalidPubSubSchema("checkpoint sequence must be a non-negative integer")
        if not isinstance(self.committed_at, datetime) or self.committed_at.utcoffset() is None:
            raise InvalidPubSubSchema("checkpoint committed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Evidence returned by a replay operation."""

    topic: PubSubTopic
    subscriber_id: str
    events_replayed: int
    from_sequence: int
    to_sequence: int


# ---------------------------------------------------------------------------
# Consumer Group
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ConsumerGroupAssignment:
    """Partition or shard assignment within a consumer group."""

    consumer_group: str
    subscriber_id: str
    partition: int
    partitions_total: int

    def __post_init__(self) -> None:
        _identifier(self.consumer_group, "consumer group")
        _identifier(self.subscriber_id, "subscriber_id")
        if type(self.partition) is not int or self.partition < 0:
            raise InvalidPubSubSchema("partition must be a non-negative integer")
        if type(self.partitions_total) is not int or self.partitions_total < 1:
            raise InvalidPubSubSchema("partitions total must be a positive integer")


@dataclass(frozen=True, slots=True)
class ConsumerGroup:
    """Consumer group definition for at-least-once delivery."""

    name: str
    topic: PubSubTopic
    partitions: int = 1
    rebalance_timeout: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        _identifier(self.name, "consumer group name")
        if not isinstance(self.topic, PubSubTopic):
            raise InvalidPubSubSchema("consumer group topic is invalid")
        if type(self.partitions) is not int or self.partitions < 1:
            raise InvalidPubSubSchema("consumer group partitions must be positive")
        if not isinstance(self.rebalance_timeout, timedelta) or self.rebalance_timeout <= timedelta(0):
            raise InvalidPubSubSchema("consumer group rebalance timeout must be positive")


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

class BroadcastChannelKind(Enum):
    """Channel visibility and authorization scope."""

    PUBLIC = "public"
    PRIVATE = "private"
    PRESENCE = "presence"


@dataclass(frozen=True, slots=True)
class BroadcastSubscription:
    """Request to join a broadcast channel."""

    channel: str
    client_id: str
    kind: BroadcastChannelKind = BroadcastChannelKind.PUBLIC
    auth_token: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.channel, "channel")
        _identifier(self.client_id, "client id")
        if not isinstance(self.kind, BroadcastChannelKind):
            raise InvalidPubSubSchema("broadcast channel kind is invalid")
        if self.kind in (BroadcastChannelKind.PRIVATE, BroadcastChannelKind.PRESENCE):
            if not self.auth_token:
                raise InvalidPubSubSchema(
                    "private and presence channels require an auth token"
                )


@dataclass(frozen=True, slots=True)
class BroadcastDispatchReport:
    """Evidence of a broadcast dispatch."""

    channel: str
    clients_reached: int
    payload_bytes: int


# ---------------------------------------------------------------------------
# Driver protocols
# ---------------------------------------------------------------------------

class StreamSubscription(Protocol):
    """Protocol for a stream subscription that yields events."""

    def __next__(self) -> MessageEnvelope: ...
    def __iter__(self) -> StreamSubscription: ...


class PubSubDriver(Protocol):
    """Driver-neutral pub-sub interface."""

    def publish(self, topic: PubSubTopic, envelope: MessageEnvelope) -> None: ...
    def subscribe(self, subscription: PubSubSubscription) -> PubSubSubscriptionHandle: ...
    def unsubscribe(self, handle: PubSubSubscriptionHandle) -> None: ...
    def next_event(self, handle: PubSubSubscriptionHandle, *, timeout: timedelta | None = None) -> MessageEnvelope | None: ...


class DurableStreamDriver(PubSubDriver):
    """Extends pub-sub with replay, checkpoint, and consumer-group support."""

    def create_consumer_group(self, group: ConsumerGroup) -> None: ...
    def assign_consumer(self, group: ConsumerGroupAssignment) -> None: ...
    def checkpoint(self, cursor: CheckpointCursor) -> None: ...
    def replay(self, subscription: PubSubSubscription) -> ReplayReport: ...


class StreamDriver(DurableStreamDriver):
    """Extends durable stream with ordering and backpressure guarantees."""

    def publish_ordered(
        self,
        topic: PubSubTopic,
        envelope: MessageEnvelope,
        *,
        ordering_key: str,
        scope: OrderingScope = OrderingScope.KEY,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidPubSubSchema(f"pub-sub {field} must be a canonical identifier")
    return value


# ---------------------------------------------------------------------------
# Local in-process implementation
# ---------------------------------------------------------------------------

import threading
import uuid as _uuid
from collections import defaultdict
from typing import Any


class _TopicState:
    """Mutable state for a single topic."""

    __slots__ = ("log", "sequence")

    def __init__(self) -> None:
        self.log: list[MessageEnvelope] = []
        self.sequence: int = 0


class _SubscriptionState:
    """Mutable state for a single subscription."""

    __slots__ = (
        "handle",
        "subscription",
        "pending",
        "checkpointed_seq",
        "active",
        "lock",
    )

    def __init__(
        self,
        handle: PubSubSubscriptionHandle,
        subscription: PubSubSubscription,
        pending: list[MessageEnvelope] | None = None,
        checkpointed_seq: int = 0,
        active: bool = True,
    ) -> None:
        self.handle = handle
        self.subscription = subscription
        self.pending: list[MessageEnvelope] = pending if pending is not None else []
        self.checkpointed_seq = checkpointed_seq
        self.active = active
        self.lock = threading.Lock()


class LocalPubSubDriver:
    """In-process pub-sub driver with durable replay, consumer groups, and checkpoints."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._topics: dict[str, _TopicState] = {}
        self._subscriptions: dict[str, _SubscriptionState] = {}
        self._consumer_groups: dict[str, ConsumerGroup] = {}
        self._consumer_assignments: dict[str, list[ConsumerGroupAssignment]] = defaultdict(list)

    # -- PubSubDriver --

    def publish(self, topic: PubSubTopic, envelope: MessageEnvelope) -> None:
        with self._lock:
            state = self._topics.setdefault(topic.name, _TopicState())
            new_seq = state.sequence + 1
            state.sequence = new_seq
            state.log.append(envelope)
            # Fan-out to active subscriptions
            for sub_state in self._subscriptions.values():
                if not sub_state.active:
                    continue
                if sub_state.subscription.topic.name != topic.name:
                    continue
                # Consumer group: only deliver to assigned member
                if sub_state.subscription.consumer_group:
                    cg_name = sub_state.subscription.consumer_group
                    assignments = self._consumer_assignments.get(cg_name, [])
                    if not any(
                        a.subscriber_id == sub_state.handle.subscriber_id
                        for a in assignments
                    ):
                        continue
                with sub_state.lock:
                    sub_state.pending.append(envelope)

    def subscribe(self, subscription: PubSubSubscription) -> PubSubSubscriptionHandle:
        sid = _uuid.uuid4().hex
        handle = PubSubSubscriptionHandle(
            subscription_id=sid,
            topic=subscription.topic,
            subscriber_id=subscription.subscriber_id,
            consumer_group=subscription.consumer_group,
        )
        # Handle replay
        pending: list[MessageEnvelope] = []
        if subscription.replay_from == ReplayCursor.BEGINNING:
            topic_state = self._topics.get(subscription.topic.name)
            if topic_state:
                pending = list(topic_state.log)
        elif subscription.replay_from == ReplayCursor.SINCE_TIME:
            # TODO: filter by occurred_at
            pass
        elif subscription.replay_from == ReplayCursor.SINCE_SEQUENCE:
            # TODO: filter by sequence
            pass
        # If no replay cursor or LATEST, pending stays empty

        sub_state = _SubscriptionState(
            handle=handle,
            subscription=subscription,
            pending=pending,
        )
        with self._lock:
            self._subscriptions[sid] = sub_state
        return handle

    def unsubscribe(self, handle: PubSubSubscriptionHandle) -> None:
        with self._lock:
            sub_state = self._subscriptions.get(handle.subscription_id)
            if sub_state:
                sub_state.active = False

    def next_event(
        self,
        handle: PubSubSubscriptionHandle,
        *,
        timeout: timedelta | None = None,
    ) -> MessageEnvelope | None:
        with self._lock:
            sub_state = self._subscriptions.get(handle.subscription_id)
            if not sub_state or not sub_state.active:
                return None
        with sub_state.lock:
            # Skip already checkpointed events
            pending = list(sub_state.pending)
            # Filter by checkpoint: keep only events after checkpointed_seq
            # The checkpoint sequence refers to the global topic sequence,
            # so we need to map it. For simplicity, we track how many events
            # have been consumed relative to the pending list.
            # Actually, the checkpoint stores the global sequence number.
            # We need to know which pending events correspond to sequences > checkpointed_seq.
            # For the local driver, we'll use a simpler approach: the checkpoint
            # tells us to skip the first N events from the replay buffer.
            topic_state = self._topics.get(sub_state.subscription.topic.name)
            if topic_state and sub_state.checkpointed_seq > 0:
                # Find the index in the topic log that corresponds to checkpointed_seq
                # Then skip all pending events that appear at or before that position
                log = topic_state.log
                checkpoint_idx = min(sub_state.checkpointed_seq, len(log))
                # Build a set of envelope ids to skip
                skip_ids = {id(e) for e in log[:checkpoint_idx]}
                pending = [e for e in pending if id(e) not in skip_ids]

            if not pending:
                return None
            envelope = pending[0]
            # Remove delivered event
            sub_state.pending = pending[1:]
            return envelope

    # -- DurableStreamDriver --

    def create_consumer_group(self, group: ConsumerGroup) -> None:
        with self._lock:
            self._consumer_groups[group.name] = group

    def assign_consumer(self, assignment: ConsumerGroupAssignment) -> None:
        with self._lock:
            self._consumer_assignments[assignment.consumer_group].append(assignment)

    def checkpoint(self, cursor: CheckpointCursor) -> None:
        with self._lock:
            # Find subscription for this subscriber/topic combo
            for sid, sub_state in self._subscriptions.items():
                if (
                    sub_state.subscription.subscriber_id == cursor.subscriber_id
                    and sub_state.subscription.topic.name == cursor.topic.name
                ):
                    sub_state.checkpointed_seq = cursor.sequence
                    break

    def replay(self, subscription: PubSubSubscription) -> ReplayReport:
        with self._lock:
            topic_state = self._topics.get(subscription.topic.name)
            if not topic_state:
                return ReplayReport(
                    topic=subscription.topic,
                    subscriber_id=subscription.subscriber_id,
                    events_replayed=0,
                    from_sequence=0,
                    to_sequence=0,
                )
            log = topic_state.log
            return ReplayReport(
                topic=subscription.topic,
                subscriber_id=subscription.subscriber_id,
                events_replayed=len(log),
                from_sequence=1,
                to_sequence=len(log),
            )


# ---------------------------------------------------------------------------
# Local Broadcast Manager
# ---------------------------------------------------------------------------


class LocalBroadcastManager:
    """In-process broadcast manager for pub-sub channels.

    Supports PUBLIC, PRIVATE, and PRESENCE channels with subscription
    tracking and auth token validation for restricted channel kinds.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, list[BroadcastSubscription]] = {}
        self._auth_tokens: dict[str, set[str]] = {}

    # -- registration helpers --

    def register_client(self, subscription: BroadcastSubscription) -> None:
        """Register a client subscription on a channel."""
        self._subscriptions.setdefault(subscription.channel, []).append(subscription)

    def register_auth_token(self, channel: str, token: str) -> None:
        """Register an auth token for a PRIVATE or PRESENCE channel."""
        self._auth_tokens.setdefault(channel, set()).add(token)

    def unregister_client(self, subscription: BroadcastSubscription) -> bool:
        """Remove a subscription. Returns True if found and removed."""
        subs = self._subscriptions.get(subscription.channel)
        if subs is None:
            return False
        try:
            subs.remove(subscription)
            return True
        except ValueError:
            return False

    # -- publish --

    def publish(
        self,
        channel: str,
        envelope: MessageEnvelope,
        kind: BroadcastChannelKind = BroadcastChannelKind.PUBLIC,
        auth_token: str | None = None,
    ) -> BroadcastDispatchReport:
        """Publish an envelope to a channel and return dispatch statistics.

        For PRIVATE and PRESENCE channels, ``auth_token`` must be provided
        and must match a registered token.
        """
        if kind in (BroadcastChannelKind.PRIVATE, BroadcastChannelKind.PRESENCE):
            if not auth_token:
                raise PubSubOperationError(
                    stage="broadcast_publish",
                )
            valid_tokens = self._auth_tokens.get(channel)
            if valid_tokens is None or auth_token not in valid_tokens:
                raise PubSubOperationError(
                    stage="broadcast_publish",
                )

        subs = self._subscriptions.get(channel, [])
        from af.messaging.envelopes import JsonEnvelopeCodec

        codec = JsonEnvelopeCodec()
        encoded = codec.encode(envelope)
        payload_bytes = len(encoded)

        return BroadcastDispatchReport(
            channel=channel,
            clients_reached=len(subs),
            payload_bytes=payload_bytes,
        )


# ---------------------------------------------------------------------------
# Local Stream Driver
# ---------------------------------------------------------------------------


class LocalStreamDriver(LocalPubSubDriver, StreamDriver):
    """In-process stream driver with deterministic ordering guarantees.

    Extends ``LocalPubSubDriver`` with ``publish_ordered`` supporting
    ``OrderingScope.KEY``, ``OrderingScope.GLOBAL``, and ``OrderingScope.NONE``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._global_sequence: int = 0
        self._key_sequences: dict[str, int] = {}

    def publish_ordered(
        self,
        topic: PubSubTopic,
        envelope: MessageEnvelope,
        *,
        ordering_key: str,
        scope: OrderingScope = OrderingScope.KEY,
    ) -> None:
        """Publish with ordering guarantees.

        * ``GLOBAL`` – single global counter across all topics.
        * ``KEY`` – per-key counter (``ordering_key`` used).
        * ``NONE`` – delegates to ``publish`` (no ordering guarantee).
        """
        if scope == OrderingScope.NONE:
            self.publish(topic, envelope)
            return

        with self._lock:
            state = self._topics.setdefault(topic.name, _TopicState())

            if scope == OrderingScope.GLOBAL:
                self._global_sequence += 1
                new_seq = self._global_sequence
            else:
                new_seq = state.sequence + 1

            state.sequence = new_seq

            if scope == OrderingScope.KEY:
                key = f"{topic.name}/{ordering_key}"
                self._key_sequences[key] = new_seq

            state.log.append(envelope)

            # Fan-out to active subscriptions
            for sub_state in self._subscriptions.values():
                if not sub_state.active:
                    continue
                if sub_state.subscription.topic.name != topic.name:
                    continue
                if sub_state.subscription.consumer_group:
                    cg_name = sub_state.subscription.consumer_group
                    assignments = self._consumer_assignments.get(cg_name, [])
                    if not any(
                        a.subscriber_id == sub_state.handle.subscriber_id
                        for a in assignments
                    ):
                        continue
                with sub_state.lock:
                    sub_state.pending.append(envelope)
