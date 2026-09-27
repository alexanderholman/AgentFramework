"""Versioned, non-configurable AF baseline and restricted decision inputs.

This is a finite policy boundary, not a sandbox for malicious installed Python.
Personal information may remain task data; it is not execution authority or a
resource-ranking signal. Consent and peer leases remain separate exact grants.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace

from af.framework.context import ExecutionContext, maybe_current_context, use_context
from af.framework.types import (
    ROUTE_METRIC_MAX,
    ProcedureDescriptor,
    ProcedureEffect,
    ProcedureRisk,
    WorkRequest,
    is_valid_procedure_id,
)


def _digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )


@dataclass(frozen=True, slots=True)
class AFConstitution:
    version: str = field(default="1.0.0", init=False)
    precedence: tuple[str, ...] = field(
        default=(
            "constitution",
            "stricter_host_policy",
            "human_consent_and_peer_lease",
            "configuration",
        ),
        init=False,
    )
    principles: tuple[str, ...] = field(
        default=(
            "accessibility",
            "inclusion",
            "diversity",
            "equity_and_equality",
            "privacy",
            "consent",
            "human_agency",
            "non_discrimination",
            "fail_closed",
        ),
        init=False,
    )
    excluded_decision_attributes: tuple[str, ...] = field(
        default=(
            "race",
            "ethnicity",
            "national_origin",
            "sex",
            "gender",
            "gender_identity",
            "sexual_orientation",
            "religion",
            "disability",
            "age",
            "pregnancy",
            "marital_status",
            "genetic_information",
            "presentation_language",
            "territory",
        ),
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


CONSTITUTION = AFConstitution()


@dataclass(frozen=True, slots=True)
class HostRestrictions:
    """Host policy can remove procedures or lower budgets; no allow override exists."""

    denied_procedures: tuple[str, ...] = ()
    maximum_cost: int = ROUTE_METRIC_MAX
    maximum_latency: int = ROUTE_METRIC_MAX

    def __post_init__(self) -> None:
        if (
            type(self.denied_procedures) is not tuple
            or len(self.denied_procedures) > 256
            or any(not is_valid_procedure_id(item) for item in self.denied_procedures)
        ):
            raise ValueError("host procedure restrictions must be bounded identities")
        object.__setattr__(self, "denied_procedures", tuple(sorted(set(self.denied_procedures))))
        if any(
            type(value) is not int or not 0 <= value <= ROUTE_METRIC_MAX
            for value in (self.maximum_cost, self.maximum_latency)
        ):
            raise ValueError("host resource restrictions must narrow the baseline bounds")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))

    def permits(self, descriptor: ProcedureDescriptor) -> bool:
        return (
            descriptor.procedure_id not in self.denied_procedures
            and descriptor.metrics.cost <= self.maximum_cost
            and descriptor.metrics.latency <= self.maximum_latency
        )


@dataclass(frozen=True, slots=True)
class ConstitutionalDecision:
    allowed: bool
    reason: str
    constitution_digest: str = field(default=CONSTITUTION.digest, init=False)


def procedure_decision(descriptor: ProcedureDescriptor) -> ConstitutionalDecision:
    """The ordinary task kernel has no consequential-action grant consumer.

    It therefore cannot accept confirmation strings, provider assertions, or
    caller metadata as approval. Dedicated consent/lease services own those paths.
    Non-pure observations such as current time remain valid read-only operations.
    """
    if (
        descriptor.effect is not ProcedureEffect.READ_ONLY
        or descriptor.risk is not ProcedureRisk.NONE
        or descriptor.requires_confirmation
    ):
        return ConstitutionalDecision(False, "independent_approval_required")
    return ConstitutionalDecision(True, "ordinary_read_only_operation")


def decision_context(context: ExecutionContext) -> ExecutionContext:
    """Retain explicit authorization facts; remove untyped claims and presentation.

    Subject and tenant are opaque authorization boundaries, not demographic facts.
    An explicit child scope is a safety restriction and is preserved; age is never
    inferred from claims. Roles/scopes must be issued through trusted authority.
    """
    principal = replace(context.principal, claims=()) if context.principal is not None else None
    return replace(context, principal=principal, locale=None)


@contextmanager
def decision_scope(context: ExecutionContext | None = None) -> Iterator[ExecutionContext | None]:
    source = context if context is not None else maybe_current_context()
    if source is None:
        yield None
    else:
        selected = decision_context(source)
        with use_context(selected):
            yield selected


def classification_input(request: WorkRequest) -> WorkRequest:
    """Metadata is not a classification feature; the supplied task remains data."""
    return WorkRequest(prompt=request.prompt)


@dataclass(frozen=True, slots=True)
class PresentationPreferences:
    """User-requested accommodations carry no routing or authorization authority."""

    plain_text: bool = False
    reduced_motion: bool = False
    high_contrast: bool = False
    large_text: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in asdict(self).values()):
            raise ValueError("presentation preferences must be explicit booleans")
