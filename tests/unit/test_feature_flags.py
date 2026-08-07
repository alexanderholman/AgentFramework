from __future__ import annotations

from datetime import UTC, datetime

import pytest

from af.domain.feature_flags import (
    EvaluationContext,
    FeatureFlag,
    FeatureFlagEvaluator,
    FeatureFlagState,
    RolloutStrategy,
)


class InMemoryFlagStore:
    """Test implementation of FeatureFlagStore."""

    def __init__(self) -> None:
        self._flags: dict[str, FeatureFlag] = {}

    def get(self, key: str) -> FeatureFlag | None:
        return self._flags.get(key.lower().strip())

    def set(self, flag: FeatureFlag) -> None:
        self._flags[flag.normalized_key] = flag

    def delete(self, key: str) -> bool:
        normalized = key.lower().strip()
        return self._flags.pop(normalized, None) is not None

    def list_all(self) -> list[FeatureFlag]:
        return sorted(self._flags.values(), key=lambda f: f.created_at)


# -- FeatureFlag validation tests --


def test_feature_flag_rejects_invalid_key_formats() -> None:
    with pytest.raises(ValueError):
        FeatureFlag(
            key="123invalid",
            description="starts with number",
            state=FeatureFlagState.INACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
            enabled=False,
        )

    with pytest.raises(ValueError):
        FeatureFlag(
            key="",
            description="empty key",
            state=FeatureFlagState.INACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
            enabled=False,
        )

    with pytest.raises(ValueError):
        FeatureFlag(
            key="a" * 129,
            description="too long",
            state=FeatureFlagState.INACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
            enabled=False,
        )


def test_feature_flag_rejects_empty_description() -> None:
    with pytest.raises(ValueError):
        FeatureFlag(
            key="valid_key",
            description="   ",
            state=FeatureFlagState.INACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
            enabled=False,
        )


def test_feature_flag_rejects_invalid_rollout_percentage() -> None:
    with pytest.raises(ValueError):
        FeatureFlag(
            key="valid_key",
            description="test",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.PERCENTAGE,
            enabled=True,
            percentage=-1.0,
        )

    with pytest.raises(ValueError):
        FeatureFlag(
            key="valid_key",
            description="test",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.PERCENTAGE,
            enabled=True,
            percentage=101.0,
        )


def test_feature_flag_rejects_allowlist_strategy_without_entries() -> None:
    with pytest.raises(ValueError):
        FeatureFlag(
            key="valid_key",
            description="test",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.ALLOWLIST,
            enabled=True,
            allowlist=(),
        )


def test_feature_flag_accepts_valid_configuration() -> None:
    flag = FeatureFlag(
        key="dark_mode",
        description="Enable dark mode UI",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.PERCENTAGE,
        enabled=True,
            percentage=50.0,
    )

    assert flag.key == "dark_mode"
    assert flag.normalized_key == "dark_mode"
    assert flag.state == FeatureFlagState.ACTIVE
    assert flag.rollout_strategy == RolloutStrategy.PERCENTAGE
    assert flag.enabled is True
    assert flag.percentage == 50.0
    assert flag.updated_at is None


def test_feature_flag_key_normalization() -> None:
    flag = FeatureFlag(
        key="  Dark_Mode  ",
        description="normalized key test",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
    )

    assert flag.normalized_key == "dark_mode"


def test_feature_flag_with_state_creates_new_instance() -> None:
    datetime.now(UTC)
    flag = FeatureFlag(
        key="test_flag",
        description="test",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
    )

    deprecated = flag.with_state(FeatureFlagState.DEPRECATED)

    assert deprecated.state == FeatureFlagState.DEPRECATED
    assert deprecated.updated_at is not None
    assert flag.state == FeatureFlagState.ACTIVE
    assert flag.updated_at is None


def test_feature_flag_toggle_inverts_enabled_state() -> None:
    flag = FeatureFlag(
        key="test_flag",
        description="test",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
    )

    toggled = flag.toggle()

    assert toggled.enabled is False
    assert toggled.updated_at is not None
    assert flag.enabled is True


# -- EvaluationContext tests --


def test_evaluation_context_rejects_empty_subject() -> None:
    with pytest.raises(ValueError):
        EvaluationContext(subject_id="")

    with pytest.raises(ValueError):
        EvaluationContext(subject_id="   ")


def test_evaluation_context_accepts_valid_subject() -> None:
    ctx = EvaluationContext(
        subject_id="user-123",
        attributes={"tier": "premium", "region": "eu"},
    )

    assert ctx.subject_id == "user-123"
    assert ctx.attributes["tier"] == "premium"


# -- FeatureFlagEvaluator tests --


def test_evaluator_returns_false_for_missing_flag() -> None:
    store = InMemoryFlagStore()
    evaluator = FeatureFlagEvaluator(store)

    ctx = EvaluationContext(subject_id="user-123")
    assert evaluator.evaluate("nonexistent", ctx) is False


def test_evaluator_returns_false_for_deprecated_flag() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="old_feature",
        description="deprecated feature",
        state=FeatureFlagState.DEPRECATED,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)
    ctx = EvaluationContext(subject_id="user-123")
    assert evaluator.evaluate("old_feature", ctx) is False


def test_evaluator_returns_false_for_disabled_flag() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="disabled_feature",
        description="disabled feature",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=False,
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)
    ctx = EvaluationContext(subject_id="user-123")
    assert evaluator.evaluate("disabled_feature", ctx) is False


def test_evaluator_all_strategy_returns_true() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="public_feature",
        description="available to all",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)
    ctx = EvaluationContext(subject_id="user-123")
    assert evaluator.evaluate("public_feature", ctx) is True


def test_evaluator_none_strategy_returns_false() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="hidden_feature",
        description="hidden from all",
        state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.NONE,
        enabled=True,
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)
    ctx = EvaluationContext(subject_id="user-123")
    assert evaluator.evaluate("hidden_feature", ctx) is False


def test_evaluator_percentage_strategy_is_deterministic() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="beta_feature",
        description="50% rollout",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.PERCENTAGE,
        enabled=True,
            percentage=50.0,
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)
    ctx = EvaluationContext(subject_id="user-123")

    result1 = evaluator.evaluate("beta_feature", ctx)
    result2 = evaluator.evaluate("beta_feature", ctx)

    assert result1 == result2


def test_evaluator_percentage_zero_returns_false() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="zero_rollout",
        description="0% rollout",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.PERCENTAGE,
        enabled=True,
            percentage=0.0,
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)
    ctx = EvaluationContext(subject_id="user-123")
    assert evaluator.evaluate("zero_rollout", ctx) is False


def test_evaluator_percentage_hundred_returns_true() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="full_rollout",
        description="100% rollout",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.PERCENTAGE,
        enabled=True,
            percentage=100.0,
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)
    ctx = EvaluationContext(subject_id="user-123")
    assert evaluator.evaluate("full_rollout", ctx) is True


def test_evaluator_allowlist_matches_subject_id() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="vip_feature",
        description="VIP only",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALLOWLIST,
        enabled=True,
        allowlist=("user-123", "user-456"),
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)

    assert evaluator.evaluate("vip_feature", EvaluationContext(subject_id="user-123")) is True
    assert evaluator.evaluate("vip_feature", EvaluationContext(subject_id="user-999")) is False


def test_evaluator_allowlist_matches_attribute() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="premium_feature",
        description="Premium tier only",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALLOWLIST,
        enabled=True,
        allowlist=("premium", "enterprise"),
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)

    assert (
        evaluator.evaluate(
            "premium_feature",
            EvaluationContext(subject_id="user-789", attributes={"tier": "premium"}),
        )
        is True
    )
    assert (
        evaluator.evaluate(
            "premium_feature",
            EvaluationContext(subject_id="user-789", attributes={"tier": "free"}),
        )
        is False
    )


def test_evaluator_gradual_strategy_delegates_to_percentage() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="gradual_feature",
        description="gradual rollout",
        state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.GRADUAL,
        enabled=True,
            percentage=100.0,
    )
    store.set(flag)

    evaluator = FeatureFlagEvaluator(store)
    ctx = EvaluationContext(subject_id="user-123")
    assert evaluator.evaluate("gradual_feature", ctx) is True


# -- InMemoryFlagStore tests --


def test_in_memory_store_set_and_get() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="test_flag",
        description="test",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
    )

    store.set(flag)
    retrieved = store.get("test_flag")

    assert retrieved is not None
    assert retrieved.key == "test_flag"
    assert retrieved.enabled is True


def test_in_memory_store_delete() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="test_flag",
        description="test",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
    )

    store.set(flag)
    assert store.delete("test_flag") is True
    assert store.get("test_flag") is None
    assert store.delete("test_flag") is False


def test_in_memory_store_list_all_ordered() -> None:
    store = InMemoryFlagStore()

    flag1 = FeatureFlag(
        key="first",
        description="first",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    flag2 = FeatureFlag(
        key="second",
        description="second",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    store.set(flag2)
    store.set(flag1)

    flags = store.list_all()
    assert len(flags) == 2
    assert flags[0].key == "first"
    assert flags[1].key == "second"


def test_in_memory_store_key_normalization() -> None:
    store = InMemoryFlagStore()
    flag = FeatureFlag(
        key="  Test_Flag  ",
        description="normalized",
        state=FeatureFlagState.ACTIVE,
        rollout_strategy=RolloutStrategy.ALL,
        enabled=True,
    )

    store.set(flag)
    assert store.get("test_flag") is not None
    assert store.get("  TEST_FLAG  ") is not None
