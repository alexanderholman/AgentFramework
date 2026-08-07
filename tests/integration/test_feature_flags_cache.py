from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import pytest

from af.cache import CacheKey, CacheNamespace, MemoryCacheDriver
from af.domain.feature_flags import (
    EvaluationContext,
    FeatureFlag,
    FeatureFlagEvaluator,
    FeatureFlagState,
    RolloutStrategy,
)
from af.domain.feature_flags import FeatureFlagManager, FeatureFlagStore
from af.messaging import EnvelopeKind, LocalEventBus, MessageEnvelope, MessageMetadata
from af.messaging.schema import MessageSchema, MessageSchemaRegistry


# --- Fixtures ---


def _namespace() -> CacheNamespace:
    return CacheNamespace(
        application_id="af",
        tenant_id="test",
        name="feature_flags",
    )


def _store() -> FeatureFlagStore:
    driver = MemoryCacheDriver()
    return FeatureFlagManager(
        namespace=_namespace(),
        driver=driver,
    )


def _registry() -> MessageSchemaRegistry:
    registry = MessageSchemaRegistry()
    registry.register_schema(
        MessageSchema(
            message_type="feature_flag.evaluated",
            kind=EnvelopeKind.INTEGRATION_EVENT,
            current_version=1,
        )
    )
    registry.freeze()
    return registry


def _event_bus() -> LocalEventBus:
    return LocalEventBus(_registry())


# --- Tests: CRUD through cache-backed store ---


class TestFeatureFlagManagerCrud:
    def test_set_and_get_flag(self) -> None:
        store = _store()
        flag = FeatureFlag(
            key="test_flag",
            description="Test flag",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
        )
        store.set(flag)
        retrieved = store.get("test_flag")
        assert retrieved is not None
        assert retrieved.key == "test_flag"
        assert retrieved.rollout_strategy == RolloutStrategy.ALL

    def test_get_missing_flag(self) -> None:
        store = _store()
        assert store.get("nonexistent") is None

    def test_delete_flag(self) -> None:
        store = _store()
        flag = FeatureFlag(
            key="delete_me",
            description="Will be deleted",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.NONE,
        )
        store.set(flag)
        assert store.get("delete_me") is not None
        store.delete("delete_me")
        assert store.get("delete_me") is None

    def test_list_all_flags(self) -> None:
        store = _store()
        flags = [
            FeatureFlag(
                key=f"flag_{i}",
                description=f"Flag {i}",
                state=FeatureFlagState.ACTIVE,
                rollout_strategy=RolloutStrategy.ALL,
            )
            for i in range(3)
        ]
        for flag in flags:
            store.set(flag)
        listed = store.list_all()
        assert len(listed) == 3
        keys = {f.key for f in listed}
        assert keys == {"flag_0", "flag_1", "flag_2"}

    def test_overwrite_flag(self) -> None:
        store = _store()
        original = FeatureFlag(
            key="overwrite_test",
            description="Original",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
        )
        updated = FeatureFlag(
            key="overwrite_test",
            description="Updated",
            state=FeatureFlagState.DEPRECATED,
            rollout_strategy=RolloutStrategy.NONE,
        )
        store.set(original)
        store.set(updated)
        retrieved = store.get("overwrite_test")
        assert retrieved is not None
        assert retrieved.state == FeatureFlagState.DEPRECATED
        assert retrieved.description == "Updated"

    def test_key_normalization_in_store(self) -> None:
        store = _store()
        flag = FeatureFlag(
            key="  Test_Flag  ",
            description="Normalization test",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
        )
        store.set(flag)
        assert store.get("test_flag") is not None
        assert store.get("  Test_Flag  ") is not None


# --- Tests: Request-scope memoization ---


class TestFeatureFlagManagerMemoization:
    def test_request_scope_memoization(self) -> None:
        store = _store()
        flag = FeatureFlag(
            key="memo_test",
            description="Memoization test",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
        )
        store.set(flag)
        evaluator = FeatureFlagEvaluator(store)
        ctx = EvaluationContext(subject_id="user-1")

        result1 = evaluator.evaluate("memo_test", ctx)
        result2 = evaluator.evaluate("memo_test", ctx)
        assert result1 is True
        assert result2 is True

    def test_new_request_scope_clears_memo(self) -> None:
        store = _store()
        flag = FeatureFlag(
            key="memo_clear",
            description="Memo clear test",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
        )
        store.set(flag)
        evaluator = FeatureFlagEvaluator(store)
        ctx = EvaluationContext(subject_id="user-1")

        result1 = evaluator.evaluate("memo_clear", ctx)
        assert result1 is True
        evaluator.clear_request_scope()
        result2 = evaluator.evaluate("memo_clear", ctx)
        assert result2 is True

    def test_memoization_isolation_between_subjects(self) -> None:
        store = _store()
        flag = FeatureFlag(
            key="isolation_test",
            description="Isolation test",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.PERCENTAGE,
            percentage=50,
        )
        store.set(flag)
        evaluator = FeatureFlagEvaluator(store)
        ctx1 = EvaluationContext(subject_id="user-1")
        ctx2 = EvaluationContext(subject_id="user-2")

        result1a = evaluator.evaluate("isolation_test", ctx1)
        result1b = evaluator.evaluate("isolation_test", ctx1)
        result2 = evaluator.evaluate("isolation_test", ctx2)
        assert result1a == result1b


# --- Tests: Fallback policy ---


class TestFeatureFlagManagerFallback:
    def test_fallback_on_store_error(self) -> None:
        failing_store = FailingStore()
        evaluator = FeatureFlagEvaluator(failing_store, fallback_policy="open_circuit")
        ctx = EvaluationContext(subject_id="user-1")
        result = evaluator.evaluate("any_flag", ctx)
        assert result is True

    def test_close_circuit_fallback(self) -> None:
        failing_store = FailingStore()
        evaluator = FeatureFlagEvaluator(failing_store, fallback_policy="close_circuit")
        ctx = EvaluationContext(subject_id="user-1")
        result = evaluator.evaluate("any_flag", ctx)
        assert result is False

    def test_raise_fallback_policy(self) -> None:
        failing_store = FailingStore()
        evaluator = FeatureFlagEvaluator(failing_store, fallback_policy="raise")
        ctx = EvaluationContext(subject_id="user-1")
        with pytest.raises(RuntimeError, match="store"):
            evaluator.evaluate("any_flag", ctx)


# --- Tests: Audit trail with event bus ---


class TestFeatureFlagManagerAuditTrail:
    def test_flag_change_emits_event(self) -> None:
        bus = _event_bus()
        store = _store()
        manager = FeatureFlagManager(
            namespace=_namespace(),
            driver=MemoryCacheDriver(),
            event_bus=bus,
        )
        flag = FeatureFlag(
            key="audit_test",
            description="Audit test",
            state=FeatureFlagState.ACTIVE,
            rollout_strategy=RolloutStrategy.ALL,
        )
        events: list[MessageEnvelope] = []

        def listener(envelope: MessageEnvelope) -> None:
            events.append(envelope)

        bus.subscribe("feature_flag.evaluated", listener)
        bus.freeze()

        manager.set(flag)
        assert len(events) == 0
        evaluator = FeatureFlagEvaluator(store)
        evaluator.evaluate("audit_test", EvaluationContext(subject_id="user-1"))
        assert len(events) == 0


# --- Helper: Failing store for fallback tests ---


class FailingStore(FeatureFlagStore):
    def get(self, key: str) -> FeatureFlag | None:
        raise RuntimeError("store failure")

    def set(self, flag: FeatureFlag) -> None:
        raise RuntimeError("store failure")

    def delete(self, key: str) -> None:
        raise RuntimeError("store failure")

    def list_all(self) -> list[FeatureFlag]:
        raise RuntimeError("store failure")
