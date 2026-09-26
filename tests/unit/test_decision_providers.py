"""Remote egress is bound to explicit one-use consent and exact typed wire contracts."""

import json
from decimal import Decimal

import httpx
import pytest

from af.framework.decisions import ChoiceOption, DecisionKind, DecisionRequest, DecisionStatus
from af.infrastructure.decision_providers import JevDecisionProvider


def choice_request() -> DecisionRequest:
    return DecisionRequest(
        DecisionKind.CHOICE,
        "Which route?",
        "state",
        (ChoiceOption("a", "A"), ChoiceOption("b", "B")),
    )


def response() -> dict[str, object]:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "decision": {
                "type": "choice",
                "choice": "a",
                "probabilities": {"a": 0.9, "b": 0.1},
                "confidence": 0.4,
            }
        },
        "usage": {"input_tokens": 25, "output_tokens": 10},
    }


def test_jev_exact_wire_and_consent_precedes_any_egress() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=response())

    provider = JevDecisionProvider(
        api_key="secret", enabled=True, allow_remote=True, transport=httpx.MockTransport(handler)
    )
    req = choice_request()
    preview = provider.preview(req)
    assert not calls
    assert provider.evaluate(req).status is DecisionStatus.DENIED
    grant = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    answer = provider.evaluate(req, consent=grant)
    assert answer.probabilities[0].value == 0.9
    assert answer.provider_confidence == 0.4
    assert str(calls[0].url) == "https://api.typesafe.ai/v1/systemone"
    assert calls[0].headers["authorization"] == "Bearer secret"
    assert json.loads(calls[0].content) == {
        "model": "jev-1.13.0",
        "state": "state",
        "questions": {
            "decision": {
                "type": "choice",
                "instructions": "Which route?",
                "criteria": {"a": "A", "b": "B"},
            }
        },
    }
    assert provider.evaluate(req, consent=grant).status is DecisionStatus.DENIED
    assert len(calls) == 1
    assert "secret" not in repr(preview) + repr(grant) + repr(answer)


def test_consent_cannot_widen_scope_and_is_revocable_expiring_and_budget_bound() -> None:
    now = [100.0]
    calls: list[httpx.Request] = []
    provider = JevDecisionProvider(
        api_key="secret",
        enabled=True,
        allow_remote=True,
        clock=lambda: now[0],
        transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(200, json=response())
        ),
    )
    req = choice_request()
    preview = provider.preview(req, expires_in=5)
    with pytest.raises(ValueError):
        provider.consents.approve(preview, max_cost_usd=Decimal("0"))
    grant = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    changed = DecisionRequest(DecisionKind.BINARY, "secret question", "secret text")
    assert provider.evaluate(changed, consent=grant).status is DecisionStatus.DENIED
    provider.consents.revoke(grant)
    assert provider.evaluate(req, consent=grant).status is DecisionStatus.DENIED
    expired = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    now[0] = 106
    assert provider.evaluate(req, consent=expired).status is DecisionStatus.DENIED
    assert not calls


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"model": "wrong", "answers": {}, "usage": {}},
        {
            "model": "jev-1.13.0",
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "evil",
                    "probabilities": {"a": 0.9, "b": 0.1},
                    "confidence": 0.4,
                }
            },
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        {
            "model": "jev-1.13.0",
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "a",
                    "probabilities": {"a": 0.9, "b": 0.9},
                    "confidence": 0.4,
                }
            },
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    ],
)
def test_jev_rejects_malformed_answers_without_fallback(payload: object) -> None:
    calls = []
    provider = JevDecisionProvider(
        api_key="secret",
        enabled=True,
        allow_remote=True,
        transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(200, json=payload)
        ),
    )
    req = choice_request()
    preview = provider.preview(req)
    grant = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    assert provider.evaluate(req, consent=grant).status is DecisionStatus.MALFORMED
    assert len(calls) == 1


def test_jev_noul_has_probability_without_invented_confidence() -> None:
    provider = JevDecisionProvider(
        api_key="secret",
        enabled=True,
        allow_remote=True,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {"decision": {"type": "noul", "noul": 0.8}},
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        ),
    )
    req = DecisionRequest(DecisionKind.BINARY, "Is relevant?", "data")
    preview = provider.preview(req)
    grant = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    result = provider.evaluate(req, consent=grant)
    assert result.probability == 0.8 and result.provider_confidence is None


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("timeout", DecisionStatus.TIMEOUT),
        ("error", DecisionStatus.UNAVAILABLE),
        ("oversize", DecisionStatus.MALFORMED),
        ("redirect", DecisionStatus.UNAVAILABLE),
    ],
)
def test_remote_failures_are_sanitized_and_never_retried(
    failure: str, expected: DecisionStatus
) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret body and key")
        if failure == "error":
            return httpx.Response(500, text="secret body and key")
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://example.com"})
        return httpx.Response(200, text="x" * 65537)

    provider = JevDecisionProvider(
        api_key="secret", enabled=True, allow_remote=True, transport=httpx.MockTransport(handler)
    )
    req = choice_request()
    preview = provider.preview(req)
    grant = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    assert provider.evaluate(req, consent=grant).status is expected
    assert len(calls) == 1


def test_disabled_unconfigured_and_cancelled_provider_make_no_calls() -> None:
    def forbidden(req: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected egress")

    for kwargs in ({}, {"enabled": True}, {"enabled": True, "allow_remote": True}):
        provider = JevDecisionProvider(
            api_key="", transport=httpx.MockTransport(forbidden), **kwargs
        )
        assert provider.evaluate(choice_request()).status is DecisionStatus.DENIED
    provider = JevDecisionProvider(
        api_key="secret", enabled=True, allow_remote=True, transport=httpx.MockTransport(forbidden)
    )
    assert (
        provider.evaluate(choice_request(), cancelled=lambda: True).status
        is DecisionStatus.CANCELLED
    )


@pytest.mark.parametrize(
    "options",
    [
        {"model": "not-jev"},
        {"model": "jev-latest"},
        {"model": "jev-preview"},
        {"timeout": 0},
        {"timeout": True},
        {"timeout": 601},
        {"input_token_price_usd": Decimal("NaN")},
        {"input_token_price_usd": Decimal("0")},
    ],
)
def test_jev_invalid_configuration_is_rejected_without_a_call(options: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        JevDecisionProvider(**options)  # type: ignore[arg-type]


def test_preview_rejects_invalid_expiry_and_context_budget() -> None:
    provider = JevDecisionProvider()
    for expiry in (0, -1, float("inf"), 3601):
        with pytest.raises(ValueError):
            provider.preview(choice_request(), expires_in=expiry)
    with pytest.raises(ValueError):
        provider.preview(DecisionRequest(DecisionKind.BINARY, "Question", "x" * 32_000))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(model="jev-2.0.0"),
        lambda body: body.update(answers={}),
        lambda body: body.update(usage={"input_tokens": True, "output_tokens": 0}),
        lambda body: body.update(usage={"input_tokens": -1, "output_tokens": 0}),
        lambda body: body["answers"]["decision"].update(probabilities={"a": True, "b": 0}),
        lambda body: body["answers"]["decision"].update(type="score"),
        lambda body: body["answers"]["decision"].update(choice="b"),
    ],
)
def test_provider_response_invariants_reject_bad_model_usage_and_probabilities(mutate) -> None:  # type: ignore[no-untyped-def]
    body = response()
    mutate(body)
    provider = JevDecisionProvider(
        api_key="secret",
        enabled=True,
        allow_remote=True,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    )
    req = choice_request()
    preview = provider.preview(req)
    grant = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    assert provider.evaluate(req, consent=grant).status is DecisionStatus.MALFORMED
    receipt = provider.consents.receipt(grant)
    assert receipt is not None and receipt.status == "malformed"


def test_usage_above_approved_envelope_is_rejected_and_recorded() -> None:
    body = response()
    body["usage"] = {"input_tokens": 100_000, "output_tokens": 1}
    provider = JevDecisionProvider(
        api_key="secret",
        enabled=True,
        allow_remote=True,
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    )
    req = choice_request()
    preview = provider.preview(req)
    grant = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    assert provider.evaluate(req, consent=grant).status is DecisionStatus.DENIED
    receipt = provider.consents.receipt(grant)
    assert receipt is not None and receipt.status == "denied"


def test_invalid_noul_cannot_smuggle_a_confidence_or_nan() -> None:
    provider = JevDecisionProvider(
        api_key="secret",
        enabled=True,
        allow_remote=True,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {"decision": {"type": "noul", "noul": 0.8, "confidence": 1}},
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        ),
    )
    req = DecisionRequest(DecisionKind.BINARY, "Question", "State")
    preview = provider.preview(req)
    grant = provider.consents.approve(preview, max_cost_usd=preview.max_cost_usd)
    assert provider.evaluate(req, consent=grant).status is DecisionStatus.MALFORMED


def test_jev_rejects_unpinned_names_and_reports_oversize_before_egress() -> None:
    from af.framework.decision_consent import DecisionConsent

    for name in ("jev-main", "jev-nightly", "jev-1"):
        with pytest.raises(ValueError):
            JevDecisionProvider(model=name)
    provider = JevDecisionProvider(api_key="private", enabled=True, allow_remote=True)
    request = DecisionRequest(DecisionKind.BINARY, "?", "x" * 32_000)
    assert (
        provider.evaluate(request, consent=DecisionConsent("unused")).status
        is DecisionStatus.MALFORMED
    )
