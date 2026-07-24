"""Immutable recursively bounded JSON payload snapshots."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite

from af.messaging.errors import InvalidMessageEnvelope

__all__ = (
    "FrozenJsonArray",
    "FrozenJsonObject",
    "PayloadLimits",
)

_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_MAX_KEY_LENGTH = 200
_MAX_INTEGER = 2**63 - 1
_MIN_INTEGER = -(2**63)
_MAX_VALIDATION_DEPTH = 64
_MAX_VALIDATION_NODES = 1_000_000
_MAX_VALIDATION_STRING_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PayloadLimits:
    """Finite recursive payload budgets applied before admission."""

    max_depth: int = 24
    max_nodes: int = 10_000
    max_string_bytes: int = 65_536

    def __post_init__(self) -> None:
        if type(self.max_depth) is not int or not 1 <= self.max_depth <= _MAX_VALIDATION_DEPTH:
            raise ValueError("payload maximum depth must be between 1 and 64")
        if (
            type(self.max_nodes) is not int
            or not 1 <= self.max_nodes <= _MAX_VALIDATION_NODES
        ):
            raise ValueError("payload maximum nodes must be between 1 and 1000000")
        if (
            type(self.max_string_bytes) is not int
            or not 1 <= self.max_string_bytes <= _MAX_VALIDATION_STRING_BYTES
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
class _Budget:
    nodes: int = 0


def _valid_key(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= _MAX_KEY_LENGTH
        and _UNSAFE_TEXT.search(value) is None
    )


def _freeze_payload(value: object, limits: PayloadLimits) -> FrozenJsonValue:
    """Snapshot JSON-compatible data under caller-selected finite limits."""

    if not isinstance(limits, PayloadLimits):
        raise InvalidMessageEnvelope("message payload limits are invalid")
    return _freeze(
        value,
        limits=limits,
        depth=0,
        budget=_Budget(),
        active=set(),
    )


def _freeze(
    value: object,
    *,
    limits: PayloadLimits,
    depth: int,
    budget: _Budget,
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
        if not _MIN_INTEGER <= value <= _MAX_INTEGER:
            raise InvalidMessageEnvelope(
                "message payload integers must be signed 64-bit values"
            )
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
        return _freeze_mapping(
            value,
            limits=limits,
            depth=depth,
            budget=budget,
            active=active,
        )
    if type(value) in {list, tuple}:
        assert isinstance(value, Sequence)
        return _freeze_sequence(
            value,
            limits=limits,
            depth=depth,
            budget=budget,
            active=active,
        )
    raise InvalidMessageEnvelope("message payload contains a non-JSON value")


def _freeze_mapping(
    value: Mapping[object, object],
    *,
    limits: PayloadLimits,
    depth: int,
    budget: _Budget,
    active: set[int],
) -> FrozenJsonObject:
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
            if not _valid_key(key):
                raise InvalidMessageEnvelope(
                    "message payload object keys must be bounded non-empty single-line text"
                )
            assert isinstance(key, str)
            snapshot.append(
                (
                    key,
                    _freeze(
                        item,
                        limits=limits,
                        depth=depth + 1,
                        budget=budget,
                        active=active,
                    ),
                )
            )
        snapshot.sort(key=lambda pair: pair[0])
        if len({key for key, _ in snapshot}) != len(snapshot):
            raise InvalidMessageEnvelope("message payload object keys must be unique")
        return FrozenJsonObject(tuple(snapshot))
    finally:
        active.remove(identity)


def _freeze_sequence(
    value: Sequence[object],
    *,
    limits: PayloadLimits,
    depth: int,
    budget: _Budget,
    active: set[int],
) -> FrozenJsonArray:
    identity = id(value)
    if identity in active:
        raise InvalidMessageEnvelope("message payload must not contain cycles")
    active.add(identity)
    try:
        return FrozenJsonArray(
            tuple(
                _freeze(
                    item,
                    limits=limits,
                    depth=depth + 1,
                    budget=budget,
                    active=active,
                )
                for item in value
            )
        )
    finally:
        active.remove(identity)


def _thaw_payload(value: FrozenJsonValue) -> object:
    """Return detached built-in JSON containers for encoding or callers."""

    if isinstance(value, FrozenJsonObject):
        return {key: _thaw_payload(item) for key, item in value.items}
    if isinstance(value, FrozenJsonArray):
        return [_thaw_payload(item) for item in value.items]
    return value


def _is_frozen_payload(value: object) -> bool:
    """Reject manually assembled values that bypass snapshot validation."""

    return _validate_frozen(
        value,
        depth=0,
        budget=_Budget(),
        active=set(),
    )


def _validate_frozen(
    value: object,
    *,
    depth: int,
    budget: _Budget,
    active: set[int],
) -> bool:
    if depth > _MAX_VALIDATION_DEPTH:
        return False
    budget.nodes += 1
    if budget.nodes > _MAX_VALIDATION_NODES:
        return False

    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return _MIN_INTEGER <= value <= _MAX_INTEGER
    if type(value) is float:
        return isfinite(value)
    if type(value) is str:
        return len(value.encode("utf-8")) <= _MAX_VALIDATION_STRING_BYTES
    if isinstance(value, FrozenJsonArray):
        return _validate_array(value, depth=depth, budget=budget, active=active)
    if isinstance(value, FrozenJsonObject):
        return _validate_object(value, depth=depth, budget=budget, active=active)
    return False


def _validate_array(
    value: FrozenJsonArray,
    *,
    depth: int,
    budget: _Budget,
    active: set[int],
) -> bool:
    identity = id(value)
    if identity in active or type(value.items) is not tuple:
        return False
    active.add(identity)
    try:
        return all(
            _validate_frozen(
                item,
                depth=depth + 1,
                budget=budget,
                active=active,
            )
            for item in value.items
        )
    finally:
        active.remove(identity)


def _validate_object(
    value: FrozenJsonObject,
    *,
    depth: int,
    budget: _Budget,
    active: set[int],
) -> bool:
    identity = id(value)
    if identity in active or type(value.items) is not tuple:
        return False
    active.add(identity)
    try:
        keys: list[str] = []
        for pair in value.items:
            if type(pair) is not tuple or len(pair) != 2:
                return False
            key, item = pair
            if not _valid_key(key):
                return False
            assert isinstance(key, str)
            keys.append(key)
            if not _validate_frozen(
                item,
                depth=depth + 1,
                budget=budget,
                active=active,
            ):
                return False
        return keys == sorted(keys) and len(keys) == len(set(keys))
    finally:
        active.remove(identity)
