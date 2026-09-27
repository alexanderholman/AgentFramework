"""Consent survives restart, but consumed/revoked grants never regain authority."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import httpx

from af.framework.decisions import DecisionKind, DecisionRequest, DecisionStatus
from af.infrastructure.decision_consents import SQLiteDecisionConsentRegistry
from af.infrastructure.decision_providers import JevDecisionProvider


def test_durable_approval_executes_once_after_restart_with_sanitized_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "consents.sqlite3"
    first = SQLiteDecisionConsentRegistry(path)
    request = DecisionRequest(DecisionKind.BINARY, "Private question", "Secret patient text")
    provider = JevDecisionProvider(
        api_key="secret-key", enabled=True, allow_remote=True, consents=first
    )
    preview = provider.preview(request)
    grant = first.approve(preview, max_cost_usd=Decimal("1"))
    restarted = SQLiteDecisionConsentRegistry(path)
    calls: list[httpx.Request] = []
    provider = JevDecisionProvider(
        api_key="secret-key",
        enabled=True,
        allow_remote=True,
        consents=restarted,
        transport=httpx.MockTransport(
            lambda req: (
                calls.append(req)
                or httpx.Response(
                    200,
                    json={
                        "model": "jev-1.13.0",
                        "answers": {"decision": {"type": "noul", "noul": 0.9}},
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                )
            )
        ),
    )
    assert provider.evaluate(request, consent=grant).status is DecisionStatus.ANSWERED
    receipt = restarted.receipt(grant)
    assert receipt is not None and receipt.status == "answered" and receipt.input_tokens == 3
    assert receipt.request_digest == request.digest
    third = SQLiteDecisionConsentRegistry(path)
    assert not third.consume(grant, expected=preview)
    assert len(calls) == 1
    stored = path.read_bytes()
    assert b"Secret patient text" not in stored and b"secret-key" not in stored


def test_durable_consumption_is_atomic_across_connections_and_expiry_revocation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "consents.sqlite3"
    now = [100.0]
    stores = [SQLiteDecisionConsentRegistry(path, clock=lambda: now[0]) for _ in range(4)]
    provider = JevDecisionProvider(clock=lambda: now[0])
    request = DecisionRequest(DecisionKind.BINARY, "Question", "State")
    preview = provider.preview(request, expires_in=5)
    grant = stores[0].approve(preview, max_cost_usd=Decimal("1"))
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda store: store.consume(grant, expected=preview), stores))
    assert sum(results) == 1
    revoked = stores[0].approve(preview, max_cost_usd=Decimal("1"))
    stores[1].revoke(revoked)
    assert not stores[2].consume(revoked, expected=preview)
    expired = stores[0].approve(preview, max_cost_usd=Decimal("1"))
    now[0] = 106
    assert not stores[3].consume(expired, expected=preview)
    receipt = stores[0].receipt(grant)
    assert receipt is not None and receipt.status == "consumed"
