"""Structural contracts keep models, persistence, and transports replaceable."""

from __future__ import annotations

from typing import Protocol

from af.framework.types import (
    Classification,
    ExecutionOutcome,
    ProcedureResult,
    RouteMetrics,
    RunTrace,
    VerificationResult,
    WorkRequest,
    WorkTier,
)


class Agent(Protocol):
    def handle(self, request: WorkRequest) -> ExecutionOutcome: ...


class Classifier(Protocol):
    def classify(self, request: WorkRequest) -> Classification | None: ...


class Procedure(Protocol):
    id: str
    version: str
    tier: WorkTier
    pure: bool
    replay_safe: bool
    metrics: RouteMetrics

    def supports(self, classification: Classification) -> bool: ...

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


class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def complete(self, prompt: str) -> str: ...
