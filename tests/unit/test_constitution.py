"""Immutable constitutional baseline, narrowing policy and presentation separation."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from af.domain.procedures import CharacterCountProcedure, HelloProcedure
from af.framework.constitution import (
    CONSTITUTION,
    AFConstitution,
    HostRestrictions,
    PresentationPreferences,
    decision_context,
    procedure_decision,
)
from af.framework.context import AcceptanceScope, ExecutionContext, Principal
from af.framework.errors import InvalidRoutingPolicy, UnsupportedWork
from af.framework.routing import LadderRouter, ProcedureRegistry, RoutingPolicy
from af.framework.types import Classification, ProcedureEffect, ProcedureRisk


def test_constitution_cannot_be_reconfigured_and_has_stable_machine_identity():
    assert AFConstitution().digest == CONSTITUTION.digest
    assert CONSTITUTION.version == "1.0.0"
    assert CONSTITUTION.precedence == (
        "constitution",
        "stricter_host_policy",
        "human_consent_and_peer_lease",
        "configuration",
    )
    with pytest.raises(FrozenInstanceError):
        CONSTITUTION.version = "0.0.0"
    with pytest.raises(TypeError):
        AFConstitution(version="0.0.0")
    copy = CONSTITUTION.to_dict()
    copy["principles"] = ()
    assert "non_discrimination" in CONSTITUTION.principles


def test_host_policy_narrows_routes_and_cannot_supply_an_allow_override():
    registry = ProcedureRegistry()
    registry.register(HelloProcedure())
    registry.register(CharacterCountProcedure())
    restrictions = HostRestrictions(denied_procedures=("hello",), maximum_cost=0)
    router = LadderRouter(registry, RoutingPolicy(restrictions=restrictions))
    for classification in (
        Classification("system.hello", {}, 1.0, "test"),
        Classification("text.character_count", {"text": "a", "character": "a"}, 1.0, "test"),
    ):
        with pytest.raises(UnsupportedWork):
            router.route(classification, prompt="test")
    assert restrictions.digest != HostRestrictions().digest
    for values in (
        {"maximum_cost": 10001},
        {"maximum_latency": -1},
        {"maximum_cost": True},
        {"denied_procedures": ["hello"]},
        {"denied_procedures": ("invalid/id",)},
        {"allow_unapproved": True},
    ):
        with pytest.raises((ValueError, TypeError)):
            HostRestrictions(**values)
    with pytest.raises(InvalidRoutingPolicy):
        RoutingPolicy(restrictions={"allow_unapproved": True})
    descriptor = registry.descriptor("hello")
    assert procedure_decision(descriptor).allowed
    for altered in (
        replace(descriptor, effect=ProcedureEffect.SIDE_EFFECT),
        replace(descriptor, risk=ProcedureRisk.ELEVATED),
        replace(descriptor, requires_confirmation=True),
    ):
        assert procedure_decision(altered).allowed is False


def test_decision_projection_preserves_restrictions_and_opaque_authority_only():
    context = ExecutionContext(
        principal=Principal(
            "subject", "user", roles=("operator",), scopes=("read",), claims=(("age", "7"),)
        ),
        tenant_id="tenant",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        effective_scope=AcceptanceScope.CHILD,
        requested_scope=AcceptanceScope.CHILD,
        locale="ar-EG",
    )
    projected = decision_context(context)
    assert projected.principal.subject_id == context.principal.subject_id
    assert projected.principal.roles == ("operator",)
    assert projected.principal.scopes == ("read",)
    assert projected.principal.claims == () and projected.locale is None
    assert projected.effective_scope is AcceptanceScope.CHILD
    assert projected.deadline == context.deadline
    assert projected.tenant_id == context.tenant_id
    assert projected.cancellation is context.cancellation
    assert context.principal.claims == (("age", "7"),)
    preferences = PresentationPreferences(plain_text=True, reduced_motion=True, high_contrast=True)
    assert preferences.plain_text
    with pytest.raises(ValueError):
        PresentationPreferences(plain_text="true")
