from __future__ import annotations

from dataclasses import replace
from itertools import permutations
from typing import Any, cast

import pytest

from af.framework.capabilities import (
    CapabilityCandidate,
    CapabilityClass,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityLifecycle,
    CapabilityLocality,
    CapabilityReasonCode,
    CapabilityResourceEnvelope,
    CapabilityRisk,
    CapabilityRoute,
    ExactCapabilityMatcher,
    RequestRequirements,
)


def _resources(
    *,
    latency_ms: int = 10,
    memory_mib: int = 64,
    cpu_millis: int = 100,
    accelerator_millis: int = 0,
    estimated_cost_micros: int = 1,
) -> CapabilityResourceEnvelope:
    return CapabilityResourceEnvelope(
        latency_ms=latency_ms,
        memory_mib=memory_mib,
        cpu_millis=cpu_millis,
        accelerator_millis=accelerator_millis,
        estimated_cost_micros=estimated_cost_micros,
    )


def _descriptor(
    suffix: str,
    capability_class: CapabilityClass,
    *,
    procedure_id: str | None,
    workflow_id: str | None = None,
    deployment_id: str | None = "deployment.local",
    dependency_ids: tuple[str, ...] = (),
    fallback_ids: tuple[str, ...] = (),
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=f"capability-{suffix}",
        asset_id=f"asset-{suffix}",
        provider_id="provider-local",
        runtime_id="runtime-python",
        deployment_id=deployment_id,
        procedure_id=procedure_id,
        workflow_id=workflow_id,
        capability_class=capability_class,
        modality="text",
        domain="assistant.general",
        intent="assistant.respond",
        input_schema="schema.prompt.v1",
        output_schema="schema.response.v1",
        languages=("en",),
        locales=("en-US",),
        max_input_bytes=4_096,
        max_output_bytes=4_096,
        max_context_items=8,
        declared_quality_evidence=(f"declared-{suffix}",),
        verifier_requirements=("verifier.response",),
        effects=(CapabilityEffect.READ_ONLY,),
        risk=CapabilityRisk.LOW,
        locality=CapabilityLocality.LOCAL_ONLY,
        licence="LicenseRef-AF-Local",
        declared_state=CapabilityLifecycle.DECLARED,
        resource_envelope=_resources(),
        dependency_ids=dependency_ids,
        fallback_ids=fallback_ids,
    )


def _requirements(
    suffix: str,
    capability_class: CapabilityClass,
    *,
    accepted_lifecycles: tuple[CapabilityLifecycle, ...] = (
        CapabilityLifecycle.BENCHMARKED,
        CapabilityLifecycle.DEPLOYED,
        CapabilityLifecycle.DEGRADED,
    ),
    require_authorized: bool = True,
    require_verified_quality: bool = True,
    max_resources: CapabilityResourceEnvelope | None = None,
) -> RequestRequirements:
    return RequestRequirements(
        request_id=f"request-{suffix}",
        capability_classes=(capability_class,),
        modality="text",
        domain="assistant.general",
        intent="assistant.respond",
        input_schema="schema.prompt.v1",
        output_schema="schema.response.v1",
        language="en",
        locale="en-US",
        input_bytes=128,
        expected_output_bytes=128,
        context_items=2,
        required_verifiers=("verifier.response",),
        allowed_effects=(CapabilityEffect.READ_ONLY,),
        max_risk=CapabilityRisk.LOW,
        accepted_localities=(CapabilityLocality.LOCAL_ONLY,),
        accepted_licenses=("LicenseRef-AF-Local",),
        accepted_lifecycles=accepted_lifecycles,
        require_authorized=require_authorized,
        require_verified_quality=require_verified_quality,
        max_resources=max_resources or _resources(latency_ms=25, memory_mib=256, cpu_millis=250),
        required_runtime_id="runtime-python",
    )


def _candidate(
    descriptor: CapabilityDescriptor,
    *,
    measured_state: CapabilityLifecycle = CapabilityLifecycle.DEPLOYED,
    quality_verified: bool = True,
    authorized: bool = True,
    measured_quality_evidence: tuple[str, ...] = ("measured",),
    estimated_resources: CapabilityResourceEnvelope | None = None,
    reason: CapabilityReasonCode = CapabilityReasonCode.UNSUPPORTED,
    exact_match: bool = False,
) -> CapabilityCandidate:
    return CapabilityCandidate(
        descriptor=descriptor,
        measured_state=measured_state,
        quality_verified=quality_verified,
        authorized=authorized,
        measured_quality_evidence=measured_quality_evidence,
        estimated_resources=estimated_resources or _resources(),
        reason=reason,
        exact_match=exact_match,
    )


def test_reason_codes_are_stable_and_complete() -> None:
    assert tuple(code.value for code in CapabilityReasonCode) == (
        "match",
        "partial_match",
        "excessive_cost",
        "unavailable",
        "unverified",
        "prohibited",
        "ambiguous",
        "unsupported",
    )


@pytest.mark.parametrize(
    ("suffix", "capability_class", "procedure_id", "workflow_id", "deployment_id"),
    [
        ("static", CapabilityClass.STATIC, None, None, "deployment.local"),
        ("procedural", CapabilityClass.PROCEDURAL, "procedure.respond", None, "deployment.local"),
        ("model", CapabilityClass.MODEL, None, None, "deployment.remote"),
        ("workflow", CapabilityClass.WORKFLOW, None, "workflow.respond", "deployment.local"),
        ("human", CapabilityClass.HUMAN, None, None, None),
    ],
)
def test_capability_contract_fixtures_round_trip_canonically_and_match_exact_requirements(
    suffix: str,
    capability_class: CapabilityClass,
    procedure_id: str | None,
    workflow_id: str | None,
    deployment_id: str | None,
) -> None:
    descriptor = _descriptor(
        suffix,
        capability_class,
        procedure_id=procedure_id,
        workflow_id=workflow_id,
        deployment_id=deployment_id,
    )
    requirements = _requirements(suffix, capability_class)
    route = ExactCapabilityMatcher().match(requirements, (_candidate(descriptor),))
    evaluated = route.candidates[0]

    assert CapabilityDescriptor.from_dict(descriptor.to_dict()) == descriptor
    assert RequestRequirements.from_dict(requirements.to_dict()) == requirements
    assert CapabilityCandidate.from_dict(evaluated.to_dict()) == evaluated
    assert CapabilityRoute.from_dict(route.to_dict()) == route
    assert route.selected_capability_id == descriptor.capability_id
    assert route.reason is CapabilityReasonCode.MATCH
    assert evaluated.reason is CapabilityReasonCode.MATCH
    assert evaluated.exact_match is True
    assert CapabilityRoute.from_dict(route.to_dict()).digest == route.digest


def test_matcher_is_order_invariant_and_exact_ties_fail_closed() -> None:
    matcher = ExactCapabilityMatcher()
    requirements = _requirements("order", CapabilityClass.PROCEDURAL)
    selected = _candidate(
        _descriptor("selected", CapabilityClass.PROCEDURAL, procedure_id="procedure.selected"),
        estimated_resources=_resources(latency_ms=5, memory_mib=32, cpu_millis=50),
    )
    rejected = _candidate(
        _descriptor("rejected", CapabilityClass.PROCEDURAL, procedure_id="procedure.rejected"),
        estimated_resources=_resources(latency_ms=6, memory_mib=48, cpu_millis=60),
    )

    digests = {
        matcher.match(requirements, ordering).digest
        for ordering in permutations((rejected, selected), 2)
    }
    ordered = matcher.match(requirements, (rejected, selected))
    ambiguous = matcher.match(
        requirements,
        (
            _candidate(
                _descriptor("tie-a", CapabilityClass.PROCEDURAL, procedure_id="procedure.tie_a"),
                estimated_resources=_resources(latency_ms=5, memory_mib=32, cpu_millis=50),
            ),
            _candidate(
                _descriptor("tie-b", CapabilityClass.PROCEDURAL, procedure_id="procedure.tie_b"),
                estimated_resources=_resources(latency_ms=5, memory_mib=32, cpu_millis=50),
            ),
        ),
    )

    assert len(digests) == 1
    assert ordered.selected_capability_id == "capability-selected"
    assert ambiguous.selected_capability_id is None
    assert ambiguous.reason is CapabilityReasonCode.AMBIGUOUS


@pytest.mark.parametrize(
    ("candidate", "requirements", "expected_reason"),
    [
        (
            _candidate(
                _descriptor(
                    "deployment",
                    CapabilityClass.PROCEDURAL,
                    procedure_id="procedure.deploy",
                ),
                measured_state=CapabilityLifecycle.BENCHMARKED,
            ),
            _requirements(
                "deployment",
                CapabilityClass.PROCEDURAL,
                accepted_lifecycles=(CapabilityLifecycle.DEPLOYED,),
            ),
            CapabilityReasonCode.UNAVAILABLE,
        ),
        (
            _candidate(
                _descriptor(
                    "quality",
                    CapabilityClass.PROCEDURAL,
                    procedure_id="procedure.quality",
                ),
                quality_verified=False,
                measured_quality_evidence=(),
            ),
            _requirements("quality", CapabilityClass.PROCEDURAL),
            CapabilityReasonCode.UNVERIFIED,
        ),
        (
            _candidate(
                _descriptor("auth", CapabilityClass.PROCEDURAL, procedure_id="procedure.auth"),
                authorized=False,
            ),
            _requirements("auth", CapabilityClass.PROCEDURAL),
            CapabilityReasonCode.PROHIBITED,
        ),
    ],
)
def test_capability_claims_do_not_imply_deployment_authorization_or_verified_quality(
    candidate: CapabilityCandidate,
    requirements: RequestRequirements,
    expected_reason: CapabilityReasonCode,
) -> None:
    route = ExactCapabilityMatcher().match(requirements, (candidate,))

    assert route.selected_capability_id is None
    assert route.reason is expected_reason
    assert route.candidates[0].reason is expected_reason


def test_contracts_reject_unknown_fields_invalid_bounds_and_impossible_state_combinations() -> None:
    descriptor = _descriptor(
        "invalid",
        CapabilityClass.PROCEDURAL,
        procedure_id="procedure.invalid",
    )
    payload = descriptor.to_dict()
    payload["unknown"] = True

    with pytest.raises(ValueError, match="unknown fields"):
        CapabilityDescriptor.from_dict(payload)

    payload = descriptor.to_dict()
    payload["max_input_bytes"] = 0
    with pytest.raises(ValueError, match="input bytes"):
        CapabilityDescriptor.from_dict(payload)

    with pytest.raises(ValueError, match="verified quality"):
        _candidate(
            descriptor,
            measured_state=CapabilityLifecycle.DECLARED,
            quality_verified=True,
        )

    with pytest.raises(ValueError, match="exact match"):
        _candidate(
            descriptor,
            reason=CapabilityReasonCode.PARTIAL_MATCH,
            exact_match=True,
        )


def test_matcher_rejects_unknown_dependency_and_fallback_cycles() -> None:
    matcher = ExactCapabilityMatcher()
    requirements = _requirements("relationships", CapabilityClass.PROCEDURAL)
    unknown_dependency = _candidate(
        replace(
            _descriptor("depends", CapabilityClass.PROCEDURAL, procedure_id="procedure.depends"),
            dependency_ids=("capability-missing",),
        )
    )
    fallback_a = _candidate(
        replace(
            _descriptor("fallback-a", CapabilityClass.PROCEDURAL, procedure_id="procedure.a"),
            fallback_ids=("capability-fallback-b",),
        )
    )
    fallback_b = _candidate(
        replace(
            _descriptor("fallback-b", CapabilityClass.PROCEDURAL, procedure_id="procedure.b"),
            fallback_ids=("capability-fallback-a",),
        )
    )

    with pytest.raises(ValueError, match="unknown capability relationship"):
        matcher.match(requirements, (unknown_dependency,))

    with pytest.raises(ValueError, match="fallback relationships must be acyclic"):
        matcher.match(requirements, (fallback_a, fallback_b))


def test_matcher_emits_excessive_cost_and_partial_match_reason_codes() -> None:
    costly = _candidate(
        _descriptor("costly", CapabilityClass.PROCEDURAL, procedure_id="procedure.costly"),
        estimated_resources=_resources(latency_ms=250, memory_mib=256, cpu_millis=250),
    )
    partial = _candidate(
        replace(
            _descriptor("partial", CapabilityClass.PROCEDURAL, procedure_id="procedure.partial"),
            locales=("fr-FR",),
        )
    )
    route = ExactCapabilityMatcher().match(
        _requirements(
            "costly",
            CapabilityClass.PROCEDURAL,
            max_resources=_resources(latency_ms=50, memory_mib=128, cpu_millis=150),
        ),
        (costly,),
    )
    partial_route = ExactCapabilityMatcher().match(
        _requirements("partial", CapabilityClass.PROCEDURAL),
        (partial,),
    )

    assert route.reason is CapabilityReasonCode.EXCESSIVE_COST
    assert route.candidates[0].reason is CapabilityReasonCode.EXCESSIVE_COST
    assert partial_route.reason is CapabilityReasonCode.PARTIAL_MATCH
    assert partial_route.candidates[0].reason is CapabilityReasonCode.PARTIAL_MATCH


def test_contract_digests_are_stable_sha256_tokens() -> None:
    descriptor = _descriptor("digest", CapabilityClass.PROCEDURAL, procedure_id="procedure.digest")
    requirements = _requirements("digest", CapabilityClass.PROCEDURAL)
    candidate = _candidate(descriptor)
    route = ExactCapabilityMatcher().match(requirements, (candidate,))

    assert _resources().digest.startswith("sha256:")
    assert descriptor.digest.startswith("sha256:")
    assert requirements.digest.startswith("sha256:")
    assert candidate.digest.startswith("sha256:")
    assert route.digest.startswith("sha256:")


def test_descriptors_requests_candidates_and_routes_fail_closed_for_invalid_shapes() -> None:
    descriptor = _descriptor(
        "invalid-shapes",
        CapabilityClass.PROCEDURAL,
        procedure_id="procedure.ok",
    )
    requirements = _requirements("invalid-shapes", CapabilityClass.PROCEDURAL)
    candidate = _candidate(descriptor)

    with pytest.raises(ValueError, match="resource schema version"):
        replace(_resources(), schema_version="2.0.0")
    with pytest.raises(ValueError, match="estimated cost"):
        _resources(estimated_cost_micros=-1)
    with pytest.raises(ValueError, match="must be an object"):
        CapabilityResourceEnvelope.from_dict([])
    with pytest.raises(ValueError, match="missing required fields"):
        CapabilityResourceEnvelope.from_dict({})

    with pytest.raises(ValueError, match="capability class is invalid"):
        replace(descriptor, capability_class=cast(Any, "procedural"))
    with pytest.raises(ValueError, match="capability risk is invalid"):
        replace(descriptor, risk=cast(Any, "low"))
    with pytest.raises(ValueError, match="capability locality is invalid"):
        replace(descriptor, locality=cast(Any, "local_only"))
    with pytest.raises(ValueError, match="capability lifecycle is invalid"):
        replace(descriptor, declared_state=cast(Any, "declared"))
    with pytest.raises(ValueError, match="resource envelope is invalid"):
        replace(descriptor, resource_envelope=cast(Any, {}))
    with pytest.raises(ValueError, match="must not reference self"):
        replace(descriptor, dependency_ids=(descriptor.capability_id,))
    with pytest.raises(ValueError, match="must not overlap"):
        replace(descriptor, dependency_ids=("capability-a",), fallback_ids=("capability-a",))
    with pytest.raises(ValueError, match="must be unique"):
        replace(descriptor, declared_quality_evidence=("dup", "dup"))
    with pytest.raises(ValueError, match="must not be empty"):
        replace(descriptor, effects=())
    with pytest.raises(ValueError, match="procedural capabilities require a procedure"):
        replace(descriptor, procedure_id=None)
    with pytest.raises(ValueError, match="workflow capabilities require a workflow"):
        replace(
            _descriptor(
                "workflow-invalid",
                CapabilityClass.WORKFLOW,
                procedure_id=None,
                workflow_id="workflow.valid",
            ),
            procedure_id="procedure.invalid",
        )
    with pytest.raises(ValueError, match="cannot bind both procedure and workflow"):
        replace(
            _descriptor("static-invalid", CapabilityClass.STATIC, procedure_id=None),
            procedure_id="procedure.invalid",
            workflow_id="workflow.invalid",
        )

    with pytest.raises(ValueError, match="request requirements schema version"):
        replace(requirements, schema_version="2.0.0")
    with pytest.raises(ValueError, match="must not be empty"):
        replace(requirements, capability_classes=())
    with pytest.raises(ValueError, match="request max risk is invalid"):
        replace(requirements, max_risk=cast(Any, "low"))
    with pytest.raises(ValueError, match="request max resources are invalid"):
        replace(requirements, max_resources=cast(Any, {}))
    with pytest.raises(ValueError, match="request runtime identifier"):
        replace(requirements, required_runtime_id="not valid")

    with pytest.raises(ValueError, match="candidate schema version"):
        replace(candidate, schema_version="2.0.0")
    with pytest.raises(ValueError, match="candidate descriptor is invalid"):
        replace(candidate, descriptor=cast(Any, {}))
    with pytest.raises(ValueError, match="measured state is invalid"):
        replace(candidate, measured_state=cast(Any, "deployed"))
    with pytest.raises(ValueError, match="resources are invalid"):
        replace(candidate, estimated_resources=cast(Any, {}))
    with pytest.raises(ValueError, match="candidate reason is invalid"):
        replace(candidate, reason=cast(Any, "unsupported"))
    with pytest.raises(ValueError, match="match reason requires exact match"):
        replace(candidate, reason=CapabilityReasonCode.MATCH, exact_match=False)

    with pytest.raises(ValueError, match="route requirements are invalid"):
        CapabilityRoute(
            requirements=cast(Any, {}),
            candidates=(),
            selected_capability_id=None,
            reason=CapabilityReasonCode.UNSUPPORTED,
        )
    with pytest.raises(ValueError, match="route candidates are invalid"):
        CapabilityRoute(
            requirements=requirements,
            candidates=cast(Any, []),
            selected_capability_id=None,
            reason=CapabilityReasonCode.UNSUPPORTED,
        )
    with pytest.raises(ValueError, match="candidates must be unique"):
        CapabilityRoute(
            requirements=requirements,
            candidates=(candidate, candidate),
            selected_capability_id=None,
            reason=CapabilityReasonCode.UNSUPPORTED,
        )
    with pytest.raises(ValueError, match="selection must reference a candidate"):
        CapabilityRoute(
            requirements=requirements,
            candidates=(replace(candidate, reason=CapabilityReasonCode.MATCH, exact_match=True),),
            selected_capability_id="capability-missing",
            reason=CapabilityReasonCode.MATCH,
        )
    with pytest.raises(ValueError, match="route reason is invalid"):
        CapabilityRoute(
            requirements=requirements,
            candidates=(),
            selected_capability_id=None,
            reason=cast(Any, "unsupported"),
        )
    with pytest.raises(ValueError, match="match requires a selected capability"):
        CapabilityRoute(
            requirements=requirements,
            candidates=(candidate,),
            selected_capability_id=None,
            reason=CapabilityReasonCode.MATCH,
        )
    with pytest.raises(ValueError, match="ambiguity cannot select a capability"):
        CapabilityRoute(
            requirements=requirements,
            candidates=(candidate,),
            selected_capability_id=descriptor.capability_id,
            reason=CapabilityReasonCode.AMBIGUOUS,
        )


def test_codecs_and_matcher_fail_closed_for_invalid_payloads_and_policy_mismatches() -> None:
    descriptor = _descriptor("codec", CapabilityClass.PROCEDURAL, procedure_id="procedure.codec")
    requirements = _requirements("codec", CapabilityClass.PROCEDURAL)
    candidate = _candidate(descriptor)
    matcher = ExactCapabilityMatcher()

    descriptor_payload = descriptor.to_dict()
    descriptor_payload["effects"] = ["not-real"]
    with pytest.raises(ValueError, match="valid CapabilityEffect"):
        CapabilityDescriptor.from_dict(descriptor_payload)

    descriptor_payload = descriptor.to_dict()
    descriptor_payload["languages"] = "en"
    with pytest.raises(ValueError, match="bounded sequence"):
        CapabilityDescriptor.from_dict(descriptor_payload)

    descriptor_payload = descriptor.to_dict()
    descriptor_payload["licence"] = ""
    with pytest.raises(ValueError, match="bounded non-empty text"):
        CapabilityDescriptor.from_dict(descriptor_payload)

    descriptor_payload = descriptor.to_dict()
    descriptor_payload["licence"] = "bad\nlicense"
    with pytest.raises(ValueError, match="control characters"):
        CapabilityDescriptor.from_dict(descriptor_payload)

    descriptor_payload = descriptor.to_dict()
    descriptor_payload["locales"] = ["not a locale"]
    with pytest.raises(ValueError, match="language or locale tag"):
        CapabilityDescriptor.from_dict(descriptor_payload)

    request_payload = requirements.to_dict()
    del request_payload["modality"]
    with pytest.raises(ValueError, match="missing required fields"):
        RequestRequirements.from_dict(request_payload)

    candidate_payload = candidate.to_dict()
    candidate_payload["quality_verified"] = "yes"
    with pytest.raises(ValueError, match="must be boolean"):
        CapabilityCandidate.from_dict(candidate_payload)

    route_payload = matcher.match(requirements, (candidate,)).to_dict()
    route_payload["selected_capability_id"] = 3
    with pytest.raises(ValueError, match="must be text"):
        CapabilityRoute.from_dict(route_payload)

    with pytest.raises(ValueError, match="typed request requirements"):
        matcher.match(cast(Any, {}), ())
    with pytest.raises(ValueError, match="typed candidates"):
        matcher.match(requirements, (cast(Any, object()),))

    assert matcher.match(requirements, ()).reason is CapabilityReasonCode.UNSUPPORTED
    assert (
        matcher.match(
            requirements,
            (_candidate(_descriptor("model", CapabilityClass.MODEL, procedure_id=None)),),
        ).reason
        is CapabilityReasonCode.UNSUPPORTED
    )
    assert (
        matcher.match(
            replace(requirements, required_runtime_id="runtime-other"),
            (candidate,),
        ).reason
        is CapabilityReasonCode.UNSUPPORTED
    )
    assert (
        matcher.match(
            replace(requirements, accepted_localities=(CapabilityLocality.REMOTE_ALLOWED,)),
            (candidate,),
        ).reason
        is CapabilityReasonCode.PROHIBITED
    )
    assert (
        matcher.match(
            replace(requirements, accepted_licenses=("LicenseRef-Other",)),
            (candidate,),
        ).reason
        is CapabilityReasonCode.PROHIBITED
    )
    assert (
        matcher.match(
            replace(requirements, max_risk=CapabilityRisk.NONE),
            (candidate,),
        ).reason
        is CapabilityReasonCode.PROHIBITED
    )
    assert (
        matcher.match(
            replace(requirements, allowed_effects=(CapabilityEffect.SIDE_EFFECT,)),
            (candidate,),
        ).reason
        is CapabilityReasonCode.PROHIBITED
    )
