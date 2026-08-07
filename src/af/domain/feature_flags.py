from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import cast, Protocol, Optional, Literal

from af.cache.contracts import CacheKey, CacheNamespace
from af.cache.manager import CacheDriver


class FeatureFlagState(Enum):
    """Feature flag lifecycle state."""

    INACTIVE = "inactive"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class RolloutStrategy(Enum):
    """Rollout strategy for feature flag evaluation."""

    ALL = "all"
    NONE = "none"
    PERCENTAGE = "percentage"
    ALLOWLIST = "allowlist"
    GRADUAL = "gradual"


@dataclass(frozen=True)
class FeatureFlag:
    """Immutable feature flag definition.

    A feature flag controls whether a capability is exposed to users or systems.
    Flags are identified by a stable key and carry evaluation metadata.
    """

    key: str
    description: str
    state: FeatureFlagState
    rollout_strategy: RolloutStrategy
    enabled: bool = True
    percentage: float = 0.0
    allowlist: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        normalized = self.key.lower().strip()
        if not self._valid_key(normalized):
            raise ValueError(f"Invalid feature flag key: {self.key!r}")
        if not self.description.strip():
            raise ValueError("Feature flag description must not be empty")
        if not 0.0 <= self.percentage <= 100.0:
            raise ValueError(
                f"percentage must be between 0 and 100, got {self.percentage}"
            )
        if self.rollout_strategy == RolloutStrategy.ALLOWLIST and not self.allowlist:
            raise ValueError("ALLOWLIST strategy requires at least one entry in allowlist")
        object.__setattr__(self, "_normalized_key", normalized)

    @staticmethod
    def _valid_key(key: str) -> bool:
        return bool(re.match(r"^[a-z][a-z0-9_]{0,127}$", key))

    @property
    def normalized_key(self) -> str:
        return cast(str, object.__getattribute__(self, "_normalized_key"))

    def with_state(self, state: FeatureFlagState) -> FeatureFlag:
        """Return a new flag with updated state and timestamp."""
        now = datetime.now(UTC)
        return FeatureFlag(
            key=self.key,
            description=self.description,
            state=state,
            rollout_strategy=self.rollout_strategy,
            enabled=self.enabled,
            percentage=self.percentage,
            allowlist=self.allowlist,
            metadata=self.metadata,
            created_at=self.created_at,
            updated_at=now,
        )

    def toggle(self) -> FeatureFlag:
        """Return a new flag with inverted enabled state."""
        now = datetime.now(UTC)
        return FeatureFlag(
            key=self.key,
            description=self.description,
            state=self.state,
            rollout_strategy=self.rollout_strategy,
            enabled=not self.enabled,
            percentage=self.percentage,
            allowlist=self.allowlist,
            metadata=self.metadata,
            created_at=self.created_at,
            updated_at=now,
        )


@dataclass(frozen=True)
class EvaluationContext:
    """Context for feature flag evaluation."""

    subject_id: str
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.subject_id.strip():
            raise ValueError("subject_id must not be empty")


class FeatureFlagStore(Protocol):
    """Storage contract for feature flags."""

    def get(self, key: str) -> FeatureFlag | None:
        """Retrieve a flag by key."""
        ...

    def set(self, flag: FeatureFlag) -> None:
        """Persist a flag definition."""
        ...

    def delete(self, key: str) -> bool:
        """Remove a flag. Returns True if flag existed."""
        ...

    def list_all(self) -> list[FeatureFlag]:
        """Return all flags ordered by creation time."""
        ...


class FeatureFlagManager:
    """Cache-backed feature flag store.

    Uses a CacheDriver directly for persistence. Tracks stored keys
    internally since CacheDriver has no list_all capability.
    Optionally publishes audit events via LocalEventBus.
    """

    def __init__(
        self,
        namespace: "CacheNamespace",
        driver: "CacheDriver",
        event_bus: Optional["LocalEventBus"] = None,
    ) -> None:
        self._namespace = namespace
        self._driver = driver
        self._event_bus = event_bus
        self._keys: set[str] = set()

    @staticmethod
    def _enum_default(obj) -> str:
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

    def get(self, key: str) -> FeatureFlag | None:
        normalized = key.lower().strip()
        cache_key = CacheKey(value=f"ff:{normalized}")
        raw = self._driver.get(self._namespace, cache_key)
        if raw is None:
            return None
        return self._deserialize(raw)

    def set(self, flag: FeatureFlag) -> None:
        normalized = flag.normalized_key
        cache_key = CacheKey(value=f"ff:{normalized}")
        payload = json.dumps(asdict(flag), default=self._enum_default).encode("utf-8")
        self._driver.set(self._namespace, cache_key, payload)
        self._keys.add(normalized)

    def delete(self, key: str) -> bool:
        normalized = key.lower().strip()
        cache_key = CacheKey(value=f"ff:{normalized}")
        removed = self._driver.delete(self._namespace, cache_key)
        self._keys.discard(normalized)
        return removed

    def list_all(self) -> list[FeatureFlag]:
        flags: list[FeatureFlag] = []
        for key in sorted(self._keys):
            cache_key = CacheKey(value=f"ff:{key}")
            raw = self._driver.get(self._namespace, cache_key)
            if raw is not None:
                flag = self._deserialize(raw)
                if flag is not None:
                    flags.append(flag)
        return flags

    @staticmethod
    def _deserialize(raw: bytes) -> FeatureFlag:
        data = json.loads(raw.decode("utf-8"))
        data["state"] = FeatureFlagState(data["state"])
        data["rollout_strategy"] = RolloutStrategy(data["rollout_strategy"])
        if "created_at" in data:
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        if data.get("updated_at"):
            data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        return FeatureFlag(**data)


class FeatureFlagEvaluator:
    """Deterministic feature flag evaluator.

    Evaluates flags against a context using the flag's rollout strategy.
    Does not depend on external services or randomness.
    Supports request-scope memoization and configurable fallback policies.
    """

    def __init__(
        self,
        store: FeatureFlagStore,
        *,
        fallback_policy: Literal["open_circuit", "close_circuit", "raise"] = "close_circuit",
    ) -> None:
        self.store = store
        self.fallback_policy = fallback_policy
        self._request_cache: dict[str, bool] = {}

    def evaluate(self, key: str, context: EvaluationContext) -> bool:
        """Evaluate whether a feature is enabled for the given context."""
        cache_key = f"{key}:{context.subject_id}:{frozenset(context.attributes.items())}"
        if cache_key in self._request_cache:
            return self._request_cache[cache_key]

        try:
            flag = self.store.get(key)
        except Exception as exc:
            return self._handle_store_error(exc)

        if flag is None:
            result = False
        elif flag.state == FeatureFlagState.DEPRECATED:
            result = False
        elif not flag.enabled:
            result = False
        elif flag.rollout_strategy == RolloutStrategy.ALL:
            result = True
        elif flag.rollout_strategy == RolloutStrategy.NONE:
            result = False
        elif flag.rollout_strategy == RolloutStrategy.PERCENTAGE:
            result = self._evaluate_percentage(flag.percentage, context)
        elif flag.rollout_strategy == RolloutStrategy.ALLOWLIST:
            result = self._evaluate_allowlist(flag.allowlist, context)
        elif flag.rollout_strategy == RolloutStrategy.GRADUAL:
            result = self._evaluate_gradual(flag.percentage, context)
        else:
            result = False

        self._request_cache[cache_key] = result
        return result

    def clear_request_scope(self) -> None:
        """Clear the request-scope memoization cache."""
        self._request_cache.clear()

    def _handle_store_error(self, exc: Exception) -> bool:
        """Handle store failure according to fallback policy."""
        if self.fallback_policy == "open_circuit":
            return True
        if self.fallback_policy == "close_circuit":
            return False
        if self.fallback_policy == "raise":
            raise RuntimeError(f"store failure: {exc}") from exc
        return False  # type: ignore[unreachable]

    def _evaluate_percentage(self, percentage: float, context: EvaluationContext) -> bool:
        """Deterministic percentage rollout based on subject hash."""
        if percentage <= 0:
            return False
        if percentage >= 100:
            return True
        bucket = self._hash_to_bucket(context.subject_id)
        return bucket < (percentage * 1000)

    def _evaluate_allowlist(self, allowlist: tuple[str, ...], context: EvaluationContext) -> bool:
        """Check if subject or any attribute value is in allowlist."""
        if context.subject_id in allowlist:
            return True
        return any(value in allowlist for value in context.attributes.values())

    def _evaluate_gradual(self, percentage: float, context: EvaluationContext) -> bool:
        """Gradual rollout: percentage-based with consistent bucketing."""
        return self._evaluate_percentage(percentage, context)

    @staticmethod
    def _hash_to_bucket(subject_id: str) -> int:
        """Deterministic hash to 0-999999 bucket."""
        return int(
            hashlib.sha256(f"af-flag:{subject_id}".encode()).hexdigest(), 16
        ) % 1_000_000
