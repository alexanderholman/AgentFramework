"""Constitutional policy applies before deterministic execution and host policy."""

from dataclasses import replace

import pytest

from af.application.bus import AuthorizationRequest, Gate, GateDecision, GateEffect
from af.bootstrap import create_application
from af.domain.procedures import CharacterCountProcedure
from af.domain.verification import DeterministicVerifier
from af.framework.container import ServiceProvider
from af.framework.context import (
    AcceptanceScope,
    ExecutionContext,
    Principal,
    current_context,
    use_context,
)
from af.framework.errors import UnsupportedWork
from af.framework.procedure_contributors import (
    InstalledProcedureContribution,
    bind_procedure_contributor,
)
from af.framework.types import (
    Classification,
    ProcedureEffect,
    ProcedureRisk,
    RouteMetrics,
    WorkRequest,
)
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository


def probe_application(
    *, effect=ProcedureEffect.READ_ONLY, risk=ProcedureRisk.NONE, confirmation=False
):
    observations, executions = [], []

    class Classifier:
        def classify(self, request):
            observations.append((request, current_context()))
            if request.prompt != "policy route":
                return None
            return Classification(
                "text.character_count", {"text": "strawberry", "character": "r"}, 1.0, "probe"
            )

    class Procedure(CharacterCountProcedure):
        id = "constitutional-probe"
        metrics = RouteMetrics(cost=0, latency=0, intelligence=0)
        pure = False
        replay_safe = False
        selection_descriptor = replace(
            CharacterCountProcedure.selection_descriptor,
            procedure_id=id,
            effect=effect,
            risk=risk,
            requires_confirmation=confirmation,
        )

        def execute(self, classification):
            executions.append(current_context())
            return super().execute(classification)

    class Provider(ServiceProvider):
        provider_id = "test.constitution"

        def register(self, registrar):
            bind_procedure_contributor(
                registrar,
                InstalledProcedureContribution(Classifier(), Procedure(), DeterministicVerifier()),
            )

    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(Provider(),),
    )
    return application, observations, executions


@pytest.mark.parametrize(
    "options",
    [
        {"effect": ProcedureEffect.SIDE_EFFECT},
        {"risk": ProcedureRisk.ELEVATED},
        {"confirmation": True},
    ],
)
def test_deterministic_contribution_cannot_bypass_consequential_action_policy(options):
    application, _, executions = probe_application(**options)
    with use_context(ExecutionContext()), pytest.raises(UnsupportedWork) as denied:
        application.tasks.create("policy route")
    assert executions == []
    assert application.tasks.list() == []
    trace = application.traces.get(denied.value.trace_id)
    assert trace.verified is False
    assert all(event.stage != "execute" for event in trace.events)


def test_personal_metadata_is_not_a_routing_signal_and_remains_owned_data(application):
    ordinary_application = application
    application, observations, executions = probe_application()
    principal = Principal("same-user", "user", scopes=("tasks",), claims=(("religion", "private"),))
    context = ExecutionContext(principal=principal, tenant_id="same-tenant", locale="ar-EG")
    request = WorkRequest("policy route", {"ethnicity": "private", "presentation": "large-text"})
    with use_context(context):
        outcome = application.agent.handle(request)
        assert current_context() is context
    assert outcome.output == "3"
    classified_request, routing_context = observations[0]
    assert classified_request.prompt == request.prompt
    assert classified_request.attributes == {}
    assert routing_context.principal.subject_id == "same-user"
    assert routing_context.principal.scopes == ("tasks",)
    assert routing_context.principal.claims == ()
    assert routing_context.locale is None
    assert executions == [context]
    assert request.attributes["ethnicity"] == "private"
    # Protected-content vocabulary remains ordinary input to an explicit text operation.
    assert (
        ordinary_application.tasks.count_characters("religion disability ethnicity", "i").output
        == "7"
    )


def test_host_gate_cannot_read_demographic_claims_or_presentation_locale():
    seen = []

    def policy(request, context):
        seen.append((context, current_context()))
        return GateDecision(
            GateEffect.ALLOW,
            policy_version="host-v1",
            reason_code="scope_allowed",
            effective_scope=AcceptanceScope.DETERMINISTIC,
        )

    gate = Gate(policy_version="host-v1", policies=(policy,))
    context = ExecutionContext(
        principal=Principal("user", "user", scopes=("read",), claims=(("disability", "private"),)),
        locale="fr-CA",
    )
    request = AuthorizationRequest("read", "task", "record", AcceptanceScope.DETERMINISTIC)
    with use_context(context):
        assert gate.decide(request, context).effect is GateEffect.ALLOW
        assert current_context() is context
    supplied, ambient = seen[0]
    assert supplied == ambient
    assert supplied.principal.scopes == ("read",)
    assert supplied.principal.claims == () and supplied.locale is None
