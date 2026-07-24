"""Versioned immutable message envelopes and a bounded JSON codec."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from af.messaging.errors import InvalidMessageEnvelope
from af.messaging.payloads import (
    FrozenJsonArray,
    FrozenJsonObject,
    FrozenJsonValue,
    PayloadLimits,
    _freeze_payload,
    _is_frozen_payload,
    _thaw_payload,
)
from af.observability.context import TelemetryContext

__all__ = (
    "EnvelopeKind",
    "FrozenJsonArray",
    "FrozenJsonObject",
    "InvalidMessageEnvelope",
    "JsonEnvelopeCodec",
    "MessageEnvelope",
    "MessageMetadata",
    "PayloadLimits",
    "PolicyContext",
)

_ENVELOPE_VERSION = "af.message-envelope.v1"
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.+-]{0,126}[A-Za-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TENANT_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_MAX_ID_LENGTH = 200
_MAX_SCHEMA_VERSION = 2**31 - 1
_MAX_WIRE_BYTES = 16 * 1024 * 1024
_REQUIRED_TOP_LEVEL_FIELDS = frozenset({"version", "metadata", "payload"})
_REQUIRED_METADATA_FIELDS = frozenset(
    {"message_id", "message_type", "schema_version", "kind", "occurred_at"}
)
_OPTIONAL_METADATA_FIELDS = frozenset(
    {"correlation_id", "causation_id", "deadline", "policy", "telemetry"}
)
_POLICY_FIELDS = frozenset({"policy_version", "decision_digest", "effective_scope", "tenant_ref"})


class EnvelopeKind(StrEnum):
    """Message roles with materially different delivery semantics."""

    DOMAIN_EVENT = "domain-event"
    INTEGRATION_EVENT = "integration-event"
    COMMAND = "command"
    JOB = "job"
    BROADCAST = "broadcast"
    NOTIFICATION = "notification"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidMessageEnvelope(f"{field} must be a canonical identifier")
    return value


def _version(value: object, field: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise InvalidMessageEnvelope(f"{field} must be a bounded version identifier")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise InvalidMessageEnvelope(f"{field} must be a lowercase SHA-256 digest")
    return value


def _opaque_id(
    value: object,
    field: str,
    *,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_ID_LENGTH
        or _UNSAFE_TEXT.search(value)
    ):
        raise InvalidMessageEnvelope(f"{field} must be bounded non-empty single-line text")
    return value


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InvalidMessageEnvelope(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class PolicyContext:
    """Opaque policy and tenant references safe to cross a process boundary."""

    policy_version: str
    decision_digest: str
    effective_scope: str
    tenant_ref: str | None = None

    def __post_init__(self) -> None:
        _version(self.policy_version, "policy context version")
        _digest(self.decision_digest, "policy decision digest")
        _identifier(self.effective_scope, "policy effective scope")
        if self.tenant_ref is not None and (
            type(self.tenant_ref) is not str or _TENANT_REF.fullmatch(self.tenant_ref) is None
        ):
            raise InvalidMessageEnvelope("policy tenant reference must be an opaque sha256 digest")

    def __repr__(self) -> str:
        return (
            f"PolicyContext(policy_version={self.policy_version!r}, "
            "decision_digest='[REDACTED]', "
            f"effective_scope={self.effective_scope!r}, "
            "tenant_ref='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MessageMetadata:
    """Versioned routing metadata that never contains a message payload."""

    message_id: str
    message_type: str
    schema_version: int
    kind: EnvelopeKind
    occurred_at: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    deadline: datetime | None = None
    policy: PolicyContext | None = None
    telemetry: TelemetryContext | None = None

    def __post_init__(self) -> None:
        message_id = _opaque_id(self.message_id, "message id")
        assert message_id is not None
        _identifier(self.message_type, "message type")
        if (
            type(self.schema_version) is not int
            or not 1 <= self.schema_version <= _MAX_SCHEMA_VERSION
        ):
            raise InvalidMessageEnvelope("message schema version must be a positive integer")
        if not isinstance(self.kind, EnvelopeKind):
            raise InvalidMessageEnvelope("message kind is invalid")

        occurred_at = _aware_utc(self.occurred_at, "message occurrence time")
        correlation_id = _opaque_id(
            self.correlation_id,
            "message correlation id",
            optional=True,
        )
        causation_id = _opaque_id(
            self.causation_id,
            "message causation id",
            optional=True,
        )
        if causation_id == message_id:
            raise InvalidMessageEnvelope("a message cannot cause itself")

        deadline = None if self.deadline is None else _aware_utc(self.deadline, "message deadline")
        if deadline is not None and deadline <= occurred_at:
            raise InvalidMessageEnvelope("message deadline must follow occurrence time")
        if self.policy is not None and not isinstance(self.policy, PolicyContext):
            raise InvalidMessageEnvelope("message policy context is invalid")
        if self.telemetry is not None and not isinstance(
            self.telemetry,
            TelemetryContext,
        ):
            raise InvalidMessageEnvelope("message telemetry context is invalid")

        self._validate_context_consistency(correlation_id, causation_id)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "deadline", deadline)

    def _validate_context_consistency(
        self,
        correlation_id: str | None,
        causation_id: str | None,
    ) -> None:
        telemetry = self.telemetry
        if telemetry is None:
            return
        for metadata_value, telemetry_value, field in (
            (correlation_id, telemetry.correlation_id, "correlation id"),
            (causation_id, telemetry.causation_id, "causation id"),
        ):
            if (
                metadata_value is not None
                and telemetry_value is not None
                and metadata_value != telemetry_value
            ):
                raise InvalidMessageEnvelope(f"message and telemetry {field} must not diverge")
        if (
            self.policy is not None
            and self.policy.tenant_ref is not None
            and telemetry.tenant_ref is not None
            and self.policy.tenant_ref != telemetry.tenant_ref
        ):
            raise InvalidMessageEnvelope(
                "message policy and telemetry tenant references must not diverge"
            )

    def __repr__(self) -> str:
        return (
            "MessageMetadata(message_id='[REDACTED]', "
            f"message_type={self.message_type!r}, "
            f"schema_version={self.schema_version!r}, "
            f"kind={self.kind.value!r}, "
            f"occurred_at={self.occurred_at.isoformat()!r}, "
            "correlation_id='[REDACTED]', causation_id='[REDACTED]', "
            f"deadline={self.deadline!r}, policy={self.policy!r}, "
            "telemetry='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MessageEnvelope:
    """One immutable versioned message plus a recursively bounded payload."""

    metadata: MessageMetadata
    payload: FrozenJsonValue

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, MessageMetadata):
            raise InvalidMessageEnvelope("message envelope metadata is invalid")
        if not _is_frozen_payload(self.payload):
            raise InvalidMessageEnvelope(
                "message envelope payload must be created through MessageEnvelope.create"
            )

    @classmethod
    def create(
        cls,
        *,
        metadata: MessageMetadata,
        payload: object,
        limits: PayloadLimits | None = None,
    ) -> MessageEnvelope:
        """Snapshot a JSON-compatible payload under explicit finite limits."""

        if not isinstance(metadata, MessageMetadata):
            raise InvalidMessageEnvelope("message envelope metadata is invalid")
        resolved_limits = PayloadLimits() if limits is None else limits
        return cls(
            metadata=metadata,
            payload=_freeze_payload(payload, resolved_limits),
        )

    def payload_copy(self) -> object:
        """Return a detached mutable JSON-compatible copy of the payload."""

        return _thaw_payload(self.payload)

    def __repr__(self) -> str:
        return f"MessageEnvelope(metadata={self.metadata!r}, payload='[REDACTED]')"


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: object, field: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise InvalidMessageEnvelope(f"{field} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as error:
        raise InvalidMessageEnvelope(f"{field} must be a canonical UTC timestamp") from error
    normalized = _aware_utc(parsed, field)
    if _format_datetime(normalized) != value:
        raise InvalidMessageEnvelope(f"{field} must be a canonical UTC timestamp")
    return normalized


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidMessageEnvelope("message envelope JSON object contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    del value
    raise InvalidMessageEnvelope("message envelope JSON numbers must be finite")


def _object(value: object, field: str) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise InvalidMessageEnvelope(f"{field} must be an object")
    return cast(dict[str, object], value)


def _required_fields(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    field: str,
) -> None:
    fields = set(value)
    if not required.issubset(fields) or fields - required - optional:
        raise InvalidMessageEnvelope(f"{field} fields are invalid")


def _decode_policy(value: object) -> PolicyContext | None:
    if value is None:
        return None
    raw = _object(value, "message policy context")
    _required_fields(
        raw,
        required=frozenset({"policy_version", "decision_digest", "effective_scope"}),
        optional=frozenset({"tenant_ref"}),
        field="message policy context",
    )
    tenant_ref = raw.get("tenant_ref")
    if tenant_ref is not None and type(tenant_ref) is not str:
        raise InvalidMessageEnvelope("message policy tenant reference is invalid")
    return PolicyContext(
        policy_version=_version(raw.get("policy_version"), "policy context version"),
        decision_digest=_digest(raw.get("decision_digest"), "policy decision digest"),
        effective_scope=_identifier(
            raw.get("effective_scope"),
            "policy effective scope",
        ),
        tenant_ref=tenant_ref,
    )


def _decode_telemetry(value: object) -> TelemetryContext | None:
    if value is None:
        return None
    raw = _object(value, "message telemetry context")
    if any(type(item) is not str for item in raw.values()):
        raise InvalidMessageEnvelope("message telemetry context fields are invalid")
    try:
        return TelemetryContext.from_envelope(cast(dict[str, str], raw))
    except (TypeError, ValueError) as error:
        raise InvalidMessageEnvelope("message telemetry context is invalid") from error


class JsonEnvelopeCodec:
    """Deterministic JSON codec that rejects unsafe input before dispatch."""

    def __init__(
        self,
        *,
        max_bytes: int = 256 * 1024,
        payload_limits: PayloadLimits | None = None,
    ) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_WIRE_BYTES:
            raise ValueError("message envelope byte limit is invalid")
        if payload_limits is not None and not isinstance(
            payload_limits,
            PayloadLimits,
        ):
            raise ValueError("message envelope payload limits are invalid")
        self._max_bytes = max_bytes
        self._payload_limits = PayloadLimits() if payload_limits is None else payload_limits

    def encode(self, envelope: MessageEnvelope, /) -> bytes:
        """Encode one validated envelope to canonical UTF-8 JSON."""

        if not isinstance(envelope, MessageEnvelope):
            raise InvalidMessageEnvelope("JSON envelope codec requires a MessageEnvelope")
        wire = {
            "version": _ENVELOPE_VERSION,
            "metadata": self._encode_metadata(envelope.metadata),
            "payload": _thaw_payload(envelope.payload),
        }
        try:
            encoded = json.dumps(
                wire,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:  # pragma: no cover
            raise InvalidMessageEnvelope("message envelope could not be encoded") from error
        if len(encoded) > self._max_bytes:
            raise InvalidMessageEnvelope("message envelope exceeds its byte limit")
        return encoded

    def _encode_metadata(self, metadata: MessageMetadata) -> dict[str, object]:
        encoded: dict[str, object] = {
            "message_id": metadata.message_id,
            "message_type": metadata.message_type,
            "schema_version": metadata.schema_version,
            "kind": metadata.kind.value,
            "occurred_at": _format_datetime(metadata.occurred_at),
        }
        if metadata.correlation_id is not None:
            encoded["correlation_id"] = metadata.correlation_id
        if metadata.causation_id is not None:
            encoded["causation_id"] = metadata.causation_id
        if metadata.deadline is not None:
            encoded["deadline"] = _format_datetime(metadata.deadline)
        if metadata.policy is not None:
            policy: dict[str, object] = {
                "policy_version": metadata.policy.policy_version,
                "decision_digest": metadata.policy.decision_digest,
                "effective_scope": metadata.policy.effective_scope,
            }
            if metadata.policy.tenant_ref is not None:
                policy["tenant_ref"] = metadata.policy.tenant_ref
            encoded["policy"] = policy
        if metadata.telemetry is not None:
            encoded["telemetry"] = dict(metadata.telemetry.to_envelope())
        return encoded

    def decode(self, encoded: bytes, /) -> MessageEnvelope:
        """Validate and decode one bounded UTF-8 JSON envelope."""

        raw = self._decode_json(encoded)
        _required_fields(
            raw,
            required=_REQUIRED_TOP_LEVEL_FIELDS,
            field="message envelope top-level",
        )
        if raw.get("version") != _ENVELOPE_VERSION:
            raise InvalidMessageEnvelope("message envelope version is unsupported")
        metadata = self._decode_metadata(raw.get("metadata"))
        return MessageEnvelope.create(
            metadata=metadata,
            payload=raw.get("payload"),
            limits=self._payload_limits,
        )

    def _decode_json(self, encoded: bytes) -> dict[str, object]:
        if type(encoded) is not bytes:
            raise InvalidMessageEnvelope("message envelope wire value must be bytes")
        if not encoded or len(encoded) > self._max_bytes:
            raise InvalidMessageEnvelope("message envelope exceeds its byte limit")
        try:
            text = encoded.decode("utf-8", errors="strict")
            raw = json.loads(
                text,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except InvalidMessageEnvelope:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise InvalidMessageEnvelope(
                "message envelope is not valid bounded UTF-8 JSON"
            ) from error
        return _object(raw, "message envelope")

    def _decode_metadata(self, value: object) -> MessageMetadata:
        raw = _object(value, "message envelope metadata")
        _required_fields(
            raw,
            required=_REQUIRED_METADATA_FIELDS,
            optional=_OPTIONAL_METADATA_FIELDS,
            field="message envelope metadata",
        )
        kind_value = raw.get("kind")
        try:
            kind = EnvelopeKind(kind_value)
        except (TypeError, ValueError) as error:
            raise InvalidMessageEnvelope("message kind is invalid") from error

        schema_version = raw.get("schema_version")
        if type(schema_version) is not int:
            raise InvalidMessageEnvelope("message schema version must be a positive integer")
        correlation_id = _opaque_id(
            raw.get("correlation_id"),
            "message correlation id",
            optional=True,
        )
        causation_id = _opaque_id(
            raw.get("causation_id"),
            "message causation id",
            optional=True,
        )
        deadline_value = raw.get("deadline")
        return MessageMetadata(
            message_id=cast(
                str,
                _opaque_id(raw.get("message_id"), "message id"),
            ),
            message_type=_identifier(raw.get("message_type"), "message type"),
            schema_version=schema_version,
            kind=kind,
            occurred_at=_parse_datetime(
                raw.get("occurred_at"),
                "message occurrence time",
            ),
            correlation_id=correlation_id,
            causation_id=causation_id,
            deadline=(
                None
                if deadline_value is None
                else _parse_datetime(deadline_value, "message deadline")
            ),
            policy=_decode_policy(raw.get("policy")),
            telemetry=_decode_telemetry(raw.get("telemetry")),
        )
