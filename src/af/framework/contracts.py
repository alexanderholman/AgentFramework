"""Structural contracts keep models, persistence, and transports replaceable."""

from __future__ import annotations

from typing import Protocol

from af.framework.types import (
    Classification,
    ExecutionOutcome,
    ProcedureDescriptor,
    ProcedureResult,
    RouteMetrics,
    RunTrace,
    SelectionDecision,
    SelectorLocality,
    VerificationResult,
    WorkRequest,
    WorkTier,
)


class Agent(Protocol):
    def handle(self, request: WorkRequest) -> ExecutionOutcome: ...

    def handle_classification(
        self,
        request: WorkRequest,
        classification: Classification,
        *,
        expected_procedure_id: str | None = None,
    ) -> ExecutionOutcome: ...


class Classifier(Protocol):
    def classify(self, request: WorkRequest) -> Classification | None: ...


class Procedure(Protocol):
    id: str
    version: str
    tier: WorkTier
    pure: bool
    replay_safe: bool
    has_selection_grounding: bool
    metrics: RouteMetrics
    selection_descriptor: ProcedureDescriptor

    def supports(self, classification: Classification) -> bool: ...

    def validate(self, classification: Classification) -> None: ...

    def selection_is_grounded(
        self,
        request: WorkRequest,
        classification: Classification,
    ) -> bool: ...

    def execute(self, classification: Classification) -> ProcedureResult: ...


class Verifier(Protocol):
    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult: ...


class TraceRepository(Protocol):
    def save(self, trace: RunTrace) -> None: ...

    def get(self, trace_id: str) -> RunTrace: ...

    def list(self) -> list[RunTrace]: ...


class ReuseStore(Protocol):
    def lookup(
        self, classification: Classification, procedure: Procedure
    ) -> ProcedureResult | None: ...

    def promote(
        self,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> None: ...


class RegressionGate(Protocol):
    def evaluate(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
        verifier: Verifier,
    ) -> bool: ...


class Selector(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def locality(self) -> SelectorLocality: ...

    def select(
        self,
        prompt: str,
        catalogue: tuple[ProcedureDescriptor, ...],
    ) -> SelectionDecision: ...
