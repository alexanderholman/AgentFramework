"""Optional root composition preserves authority and durable service state."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

from af.application.authentication import AuthenticationService
from af.application.decisions import preview_payload
from af.bootstrap_extensions import (
    create_authentication_service,
    create_capability_snapshot,
    create_decision_controller,
    create_oauth_controller,
)
from af.config import Settings
from af.framework.authentication import AuthenticationDenied
from af.framework.decisions import DecisionKind, DecisionRequest
from af.infrastructure.authentication import SQLiteAuthenticationRepository
from af.infrastructure.database import Database


def test_configured_oauth_and_authentication_share_tenant_bound_persistent_tokens(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'state.db'}", auth_tenant_id="workspace-a"
    )
    database = Database(settings.database_url)
    try:
        auth = create_authentication_service(database)
        oauth = create_oauth_controller(settings, database, auth)
        client = oauth.run("service-register", {"client_id": "worker", "scopes": ["af:read"]})
    finally:
        database.close()
    restarted = Database(settings.database_url)
    try:
        auth = create_authentication_service(restarted)
        oauth = create_oauth_controller(settings, restarted, auth)
        issued = oauth.run("client-credentials", {**client, "scopes": ["af:read"]})
        principal = auth.authenticate(str(issued["secret"]), tenant_id="workspace-a")
        assert principal.subject_id == "worker"
        assert principal.scopes == ("af:read",)
        with pytest.raises(AuthenticationDenied):
            auth.authenticate(str(issued["secret"]), tenant_id="local")
        oauth.run("service-revoke", {"client_id": "worker"})
        with pytest.raises(AuthenticationDenied):
            oauth.run("client-credentials", {**client, "scopes": ["af:read"]})
    finally:
        restarted.close()


@pytest.mark.parametrize("url", ["sqlite://", "postgresql://host/database"])
def test_authentication_composition_rejects_nonpersistent_reference_storage(
    tmp_path: Path, url: str
) -> None:
    # The rejection depends on URL metadata and must precede any database IO.
    database = Mock(spec=Database)
    database.engine = Mock(url=make_url(url))
    auth = AuthenticationService(SQLiteAuthenticationRepository(tmp_path / "auth.db"))
    with pytest.raises(ValueError, match="persistent SQLite"):
        create_authentication_service(database)
    with pytest.raises(ValueError, match="persistent SQLite"):
        create_oauth_controller(Settings(), database, auth)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_decision_consents_persist_with_both_supported_composition_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-api-key-must-not-appear")
    # The PostgreSQL composition stores consent locally; no remote DB is contacted.
    database = Mock(spec=Database)
    url = f"sqlite:///{tmp_path / 'state.db'}" if backend == "sqlite" else "postgresql://host/db"
    database.engine = Mock(url=make_url(url))
    request = DecisionRequest(DecisionKind.BINARY, "Is two even?", "2")
    first = create_decision_controller(Settings(), database)
    disclosure = preview_payload(first.preview(request))
    assert "secret-api-key-must-not-appear" not in json.dumps(disclosure)
    consent = first.approve(request, disclosure, max_cost_usd=Decimal("0.01"))
    restarted = create_decision_controller(Settings(), database)
    restarted.revoke(consent)
    receipt = first.receipt(consent)
    assert receipt is not None and receipt.status == "revoked"
    assert receipt.request_digest == request.digest
    assert first.evaluate(request, consent).status.value == "denied"


def test_in_memory_decision_consent_is_scoped_to_one_application(tmp_path: Path) -> None:
    database = Database("sqlite://")
    try:
        first = create_decision_controller(Settings(), database)
        other = create_decision_controller(Settings(), database)
        request = DecisionRequest(DecisionKind.BINARY, "Is two even?", "2")
        consent = first.approve(
            request, preview_payload(first.preview(request)), max_cost_usd=Decimal("0.01")
        )
        other.revoke(consent)
        assert other.receipt(consent) is None
        first.revoke(consent)
        receipt = first.receipt(consent)
        assert receipt is not None and receipt.status == "revoked"
    finally:
        database.close()


def test_capability_snapshot_has_stable_policy_identity_without_invented_evidence() -> None:
    base = Settings()
    first, repeated = create_capability_snapshot(base), create_capability_snapshot(base)
    assert first.policy_digest == repeated.policy_digest
    assert first.source_digest == repeated.source_digest
    assert first.records == () and first.authorized_capability_ids == ()
    assert first.diagnostics
    for settings in (
        replace(base, allow_remote_inference=True),
        replace(base, selector_provider="ollama", selector_model="local-model"),
        replace(base, decision_enabled=True),
    ):
        changed = create_capability_snapshot(settings)
        assert changed.policy_digest != first.policy_digest
        assert changed.source_digest == first.source_digest
        assert changed.records == () and changed.authorized_capability_ids == ()
