"""Jacob's-ladder routing with an explicit cheap/fast/smart trade-off."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from af.framework.contracts import Procedure
from af.framework.errors import InvalidRoutingPolicy, UnsupportedWork
from af.framework.types import (
    ARGUMENT_NAME_MAX_LENGTH,
    PROCEDURE_ARGUMENT_COUNT_MAX,
    PROMPT_MAX_LENGTH,
    ROUTE_METRIC_MAX,
    ArgumentValueType,
    ChildRoute,
    ClassifierFailure,
    ClassifierNode,
    ClassifierTree,
    Classification,
    Denied,
    FunctionRoute,
    NeedsClarification,
    NoRoute,
    ProcedureArgumentDescriptor,
    ProcedureDescriptor,
    ProcedureEffect,
    ProcedureRisk,
    RouteBudget,
    RouteMetrics,
    RoutingResult,
    RoutingTrace,
    RungEscalation,
    VisitedNode,
    WorkTier,
    is_valid_argument_name,
    is_valid_procedure_id,
    is_valid_procedure_intent,
    is_valid_procedure_version,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class OptimisationGoal(StrEnum):
    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    SMARTEST = "smartest"


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    goals: tuple[OptimisationGoal, ...] = (
        OptimisationGoal.CHEAPEST,
        OptimisationGoal.FASTEST,
    )

    def __post_init__(self) -> None:
        if type(self.goals) is not tuple:
            raise InvalidRoutingPolicy("choose one or two distinct routing goals")
        if any(type(goal) is not OptimisationGoal for goal in self.goals):
            raise InvalidRoutingPolicy("choose one or two distinct routing goals")
        if not 1 <= len(self.goals) <= 2 or len(set(self.goals)) != len(self.goals):
            raise InvalidRoutingPolicy("choose one or two distinct routing goals")


class ProcedureRegistry:
    def __init__(self) -> None:
        self._procedures: dict[str, Procedure] = {}
        self._descriptors_by_identity: dict[tuple[str, str], ProcedureDescriptor] = {}
        self._descriptors: dict[str, ProcedureDescriptor] = {}

    def register(self, procedure: Procedure) -> None:
        descriptor = self._validated_descriptor(procedure)
        identity = (descriptor.procedure_id, descriptor.version)
        if identity in self._descriptors_by_identity:
            raise ValueError(
                f"procedure already registered: {descriptor.procedure_id}@{descriptor.version}"
            )
        if descriptor.procedure_id in self._procedures:
            raise ValueError(
                f"procedure id conflicts with existing registration: {descriptor.procedure_id}"
            )
        self._procedures[procedure.id] = procedure
        self._descriptors_by_identity[identity] = descriptor
        self._descriptors[procedure.id] = descriptor

    def all(self) -> tuple[Procedure, ...]:
        return tuple(self._procedures.values())

    def catalogue(self) -> tuple[ProcedureDescriptor, ...]:
        return tuple(self._descriptors.values())

    def get(self, procedure_id: str) -> Procedure | None:
        return self._procedures.get(procedure_id)

    def descriptor(self, procedure_id: str) -> ProcedureDescriptor | None:
        return self._descriptors.get(procedure_id)

    @staticmethod
    def _validated_descriptor(procedure: Procedure) -> ProcedureDescriptor:
        if not is_valid_procedure_id(procedure.id):
            raise ValueError(f"invalid procedure id: {procedure.id!r}")
        if not is_valid_procedure_version(procedure.version):
            raise ValueError(f"invalid procedure version: {procedure.version!r}")
        if type(procedure.tier) is not WorkTier:
            raise ValueError("procedure has an invalid execution tier")
        if type(procedure.pure) is not bool:
            raise ValueError("procedure has an invalid purity policy")
        if type(procedure.replay_safe) is not bool:
            raise ValueError("procedure has an invalid replay safety policy")
        if type(procedure.has_selection_grounding) is not bool:
            raise ValueError("procedure has an invalid grounding policy")
        metrics = procedure.metrics
        if not isinstance(metrics, RouteMetrics):
            raise ValueError("procedure has invalid route metrics")
        if any(
            type(value) is not int or not 0 <= value <= ROUTE_METRIC_MAX
            for value in (metrics.cost, metrics.latency, metrics.intelligence)
        ):
            raise ValueError(
                "procedure has invalid route metrics: values must be integers between "
                f"0 and {ROUTE_METRIC_MAX}"
            )
        descriptor = procedure.selection_descriptor
        if not isinstance(descriptor, ProcedureDescriptor):
            raise ValueError("procedure selection descriptor is invalid")
        if descriptor.procedure_id != procedure.id:
            raise ValueError(
                "procedure selection descriptor must match the registered procedure id"
            )
        if descriptor.version != procedure.version:
            raise ValueError(
                "procedure selection descriptor must match the registered procedure version"
            )
        if not is_valid_procedure_id(descriptor.procedure_id):
            raise ValueError("procedure selection descriptor has an invalid id")
        if not is_valid_procedure_version(descriptor.version):
            raise ValueError("procedure selection descriptor has an invalid version")
        if not is_valid_procedure_intent(descriptor.intent):
            raise ValueError("procedure selection descriptor has an invalid intent")
        if not isinstance(descriptor.effect, ProcedureEffect):
            raise ValueError("procedure selection descriptor has an invalid effect")
        if not isinstance(descriptor.risk, ProcedureRisk):
            raise ValueError("procedure selection descriptor has an invalid risk")
        if type(descriptor.requires_confirmation) is not bool:
            raise ValueError("procedure selection descriptor has an invalid confirmation policy")
        if type(descriptor.arguments) is not tuple:
            raise ValueError("procedure selection descriptor arguments must be a tuple")
        if len(descriptor.arguments) > PROCEDURE_ARGUMENT_COUNT_MAX:
            raise ValueError("procedure selection descriptor has too many argument descriptors")

        seen_names: set[str] = set()
        arguments: list[ProcedureArgumentDescriptor] = []
        for argument in descriptor.arguments:
            if not isinstance(argument, ProcedureArgumentDescriptor):
                raise ValueError(
                    "procedure selection descriptor has an invalid argument descriptor"
                )
            if argument.name in seen_names:
                raise ValueError("procedure selection descriptor argument names must be unique")
            if not is_valid_argument_name(argument.name):
                raise ValueError(
                    "procedure selection descriptor argument names must match the "
                    f"{ARGUMENT_NAME_MAX_LENGTH}-character schema"
                )
            if not isinstance(argument.value_type, ArgumentValueType):
                raise ValueError("procedure selection descriptor has an invalid argument type")
            if (
                type(argument.min_length) is not int
                or not 0 <= argument.min_length <= PROMPT_MAX_LENGTH
            ):
                raise ValueError("procedure selection descriptor has an invalid minimum length")
            if argument.max_length is not None and (
                type(argument.max_length) is not int
                or argument.max_length < argument.min_length
                or argument.max_length > PROMPT_MAX_LENGTH
            ):
                raise ValueError("procedure selection descriptor has an invalid maximum length")
            if type(argument.allow_blank) is not bool:
                raise ValueError("procedure selection descriptor has an invalid blank policy")
            seen_names.add(argument.name)
            arguments.append(
                ProcedureArgumentDescriptor(
                    name=argument.name,
                    value_type=argument.value_type,
                    min_length=argument.min_length,
                    max_length=argument.max_length,
                    allow_blank=argument.allow_blank,
                )
            )

        return ProcedureDescriptor(
            procedure_id=descriptor.procedure_id,
            version=descriptor.version,
            intent=descriptor.intent,
            arguments=tuple(arguments),
            effect=descriptor.effect,
            risk=descriptor.risk,
            requires_confirmation=descriptor.requires_confirmation,
            tier=procedure.tier,
            pure=procedure.pure,
            replay_safe=procedure.replay_safe,
            has_selection_grounding=procedure.has_selection_grounding,
            metrics=RouteMetrics(
                cost=metrics.cost,
                latency=metrics.latency,
                intelligence=metrics.intelligence,
            ),
        )


class LadderRouter:
    def __init__(self, registry: ProcedureRegistry, policy: RoutingPolicy) -> None:
        self._registry = registry
        self._policy = policy

    def route(self, classification: Classification, *, prompt: str) -> Procedure:
        candidates = [
            procedure for procedure in self._registry.all() if procedure.supports(classification)
        ]
        if not candidates:
            raise UnsupportedWork(prompt)
        return min(candidates, key=self._score)

    def _score(self, procedure: Procedure) -> tuple[int, ...]:
        descriptor = self._registry.descriptor(procedure.id)
        if descriptor is None:
            raise UnsupportedWork(procedure.id)
        score = [int(descriptor.tier)]
        for goal in self._policy.goals:
            if goal is OptimisationGoal.CHEAPEST:
                score.append(descriptor.metrics.cost)
            elif goal is OptimisationGoal.FASTEST:
                score.append(descriptor.metrics.latency)
            elif goal is OptimisationGoal.SMARTEST:
                score.append(-descriptor.metrics.intelligence)
            else:
                raise InvalidRoutingPolicy("choose one or two distinct routing goals")
        return tuple(score)


# -- Hierarchy routing --


class _Match:
    """Internal helper representing a single classifier match result."""

    __slots__ = ("node", "match", "procedure_id", "child_index", "reason")

    def __init__(
        self,
        *,
        node: ClassifierNode,
        match: bool,
        procedure_id: str | None = None,
        child_index: int | None = None,
        reason: str = "",
    ) -> None:
        self.node = node
        self.match = match
        self.procedure_id = procedure_id
        self.child_index = child_index
        self.reason = reason


class HierarchyRouter:
    """Deterministic tree traversal with sibling search and rung escalation."""

    def __init__(self, tree: ClassifierTree) -> None:
        self._tree = tree

    def route(self, prompt: str, *, budget: RouteBudget | None = None) -> RoutingResult:
        budget = budget or RouteBudget()
        visited: set[str] = set()
        return self._traverse(
            self._tree.root,
            prompt,
            rung=0,
            visited=visited,
            nodes_visited=[],
            all_visited=[],
            escalations=[],
            budget=budget,
        )

    def _traverse(
        self,
        node: ClassifierNode,
        prompt: str,
        *,
        rung: int,
        visited: set[str],
        nodes_visited: list[VisitedNode],
        all_visited: list[VisitedNode],
        escalations: list[RungEscalation],
        budget: RouteBudget,
    ) -> RoutingResult:
        # Budget / cycle checks
        if node.id in visited:
            return ClassifierFailure(
                classifier_id=node.id,
                reason="cycle_detected",
                trace=RoutingTrace(
                    nodes_visited=tuple(nodes_visited),
                    rung_escalations=tuple(escalations),
                    reason_code="cycle_detected",
                ),
            )
        if len(nodes_visited) >= budget.max_nodes:
            return ClassifierFailure(
                classifier_id=node.id,
                reason="budget_exhausted",
                trace=RoutingTrace(
                    nodes_visited=tuple(nodes_visited),
                    rung_escalations=tuple(escalations),
                    reason_code="budget_exhausted",
                ),
            )
        if rung > budget.max_rungs:
            return ClassifierFailure(
                classifier_id=node.id,
                reason="rung_exhausted",
                trace=RoutingTrace(
                    nodes_visited=tuple(nodes_visited),
                    rung_escalations=tuple(escalations),
                    reason_code="rung_exhausted",
                ),
            )

        visited_copy = visited | {node.id}
        ref = VisitedNode(id=node.id)
        nodes_visited.append(ref)
        all_visited.append(ref)

        match = self._match(node, prompt)

        if not match.match:
            # Wrong child - pop from current path, use all_visited for trace
            nodes_visited.pop()
            return NoRoute(
                classifier_id=node.id,
                reason=match.reason or "no_match",
                trace=RoutingTrace(
                    nodes_visited=tuple(all_visited),
                    rung_escalations=tuple(escalations),
                    reason_code="no_match",
                ),
            )

        # Exact leaf match → procedure
        if match.procedure_id is not None:
            return FunctionRoute(
                procedure_id=match.procedure_id,
                classifier_id=node.id,
                trace=RoutingTrace(
                    nodes_visited=tuple(nodes_visited),
                    rung_escalations=tuple(escalations),
                    reason_code="function_route",
                ),
            )

        # Has children – try them in order (sibling search)
        if node.children:
            for child in node.children:
                child_result = self._traverse(
                    child,
                    prompt,
                    rung=rung + 1,
                    visited=visited_copy,
                    nodes_visited=nodes_visited,
                    all_visited=all_visited,
                    escalations=escalations,
                    budget=budget,
                )
                # If child returned a concrete route, bubble it up
                if isinstance(child_result, (FunctionRoute, Denied, NeedsClarification)):
                    return child_result
                # If child returned NoRoute, try next sibling
                if isinstance(child_result, NoRoute):
                    continue
                # ClassifierFailure from child – accumulate, keep trying siblings
                if isinstance(child_result, ClassifierFailure):
                    continue

            # All children exhausted – escalate
            new_escalation = RungEscalation(
                from_rung=rung,
                to_rung=rung + 1,
                reason="all_children_exhausted",
            )
            escalations.append(new_escalation)
            nodes_visited.pop()
            return NoRoute(
                classifier_id=node.id,
                reason="all_children_exhausted",
                trace=RoutingTrace(
                    nodes_visited=tuple(all_visited),
                    rung_escalations=tuple(escalations),
                    reason_code="rung_exhaustion",
                ),
            )

        # Leaf with no procedure and no children
        nodes_visited.pop()
        return NoRoute(
            classifier_id=node.id,
            reason="leaf_no_procedure",
            trace=RoutingTrace(
                nodes_visited=tuple(all_visited),
                rung_escalations=tuple(escalations),
                reason_code="leaf_no_procedure",
            ),
        )

    @staticmethod
    def _match(node: ClassifierNode, prompt: str) -> _Match:
        """Determine whether a node matches the prompt."""
        intent = node.metadata.get("intent", "")

        # If node has a procedure_id, check intent match
        if node.procedure_id:
            if intent and _prompt_matches_intent(prompt, intent):
                return _Match(
                    node=node,
                    match=True,
                    procedure_id=node.procedure_id,
                )
            # No intent metadata – check if procedure_id keywords appear in prompt
            if _prompt_matches_keyword(prompt, node.procedure_id):
                return _Match(
                    node=node,
                    match=True,
                    procedure_id=node.procedure_id,
                )
            return _Match(node=node, match=False, reason="intent_mismatch")

        # Internal node – check if metadata intent matches
        if intent and _prompt_matches_intent(prompt, intent):
            return _Match(node=node, match=True)

        # Default: match internal nodes that have children for further traversal
        if node.children:
            return _Match(node=node, match=True)

        return _Match(node=node, match=False, reason="no_match")


def _prompt_matches_intent(prompt: str, intent: str) -> bool:
    """Check if prompt text contains or matches the intent string."""
    normalized = prompt.strip().lower()
    return intent.lower() in normalized or normalized in intent.lower()


def _prompt_matches_keyword(prompt: str, keyword: str) -> bool:
    """Check if prompt contains keyword tokens (underscore-separated)."""
    normalized = prompt.strip().lower()
    tokens = keyword.lower().split("_")
    return any(token in normalized for token in tokens if len(token) > 2)


class JacobLadderRouter:
    """High-level router combining hierarchy tree with procedure registry."""

    def __init__(self, tree: ClassifierTree) -> None:
        self._tree = tree
        self._hierarchy = HierarchyRouter(tree)

    def route(self, prompt: str, *, budget: RouteBudget | None = None) -> RoutingResult:
        return self._hierarchy.route(prompt, budget=budget)
