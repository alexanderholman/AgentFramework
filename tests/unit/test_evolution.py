from __future__ import annotations

from datetime import UTC, datetime, timedelta

from af.framework.evolution import (
    EvolutionCandidate,
    EvolutionObservation,
    EvolutionPlan,
    EvolutionSnapshot,
    EvolutionStop,
    plan_evolution,
)

NOW = datetime(2026, 9, 26, tzinfo=UTC)
D = "sha256:" + "a" * 64


def snapshot():
    observations = tuple(
        EvolutionObservation(
            admitted=True,
            period_start=NOW - timedelta(days=5 - index),
            period_end=NOW - timedelta(days=4 - index),
            baseline=10,
            observed=15 + index,
            uncertainty=0.5,
            samples=100,
            held_out=index == 3,
            evidence_digest=D,
        )
        for index in range(4)
    )
    return EvolutionSnapshot(
        objective_id="reduce-local-routing-cost",
        metric="latency-ms",
        direction="minimize",
        captured_at=NOW,
        expires_at=NOW + timedelta(days=1),
        policy_digest=D,
        observations=observations,
        candidate=EvolutionCandidate(
            change="Add a bounded index for installed procedures",
            artifact_digest=D,
            predicted_gain=(2.0, 4.0),
            rollback="Remove index and restore snapshot",
            alternatives=("Retain baseline scanner",),
            guardrails=("No accuracy regression",),
            counterfactual_digest=D,
            goodhart_checked=True,
        ),
    )


def test_evolution_is_byte_deterministic_and_records_uncertainty_and_holdout():
    value = snapshot()
    result = plan_evolution(value)
    assert isinstance(result, EvolutionPlan)
    assert result.canonical_json() == plan_evolution(value).canonical_json()
    assert result.observed_gap == (4.5, 8.5)
    assert result.validation == "held-out time-separated baseline comparison"
    assert result.rollback
    assert result.digest.startswith("sha256:")


def test_confounded_sensitive_and_weak_evidence_stop_without_proposal():
    value = snapshot()
    for update, reason in [
        ({"sensitive": True}, "sensitive_evidence"),
        ({"confounded": True}, "confounded_evidence"),
        ({"samples": 1}, "weak_evidence"),
    ]:
        observations = (value.observations[0].model_copy(update=update), *value.observations[1:])
        result = plan_evolution(value.model_copy(update={"observations": observations}))
        assert isinstance(result, EvolutionStop)
        assert result.reason == reason


def test_heldout_and_anti_goodhart_are_mandatory():
    value = snapshot()
    result = plan_evolution(value.model_copy(update={"observations": value.observations[:3]}))
    assert result.reason == "insufficient_longitudinal_evidence"
    result = plan_evolution(
        value.model_copy(
            update={"candidate": value.candidate.model_copy(update={"goodhart_checked": False})}
        )
    )
    assert result.reason == "goodhart_review_missing"


def test_admission_requires_exact_independent_current_digest():
    import pytest

    from af.framework.evolution import EvolutionAdmission, admitted_evolution_objective

    plan = plan_evolution(snapshot())
    admission = EvolutionAdmission(
        proposal_digest=plan.digest,
        reviewer="independent-review",
        independent=True,
        admitted=True,
        expires_at=NOW + timedelta(hours=1),
    )
    assert admitted_evolution_objective(plan, admission, now=NOW) == plan.digest
    for changed in (
        admission.model_copy(update={"independent": False}),
        admission.model_copy(update={"proposal_digest": D}),
        admission.model_copy(update={"expires_at": NOW}),
    ):
        with pytest.raises(ValueError, match="independent exact-digest"):
            admitted_evolution_objective(plan, changed, now=NOW)


def test_permuted_evidence_has_canonical_identical_proposal():
    value = snapshot()
    reordered = value.model_copy(update={"observations": tuple(reversed(value.observations))})
    assert plan_evolution(value).canonical_json() == plan_evolution(reordered).canonical_json()


def test_evolution_stops_on_unadmitted_nonpersistent_or_ineligible_candidate():
    value = snapshot()
    changes = [
        (
            {
                "observations": (
                    value.observations[0].model_copy(update={"admitted": False}),
                    *value.observations[1:],
                )
            },
            "unadmitted_evidence",
        ),
        (
            {
                "observations": (
                    value.observations[0].model_copy(update={"observed": 10}),
                    *value.observations[1:],
                )
            },
            "no_persistent_material_gap",
        ),
        ({"candidate": None}, "no_bounded_candidate"),
        (
            {"candidate": value.candidate.model_copy(update={"reversible": False})},
            "unsafe_objective_or_irreversible_change",
        ),
        (
            {"candidate": value.candidate.model_copy(update={"counterfactual_digest": None})},
            "causal_alternatives_unchecked",
        ),
        (
            {"candidate": value.candidate.model_copy(update={"estimated_cost": 101})},
            "cost_budget_exhausted",
        ),
        (
            {
                "observations": (
                    *value.observations[:3],
                    value.observations[3].model_copy(
                        update={"period_start": value.observations[0].period_start}
                    ),
                )
            },
            "time_partition_invalid",
        ),
    ]
    for update, reason in changes:
        result = plan_evolution(value.model_copy(update=update))
        assert isinstance(result, EvolutionStop)
        assert result.reason == reason


def test_evolution_schema_rejects_invalid_time_and_effect_intervals():
    import pytest

    from af.framework.evolution import EvolutionAdmission

    value = snapshot()
    observation = value.observations[0]
    for update in (
        {"period_start": observation.period_start.replace(tzinfo=None)},
        {"period_end": observation.period_start},
    ):
        with pytest.raises(ValueError):
            EvolutionObservation.model_validate({**observation.model_dump(), **update})
    with pytest.raises(ValueError, match="gain"):
        EvolutionCandidate.model_validate(
            {**value.candidate.model_dump(), "predicted_gain": (4, 2)}
        )
    for update in ({"captured_at": NOW.replace(tzinfo=None)}, {"expires_at": NOW}):
        with pytest.raises(ValueError):
            EvolutionSnapshot.model_validate({**value.model_dump(), **update})
    with pytest.raises(ValueError, match="timezone"):
        EvolutionAdmission(
            proposal_digest=D,
            reviewer="reviewer",
            independent=True,
            admitted=True,
            expires_at=NOW.replace(tzinfo=None),
        )
