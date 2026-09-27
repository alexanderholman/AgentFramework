"""Bounded classifier traversal; proposals still execute through the ordinary pipeline.

Nodes select only their declared children or version-bound procedure catalogue.
Parents own sibling recovery. Only a root NoRoute permits the next configured rung.
Synchronous callbacks cooperate with deadlines; late results are never executable.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from math import isfinite
from time import monotonic
from typing import Protocol, runtime_checkable

from af.framework.constitution import decision_scope
from af.framework.context import AcceptanceScope, ExecutionContext, maybe_current_context
from af.framework.types import (
    PROMPT_MAX_LENGTH,
    ProcedureDescriptor,
    SelectionProposal,
    SelectorLocality,
    SelectorMetadata,
    TraceEvent,
    WorkRequest,
    is_valid_classifier_id,
    is_valid_procedure_version,
    parse_probability,
)

_FAILURES = frozenset(
    {
        "unavailable",
        "malformed",
        "stale_catalogue",
        "budget_exhausted",
        "cancelled",
        "timeout",
        "classifier_failure",
        "clock_invalid",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class FunctionRoute:
    proposal: SelectionProposal

    def __repr__(self) -> str:
        return "FunctionRoute(proposal='[REDACTED]')"


@dataclass(frozen=True, slots=True)
class ChildRoute:
    child_id: str


@dataclass(frozen=True, slots=True)
class NoRoute:
    """This branch is exhausted without executing a function."""


@dataclass(frozen=True, slots=True)
class NeedsClarification:
    """Ambiguity or low confidence is terminal for this route attempt."""


@dataclass(frozen=True, slots=True)
class Denied:
    """A denial cannot be bypassed by sibling search or escalation."""


@dataclass(frozen=True, slots=True)
class ClassifierFailure:
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in _FAILURES:
            raise ValueError("classifier failure requires a finite reason code")


type RouteDecision = (
    FunctionRoute | ChildRoute | NoRoute | NeedsClarification | Denied | ClassifierFailure
)


@dataclass(frozen=True, slots=True)
class RouteBudget:
    max_nodes: int = 128
    max_calls: int = 128
    max_depth: int = 16
    max_rungs: int = 4
    max_nodes_per_rung: int = 128
    max_cost: int = 1_000_000
    timeout_ms: int = 1_000

    def __post_init__(self) -> None:
        for name in ("max_nodes", "max_calls", "max_nodes_per_rung"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 1_024:
                raise ValueError("classifier node/call budgets must be within 1..1024")
        for name, limit in (("max_depth", 64), ("max_rungs", 8), ("timeout_ms", 60_000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError("classifier traversal budget is invalid")
        if type(self.max_cost) is not int or not 0 <= self.max_cost <= 10**12:
            raise ValueError("classifier cost budget is invalid")


@dataclass(frozen=True, slots=True)
class RouteVisit:
    node_id: str
    version: str
    digest: str
    rung: int
    outcome: str
    reason: str
    elapsed_ms: float
    confidence: float | None = None
    procedure_id: str | None = None
    procedure_version: str | None = None
    procedure_digest: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class NodeInput:
    prompt: str
    rung: int
    scope: AcceptanceScope | None
    history: tuple[RouteVisit, ...]
    remaining_nodes: int
    remaining_calls: int
    remaining_cost: int
    remaining_ms: float


@dataclass(frozen=True, slots=True)
class ClassifierNode:
    node_id: str
    classify: Callable[[NodeInput], object] = field(repr=False, compare=False)
    children: tuple[str, ...] = ()
    procedures: tuple[ProcedureDescriptor, ...] = ()
    version: str = "1.0.0"
    cost: int = 0
    selector_metadata: SelectorMetadata | None = None

    def __post_init__(self) -> None:
        if self.selector_metadata is not None and (
            type(self.selector_metadata) is not SelectorMetadata
            or not is_valid_classifier_id(self.selector_metadata.provider_id)
            or type(self.selector_metadata.locality) is not SelectorLocality
        ):
            raise ValueError("classifier selector metadata is invalid")
        if not is_valid_classifier_id(self.node_id) or not is_valid_procedure_version(self.version):
            raise ValueError("classifier node identity is invalid")
        if (
            not callable(self.classify)
            or inspect.iscoroutinefunction(self.classify)
            or inspect.isasyncgenfunction(self.classify)
        ):
            raise TypeError("classifier nodes require synchronous bounded callbacks")
        if (
            type(self.children) is not tuple
            or len(self.children) > 64
            or any(not is_valid_classifier_id(child) for child in self.children)
        ):
            raise ValueError("classifier children must be bounded canonical identities")
        if (
            type(self.procedures) is not tuple
            or len(self.procedures) > 256
            or any(type(p) is not ProcedureDescriptor for p in self.procedures)
        ):
            raise ValueError("classifier procedure catalogue is invalid")
        if len({p.procedure_id for p in self.procedures}) != len(self.procedures):
            raise ValueError("classifier procedure identities must be unique")
        if type(self.cost) is not int or not 0 <= self.cost <= 10**9:
            raise ValueError("classifier declared cost is invalid")

    @property
    def digest(self) -> str:
        """Digest of the declared node/version/catalogue, not a model attestation."""
        payload = [
            self.node_id,
            self.version,
            self.children,
            [p.digest for p in self.procedures],
            self.cost,
            None if self.selector_metadata is None else asdict(self.selector_metadata),
        ]
        return (
            "sha256:"
            + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()
        )


@dataclass(frozen=True, slots=True)
class HierarchyResult:
    outcome: RouteDecision
    visits: tuple[RouteVisit, ...]
    escalations: tuple[tuple[int, int], ...]
    events: tuple[TraceEvent, ...]
    selector_metadata: SelectorMetadata | None = None


@runtime_checkable
class TracedClassifier(Protocol):
    def classify_with_trace(self, request: WorkRequest) -> HierarchyResult | None: ...


class HierarchyRouter:
    def __init__(
        self,
        nodes: tuple[ClassifierNode, ...],
        *,
        roots: tuple[str, ...],
        budget: RouteBudget | None = None,
        confidence_threshold: float = 0.8,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(nodes) is not tuple
            or not 1 <= len(nodes) <= 256
            or any(type(n) is not ClassifierNode for n in nodes)
        ):
            raise ValueError("classifier topology requires 1..256 typed nodes")
        if type(roots) is not tuple or not 1 <= len(roots) <= 8 or len(set(roots)) != len(roots):
            raise ValueError("classifier roots must be distinct and bounded")
        by_id = {node.node_id: node for node in nodes}
        if len(by_id) != len(nodes) or any(root not in by_id for root in roots):
            raise ValueError("classifier topology identities are invalid")
        parents: dict[str, str] = {}
        for node in nodes:
            for child in node.children:
                if child not in by_id or child in parents or child in roots:
                    raise ValueError("classifier children require one declared parent")
                parents[child] = node.node_id
        seen: set[str] = set()
        pending = list(roots)
        while pending:
            identity = pending.pop()
            if identity in seen:
                raise ValueError("classifier topology contains a cycle")
            seen.add(identity)
            pending.extend(by_id[identity].children)
        if len(seen) != len(nodes):
            raise ValueError("classifier topology contains unreachable nodes or a cycle")
        budget = RouteBudget() if budget is None else budget
        threshold = parse_probability(confidence_threshold)
        if (
            threshold is None
            or type(budget) is not RouteBudget
            or not callable(clock)
            or not callable(wall_clock)
        ):
            raise ValueError("classifier routing policy is invalid")
        self._nodes, self._roots = by_id, roots
        self._budget, self._threshold = budget, threshold
        self._clock, self._wall_clock = clock, wall_clock

    def describe(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "roots": list(self._roots),
            "budget": asdict(self._budget),
            "confidence_threshold": self._threshold,
            "nodes": [
                {
                    "id": n.node_id,
                    "version": n.version,
                    "digest": n.digest,
                    "children": list(n.children),
                    "cost": n.cost,
                    "selector": None
                    if n.selector_metadata is None
                    else asdict(n.selector_metadata),
                    "procedures": [
                        {"id": p.procedure_id, "version": p.version, "digest": p.digest}
                        for p in n.procedures
                    ],
                }
                for n in self._nodes.values()
            ],
        }

    def route(
        self, request: WorkRequest, *, context: ExecutionContext | None = None
    ) -> HierarchyResult:
        if (
            type(request) is not WorkRequest
            or type(request.prompt) is not str
            or not request.prompt.strip()
            or len(request.prompt) > PROMPT_MAX_LENGTH
        ):
            raise ValueError("classifier request is invalid")
        if context is not None and type(context) is not ExecutionContext:
            raise TypeError("classifier context is invalid")
        state = _Traversal(self, request.prompt, context or maybe_current_context())
        return state.run()

    def _function(self, node: ClassifierNode, decision: FunctionRoute) -> RouteDecision:
        proposal = decision.proposal
        if type(proposal) is not SelectionProposal:
            return ClassifierFailure("malformed")
        descriptor = next(
            (p for p in node.procedures if p.procedure_id == proposal.procedure_id), None
        )
        if descriptor is None or proposal.intent != descriptor.intent:
            return ClassifierFailure("malformed")
        if proposal.version != descriptor.version or proposal.digest != descriptor.digest:
            return ClassifierFailure("stale_catalogue")
        confidence = parse_probability(proposal.confidence)
        if confidence is None or type(proposal.arguments) is not dict:
            return ClassifierFailure("malformed")
        if set(proposal.arguments) != {a.name for a in descriptor.arguments}:
            return ClassifierFailure("malformed")
        for argument in descriptor.arguments:
            value = proposal.arguments[argument.name]
            if (
                type(value) is not str
                or len(value) < argument.min_length
                or len(value)
                > (argument.max_length if argument.max_length is not None else PROMPT_MAX_LENGTH)
                or (not argument.allow_blank and not value.strip())
            ):
                return ClassifierFailure("malformed")
        if confidence < self._threshold:
            return NeedsClarification()
        return FunctionRoute(
            SelectionProposal(
                proposal.procedure_id,
                proposal.intent,
                dict(proposal.arguments),
                confidence,
                version=proposal.version,
                digest=proposal.digest,
            )
        )


class _Traversal:
    """Per-invocation state; no request data or visited sets live on shared routers."""

    def __init__(
        self, router: HierarchyRouter, prompt: str, context: ExecutionContext | None
    ) -> None:
        self.router, self.prompt, self.context = router, prompt, context
        self.visits: list[RouteVisit] = []
        self.events: list[TraceEvent] = []
        self.escalations: list[tuple[int, int]] = []
        self.visited: set[str] = set()
        self.cost, self.rung_nodes = 0, 0
        self.selector_metadata: SelectorMetadata | None = None
        self.started: float | None = None
        self.latest = 0.0

    def _stop(self) -> ClassifierFailure | None:
        if self.context is not None and self.context.cancellation.cancelled:
            return ClassifierFailure("cancelled")
        try:
            now = self.router._clock()
            if (
                type(now) not in (int, float)
                or not isfinite(now)
                or (self.started is not None and now < self.latest)
            ):
                return ClassifierFailure("clock_invalid")
            self.latest = now
            if self.started is None:
                self.started = now
            if (now - self.started) * 1000 >= self.router._budget.timeout_ms:
                return ClassifierFailure("timeout")
            if self.context is not None and self.context.deadline is not None:
                wall = self.router._wall_clock()
                if not isinstance(wall, datetime) or wall.utcoffset() is None:
                    return ClassifierFailure("clock_invalid")
                if wall >= self.context.deadline:
                    return ClassifierFailure("timeout")
        except Exception:
            return ClassifierFailure("clock_invalid")
        return None

    def run(self) -> HierarchyResult:
        outcome: RouteDecision = NoRoute()
        for rung, root in enumerate(self.router._roots):
            stop = self._stop()
            if stop is not None:
                outcome = stop
                break
            if rung >= self.router._budget.max_rungs:
                outcome = ClassifierFailure("budget_exhausted")
                break
            if rung:
                self.escalations.append((rung - 1, rung))
                self.events.append(
                    TraceEvent("classify-rung", f"from={rung - 1} to={rung} reason=no_route")
                )
            self.rung_nodes = 0
            outcome = self._visit(root, rung, depth=1)
            if not isinstance(outcome, NoRoute):
                break
        if isinstance(outcome, ClassifierFailure):
            self.events.append(TraceEvent("classify-stop", outcome.reason))
        return HierarchyResult(
            outcome,
            tuple(self.visits),
            tuple(self.escalations),
            tuple(self.events),
            self.selector_metadata,
        )

    def _visit(self, identity: str, rung: int, *, depth: int) -> RouteDecision:
        stop = self._stop()
        if stop is not None:
            return stop
        node = self.router._nodes[identity]
        budget = self.router._budget
        if identity in self.visited:
            return NoRoute()
        if (
            len(self.visited) >= budget.max_nodes
            or len(self.visits) >= budget.max_calls
            or depth > budget.max_depth
            or self.rung_nodes >= budget.max_nodes_per_rung
            or self.cost + node.cost > budget.max_cost
        ):
            return ClassifierFailure("budget_exhausted")
        self.visited.add(identity)
        self.rung_nodes += 1
        self.cost += node.cost
        before = self.latest
        node_input = NodeInput(
            self.prompt,
            rung,
            None if self.context is None else self.context.effective_scope,
            tuple(self.visits),
            budget.max_nodes - len(self.visited),
            budget.max_calls - len(self.visits) - 1,
            budget.max_cost - self.cost,
            budget.timeout_ms - (self.latest - (self.started or 0.0)) * 1000,
        )
        try:
            if node.selector_metadata is not None:
                self.selector_metadata = node.selector_metadata
            if (
                node.selector_metadata is not None
                and node.selector_metadata.locality is SelectorLocality.REMOTE
            ):
                raw: object = Denied()
            else:
                with decision_scope(self.context):
                    raw = node.classify(node_input)
        except Exception:
            raw = ClassifierFailure("classifier_failure")
        if inspect.iscoroutine(raw):
            raw.close()
            raw = ClassifierFailure("malformed")
        if type(raw) not in (
            ChildRoute,
            FunctionRoute,
            NoRoute,
            NeedsClarification,
            Denied,
            ClassifierFailure,
        ):
            decision: RouteDecision = ClassifierFailure("malformed")
        elif isinstance(raw, FunctionRoute):
            decision = self.router._function(node, raw)
        elif isinstance(raw, ChildRoute):
            decision = raw if raw.child_id in node.children else ClassifierFailure("malformed")
        else:
            # Exact runtime variants above exclude arbitrary callbacks and subclass behavior.
            assert isinstance(raw, (NoRoute, NeedsClarification, Denied, ClassifierFailure))
            decision = raw
        stop = self._stop()
        if stop is not None:
            decision = stop
        reason = (
            decision.reason if isinstance(decision, ClassifierFailure) else type(decision).__name__
        )
        confidence = decision.proposal.confidence if isinstance(decision, FunctionRoute) else None
        visit = RouteVisit(
            identity,
            node.version,
            node.digest,
            rung,
            type(decision).__name__,
            reason,
            round(max(0.0, self.latest - before) * 1000, 3),
            confidence,
            decision.proposal.procedure_id if isinstance(decision, FunctionRoute) else None,
            decision.proposal.version if isinstance(decision, FunctionRoute) else None,
            decision.proposal.digest if isinstance(decision, FunctionRoute) else None,
        )
        self.visits.append(visit)
        self.events.append(TraceEvent("classify-node", json.dumps(asdict(visit), sort_keys=True)))
        if not isinstance(decision, ChildRoute):
            return decision
        siblings = (
            decision.child_id,
            *(child for child in node.children if child != decision.child_id),
        )
        for child in siblings:
            outcome = self._visit(child, rung, depth=depth + 1)
            if not isinstance(outcome, NoRoute):
                return outcome
        self.events.append(
            TraceEvent("classify-parent", f"node={identity} rung={rung} reason=no_route")
        )
        return NoRoute()
