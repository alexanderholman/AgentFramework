"""Installed reference workflow components backed by AF's verified procedures."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from af.domain.procedures import CharacterCountProcedure
from af.domain.verification import DeterministicVerifier
from af.framework.context import ExecutionContext
from af.framework.types import Classification, ProcedureResult, WorkRequest
from af.framework.workflows import WorkflowComponent, WorkflowOutput


class CountBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    texts: tuple[str, ...] = Field(min_length=1, max_length=128)
    character: str = Field(min_length=1, max_length=1)


class CountVector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    counts: tuple[int, ...] = Field(min_length=1, max_length=128)


class CountTotal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    total: int = Field(ge=0)


def _input(data: bytes, predecessors: Mapping[str, bytes]) -> bytes:
    if len(predecessors) > 1:
        raise ValueError("unary workflow component requires exactly one input")
    return next(iter(predecessors.values())) if predecessors else data


class BatchCharacterCount:
    def __init__(self) -> None:
        self._procedure = CharacterCountProcedure()
        self._verifier = DeterministicVerifier()

    @staticmethod
    def _classification(text: str, character: str) -> Classification:
        return Classification(
            "text.character_count",
            {"text": text, "character": character},
            1.0,
            "installed-composition",
        )

    def execute(
        self,
        data: bytes,
        predecessors: Mapping[str, bytes],
        context: ExecutionContext,
        *,
        effect_key: str,
    ) -> WorkflowOutput:
        del effect_key
        request = CountBatch.model_validate_json(_input(data, predecessors))
        counts = []
        for text in request.texts:
            if context.cancellation.cancelled:
                raise ValueError("cancelled")
            classification = self._classification(text, request.character)
            self._procedure.validate(classification)
            result = self._procedure.execute(classification)
            counts.append(int(result.output))
        return WorkflowOutput(data=CountVector(counts=tuple(counts)).model_dump_json().encode())

    def verify(
        self, data: bytes, predecessors: Mapping[str, bytes], output: WorkflowOutput
    ) -> bool:
        request = CountBatch.model_validate_json(_input(data, predecessors))
        counts = CountVector.model_validate_json(output.data).counts
        if len(counts) != len(request.texts):
            return False
        return all(
            self._verifier.verify(
                WorkRequest("installed batch character count"),
                self._classification(text, request.character),
                self._procedure,
                ProcedureResult(output=str(count), evidence={}),
            ).passed
            for text, count in zip(request.texts, counts, strict=True)
        )


class SumCounts:
    def execute(
        self,
        data: bytes,
        predecessors: Mapping[str, bytes],
        context: ExecutionContext,
        *,
        effect_key: str,
    ) -> WorkflowOutput:
        del context, effect_key
        values = CountVector.model_validate_json(_input(data, predecessors))
        return WorkflowOutput(data=CountTotal(total=sum(values.counts)).model_dump_json().encode())

    def verify(
        self, data: bytes, predecessors: Mapping[str, bytes], output: WorkflowOutput
    ) -> bool:
        values = CountVector.model_validate_json(_input(data, predecessors))
        total = 0
        for count in values.counts:
            if count < 0:
                return False
            total += count
        return CountTotal.model_validate_json(output.data).total == total


def reference_workflow_components() -> tuple[WorkflowComponent, ...]:
    return (
        WorkflowComponent(
            "text.count-batch",
            "1.0.0",
            hashlib.sha256(
                b"af.text.count-batch.v1:CharacterCountProcedure:DeterministicVerifier"
            ).hexdigest(),
            True,
            BatchCharacterCount(),
        ),
        WorkflowComponent(
            "number.sum-counts",
            "1.0.0",
            hashlib.sha256(b"af.number.sum-counts.v1:CountVector:CountTotal").hexdigest(),
            True,
            SumCounts(),
        ),
    )
