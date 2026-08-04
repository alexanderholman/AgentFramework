"""Tests for feature flag evaluation, strategies, and stores."""

from __future__ import annotations

import pytest

from af.flags import (
    EmergencyOverride,
    FlagContext,
    FlagDefinition,
    FlagKey,
    FlagManager,
    FlagValue,
    InMemoryFlagStore,
    OverrideStrategy,
    PercentageRollout,
)
from af.flags.contracts import FlagType

# -- FlagValue tests ----------------------------------------------------------


class TestFlagValue:
    def test_bool_value(self) -> None:
        v = FlagValue(raw=True, flag_type=FlagType.BOOLEAN)
        assert v.as_bool is True

    def test_string_value(self) -> None:
        v = FlagValue(raw="hello", flag_type=FlagType.STRING)
        assert v.as_str == "hello"

    def test_number_value(self) -> None:
        v = FlagValue(raw=42.5, flag_type=FlagType.NUMBER)
        assert v.as_number == 42.5

    def test_json_value(self) -> None:
        data = {"key": "val"}
        v = FlagValue(raw=data, flag_type=FlagType.JSON)
        assert v.as_json == data

    def test_type_mismatch_raises(self) -> None:
        v = FlagValue(raw=True, flag_type=FlagType.BOOLEAN)
        with pytest.raises(TypeError):
            _ = v.as_str


# -- FlagContext tests --------------------------------------------------------


class TestFlagContext:
    def test_default_values(self) -> None:
        ctx = FlagContext()
        assert ctx.user_id is None
        assert ctx.request_id is None
        assert ctx.tags == {}
        assert ctx.metadata == {}

    def test_user_id(self) -> None:
        ctx = FlagContext(user_id="u1")
        assert ctx.user_id == "u1"

    def test_tags(self) -> None:
        ctx = FlagContext(tags={"env": "prod"})
        assert ctx.tags["env"] == "prod"

    def test_memoization(self) -> None:
        ctx = FlagContext()
        key = FlagKey("test")
        val = FlagValue(raw=True, flag_type=FlagType.BOOLEAN)
        ctx.cache_value(key, val)
        assert ctx.get_cached(key) is val
        assert ctx.get_cached(FlagKey("other")) is None


# -- PercentageRollout tests --------------------------------------------------


class TestPercentageRollout:
    def test_deterministic_for_same_identity(self) -> None:
        strategy = PercentageRollout(percentage=50, attribute="user_id")
        ctx = FlagContext(user_id="u1")
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            strategies=[strategy],
        )
        r1 = strategy.evaluate(defn, ctx)
        r2 = strategy.evaluate(defn, ctx)
        assert r1 == r2

    def test_zero_percentage(self) -> None:
        strategy = PercentageRollout(percentage=0, attribute="user_id")
        ctx = FlagContext(user_id="u1")
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            strategies=[strategy],
        )
        result = strategy.evaluate(defn, ctx)
        assert result is not None
        assert result.raw is False

    def test_hundred_percentage(self) -> None:
        strategy = PercentageRollout(percentage=100, attribute="user_id")
        ctx = FlagContext(user_id="u1")
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[strategy],
        )
        result = strategy.evaluate(defn, ctx)
        assert result is not None

    def test_different_users_get_different_results(self) -> None:
        strategy = PercentageRollout(percentage=50, attribute="user_id")
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[strategy],
        )
        results = set()
        for i in range(100):
            ctx = FlagContext(user_id=f"user-{i}")
            r = strategy.evaluate(defn, ctx)
            results.add(r.raw if r else False)
        # With 50% rollout across 100 users, expect both True and False
        assert len(results) == 2

    def test_missing_attribute_defers(self) -> None:
        strategy = PercentageRollout(percentage=100, attribute="user_id")
        ctx = FlagContext()  # no user_id
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[strategy],
        )
        assert strategy.evaluate(defn, ctx) is None


# -- OverrideStrategy tests ---------------------------------------------------


class TestOverrideStrategy:
    def test_matching_target(self) -> None:
        strategy = OverrideStrategy(
            targets=["vip"],
            attribute="user_id",
            value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
        )
        ctx = FlagContext(user_id="vip")
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            strategies=[strategy],
        )
        result = strategy.evaluate(defn, ctx)
        assert result is not None
        assert result.raw is True

    def test_non_matching_target(self) -> None:
        strategy = OverrideStrategy(
            targets=["vip"],
            attribute="user_id",
            value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
        )
        ctx = FlagContext(user_id="regular")
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            strategies=[strategy],
        )
        assert strategy.evaluate(defn, ctx) is None

    def test_tag_attribute(self) -> None:
        strategy = OverrideStrategy(
            targets=["beta"],
            attribute="tier",
            value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
        )
        ctx = FlagContext(tags={"tier": "beta"})
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            strategies=[strategy],
        )
        result = strategy.evaluate(defn, ctx)
        assert result is not None
        assert result.raw is True


# -- EmergencyOverride tests --------------------------------------------------


class TestEmergencyOverride:
    def test_disabled_defers(self) -> None:
        strategy = EmergencyOverride(enabled=False)
        ctx = FlagContext()
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        assert strategy.evaluate(defn, ctx) is None

    def test_force_off(self) -> None:
        strategy = EmergencyOverride(
            enabled=True,
            forced_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
        )
        ctx = FlagContext()
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        result = strategy.evaluate(defn, ctx)
        assert result is not None
        assert result.raw is False

    def test_force_on(self) -> None:
        strategy = EmergencyOverride(
            enabled=True,
            forced_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
        )
        ctx = FlagContext()
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        result = strategy.evaluate(defn, ctx)
        assert result is not None
        assert result.raw is True

    def test_no_forced_value_defaults_false(self) -> None:
        strategy = EmergencyOverride(enabled=True)
        ctx = FlagContext()
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        result = strategy.evaluate(defn, ctx)
        assert result is not None
        assert result.raw is False


# -- InMemoryFlagStore tests --------------------------------------------------


class TestInMemoryFlagStore:
    def test_set_and_get(self) -> None:
        store = InMemoryFlagStore()
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        store.set_definition(defn)
        assert store.get_definition(FlagKey("test")) is defn

    def test_get_missing(self) -> None:
        store = InMemoryFlagStore()
        assert store.get_definition(FlagKey("missing")) is None

    def test_delete_existing(self) -> None:
        store = InMemoryFlagStore()
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        store.set_definition(defn)
        assert store.delete_definition(FlagKey("test")) is True
        assert store.get_definition(FlagKey("test")) is None

    def test_delete_missing(self) -> None:
        store = InMemoryFlagStore()
        assert store.delete_definition(FlagKey("missing")) is False

    def test_list_definitions(self) -> None:
        store = InMemoryFlagStore()
        d1 = FlagDefinition(
            key=FlagKey("a"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        d2 = FlagDefinition(
            key=FlagKey("b"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        store.set_definition(d1)
        store.set_definition(d2)
        defs = store.list_definitions()
        assert len(defs) == 2
        keys = {d.key for d in defs}
        assert FlagKey("a") in keys
        assert FlagKey("b") in keys


# -- FlagManager tests --------------------------------------------------------


class TestFlagManager:
    def _make_manager(self) -> FlagManager:
        return FlagManager(store=InMemoryFlagStore())

    def test_register_and_evaluate_default(self) -> None:
        mgr = self._make_manager()
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.BOOLEAN,
                default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
                strategies=[],
            )
        )
        assert mgr.evaluate_bool(FlagKey("test"), FlagContext()) is True

    def test_evaluate_unregistered_raises(self) -> None:
        mgr = self._make_manager()
        with pytest.raises(KeyError):
            mgr.evaluate(FlagKey("missing"), FlagContext())

    def test_strategy_wins_over_default(self) -> None:
        mgr = self._make_manager()
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.BOOLEAN,
                default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
                strategies=[
                    OverrideStrategy(
                        targets=["vip"],
                        attribute="user_id",
                        value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
                    )
                ],
            )
        )
        assert mgr.evaluate_bool(FlagKey("test"), FlagContext(user_id="vip")) is True

    def test_emergency_override_wins_over_strategy(self) -> None:
        mgr = self._make_manager()
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.BOOLEAN,
                default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
                strategies=[
                    OverrideStrategy(
                        targets=["vip"],
                        attribute="user_id",
                        value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
                    )
                ],
            )
        )
        mgr.set_emergency_override(
            FlagKey("test"),
            EmergencyOverride(
                enabled=True,
                forced_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            ),
        )
        assert mgr.evaluate_bool(FlagKey("test"), FlagContext(user_id="vip")) is False

    def test_clear_emergency_restores_strategy(self) -> None:
        mgr = self._make_manager()
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.BOOLEAN,
                default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
                strategies=[
                    OverrideStrategy(
                        targets=["vip"],
                        attribute="user_id",
                        value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
                    )
                ],
            )
        )
        mgr.set_emergency_override(
            FlagKey("test"),
            EmergencyOverride(
                enabled=True,
                forced_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            ),
        )
        mgr.clear_emergency_override(FlagKey("test"))
        assert mgr.evaluate_bool(FlagKey("test"), FlagContext(user_id="vip")) is True

    def test_memoization_within_context(self) -> None:
        mgr = self._make_manager()
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.BOOLEAN,
                default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
                strategies=[],
            )
        )
        ctx = FlagContext()
        r1 = mgr.evaluate(FlagKey("test"), ctx)
        r2 = mgr.evaluate(FlagKey("test"), ctx)
        assert r1 is r2

    def test_event_handler_called_on_register(self) -> None:
        events: list[tuple[str, FlagKey, object]] = []
        mgr = self._make_manager()
        mgr.on_event(lambda event_type, key, payload: events.append((event_type, key, payload)))
        defn = FlagDefinition(
            key=FlagKey("test"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        mgr.register(defn)
        assert len(events) == 1
        assert events[0][0] == "flag.registered"
        assert events[0][1] == FlagKey("test")

    def test_list_flags(self) -> None:
        mgr = self._make_manager()
        d1 = FlagDefinition(
            key=FlagKey("a"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        d2 = FlagDefinition(
            key=FlagKey("b"),
            flag_type=FlagType.BOOLEAN,
            default_value=FlagValue(raw=False, flag_type=FlagType.BOOLEAN),
            strategies=[],
        )
        mgr.register(d1)
        mgr.register(d2)
        flags = mgr.list_flags()
        assert len(flags) == 2

    def test_unregister(self) -> None:
        mgr = self._make_manager()
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.BOOLEAN,
                default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
                strategies=[],
            )
        )
        assert mgr.unregister(FlagKey("test")) is True
        with pytest.raises(KeyError):
            mgr.evaluate(FlagKey("test"), FlagContext())

    def test_evaluate_str(self) -> None:
        mgr = self._make_manager()
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.STRING,
                default_value=FlagValue(raw="hello", flag_type=FlagType.STRING),
                strategies=[],
            )
        )
        assert mgr.evaluate_str(FlagKey("test"), FlagContext()) == "hello"

    def test_evaluate_number(self) -> None:
        mgr = self._make_manager()
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.NUMBER,
                default_value=FlagValue(raw=3.14, flag_type=FlagType.NUMBER),
                strategies=[],
            )
        )
        assert mgr.evaluate_number(FlagKey("test"), FlagContext()) == 3.14

    def test_evaluate_json(self) -> None:
        mgr = self._make_manager()
        data = {"nested": True}
        mgr.register(
            FlagDefinition(
                key=FlagKey("test"),
                flag_type=FlagType.JSON,
                default_value=FlagValue(raw=data, flag_type=FlagType.JSON),
                strategies=[],
            )
        )
        assert mgr.evaluate_json(FlagKey("test"), FlagContext()) == data

    def test_emergency_invalid_type_raises(self) -> None:
        mgr = self._make_manager()
        with pytest.raises(TypeError):
            mgr.set_emergency_override(FlagKey("test"), "not a strategy")  # type: ignore

    def test_namespace_in_key(self) -> None:
        mgr = self._make_manager()
        key = FlagKey("test", namespace="billing")
        mgr.register(
            FlagDefinition(
                key=key,
                flag_type=FlagType.BOOLEAN,
                default_value=FlagValue(raw=True, flag_type=FlagType.BOOLEAN),
                strategies=[],
            )
        )
        assert mgr.evaluate_bool(key, FlagContext()) is True
        # Different namespace should not match
        with pytest.raises(KeyError):
            mgr.evaluate(FlagKey("test"), FlagContext())
