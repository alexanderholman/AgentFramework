"""Governed benchmark plugin contracts, results, and fixture eligibility."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from inspect import isasyncgen, isawaitable, isgenerator
from typing import Protocol, cast

from af.application.bus import GateDecision, GateEffect
from af.framework.context import AcceptanceScope

__all__ = (
    "BenchmarkBinding",
    "BenchmarkCompatibility",
    "BenchmarkExecutionCancelled",
    "BenchmarkExecutionFailure",
    "BenchmarkExport",
    "BenchmarkFixtureAuthorization",
    "BenchmarkFixtureManifest",
    "BenchmarkInconclusiveResult",
    "BenchmarkMeasurement",
    "BenchmarkOutcome",
    "BenchmarkPlugin",
    "BenchmarkPluginDescriptor",
    "BenchmarkPluginRegistry",
    "BenchmarkPluginSession",
    "BenchmarkRunRequest",
    "BenchmarkRunResult",
    "BenchmarkRunner",
    "BenchmarkVerification",
    "BenchmarkWorkspace",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_VERSION = re.compile(
    r"^(?P<release>[0-9]+(?:\.[0-9]+){0,7})(?:(?P<pre>a|b|rc)(?P<pre_number>[0-9]+))?$"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PAIRS = 256
_MAX_TEXT = 512
_SCOPE_ORDER = {
    AcceptanceScope.CHILD: 0,
    AcceptanceScope.DETERMINISTIC: 1,
    AcceptanceScope.REVIEWED_GENERATIVE: 2,
    AcceptanceScope.OPERATOR_EVALUATION: 3,
}


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical identifier")
    return value


def _text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_TEXT
        or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value)
    ):
        raise ValueError(f"{label} must be a bounded single-line string")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _version(value: object, label: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{label} must use the supported version syntax")
    return value


def _digest(value: object, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone aware")
    return value.astimezone(UTC)


def _iso8601(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reason_code(value: object, label: str = "reason code") -> str:
    return _identifier(value, label)


def _sorted_unique_identifiers(values: object, label: str) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > _MAX_PAIRS:
        raise ValueError(f"{label} must be a bounded tuple")
    normalized = tuple(_identifier(item, label) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(normalized))


def _sorted_unique_digests(values: object, label: str) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > _MAX_PAIRS:
        raise ValueError(f"{label} must be a bounded tuple")
    normalized = tuple(_digest(item, label) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(normalized))


def _measurement_pairs(values: object, label: str) -> tuple[tuple[str, object], ...]:
    if type(values) is not tuple or len(values) > _MAX_PAIRS:
        raise ValueError(f"{label} must be a bounded tuple")
    normalized: list[tuple[str, object]] = []
    seen: set[str] = set()
    for item in values:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError(f"{label} items must be name/value pairs")
        name = _identifier(item[0], f"{label} name")
        if name in seen:
            raise ValueError(f"{label} names must be unique")
        scalar = item[1]
        if type(scalar) not in {str, int, float, bool} and scalar is not None:
            raise ValueError(f"{label} values must be JSON scalars")
        if isinstance(scalar, float) and (
            scalar != scalar or scalar in {float("inf"), float("-inf")}
        ):
            raise ValueError(f"{label} values must be finite")
        seen.add(name)
        normalized.append((name, scalar))
    return tuple(sorted(normalized))


def _scope_satisfies(granted: AcceptanceScope, required: AcceptanceScope) -> bool:
    return _SCOPE_ORDER[granted] >= _SCOPE_ORDER[required]


class BenchmarkOutcome(StrEnum):
    COMPLETED = "completed"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    CANCELLED = "cancelled"
    SKIPPED_BY_POLICY = "skipped_by_policy"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"


class BenchmarkExecutionFailure(RuntimeError):
    """Execution or scoring attempted and produced immutable negative evidence."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _reason_code(reason_code)
        super().__init__(self.reason_code)


class BenchmarkExecutionCancelled(RuntimeError):
    """Execution started but ended through a bounded cancellation path."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _reason_code(reason_code)
        super().__init__(self.reason_code)


class BenchmarkInconclusiveResult(RuntimeError):
    """Execution completed but independent verification refused a conclusive score."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _reason_code(reason_code)
        super().__init__(self.reason_code)


@dataclass(frozen=True, slots=True)
class BenchmarkCompatibility:
    is_supported: bool
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.is_supported) is not bool:
            raise ValueError("benchmark compatibility supported must be boolean")
        _reason_code(self.reason_code)

    @classmethod
    def supported(cls) -> BenchmarkCompatibility:
        return cls(True, "compatible")

    @classmethod
    def unsupported(cls, reason_code: str) -> BenchmarkCompatibility:
        return cls(False, reason_code)


@dataclass(frozen=True, slots=True)
class BenchmarkPluginDescriptor:
    plugin_id: str
    plugin_version: str
    artifact_digest: str

    def __post_init__(self) -> None:
        _identifier(self.plugin_id, "benchmark plugin id")
        _version(self.plugin_version, "benchmark plugin version")
        _digest(self.artifact_digest, "benchmark plugin artifact digest")


@dataclass(frozen=True, slots=True)
class BenchmarkMeasurement:
    values: tuple[tuple[str, object], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _measurement_pairs(self.values, "benchmark measurement"))

    def payload(self) -> dict[str, object]:
        return {name: value for name, value in self.values}


@dataclass(frozen=True, slots=True)
class BenchmarkVerification:
    score: float | None
    metrics: tuple[tuple[str, object], ...] = ()
    reason_code: str = "verified"

    def __post_init__(self) -> None:
        if self.score is not None:
            if type(self.score) not in {float, int}:
                raise ValueError("benchmark verification score must be numeric")
            numeric = float(self.score)
            if numeric != numeric or numeric in {float("inf"), float("-inf")}:
                raise ValueError("benchmark verification score must be finite")
            object.__setattr__(self, "score", numeric)
        object.__setattr__(self, "metrics", _measurement_pairs(self.metrics, "benchmark metric"))
        _reason_code(self.reason_code)

    def payload(self) -> dict[str, object]:
        return {
            "metrics": {name: value for name, value in self.metrics},
            "reason_code": self.reason_code,
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkFixtureManifest:
    fixture_id: str
    content_digest: str
    source_id: str
    source_revision: str
    provenance: tuple[str, ...]
    license_basis: str | None
    copyright_basis: str | None
    collection_method: str | None
    consent_basis: str | None
    legal_basis: str | None
    transformations: tuple[str, ...] = ()
    splits: tuple[str, ...] = ()
    leakage_controls: tuple[str, ...] = ()
    synthetic: bool = False
    maintainers: tuple[str, ...] = ()
    access_tier: str | None = None
    intended_uses: tuple[str, ...] = ()
    intended_audiences: tuple[str, ...] = ()
    retention_policy: str | None = None
    deletion_policy: str | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revocation_policy: str | None = None
    required_approval_class: str | None = None
    reviewer_authority: str | None = None
    prohibited_uses: tuple[str, ...] = ()
    export_policy: str | None = None
    governance_categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.fixture_id, "benchmark fixture id")
        _digest(self.content_digest, "benchmark fixture content digest")
        _identifier(self.source_id, "benchmark fixture source id")
        _text(self.source_revision, "benchmark fixture source revision")
        object.__setattr__(
            self, "provenance", _sorted_unique_digests(self.provenance, "benchmark provenance")
        )
        for field_name in (
            "license_basis",
            "copyright_basis",
            "collection_method",
            "consent_basis",
            "legal_basis",
            "access_tier",
            "retention_policy",
            "deletion_policy",
            "revocation_policy",
            "required_approval_class",
            "reviewer_authority",
            "export_policy",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), f"benchmark {field_name}"),
            )
        for field_name in (
            "transformations",
            "splits",
            "leakage_controls",
            "maintainers",
            "intended_uses",
            "intended_audiences",
            "prohibited_uses",
            "governance_categories",
        ):
            object.__setattr__(
                self,
                field_name,
                _sorted_unique_identifiers(getattr(self, field_name), f"benchmark {field_name}"),
            )
        if type(self.synthetic) is not bool:
            raise ValueError("benchmark synthetic flag must be boolean")
        if self.expires_at is not None:
            object.__setattr__(
                self, "expires_at", _aware_utc(self.expires_at, "benchmark expiry time")
            )
        if self.revoked_at is not None:
            object.__setattr__(
                self, "revoked_at", _aware_utc(self.revoked_at, "benchmark revocation time")
            )
        if (
            self.expires_at is not None
            and self.revoked_at is not None
            and self.revoked_at > self.expires_at
        ):
            raise ValueError("benchmark revocation cannot follow expiry")

    def eligibility_reason(self, observed_at: datetime) -> str | None:
        observed = _aware_utc(observed_at, "benchmark eligibility observation")
        required = (
            (self.license_basis, "missing-license-basis"),
            (self.copyright_basis, "missing-copyright-basis"),
            (self.collection_method, "missing-collection-method"),
            (self.consent_basis, "missing-consent-basis"),
            (self.legal_basis, "missing-legal-basis"),
            (self.access_tier, "missing-access-tier"),
            (self.retention_policy, "missing-retention-policy"),
            (self.deletion_policy, "missing-deletion-policy"),
            (self.required_approval_class, "missing-required-approval-class"),
            (self.reviewer_authority, "missing-reviewer-authority"),
            (self.export_policy, "missing-export-policy"),
        )
        for value, reason in required:
            if value is None:
                return reason
        if not self.provenance:
            return "missing-provenance"
        if not self.intended_uses:
            return "missing-intended-use"
        if not self.intended_audiences:
            return "missing-intended-audience"
        if self.revoked_at is not None and observed >= self.revoked_at:
            return "fixture-revoked"
        if self.expires_at is not None and observed > self.expires_at:
            return "fixture-expired"
        return None

    def payload(self) -> dict[str, object]:
        return {
            "access_tier": self.access_tier,
            "collection_method": self.collection_method,
            "consent_basis": self.consent_basis,
            "content_digest": self.content_digest,
            "copyright_basis": self.copyright_basis,
            "deletion_policy": self.deletion_policy,
            "expires_at": _iso8601(self.expires_at),
            "export_policy": self.export_policy,
            "fixture_id": self.fixture_id,
            "governance_categories": list(self.governance_categories),
            "intended_audiences": list(self.intended_audiences),
            "intended_uses": list(self.intended_uses),
            "leakage_controls": list(self.leakage_controls),
            "legal_basis": self.legal_basis,
            "license_basis": self.license_basis,
            "maintainers": list(self.maintainers),
            "prohibited_uses": list(self.prohibited_uses),
            "provenance": list(self.provenance),
            "required_approval_class": self.required_approval_class,
            "retention_policy": self.retention_policy,
            "reviewer_authority": self.reviewer_authority,
            "revocation_policy": self.revocation_policy,
            "revoked_at": _iso8601(self.revoked_at),
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "splits": list(self.splits),
            "synthetic": self.synthetic,
            "transformations": list(self.transformations),
        }

    @property
    def digest(self) -> str:
        return _sha256(self.payload())


@dataclass(frozen=True, slots=True, repr=False)
class BenchmarkFixtureAuthorization:
    approval_class: str
    reviewer_authority: str
    decision: GateDecision
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    permitted_uses: tuple[str, ...] = ()
    permitted_audiences: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.approval_class, "benchmark approval class")
        _identifier(self.reviewer_authority, "benchmark reviewer authority")
        if not isinstance(self.decision, GateDecision):
            raise ValueError("benchmark authorization decision is invalid")
        issued = _aware_utc(self.issued_at, "benchmark authorization issue time")
        expires = _aware_utc(self.expires_at, "benchmark authorization expiry time")
        if expires <= issued:
            raise ValueError("benchmark authorization expiry must follow issue time")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        if self.revoked_at is not None:
            object.__setattr__(
                self,
                "revoked_at",
                _aware_utc(self.revoked_at, "benchmark authorization revocation time"),
            )
        object.__setattr__(
            self,
            "permitted_uses",
            _sorted_unique_identifiers(self.permitted_uses, "benchmark permitted uses"),
        )
        object.__setattr__(
            self,
            "permitted_audiences",
            _sorted_unique_identifiers(self.permitted_audiences, "benchmark permitted audiences"),
        )

    def policy_reason(
        self,
        observed_at: datetime,
        required_scope: AcceptanceScope,
        *,
        fixture: BenchmarkFixtureManifest,
    ) -> str | None:
        observed = _aware_utc(observed_at, "benchmark authorization observation")
        if self.decision.effect is GateEffect.DENY:
            return self.decision.reason_code
        if self.revoked_at is not None and observed >= self.revoked_at:
            return "authorization-revoked"
        if not self.issued_at <= observed <= self.expires_at:
            return "authorization-expired"
        granted = self.decision.effective_scope
        if granted is None or not _scope_satisfies(granted, required_scope):
            return "effective-scope-narrowed"
        if self.approval_class != fixture.required_approval_class:
            return "fixture-approval-class-mismatch"
        if self.reviewer_authority != fixture.reviewer_authority:
            return "fixture-reviewer-authority-mismatch"
        permitted_uses = set(self.permitted_uses)
        if any(use in permitted_uses for use in fixture.prohibited_uses):
            return "fixture-use-prohibited"
        if any(use not in permitted_uses for use in fixture.intended_uses):
            return "fixture-use-not-permitted"
        permitted_audiences = set(self.permitted_audiences)
        if any(audience not in permitted_audiences for audience in fixture.intended_audiences):
            return "fixture-audience-not-permitted"
        return None

    def payload(self) -> dict[str, object]:
        return {
            "approval_class": self.approval_class,
            "decision_effect": self.decision.effect.value,
            "decision_reason_code": self.decision.reason_code,
            "decision_scope": None
            if self.decision.effective_scope is None
            else self.decision.effective_scope.value,
            "expires_at": _iso8601(self.expires_at),
            "issued_at": _iso8601(self.issued_at),
            "permitted_audiences": list(self.permitted_audiences),
            "permitted_uses": list(self.permitted_uses),
            "policy_version": self.decision.policy_version,
            "reviewer_authority": self.reviewer_authority,
            "revoked_at": _iso8601(self.revoked_at),
        }

    @property
    def digest(self) -> str:
        return _sha256(self.payload())

    def __repr__(self) -> str:
        return (
            "BenchmarkFixtureAuthorization(approval_class='[REDACTED]', "
            "reviewer_authority='[REDACTED]', decision='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True)
class BenchmarkWorkspace:
    workspace_id: str
    access_tier: str
    encrypted: bool
    declared_network_egress: tuple[str, ...]
    retention_policy: str
    deletion_policy: str

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, "benchmark workspace id")
        _identifier(self.access_tier, "benchmark workspace access tier")
        if type(self.encrypted) is not bool:
            raise ValueError("benchmark workspace encryption must be boolean")
        object.__setattr__(
            self,
            "declared_network_egress",
            _sorted_unique_identifiers(self.declared_network_egress, "benchmark network egress"),
        )
        _identifier(self.retention_policy, "benchmark workspace retention policy")
        _identifier(self.deletion_policy, "benchmark workspace deletion policy")

    def policy_reason(self, fixture: BenchmarkFixtureManifest) -> str | None:
        if not self.encrypted:
            return "workspace-not-encrypted"
        if self.declared_network_egress:
            return "workspace-network-egress-declared"
        if fixture.access_tier is not None and self.access_tier != fixture.access_tier:
            return "workspace-access-tier-mismatch"
        if (
            fixture.retention_policy is not None
            and self.retention_policy != fixture.retention_policy
        ):
            return "workspace-retention-policy-mismatch"
        if fixture.deletion_policy is not None and self.deletion_policy != fixture.deletion_policy:
            return "workspace-deletion-policy-mismatch"
        return None


@dataclass(frozen=True, slots=True)
class BenchmarkBinding:
    plugin_id: str
    plugin_version: str | None
    procedure_id: str
    artifact_digest: str | None
    runtime_name: str
    runtime_version: str
    adapter_id: str
    adapter_version: str
    verifier_id: str
    verifier_version: str
    fixture_id: str
    fixture_manifest_digest: str
    fixture_content_digest: str
    environment_digest: str
    hardware_class: str
    precision: str
    configuration_digest: str
    threshold_digest: str
    effective_scope: AcceptanceScope | None
    authorization_digest: str

    def __post_init__(self) -> None:
        _identifier(self.plugin_id, "benchmark plugin id")
        if self.plugin_version is not None:
            _version(self.plugin_version, "benchmark plugin version")
        _identifier(self.procedure_id, "benchmark procedure id")
        if self.artifact_digest is not None:
            _digest(self.artifact_digest, "benchmark artifact digest")
        _identifier(self.runtime_name, "benchmark runtime name")
        _version(self.runtime_version, "benchmark runtime version")
        _identifier(self.adapter_id, "benchmark adapter id")
        _version(self.adapter_version, "benchmark adapter version")
        _identifier(self.verifier_id, "benchmark verifier id")
        _version(self.verifier_version, "benchmark verifier version")
        _identifier(self.fixture_id, "benchmark fixture id")
        _digest(self.fixture_manifest_digest, "benchmark fixture manifest digest")
        _digest(self.fixture_content_digest, "benchmark fixture content digest")
        _digest(self.environment_digest, "benchmark environment digest")
        _identifier(self.hardware_class, "benchmark hardware class")
        _identifier(self.precision, "benchmark precision")
        _digest(self.configuration_digest, "benchmark configuration digest")
        _digest(self.threshold_digest, "benchmark threshold digest")
        if self.effective_scope is not None and not isinstance(
            self.effective_scope, AcceptanceScope
        ):
            raise ValueError("benchmark effective scope is invalid")
        _digest(self.authorization_digest, "benchmark authorization digest")

    def payload(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "artifact_digest": self.artifact_digest,
            "authorization_digest": self.authorization_digest,
            "configuration_digest": self.configuration_digest,
            "effective_scope": None if self.effective_scope is None else self.effective_scope.value,
            "environment_digest": self.environment_digest,
            "fixture_content_digest": self.fixture_content_digest,
            "fixture_id": self.fixture_id,
            "fixture_manifest_digest": self.fixture_manifest_digest,
            "hardware_class": self.hardware_class,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "precision": self.precision,
            "procedure_id": self.procedure_id,
            "runtime_name": self.runtime_name,
            "runtime_version": self.runtime_version,
            "threshold_digest": self.threshold_digest,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
        }

    @property
    def digest(self) -> str:
        return _sha256(self.payload())


class BenchmarkVerifier(Protocol):
    def __call__(
        self,
        request: BenchmarkRunRequest,
        measurement: BenchmarkMeasurement,
        /,
    ) -> BenchmarkVerification: ...


@dataclass(frozen=True, slots=True)
class BenchmarkRunRequest:
    benchmark_id: str
    plugin_id: str
    procedure_id: str
    fixture: BenchmarkFixtureManifest
    authorization: BenchmarkFixtureAuthorization
    workspace: BenchmarkWorkspace
    runtime_name: str
    runtime_version: str
    adapter_id: str
    adapter_version: str
    verifier_id: str
    verifier_version: str
    environment_digest: str
    hardware_class: str
    precision: str
    configuration_digest: str
    threshold_digest: str
    required_scope: AcceptanceScope
    verifier: BenchmarkVerifier
    observed_at: datetime
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.benchmark_id, "benchmark id")
        _identifier(self.plugin_id, "benchmark plugin id")
        _identifier(self.procedure_id, "benchmark procedure id")
        if not isinstance(self.fixture, BenchmarkFixtureManifest):
            raise TypeError("benchmark fixture manifest is invalid")
        if not isinstance(self.authorization, BenchmarkFixtureAuthorization):
            raise TypeError("benchmark fixture authorization is invalid")
        if not isinstance(self.workspace, BenchmarkWorkspace):
            raise TypeError("benchmark workspace is invalid")
        _identifier(self.runtime_name, "benchmark runtime name")
        _version(self.runtime_version, "benchmark runtime version")
        _identifier(self.adapter_id, "benchmark adapter id")
        _version(self.adapter_version, "benchmark adapter version")
        _identifier(self.verifier_id, "benchmark verifier id")
        _version(self.verifier_version, "benchmark verifier version")
        _digest(self.environment_digest, "benchmark environment digest")
        _identifier(self.hardware_class, "benchmark hardware class")
        _identifier(self.precision, "benchmark precision")
        _digest(self.configuration_digest, "benchmark configuration digest")
        _digest(self.threshold_digest, "benchmark threshold digest")
        if not isinstance(self.required_scope, AcceptanceScope):
            raise TypeError("benchmark required scope is invalid")
        if not callable(self.verifier):
            raise ValueError("benchmark verifier must be callable")
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "benchmark observation time")
        )
        object.__setattr__(
            self,
            "supersedes",
            _sorted_unique_digests(self.supersedes, "benchmark supersedes digest"),
        )


class BenchmarkPluginSession(Protocol):
    def setup(self) -> None: ...

    def execute(self) -> BenchmarkMeasurement: ...

    def teardown(self) -> None: ...


class BenchmarkPlugin(Protocol):
    def descriptor(self) -> BenchmarkPluginDescriptor: ...

    def compatibility(self, request: BenchmarkRunRequest) -> BenchmarkCompatibility: ...

    def open(self, request: BenchmarkRunRequest) -> BenchmarkPluginSession: ...


class BenchmarkPluginRegistry:
    """Deterministic in-process plugin discovery for one benchmark runner."""

    def __init__(self, plugins: Iterable[BenchmarkPlugin]) -> None:
        descriptors: dict[str, tuple[BenchmarkPluginDescriptor, BenchmarkPlugin]] = {}
        for plugin in tuple(plugins):
            if not hasattr(plugin, "descriptor") or not callable(plugin.descriptor):
                raise TypeError("benchmark plugins must expose a descriptor() method")
            descriptor = plugin.descriptor()
            if not isinstance(descriptor, BenchmarkPluginDescriptor):
                raise TypeError("benchmark plugin descriptors are invalid")
            if descriptor.plugin_id in descriptors:
                raise ValueError("benchmark plugin ids must be unique")
            descriptors[descriptor.plugin_id] = (descriptor, plugin)
        self._plugins = descriptors

    def descriptors(self) -> tuple[BenchmarkPluginDescriptor, ...]:
        return tuple(
            descriptor
            for descriptor, _plugin in sorted(
                self._plugins.values(), key=lambda item: item[0].plugin_id
            )
        )

    def resolve(self, plugin_id: str) -> tuple[BenchmarkPluginDescriptor, BenchmarkPlugin] | None:
        _identifier(plugin_id, "benchmark plugin id")
        return self._plugins.get(plugin_id)


@dataclass(frozen=True, slots=True)
class BenchmarkRunResult:
    outcome: BenchmarkOutcome
    reason_code: str
    attempted: bool
    binding: BenchmarkBinding
    score: float | None
    metrics: tuple[tuple[str, object], ...]
    observed_at: datetime
    finished_at: datetime
    evidence_digest: str
    supersedes: tuple[str, ...]
    fixture_manifest_digest: str
    fixture_content_digest: str
    permitted_uses: tuple[str, ...]
    permitted_audiences: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, BenchmarkOutcome):
            raise ValueError("benchmark outcome is invalid")
        _reason_code(self.reason_code)
        if type(self.attempted) is not bool:
            raise ValueError("benchmark attempted flag must be boolean")
        if not isinstance(self.binding, BenchmarkBinding):
            raise TypeError("benchmark binding is invalid")
        if self.score is not None:
            if type(self.score) not in {float, int}:
                raise ValueError("benchmark score must be numeric")
            numeric = float(self.score)
            if numeric != numeric or numeric in {float("inf"), float("-inf")}:
                raise ValueError("benchmark score must be finite")
            object.__setattr__(self, "score", numeric)
        object.__setattr__(
            self, "metrics", _measurement_pairs(self.metrics, "benchmark result metric")
        )
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "benchmark result observation")
        )
        object.__setattr__(
            self, "finished_at", _aware_utc(self.finished_at, "benchmark result finish time")
        )
        if self.finished_at < self.observed_at:
            raise ValueError("benchmark finish time cannot precede observation")
        _digest(self.evidence_digest, "benchmark evidence digest")
        object.__setattr__(
            self,
            "supersedes",
            _sorted_unique_digests(self.supersedes, "benchmark supersedes digest"),
        )
        _digest(self.fixture_manifest_digest, "benchmark fixture manifest digest")
        _digest(self.fixture_content_digest, "benchmark fixture content digest")
        object.__setattr__(
            self,
            "permitted_uses",
            _sorted_unique_identifiers(self.permitted_uses, "benchmark permitted uses"),
        )
        object.__setattr__(
            self,
            "permitted_audiences",
            _sorted_unique_identifiers(self.permitted_audiences, "benchmark permitted audiences"),
        )

    @property
    def certifies_production(self) -> bool:
        return False

    def payload(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "binding": self.binding.payload(),
            "evidence_digest": self.evidence_digest,
            "finished_at": _iso8601(self.finished_at),
            "fixture_content_digest": self.fixture_content_digest,
            "fixture_manifest_digest": self.fixture_manifest_digest,
            "metrics": {name: value for name, value in self.metrics},
            "observed_at": _iso8601(self.observed_at),
            "outcome": self.outcome.value,
            "permitted_audiences": list(self.permitted_audiences),
            "permitted_uses": list(self.permitted_uses),
            "reason_code": self.reason_code,
            "score": self.score,
            "supersedes": list(self.supersedes),
        }

    def export(self) -> BenchmarkExport:
        return BenchmarkExport(self)


@dataclass(frozen=True, slots=True)
class BenchmarkExport:
    result: BenchmarkRunResult

    def __post_init__(self) -> None:
        if not isinstance(self.result, BenchmarkRunResult):
            raise TypeError("benchmark export result is invalid")

    @property
    def certifies_production(self) -> bool:
        return False

    @property
    def certifies_domain_governance(self) -> bool:
        return False

    @property
    def certifies_physical_isolation(self) -> bool:
        return False

    def payload(self) -> dict[str, object]:
        return {
            "binding_digest": self.result.binding.digest,
            "evidence_digest": self.result.evidence_digest,
            "fixture": {
                "content_digest": self.result.fixture_content_digest,
                "fixture_id": self.result.binding.fixture_id,
                "manifest_digest": self.result.fixture_manifest_digest,
            },
            "finished_at": _iso8601(self.result.finished_at),
            "metrics": {name: value for name, value in self.result.metrics},
            "observed_at": _iso8601(self.result.observed_at),
            "outcome": self.result.outcome.value,
            "permitted_audiences": list(self.result.permitted_audiences),
            "permitted_uses": list(self.result.permitted_uses),
            "reason_code": self.result.reason_code,
            "score": self.result.score,
            "supersedes": list(self.result.supersedes),
        }


def _call_sync(fn: Callable[..., object], *args: object) -> object:
    result = fn(*args)
    if isawaitable(result) or isgenerator(result) or isasyncgen(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise TypeError("benchmark callbacks must run synchronously without yielding")
    return result


class BenchmarkRunner:
    """Execute one benchmark request against a deterministic local plugin registry."""

    def __init__(
        self,
        plugins: Iterable[BenchmarkPlugin],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = BenchmarkPluginRegistry(plugins)
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(self, request: BenchmarkRunRequest) -> BenchmarkRunResult:
        if not isinstance(request, BenchmarkRunRequest):
            raise TypeError("benchmark request is invalid")
        started_at = _aware_utc(self._clock(), "benchmark start time")
        preflight_reason = self._policy_reason(request, started_at)
        if preflight_reason is not None:
            return self._result(
                request,
                descriptor=None,
                outcome=BenchmarkOutcome.SKIPPED_BY_POLICY,
                reason_code=preflight_reason,
                attempted=False,
                score=None,
                metrics=(),
                observed_at=started_at,
                finished_at=started_at,
            )

        resolved = self._registry.resolve(request.plugin_id)
        if resolved is None:
            return self._result(
                request,
                descriptor=None,
                outcome=BenchmarkOutcome.UNSUPPORTED,
                reason_code="plugin-not-installed",
                attempted=False,
                score=None,
                metrics=(),
                observed_at=started_at,
                finished_at=started_at,
            )
        descriptor, plugin = resolved
        compatibility = _call_sync(plugin.compatibility, request)
        if not isinstance(compatibility, BenchmarkCompatibility):
            raise TypeError("benchmark compatibility responses are invalid")
        if not compatibility.is_supported:
            return self._result(
                request,
                descriptor=descriptor,
                outcome=BenchmarkOutcome.UNSUPPORTED,
                reason_code=compatibility.reason_code,
                attempted=False,
                score=None,
                metrics=(),
                observed_at=started_at,
                finished_at=started_at,
            )

        attempted = False
        score: float | None = None
        metrics: tuple[tuple[str, object], ...] = ()
        outcome = BenchmarkOutcome.INFRASTRUCTURE_FAILURE
        reason_code = "plugin-open-failed"
        session: BenchmarkPluginSession | None = None
        try:
            opened = _call_sync(plugin.open, request)
            if opened is None:
                raise TypeError("benchmark plugin session is invalid")
            session = cast(BenchmarkPluginSession, opened)
            _call_sync(session.setup)
            attempted = True
            measurement = _call_sync(session.execute)
            if not isinstance(measurement, BenchmarkMeasurement):
                raise TypeError("benchmark measurements are invalid")
            verification = _call_sync(request.verifier, request, measurement)
            if not isinstance(verification, BenchmarkVerification):
                raise BenchmarkExecutionFailure("verifier-invalid-output")
            postflight_reason = self._policy_reason(
                request,
                _aware_utc(self._clock(), "benchmark acceptance time"),
            )
            if postflight_reason is not None:
                outcome = BenchmarkOutcome.SKIPPED_BY_POLICY
                reason_code = postflight_reason
            else:
                outcome = BenchmarkOutcome.COMPLETED
                reason_code = verification.reason_code
                score = verification.score
                metrics = verification.metrics
        except BenchmarkExecutionCancelled as error:
            outcome = BenchmarkOutcome.CANCELLED
            reason_code = error.reason_code
        except TimeoutError:
            outcome = BenchmarkOutcome.CANCELLED
            reason_code = "deadline-exceeded"
        except BenchmarkInconclusiveResult as error:
            outcome = BenchmarkOutcome.INCONCLUSIVE
            reason_code = error.reason_code
        except BenchmarkExecutionFailure as error:
            outcome = BenchmarkOutcome.FAILED
            reason_code = error.reason_code
        except Exception:
            outcome = (
                BenchmarkOutcome.FAILED if attempted else BenchmarkOutcome.INFRASTRUCTURE_FAILURE
            )
            reason_code = "plugin-exception" if attempted else "plugin-setup-failed"
        finally:
            teardown_error: Exception | None = None
            if session is not None:
                try:
                    _call_sync(session.teardown)
                except Exception as error:  # pragma: no cover - exercised through result semantics
                    teardown_error = error
            if teardown_error is not None:
                outcome = BenchmarkOutcome.INFRASTRUCTURE_FAILURE
                reason_code = "teardown-failed"

        finished_at = _aware_utc(self._clock(), "benchmark finish time")
        return self._result(
            request,
            descriptor=descriptor,
            outcome=outcome,
            reason_code=reason_code,
            attempted=attempted,
            score=score,
            metrics=metrics,
            observed_at=started_at,
            finished_at=finished_at,
        )

    @staticmethod
    def _policy_reason(request: BenchmarkRunRequest, observed_at: datetime) -> str | None:
        return (
            request.fixture.eligibility_reason(observed_at)
            or request.workspace.policy_reason(request.fixture)
            or request.authorization.policy_reason(
                observed_at,
                request.required_scope,
                fixture=request.fixture,
            )
        )

    @staticmethod
    def _binding(
        request: BenchmarkRunRequest,
        descriptor: BenchmarkPluginDescriptor | None,
    ) -> BenchmarkBinding:
        return BenchmarkBinding(
            plugin_id=request.plugin_id,
            plugin_version=None if descriptor is None else descriptor.plugin_version,
            procedure_id=request.procedure_id,
            artifact_digest=None if descriptor is None else descriptor.artifact_digest,
            runtime_name=request.runtime_name,
            runtime_version=request.runtime_version,
            adapter_id=request.adapter_id,
            adapter_version=request.adapter_version,
            verifier_id=request.verifier_id,
            verifier_version=request.verifier_version,
            fixture_id=request.fixture.fixture_id,
            fixture_manifest_digest=request.fixture.digest,
            fixture_content_digest=request.fixture.content_digest,
            environment_digest=request.environment_digest,
            hardware_class=request.hardware_class,
            precision=request.precision,
            configuration_digest=request.configuration_digest,
            threshold_digest=request.threshold_digest,
            effective_scope=request.authorization.decision.effective_scope,
            authorization_digest=request.authorization.digest,
        )

    @classmethod
    def _result(
        cls,
        request: BenchmarkRunRequest,
        *,
        descriptor: BenchmarkPluginDescriptor | None,
        outcome: BenchmarkOutcome,
        reason_code: str,
        attempted: bool,
        score: float | None,
        metrics: tuple[tuple[str, object], ...],
        observed_at: datetime,
        finished_at: datetime,
    ) -> BenchmarkRunResult:
        binding = cls._binding(request, descriptor)
        payload = {
            "attempted": attempted,
            "binding": binding.payload(),
            "finished_at": _iso8601(finished_at),
            "metrics": {name: value for name, value in metrics},
            "observed_at": _iso8601(observed_at),
            "outcome": outcome.value,
            "reason_code": reason_code,
            "score": score,
            "supersedes": list(request.supersedes),
        }
        return BenchmarkRunResult(
            outcome=outcome,
            reason_code=reason_code,
            attempted=attempted,
            binding=binding,
            score=score,
            metrics=metrics,
            observed_at=observed_at,
            finished_at=finished_at,
            evidence_digest=_sha256(payload),
            supersedes=request.supersedes,
            fixture_manifest_digest=request.fixture.digest,
            fixture_content_digest=request.fixture.content_digest,
            permitted_uses=request.authorization.permitted_uses,
            permitted_audiences=request.authorization.permitted_audiences,
        )
