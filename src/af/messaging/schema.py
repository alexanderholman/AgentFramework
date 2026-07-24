"""Finite message-schema registration and deterministic payload upcasting."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import RLock

from af.messaging.envelopes import EnvelopeKind, MessageEnvelope, PayloadLimits
from af.messaging.errors import (
    InvalidMessageEnvelope,
    InvalidMessageSchema,
    MessageUpcastError,
    UnsupportedMessageSchema,
)

__all__ = (
    "MessageSchema",
    "MessageSchemaRegistry",
    "PayloadUpcaster",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_MAX_SCHEMA_VERSION = 2**31 - 1
_MAX_SCHEMAS = 16_384
_MAX_UPCAST_STEPS = 256

type PayloadUpcaster = Callable[[object], object]


def _message_type(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidMessageSchema("message schema type must be a canonical identifier")
    return value


def _schema_version(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SCHEMA_VERSION:
        raise InvalidMessageSchema(f"{field} must be a positive bounded integer")
    return value


def _is_async_callable(value: object) -> bool:
    if inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value):
        return True
    call = inspect.getattr_static(value, "__call__", None)
    return inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)


@dataclass(frozen=True, slots=True)
class MessageSchema:
    """One exact message role and its supported contiguous schema interval."""

    message_type: str
    kind: EnvelopeKind
    current_version: int
    minimum_version: int = 1

    def __post_init__(self) -> None:
        _message_type(self.message_type)
        if not isinstance(self.kind, EnvelopeKind):
            raise InvalidMessageSchema("message schema kind is invalid")
        minimum = _schema_version(self.minimum_version, "minimum schema version")
        current = _schema_version(self.current_version, "current schema version")
        if minimum > current:
            raise InvalidMessageSchema("minimum schema version cannot exceed current version")
        if current - minimum > _MAX_UPCAST_STEPS:
            raise InvalidMessageSchema("message schema exceeds the maximum upcast chain")


class MessageSchemaRegistry:
    """Mutable declarations frozen into one deterministic upcast authority."""

    def __init__(
        self,
        *,
        payload_limits: PayloadLimits | None = None,
        max_schemas: int = 1_024,
    ) -> None:
        if payload_limits is not None and not isinstance(payload_limits, PayloadLimits):
            raise InvalidMessageSchema("message schema payload limits are invalid")
        if type(max_schemas) is not int or not 1 <= max_schemas <= _MAX_SCHEMAS:
            raise InvalidMessageSchema("message schema registry size limit is invalid")
        self._payload_limits = PayloadLimits() if payload_limits is None else payload_limits
        self._max_schemas = max_schemas
        self._schemas: dict[str, MessageSchema] = {}
        self._upcasters: dict[tuple[str, int], PayloadUpcaster] = {}
        self._frozen = False
        self._lock = RLock()

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def schemas(self) -> tuple[MessageSchema, ...]:
        with self._lock:
            return tuple(self._schemas[key] for key in sorted(self._schemas))

    def register_schema(self, schema: MessageSchema) -> None:
        if not isinstance(schema, MessageSchema):
            raise InvalidMessageSchema("message schema declaration is invalid")
        with self._lock:
            self._require_mutable()
            if schema.message_type in self._schemas:
                raise InvalidMessageSchema("message schema type is already registered")
            if len(self._schemas) >= self._max_schemas:
                raise InvalidMessageSchema("message schema registry is full")
            self._schemas[schema.message_type] = schema

    def register_upcaster(
        self,
        message_type: str,
        from_version: int,
        upcaster: PayloadUpcaster,
    ) -> None:
        normalized_type = _message_type(message_type)
        normalized_version = _schema_version(from_version, "upcaster source version")
        if not callable(upcaster) or _is_async_callable(upcaster):
            raise InvalidMessageSchema("message upcaster must be a synchronous callable")
        with self._lock:
            self._require_mutable()
            schema = self._schemas.get(normalized_type)
            if schema is None:
                raise UnsupportedMessageSchema("message schema type is not registered")
            if not schema.minimum_version <= normalized_version < schema.current_version:
                raise InvalidMessageSchema(
                    "message upcaster source version is outside the schema interval"
                )
            key = (normalized_type, normalized_version)
            if key in self._upcasters:
                raise InvalidMessageSchema("message upcaster step is already registered")
            self._upcasters[key] = upcaster

    def freeze(self) -> None:
        with self._lock:
            if self._frozen:
                return
            for schema in self._schemas.values():
                missing = [
                    version
                    for version in range(schema.minimum_version, schema.current_version)
                    if (schema.message_type, version) not in self._upcasters
                ]
                if missing:
                    raise InvalidMessageSchema("message schema has an incomplete upcast chain")
            self._frozen = True

    def schema_for(self, message_type: str) -> MessageSchema:
        normalized = _message_type(message_type)
        with self._lock:
            schema = self._schemas.get(normalized)
        if schema is None:
            raise UnsupportedMessageSchema("message schema type is not registered")
        return schema

    def normalize(self, envelope: MessageEnvelope) -> MessageEnvelope:
        """Return the exact current schema using every registered contiguous step once."""

        if not isinstance(envelope, MessageEnvelope):
            raise InvalidMessageSchema("message schema normalization requires an envelope")
        with self._lock:
            if not self._frozen:
                raise InvalidMessageSchema("message schema registry must be frozen before normalization")
            schema = self._schemas.get(envelope.metadata.message_type)
            if schema is None:
                raise UnsupportedMessageSchema("message schema type is not registered")
            if envelope.metadata.kind is not schema.kind:
                raise UnsupportedMessageSchema("message kind does not match its registered schema")
            source_version = envelope.metadata.schema_version
            if not schema.minimum_version <= source_version <= schema.current_version:
                raise UnsupportedMessageSchema("message schema version is unsupported")
            upcasters = tuple(
                self._upcasters[(schema.message_type, version)]
                for version in range(source_version, schema.current_version)
            )

        current = envelope
        for upcaster in upcasters:
            current = self._apply_upcaster(current, upcaster)
        return current

    def _apply_upcaster(
        self,
        envelope: MessageEnvelope,
        upcaster: PayloadUpcaster,
    ) -> MessageEnvelope:
        try:
            result = upcaster(envelope.payload_copy())
        except Exception as error:
            raise MessageUpcastError("message payload upcaster failed") from error
        if inspect.iscoroutine(result):
            result.close()
            raise MessageUpcastError("message payload upcaster returned asynchronous work")
        if inspect.isawaitable(result) or inspect.isasyncgen(result):
            raise MessageUpcastError("message payload upcaster returned asynchronous work")
        try:
            return MessageEnvelope.create(
                metadata=replace(
                    envelope.metadata,
                    schema_version=envelope.metadata.schema_version + 1,
                ),
                payload=result,
                limits=self._payload_limits,
            )
        except InvalidMessageEnvelope as error:
            raise MessageUpcastError("message payload upcaster returned invalid data") from error

    def _require_mutable(self) -> None:
        if self._frozen:
            raise InvalidMessageSchema("message schema registry is frozen")
