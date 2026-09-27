# Local durable streams

`app.streams` uses `SQLiteStreamDriver` against the application's persistent state
directory. SQLite commits the event before a reader can receive it. This adapter
qualifies local SQLite behavior; it does not establish Redis or Kafka support.

```python
from datetime import UTC, datetime
from af.messaging.pubsub import CheckpointCursor, PubSubSubscription, PubSubTopic, ReplayCursor

topic = PubSubTopic("tasks.changed")
handle = app.streams.subscribe(PubSubSubscription(
    topic, "task-indexer", replay_from=ReplayCursor.BEGINNING, max_pending=1024,
))
# Publish a validated MessageEnvelope using app.streams.publish(topic, envelope).
delivery = app.streams.next_delivery(handle)
if delivery is not None:
    process_idempotently(delivery.envelope)
    app.streams.checkpoint(CheckpointCursor(
        topic, "task-indexer", delivery.sequence, datetime.now(UTC),
        delivery.ownership_token,
    ))
```

Reopen the same database and subscribe with the same topic, subscriber identifier,
and subscription configuration. Committed events stay committed. Unacknowledged
events become available after the visibility timeout (30 seconds by default).
Each subscriber partition has one outstanding delivery. Receipts have random
ownership tokens; a checkpoint without the current token is rejected, including
after redelivery by the same process. Processing must therefore be idempotent.

`SINCE_SEQUENCE` takes an exclusive `since_sequence`; `SINCE_TIME` takes an aware
inclusive `since_time`. An ordering key filters independent subscriptions.
`replay` returns a bounded read-only report and does not move a checkpoint.
Global insertion order also preserves key order. Consumer groups use deterministic
key/message partitioning and explicitly assigned, expiring partition ownership.
An unexpired partition cannot transfer to another subscriber. Expired ownership
increments its generation and fences the previous receipt. Member-specific key
filters or replay positions are rejected because the group owns shared cursors.

The adapter bounds stored event count, envelope bytes, subscriptions, groups, and
subscriber backlog. `BUFFER` refuses a publish atomically when a subscriber is
full. `BLOCK` waits for a checkpoint up to the configured publish timeout. Durable
drop policies are explicitly unavailable. Retention capacity errors require
operator action; `prune(topic, through_sequence=...)` removes only history that
every relevant cursor has checkpointed. Requests for removed history fail with a
replay-gap outcome. Unsubscribing pauses delivery and retains its durable cursor
and backlog obligations. SQLite bookkeeping/WAL overhead is additional to the
configured envelope-byte budget.

`LocalPubSubDriver` and `LocalStreamDriver` are bounded volatile alternatives.
Their events and checkpoints do not survive process restart. Their explicit drop
policies can lose events; blocking backpressure is rejected. They must not be
advertised as durable adapters.

Qualification: `tests/integration/test_sqlite_streams.py` uses real SQLite files,
multiple independent owners, concurrent publishing, restart recovery, time/key
replay, stale receipts, ownership transfer, retention gaps, and capacity failures.
