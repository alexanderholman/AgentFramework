"""Explicit, expiring, one-use consent for a single immutable remote decision.

Consent is an application capability, never a statistical model output. Neither
previews nor receipts retain request content, API keys or provider error bodies.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from math import isfinite
from typing import Protocol

from af.framework.decisions import DecisionAnswer


@dataclass(frozen=True, slots=True)
class DecisionPreview:
    provider: str
    model: str
    destination: str
    request_digest: str
    payload_digest: str
    request_bytes: int
    max_cost_usd: Decimal
    expires_at: float
    data_categories: tuple[str, ...] = ("state", "question", "candidate descriptions")
    route: str = "typed-decision"
    effects: tuple[str, ...] = ("remote inference",)
    risks: tuple[str, ...] = (
        "discloses request to configured provider",
        "statistical judgment may be wrong",
    )
    verification: str = "strict schema, candidate, probability and request binding checks"
    fallback: str = "none"


@dataclass(frozen=True, slots=True)
class DecisionConsent:
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    request_digest: str
    provider: str
    model: str
    destination: str
    status: str
    recorded_at: float
    input_tokens: int = 0
    output_tokens: int = 0
    result_digest: str | None = None


def consent_scope(preview: DecisionPreview) -> str:
    """Stable binding of every disclosure field except separately checked expiry."""
    data = asdict(preview)
    del data["expires_at"]
    data["max_cost_usd"] = str(preview.max_cost_usd)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def validate_approval(preview: DecisionPreview, max_cost_usd: Decimal, now: float) -> None:
    if (
        not isinstance(max_cost_usd, Decimal)
        or not max_cost_usd.is_finite()
        or max_cost_usd < 0
        or not preview.max_cost_usd.is_finite()
        or preview.max_cost_usd < 0
        or max_cost_usd < preview.max_cost_usd
        or not isfinite(preview.expires_at)
        or preview.expires_at <= now
        or preview.expires_at > now + 3600
    ):
        raise ValueError("remote decision approval is expired or exceeds its budget")


def decision_receipt(
    preview: DecisionPreview,
    now: float,
    answer: DecisionAnswer | None = None,
    *,
    status: str = "consumed",
) -> DecisionReceipt:
    digest = None
    if answer is not None:
        if answer.request_digest != preview.request_digest or answer.provider != preview.provider:
            raise ValueError("decision receipt does not match approved request")
        encoded = json.dumps(asdict(answer), sort_keys=True, separators=(",", ":")).encode()
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return DecisionReceipt(
        request_digest=preview.request_digest,
        provider=preview.provider,
        model=preview.model,
        destination=preview.destination,
        status=status if answer is None else answer.status.value,
        recorded_at=now,
        input_tokens=0 if answer is None else answer.input_tokens,
        output_tokens=0 if answer is None else answer.output_tokens,
        result_digest=digest,
    )


class DecisionConsentStore(Protocol):
    def approve(self, preview: DecisionPreview, *, max_cost_usd: Decimal) -> DecisionConsent: ...
    def revoke(self, consent: DecisionConsent) -> None: ...
    def consume(self, consent: DecisionConsent, *, expected: DecisionPreview) -> bool: ...
    def record(self, consent: DecisionConsent, answer: DecisionAnswer) -> None: ...
    def receipt(self, consent: DecisionConsent) -> DecisionReceipt | None: ...


class DecisionConsentRegistry:
    """Atomic in-memory grants. For restart durability inject a persistent store."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._approved: dict[str, DecisionPreview] = {}
        self._consumed: dict[str, DecisionPreview] = {}
        self._receipts: dict[str, DecisionReceipt] = {}

    def approve(self, preview: DecisionPreview, *, max_cost_usd: Decimal) -> DecisionConsent:
        validate_approval(preview, max_cost_usd, self._clock())
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._approved = {
                key: value for key, value in self._approved.items() if value.expires_at > now
            }
            self._approved[token] = preview
        return DecisionConsent(token)

    def revoke(self, consent: DecisionConsent) -> None:
        with self._lock:
            preview = self._approved.pop(consent.token, None)
            if preview is not None:
                self._receipts[consent.token] = decision_receipt(
                    preview, self._clock(), status="revoked"
                )

    def consume(self, consent: DecisionConsent, *, expected: DecisionPreview) -> bool:
        with self._lock:
            approved = self._approved.pop(consent.token, None)
            if approved is None or approved.expires_at <= self._clock():
                return False
            if consent_scope(approved) != consent_scope(expected):
                return False
            self._consumed[consent.token] = approved
            self._receipts[consent.token] = decision_receipt(approved, self._clock())
            return True

    def record(self, consent: DecisionConsent, answer: DecisionAnswer) -> None:
        with self._lock:
            preview = self._consumed.get(consent.token)
            if preview is None or self._receipts[consent.token].status != "consumed":
                raise ValueError("decision consent has no unfinished receipt")
            self._receipts[consent.token] = decision_receipt(preview, self._clock(), answer)

    def receipt(self, consent: DecisionConsent) -> DecisionReceipt | None:
        with self._lock:
            return self._receipts.get(consent.token)
