from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from af.messaging import (
    EnvelopeKind,
    FrozenJsonObject,
    InvalidMessageEnvelope,
    JsonEnvelopeCodec,
    MessageEnvelope,
    MessageMetadata,
    PayloadLimits,
    PolicyContext,
)
from af.observability import TelemetryContext

_NOW = datetime(2026, 7, 24, 1, 30, tzinfo=UTC)
_TENANT_REF = f"sha256:{'a' * 64}"
_DECISION_DIGEST = "b" * 64


def _metadata(**overrides: object) -> MessageMetadata:
    values: dict[str, object] = {
        "message_id": "01j-message-root",
        "message_type": "tasks.created",
        "schema_version": 1,
        "kind": EnvelopeKind.INTEGRATION_EVENT,
        "occurred_at": _NOW,
        "correlation_id": "correlation-1",
        "causation_id": "cause-1",
        "deadline": _NOW + timedelta(minutes=5),
        "policy": PolicyContext(
            policy_version="policy-v1",
            decision_digest=_DECISION_DIGEST,
            effective_scope="deterministic",
            tenant_ref=_TENANT_REF,
        ),
        "telemetry": TelemetryContext(
            operation="tasks.publish",
            correlation_id="correlation-1",
            causation_id="cause-1",
            trace_id="trace-1",
            span_id="span-1",
            trace_flags=1,
            tenant_ref=_TENANT_REF,
        ),
    }
    values.update(overrides)
    return MessageMetadata(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", tuple(EnvelopeKind))
def test_all_message_roles_round_trip_without_collapsing_semantics(kind: EnvelopeKind) -> None:
    envelope = MessageEnvelope.create(
        metadata=_metadata(kind=kind),
        payload={"attempt": 1, "enabled": True, "labels": ["a", "b"]},
    )
    codec = JsonEnvelopeCodec()

    encoded = codec.encode(envelope)
    decoded = codec.decode(encoded)

    assert codec.encode(decoded) == encoded
    assert decoded.metadata == envelope.metadata
    assert decoded.metadata.kind is kind
    assert decoded.payload_copy() == {
        "attempt": 1,
        "enabled": True,
        "labels": ["a", "b"],
    }
    assert json.loads(encoded)["version"] == "af.message-envelope.v1"


def test_envelope_snapshots_payload_and_normalizes_times_to_utc() -> None:
    source = {"nested": [1, {"value": "original"}]}
    local_time = _NOW.astimezone(timezone(timedelta(hours=2)))
    envelope = MessageEnvelope.create(
        metadata=_metadata(occurred_at=local_time),
        payload=source,
    )

    source["nested"][1]["value"] = "mutated"  # type: ignore[index]
    detached = envelope.payload_copy()
    detached["nested"][1]["value"] = "detached"  # type: ignore[index]

    assert envelope.metadata.occurred_at == _NOW
    assert envelope.metadata.occurred_at.tzinfo is UTC
    assert envelope.payload_copy() == {"nested": [1, {"value": "original"}]}
    assert "original" not in repr(envelope)
    assert "correlation-1" not in repr(envelope.metadata)


def test_codec_is_deterministic_and_uses_canonical_utc_timestamps() -> None:
    codec = JsonEnvelopeCodec()
    envelope = MessageEnvelope.create(
        metadata=_metadata(),
        payload={"z": 1, "a": {"second": 2, "first": 1}},
    )

    first = codec.encode(envelope)
    second = codec.encode(envelope)
    raw = json.loads(first)

    assert first == second
    assert raw["metadata"]["occurred_at"] == "2026-07-24T01:30:00.000000Z"
    assert raw["metadata"]["deadline"] == "2026-07-24T01:35:00.000000Z"
    assert list(raw["payload"]) == ["a", "z"]
    assert codec.decode(first) == envelope


@pytest.mark.parametrize(
    "encoded",
    [
        b"",
        b"not-json",
        b"\xff",
        b'{"version":"af.message-envelope.v1","metadata":{},"payload":{},"extra":1}',
        b'{"version":"wrong","metadata":{},"payload":{}}',
        b'{"version":"af.message-envelope.v1","metadata":{},"payload":{},"payload":{}}',
        b'{"version":"af.message-envelope.v1","metadata":[],"payload":{}}',
        b'{"version":"af.message-envelope.v1","metadata":{},"payload":NaN}',
    ],
)
def test_codec_rejects_malformed_unknown_duplicate_and_nonfinite_wire_input(
    encoded: bytes,
) -> None:
    with pytest.raises(InvalidMessageEnvelope):
        JsonEnvelopeCodec().decode(encoded)


def test_codec_rejects_oversize_before_attempting_to_parse() -> None:
    codec = JsonEnvelopeCodec(max_bytes=32)

    with pytest.raises(InvalidMessageEnvelope, match="byte limit"):
        codec.decode(b"{" + b"x" * 64)


def test_codec_rejects_noncanonical_timestamps_and_unknown_metadata_fields() -> None:
    codec = JsonEnvelopeCodec()
    encoded = codec.encode(
        MessageEnvelope.create(metadata=_metadata(), payload={"ok": True})
    )
    raw = json.loads(encoded)
    raw["metadata"]["occurred_at"] = "2026-07-24T02:30:00+01:00"

    with pytest.raises(InvalidMessageEnvelope, match="canonical UTC"):
        codec.decode(json.dumps(raw).encode())

    raw = json.loads(encoded)
    raw["metadata"]["secret"] = "must-not-pass"
    with pytest.raises(InvalidMessageEnvelope, match="metadata fields"):
        codec.decode(json.dumps(raw).encode())


def test_codec_rejects_invalid_policy_and_telemetry_envelopes() -> None:
    codec = JsonEnvelopeCodec()
    encoded = codec.encode(
        MessageEnvelope.create(metadata=_metadata(), payload={"ok": True})
    )
    raw = json.loads(encoded)
    raw["metadata"]["policy"]["raw_principal"] = "hidden"
    with pytest.raises(InvalidMessageEnvelope, match="policy context fields"):
        codec.decode(json.dumps(raw).encode())

    raw = json.loads(encoded)
    raw["metadata"]["telemetry"]["prompt"] = "hidden"
    with pytest.raises(InvalidMessageEnvelope, match="telemetry context"):
        codec.decode(json.dumps(raw).encode())


def test_payload_rejects_cycles_unsupported_values_and_resource_exhaustion() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)

    with pytest.raises(InvalidMessageEnvelope, match="cycles"):
        MessageEnvelope.create(metadata=_metadata(), payload=cyclic)
    with pytest.raises(InvalidMessageEnvelope, match="non-JSON"):
        MessageEnvelope.create(metadata=_metadata(), payload={"value": object()})
    with pytest.raises(InvalidMessageEnvelope, match="finite"):
        MessageEnvelope.create(metadata=_metadata(), payload={"value": float("inf")})
    with pytest.raises(InvalidMessageEnvelope, match="signed 64-bit"):
        MessageEnvelope.create(metadata=_metadata(), payload={"value": 2**64})
    with pytest.raises(InvalidMessageEnvelope, match="depth"):
        MessageEnvelope.create(
            metadata=_metadata(),
            payload={"one": {"two": {"three": 3}}},
            limits=PayloadLimits(max_depth=1),
        )
    with pytest.raises(InvalidMessageEnvelope, match="node"):
        MessageEnvelope.create(
            metadata=_metadata(),
            payload=[1, 2, 3],
            limits=PayloadLimits(max_nodes=2),
        )
    with pytest.raises(InvalidMessageEnvelope, match="string"):
        MessageEnvelope.create(
            metadata=_metadata(),
            payload="long",
            limits=PayloadLimits(max_string_bytes=3),
        )


def test_direct_payload_construction_cannot_bypass_snapshot_validation() -> None:
    with pytest.raises(InvalidMessageEnvelope, match="MessageEnvelope.create"):
        MessageEnvelope(metadata=_metadata(), payload={"mutable": True})  # type: ignore[arg-type]
    with pytest.raises(InvalidMessageEnvelope, match="MessageEnvelope.create"):
        MessageEnvelope(
            metadata=_metadata(),
            payload=FrozenJsonObject((("z", 1), ("a", 2))),
        )


def test_metadata_rejects_invalid_identity_deadline_and_divergent_context() -> None:
    with pytest.raises(InvalidMessageEnvelope, match="cause itself"):
        _metadata(message_id="same", causation_id="same")
    with pytest.raises(InvalidMessageEnvelope, match="deadline"):
        _metadata(deadline=_NOW)
    with pytest.raises(InvalidMessageEnvelope, match="schema version"):
        _metadata(schema_version=True)
    with pytest.raises(InvalidMessageEnvelope, match="correlation id"):
        _metadata(
            telemetry=TelemetryContext(
                operation="tasks.publish",
                correlation_id="different",
            )
        )
    with pytest.raises(InvalidMessageEnvelope, match="tenant references"):
        _metadata(
            telemetry=TelemetryContext(
                operation="tasks.publish",
                correlation_id="correlation-1",
                causation_id="cause-1",
                tenant_ref=f"sha256:{'c' * 64}",
            )
        )


def test_limits_and_codec_configuration_fail_closed() -> None:
    with pytest.raises(ValueError):
        PayloadLimits(max_depth=0)
    with pytest.raises(ValueError):
        PayloadLimits(max_nodes=0)
    with pytest.raises(ValueError):
        PayloadLimits(max_string_bytes=0)
    with pytest.raises(ValueError):
        JsonEnvelopeCodec(max_bytes=0)
    with pytest.raises(ValueError):
        JsonEnvelopeCodec(payload_limits=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidMessageEnvelope):
        JsonEnvelopeCodec().encode(object())  # type: ignore[arg-type]
    with pytest.raises(InvalidMessageEnvelope):
        JsonEnvelopeCodec().decode("not-bytes")  # type: ignore[arg-type]
