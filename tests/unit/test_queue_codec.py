from datetime import UTC, datetime, timedelta

import pytest

from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import QueueJob, QueueJobTags, WorkerJobCoordinates
from af.queue.codec import QueueJobCodec


def test_queue_codec_round_trips_complete_job_and_bounds_payload() -> None:
    envelope = MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id="wire-job",
            message_type="jobs.example",
            schema_version=1,
            kind=EnvelopeKind.JOB,
            occurred_at=datetime(2026, 9, 26, tzinfo=UTC),
        ),
        payload={"text": "hello", "values": [1, True, None]},
    )
    job = QueueJob(
        envelope=envelope,
        lane="critical",
        delay=timedelta(microseconds=1001),
        priority=10,
        max_attempts=3,
        backoff=timedelta(seconds=5),
        tags=QueueJobTags(
            coordinates=WorkerJobCoordinates(
                workflow_id="flow-1", run_id="run-1", node_id="node-1", checkpoint_id="checkpoint-1"
            )
        ),
    )
    codec = QueueJobCodec()
    assert codec.decode(codec.encode(job)) == job
    with pytest.raises(ValueError, match="bounded"):
        QueueJobCodec(max_bytes=10).encode(job)


@pytest.mark.parametrize("payload", [b"[]", b"{}", b"null", b"not-json", b'{"version":true}'])
def test_queue_codec_rejects_invalid_wire_shapes(payload: bytes) -> None:
    with pytest.raises(ValueError):
        QueueJobCodec().decode(payload)


def test_queue_codec_rejects_duplicate_keys_and_excessive_nesting() -> None:

    envelope = MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id="duplicate",
            message_type="jobs.example",
            schema_version=1,
            kind=EnvelopeKind.JOB,
            occurred_at=datetime(2026, 9, 26, tzinfo=UTC),
        ),
        payload={},
    )
    encoded = QueueJobCodec().encode(QueueJob(envelope=envelope))
    duplicate = encoded.replace(b'"max_attempts":1', b'"max_attempts":99,"max_attempts":1')
    with pytest.raises(ValueError):
        QueueJobCodec().decode(duplicate)
    deep = b"[" * 2000 + b"0" + b"]" * 2000
    with pytest.raises(ValueError):
        QueueJobCodec().decode(deep)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("delay", 0),
        ("delay", True),
        ("delay", 10**40),
        ("tags", []),
        ("tags", {"coordinates": []}),
        ("priority", float("nan")),
    ],
)
def test_queue_codec_rejects_invalid_field_values(field: str, value: object) -> None:
    import json

    from tests.unit.test_queue_sqs import _job

    payload = json.loads(QueueJobCodec().encode(_job()))
    payload[field] = value
    with pytest.raises(ValueError):
        QueueJobCodec().decode(json.dumps(payload).encode())


def test_queue_codec_rejects_invalid_bounds_and_input_types() -> None:
    with pytest.raises(ValueError):
        QueueJobCodec(max_bytes=0)
    with pytest.raises(TypeError):
        QueueJobCodec().encode(object())  # type: ignore[arg-type]
