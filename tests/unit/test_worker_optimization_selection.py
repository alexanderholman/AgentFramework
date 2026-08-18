from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from af.application.worker_generation import WorkerRuntimeOptimization
from af.application.worker_qualification import (
    WorkerOptimizationCandidate,
    WorkerOptimizationPriorities,
    WorkerOptimizationQualification,
    WorkerOptimizationSelector,
)
from af.framework.errors import InvalidProviderConfiguration
from af.framework.managers import CapabilityBinding

NOW = datetime(2026, 8, 18, 10, 30, tzinfo=UTC)


def _qualification(
    evidence_char: str,
    workers: int,
    **overrides: object,
) -> WorkerOptimizationQualification:
    values: dict[str, object] = {
        "driver_name": "database",
        "binding": CapabilityBinding(
            driver_version="1.5.0",
            artifact_digest="a" * 64,
            dependency_graph_digest="b" * 64,
            configuration_digest="c" * 64,
            deployment_profile="production",
            policy_version="worker-policy-v1",
        ),
        "optimization": WorkerRuntimeOptimization(
            worker_count=workers,
            max_concurrency=8,
            poll_interval=timedelta(milliseconds=25),
            heartbeat_interval=timedelta(seconds=5),
            max_idle_time=timedelta(minutes=2),
            queue_wait_alert_after=timedelta(seconds=3),
            renewal_min_interval=timedelta(milliseconds=100),
            renewal_max_interval=timedelta(seconds=10),
        ),
        "evidence_digest": evidence_char * 64,
        "environment_digest": "e" * 64,
        "observed_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(days=7),
        "sample_count": 10_000,
        "real_service": True,
        "correctness_failures": 0,
        "settlement_failures": 0,
        "reservation_losses": 0,
    }
    values.update(overrides)
    return WorkerOptimizationQualification(**values)  # type: ignore[arg-type]


def _candidates() -> tuple[WorkerOptimizationCandidate, ...]:
    return (
        WorkerOptimizationCandidate(
            _qualification("1", 1),
            performance_index=900_000,
            safety_index=500_000,
            cost_efficiency_index=400_000,
        ),
        WorkerOptimizationCandidate(
            _qualification("2", 2),
            performance_index=600_000,
            safety_index=950_000,
            cost_efficiency_index=300_000,
        ),
        WorkerOptimizationCandidate(
            _qualification("3", 3),
            performance_index=500_000,
            safety_index=700_000,
            cost_efficiency_index=950_000,
        ),
    )


@pytest.mark.parametrize(
    ("priorities", "expected_workers"),
    [
        (WorkerOptimizationPriorities(performance=100, safety=0, cost=0), 1),
        (WorkerOptimizationPriorities(performance=0, safety=100, cost=0), 2),
        (WorkerOptimizationPriorities(performance=0, safety=0, cost=100), 3),
    ],
)
def test_selector_applies_explicit_operator_priorities(
    priorities: WorkerOptimizationPriorities,
    expected_workers: int,
) -> None:
    selection = WorkerOptimizationSelector().select(
        _candidates(),
        priorities=priorities,
        now=NOW,
    )

    assert selection.qualification.optimization.worker_count == expected_workers
    assert selection.candidate_count == 3
    assert len(selection.selection_digest) == 64


def test_selector_is_order_independent_and_breaks_equal_score_ties_safety_first() -> None:
    performance = WorkerOptimizationCandidate(
        _qualification("4", 4),
        performance_index=1_000_000,
        safety_index=0,
        cost_efficiency_index=0,
    )
    safety = WorkerOptimizationCandidate(
        _qualification("5", 5),
        performance_index=0,
        safety_index=1_000_000,
        cost_efficiency_index=0,
    )
    priorities = WorkerOptimizationPriorities(performance=50, safety=50, cost=0)

    first = WorkerOptimizationSelector().select(
        (performance, safety), priorities=priorities, now=NOW
    )
    second = WorkerOptimizationSelector().select(
        (safety, performance), priorities=priorities, now=NOW
    )

    assert first.qualification.evidence_digest == "5" * 64
    assert second.qualification == first.qualification
    assert second.selection_digest == first.selection_digest


def test_selection_digest_commits_to_exact_runtime_values() -> None:
    candidate = _candidates()[0]
    priorities = WorkerOptimizationPriorities(performance=34, safety=33, cost=33)
    changed_optimization = replace(candidate.qualification.optimization, worker_count=4)
    changed = replace(
        candidate,
        qualification=replace(
            candidate.qualification,
            optimization=changed_optimization,
        ),
    )

    first = WorkerOptimizationSelector().select((candidate,), priorities=priorities, now=NOW)
    second = WorkerOptimizationSelector().select((changed,), priorities=priorities, now=NOW)

    assert first.selection_digest != second.selection_digest


@pytest.mark.parametrize(
    "qualification",
    [
        _qualification("6", 1, real_service=False),
        _qualification("7", 1, correctness_failures=1),
        _qualification("8", 1, settlement_failures=1),
        _qualification("9", 1, reservation_losses=1),
        _qualification("a", 1, observed_at=NOW + timedelta(seconds=1)),
        _qualification("b", 1, expires_at=NOW),
    ],
)
def test_selector_rejects_candidates_that_are_not_current_safe_real_service_evidence(
    qualification: WorkerOptimizationQualification,
) -> None:
    candidate = WorkerOptimizationCandidate(
        qualification,
        performance_index=1,
        safety_index=1,
        cost_efficiency_index=1,
    )

    with pytest.raises(InvalidProviderConfiguration):
        WorkerOptimizationSelector().select(
            (candidate,),
            priorities=WorkerOptimizationPriorities(performance=34, safety=33, cost=33),
            now=NOW,
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"environment_digest": "f" * 64},
        {
            "binding": CapabilityBinding(
                driver_version="1.5.0",
                artifact_digest="a" * 64,
                dependency_graph_digest="b" * 64,
                configuration_digest="d" * 64,
                deployment_profile="production",
                policy_version="worker-policy-v1",
            )
        },
        {"driver_name": "redis"},
    ],
)
def test_selector_rejects_comparison_across_different_targets(
    changed: dict[str, object],
) -> None:
    first = _candidates()[0]
    second = replace(_candidates()[1], qualification=replace(first.qualification, **changed))

    with pytest.raises(InvalidProviderConfiguration, match="same target"):
        WorkerOptimizationSelector().select(
            (first, second),
            priorities=WorkerOptimizationPriorities(performance=34, safety=33, cost=33),
            now=NOW,
        )


@pytest.mark.parametrize(
    "values",
    [
        {"performance": 0, "safety": 0, "cost": 0},
        {"performance": 34, "safety": 34, "cost": 34},
        {"performance": True, "safety": 0, "cost": 99},
        {"performance": -1, "safety": 1, "cost": 100},
    ],
)
def test_priorities_require_integer_percentages_totalling_one_hundred(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        WorkerOptimizationPriorities(**values)  # type: ignore[arg-type]


def test_candidate_indices_are_bounded_integers() -> None:
    with pytest.raises(ValueError):
        WorkerOptimizationCandidate(
            _qualification("c", 1),
            performance_index=True,
            safety_index=0,
            cost_efficiency_index=0,
        )

    with pytest.raises(ValueError):
        WorkerOptimizationCandidate(
            _qualification("d", 1),
            performance_index=1_000_001,
            safety_index=0,
            cost_efficiency_index=0,
        )
