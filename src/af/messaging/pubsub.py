"""Pub-sub, durable event-stream, and broadcast contracts."""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid as _uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from typing import Protocol

from af.framework.errors import PlatformError, PlatformReason
from af.messaging.envelopes import MessageEnvelope

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
    backpressure: BackpressurePolicy = BackpressurePolicy.BUFFER
    slow_consumer: SlowConsumerPolicy = SlowConsumerPolicy.LOG
    since_time: datetime | None = None
    since_sequence: int | None = None
    max_pending: int = 1024

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
        if self.replay_from is not None and not isinstance(self.replay_from, ReplayCursor):
            raise InvalidPubSubSchema("subscription replay cursor is invalid")
        if self.replay_from is ReplayCursor.SINCE_TIME:
            if self.since_time is None or self.since_time.utcoffset() is None:
                raise InvalidPubSubSchema("time replay requires an aware since_time")
        elif self.since_time is not None:
            raise InvalidPubSubSchema("since_time requires time replay")
        if self.replay_from is ReplayCursor.SINCE_SEQUENCE:
            if type(self.since_sequence) is not int or self.since_sequence < 0:
                raise InvalidPubSubSchema("sequence replay requires a nonnegative sequence")
        elif self.since_sequence is not None:
            raise InvalidPubSubSchema("since_sequence requires sequence replay")
        if type(self.max_pending) is not int or not 1 <= self.max_pending <= 100_000:
            raise InvalidPubSubSchema("subscription backlog must be bounded")


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
    ownership_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.topic, PubSubTopic):
            raise InvalidPubSubSchema("checkpoint topic is invalid")
        _identifier(self.subscriber_id, "subscriber id")
        if type(self.sequence) is not int or self.sequence < 0:
            raise InvalidPubSubSchema("checkpoint sequence must be a non-negative integer")
        if not isinstance(self.committed_at, datetime) or self.committed_at.utcoffset() is None:
            raise InvalidPubSubSchema("checkpoint committed_at must be timezone-aware")
        if self.ownership_token is not None and (
            type(self.ownership_token) is not str
            or not 1 <= len(self.ownership_token) <= 128
            or not self.ownership_token.isascii()
            or not self.ownership_token.isalnum()
        ):
            raise InvalidPubSubSchema("checkpoint ownership token is invalid")


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
        if not isinstance(self.rebalance_timeout, timedelta) or self.rebalance_timeout <= timedelta(
            0
        ):
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
    auth_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _identifier(self.channel, "channel")
        _identifier(self.client_id, "client id")
        if not isinstance(self.kind, BroadcastChannelKind):
            raise InvalidPubSubSchema("broadcast channel kind is invalid")
        if (
            self.kind in (BroadcastChannelKind.PRIVATE, BroadcastChannelKind.PRESENCE)
            and not self.auth_token
        ):
            raise InvalidPubSubSchema("private and presence channels require an auth token")


@dataclass(frozen=True, slots=True)
class BroadcastDispatchReport:
    """Evidence of a broadcast dispatch."""

    channel: str
    clients_reached: int
    payload_bytes: int


@dataclass(frozen=True, slots=True)
class _BroadcastRecipient:
    """Credential-free recipient retained after subscription authorization."""

    channel: str
    client_id: str
    kind: BroadcastChannelKind

    @classmethod
    def from_subscription(cls, subscription: BroadcastSubscription) -> _BroadcastRecipient:
        return cls(
            channel=subscription.channel,
            client_id=subscription.client_id,
            kind=subscription.kind,
        )


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
    def next_event(
        self, handle: PubSubSubscriptionHandle, *, timeout: timedelta | None = None
    ) -> MessageEnvelope | None: ...


class DurableStreamDriver(PubSubDriver, Protocol):
    """Extends pub-sub with replay, checkpoint, and consumer-group support."""

    def create_consumer_group(self, group: ConsumerGroup) -> None: ...
    def assign_consumer(self, group: ConsumerGroupAssignment) -> None: ...
    def checkpoint(self, cursor: CheckpointCursor) -> None: ...
    def replay(self, subscription: PubSubSubscription) -> ReplayReport: ...


class StreamDriver(DurableStreamDriver, Protocol):
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


class _TopicState:
    def __init__(self) -> None:
        self.log: list[tuple[int, MessageEnvelope, str | None]] = []
        self.sequence = 0


class _SubscriptionState:
    def __init__(
        self,
        handle: PubSubSubscriptionHandle,
        subscription: PubSubSubscription,
        pending: list[tuple[int, MessageEnvelope, str | None]],
    ) -> None:
        self.handle = handle
        self.subscription = subscription
        self.pending = pending
        self.checkpointed_seq = 0
        self.delivered_seq = 0


class LocalPubSubDriver:
    """Bounded in-process pub-sub. Restart loses every event and checkpoint.

    For durable recovery use a persistent stream adapter. Local BLOCK policy
    refuses subscription because synchronous volatile delivery has no wait lease.
    """

    def __init__(self, *, max_events: int = 10_000, max_subscriptions: int = 1024) -> None:
        if any(type(n) is not int or n < 1 for n in (max_events, max_subscriptions)):
            raise InvalidPubSubSchema("local stream bounds must be positive")
        self._max_events = max_events
        self._max_subscriptions = max_subscriptions
        self._lock = threading.Lock()
        self._topics: dict[str, _TopicState] = {}
        self._subscriptions: dict[str, _SubscriptionState] = {}
        self._consumer_groups: dict[str, ConsumerGroup] = {}
        self._consumer_assignments: dict[str, list[ConsumerGroupAssignment]] = defaultdict(list)

    def publish(self, topic: PubSubTopic, envelope: MessageEnvelope) -> None:
        with self._lock:
            self._publish_locked(topic, envelope, None)

    def _matches(self, sub: PubSubSubscription, envelope: MessageEnvelope, key: str | None) -> bool:
        if sub.ordering_key is not None and sub.ordering_key != key:
            return False
        if sub.since_time is not None and envelope.metadata.occurred_at < sub.since_time:
            return False
        if sub.consumer_group:
            group = self._consumer_groups.get(sub.consumer_group)
            if group is None:
                return False
            partition = (
                int.from_bytes(
                    hashlib.sha256((key or envelope.metadata.message_id).encode()).digest()[:8],
                    "big",
                )
                % group.partitions
            )
            return any(
                a.subscriber_id == sub.subscriber_id and a.partition == partition
                for a in self._consumer_assignments[sub.consumer_group]
            )
        return True

    def _publish_locked(
        self, topic: PubSubTopic, envelope: MessageEnvelope, key: str | None
    ) -> None:
        if sum(len(state.log) for state in self._topics.values()) >= self._max_events:
            raise PubSubOperationError(stage="pubsub.retention_capacity")
        targets = [
            sub
            for sub in self._subscriptions.values()
            if sub.subscription.topic == topic and self._matches(sub.subscription, envelope, key)
        ]
        for sub in targets:
            if (
                len(sub.pending) >= sub.subscription.max_pending
                and sub.subscription.backpressure is BackpressurePolicy.BUFFER
            ):
                raise PubSubOperationError(stage="pubsub.subscriber_capacity")
        state = self._topics.setdefault(topic.name, _TopicState())
        state.sequence += 1
        item = (state.sequence, envelope, key)
        state.log.append(item)
        for sub in targets:
            if len(sub.pending) >= sub.subscription.max_pending:
                if sub.subscription.backpressure is BackpressurePolicy.DROP_NEWEST:
                    continue
                sub.pending.pop(0)
            sub.pending.append(item)

    def _replay_locked(
        self, subscription: PubSubSubscription
    ) -> list[tuple[int, MessageEnvelope, str | None]]:
        state = self._topics.get(subscription.topic.name)
        if state is None or subscription.replay_from is ReplayCursor.LATEST:
            return []
        pending = [
            item
            for item in state.log
            if item[0] > (subscription.since_sequence or 0)
            and self._matches(subscription, item[1], item[2])
        ]
        if len(pending) > subscription.max_pending:
            raise PubSubOperationError(stage="pubsub.replay_capacity")
        return pending

    def subscribe(self, subscription: PubSubSubscription) -> PubSubSubscriptionHandle:
        if subscription.backpressure is BackpressurePolicy.BLOCK:
            raise UnsupportedPubSubFeature("local_blocking_backpressure")
        if subscription.slow_consumer is not SlowConsumerPolicy.LOG:
            raise UnsupportedPubSubFeature("local_slow_consumer_action")
        with self._lock:
            if len(self._subscriptions) >= self._max_subscriptions:
                raise PubSubOperationError(stage="pubsub.subscription_capacity")
            if subscription.consumer_group:
                group = self._consumer_groups.get(subscription.consumer_group)
                if group is None or group.topic != subscription.topic:
                    raise PubSubOperationError(stage="pubsub.group_missing")
            sid = _uuid.uuid4().hex
            handle = PubSubSubscriptionHandle(
                sid, subscription.topic, subscription.subscriber_id, subscription.consumer_group
            )
            pending = self._replay_locked(subscription) if subscription.replay_from else []
            self._subscriptions[sid] = _SubscriptionState(handle, subscription, pending)
            return handle

    def unsubscribe(self, handle: PubSubSubscriptionHandle) -> None:
        with self._lock:
            state = self._subscriptions.get(handle.subscription_id)
            if state is not None and state.handle == handle:
                del self._subscriptions[handle.subscription_id]

    def next_event(
        self, handle: PubSubSubscriptionHandle, *, timeout: timedelta | None = None
    ) -> MessageEnvelope | None:
        seconds = timeout.total_seconds() if timeout else 0
        if not 0 <= seconds <= 30:
            raise InvalidPubSubSchema("local read timeout must be bounded")
        deadline = time.monotonic() + seconds
        while True:
            with self._lock:
                state = self._subscriptions.get(handle.subscription_id)
                if state is None or state.handle != handle:
                    return None
                if state.pending:
                    sequence, envelope, _ = state.pending.pop(0)
                    state.delivered_seq = sequence
                    return envelope
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def create_consumer_group(self, group: ConsumerGroup) -> None:
        with self._lock:
            existing = self._consumer_groups.get(group.name)
            if existing is not None and existing != group:
                raise PubSubOperationError(stage="pubsub.group_conflict")
            if existing is None and len(self._consumer_groups) >= self._max_subscriptions:
                raise PubSubOperationError(stage="pubsub.group_capacity")
            self._consumer_groups[group.name] = group

    def assign_consumer(self, assignment: ConsumerGroupAssignment) -> None:
        with self._lock:
            group = self._consumer_groups.get(assignment.consumer_group)
            if (
                group is None
                or assignment.partitions_total != group.partitions
                or assignment.partition >= group.partitions
            ):
                raise PubSubOperationError(stage="pubsub.assignment_invalid")
            assignments = self._consumer_assignments[assignment.consumer_group]
            for existing in assignments:
                if existing.partition == assignment.partition:
                    if existing != assignment:
                        raise PubSubOperationError(stage="pubsub.partition_owned")
                    return
            assignments.append(assignment)

    def checkpoint(self, cursor: CheckpointCursor) -> None:
        with self._lock:
            for state in self._subscriptions.values():
                if (
                    state.subscription.subscriber_id == cursor.subscriber_id
                    and state.subscription.topic == cursor.topic
                ):
                    if not state.checkpointed_seq <= cursor.sequence <= state.delivered_seq:
                        raise PubSubOperationError(stage="pubsub.checkpoint_undelivered")
                    state.checkpointed_seq = cursor.sequence
                    return
            raise PubSubOperationError(stage="pubsub.subscription_unknown")

    def replay(self, subscription: PubSubSubscription) -> ReplayReport:
        with self._lock:
            pending = self._replay_locked(subscription)
            return ReplayReport(
                subscription.topic,
                subscription.subscriber_id,
                len(pending),
                pending[0][0] if pending else 0,
                pending[-1][0] if pending else 0,
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
        self._subscriptions: dict[str, list[_BroadcastRecipient]] = {}
        self._auth_tokens: dict[tuple[str, BroadcastChannelKind], set[str]] = {}

    # -- registration helpers --

    def register_client(self, subscription: BroadcastSubscription) -> None:
        """Register a client subscription on a channel."""
        if subscription.kind in (
            BroadcastChannelKind.PRIVATE,
            BroadcastChannelKind.PRESENCE,
        ):
            self._require_authorized(
                subscription.channel,
                subscription.kind,
                subscription.auth_token,
                stage="broadcast_register",
            )
        recipient = _BroadcastRecipient.from_subscription(subscription)
        self._subscriptions.setdefault(subscription.channel, []).append(recipient)

    def register_auth_token(
        self,
        channel: str,
        token: str,
        *,
        kind: BroadcastChannelKind,
    ) -> None:
        """Register an auth token for a PRIVATE or PRESENCE channel."""
        if kind not in (BroadcastChannelKind.PRIVATE, BroadcastChannelKind.PRESENCE):
            raise InvalidPubSubSchema("auth tokens require a private or presence channel kind")
        self._auth_tokens.setdefault((channel, kind), set()).add(token)

    def unregister_client(self, subscription: BroadcastSubscription) -> bool:
        """Remove a subscription. Returns True if found and removed."""
        if subscription.kind in (
            BroadcastChannelKind.PRIVATE,
            BroadcastChannelKind.PRESENCE,
        ):
            self._require_authorized(
                subscription.channel,
                subscription.kind,
                subscription.auth_token,
                stage="broadcast_unregister",
            )
        subs = self._subscriptions.get(subscription.channel)
        if subs is None:
            return False
        recipient = _BroadcastRecipient.from_subscription(subscription)
        try:
            subs.remove(recipient)
            return True
        except ValueError:
            return False

    def _require_authorized(
        self,
        channel: str,
        kind: BroadcastChannelKind,
        token: str | None,
        *,
        stage: str,
    ) -> None:
        valid_tokens = self._auth_tokens.get((channel, kind))
        if not token or valid_tokens is None or token not in valid_tokens:
            raise PubSubOperationError(
                reason=PlatformReason.AUTHORIZATION_DENIED,
                stage=stage,
            )

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
            self._require_authorized(channel, kind, auth_token, stage="broadcast_publish")

        subs = [
            subscription
            for subscription in self._subscriptions.get(channel, [])
            if subscription.kind is kind
        ]
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

        * ``GLOBAL`` - single global counter across all topics.
        * ``KEY`` - per-key counter (``ordering_key`` used).
        * ``NONE`` - delegates to ``publish`` (no ordering guarantee).
        """
        _identifier(ordering_key, "ordering key")
        if not isinstance(scope, OrderingScope):
            raise InvalidPubSubSchema("stream ordering scope is invalid")
        with self._lock:
            self._publish_locked(topic, envelope, ordering_key)
            if scope is OrderingScope.GLOBAL:
                self._global_sequence += 1
            elif scope is OrderingScope.KEY:
                key = f"{topic.name}/{ordering_key}"
                self._key_sequences[key] = self._key_sequences.get(key, 0) + 1
