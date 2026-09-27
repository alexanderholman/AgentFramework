"""Recover #54 hierarchy intent without the historical parallel executor."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from af.domain.procedures import CurrentTimeProcedure
from af.framework.classification_hierarchy import (
    ChildRoute,
    ClassifierFailure,
    ClassifierNode,
    Denied,
    FunctionRoute,
    HierarchyRouter,
    NeedsClarification,
    NoRoute,
    RouteBudget,
)
from af.framework.context import AcceptanceScope, ExecutionContext
from af.framework.routing import ProcedureRegistry
from af.framework.types import SelectionProposal, WorkRequest


def clock_descriptor():
    registry = ProcedureRegistry()
    registry.register(CurrentTimeProcedure())
    return registry.catalogue()[0]


def time_route(descriptor=None, **changes):
    descriptor = descriptor or clock_descriptor()
    proposal = SelectionProposal(
        descriptor.procedure_id,
        descriptor.intent,
        {"timezone": "UTC"},
        1.0,
        version=descriptor.version,
        digest=descriptor.digest,
    )
    return FunctionRoute(replace(proposal, **changes))


def test_parent_recovers_from_wrong_child_and_selects_one_installed_function():
    seen = []
    descriptor = clock_descriptor()

    def clock(request):
        seen.append(request)
        return time_route(descriptor)

    router = HierarchyRouter(
        (
            ClassifierNode("root", lambda _: ChildRoute("weather"), children=("weather", "clock")),
            ClassifierNode("weather", lambda _: NoRoute()),
            ClassifierNode("clock", clock, procedures=(descriptor,)),
        ),
        roots=("root",),
    )
    context = ExecutionContext(effective_scope=AcceptanceScope.DETERMINISTIC)
    result = router.route(WorkRequest("What time is it?"), context=context)
    assert isinstance(result.outcome, FunctionRoute)
    assert [visit.node_id for visit in result.visits] == ["root", "weather", "clock"]
    assert len(seen) == 1
    assert seen[0].scope is AcceptanceScope.DETERMINISTIC
    assert result.escalations == ()
    assert result.outcome.proposal.procedure_id == "current-time"


def test_root_exhaustion_escalates_exactly_once_with_preserved_history():
    seen = []
    descriptor = clock_descriptor()

    def higher(request):
        seen.append(request)
        return time_route()

    router = HierarchyRouter(
        (
            ClassifierNode("root", lambda _: ChildRoute("empty"), children=("empty",)),
            ClassifierNode("empty", lambda _: NoRoute()),
            ClassifierNode("second", higher, procedures=(descriptor,)),
        ),
        roots=("root", "second"),
    )
    result = router.route(WorkRequest("unchanged request"))
    assert isinstance(result.outcome, FunctionRoute)
    assert result.escalations == ((0, 1),)
    assert seen[0].prompt == "unchanged request"
    assert seen[0].rung == 1
    assert [visit.node_id for visit in seen[0].history] == ["root", "empty"]


@pytest.mark.parametrize(
    "decision", [Denied(), NeedsClarification(), ClassifierFailure("unavailable")]
)
def test_terminal_decisions_do_not_try_siblings_or_higher_rungs(decision):
    calls = []
    router = HierarchyRouter(
        (
            ClassifierNode(
                "root", lambda _: ChildRoute("blocked"), children=("blocked", "sibling")
            ),
            ClassifierNode("blocked", lambda _: decision),
            ClassifierNode("sibling", lambda _: calls.append("sibling") or NoRoute()),
            ClassifierNode("higher", lambda _: calls.append("higher") or NoRoute()),
        ),
        roots=("root", "higher"),
    )
    result = router.route(WorkRequest("request"))
    assert result.outcome == decision
    assert calls == []
    assert result.escalations == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"procedure_id": "uninstalled"},
        {"version": "0.0.1"},
        {"digest": "sha256:" + "0" * 64},
        {"arguments": {"arbitrary": "value"}},
        {"confidence": float("nan")},
    ],
)
def test_invalid_function_routes_never_leave_the_node_catalogue(changes):
    router = HierarchyRouter(
        (
            ClassifierNode(
                "root",
                lambda _: time_route(**changes),
                procedures=(clock_descriptor(),),
            ),
        ),
        roots=("root",),
    )
    assert isinstance(router.route(WorkRequest("time")).outcome, ClassifierFailure)


def test_low_confidence_is_clarification_and_not_permission_to_escalate():
    router = HierarchyRouter(
        (
            ClassifierNode(
                "root",
                lambda _: time_route(confidence=0.2),
                procedures=(clock_descriptor(),),
            ),
        ),
        roots=("root",),
    )
    assert isinstance(router.route(WorkRequest("time")).outcome, NeedsClarification)


@pytest.mark.parametrize("decision", [ChildRoute("unknown"), object(), "current-time"])
def test_malformed_or_out_of_scope_child_decision_fails_closed(decision):
    router = HierarchyRouter((ClassifierNode("root", lambda _: decision),), roots=("root",))
    result = router.route(WorkRequest("private request"))
    assert isinstance(result.outcome, ClassifierFailure)
    assert result.escalations == ()
    assert "private request" not in repr(result)


def test_catalogue_cycles_duplicate_children_and_unreachable_nodes_are_rejected():
    for nodes in (
        (ClassifierNode("root", lambda _: NoRoute(), children=("root",)),),
        (
            ClassifierNode("root", lambda _: NoRoute(), children=("leaf", "leaf")),
            ClassifierNode("leaf", lambda _: NoRoute()),
        ),
        (
            ClassifierNode("root", lambda _: NoRoute()),
            ClassifierNode("orphan", lambda _: NoRoute()),
        ),
    ):
        with pytest.raises(ValueError):
            HierarchyRouter(nodes, roots=("root",))


@pytest.mark.parametrize(
    "budget",
    [
        RouteBudget(max_nodes=1),
        RouteBudget(max_calls=1),
        RouteBudget(max_depth=1),
        RouteBudget(max_cost=0),
    ],
)
def test_budget_exhaustion_is_terminal_before_another_classifier_call(budget):
    calls = []
    router = HierarchyRouter(
        (
            ClassifierNode("root", lambda _: ChildRoute("leaf"), children=("leaf",), cost=1),
            ClassifierNode("leaf", lambda _: calls.append("leaf") or NoRoute()),
        ),
        roots=("root",),
        budget=budget,
    )
    result = router.route(WorkRequest("time"))
    assert isinstance(result.outcome, ClassifierFailure)
    assert result.outcome.reason == "budget_exhausted"
    assert calls == []


def test_cancellation_after_child_prevents_sibling_and_escalation():
    context = ExecutionContext()
    calls = []

    def cancel(_):
        context.cancellation.cancel()
        return NoRoute()

    router = HierarchyRouter(
        (
            ClassifierNode("root", lambda _: ChildRoute("first"), children=("first", "second")),
            ClassifierNode("first", cancel),
            ClassifierNode("second", lambda _: calls.append("second") or NoRoute()),
        ),
        roots=("root",),
    )
    result = router.route(WorkRequest("time"), context=context)
    assert isinstance(result.outcome, ClassifierFailure)
    assert result.outcome.reason == "cancelled"
    assert calls == []


def test_expired_context_and_late_classifier_results_cannot_route():
    now = datetime(2026, 9, 26, tzinfo=UTC)
    calls = []
    router = HierarchyRouter(
        (ClassifierNode("root", lambda _: calls.append(1) or NoRoute()),),
        roots=("root",),
        wall_clock=lambda: now,
    )
    result = router.route(
        WorkRequest("time"), context=ExecutionContext(deadline=now - timedelta(seconds=1))
    )
    assert isinstance(result.outcome, ClassifierFailure)
    assert result.outcome.reason == "timeout"
    assert calls == []

    instant = [0.0]

    def late(_):
        instant[0] = 2.0
        return time_route()

    router = HierarchyRouter(
        (ClassifierNode("root", late, procedures=(clock_descriptor(),)),),
        roots=("root",),
        clock=lambda: instant[0],
        budget=RouteBudget(timeout_ms=100),
    )
    result = router.route(WorkRequest("time"))
    assert isinstance(result.outcome, ClassifierFailure)
    assert result.outcome.reason == "timeout"


def test_classifier_exception_is_sanitized_and_not_an_exhausted_branch():
    def broken(_):
        raise RuntimeError("private provider failure")

    router = HierarchyRouter((ClassifierNode("root", broken),), roots=("root",))
    result = router.route(WorkRequest("private prompt"))
    assert isinstance(result.outcome, ClassifierFailure)
    assert result.outcome.reason == "classifier_failure"
    assert "private" not in repr(result)


def test_topology_is_inspectable_without_calling_classifiers():
    router = HierarchyRouter(
        (ClassifierNode("root", lambda _: pytest.fail("discovery executed")),), roots=("root",)
    )
    document = router.describe()
    assert document["roots"] == ["root"]
    assert document["nodes"][0]["id"] == "root"
    assert document["nodes"][0]["digest"].startswith("sha256:")


@pytest.mark.parametrize("value", [-1, 0, 1025, True, 1.5])
def test_invalid_call_budget_is_rejected(value):
    with pytest.raises(ValueError):
        RouteBudget(max_calls=value)


@pytest.mark.parametrize(
    "name,value", [("max_depth", 65), ("max_rungs", 9), ("timeout_ms", 60001), ("max_cost", -1)]
)
def test_invalid_depth_rung_time_and_cost_budget_is_rejected(name, value):
    with pytest.raises(ValueError):
        RouteBudget(**{name: value})


@pytest.mark.parametrize("budget", [RouteBudget(max_nodes_per_rung=1), RouteBudget(max_rungs=1)])
def test_rung_exhaustion_does_not_call_a_higher_provider(budget):
    seen = []
    router = HierarchyRouter(
        (
            ClassifierNode("root", lambda _: ChildRoute("leaf"), children=("leaf",)),
            ClassifierNode("leaf", lambda _: NoRoute()),
            ClassifierNode("next", lambda _: seen.append(1) or NoRoute()),
        ),
        roots=("root", "next"),
        budget=budget,
    )
    result = router.route(WorkRequest("request"))
    assert result.outcome == ClassifierFailure("budget_exhausted")
    assert seen == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "0"])
def test_invalid_monotonic_clock_never_calls_provider(bad):
    router = HierarchyRouter(
        (ClassifierNode("root", lambda _: pytest.fail("called")),),
        roots=("root",),
        clock=lambda: bad,
    )
    assert router.route(WorkRequest("request")).outcome == ClassifierFailure("clock_invalid")


def test_backward_clock_and_invalid_wall_clock_fail_closed():
    ticks = iter([2.0, 1.0])
    router = HierarchyRouter(
        (ClassifierNode("root", lambda _: pytest.fail("called")),),
        roots=("root",),
        clock=lambda: next(ticks),
    )
    assert router.route(WorkRequest("request")).outcome == ClassifierFailure("clock_invalid")
    router = HierarchyRouter(
        (ClassifierNode("root", lambda _: pytest.fail("called")),),
        roots=("root",),
        wall_clock=lambda: datetime(2026, 1, 1),
    )
    assert router.route(
        WorkRequest("request"), context=ExecutionContext(deadline=datetime.now(UTC))
    ).outcome == ClassifierFailure("clock_invalid")


def test_accidentally_async_result_is_closed_and_never_executed():
    seen = []

    async def accidental():
        seen.append(1)
        return NoRoute()

    router = HierarchyRouter((ClassifierNode("root", lambda _: accidental()),), roots=("root",))
    assert router.route(WorkRequest("request")).outcome == ClassifierFailure("malformed")
    assert seen == []
    with pytest.raises(TypeError):
        ClassifierNode("root", accidental)


def test_route_attempts_do_not_share_history_or_budget():
    histories = []
    router = HierarchyRouter(
        (ClassifierNode("root", lambda request: histories.append(request.history) or NoRoute()),),
        roots=("root",),
        budget=RouteBudget(max_calls=1),
    )
    assert router.route(WorkRequest("first")).outcome == NoRoute()
    assert router.route(WorkRequest("second")).outcome == NoRoute()
    assert histories == [(), ()]
