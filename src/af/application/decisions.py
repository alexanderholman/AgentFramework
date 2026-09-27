"""Transport-independent remote decision preview, approval, execution and receipts."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from typing import Protocol

from af.framework.decision_consent import (
    DecisionConsent,
    DecisionConsentStore,
    DecisionPreview,
    DecisionReceipt,
    consent_scope,
)
from af.framework.decisions import (
    ChoiceOption,
    DecisionAnswer,
    DecisionKind,
    DecisionRequest,
)


class ConsentedDecisionProvider(Protocol):
    consents: DecisionConsentStore

    def preview(
        self, request: DecisionRequest, *, expires_in: float = 300.0
    ) -> DecisionPreview: ...
    def evaluate(
        self, request: DecisionRequest, *, consent: DecisionConsent | None = None
    ) -> DecisionAnswer: ...


def decision_request_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "question", "state"],
        "properties": {
            "kind": {"enum": ["binary", "choice"]},
            "question": {"type": "string", "minLength": 1, "maxLength": 4000},
            "state": {"type": "string", "maxLength": 32000},
            "options": {
                "type": "array",
                "maxItems": 255,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "description"],
                    "properties": {
                        "id": {"type": "string", "pattern": "^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$"},
                        "description": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                },
            },
        },
        "allOf": [
            {
                "if": {"properties": {"kind": {"const": "choice"}}},
                "then": {"required": ["options"], "properties": {"options": {"minItems": 2}}},
                "else": {"properties": {"options": {"maxItems": 0}}},
            }
        ],
    }


def parse_request(data: object) -> DecisionRequest:
    if (
        type(data) is not dict
        or not {"kind", "question", "state"} <= set(data)
        or set(data) - {"kind", "question", "state", "options"}
    ):
        raise ValueError("invalid decision request")
    options = data.get("options", [])
    if type(options) is not list or len(options) > 255:
        raise ValueError("invalid decision options")
    parsed: list[ChoiceOption] = []
    for item in options:
        if type(item) is not dict or set(item) != {"id", "description"}:
            raise ValueError("invalid decision option")
        parsed.append(ChoiceOption(item["id"], item["description"]))
    return DecisionRequest(
        DecisionKind(data["kind"]), data["question"], data["state"], tuple(parsed)
    )


def preview_payload(preview: DecisionPreview) -> dict[str, object]:
    result = asdict(preview)
    result["max_cost_usd"] = str(preview.max_cost_usd)
    return result


class DecisionController:
    def __init__(self, provider: ConsentedDecisionProvider) -> None:
        self._provider = provider

    def preview(self, request: DecisionRequest, *, expires_in: float = 300.0) -> DecisionPreview:
        return self._provider.preview(request, expires_in=expires_in)

    def approve(
        self, request: DecisionRequest, disclosure: object, *, max_cost_usd: Decimal
    ) -> DecisionConsent:
        """Explicit caller action. Reconstruct expected scope; never trust edited JSON."""
        fresh = self.preview(request)
        if type(disclosure) is not dict or set(disclosure) != set(preview_payload(fresh)):
            raise ValueError("decision disclosure is invalid")
        try:
            data = dict(disclosure)
            data["max_cost_usd"] = Decimal(data["max_cost_usd"])
            for field in ("effects", "risks", "data_categories"):
                value = data[field]
                if type(value) not in (tuple, list) or any(type(item) is not str for item in value):
                    raise ValueError("decision disclosure is invalid")
                data[field] = tuple(value)
            shown = DecisionPreview(**data)
            if type(shown.expires_at) not in (int, float) or consent_scope(shown) != consent_scope(
                fresh
            ):
                raise ValueError("decision disclosure no longer matches request")
        except (TypeError, InvalidOperation):
            raise ValueError("decision disclosure is invalid") from None
        return self._provider.consents.approve(shown, max_cost_usd=max_cost_usd)

    def evaluate(self, request: DecisionRequest, consent: DecisionConsent) -> DecisionAnswer:
        return self._provider.evaluate(request, consent=consent)

    def revoke(self, consent: DecisionConsent) -> None:
        self._provider.consents.revoke(consent)

    def receipt(self, consent: DecisionConsent) -> DecisionReceipt | None:
        return self._provider.consents.receipt(consent)
