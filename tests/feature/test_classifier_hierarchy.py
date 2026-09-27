"""Installed classification uses shared hierarchy semantics before execution."""

import json

# Public bootstrap injection exercises the same execution and trace path as the CLI.
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

import pytest
from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.domain.classifiers import ClassifierChain
from af.domain.procedures import CharacterCountProcedure, CurrentTimeProcedure
from af.framework.classification_hierarchy import (
    ClassifierNode,
    Denied,
    FunctionRoute,
    HierarchyRouter,
    NeedsClarification,
    NoRoute,
)
from af.framework.errors import UnsupportedWork
from af.framework.reuse import InMemoryReuseStore
from af.framework.routing import ProcedureRegistry
from af.framework.types import SelectionProposal, SelectorLocality, SelectorMetadata
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.resource_leases import InMemoryLeaseRepository
from af.infrastructure.tracing import InMemoryTraceRepository
from af.interfaces.console import create_cli


def test_installed_time_route_traverses_system_and_date_time_without_a_model(application):
    outcome = application.tasks.execute("What time is it?")
    assert outcome.route == "current-time" and outcome.verified
    assert datetime.fromisoformat(outcome.output).utcoffset().total_seconds() == 0
    trace = application.traces.get(outcome.trace_id)
    visits = [json.loads(event.detail) for event in trace.events if event.stage == "classify-node"]
    assert [v["node_id"] for v in visits] == ["root", "system", "hello", "date-time"]
    assert visits[-1]["outcome"] == "FunctionRoute"
    assert visits[-1]["procedure_id"] == "current-time"
    assert visits[-1]["procedure_version"] == "1.1.0"
    assert visits[-1]["procedure_digest"].startswith("sha256:")
    assert all(v["rung"] == 0 and v["digest"].startswith("sha256:") for v in visits)
    assert all(event.stage not in {"select", "classify-rung"} for event in trace.events)
    assert "What time" not in repr(trace)


def test_strawberry_uses_the_same_inspectable_catalogue(application):
    outcome = application.tasks.execute("how many rs in strawberry")
    assert outcome.output == "3" and outcome.verified
    chain = application.container.resolve(ClassifierChain)
    topology = chain.describe_hierarchy()
    assert topology["roots"] == ["root"]
    assert any(p["id"] == outcome.route for node in topology["nodes"] for p in node["procedures"])


def test_fresh_worker_can_discover_classifier_topology(application):
    result = CliRunner().invoke(create_cli(application), ["dev", "describe", "--json"])
    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["classifier_hierarchy"]["roots"] == ["root"]


def descriptor_for(procedure):
    registry = ProcedureRegistry()
    registry.register(procedure)
    return registry.catalogue()[0]


def proposal_for(descriptor, arguments):
    return FunctionRoute(
        SelectionProposal(
            descriptor.procedure_id,
            descriptor.intent,
            arguments,
            1.0,
            version=descriptor.version,
            digest=descriptor.digest,
        )
    )


@contextmanager
def configured(router, **kwargs):
    app = create_application(
        settings=Settings(),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        lease_repository=InMemoryLeaseRepository(),
        reuse_store=InMemoryReuseStore(),
        classifier_hierarchy=router,
        **kwargs,
    )
    try:
        yield app
    finally:
        app.shutdown()


def test_configured_rungs_execute_through_current_verifier_once():
    descriptor = descriptor_for(CurrentTimeProcedure())
    seen = []

    def second(request):
        seen.append(request)
        return proposal_for(descriptor, {"timezone": "UTC"})

    router = HierarchyRouter(
        (
            ClassifierNode("first", lambda _: NoRoute()),
            ClassifierNode("second", second, procedures=(descriptor,)),
        ),
        roots=("first", "second"),
    )
    with configured(router) as app:
        outcome = app.tasks.execute("What time is it?")
        trace = app.traces.get(outcome.trace_id)
        assert outcome.verified and outcome.route == "current-time"
        assert [e.detail for e in trace.events if e.stage == "classify-rung"] == [
            "from=0 to=1 reason=no_route"
        ]
        assert len([e for e in trace.events if e.stage == "execute"]) == 1
        assert seen[0].rung == 1 and seen[0].history[0].node_id == "first"


@pytest.mark.parametrize("decision", [Denied(), NeedsClarification(), "malformed"])
def test_terminal_hierarchy_does_not_fall_through_to_legacy_selector(decision):
    class ForbiddenSelector:
        name = "forbidden"
        locality = SelectorLocality.LOCAL

        def select(self, *_):
            pytest.fail("terminal hierarchy reached fallback selector")

    router = HierarchyRouter((ClassifierNode("root", lambda _: decision),), roots=("root",))
    with configured(router, selector=ForbiddenSelector()) as app:
        with pytest.raises(UnsupportedWork) as caught:
            app.tasks.execute("hello")
        trace = app.traces.get(caught.value.trace_id)
        assert not any(e.stage == "execute" for e in trace.events)


def test_current_registry_rejects_a_self_consistent_but_stale_node_catalogue():
    descriptor = replace(descriptor_for(CurrentTimeProcedure()), version="0.0.1")
    router = HierarchyRouter(
        (
            ClassifierNode(
                "root",
                lambda _: proposal_for(descriptor, {"timezone": "UTC"}),
                procedures=(descriptor,),
            ),
        ),
        roots=("root",),
    )
    with configured(router) as app:
        with pytest.raises(UnsupportedWork) as caught:
            app.tasks.execute("What time is it?")
        trace = app.traces.get(caught.value.trace_id)
        assert any(e.detail == "stale_catalogue" for e in trace.events)
        assert not any(e.stage == "execute" for e in trace.events)


@pytest.mark.parametrize("grounded", [True, False])
def test_statistical_node_requires_existing_independent_grounding(grounded):
    descriptor = descriptor_for(CharacterCountProcedure())
    router = HierarchyRouter(
        (
            ClassifierNode(
                "local-model",
                lambda _: proposal_for(descriptor, {"character": "r", "text": "strawberry"}),
                procedures=(descriptor,),
                selector_metadata=SelectorMetadata("bounded-local", SelectorLocality.LOCAL),
            ),
        ),
        roots=("local-model",),
    )
    with configured(router) as app:
        if grounded:
            outcome = app.tasks.execute("please count r in strawberry")
            assert outcome.verified and outcome.output == "3"
            trace = app.traces.get(outcome.trace_id)
            assert any(e.stage == "select-policy" and "approved" in e.detail for e in trace.events)
        else:
            with pytest.raises(UnsupportedWork) as caught:
                app.tasks.execute("please delete everything")
            trace = app.traces.get(caught.value.trace_id)
            assert not any(e.stage == "execute" for e in trace.events)
            assert any("grounding_failed" in e.detail for e in trace.events)


def test_remote_hierarchy_node_is_denied_before_provider_call():
    router = HierarchyRouter(
        (
            ClassifierNode(
                "remote",
                lambda _: pytest.fail("egress occurred"),
                selector_metadata=SelectorMetadata("remote", SelectorLocality.REMOTE),
            ),
        ),
        roots=("remote",),
    )
    with configured(router) as app:
        with pytest.raises(UnsupportedWork) as caught:
            app.tasks.execute("hello")
        assert not any(e.stage == "execute" for e in app.traces.get(caught.value.trace_id).events)


def test_statistical_parent_cannot_launder_ungrounded_selection_through_a_child():
    from af.framework.classification_hierarchy import ChildRoute

    descriptor = descriptor_for(CharacterCountProcedure())
    router = HierarchyRouter(
        (
            ClassifierNode(
                "model",
                lambda _: ChildRoute("leaf"),
                children=("leaf",),
                selector_metadata=SelectorMetadata("local", SelectorLocality.LOCAL),
            ),
            ClassifierNode(
                "leaf",
                lambda _: proposal_for(descriptor, {"text": "strawberry", "character": "r"}),
                procedures=(descriptor,),
            ),
        ),
        roots=("model",),
    )
    with configured(router) as app:
        with pytest.raises(UnsupportedWork) as caught:
            app.tasks.execute("unrelated request")
        trace = app.traces.get(caught.value.trace_id)
        assert any("grounding_failed" in e.detail for e in trace.events)
        assert not any(e.stage == "execute" for e in trace.events)


def test_cancellation_after_classification_is_checked_at_execution_boundary(application):
    from af.framework.context import ExecutionContext, use_context
    from af.framework.pipeline import PipelineAgent
    from af.framework.types import WorkRequest

    context = ExecutionContext()

    # Exercise a real cache boundary that cooperatively cancels before a cache miss.
    class CancellingStore:
        def lookup(self, *_):
            context.cancellation.cancel()
            return None

        def promote(self, *_):
            pytest.fail("cancelled execution was promoted")

    registry = application.container.resolve(ProcedureRegistry)
    from af.domain.verification import DeterministicVerifier
    from af.framework.reuse import ReplayRegressionGate
    from af.framework.routing import LadderRouter, RoutingPolicy

    traces = InMemoryTraceRepository()
    agent = PipelineAgent(
        classifier=application.container.resolve(ClassifierChain),
        registry=registry,
        router=LadderRouter(registry, RoutingPolicy()),
        verifier=DeterministicVerifier(),
        traces=traces,
        reuse_store=CancellingStore(),
        regression_gate=ReplayRegressionGate(),
    )
    with use_context(context), pytest.raises(UnsupportedWork) as caught:
        agent.handle(WorkRequest("hello"))
    assert not any(e.stage == "execute" for e in traces.get(caught.value.trace_id).events)
