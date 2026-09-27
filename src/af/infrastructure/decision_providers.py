"""Optional Jev adapter for the documented TypeSafe System One HTTP contract.

No SDK dependency or implicit installation. A pinned model, explicit enabling,
remote policy and one-use consent are all required before any data leaves AF.
API: https://docs.typesafe.ai/api (verified 2026-09-26).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from decimal import Decimal
from math import isclose, isfinite

import httpx

from af.framework.decision_consent import (
    DecisionConsent,
    DecisionConsentRegistry,
    DecisionConsentStore,
    DecisionPreview,
)
from af.framework.decisions import (
    DecisionAnswer,
    DecisionKind,
    DecisionRequest,
    DecisionStatus,
    Probability,
)
from af.framework.types import SelectorLocality, parse_probability
from af.infrastructure.provider_http import ProviderFailure, object_map, post_json


class JevDecisionProvider:
    name = "jev"
    locality = SelectorLocality.REMOTE
    endpoint = "https://api.typesafe.ai/v1/systemone"

    def __init__(
        self,
        *,
        api_key: str = "",
        enabled: bool = False,
        allow_remote: bool = False,
        model: str = "jev-1.13.0",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        consents: DecisionConsentStore | None = None,
        input_token_price_usd: Decimal = Decimal("0.000000042"),
    ) -> None:
        if (
            type(model) is not str
            or re.fullmatch(r"jev-[0-9]+\.[0-9]+\.[0-9]+", model) is None
            or len(model) > 128
            or any(char.isspace() for char in model)
        ):
            raise ValueError("invalid Jev model")
        if type(timeout) not in (int, float) or not isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError("invalid decision timeout")
        if (
            not isinstance(input_token_price_usd, Decimal)
            or not input_token_price_usd.is_finite()
            or input_token_price_usd <= 0
        ):
            raise ValueError("invalid decision token tariff")
        self._api_key = api_key
        self._enabled = enabled is True
        self._allow_remote = allow_remote is True
        self._model = model
        self._timeout = timeout
        self._transport = transport
        self._clock = clock
        self._price = input_token_price_usd
        self.consents: DecisionConsentStore = (
            consents if consents is not None else DecisionConsentRegistry(clock=clock)
        )

    def _payload(self, request: DecisionRequest) -> bytes:
        question: dict[str, object] = {
            "type": "noul" if request.kind is DecisionKind.BINARY else "choice",
            "instructions": request.question,
        }
        if request.kind is DecisionKind.CHOICE:
            question["criteria"] = {option.id: option.description for option in request.options}
        return json.dumps(
            {"model": self._model, "state": request.state, "questions": {"decision": question}},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()

    def preview(self, request: DecisionRequest, *, expires_in: float = 300.0) -> DecisionPreview:
        """Pure disclosure and conservative configured-tariff quote; no provider call.

        The token ceiling reserves twice the wire byte count plus 2048 framing
        tokens. This is an application budget bound, not a provider price promise;
        actual usage is checked against it. No retry is purchased by the consent.
        """
        if (
            type(expires_in) not in (int, float)
            or not isfinite(expires_in)
            or not 0 < expires_in <= 3600
        ):
            raise ValueError("invalid decision consent expiry")
        payload = self._payload(request)
        token_ceiling = len(payload) * 2 + 2048
        if token_ceiling > 64_000:
            raise ValueError("decision exceeds conservative provider context budget")
        return DecisionPreview(
            provider=self.name,
            model=self._model,
            destination=self.endpoint,
            request_digest=request.digest,
            payload_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
            request_bytes=len(payload),
            max_cost_usd=Decimal(token_ceiling) * self._price,
            expires_at=self._clock() + expires_in,
        )

    def evaluate(
        self,
        request: DecisionRequest,
        *,
        cancelled: Callable[[], bool] | None = None,
        consent: DecisionConsent | None = None,
    ) -> DecisionAnswer:
        if cancelled is not None and cancelled():
            return self._failure(request, DecisionStatus.CANCELLED)
        if not self._enabled or not self._allow_remote or not self._api_key or consent is None:
            return self._failure(request, DecisionStatus.DENIED)
        try:
            preview = self.preview(request)
        except ValueError:
            return self._failure(request, DecisionStatus.MALFORMED)
        if not self.consents.consume(consent, expected=preview):
            return self._failure(request, DecisionStatus.DENIED)
        try:
            raw = post_json(
                self.endpoint,
                self._payload(request),
                headers={"Authorization": "Bearer " + self._api_key},
                timeout=self._timeout,
                transport=self._transport,
                cancelled=cancelled,
            )
            result = self._answer(request, raw)
            if Decimal(result.input_tokens) * self._price > preview.max_cost_usd:
                result = self._failure(request, DecisionStatus.DENIED)
        except ProviderFailure as error:
            result = self._failure(request, error.status)
        except (ValueError, TypeError, OverflowError):
            result = self._failure(request, DecisionStatus.MALFORMED)
        self.consents.record(consent, result)
        return result

    def _failure(self, request: DecisionRequest, status: DecisionStatus) -> DecisionAnswer:
        return DecisionAnswer(request.digest, self.name, status)

    def _answer(self, request: DecisionRequest, raw: object) -> DecisionAnswer:
        body = object_map(raw)
        if body.get("model") != self._model:
            raise ProviderFailure(DecisionStatus.MALFORMED)
        answers = object_map(body.get("answers"))
        if set(answers) != {"decision"}:
            raise ProviderFailure(DecisionStatus.MALFORMED)
        answer = object_map(answers["decision"])
        usage = object_map(body.get("usage"))
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        if type(input_tokens) is not int or type(output_tokens) is not int:
            raise ProviderFailure(DecisionStatus.MALFORMED)
        if request.kind is DecisionKind.BINARY:
            probability = parse_probability(answer.get("noul"))
            if (
                set(answer) != {"type", "noul"}
                or answer.get("type") != "noul"
                or probability is None
            ):
                raise ProviderFailure(DecisionStatus.MALFORMED)
            return DecisionAnswer(
                request.digest,
                self.name,
                model=self._model,
                probability=probability,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        if (
            set(answer) != {"type", "choice", "probabilities", "confidence"}
            or answer.get("type") != "choice"
        ):
            raise ProviderFailure(DecisionStatus.MALFORMED)
        distribution = object_map(answer.get("probabilities"))
        choice = answer.get("choice")
        confidence = parse_probability(answer.get("confidence"))
        if (
            type(choice) is not str
            or set(distribution) != {item.id for item in request.options}
            or choice not in distribution
            or confidence is None
        ):
            raise ProviderFailure(DecisionStatus.MALFORMED)
        probabilities: list[Probability] = []
        for option in request.options:
            value = parse_probability(distribution[option.id])
            if value is None:
                raise ProviderFailure(DecisionStatus.MALFORMED)
            probabilities.append(Probability(option.id, value))
        if not isclose(sum(item.value for item in probabilities), 1.0, abs_tol=1e-6):
            raise ProviderFailure(DecisionStatus.MALFORMED)
        selected = next(item.value for item in probabilities if item.option == choice)
        if selected != max(item.value for item in probabilities):
            raise ProviderFailure(DecisionStatus.MALFORMED)
        return DecisionAnswer(
            request.digest,
            self.name,
            model=self._model,
            probabilities=tuple(probabilities),
            provider_confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
