"""Jacob's-ladder routing with an explicit cheap/fast/smart trade-off."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from af.framework.contracts import Procedure
from af.framework.errors import InvalidRoutingPolicy, UnsupportedWork
from af.framework.types import (
    ARGUMENT_NAME_MAX_LENGTH,
    PROCEDURE_ARGUMENT_COUNT_MAX,
    PROMPT_MAX_LENGTH,
    ROUTE_METRIC_MAX,
    ArgumentValueType,
    Classification,
    ProcedureArgumentDescriptor,
    ProcedureDescriptor,
    ProcedureEffect,
    ProcedureRisk,
    RouteMetrics,
    WorkTier,
    is_valid_argument_name,
    is_valid_procedure_id,
    is_valid_procedure_intent,
    is_valid_procedure_version,
)


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
        self._descriptors: dict[str, ProcedureDescriptor] = {}

    def register(self, procedure: Procedure) -> None:
        if procedure.id in self._procedures:
            raise ValueError(f"procedure already registered: {procedure.id}")
        descriptor = self._validated_descriptor(procedure)
        self._procedures[procedure.id] = procedure
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
