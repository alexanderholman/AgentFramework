"""OAuth grants remain tenant/client scoped and one-use across restart."""

from pathlib import Path

import pytest

from af.application.authentication import AuthenticationService, OAuthAuthorizationService
from af.application.oauth_grants import OAuthGrantService
from af.framework.authentication import AuthenticationDenied, OAuthClient
from af.framework.context import Principal
from af.framework.oauth_grants import OAuthGrantError
from af.infrastructure.oauth_grants import SQLiteOAuthGrantRepository


def test_client_credentials_are_hashed_scoped_and_revocable(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite3"
    repo = SQLiteOAuthGrantRepository(path)
    auth = AuthenticationService(repo, clock=lambda: 100)
    grants = OAuthGrantService(repo, auth, clock=lambda: 100)
    credential = grants.register_service_client("worker", tenant_id="a", scopes=("read",))
    token = grants.client_credentials("worker", credential.secret, tenant_id="a", scopes=("read",))
    assert auth.authenticate(token.secret, tenant_id="a").principal_type == "service"
    assert credential.secret.encode() not in path.read_bytes()
    for secret, tenant, scopes in (
        ("wrong", "a", ("read",)),
        (credential.secret, "b", ("read",)),
        (credential.secret, "a", ("write",)),
    ):
        with pytest.raises(AuthenticationDenied):
            grants.client_credentials("worker", secret, tenant_id=tenant, scopes=scopes)
    grants.revoke_service_client("worker", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        grants.client_credentials("worker", credential.secret, tenant_id="a", scopes=("read",))


def test_refresh_rotation_detects_reuse_and_revokes_entire_token_family(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite3"
    repo = SQLiteOAuthGrantRepository(path)
    auth = AuthenticationService(repo, clock=lambda: 100)
    grants = OAuthGrantService(repo, auth, clock=lambda: 100)
    repo.add_client(OAuthClient("desktop", "a", ("https://af.example/cb",), ("read",)))
    issued = grants.issue_refresh_pair(
        Principal("alice", "human", scopes=("read",)), tenant_id="a", client_id="desktop"
    )
    with pytest.raises(AuthenticationDenied):
        grants.refresh(issued.refresh_secret, tenant_id="a", client_id="other")
    with pytest.raises(AuthenticationDenied):
        grants.refresh(issued.refresh_secret, tenant_id="a", client_id="desktop", scopes=("write",))
    restarted_repo = SQLiteOAuthGrantRepository(path)
    restarted = OAuthGrantService(
        restarted_repo, AuthenticationService(restarted_repo, clock=lambda: 100), clock=lambda: 100
    )
    newer = restarted.refresh(issued.refresh_secret, tenant_id="a", client_id="desktop")
    assert auth.authenticate(newer.access.secret, tenant_id="a").subject_id == "alice"
    with pytest.raises(AuthenticationDenied):
        restarted.refresh(issued.refresh_secret, tenant_id="a", client_id="desktop")
    for secret in (issued.access.secret, newer.access.secret):
        with pytest.raises(AuthenticationDenied):
            auth.authenticate(secret, tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        restarted.refresh(newer.refresh_secret, tenant_id="a", client_id="desktop")


def test_device_flow_pending_slow_down_approval_and_single_redemption(tmp_path: Path) -> None:
    now = [100.0]
    repo = SQLiteOAuthGrantRepository(tmp_path / "auth.sqlite3")
    auth = AuthenticationService(repo, clock=lambda: now[0])
    OAuthAuthorizationService(repo, auth).register_client(
        OAuthClient("tv", "a", ("https://example.com/callback",), ("read",))
    )
    grants = OAuthGrantService(repo, auth, clock=lambda: now[0])
    device = grants.start_device(
        "tv", tenant_id="a", scopes=("read",), verification_uri="https://af.example/device"
    )
    with pytest.raises(OAuthGrantError, match="authorization_pending"):
        grants.poll_device(device.device_code, client_id="tv", tenant_id="a")
    with pytest.raises(OAuthGrantError, match="slow_down"):
        grants.poll_device(device.device_code, client_id="tv", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        grants.approve_device(
            device.user_code,
            Principal("alice", "human", scopes=("read",)),
            tenant_id="b",
            consented=True,
        )
    grants.approve_device(
        device.user_code,
        Principal("alice", "human", scopes=("read",)),
        tenant_id="a",
        consented=True,
    )
    now[0] += 10
    token = grants.poll_device(device.device_code, client_id="tv", tenant_id="a")
    assert auth.authenticate(token.secret, tenant_id="a").subject_id == "alice"
    with pytest.raises(OAuthGrantError, match="invalid_grant"):
        grants.poll_device(device.device_code, client_id="tv", tenant_id="a")


def test_device_denial_expiry_bad_scope_and_revoke_are_enforced(tmp_path: Path) -> None:
    now = [100.0]
    repo = SQLiteOAuthGrantRepository(tmp_path / "auth.sqlite3")
    auth = AuthenticationService(repo, clock=lambda: now[0])
    oauth = OAuthAuthorizationService(repo, auth, clock=lambda: now[0])
    oauth.register_client(OAuthClient("tv", "a", ("https://example.com/cb",), ("read",)))
    grants = OAuthGrantService(repo, auth, clock=lambda: now[0])
    with pytest.raises(AuthenticationDenied):
        grants.start_device(
            "unknown", tenant_id="a", scopes=("read",), verification_uri="https://af.example/device"
        )
    device = grants.start_device(
        "tv", tenant_id="a", scopes=("read",), verification_uri="https://af.example/device"
    )
    with pytest.raises(AuthenticationDenied):
        grants.approve_device(
            "unknown", Principal("alice", "human", scopes=("read",)), tenant_id="a", consented=True
        )
    grants.approve_device(
        device.user_code,
        Principal("alice", "human", scopes=("read",)),
        tenant_id="a",
        consented=False,
    )
    with pytest.raises(OAuthGrantError, match="access_denied"):
        grants.poll_device(device.device_code, client_id="tv", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        grants.approve_device(
            device.user_code,
            Principal("alice", "human", scopes=("read",)),
            tenant_id="a",
            consented=True,
        )
    expired = grants.start_device(
        "tv", tenant_id="a", scopes=("read",), verification_uri="https://af.example/device"
    )
    now[0] = 701
    with pytest.raises(OAuthGrantError, match="expired_token"):
        grants.poll_device(expired.device_code, client_id="tv", tenant_id="a")
    with pytest.raises(OAuthGrantError, match="invalid_grant"):
        grants.poll_device("unknown", client_id="tv", tenant_id="a")
    oauth.revoke_client("tv", tenant_id="a")
    with pytest.raises(OAuthGrantError, match="invalid_grant"):
        grants.poll_device(expired.device_code, client_id="tv", tenant_id="a")


def test_explicit_refresh_revocation_and_expiry(tmp_path: Path) -> None:
    now = [100.0]
    repo = SQLiteOAuthGrantRepository(tmp_path / "auth.sqlite3")
    auth = AuthenticationService(repo, clock=lambda: now[0])
    grants = OAuthGrantService(repo, auth, clock=lambda: now[0])
    repo.add_client(OAuthClient("web", "a", ("https://af.example/cb",), ("read",)))
    first = grants.issue_refresh_pair(
        Principal("a", "human", scopes=("read",)), tenant_id="a", client_id="web"
    )
    with pytest.raises(AuthenticationDenied):
        grants.revoke_refresh("unknown", tenant_id="a", client_id="web")
    grants.revoke_refresh(first.refresh_secret, tenant_id="a", client_id="web")
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(first.access.secret, tenant_id="a")
    second = grants.issue_refresh_pair(
        Principal("a", "human", scopes=("read",)), tenant_id="a", client_id="web"
    )
    now[0] = second.refresh_expires_at + 1
    with pytest.raises(AuthenticationDenied):
        grants.refresh(second.refresh_secret, tenant_id="a", client_id="web")
    with pytest.raises(AuthenticationDenied):
        grants.refresh("unknown", tenant_id="a", client_id="web")


def test_generic_access_rotation_cannot_escape_client_or_refresh_revocation(tmp_path: Path) -> None:
    from af.infrastructure.authentication import SQLiteAuthenticationRepository

    path = tmp_path / "auth.sqlite3"
    repo = SQLiteOAuthGrantRepository(path)
    auth = AuthenticationService(SQLiteAuthenticationRepository(path), clock=lambda: 100)
    grants = OAuthGrantService(repo, auth, clock=lambda: 100)
    service = grants.register_service_client("worker", tenant_id="a", scopes=("read",))
    token = grants.client_credentials("worker", service.secret, tenant_id="a", scopes=("read",))
    rotated = auth.rotate(token.secret, tenant_id="a")
    grants.revoke_service_client("worker", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(rotated.secret, tenant_id="a")
    repo.add_client(OAuthClient("web", "a", ("https://af.example/cb",), ("read",)))
    pair = grants.issue_refresh_pair(
        Principal("alice", "human", scopes=("read",)), tenant_id="a", client_id="web"
    )
    rotated_refresh_access = auth.rotate(pair.access.secret, tenant_id="a")
    grants.revoke_refresh(pair.refresh_secret, tenant_id="a", client_id="web")
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(rotated_refresh_access.secret, tenant_id="a")


def test_refresh_requires_registered_client_and_client_revoke_reaches_rotated_access(
    tmp_path: Path,
) -> None:
    repo = SQLiteOAuthGrantRepository(tmp_path / "db")
    auth = AuthenticationService(repo, clock=lambda: 100)
    grants = OAuthGrantService(repo, auth, clock=lambda: 100)
    oauth = OAuthAuthorizationService(repo, auth, clock=lambda: 100)
    principal = Principal("alice", "human", scopes=("read",))
    with pytest.raises(AuthenticationDenied):
        grants.issue_refresh_pair(principal, tenant_id="a", client_id="unknown")
    oauth.register_client(OAuthClient("web", "a", ("https://af.example/cb",), ("read",)))
    pair = grants.issue_refresh_pair(principal, tenant_id="a", client_id="web")
    rotated = auth.rotate(pair.access.secret, tenant_id="a")
    oauth.revoke_client("web", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        grants.refresh(pair.refresh_secret, tenant_id="a", client_id="web")
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(rotated.secret, tenant_id="a")
