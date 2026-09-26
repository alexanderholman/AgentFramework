"""Bounded SQLite streams with durable cursors and fenced at-least-once delivery.

One event may be outstanding per subscriber partition. Checkpoint it before the
next delivery; expired deliveries are redelivered. Global SQLite insertion order
also supplies per-key order. This is a local persistent adapter, not Redis/Kafka.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from af.framework.errors import PlatformReason
from af.messaging.envelopes import JsonEnvelopeCodec, MessageEnvelope
from af.messaging.pubsub import (
    BackpressurePolicy,
    CheckpointCursor,
    ConsumerGroup,
    ConsumerGroupAssignment,
    InvalidPubSubSchema,
    OrderingScope,
    PubSubOperationError,
    PubSubSubscription,
    PubSubSubscriptionHandle,
    PubSubTopic,
    ReplayCursor,
    ReplayReport,
    SlowConsumerPolicy,
    UnsupportedPubSubFeature,
)


@dataclass(frozen=True, slots=True)
class StreamDelivery:
    """A leased delivery; its sequence is the checkpoint position."""

    sequence: int
    envelope: MessageEnvelope
    ownership_token: str
    partition: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS stream_events(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
 message_id TEXT NOT NULL, envelope BLOB NOT NULL, occurred REAL NOT NULL,
 ordering_key TEXT, partition_hash INTEGER NOT NULL, UNIQUE(topic,message_id));
CREATE INDEX IF NOT EXISTS stream_topic_sequence ON stream_events(topic,sequence);
CREATE TABLE IF NOT EXISTS stream_floors(topic TEXT PRIMARY KEY, sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS stream_subscriptions(
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, subscriber TEXT NOT NULL,
 config TEXT NOT NULL, consumer TEXT NOT NULL, group_name TEXT, ordering_key TEXT,
 since_time REAL, max_pending INTEGER NOT NULL, active INTEGER NOT NULL,
 UNIQUE(topic,subscriber));
CREATE TABLE IF NOT EXISTS stream_cursors(
 topic TEXT NOT NULL, consumer TEXT NOT NULL, partition INTEGER NOT NULL,
 committed INTEGER NOT NULL, delivered INTEGER, token TEXT, owner TEXT,
 expires REAL, generation INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(topic,consumer,partition));
CREATE TABLE IF NOT EXISTS stream_groups(
 name TEXT PRIMARY KEY, topic TEXT NOT NULL, partitions INTEGER NOT NULL,
 timeout REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stream_assignments(
 group_name TEXT NOT NULL, partition INTEGER NOT NULL, subscriber TEXT NOT NULL,
 generation INTEGER NOT NULL, expires REAL NOT NULL,
 PRIMARY KEY(group_name,partition));
"""


class SQLiteStreamDriver:
    """Persist events before exposing them and reject capacity/ownership loss.

    Retention is explicit: ``prune`` refuses to remove any uncheckpointed history.
    BUFFER fails immediately at a bound. BLOCK waits up to ``publish_timeout``.
    Dropping policies are unavailable because they cannot preserve at-least-once
    delivery. Subscriber identity/configuration survives unsubscribe and restart.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_events: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        max_subscriptions: int = 1024,
        visibility_timeout: timedelta = timedelta(seconds=30),
        publish_timeout: timedelta = timedelta(seconds=1),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if any(type(n) is not int or n < 1 for n in (max_events, max_bytes, max_subscriptions)):
            raise InvalidPubSubSchema("stream capacity must be positive")
        if not timedelta(0) < visibility_timeout <= timedelta(hours=1):
            raise InvalidPubSubSchema("stream visibility timeout must be bounded")
        if not timedelta(0) <= publish_timeout <= timedelta(seconds=30):
            raise InvalidPubSubSchema("stream publish timeout must be bounded")
        if str(path) == ":memory:":
            raise InvalidPubSubSchema("durable streams require a persistent file")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.max_subscriptions = max_subscriptions
        self.visibility_seconds = visibility_timeout.total_seconds()
        self.publish_seconds = publish_timeout.total_seconds()
        self.clock = clock
        self._owner = uuid.uuid4().hex
        self._codec = JsonEnvelopeCodec()
        self._receipts: dict[tuple[str, int], tuple[int, str]] = {}
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        except sqlite3.Error as exc:
            raise PubSubOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="streams.storage",
                protected_cause=exc,
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def _now(self) -> float:
        now = self.clock()
        if now.utcoffset() is None:
            raise InvalidPubSubSchema("stream clock must be timezone-aware")
        return now.timestamp()

    @staticmethod
    def _fail(stage: str) -> PubSubOperationError:
        return PubSubOperationError(stage=f"streams.{stage}")

    @staticmethod
    def _config(subscription: PubSubSubscription) -> str:
        if subscription.backpressure not in (BackpressurePolicy.BUFFER, BackpressurePolicy.BLOCK):
            raise UnsupportedPubSubFeature("durable_drop_policy")
        if subscription.slow_consumer is not SlowConsumerPolicy.LOG:
            raise UnsupportedPubSubFeature("slow_consumer_action")
        if subscription.consumer_group and (
            subscription.ordering_key is not None
            or subscription.replay_from not in (None, ReplayCursor.BEGINNING)
        ):
            raise UnsupportedPubSubFeature("per_member_group_replay")
        return json.dumps(
            {
                "key": subscription.ordering_key,
                "group": subscription.consumer_group,
                "replay": subscription.replay_from,
                "sequence": subscription.since_sequence,
                "time": subscription.since_time.isoformat() if subscription.since_time else None,
                "pending": subscription.max_pending,
                "backpressure": subscription.backpressure,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _floor(connection: sqlite3.Connection, topic: str) -> int:
        row = connection.execute(
            "SELECT sequence FROM stream_floors WHERE topic=?", (topic,)
        ).fetchone()
        return int(row[0]) if row else 0

    def _start(self, connection: sqlite3.Connection, subscription: PubSubSubscription) -> int:
        floor = self._floor(connection, subscription.topic.name)
        if subscription.replay_from in (None, ReplayCursor.LATEST):
            maximum = connection.execute(
                "SELECT MAX(sequence) FROM stream_events WHERE topic=?",
                (subscription.topic.name,),
            ).fetchone()[0]
            return max(floor, int(maximum or 0))
        start = subscription.since_sequence or 0
        if start < floor:
            raise self._fail("replay_gap")
        return start

    @staticmethod
    def _query(
        topic: str,
        start: int,
        ordering_key: str | None,
        since_time: float | None,
        partition: int = 0,
        partitions: int = 1,
    ) -> tuple[str, list[object]]:
        query = "topic=? AND sequence>? AND partition_hash % ? = ?"
        parameters: list[object] = [topic, start, partitions, partition]
        if ordering_key is not None:
            query += " AND ordering_key=?"
            parameters.append(ordering_key)
        if since_time is not None:
            query += " AND occurred>=?"
            parameters.append(since_time)
        return query, parameters

    def publish(self, topic: PubSubTopic, envelope: MessageEnvelope) -> None:
        self._publish(topic, envelope, None)

    def publish_ordered(
        self,
        topic: PubSubTopic,
        envelope: MessageEnvelope,
        *,
        ordering_key: str,
        scope: OrderingScope = OrderingScope.KEY,
    ) -> None:
        PubSubSubscription(topic, "validation", ordering_key=ordering_key)
        if not isinstance(scope, OrderingScope):
            raise InvalidPubSubSchema("stream ordering scope is invalid")
        self._publish(topic, envelope, ordering_key)

    def _publish(self, topic: PubSubTopic, envelope: MessageEnvelope, key: str | None) -> None:
        encoded = self._codec.encode(envelope)
        partition_hash = int.from_bytes(
            hashlib.sha256((key or envelope.metadata.message_id).encode()).digest()[:8],
            "big",
        ) & ((1 << 63) - 1)
        deadline = time.monotonic() + self.publish_seconds
        while True:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT envelope,ordering_key FROM stream_events WHERE topic=? AND "
                    "message_id=?",
                    (topic.name, envelope.metadata.message_id),
                ).fetchone()
                if existing:
                    if bytes(existing[0]) != encoded or existing[1] != key:
                        raise self._fail("message_conflict")
                    return
                totals = connection.execute(
                    "SELECT COUNT(*),COALESCE(SUM(length(envelope)),0) FROM stream_events"
                ).fetchone()
                if (
                    int(totals[0]) >= self.max_events
                    or int(totals[1]) + len(encoded) > self.max_bytes
                ):
                    raise self._fail("retention_capacity")
                blocked = False
                subscribers = connection.execute(
                    "SELECT * FROM stream_subscriptions WHERE topic=?", (topic.name,)
                ).fetchall()
                for sub in subscribers:
                    if sub["ordering_key"] is not None and sub["ordering_key"] != key:
                        continue
                    if (
                        sub["since_time"] is not None
                        and envelope.metadata.occurred_at.timestamp() < sub["since_time"]
                    ):
                        continue
                    count = 1
                    if sub["group_name"]:
                        count = int(
                            connection.execute(
                                "SELECT partitions FROM stream_groups WHERE name=?",
                                (sub["group_name"],),
                            ).fetchone()[0]
                        )
                    partition = partition_hash % count
                    cursor = connection.execute(
                        "SELECT committed FROM stream_cursors WHERE topic=? AND consumer=? AND "
                        "partition=?",
                        (topic.name, sub["consumer"], partition),
                    ).fetchone()
                    query, params = self._query(
                        topic.name,
                        int(cursor[0]),
                        sub["ordering_key"],
                        sub["since_time"],
                        partition,
                        count,
                    )
                    pending = int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM stream_events WHERE {query}", params
                        ).fetchone()[0]
                    )
                    if pending >= int(sub["max_pending"]):
                        config = json.loads(sub["config"])
                        if config["backpressure"] != BackpressurePolicy.BLOCK:
                            raise self._fail("subscriber_capacity")
                        blocked = True
                if not blocked:
                    connection.execute(
                        "INSERT INTO "
                        "stream_events(topic,message_id,envelope,occurred,ordering_key,"
                        "partition_hash) VALUES(?,?,?,?,?,?)",
                        (
                            topic.name,
                            envelope.metadata.message_id,
                            encoded,
                            envelope.metadata.occurred_at.timestamp(),
                            key,
                            partition_hash,
                        ),
                    )
                    return
            if time.monotonic() >= deadline:
                raise self._fail("backpressure_timeout")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def subscribe(self, subscription: PubSubSubscription) -> PubSubSubscriptionHandle:
        config = self._config(subscription)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM stream_subscriptions WHERE topic=? AND subscriber=?",
                (subscription.topic.name, subscription.subscriber_id),
            ).fetchone()
            if existing:
                if existing["config"] != config:
                    raise self._fail("subscription_conflict")
                sid = str(existing["id"])
                connection.execute("UPDATE stream_subscriptions SET active=1 WHERE id=?", (sid,))
            else:
                total = int(
                    connection.execute("SELECT COUNT(*) FROM stream_subscriptions").fetchone()[0]
                )
                if total >= self.max_subscriptions:
                    raise self._fail("subscription_capacity")
                start = 0
                consumer = "sub:" + subscription.subscriber_id
                if subscription.consumer_group:
                    group = connection.execute(
                        "SELECT topic FROM stream_groups WHERE name=?",
                        (subscription.consumer_group,),
                    ).fetchone()
                    if not group or group[0] != subscription.topic.name:
                        raise self._fail("group_missing")
                    consumer = "group:" + subscription.consumer_group
                    start = int(
                        connection.execute(
                            "SELECT MIN(committed) FROM stream_cursors "
                            "WHERE topic=? AND consumer=?",
                            (subscription.topic.name, consumer),
                        ).fetchone()[0]
                    )
                else:
                    start = self._start(connection, subscription)
                    connection.execute(
                        "INSERT INTO stream_cursors(topic,consumer,partition,committed) "
                        "VALUES(?,?,0,?)",
                        (subscription.topic.name, consumer, start),
                    )
                query, params = self._query(
                    subscription.topic.name,
                    start,
                    subscription.ordering_key,
                    subscription.since_time.timestamp() if subscription.since_time else None,
                )
                pending = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM stream_events WHERE {query}", params
                    ).fetchone()[0]
                )
                if pending > subscription.max_pending:
                    raise self._fail("replay_capacity")
                sid = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stream_subscriptions VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (
                        sid,
                        subscription.topic.name,
                        subscription.subscriber_id,
                        config,
                        consumer,
                        subscription.consumer_group,
                        subscription.ordering_key,
                        subscription.since_time.timestamp() if subscription.since_time else None,
                        subscription.max_pending,
                    ),
                )
            return PubSubSubscriptionHandle(
                sid, subscription.topic, subscription.subscriber_id, subscription.consumer_group
            )

    def _subscription(
        self, connection: sqlite3.Connection, handle: PubSubSubscriptionHandle
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM stream_subscriptions WHERE id=?", (handle.subscription_id,)
        ).fetchone()
        if not row or (row["topic"], row["subscriber"], row["group_name"]) != (
            handle.topic.name,
            handle.subscriber_id,
            handle.consumer_group,
        ):
            raise self._fail("subscription_unknown")
        return cast(sqlite3.Row, row)

    def unsubscribe(self, handle: PubSubSubscriptionHandle) -> None:
        with self._connection() as connection:
            self._subscription(connection, handle)
            connection.execute(
                "UPDATE stream_subscriptions SET active=0 WHERE id=?", (handle.subscription_id,)
            )

    def next_event(
        self, handle: PubSubSubscriptionHandle, *, timeout: timedelta | None = None
    ) -> MessageEnvelope | None:
        delivery = self.next_delivery(handle, timeout=timeout)
        return delivery.envelope if delivery else None

    def next_delivery(
        self, handle: PubSubSubscriptionHandle, *, timeout: timedelta | None = None
    ) -> StreamDelivery | None:
        seconds = timeout.total_seconds() if timeout else 0
        if not 0 <= seconds <= 30:
            raise InvalidPubSubSchema("stream read timeout must be bounded")
        deadline = time.monotonic() + seconds
        while True:
            delivery = self._read(handle)
            if delivery is not None or time.monotonic() >= deadline:
                return delivery
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def _read(self, handle: PubSubSubscriptionHandle) -> StreamDelivery | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sub = self._subscription(connection, handle)
            if not sub["active"]:
                return None
            now = self._now()
            assignments: list[tuple[int, int]] = [(0, 0)]
            partitions = 1
            if sub["group_name"]:
                group = connection.execute(
                    "SELECT * FROM stream_groups WHERE name=?", (sub["group_name"],)
                ).fetchone()
                partitions = int(group["partitions"])
                rows = connection.execute(
                    "SELECT partition,generation FROM stream_assignments WHERE group_name=? AND "
                    "subscriber=? AND expires>? ORDER BY partition",
                    (sub["group_name"], sub["subscriber"], now),
                ).fetchall()
                assignments = [(int(row[0]), int(row[1])) for row in rows]
                connection.execute(
                    "UPDATE stream_assignments SET expires=? WHERE group_name=? AND subscriber=? "
                    "AND expires>?",
                    (now + float(group["timeout"]), sub["group_name"], sub["subscriber"], now),
                )
            for partition, generation in assignments:
                cursor = connection.execute(
                    "SELECT * FROM stream_cursors WHERE topic=? AND consumer=? AND partition=?",
                    (sub["topic"], sub["consumer"], partition),
                ).fetchone()
                if cursor["delivered"] is not None and float(cursor["expires"]) > now:
                    continue
                query, params = self._query(
                    str(sub["topic"]),
                    int(cursor["committed"]),
                    sub["ordering_key"],
                    sub["since_time"],
                    partition,
                    partitions,
                )
                record = connection.execute(
                    f"SELECT sequence,envelope FROM stream_events WHERE {query} "
                    "ORDER BY sequence LIMIT 1",
                    params,
                ).fetchone()
                if record is None:
                    continue
                token = uuid.uuid4().hex
                sequence = int(record["sequence"])
                decoded = self._codec.decode(bytes(record["envelope"]))
                connection.execute(
                    "UPDATE stream_cursors SET "
                    "delivered=?,token=?,owner=?,expires=?,generation=? WHERE topic=? AND "
                    "consumer=? AND partition=?",
                    (
                        sequence,
                        token,
                        self._owner,
                        now + self.visibility_seconds,
                        generation,
                        sub["topic"],
                        sub["consumer"],
                        partition,
                    ),
                )
                self._receipts[(handle.subscription_id, partition)] = (sequence, token)
                return StreamDelivery(sequence, decoded, token, partition)
            return None

    def checkpoint(self, cursor: CheckpointCursor) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sub = connection.execute(
                "SELECT * FROM stream_subscriptions WHERE topic=? AND subscriber=? AND active=1",
                (cursor.topic.name, cursor.subscriber_id),
            ).fetchone()
            if not sub:
                raise self._fail("subscription_unknown")
            row = connection.execute(
                "SELECT * FROM stream_cursors WHERE topic=? AND consumer=? AND delivered=?",
                (cursor.topic.name, sub["consumer"], cursor.sequence),
            ).fetchone()
            now = self._now()
            receipt = self._receipts.get((str(sub["id"]), int(row["partition"]))) if row else None
            if (
                not row
                or not receipt
                or receipt != (cursor.sequence, row["token"])
                or cursor.ownership_token != row["token"]
                or row["owner"] != self._owner
                or float(row["expires"]) <= now
            ):
                raise self._fail("checkpoint_fenced")
            if sub["group_name"]:
                assignment = connection.execute(
                    "SELECT * FROM stream_assignments WHERE group_name=? AND partition=?",
                    (sub["group_name"], row["partition"]),
                ).fetchone()
                if (
                    not assignment
                    or assignment["subscriber"] != sub["subscriber"]
                    or int(assignment["generation"]) != int(row["generation"])
                    or float(assignment["expires"]) <= now
                ):
                    raise self._fail("checkpoint_fenced")
            connection.execute(
                "UPDATE stream_cursors SET "
                "committed=?,delivered=NULL,token=NULL,owner=NULL,expires=NULL WHERE topic=? AND "
                "consumer=? AND partition=?",
                (cursor.sequence, cursor.topic.name, sub["consumer"], row["partition"]),
            )
            self._receipts.pop((str(sub["id"]), int(row["partition"])), None)

    def create_consumer_group(self, group: ConsumerGroup) -> None:
        if group.partitions > 1024 or group.rebalance_timeout > timedelta(hours=1):
            raise InvalidPubSubSchema("stream group must be bounded")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT topic,partitions,timeout FROM stream_groups WHERE name=?", (group.name,)
            ).fetchone()
            expected = (group.topic.name, group.partitions, group.rebalance_timeout.total_seconds())
            if existing:
                if tuple(existing) != expected:
                    raise self._fail("group_conflict")
                return
            if self._floor(connection, group.topic.name):
                raise self._fail("replay_gap")
            count = int(connection.execute("SELECT COUNT(*) FROM stream_groups").fetchone()[0])
            if count >= self.max_subscriptions:
                raise self._fail("group_capacity")
            connection.execute("INSERT INTO stream_groups VALUES(?,?,?,?)", (group.name, *expected))
            for partition in range(group.partitions):
                connection.execute(
                    "INSERT INTO stream_cursors(topic,consumer,partition,committed) "
                    "VALUES(?,?,?,0)",
                    (group.topic.name, "group:" + group.name, partition),
                )

    def assign_consumer(self, assignment: ConsumerGroupAssignment) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            group = connection.execute(
                "SELECT * FROM stream_groups WHERE name=?", (assignment.consumer_group,)
            ).fetchone()
            if (
                not group
                or int(group["partitions"]) != assignment.partitions_total
                or assignment.partition >= assignment.partitions_total
            ):
                raise self._fail("assignment_invalid")
            existing = connection.execute(
                "SELECT * FROM stream_assignments WHERE group_name=? AND partition=?",
                (assignment.consumer_group, assignment.partition),
            ).fetchone()
            now = self._now()
            if existing and float(existing["expires"]) > now:
                if existing["subscriber"] != assignment.subscriber_id:
                    raise self._fail("partition_owned")
                generation = int(existing["generation"])
            else:
                generation = int(existing["generation"]) + 1 if existing else 1
                connection.execute(
                    "UPDATE stream_cursors SET delivered=NULL,token=NULL,owner=NULL,expires=NULL "
                    "WHERE topic=? AND consumer=? AND partition=?",
                    (group["topic"], "group:" + assignment.consumer_group, assignment.partition),
                )
            connection.execute(
                "INSERT OR REPLACE INTO stream_assignments VALUES(?,?,?,?,?)",
                (
                    assignment.consumer_group,
                    assignment.partition,
                    assignment.subscriber_id,
                    generation,
                    now + float(group["timeout"]),
                ),
            )

    def replay(self, subscription: PubSubSubscription) -> ReplayReport:
        self._config(subscription)
        with self._connection() as connection:
            start = self._start(connection, subscription)
            query, params = self._query(
                subscription.topic.name,
                start,
                subscription.ordering_key,
                subscription.since_time.timestamp() if subscription.since_time else None,
            )
            row = connection.execute(
                f"SELECT COUNT(*),MIN(sequence),MAX(sequence) FROM stream_events WHERE {query}",
                params,
            ).fetchone()
            count = int(row[0])
            if count > subscription.max_pending:
                raise self._fail("replay_capacity")
            return ReplayReport(
                subscription.topic,
                subscription.subscriber_id,
                count,
                int(row[1] or 0),
                int(row[2] or 0),
            )

    def prune(self, topic: PubSubTopic, *, through_sequence: int) -> int:
        if type(through_sequence) is not int or through_sequence < 0:
            raise InvalidPubSubSchema("prune sequence must be nonnegative")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursors = connection.execute(
                "SELECT * FROM stream_cursors WHERE topic=?",
                (topic.name,),
            ).fetchall()
            for cursor in cursors:
                partitions = 1
                key = None
                since_time = None
                if str(cursor["consumer"]).startswith("group:"):
                    partitions = int(
                        connection.execute(
                            "SELECT partitions FROM stream_groups WHERE name=?",
                            (str(cursor["consumer"])[6:],),
                        ).fetchone()[0]
                    )
                else:
                    sub = connection.execute(
                        "SELECT ordering_key,since_time FROM stream_subscriptions "
                        "WHERE topic=? AND consumer=?",
                        (topic.name, cursor["consumer"]),
                    ).fetchone()
                    key, since_time = sub[0], sub[1]
                query, params = self._query(
                    topic.name,
                    int(cursor["committed"]),
                    key,
                    since_time,
                    int(cursor["partition"]),
                    partitions,
                )
                uncommitted = connection.execute(
                    f"SELECT 1 FROM stream_events WHERE {query} AND sequence<=? LIMIT 1",
                    [*params, through_sequence],
                ).fetchone()
                if uncommitted:
                    raise self._fail("uncheckpointed_retention")
            maximum = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM stream_events WHERE topic=?",
                    (topic.name,),
                ).fetchone()[0]
            )
            floor = max(self._floor(connection, topic.name), min(through_sequence, maximum))
            deleted = connection.execute(
                "DELETE FROM stream_events WHERE topic=? AND sequence<=?", (topic.name, floor)
            ).rowcount
            connection.execute(
                "INSERT INTO stream_floors VALUES(?,?) ON CONFLICT(topic) DO UPDATE SET "
                "sequence=excluded.sequence",
                (topic.name, floor),
            )
            return deleted
