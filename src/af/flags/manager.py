"""Flag evaluation manager with strategy resolution and event emission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .contracts import (
    FlagContext,
    FlagDefinition,
    FlagKey,
    FlagStore,
    FlagValue,
    RolloutStrategy,
)


@runtime_checkable
class FlagEventHandler(Protocol):
    def __call__(self, event_type: str, key: FlagKey, payload: object) -> None: ...


@dataclass
class FlagManager:
    """Central evaluation manager for feature flags.

    Follows the AF manager/driver pattern: a single FlagStore driver is bound
    at construction time, and the manager orchestrates evaluation, caching,
    and event emission.
    """

    store: FlagStore
    event_handlers: list[FlagEventHandler] = field(default_factory=list, repr=False)
    _emergency_overrides: dict[FlagKey, RolloutStrategy] = field(default_factory=dict, repr=False)

    # -- Registration --------------------------------------------------------

    def register(self, definition: FlagDefinition) -> None:
        """Persist a flag definition through the bound store."""
        self.store.set_definition(definition)
        self._emit_event("flag.registered", definition.key, definition)

    def unregister(self, key: FlagKey) -> bool:
        """Remove a flag definition."""
        removed = self.store.delete_definition(key)
        if removed:
            self._emit_event("flag.unregistered", key, None)
        return removed

    def list_flags(self) -> list[FlagDefinition]:
        return self.store.list_definitions()

    # -- Emergency overrides -------------------------------------------------

    def set_emergency_override(
        self,
        key: FlagKey,
        strategy: RolloutStrategy,
    ) -> None:
        if not isinstance(strategy, RolloutStrategy):
            raise TypeError("Emergency override must implement RolloutStrategy")
        self._emergency_overrides[key] = strategy
        self._emit_event("flag.emergency_override", key, strategy)

    def clear_emergency_override(self, key: FlagKey) -> None:
        self._emergency_overrides.pop(key, None)
        self._emit_event("flag.emergency_cleared", key, None)

    # -- Evaluation ----------------------------------------------------------

    def evaluate(
        self,
        key: FlagKey,
        context: FlagContext,
    ) -> FlagValue:
        """Evaluate a flag for the given context.

        Order of precedence:
        1. Request-scope cache (memoization)
        2. Emergency override
        3. Flag definition strategies (first non-None wins)
        4. Default value
        """
        cached = context.get_cached(key)
        if cached is not None:
            return cached

        emergency = self._emergency_overrides.get(key)
        if emergency is not None:
            definition = self.store.get_definition(key)
            result = emergency.evaluate(definition, context) if definition else None
            if result is not None:
                context.cache_value(key, result)
                return result

        definition = self.store.get_definition(key)
        if definition is None:
            raise KeyError(f"Flag {key} is not registered")

        result = self._evaluate_strategies(definition, context)
        context.cache_value(key, result)
        return result

    def evaluate_bool(
        self,
        key: FlagKey,
        context: FlagContext,
    ) -> bool:
        return self.evaluate(key, context).as_bool

    def evaluate_str(
        self,
        key: FlagKey,
        context: FlagContext,
    ) -> str:
        return self.evaluate(key, context).as_str

    def evaluate_number(
        self,
        key: FlagKey,
        context: FlagContext,
    ) -> float:
        return self.evaluate(key, context).as_number

    def evaluate_json(
        self,
        key: FlagKey,
        context: FlagContext,
    ) -> object:
        return self.evaluate(key, context).as_json

    # -- Internals -----------------------------------------------------------

    def _evaluate_strategies(
        self,
        definition: FlagDefinition,
        context: FlagContext,
    ) -> FlagValue:
        for strategy in definition.strategies:
            result = strategy.evaluate(definition, context)
            if result is not None:
                return result
        return definition.default_value

    def on_event(self, handler: FlagEventHandler) -> None:
        """Register a callback for flag lifecycle events."""
        self.event_handlers.append(handler)

    def _emit_event(self, event_type: str, key: FlagKey, payload: object) -> None:
        for handler in self.event_handlers:
            handler(event_type, key, payload)
