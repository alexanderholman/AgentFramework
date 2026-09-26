from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from af.application.capability_preview import (
    CapabilityObservation,
    CapabilityPreviewService,
    CapabilityRecord,
    CapabilitySnapshot,
    CostInterval,
    CostProjection,
    PreviewOutcome,
    import_capability_catalogue,
)
from af.framework.capabilities import (
    CapabilityClass,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityLifecycle,
    CapabilityLocality,
    CapabilityResourceEnvelope,
    CapabilityRisk,
    RequestRequirements,
)

NOW = datetime(2026, 9, 26, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def record() -> CapabilityRecord:
    descriptor = CapabilityDescriptor(
        capability_id="model.local",
        asset_id="asset.qwen",
        provider_id="local",
        runtime_id="llama.cpp",
        deployment_id="local",
        procedure_id=None,
        workflow_id=None,
        capability_class=CapabilityClass.MODEL,
        modality="text",
        domain="general",
        intent="select",
        input_schema="proposal-input.v1",
        output_schema="proposal.v1",
        languages=("en",),
        locales=("en-GB",),
        max_input_bytes=4096,
        max_output_bytes=256,
        max_context_items=8,
        declared_quality_evidence=(),
        verifier_requirements=("catalogue",),
        effects=(CapabilityEffect.READ_ONLY,),
        risk=CapabilityRisk.LOW,
        locality=CapabilityLocality.LOCAL_ONLY,
        licence="Apache-2.0",
        declared_state=CapabilityLifecycle.ACQUIRED,
        resource_envelope=CapabilityResourceEnvelope(20, 200, 10, 0, 0),
    )
    observation = CapabilityObservation(
        observation_id="bench.1",
        descriptor_digest=descriptor.digest,
        artifact_digest=DIGEST,
        runtime_digest=DIGEST,
        configuration_digest=DIGEST,
        hardware_digest=DIGEST,
        benchmark_digest=DIGEST,
        verifier_id="catalogue",
        outcome="pass",
        observed_at=NOW,
        expires_at=NOW + timedelta(days=1),
        projection=CostProjection.from_envelope(descriptor.resource_envelope),
    )
    return CapabilityRecord(
        descriptor=descriptor,
        artifact_digest=DIGEST,
        runtime_digest=DIGEST,
        configuration_digest=DIGEST,
        hardware_digest=DIGEST,
        installed=True,
        healthy=True,
        observations=(observation,),
    )


def requirements() -> RequestRequirements:
    return RequestRequirements(
        request_id="r1",
        capability_classes=(CapabilityClass.MODEL,),
        modality="text",
        domain="general",
        intent="select",
        input_schema="proposal-input.v1",
        output_schema="proposal.v1",
        language="en",
        locale="en-GB",
        input_bytes=20,
        expected_output_bytes=20,
        context_items=1,
        required_verifiers=("catalogue",),
        allowed_effects=(CapabilityEffect.READ_ONLY,),
        max_risk=CapabilityRisk.LOW,
        accepted_localities=(CapabilityLocality.LOCAL_ONLY,),
        accepted_licenses=("Apache-2.0",),
        accepted_lifecycles=(CapabilityLifecycle.BENCHMARKED,),
        require_authorized=True,
        require_verified_quality=True,
        max_resources=CapabilityResourceEnvelope(100, 500, 100, 0, 10),
        required_runtime_id=None,
    )


def snapshot(*records: CapabilityRecord) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        records=tuple(records),
        observed_at=NOW,
        policy_digest=DIGEST,
        authorized_capability_ids=tuple(r.descriptor.capability_id for r in records),
    )


def test_preview_is_pure_repeatable_and_bound_to_exact_snapshot() -> None:
    service = CapabilityPreviewService(snapshot(record()))
    first = service.preview(requirements())
    assert first == service.preview(requirements())
    assert first.outcome is PreviewOutcome.AVAILABLE
    assert first.completed is False
    assert first.selected_capability_id == "model.local"
    assert first.snapshot_digest == service.snapshot.digest
    assert first.routes[0].projection is not None
    assert first.to_dict()["execution_performed"] is False


def test_costly_is_feasible_but_not_task_completion_or_authorization() -> None:
    req = replace(requirements(), max_resources=CapabilityResourceEnvelope(1, 100, 1, 0, 0))
    result = CapabilityPreviewService(snapshot(record())).preview(req)
    assert result.outcome is PreviewOutcome.AVAILABLE_BUT_COSTLY
    assert result.selected_capability_id == "model.local"
    assert result.completed is False
    assert result.routes[0].reason_codes == ("budget_exceeded",)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"observations": ()}, "missing_evidence"),
        ({"hardware_digest": "sha256:" + "b" * 64}, "incomparable_evidence"),
        ({"healthy": False}, "unavailable_health"),
        ({"installed": False}, "acquisition_required"),
    ],
)
def test_ineligible_evidence_never_establishes_availability(
    change: dict[str, object],
    reason: str,
) -> None:
    result = CapabilityPreviewService(snapshot(replace(record(), **change))).preview(requirements())
    assert result.outcome not in {PreviewOutcome.AVAILABLE, PreviewOutcome.AVAILABLE_BUT_COSTLY}
    assert reason in result.routes[0].reason_codes


@pytest.mark.parametrize("outcome", ["fail", "inconclusive"])
def test_contradictory_or_inconclusive_observation_remains_visible(outcome: str) -> None:
    candidate = record()
    second = replace(candidate.observations[0], observation_id="bench.2", outcome=outcome)
    candidate = replace(candidate, observations=(*candidate.observations, second))
    result = CapabilityPreviewService(snapshot(candidate)).preview(requirements())
    assert result.outcome is PreviewOutcome.UNCERTAIN
    assert len(result.routes[0].evidence) == 2


def test_stale_revoked_and_unknown_cost_are_not_free() -> None:
    candidate = record()
    for observation in (
        replace(candidate.observations[0], expires_at=NOW),
        replace(candidate.observations[0], revoked=True),
        replace(candidate.observations[0], projection=CostProjection()),
    ):
        changed = replace(candidate, observations=(observation,))
        result = CapabilityPreviewService(snapshot(changed)).preview(requirements())
        assert result.outcome is PreviewOutcome.UNCERTAIN


def test_import_preserves_provenance_and_never_authorizes_or_promotes_declarations() -> None:
    raw = {
        "schema_version": "1.0.0",
        "records": [
            {
                "descriptor": record().descriptor.to_dict(),
                "kind": "model",
                "upstream_revision": "exact-commit",
                "quantization": "q4_k_m",
            }
        ],
    }
    imported = import_capability_catalogue(raw, source="lovelace.local", observed_at=NOW)
    assert imported == import_capability_catalogue(raw, source="lovelace.local", observed_at=NOW)
    assert imported.authorized_capability_ids == ()
    assert imported.records[0].observations == ()
    assert imported.records[0].installed is False
    assert imported.records[0].descriptor.declared_state is CapabilityLifecycle.ACQUIRED
    assert "exact-commit" in imported.records[0].source_json
    assert imported.source_digest.startswith("sha256:")
    assert (
        CapabilityPreviewService(imported).preview(requirements()).outcome is PreviewOutcome.DENIED
    )


def test_components_and_restricted_assets_are_not_callable() -> None:
    for changed in (
        replace(record(), kind="tokenizer"),
        replace(record(), restrictions=("medical",)),
    ):
        result = CapabilityPreviewService(snapshot(changed)).preview(requirements())
        assert result.selected_capability_id is None
        assert result.outcome in {PreviewOutcome.UNSUPPORTED, PreviewOutcome.DENIED}


def test_catalogue_rejects_duplicate_identity_and_cost_intervals_reject_nan() -> None:
    with pytest.raises(ValueError, match="unique"):
        snapshot(record(), record())
    with pytest.raises(ValueError):
        CostInterval(float("nan"), 10)


def test_supersession_is_explicit_and_does_not_hide_historical_evidence() -> None:
    candidate = record()
    old = replace(candidate.observations[0], observation_id="bench.old", outcome="fail")
    new = replace(candidate.observations[0], supersedes=(old.digest,))
    result = CapabilityPreviewService(
        snapshot(replace(candidate, observations=(old, new)))
    ).preview(requirements())
    assert result.outcome is PreviewOutcome.AVAILABLE
    assert len(result.routes[0].evidence) == 2


def test_conflicting_cost_measurements_use_conservative_union_and_preserve_unknown() -> None:
    candidate = record()
    first = candidate.observations[0]
    expensive = replace(
        first,
        observation_id="bench.expensive",
        projection=replace(
            first.projection,
            latency_ms=CostInterval(10, 200),
            privacy_exposure="none",
            operational_risk="low",
            assumptions=("cold",),
        ),
    )
    records = replace(candidate, observations=(expensive, first))
    result = CapabilityPreviewService(snapshot(records)).preview(requirements())
    assert result.outcome is PreviewOutcome.AVAILABLE_BUT_COSTLY
    projection = result.routes[0].projection
    assert projection is not None
    assert projection.latency_ms == CostInterval(10, 200)
    assert projection.privacy_exposure == "unknown"
    assert projection.assumptions == ("cold",)
    unknown = replace(first, observation_id="bench.unknown", projection=CostProjection())
    result = CapabilityPreviewService(
        snapshot(replace(candidate, observations=(first, unknown)))
    ).preview(requirements())
    assert result.outcome is PreviewOutcome.UNCERTAIN


def test_empty_partial_revoked_missing_verifier_and_ambiguous_routes_are_distinct() -> None:
    assert (
        CapabilityPreviewService(snapshot()).preview(requirements()).outcome
        is PreviewOutcome.UNSUPPORTED
    )
    candidate = record()
    partial = replace(requirements(), locale="en-US")
    assert (
        CapabilityPreviewService(snapshot(candidate)).preview(partial).outcome
        is PreviewOutcome.UNSUPPORTED
    )
    revoked = replace(
        candidate,
        descriptor=replace(candidate.descriptor, declared_state=CapabilityLifecycle.REVOKED),
    )
    assert (
        CapabilityPreviewService(snapshot(revoked)).preview(requirements()).outcome
        is PreviewOutcome.DENIED
    )
    no_verifier = replace(
        candidate, observations=(replace(candidate.observations[0], verifier_id="other"),)
    )
    result = CapabilityPreviewService(snapshot(no_verifier)).preview(requirements())
    assert result.routes[0].reason_codes == ("missing_verifier_evidence",)
    other_descriptor = replace(candidate.descriptor, capability_id="model.other")
    other = replace(
        candidate,
        descriptor=other_descriptor,
        observations=(
            replace(candidate.observations[0], descriptor_digest=other_descriptor.digest),
        ),
    )
    result = CapabilityPreviewService(snapshot(candidate, other)).preview(requirements())
    assert result.outcome is PreviewOutcome.AMBIGUOUS
    assert result.selected_capability_id is None


def test_dependency_readiness_authorization_and_evidence_are_required() -> None:
    dependency = record()
    dependent_descriptor = replace(
        dependency.descriptor,
        capability_id="model.composite",
        dependency_ids=(dependency.descriptor.capability_id,),
    )
    dependent = replace(
        dependency,
        descriptor=dependent_descriptor,
        observations=(
            replace(dependency.observations[0], descriptor_digest=dependent_descriptor.digest),
        ),
    )
    for change, reason in [
        ({"healthy": False}, "dependency_unavailable"),
        ({"restrictions": ("gated",)}, "dependency_policy_denied"),
        ({"observations": ()}, "dependency_insufficient_evidence"),
    ]:
        result = CapabilityPreviewService(
            snapshot(dependent, replace(dependency, **change))
        ).preview(requirements())
        route = next(item for item in result.routes if item.capability_id == "model.composite")
        assert route.reason_codes == (reason,)
    result = CapabilityPreviewService(snapshot(dependent, dependency)).preview(requirements())
    assert all(route.outcome is PreviewOutcome.AVAILABLE for route in result.routes)


def test_import_diagnostics_and_schema_fail_closed_without_effects() -> None:
    partial = {
        "schema_version": "1.0.0",
        "records": [
            None,
            {},
            {
                "descriptor": record().descriptor.to_dict(),
                "restrictions": "not-a-list",
            },
        ],
    }
    imported = import_capability_catalogue(partial, source="test", observed_at=NOW)
    assert imported.records == ()
    assert imported.diagnostics == ("record.0.invalid", "record.1.invalid", "record.2.invalid")
    with pytest.raises(ValueError):
        import_capability_catalogue(
            {"schema_version": "0", "records": []}, source="test", observed_at=NOW
        )
    with pytest.raises(ValueError):
        import_capability_catalogue(
            {"schema_version": "1.0.0", "records": [None] * 10001}, source="test", observed_at=NOW
        )
    with pytest.raises(ValueError):
        import_capability_catalogue(
            {"schema_version": "1.0.0", "records": [], "huge": "x" * (17 * 1_048_576)},
            source="test",
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_digest": "not-pinned"},
        {"outcome": "maybe"},
        {"verifier_id": "Invalid ID"},
        {"observed_at": datetime(2026, 1, 1)},
        {"expires_at": NOW - timedelta(seconds=1)},
        {"projection": None},
        {"supersedes": []},
    ],
)
def test_benchmark_evidence_rejects_invalid_or_mutable_contracts(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(record().observations[0], **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"descriptor": None},
        {"kind": "executable"},
        {"installed": "yes"},
        {"observations": []},
        {"restrictions": []},
        {"source_json": " " * (1_048_577)},
    ],
)
def test_catalogue_records_require_immutable_typed_local_state(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(record(), **changes)


def test_snapshot_validation_and_projection_bounds_are_explicit() -> None:
    candidate = record()
    with pytest.raises(ValueError):
        replace(candidate, observations=(*candidate.observations, *candidate.observations))
    for change in (
        {"schema_version": "2"},
        {"records": []},
        {"authorized_capability_ids": []},
        {"authorized_capability_ids": ("unknown",)},
    ):
        with pytest.raises(ValueError):
            replace(snapshot(candidate), **change)
    with pytest.raises(ValueError):
        CostProjection(latency_ms=10)
    with pytest.raises(ValueError):
        CostProjection(assumptions=[])
    oversized = replace(
        candidate.observations[0].projection, latency_ms=CostInterval(0, 2_000_000_000)
    )
    assert oversized.upper_envelope() is None
