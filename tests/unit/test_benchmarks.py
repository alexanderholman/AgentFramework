from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from af.application.bus import GateDecision, GateEffect
from af.framework.context import AcceptanceScope
from af.testing import (
    BenchmarkBinding,
    BenchmarkCompatibility,
    BenchmarkExecutionCancelled,
    BenchmarkExecutionFailure,
    BenchmarkExport,
    BenchmarkFixtureAuthorization,
    BenchmarkFixtureManifest,
    BenchmarkInconclusiveResult,
    BenchmarkMeasurement,
    BenchmarkOutcome,
    BenchmarkPluginDescriptor,
    BenchmarkPluginRegistry,
    BenchmarkRunner,
    BenchmarkRunRequest,
    BenchmarkRunResult,
    BenchmarkVerification,
    BenchmarkWorkspace,
)


def _sha(char: str) -> str:
    return f"sha256:{char * 64}"


def _manifest(**overrides: object) -> BenchmarkFixtureManifest:
    values: dict[str, object] = {
        "fixture_id": "asr-fixture",
        "content_digest": _sha("1"),
        "source_id": "lovelace-catalogue",
        "source_revision": "2026-07-22",
        "provenance": (_sha("2"),),
        "license_basis": "research-license-v1",
        "copyright_basis": "owner-cleared",
        "collection_method": "documented-ingest",
        "consent_basis": "participant-consent-v1",
        "legal_basis": "research-consent",
        "transformations": ("normalized-audio",),
        "splits": ("eval",),
        "leakage_controls": ("speaker-disjoint",),
        "synthetic": False,
        "maintainers": ("benchmark-team",),
        "access_tier": "restricted",
        "intended_uses": ("benchmarking",),
        "intended_audiences": ("reviewers",),
        "retention_policy": "retain-30-days",
        "deletion_policy": "delete-on-expiry",
        "expires_at": datetime(2026, 12, 31, tzinfo=UTC),
        "revocation_policy": "owner-may-revoke",
        "required_approval_class": "human-review",
        "reviewer_authority": "benchmark-governor",
        "prohibited_uses": ("training",),
        "export_policy": "aggregate-only",
        "governance_categories": ("accessibility",),
    }
    values.update(overrides)
    return BenchmarkFixtureManifest(**values)


def _authorization(**overrides: object) -> BenchmarkFixtureAuthorization:
    values: dict[str, object] = {
        "approval_class": "human-review",
        "reviewer_authority": "benchmark-governor",
        "decision": GateDecision(
            effect=GateEffect.ALLOW,
            policy_version="af-benchmark-v1",
            reason_code="fixture-approved",
            effective_scope=AcceptanceScope.DETERMINISTIC,
        ),
        "issued_at": datetime(2026, 7, 22, 21, 0, tzinfo=UTC),
        "expires_at": datetime(2026, 12, 31, 0, 0, tzinfo=UTC),
        "revoked_at": None,
        "permitted_uses": ("benchmarking",),
        "permitted_audiences": ("reviewers",),
    }
    values.update(overrides)
    return BenchmarkFixtureAuthorization(**values)


def _workspace(**overrides: object) -> BenchmarkWorkspace:
    values: dict[str, object] = {
        "workspace_id": "benchmark-workspace",
        "access_tier": "restricted",
        "encrypted": True,
        "declared_network_egress": (),
        "retention_policy": "retain-30-days",
        "deletion_policy": "delete-on-expiry",
    }
    values.update(overrides)
    return BenchmarkWorkspace(**values)


def _verification(
    *,
    score: float | None = 0.91,
    metrics: tuple[tuple[str, object], ...] = (("wer", 0.09),),
    reason_code: str = "verified",
) -> BenchmarkVerification:
    return BenchmarkVerification(score=score, metrics=metrics, reason_code=reason_code)


def _request(**overrides: object) -> BenchmarkRunRequest:
    values: dict[str, object] = {
        "benchmark_id": "asr-benchmark",
        "plugin_id": "accessibility-ocr",
        "procedure_id": "ocr-procedure",
        "fixture": _manifest(),
        "authorization": _authorization(),
        "workspace": _workspace(),
        "runtime_name": "python",
        "runtime_version": "3.12.4",
        "adapter_id": "benchmark-adapter",
        "adapter_version": "1.0.0",
        "verifier_id": "score-verifier",
        "verifier_version": "1.0.0",
        "environment_digest": _sha("3"),
        "hardware_class": "cpu",
        "precision": "fp32",
        "configuration_digest": _sha("4"),
        "threshold_digest": _sha("5"),
        "required_scope": AcceptanceScope.DETERMINISTIC,
        "verifier": lambda _request, _measurement: _verification(),
        "observed_at": datetime(2026, 7, 22, 22, 0, tzinfo=UTC),
        "supersedes": (),
    }
    values.update(overrides)
    return BenchmarkRunRequest(**values)


class _Plugin:
    def __init__(
        self,
        *,
        events: list[str],
        compatibility: BenchmarkCompatibility | None = None,
        session: _Session | None = None,
        descriptor: BenchmarkPluginDescriptor | None = None,
    ) -> None:
        self._events = events
        self._compatibility = compatibility or BenchmarkCompatibility.supported()
        self._session = session or _Session(events)
        self._descriptor = descriptor or BenchmarkPluginDescriptor(
            plugin_id="accessibility-ocr",
            plugin_version="1.2.3",
            artifact_digest=_sha("6"),
        )

    def descriptor(self) -> BenchmarkPluginDescriptor:
        return self._descriptor

    def compatibility(self, _request: BenchmarkRunRequest) -> BenchmarkCompatibility:
        self._events.append("compatibility")
        return self._compatibility

    def open(self, _request: BenchmarkRunRequest) -> _Session:
        self._events.append("open")
        return self._session


class _Session:
    def __init__(
        self,
        events: list[str],
        *,
        setup_error: Exception | None = None,
        execute_error: Exception | None = None,
        execute_result: BenchmarkMeasurement | None = None,
        teardown_error: Exception | None = None,
    ) -> None:
        self._events = events
        self._setup_error = setup_error
        self._execute_error = execute_error
        self._execute_result = execute_result or BenchmarkMeasurement((("frames", 1),))
        self._teardown_error = teardown_error

    def setup(self) -> None:
        self._events.append("setup")
        if self._setup_error is not None:
            raise self._setup_error

    def execute(self) -> BenchmarkMeasurement:
        self._events.append("execute")
        if self._execute_error is not None:
            raise self._execute_error
        return self._execute_result

    def teardown(self) -> None:
        self._events.append("teardown")
        if self._teardown_error is not None:
            raise self._teardown_error


def _clock(*moments: datetime):
    values = list(moments)
    assert values

    def read() -> datetime:
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    return read


def test_missing_or_incompatible_plugins_return_unsupported_before_setup() -> None:
    request = _request()

    missing = BenchmarkRunner(())
    missing_result = missing.run(request)
    assert missing_result.outcome is BenchmarkOutcome.UNSUPPORTED
    assert missing_result.reason_code == "plugin-not-installed"
    assert missing_result.attempted is False
    assert missing_result.score is None
    assert missing_result.binding.plugin_version is None
    assert missing_result.binding.artifact_digest is None

    events: list[str] = []
    incompatible = BenchmarkRunner(
        (
            _Plugin(
                events=events,
                compatibility=BenchmarkCompatibility.unsupported("runtime-incompatible"),
                session=_Session(events),
            ),
        )
    )
    incompatible_result = incompatible.run(request)
    assert incompatible_result.outcome is BenchmarkOutcome.UNSUPPORTED
    assert incompatible_result.reason_code == "runtime-incompatible"
    assert incompatible_result.attempted is False
    assert events == ["compatibility"]


def test_attempted_execution_failure_records_negative_evidence_and_float_wav_regression() -> None:
    events: list[str] = []
    runner = BenchmarkRunner(
        (
            _Plugin(
                events=events,
                session=_Session(
                    events,
                    execute_error=BenchmarkExecutionFailure("float-wav-reader-incompatible"),
                ),
            ),
        )
    )

    result = runner.run(_request())

    assert result.outcome is BenchmarkOutcome.FAILED
    assert result.reason_code == "float-wav-reader-incompatible"
    assert result.attempted is True
    assert result.score is None
    assert result.evidence_digest.startswith("sha256:")
    assert events == ["compatibility", "open", "setup", "execute", "teardown"]


def test_missing_governance_or_narrowed_scope_denies_before_setup() -> None:
    events: list[str] = []
    runner = BenchmarkRunner((_Plugin(events=events),))

    ineligible_result = runner.run(
        _request(
            fixture=_manifest(license_basis=None),
        )
    )
    assert ineligible_result.outcome is BenchmarkOutcome.SKIPPED_BY_POLICY
    assert ineligible_result.reason_code == "missing-license-basis"
    assert ineligible_result.attempted is False
    assert events == []

    narrowed_result = runner.run(
        _request(
            authorization=_authorization(
                decision=GateDecision(
                    effect=GateEffect.ALLOW,
                    policy_version="af-benchmark-v1",
                    reason_code="scope-narrowed",
                    effective_scope=AcceptanceScope.CHILD,
                )
            )
        )
    )
    assert narrowed_result.outcome is BenchmarkOutcome.SKIPPED_BY_POLICY
    assert narrowed_result.reason_code == "effective-scope-narrowed"
    assert narrowed_result.attempted is False

    mismatched_use = runner.run(
        _request(
            authorization=_authorization(permitted_uses=("training",)),
        )
    )
    assert mismatched_use.outcome is BenchmarkOutcome.SKIPPED_BY_POLICY
    assert mismatched_use.reason_code == "fixture-use-prohibited"
    assert mismatched_use.attempted is False

    unapproved_use = runner.run(
        _request(
            authorization=_authorization(permitted_uses=("reporting",)),
        )
    )
    assert unapproved_use.outcome is BenchmarkOutcome.SKIPPED_BY_POLICY
    assert unapproved_use.reason_code == "fixture-use-not-permitted"
    assert unapproved_use.attempted is False

    mismatched_audience = runner.run(
        _request(
            authorization=_authorization(permitted_audiences=("operators",)),
        )
    )
    assert mismatched_audience.outcome is BenchmarkOutcome.SKIPPED_BY_POLICY
    assert mismatched_audience.reason_code == "fixture-audience-not-permitted"
    assert mismatched_audience.attempted is False

    mismatched_approval = runner.run(
        _request(
            authorization=_authorization(approval_class="expedited-review"),
        )
    )
    assert mismatched_approval.outcome is BenchmarkOutcome.SKIPPED_BY_POLICY
    assert mismatched_approval.reason_code == "fixture-approval-class-mismatch"
    assert mismatched_approval.attempted is False

    mismatched_reviewer = runner.run(
        _request(
            authorization=_authorization(reviewer_authority="other-reviewer"),
        )
    )
    assert mismatched_reviewer.outcome is BenchmarkOutcome.SKIPPED_BY_POLICY
    assert mismatched_reviewer.reason_code == "fixture-reviewer-authority-mismatch"
    assert mismatched_reviewer.attempted is False


def test_authorization_is_checked_again_before_evidence_acceptance() -> None:
    events: list[str] = []
    observed_at = datetime(2026, 7, 22, 22, 0, tzinfo=UTC)
    runner = BenchmarkRunner(
        (_Plugin(events=events),),
        clock=_clock(
            observed_at,
            observed_at + timedelta(seconds=2),
            observed_at + timedelta(seconds=2),
        ),
    )

    result = runner.run(
        _request(
            authorization=_authorization(expires_at=observed_at + timedelta(seconds=1)),
            observed_at=observed_at,
        )
    )

    assert result.outcome is BenchmarkOutcome.SKIPPED_BY_POLICY
    assert result.reason_code == "authorization-expired"
    assert result.attempted is True
    assert events == ["compatibility", "open", "setup", "execute", "teardown"]


@pytest.mark.parametrize(
    ("execute_error", "verifier", "expected_outcome", "expected_reason"),
    [
        (
            BenchmarkExecutionCancelled("operator-cancelled"),
            lambda _request, _measurement: _verification(),
            BenchmarkOutcome.CANCELLED,
            "operator-cancelled",
        ),
        (
            TimeoutError(),
            lambda _request, _measurement: _verification(),
            BenchmarkOutcome.CANCELLED,
            "deadline-exceeded",
        ),
        (
            RuntimeError("crash"),
            lambda _request, _measurement: _verification(),
            BenchmarkOutcome.FAILED,
            "plugin-exception",
        ),
        (
            None,
            lambda _request, _measurement: (_ for _ in ()).throw(
                BenchmarkExecutionFailure("verifier-failed")
            ),
            BenchmarkOutcome.FAILED,
            "verifier-failed",
        ),
        (
            None,
            lambda _request, _measurement: (_ for _ in ()).throw(
                BenchmarkInconclusiveResult("needs-human-review")
            ),
            BenchmarkOutcome.INCONCLUSIVE,
            "needs-human-review",
        ),
    ],
)
def test_runner_tears_down_after_terminal_paths(
    execute_error: Exception | None,
    verifier,
    expected_outcome: BenchmarkOutcome,
    expected_reason: str,
) -> None:
    events: list[str] = []
    runner = BenchmarkRunner(
        (
            _Plugin(
                events=events,
                session=_Session(events, execute_error=execute_error),
            ),
        )
    )

    result = runner.run(_request(verifier=verifier))

    assert result.outcome is expected_outcome
    assert result.reason_code == expected_reason
    assert events[-1] == "teardown"


def test_evidence_identity_changes_when_binding_inputs_change() -> None:
    binding = BenchmarkBinding(
        plugin_id="accessibility-ocr",
        plugin_version="1.2.3",
        procedure_id="ocr-procedure",
        artifact_digest=_sha("6"),
        runtime_name="python",
        runtime_version="3.12.4",
        adapter_id="benchmark-adapter",
        adapter_version="1.0.0",
        verifier_id="score-verifier",
        verifier_version="1.0.0",
        fixture_id="asr-fixture",
        fixture_manifest_digest=_manifest().digest,
        fixture_content_digest=_manifest().content_digest,
        environment_digest=_sha("3"),
        hardware_class="cpu",
        precision="fp32",
        configuration_digest=_sha("4"),
        threshold_digest=_sha("5"),
        effective_scope=AcceptanceScope.DETERMINISTIC,
        authorization_digest=_authorization().digest,
    )

    fixture_with_new_split = _manifest(splits=("holdout",))
    variants = (
        replace(binding, plugin_version="1.2.4"),
        replace(binding, verifier_id="alternate-verifier"),
        replace(binding, runtime_version="3.12.5"),
        replace(binding, hardware_class="gpu"),
        replace(binding, threshold_digest=_sha("7")),
        replace(binding, authorization_digest=_sha("8")),
        replace(binding, fixture_manifest_digest=fixture_with_new_split.digest),
    )

    assert all(variant.digest != binding.digest for variant in variants)


def test_export_is_minimized_and_declares_permitted_audiences_and_uses() -> None:
    result = BenchmarkRunner((_Plugin(events=[]),)).run(_request())

    export = result.export()
    payload = export.payload()

    assert result.outcome is BenchmarkOutcome.COMPLETED
    assert result.certifies_production is False
    assert export.certifies_domain_governance is False
    assert export.certifies_physical_isolation is False
    assert payload["permitted_audiences"] == ["reviewers"]
    assert payload["permitted_uses"] == ["benchmarking"]
    assert payload["fixture"] == {
        "content_digest": _manifest().content_digest,
        "fixture_id": "asr-fixture",
        "manifest_digest": _manifest().digest,
    }
    assert "collection_method" not in payload
    assert "consent_basis" not in payload
    assert "source_revision" not in payload


def test_registry_and_declarations_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="bounded tuple"):
        BenchmarkMeasurement("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="name/value pairs"):
        BenchmarkMeasurement((("ok", 1), "bad"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be finite"):
        BenchmarkVerification(score=float("inf"))
    with pytest.raises(ValueError, match="synthetic flag"):
        _manifest(synthetic=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="revocation cannot follow expiry"):
        _manifest(
            expires_at=datetime(2026, 7, 22, 22, 0, tzinfo=UTC),
            revoked_at=datetime(2026, 7, 22, 22, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="expiry must follow issue time"):
        _authorization(
            issued_at=datetime(2026, 7, 22, 22, 0, tzinfo=UTC),
            expires_at=datetime(2026, 7, 22, 22, 0, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="encryption must be boolean"):
        _workspace(encrypted=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="effective scope"):
        replace(
            BenchmarkBinding(
                plugin_id="accessibility-ocr",
                plugin_version="1.2.3",
                procedure_id="ocr-procedure",
                artifact_digest=_sha("6"),
                runtime_name="python",
                runtime_version="3.12.4",
                adapter_id="benchmark-adapter",
                adapter_version="1.0.0",
                verifier_id="score-verifier",
                verifier_version="1.0.0",
                fixture_id="asr-fixture",
                fixture_manifest_digest=_manifest().digest,
                fixture_content_digest=_manifest().content_digest,
                environment_digest=_sha("3"),
                hardware_class="cpu",
                precision="fp32",
                configuration_digest=_sha("4"),
                threshold_digest=_sha("5"),
                effective_scope=AcceptanceScope.DETERMINISTIC,
                authorization_digest=_authorization().digest,
            ),
            effective_scope="bad",  # type: ignore[arg-type]
        )

    class _NoDescriptor:
        pass

    class _BadDescriptor:
        def descriptor(self) -> str:
            return "bad"

    with pytest.raises(TypeError, match=r"descriptor\(\)"):
        BenchmarkPluginRegistry((_NoDescriptor(),))
    with pytest.raises(TypeError, match="descriptors are invalid"):
        BenchmarkPluginRegistry((_BadDescriptor(),))
    with pytest.raises(ValueError, match="unique"):
        BenchmarkPluginRegistry((_Plugin(events=[]), _Plugin(events=[])))


def test_runner_fail_closed_edge_cases_and_result_validation() -> None:
    denied = _authorization(
        decision=GateDecision(
            effect=GateEffect.DENY,
            policy_version="af-benchmark-v1",
            reason_code="policy-denied",
        )
    )
    denied_result = BenchmarkRunner((_Plugin(events=[]),)).run(_request(authorization=denied))
    assert denied_result.reason_code == "policy-denied"
    assert denied_result.binding.effective_scope is None
    assert denied_result.binding.payload()["effective_scope"] is None

    revoked_result = BenchmarkRunner((_Plugin(events=[]),)).run(
        _request(
            authorization=_authorization(revoked_at=datetime(2026, 7, 22, 21, 30, tzinfo=UTC)),
            observed_at=datetime(2026, 7, 22, 22, 0, tzinfo=UTC),
        )
    )
    assert revoked_result.reason_code == "authorization-revoked"

    assert _workspace(encrypted=False).policy_reason(_manifest()) == "workspace-not-encrypted"
    assert (
        _workspace(declared_network_egress=("declared-egress",)).policy_reason(_manifest())
        == "workspace-network-egress-declared"
    )
    assert (
        _workspace(access_tier="internal").policy_reason(_manifest())
        == "workspace-access-tier-mismatch"
    )
    assert (
        _workspace(retention_policy="retain-forever").policy_reason(_manifest())
        == "workspace-retention-policy-mismatch"
    )
    assert (
        _workspace(deletion_policy="manual-delete").policy_reason(_manifest())
        == "workspace-deletion-policy-mismatch"
    )
    assert (
        _manifest(provenance=()).eligibility_reason(datetime(2026, 7, 22, 22, 0, tzinfo=UTC))
        == "missing-provenance"
    )
    assert (
        _manifest(intended_uses=()).eligibility_reason(datetime(2026, 7, 22, 22, 0, tzinfo=UTC))
        == "missing-intended-use"
    )
    assert (
        _manifest(intended_audiences=()).eligibility_reason(
            datetime(2026, 7, 22, 22, 0, tzinfo=UTC)
        )
        == "missing-intended-audience"
    )
    assert (
        _manifest(revoked_at=datetime(2026, 7, 22, 21, 0, tzinfo=UTC)).eligibility_reason(
            datetime(2026, 7, 22, 22, 0, tzinfo=UTC)
        )
        == "fixture-revoked"
    )

    async def async_verifier(_request: BenchmarkRunRequest, _measurement: BenchmarkMeasurement):
        return _verification()

    async_result = BenchmarkRunner((_Plugin(events=[]),)).run(_request(verifier=async_verifier))
    assert async_result.outcome is BenchmarkOutcome.FAILED
    assert async_result.reason_code == "plugin-exception"

    bad_compatibility = BenchmarkRunner((_Plugin(events=[], compatibility="bad"),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="compatibility responses"):
        bad_compatibility.run(_request())

    class _NoneSessionPlugin(_Plugin):
        def open(self, _request: BenchmarkRunRequest):
            self._events.append("open")
            return None

    infrastructure_result = BenchmarkRunner((_NoneSessionPlugin(events=[]),)).run(_request())
    assert infrastructure_result.outcome is BenchmarkOutcome.INFRASTRUCTURE_FAILURE
    assert infrastructure_result.reason_code == "plugin-setup-failed"

    class _BadMeasurementSession(_Session):
        def execute(self):
            self._events.append("execute")
            return "bad"

    bad_measurement = BenchmarkRunner(
        (_Plugin(events=[], session=_BadMeasurementSession([])),)
    ).run(_request())
    assert bad_measurement.reason_code == "plugin-exception"

    class _AwaitableOpen:
        def __init__(self) -> None:
            self.closed = False

        def __await__(self):
            yield "unexpected"

        def close(self) -> None:
            self.closed = True

    class _AwaitableOpenPlugin(_Plugin):
        def __init__(self, awaitable_open: _AwaitableOpen) -> None:
            super().__init__(events=[])
            self.awaitable_open = awaitable_open

        def open(self, _request: BenchmarkRunRequest):
            self._events.append("open")
            return self.awaitable_open

    awaitable_open = _AwaitableOpen()
    awaitable_result = BenchmarkRunner((_AwaitableOpenPlugin(awaitable_open),)).run(_request())
    assert awaitable_result.outcome is BenchmarkOutcome.INFRASTRUCTURE_FAILURE
    assert awaitable_result.reason_code == "plugin-setup-failed"
    assert awaitable_open.closed is True

    invalid_result = BenchmarkRunResult(
        outcome=BenchmarkOutcome.COMPLETED,
        reason_code="verified",
        attempted=True,
        binding=BenchmarkBinding(
            plugin_id="accessibility-ocr",
            plugin_version="1.2.3",
            procedure_id="ocr-procedure",
            artifact_digest=_sha("6"),
            runtime_name="python",
            runtime_version="3.12.4",
            adapter_id="benchmark-adapter",
            adapter_version="1.0.0",
            verifier_id="score-verifier",
            verifier_version="1.0.0",
            fixture_id="asr-fixture",
            fixture_manifest_digest=_manifest().digest,
            fixture_content_digest=_manifest().content_digest,
            environment_digest=_sha("3"),
            hardware_class="cpu",
            precision="fp32",
            configuration_digest=_sha("4"),
            threshold_digest=_sha("5"),
            effective_scope=AcceptanceScope.DETERMINISTIC,
            authorization_digest=_authorization().digest,
        ),
        score=1.0,
        metrics=(("wer", 0.0),),
        observed_at=datetime(2026, 7, 22, 22, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 22, 22, 0, tzinfo=UTC),
        evidence_digest=_sha("9"),
        supersedes=(),
        fixture_manifest_digest=_manifest().digest,
        fixture_content_digest=_manifest().content_digest,
        permitted_uses=("benchmarking",),
        permitted_audiences=("reviewers",),
    )
    assert invalid_result.payload()["metrics"] == {"wer": 0.0}
    with pytest.raises(ValueError, match="score must be finite"):
        replace(invalid_result, score=float("nan"))
    with pytest.raises(TypeError, match="export result"):
        BenchmarkExport(object())  # type: ignore[arg-type]
