"""Bounded deterministic planning over installed approved component contracts.

The planner never imports or executes generated source. Missing functionality is
returned as a typed engineering gap; an external engineering admission workflow
must install and verify it before it can enter this catalogue.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from af.framework.workflows import (
    EvidenceRequirement,
    WorkflowComponent,
    WorkflowDefinition,
    WorkflowNode,
)


@dataclass(frozen=True, slots=True)
class ComponentContract:
    component: WorkflowComponent
    input_schema: str
    output_schema: str
    effects: frozenset[str] = frozenset()
    required_scopes: frozenset[str] = frozenset()
    locality: str = "local"

    def __post_init__(self) -> None:
        if not self.input_schema or not self.output_schema:
            raise ValueError("component requires versioned input and output schema identities")
        if self.locality != "local":
            raise ValueError("reference composer accepts installed local components only")


@dataclass(frozen=True, slots=True)
class CompositionGap:
    request_family: str
    input_schema: str
    required_output_schema: str
    reachable_schemas: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class CompiledComposition:
    request_family: str
    input_schema: str
    output_schema: str
    definition: WorkflowDefinition
    component_ids: tuple[str, ...]


class CompositionPlanner:
    def __init__(self, components: tuple[ComponentContract, ...], *, max_stages: int = 8) -> None:
        if not 1 <= max_stages <= 128:
            raise ValueError("composition stages must be between 1 and 128")
        if len({item.component.component_id for item in components}) != len(components):
            raise ValueError("composition component IDs must be unique")
        self._components = tuple(sorted(components, key=lambda item: item.component.component_id))
        self._max_stages = max_stages

    def compose(
        self,
        request_family: str,
        input_schema: str,
        output_schema: str,
        *,
        permitted_effects: frozenset[str] = frozenset(),
        scopes: frozenset[str] = frozenset(),
    ) -> CompiledComposition | CompositionGap:
        if not request_family or not input_schema or not output_schema:
            raise ValueError("composition requires family and schema identities")
        pending: deque[tuple[str, tuple[ComponentContract, ...]]] = deque([(input_schema, ())])
        visited = {input_schema}
        while pending:
            schema, path = pending.popleft()
            if len(path) >= self._max_stages:
                continue
            for contract in self._components:
                if (
                    contract.input_schema != schema
                    or not contract.effects <= permitted_effects
                    or not contract.required_scopes <= scopes
                ):
                    continue
                candidate = (*path, contract)
                if contract.output_schema == output_schema:
                    nodes = tuple(
                        WorkflowNode(
                            node_id=f"step-{index + 1}",
                            component_id=stage.component.component_id,
                            component_digest=stage.component.digest,
                            dependencies=()
                            if index == 0
                            else (EvidenceRequirement(node_id=f"step-{index}", minimum_bytes=1),),
                        )
                        for index, stage in enumerate(candidate)
                    )
                    return CompiledComposition(
                        request_family,
                        input_schema,
                        output_schema,
                        WorkflowDefinition(workflow_id=request_family, version="1", nodes=nodes),
                        tuple(stage.component.component_id for stage in candidate),
                    )
                if contract.output_schema not in visited:
                    visited.add(contract.output_schema)
                    pending.append((contract.output_schema, candidate))
        return CompositionGap(
            request_family,
            input_schema,
            output_schema,
            tuple(sorted(visited)),
            "no_approved_typed_path",
        )
