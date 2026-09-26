"""Composition helpers for optional configured application services."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from af.application.account_controller import AccountController
from af.application.accounts import AccountService
from af.application.authentication import AuthenticationService, OAuthAuthorizationService
from af.application.capability_preview import CapabilitySnapshot
from af.application.decisions import DecisionController
from af.application.human_decisions import HumanDecisionService
from af.application.oauth_controller import OAuthController
from af.application.oauth_grants import OAuthGrantService
from af.application.passkeys import PasskeyService
from af.config import Settings
from af.framework.contracts import Agent, Classifier
from af.framework.decision_consent import DecisionConsentRegistry, DecisionConsentStore
from af.framework.evolution import EvolutionSnapshot
from af.framework.routing import LadderRouter, ProcedureRegistry
from af.infrastructure.account_notifications import SQLiteAccountNotificationOutbox
from af.infrastructure.accounts import FernetSecretCipher, ScryptPasswordHasher
from af.infrastructure.authentication import SQLiteAuthenticationRepository
from af.infrastructure.database import Database
from af.infrastructure.decision_consents import SQLiteDecisionConsentRegistry
from af.infrastructure.decision_providers import JevDecisionProvider
from af.infrastructure.human_decisions import SQLiteHumanDecisionRepository
from af.infrastructure.oauth_grants import SQLiteOAuthGrantRepository
from af.infrastructure.passkeys import SQLitePasskeyRepository, WebAuthnVerifier
from af.infrastructure.self_operation_fixtures import reference_evolution_snapshot
from af.infrastructure.sqlite_streams import SQLiteStreamDriver


def create_decision_controller(settings: Settings, database: Database) -> DecisionController:
    url = database.engine.url
    consents: DecisionConsentStore
    if url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:"):
        consents = DecisionConsentRegistry()
    else:
        path = (
            Path(str(url.database)).expanduser().resolve()
            if url.get_backend_name() == "sqlite"
            else Path("storage/af-decisions.sqlite3").resolve()
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        consents = SQLiteDecisionConsentRegistry(path)
    return DecisionController(
        JevDecisionProvider(
            api_key=os.environ.get("TYPESAFE_API_KEY", ""),
            enabled=settings.decision_enabled,
            allow_remote=settings.allow_remote_inference,
            model=settings.decision_model,
            timeout=settings.decision_timeout_seconds,
            input_token_price_usd=Decimal(settings.decision_token_price_usd),
            consents=consents,
        )
    )


def create_authentication_service(database: Database) -> AuthenticationService:
    url = database.engine.url
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise ValueError("reference authentication requires a persistent SQLite database")
    return AuthenticationService(
        SQLiteAuthenticationRepository(Path(str(url.database)).expanduser().resolve())
    )


def create_stream_driver(database: Database) -> SQLiteStreamDriver:
    url = database.engine.url
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise ValueError("reference durable streams require a persistent SQLite database")
    return SQLiteStreamDriver(Path(str(url.database)).expanduser().resolve())


def create_human_decision_service(
    database: Database,
    authentication_key: bytes,
    agent: Agent,
    classifier: Classifier,
    registry: ProcedureRegistry,
    router: LadderRouter,
) -> HumanDecisionService:
    url = database.engine.url
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise ValueError("reference human decisions require a persistent SQLite database")
    return HumanDecisionService(
        SQLiteHumanDecisionRepository(
            Path(str(url.database)).expanduser().resolve(), authentication_key
        ),
        agent,
        classifier,
        registry,
        router,
        policy_digest=hashlib.sha256(b"af.human.pure-local-read-only.v1").hexdigest(),
    )


def create_account_controller(
    settings: Settings,
    database: Database,
    authentication: AuthenticationService,
    authentication_key: bytes,
) -> AccountController:
    url = database.engine.url
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise ValueError("reference accounts require a persistent SQLite database")
    path = Path(str(url.database)).expanduser().resolve()
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"af.accounts.encryption.v1"
    ).derive(authentication_key)
    cipher = FernetSecretCipher(base64.urlsafe_b64encode(key))
    repository = SQLitePasskeyRepository(path)
    notifications = SQLiteAccountNotificationOutbox(path, cipher, tenant_id=settings.auth_tenant_id)
    accounts = AccountService(
        repository,
        authentication,
        ScryptPasswordHasher(),
        cipher,
        notifications,
        # Registration is an explicit local administrative command. There is no
        # public registration endpoint or caller-selected role in this binding.
        default_scopes=("af:read", "af:write"),
    )
    passkeys = None
    if settings.passkey_rp_id is not None and settings.passkey_origin is not None:
        passkeys = PasskeyService(
            repository,
            authentication,
            accounts,
            WebAuthnVerifier(rp_id=settings.passkey_rp_id, origin=settings.passkey_origin),
        )
    return AccountController(
        accounts,
        authentication,
        notifications,
        tenant_id=settings.auth_tenant_id,
        passkeys=passkeys,
    )


def create_capability_snapshot(settings: Settings) -> CapabilitySnapshot:
    policy = json.dumps(
        {
            "allow_remote_inference": settings.allow_remote_inference,
            "selector_provider": settings.selector_provider,
            "decision_enabled": settings.decision_enabled,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CapabilitySnapshot(
        records=(),
        observed_at=datetime.now(UTC),
        policy_digest="sha256:" + hashlib.sha256(policy.encode()).hexdigest(),
        source_digest="sha256:" + hashlib.sha256(b"[]").hexdigest(),
        diagnostics=(
            "No evidence-backed capability catalogue configured; "
            "installed deterministic procedures are listed by af procedures.",
        ),
    )


def reference_evolution_fixture() -> EvolutionSnapshot:
    return reference_evolution_snapshot(now=datetime.now(UTC))


def peer_qualification_demo() -> dict[str, object]:
    from af.infrastructure.peer_demo import run_peer_demo

    return run_peer_demo()


def create_oauth_controller(
    settings: Settings, database: Database, authentication: AuthenticationService
) -> OAuthController:
    url = database.engine.url
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise ValueError("reference OAuth requires a persistent SQLite database")
    repository = SQLiteOAuthGrantRepository(Path(str(url.database)).expanduser().resolve())
    return OAuthController(
        OAuthGrantService(repository, authentication),
        OAuthAuthorizationService(repository, authentication),
        authentication,
        tenant_id=settings.auth_tenant_id,
    )
