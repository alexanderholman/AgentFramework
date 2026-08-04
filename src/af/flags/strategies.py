"""Rollout strategy implementations for feature flags."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import md5

from .contracts import FlagContext, FlagDefinition, FlagValue, RolloutStrategy

# -- Percentage rollout ------------------------------------------------------


@dataclass
class PercentageRollout(RolloutStrategy):
    """Deterministic percentage rollout based on a context attribute hash."""

    percentage: float  # 0.0 - 100.0
    attribute: str = "user_id"  # context attribute to hash on
    _name: str = field(default="percentage", init=False)

    @property
    def name(self) -> str:
        return self._name

    def evaluate(
        self,
        definition: FlagDefinition,
        context: FlagContext,
    ) -> FlagValue | None:
        attr_value = self._resolve_attribute(context)
        if attr_value is None:
            return None  # defer when attribute is missing

        bucket = self._hash_bucket(attr_value)
        if bucket < (self.percentage / 100.0):
            return FlagValue(
                raw=definition.default_value.raw,
                flag_type=definition.flag_type,
                source="strategy",
            )
        return FlagValue(
            raw=False if definition.flag_type == "boolean" else definition.default_value.raw,
            flag_type=definition.flag_type,
            source="strategy",
        )

    def _resolve_attribute(self, context: FlagContext) -> str | None:
        if self.attribute == "user_id":
            return context.user_id
        if self.attribute == "request_id":
            return context.request_id
        return context.tags.get(self.attribute) or context.metadata.get(self.attribute)

    @staticmethod
    def _hash_bucket(value: str) -> float:
        digest = md5(value.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF


# -- Override ----------------------------------------------------------------


@dataclass
class OverrideStrategy(RolloutStrategy):
    """Force a specific value for matched identifiers."""

    targets: list[str]  # user_ids, request_ids, or tag values
    attribute: str = "user_id"
    value: FlagValue | None = None
    _name: str = field(default="override", init=False)

    @property
    def name(self) -> str:
        return self._name

    def evaluate(
        self,
        definition: FlagDefinition,
        context: FlagContext,
    ) -> FlagValue | None:
        attr_value = self._resolve_attribute(context)
        if attr_value in self.targets:
            if self.value is not None:
                return FlagValue(
                    raw=self.value.raw,
                    flag_type=self.value.flag_type,
                    source="override",
                )
            return FlagValue(
                raw=definition.default_value.raw,
                flag_type=definition.flag_type,
                source="override",
            )
        return None

    def _resolve_attribute(self, context: FlagContext) -> str | None:
        if self.attribute == "user_id":
            return context.user_id
        if self.attribute == "request_id":
            return context.request_id
        return context.tags.get(self.attribute) or context.metadata.get(self.attribute)


# -- Emergency override ------------------------------------------------------


@dataclass
class EmergencyOverride(RolloutStrategy):
    """Global kill-switch that overrides all other strategies."""

    enabled: bool = False
    forced_value: FlagValue | None = None
    _name: str = field(default="emergency", init=False)

    @property
    def name(self) -> str:
        return self._name

    def evaluate(
        self,
        definition: FlagDefinition,
        context: FlagContext,
    ) -> FlagValue | None:
        if not self.enabled:
            return None
        if self.forced_value is not None:
            return FlagValue(
                raw=self.forced_value.raw,
                flag_type=self.forced_value.flag_type,
                source="emergency",
            )
        return FlagValue(
            raw=False,
            flag_type=definition.flag_type,
            source="emergency",
        )
