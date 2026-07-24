"""Versioned immutable message envelopes and a bounded JSON codec."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite

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
_MAX_KEY_LENGTH = 200
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


class InvalidMessageEnvelope(ValueError):
    """A message envelope or its wire representation is invalid."""


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


def _opaque_id(value: object, field: str, *, optional: bool = False) -> str | None:
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
            raise InvalidMessageEnvelope(
                "policy tenant reference must be an opaque sha256 digest"
            )

    def __repr__(self) -> str:
        return (
            f"PolicyContext(policy_version={self.policy_version!r}, "
            "decision_digest='[REDACTED]', "
            f"effective_scope={self.effective_scope!r}, tenant_ref='[REDACTED]')"
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
        deadline = (
            None
            if self.deadline is None
            else _aware_utc(self.deadline, "message deadline")
        )
        if deadline is not None and deadline <= occurred_at:
            raise InvalidMessageEnvelope("message deadline must follow occurrence time")
        if self.policy is not None and not isinstance(self.policy, PolicyContext):
            raise InvalidMessageEnvelope("message policy context is invalid")
        if self.telemetry is not None and not isinstance(self.telemetry, TelemetryContext):
            raise InvalidMessageEnvelope("message telemetry context is invalid")
        if self.telemetry is not None:
            for metadata_value, telemetry_value, field in (
                (correlation_id, self.telemetry.correlation_id, "correlation id"),
                (causation_id, self.telemetry.causation_id, "causation id"),
            ):
                if (
                    metadata_value is not None
                    and telemetry_value is not None
                    and metadata_value != telemetry_value
                ):
                    raise InvalidMessageEnvelope(
                        f"message and telemetry {field} must not diverge"
                    )
            if (
                self.policy is not None
                and self.policy.tenant_ref is not None
                and self.telemetry.tenant_ref is not None
                and self.policy.tenant_ref != self.telemetry.tenant_ref
            ):
                raise InvalidMessageEnvelope(
                    "message policy and telemetry tenant references must not diverge"
                )
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "deadline", deadline)

    def __repr__(self) -> str:
        return (
            "MessageMetadata(message_id='[REDACTED]', "
            f"message_type={self.message_type!r}, schema_version={self.schema_version!r}, "
            f"kind={self.kind.value!r}, occurred_at={self.occurred_at.isoformat()!r}, "
            "correlation_id='[REDACTED]', causation_id='[REDACTED]', "
            f"deadline={self.deadline!r}, policy={self.policy!r}, telemetry='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True)
class PayloadLimits:
    """Finite recursive payload budgets applied before an envelope is admitted."""

    max_depth: int = 24
    max_nodes: int = 10_000
    max_string_bytes: int = 65_536

    def __post_init__(self) -> None:
        if type(self.max_depth) is not int or not 1 <= self.max_depth <= 64:
            raise ValueError("payload maximum depth must be between 1 and 64")
        if type(self.max_nodes) is not int or not 1 <= self.max_nodes <= 1_000_000:
            raise ValueError("payload maximum nodes must be between 1 and 1000000")
        if (
            type(self.max_string_bytes) is not int
            or not 1 <= self.max_string_bytes <= 1024 * 1024
        ):
            raise ValueError("payload maximum string bytes must be between 1 and 1048576")


@dataclass(frozen=True, slots=True)
class FrozenJsonArray:
    """Immutable JSON array snapshot."""

    items: tuple[FrozenJsonValue, ...]


@dataclass(frozen=True, slots=True)
class FrozenJsonObject:
    """Immutable, key-sorted JSON object snapshot."""

    items: tuple[tuple[str, FrozenJsonValue], ...]


type JsonScalar = None | bool | int | float | str
type FrozenJsonValue = JsonScalar | FrozenJsonArray | FrozenJsonObject


@dataclass(slots=True)
class _PayloadBudget:
    nodes: int = 0


def _freeze_json(
    value: object,
    *,
    limits: PayloadLimits,
    depth: int,
    budget: _PayloadBudget,
    active: set[int],
) -> FrozenJsonValue:
    if depth > limits.max_depth:
        raise InvalidMessageEnvelope("message payload exceeds its depth limit")
    budget.nodes += 1
    if budget.nodes > limits.max_nodes:
        raise InvalidMessageEnvelope("message payload exceeds its node limit")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise InvalidMessageEnvelope("message payload integers must be signed 64-bit values")
        return value
    if type(value) is float:
        if not isfinite(value):
            raise InvalidMessageEnvelope("message payload floats must be finite")
        return value
    if type(value) is str:
        if len(value.encode("utf-8")) > limits.max_string_bytes:
            raise InvalidMessageEnvelope("message payload string exceeds its byte limit")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise InvalidMessageEnvelope("message payload must not contain cycles")
        active.add(identity)
        try:
            try:
                pairs = tuple(value.items())
            except Exception as error:
                raise InvalidMessageEnvelope("message payload mapping cannot be read") from error
            snapshot: list[tuple[str, FrozenJsonValue]] = []
            for key, item in pairs:
                if (
                    type(key) is not str
                    or not key
                    or len(key) > _MAX_KEY_LENGTH
                    or _UNSAFE_TEXT.search(key)
                ):
                    raise InvalidMessageEnvelope(
                        "message payload object keys must be bounded non-empty single-line text"
                    )
                snapshot.append(
                    (
                        key,
                        _freeze_json(
                            item,
                            limits=limits,
                            depth=depth + 1,
                            budget=budget,
                            active=active,
                        ),
                    )
                )
            snapshot.sort(key=lambda item: item[0])
            if len({key for key, _ in snapshot}) != len(snapshot):
                raise InvalidMessageEnvelope("message payload object keys must be unique")
            return FrozenJsonObject(tuple(snapshot))
        finally:
            active.remove(identity)
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in active:
            raise InvalidMessageEnvelope("message payload must not contain cycles")
        active.add(identity)
        try:
            sequence = value
            assert isinstance(sequence, Sequence)
            return FrozenJsonArray(
                tuple(
                    _freeze_json(
                        item,
                        limits=limits,
                        depth=depth + 1,
                        budget=budget,
                        active=active,
                    )
                    for item in sequence
                )
            )
        finally:
            active.remove(identity)
    raise InvalidMessageEnvelope("message payload contains a non-JSON value")


def _thaw_json(value: FrozenJsonValue) -> object:
    if isinstance(value, FrozenJsonObject):
        return {key: _thaw_json(item) for key, item in value.items}
    if isinstance(value, FrozenJsonArray):
        return [_thaw_json(item) for item in value.items]
    return value


@dataclass(frozen=True, slots=True, repr=False)
class MessageEnvelope:
    """One immutable versioned message plus a recursively bounded payload."""

    metadata: MessageMetadata
    payload: FrozenJsonValue

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, MessageMetadata):
            raise InvalidMessageEnvelope("message envelope metadata is invalid")
        if not _is_frozen_json(self.payload, depth=0, budget=_PayloadBudget(), active=set()):
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

        resolved_limits = PayloadLimits() if limits is None else limits
        if not isinstance(resolved_limits, PayloadLimits):
            raise InvalidMessageEnvelope("message payload limits are invalid")
        frozen = _freeze_json(
            payload,
            limits=resolved_limits,
            depth=0,
            budget=_PayloadBudget(),
            active=set(),
        )
        return cls(metadata=metadata, payload=frozen)

    def payload_copy(self) -> object:
        """Return a detached mutable JSON-compatible copy of the payload."""

        return _thaw_json(self.payload)

    def __repr__(self) -> str:
        return f"MessageEnvelope(metadata={self.metadata!r}, payload='[REDACTED]')"


def _is_frozen_json(
    value: object,
    *,
    depth: int,
    budget: _PayloadBudget,
    active: set[int],
) -> bool:
    if depth > 64:
        return False
    budget.nodes += 1
    if budget.nodes > 1_000_000:
        return False
    if value is None or type(value) in {bool, int, float, str}:
        return not (type(value) is float and not isfinite(value))
    if isinstance(value, FrozenJsonArray):
        identity = id(value)
        if identity in active:
            return False
        active.add(identity)
        try:
            return type(value.items) is tuple and all(
                _is_frozen_json(item, depth=depth + 1, budget=budget, active=active)
                for item in value.items
            )
        finally:
            active.remove(identity)
    if isinstance(value, FrozenJsonObject):
        identity = id(value)
        if identity in active:
            return False
        active.add(identity)
        try:
            if type(value.items) is not tuple:
                return False
            keys: list[str] = []
            for pair in value.items:
                if type(pair) is not tuple or len(pair) != 2:
                    return False
                key, item = pair
                if type(key) is not str:
                    return False
                keys.append(key)
                if not _is_frozen_json(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    active=active,
                ):
                    return False
            return keys == sorted(keys) and len(keys) == len(set(keys))
        finally:
            active.remove(identity)
    return False


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
        if payload_limits is not None and not isinstance(payload_limits, PayloadLimits):
            raise ValueError("message envelope payload limits are invalid")
        self._max_bytes = max_bytes
        self._payload_limits = PayloadLimits() if payload_limits is None else payload_limits

    def encode(self, envelope: MessageEnvelope, /) -> bytes:
        """Encode one validated envelope to canonical UTF-8 JSON."""

        if not isinstance(envelope, MessageEnvelope):
            raise InvalidMessageEnvelope("JSON envelope codec requires a MessageEnvelope")
        metadata = envelope.metadata
        encoded_metadata: dict[str, object] = {
            "message_id": metadata.message_id,
            "message_type": metadata.message_type,
            "schema_version": metadata.schema_version,
            "kind": metadata.kind.value,
            "occurred_at": _format_datetime(metadata.occurred_at),
        }
        if metadata.correlation_id is not None:
            encoded_metadata["correlation_id"] = metadata.correlation_id
        if metadata.causation_id is not None:
            encoded_metadata["causation_id"] = metadata.causation_id
        if metadata.deadline is not None:
            encoded_metadata["deadline"] = _format_datetime(metadata.deadline)
        if metadata.policy is not None:
            encoded_metadata["policy"] = {
                "policy_version": metadata.policy.policy_version,
                "decision_digest": metadata.policy.decision_digest,
                "effective_scope": metadata.policy.effective_scope,
                **(
                    {}
                    if metadata.policy.tenant_ref is None
                    else {"tenant_ref": metadata.policy.tenant_ref}
                ),
            }
        if metadata.telemetry is not None:
            encoded_metadata["telemetry"] = dict(metadata.telemetry.to_envelope())
        wire = {
            "version": _ENVELOPE_VERSION,
            "metadata": encoded_metadata,
            "payload": _thaw_json(envelope.payload),
        }
        try:
            encoded = json.dumps(
                wire,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:  # pragma: no cover - envelope prevalidated
            raise InvalidMessageEnvelope("message envelope could not be encoded") from error
        if len(encoded) > self._max_bytes:
            raise InvalidMessageEnvelope("message envelope exceeds its byte limit")
        return encoded

    def decode(self, encoded: bytes, /) -> MessageEnvelope:
        """Validate and decode one bounded UTF-8 JSON envelope."""

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
            raise InvalidMessageEnvelope("message envelope is not valid bounded UTF-8 JSON") from error
        if type(raw) is not dict or set(raw) != _REQUIRED_TOP_LEVEL_FIELDS:
            raise InvalidMessageEnvelope("message envelope top-level fields are invalid")
        if raw.get("version") != _ENVELOPE_VERSION:
            raise InvalidMessageEnvelope("message envelope version is unsupported")
        raw_metadata = raw.get("metadata")
        if type(raw_metadata) is not dict:
            raise InvalidMessageEnvelope("message envelope metadata must be an object")
        metadata_fields = set(raw_metadata)
        if (
            not _REQUIRED_METADATA_FIELDS.issubset(metadata_fields)
            or metadata_fields - _REQUIRED_METADATA_FIELDS - _OPTIONAL_METADATA_FIELDS
        ):
            raise InvalidMessageEnvelope("message envelope metadata fields are invalid")
        raw_kind = raw_metadata.get("kind")
        try:
            kind = EnvelopeKind(raw_kind)
        except (TypeError, ValueError) as error:
            raise InvalidMessageEnvelope("message kind is invalid") from error
        raw_policy = raw_metadata.get("policy")
        policy: PolicyContext | None = None
        if raw_policy is not None:
            if type(raw_policy) is not dict or not {
                "policy_version",
                "decision_digest",
                "effective_scope",
            }.issubset(raw_policy) or set(raw_policy) - _POLICY_FIELDS:
                raise InvalidMessageEnvelope("message policy context fields are invalid")
            policy = PolicyContext(
                policy_version=raw_policy.get("policy_version"),
                decision_digest=raw_policy.get("decision_digest"),
                effective_scope=raw_policy.get("effective_scope"),
                tenant_ref=raw_policy.get("tenant_ref"),
            )
        raw_telemetry = raw_metadata.get("telemetry")
        telemetry: TelemetryContext | None = None
        if raw_telemetry is not None:
            if type(raw_telemetry) is not dict or any(
                type(key) is not str or type(value) is not str
                for key, value in raw_telemetry.items()
            ):
                raise InvalidMessageEnvelope("message telemetry context fields are invalid")
            try:
                telemetry = TelemetryContext.from_envelope(raw_telemetry)
            except (TypeError, ValueError) as error:
                raise InvalidMessageEnvelope("message telemetry context is invalid") from error
        metadata = MessageMetadata(
            message_id=raw_metadata.get("message_id"),
            message_type=raw_metadata.get("message_type"),
            schema_version=raw_metadata.get("schema_version"),
            kind=kind,
            occurred_at=_parse_datetime(
                raw_metadata.get("occurred_at"),
                "message occurrence time",
            ),
            correlation_id=raw_metadata.get("correlation_id"),
            causation_id=raw_metadata.get("causation_id"),
            deadline=(
                None
                if raw_metadata.get("deadline") is None
                else _parse_datetime(raw_metadata.get("deadline"), "message deadline")
            ),
            policy=policy,
            telemetry=telemetry,
        )
        return MessageEnvelope.create(
            metadata=metadata,
            payload=raw.get("payload"),
            limits=self._payload_limits,
        )
