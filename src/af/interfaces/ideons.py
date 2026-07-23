"""REST adapter for the deterministic Ideon interpretation boundary."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from af.application.ideons import IdeonController, InterpretRequest
from af.domain.ideons.records import ContextGraph

_MAX_CONTEXT_FIELDS = 64


class IdeonInterpretPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str = Field(min_length=1, max_length=10_000)
    source_id: str = Field(min_length=1, max_length=200)
    rights: str = Field(min_length=1, max_length=500)
    context: dict[str, str] = Field(default_factory=dict)
    identity_fields: list[str] = Field(default_factory=list, max_length=_MAX_CONTEXT_FIELDS)
    action_requested: bool = False
    media_complete: bool = True

    @field_validator("surface", "source_id", "rights")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_context_contract(self) -> IdeonInterpretPayload:
        if len(self.context) > _MAX_CONTEXT_FIELDS:
            raise ValueError(f"context must contain at most {_MAX_CONTEXT_FIELDS} fields")
        if len(set(self.identity_fields)) != len(self.identity_fields):
            raise ValueError("identity fields must be unique")
        if not set(self.identity_fields).issubset(self.context):
            raise ValueError("identity fields must name supplied context values")
        return self


def create_ideon_router(controller: IdeonController) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ideons", tags=["ideons"])

    @router.post("/interpret", status_code=status.HTTP_200_OK)
    def interpret(payload: IdeonInterpretPayload) -> dict[str, Any]:
        try:
            context = ContextGraph.from_dict(
                payload.context,
                identity_fields=tuple(payload.identity_fields),
            )
            record = controller.interpret(
                InterpretRequest(
                    surface=payload.surface,
                    source_id=payload.source_id,
                    rights=payload.rights,
                    context=context,
                    action_requested=payload.action_requested,
                    media_complete=payload.media_complete,
                )
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from None
        return {"record_id": record.record_id, **record.to_dict()}

    return router
