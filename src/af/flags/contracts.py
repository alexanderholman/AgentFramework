"""Feature-flag domain contracts for AF.

FlagKey  - strongly typed identifier for a single flag.
FlagValue - typed payload returned by the evaluation pipeline.
FlagContext - scoped request- or user-level attributes the strategies may read.
FlagDefinition - static declaration of a flag (type, default, strategies).
RolloutStrategy - protocol that decides a value given context + definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

# -- Keys --------------------------------------------------------------------


@dataclass(frozen=True)
class FlagKey:
    """Immutable identifier for a feature flag."""

    name: str
    namespace: str = "default"

    def __str__(self) -> str:
        return f"{self.namespace}:{self.name}"


# -- Values ------------------------------------------------------------------


class FlagType(StrEnum):
    BOOLEAN = "boolean"
    STRING = "string"
    NUMBER = "number"
    JSON = "json"


@dataclass
class FlagValue:
    """Typed payload for a flag evaluation result."""

    raw: Any
    flag_type: FlagType
    source: str = "default"  # default | override | strategy | emergency

    @property
    def as_bool(self) -> bool:
        if self.flag_type != FlagType.BOOLEAN:
            raise TypeError(f"FlagValue is {self.flag_type}, not boolean")
        return bool(self.raw)

    @property
    def as_str(self) -> str:
        if self.flag_type != FlagType.STRING:
            raise TypeError(f"FlagValue is {self.flag_type}, not string")
        return str(self.raw)

    @property
    def as_number(self) -> float:
        if self.flag_type != FlagType.NUMBER:
            raise TypeError(f"FlagValue is {self.flag_type}, not number")
        return float(self.raw)

    @property
    def as_json(self) -> Any:
        if self.flag_type != FlagType.JSON:
            raise TypeError(f"FlagValue is {self.flag_type}, not json")
        return self.raw


# -- Context -----------------------------------------------------------------


@dataclass
class FlagContext:
    """Scoped attributes available to rollout strategies."""

    user_id: str | None = None
    request_id: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Request-scope evaluation cache (memoization)
    _eval_cache: dict[FlagKey, FlagValue] = field(default_factory=dict, repr=False)

    def cache_value(self, key: FlagKey, value: FlagValue) -> None:
        self._eval_cache[key] = value

    def get_cached(self, key: FlagKey) -> FlagValue | None:
        return self._eval_cache.get(key)


# -- Definition --------------------------------------------------------------


@dataclass
class FlagDefinition:
    """Static declaration of a feature flag."""

    key: FlagKey
    flag_type: FlagType
    default_value: FlagValue
    strategies: list[RolloutStrategy] = field(default_factory=list)
    description: str = ""
    tags: dict[str, str] = field(default_factory=dict)


# -- Strategies --------------------------------------------------------------


@runtime_checkable
class RolloutStrategy(Protocol):
    """Decides whether a flag is enabled (and to what value) for context."""

    def evaluate(
        self,
        definition: FlagDefinition,
        context: FlagContext,
    ) -> FlagValue | None:
        """Return a FlagValue to use, or None to defer to next strategy / default."""
        ...

    @property
    def name(self) -> str: ...


# -- Store contract ----------------------------------------------------------


@runtime_checkable
class FlagStore(Protocol):
    """Persistent or in-memory backing store for flag definitions."""

    def get_definition(self, key: FlagKey) -> FlagDefinition | None: ...

    def set_definition(self, definition: FlagDefinition) -> None: ...

    def delete_definition(self, key: FlagKey) -> bool: ...

    def list_definitions(self) -> list[FlagDefinition]: ...
