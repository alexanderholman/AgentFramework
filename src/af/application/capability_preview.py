"""Pure, immutable capability projections; this module has no execution ports.

Manifest declarations are deliberately separate from locally supplied observations
and policy. Importing a catalogue cannot make a capability authorized or ready.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from graphlib import TopologicalSorter
from math import isfinite

from af.framework.capabilities import (
    CapabilityCandidate,
    CapabilityDescriptor,
    CapabilityLifecycle,
    CapabilityReasonCode,
    CapabilityResourceEnvelope,
    ExactCapabilityMatcher,
    RequestRequirements,
)


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _check_digest(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("expected a canonical SHA-256 digest")


def _time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("observation timestamps must be timezone aware")
    return value.astimezone(UTC)


class PreviewOutcome(StrEnum):
    AVAILABLE = "available"
    AVAILABLE_BUT_COSTLY = "available_but_costly"
    CONDITIONAL = "conditionally_available"
    UNAVAILABLE = "unavailable"
    UNCERTAIN = "uncertain"
    DENIED = "policy_denied"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class CostInterval:
    lower: float
    upper: float

    def __post_init__(self) -> None:
        if (
            any(
                type(value) not in {float, int} or not isfinite(value)
                for value in (self.lower, self.upper)
            )
            or not 0 <= self.lower <= self.upper
        ):
            raise ValueError("cost intervals must be finite, nonnegative and ordered")


@dataclass(frozen=True, slots=True)
class CostProjection:
    """Units are in field names. None means unmeasured, never zero.

    latency_ms is the total projected latency, including any applicable cold load,
    swap, queue, preprocessing, verification, review and fallback. Breakdowns are
    additional evidence, not quantities to add to that total a second time.
    """

    latency_ms: CostInterval | None = None
    memory_mib: CostInterval | None = None
    cpu_millis: CostInterval | None = None
    accelerator_millis: CostInterval | None = None
    estimated_cost_micros: CostInterval | None = None
    storage_bytes: CostInterval | None = None
    transfer_bytes: CostInterval | None = None
    energy_joules: CostInterval | None = None
    cold_load_ms: CostInterval | None = None
    swap_ms: CostInterval | None = None
    queue_ms: CostInterval | None = None
    verification_ms: CostInterval | None = None
    review_ms: CostInterval | None = None
    fallback_ms: CostInterval | None = None
    privacy_exposure: str = "unknown"
    operational_risk: str = "unknown"
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if name not in {"privacy_exposure", "operational_risk", "assumptions"}:
                value = getattr(self, name)
                if value is not None and not isinstance(value, CostInterval):
                    raise ValueError("projection measurements must be typed intervals")
        if type(self.assumptions) is not tuple or any(
            not isinstance(item, str) or len(item) > 512 for item in self.assumptions
        ):
            raise ValueError("projection assumptions must be bounded immutable text")

    @classmethod
    def from_envelope(cls, envelope: CapabilityResourceEnvelope) -> CostProjection:
        return cls(
            latency_ms=CostInterval(envelope.latency_ms, envelope.latency_ms),
            memory_mib=CostInterval(envelope.memory_mib, envelope.memory_mib),
            cpu_millis=CostInterval(envelope.cpu_millis, envelope.cpu_millis),
            accelerator_millis=CostInterval(
                envelope.accelerator_millis, envelope.accelerator_millis
            ),
            estimated_cost_micros=CostInterval(
                envelope.estimated_cost_micros, envelope.estimated_cost_micros
            ),
        )

    def upper_envelope(self) -> CapabilityResourceEnvelope | None:
        fields = (
            self.latency_ms,
            self.memory_mib,
            self.cpu_millis,
            self.accelerator_millis,
            self.estimated_cost_micros,
        )
        if any(value is None for value in fields):
            return None
        # The matcher contract has bounded integer units. A projection outside
        # those bounds is unknown to that matcher, not a truncated cheap value.
        from math import ceil

        values = [ceil(value.upper) for value in fields if value is not None]
        if any(value > 1_000_000_000 for value in values):
            return None
        return CapabilityResourceEnvelope(values[0], values[1], values[2], values[3], values[4])

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def combine(cls, projections: tuple[CostProjection, ...]) -> CostProjection | None:
        """Retain the full observed range; unknown constituents remain unknown."""
        if not projections:
            return None

        def interval(name: str) -> CostInterval | None:
            values: list[CostInterval] = []
            for projection in projections:
                value = getattr(projection, name)
                if not isinstance(value, CostInterval):
                    return None
                values.append(value)
            return CostInterval(
                min(value.lower for value in values), max(value.upper for value in values)
            )

        return cls(
            latency_ms=interval("latency_ms"),
            memory_mib=interval("memory_mib"),
            cpu_millis=interval("cpu_millis"),
            accelerator_millis=interval("accelerator_millis"),
            estimated_cost_micros=interval("estimated_cost_micros"),
            storage_bytes=interval("storage_bytes"),
            transfer_bytes=interval("transfer_bytes"),
            energy_joules=interval("energy_joules"),
            cold_load_ms=interval("cold_load_ms"),
            swap_ms=interval("swap_ms"),
            queue_ms=interval("queue_ms"),
            verification_ms=interval("verification_ms"),
            review_ms=interval("review_ms"),
            fallback_ms=interval("fallback_ms"),
            privacy_exposure=projections[0].privacy_exposure
            if len({item.privacy_exposure for item in projections}) == 1
            else "unknown",
            operational_risk=projections[0].operational_risk
            if len({item.operational_risk for item in projections}) == 1
            else "unknown",
            assumptions=tuple(
                sorted(
                    {
                        assumption
                        for projection in projections
                        for assumption in projection.assumptions
                    }
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class CapabilityObservation:
    observation_id: str
    descriptor_digest: str
    artifact_digest: str
    runtime_digest: str
    configuration_digest: str
    hardware_digest: str
    benchmark_digest: str
    verifier_id: str
    outcome: str
    observed_at: datetime
    expires_at: datetime
    projection: CostProjection
    revoked: bool = False
    supersedes: tuple[str, ...] = ()
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0" or self.outcome not in {"pass", "fail", "inconclusive"}:
            raise ValueError("invalid observation version or outcome")
        for value in (
            self.descriptor_digest,
            self.artifact_digest,
            self.runtime_digest,
            self.configuration_digest,
            self.hardware_digest,
            self.benchmark_digest,
        ):
            _check_digest(value)
        for name in ("observation_id", "verifier_id"):
            if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", getattr(self, name)) is None:
                raise ValueError("observation identity must be canonical")
        object.__setattr__(self, "observed_at", _time(self.observed_at))
        object.__setattr__(self, "expires_at", _time(self.expires_at))
        if self.expires_at < self.observed_at:
            raise ValueError("observation expiry predates observation")
        if not isinstance(self.projection, CostProjection) or type(self.revoked) is not bool:
            raise ValueError("invalid observation projection or revocation")
        if type(self.supersedes) is not tuple:
            raise ValueError("supersession links must be immutable")
        for digest in self.supersedes:
            _check_digest(digest)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["observed_at"] = self.observed_at.isoformat()
        result["expires_at"] = self.expires_at.isoformat()
        return result

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    descriptor: CapabilityDescriptor
    artifact_digest: str | None = None
    runtime_digest: str | None = None
    configuration_digest: str | None = None
    hardware_digest: str | None = None
    kind: str = "model"
    installed: bool = False
    healthy: bool = False
    observations: tuple[CapabilityObservation, ...] = ()
    restrictions: tuple[str, ...] = ()
    source_json: str = "{}"

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, CapabilityDescriptor):
            raise ValueError("catalogue record requires a typed capability descriptor")
        if self.kind not in {
            "model",
            "component",
            "encoder",
            "tokenizer",
            "fixture",
            "runtime",
            "software",
            "pipeline",
            "group",
            "procedure",
            "workflow",
            "human",
            "static",
        }:
            raise ValueError("unknown catalogue asset kind")
        for value in (
            self.artifact_digest,
            self.runtime_digest,
            self.configuration_digest,
            self.hardware_digest,
        ):
            if value is not None:
                _check_digest(value)
        if type(self.installed) is not bool or type(self.healthy) is not bool:
            raise ValueError("catalogue operational state must be boolean")
        if type(self.observations) is not tuple or any(
            not isinstance(item, CapabilityObservation) for item in self.observations
        ):
            raise ValueError("catalogue observations must be immutable")
        if len({item.observation_id for item in self.observations}) != len(self.observations):
            raise ValueError("observation identities must be unique")
        object.__setattr__(
            self,
            "observations",
            tuple(
                sorted(self.observations, key=lambda item: (item.observed_at, item.observation_id))
            ),
        )
        if type(self.restrictions) is not tuple or any(
            not isinstance(item, str) for item in self.restrictions
        ):
            raise ValueError("catalogue restrictions must be immutable")
        if len(self.source_json) > 1_048_576:
            raise ValueError("catalogue source record is oversized")
        _json(json.loads(self.source_json))

    def to_dict(self) -> dict[str, object]:
        return {
            "descriptor": self.descriptor.to_dict(),
            "artifact_digest": self.artifact_digest,
            "runtime_digest": self.runtime_digest,
            "configuration_digest": self.configuration_digest,
            "hardware_digest": self.hardware_digest,
            "kind": self.kind,
            "installed": self.installed,
            "healthy": self.healthy,
            "observations": [item.to_dict() for item in self.observations],
            "restrictions": list(self.restrictions),
            "source_json": self.source_json,
        }


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    records: tuple[CapabilityRecord, ...]
    observed_at: datetime
    policy_digest: str
    authorized_capability_ids: tuple[str, ...] = ()
    source: str = "local"
    source_digest: str = "sha256:" + "0" * 64
    diagnostics: tuple[str, ...] = ()
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise ValueError("unsupported capability snapshot version")
        if type(self.records) is not tuple or len(self.records) > 10_000:
            raise ValueError("capability snapshot must be bounded and immutable")
        ids = [record.descriptor.capability_id for record in self.records]
        if len(set(ids)) != len(ids):
            raise ValueError("capability snapshot identities must be unique")
        object.__setattr__(
            self,
            "records",
            tuple(sorted(self.records, key=lambda item: item.descriptor.capability_id)),
        )
        object.__setattr__(self, "observed_at", _time(self.observed_at))
        _check_digest(self.policy_digest)
        _check_digest(self.source_digest)
        if type(self.authorized_capability_ids) is not tuple:
            raise ValueError("capability authorizations must be immutable")
        if not set(self.authorized_capability_ids).issubset(ids):
            raise ValueError("authorization references unknown capability")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observed_at": self.observed_at.isoformat(),
            "policy_digest": self.policy_digest,
            "source": self.source,
            "source_digest": self.source_digest,
            "diagnostics": list(self.diagnostics),
            "authorized_capability_ids": sorted(self.authorized_capability_ids),
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(frozen=True, slots=True)
class RouteProjection:
    capability_id: str
    outcome: PreviewOutcome
    reason_codes: tuple[str, ...]
    artifact_digest: str | None
    runtime_digest: str | None
    projection: CostProjection | None
    evidence: tuple[CapabilityObservation, ...]
    locality: str
    confidence: str

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "outcome": self.outcome.value,
            "reason_codes": list(self.reason_codes),
            "artifact_digest": self.artifact_digest,
            "runtime_digest": self.runtime_digest,
            "projection": self.projection.to_dict() if self.projection else None,
            "evidence": [{**item.to_dict(), "digest": item.digest} for item in self.evidence],
            "locality": self.locality,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class CapabilityPreview:
    outcome: PreviewOutcome
    selected_capability_id: str | None
    routes: tuple[RouteProjection, ...]
    request_digest: str
    snapshot_digest: str
    observed_at: datetime
    completed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "outcome": self.outcome.value,
            "selected_capability_id": self.selected_capability_id,
            "routes": [route.to_dict() for route in self.routes],
            "request_digest": self.request_digest,
            "snapshot_digest": self.snapshot_digest,
            "observed_at": self.observed_at.isoformat(),
            "completed": False,
            "execution_performed": False,
            "execution_authorized": False,
            "rung": "capability_feasibility",
        }


class CapabilityPreviewService:
    """Evaluate only supplied snapshots. It cannot load, execute, reserve or fetch."""

    def __init__(self, snapshot: CapabilitySnapshot) -> None:
        self.snapshot = snapshot

    def preview(self, requirements: RequestRequirements) -> CapabilityPreview:
        candidates = tuple(self._candidate(record) for record in self.snapshot.records)
        matched = ExactCapabilityMatcher().match(requirements, candidates)
        by_id = {item.descriptor.capability_id: item for item in matched.candidates}
        dependencies = self._dependency_reasons()
        routes = tuple(
            self._project(
                record, by_id[record.descriptor.capability_id], requirements, dependencies
            )
            for record in self.snapshot.records
        )
        routes = tuple(sorted(routes, key=self._rank))
        viable = [
            route
            for route in routes
            if route.outcome
            in {
                PreviewOutcome.AVAILABLE,
                PreviewOutcome.AVAILABLE_BUT_COSTLY,
            }
        ]
        selected = viable[0].capability_id if viable else None
        outcome = routes[0].outcome if routes else PreviewOutcome.UNSUPPORTED
        if len(viable) > 1 and self._rank(viable[0])[:-1] == self._rank(viable[1])[:-1]:
            selected, outcome = None, PreviewOutcome.AMBIGUOUS
        return CapabilityPreview(
            outcome,
            selected,
            routes,
            requirements.digest,
            self.snapshot.digest,
            self.snapshot.observed_at,
        )

    def _evidence(
        self,
        record: CapabilityRecord,
    ) -> tuple[tuple[CapabilityObservation, ...], tuple[str, ...]]:
        if not record.observations:
            return (), ("missing_evidence",)
        comparable = tuple(
            item
            for item in record.observations
            if (
                item.descriptor_digest,
                item.artifact_digest,
                item.runtime_digest,
                item.configuration_digest,
                item.hardware_digest,
            )
            == (
                record.descriptor.digest,
                record.artifact_digest,
                record.runtime_digest,
                record.configuration_digest,
                record.hardware_digest,
            )
        )
        if not comparable:
            return (), ("incomparable_evidence",)
        current = tuple(
            item
            for item in comparable
            if item.observed_at <= self.snapshot.observed_at < item.expires_at and not item.revoked
        )
        superseded = {digest for item in current for digest in item.supersedes}
        current = tuple(item for item in current if item.digest not in superseded)
        if not current:
            return (), ("stale_or_revoked_evidence",)
        if any(item.outcome != "pass" for item in current):
            return current, ("contradictory_or_inconclusive_evidence",)
        return current, ()

    def _candidate(self, record: CapabilityRecord) -> CapabilityCandidate:
        evidence, reasons = self._evidence(record)
        verified = bool(evidence) and not reasons
        projection = CostProjection.combine(tuple(item.projection for item in evidence))
        envelope = projection.upper_envelope() if projection else None
        return CapabilityCandidate(
            descriptor=record.descriptor,
            measured_state=CapabilityLifecycle.BENCHMARKED,
            quality_verified=verified,
            authorized=record.descriptor.capability_id in self.snapshot.authorized_capability_ids,
            measured_quality_evidence=tuple(item.observation_id for item in evidence)
            if verified
            else (),
            estimated_resources=envelope or record.descriptor.resource_envelope,
            reason=CapabilityReasonCode.UNSUPPORTED,
            exact_match=False,
        )

    def _project(
        self,
        record: CapabilityRecord,
        candidate: CapabilityCandidate,
        requirements: RequestRequirements,
        dependencies: Mapping[str, tuple[str, ...]],
    ) -> RouteProjection:
        evidence, reasons = self._evidence(record)
        projection = CostProjection.combine(tuple(item.projection for item in evidence))
        outcome = PreviewOutcome.UNCERTAIN
        if candidate.reason in {
            CapabilityReasonCode.UNSUPPORTED,
            CapabilityReasonCode.PARTIAL_MATCH,
        }:
            outcome, reasons = PreviewOutcome.UNSUPPORTED, (candidate.reason.value,)
        elif record.kind in {"component", "encoder", "tokenizer", "fixture", "runtime", "group"}:
            outcome, reasons = PreviewOutcome.UNSUPPORTED, ("not_independently_callable",)
        elif (
            not candidate.authorized
            or record.restrictions
            or candidate.reason is CapabilityReasonCode.PROHIBITED
        ):
            outcome, reasons = PreviewOutcome.DENIED, ("policy_denied",)
        elif record.descriptor.declared_state is CapabilityLifecycle.REVOKED:
            outcome, reasons = PreviewOutcome.DENIED, ("revoked_capability",)
        elif not record.installed:
            outcome, reasons = PreviewOutcome.CONDITIONAL, ("acquisition_required",)
        elif not record.healthy:
            outcome, reasons = PreviewOutcome.UNAVAILABLE, ("unavailable_health",)
        elif not reasons and not set(requirements.required_verifiers).issubset(
            item.verifier_id for item in evidence
        ):
            reasons = ("missing_verifier_evidence",)
        elif not reasons and (projection is None or projection.upper_envelope() is None):
            reasons = ("unknown_cost",)
        elif not reasons:
            dependency_reasons = dependencies[record.descriptor.capability_id]
            if dependency_reasons:
                reasons = dependency_reasons
            elif candidate.reason is CapabilityReasonCode.EXCESSIVE_COST:
                outcome, reasons = PreviewOutcome.AVAILABLE_BUT_COSTLY, ("budget_exceeded",)
            elif candidate.reason is CapabilityReasonCode.MATCH:
                outcome, reasons = PreviewOutcome.AVAILABLE, ("supported_by_evidence",)
            else:
                reasons = (candidate.reason.value,)
        return RouteProjection(
            record.descriptor.capability_id,
            outcome,
            reasons,
            record.artifact_digest,
            record.runtime_digest,
            projection,
            record.observations,
            record.descriptor.locality.value,
            "measured"
            if outcome in {PreviewOutcome.AVAILABLE, PreviewOutcome.AVAILABLE_BUT_COSTLY}
            else "insufficient",
        )

    def _dependency_reasons(self) -> dict[str, tuple[str, ...]]:
        """Propagate failures once per edge after the matcher validates the graph."""
        records = {item.descriptor.capability_id: item for item in self.snapshot.records}
        authorized = set(self.snapshot.authorized_capability_ids)
        own_reasons: dict[str, tuple[str, ...]] = {}
        for identity, record in records.items():
            if (
                identity not in authorized
                or record.restrictions
                or record.descriptor.declared_state is CapabilityLifecycle.REVOKED
            ):
                own_reasons[identity] = ("dependency_policy_denied",)
            elif (
                not record.installed
                or not record.healthy
                or record.descriptor.declared_state is CapabilityLifecycle.UNAVAILABLE
            ):
                own_reasons[identity] = ("dependency_unavailable",)
            elif self._evidence(record)[1]:
                own_reasons[identity] = ("dependency_insufficient_evidence",)
            else:
                own_reasons[identity] = ()
        graph = {identity: record.descriptor.dependency_ids for identity, record in records.items()}
        inherited: dict[str, tuple[str, ...]] = {}
        for identity in TopologicalSorter(graph).static_order():
            inherited[identity] = ()
            for dependency_id in graph[identity]:
                reasons = own_reasons[dependency_id] or inherited[dependency_id]
                if reasons:
                    inherited[identity] = reasons
                    break
        return inherited

    def _rank(self, route: RouteProjection) -> tuple[int, float, float, str]:
        outcomes = list(PreviewOutcome)
        cost = route.projection.estimated_cost_micros if route.projection else None
        latency = route.projection.latency_ms if route.projection else None
        return (
            outcomes.index(route.outcome),
            cost.upper if cost else float("inf"),
            latency.upper if latency else float("inf"),
            route.capability_id,
        )


def import_capability_catalogue(
    payload: Mapping[str, object],
    *,
    source: str,
    observed_at: datetime,
) -> CapabilitySnapshot:
    """Import the AF 1.0 envelope with per-record diagnostics and raw provenance.

    The caller must explicitly map an upstream format into this envelope. Upstream
    operational, permission and quality claims are preserved as provenance only.
    """
    if payload.get("schema_version") != "1.0.0" or not isinstance(payload.get("records"), list):
        raise ValueError("catalogue requires schema_version 1.0.0 and records")
    raw_records = payload["records"]
    if not isinstance(raw_records, list) or len(raw_records) > 10_000:
        raise ValueError("catalogue records must be bounded")
    if len(_json(payload).encode()) > 16 * 1_048_576:
        raise ValueError("catalogue payload is oversized")
    records: list[CapabilityRecord] = []
    diagnostics: list[str] = []
    for index, raw in enumerate(raw_records):
        try:
            if not isinstance(raw, dict):
                raise ValueError("record must be an object")
            descriptor = CapabilityDescriptor.from_dict(raw.get("descriptor"))
            kind = raw.get("kind", "model")
            restrictions = raw.get("restrictions", [])
            if (
                not isinstance(kind, str)
                or not isinstance(restrictions, list)
                or any(not isinstance(item, str) for item in restrictions)
            ):
                raise ValueError("invalid asset kind or restrictions")
            records.append(
                CapabilityRecord(
                    descriptor=descriptor,
                    kind=kind,
                    restrictions=tuple(restrictions),
                    source_json=_json(raw),
                )
            )
        except (TypeError, ValueError):
            diagnostics.append(f"record.{index}.invalid")
    return CapabilitySnapshot(
        records=tuple(records),
        observed_at=observed_at,
        policy_digest=_digest({"authorized": []}),
        source=source,
        source_digest=_digest(payload),
        diagnostics=tuple(diagnostics),
    )
