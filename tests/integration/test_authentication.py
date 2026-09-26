"""Headless authentication persists scoped authority and consumes credentials atomically."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from af.application.authentication import AuthenticationService, OAuthAuthorizationService
from af.framework.authentication import AuthenticationDenied, OAuthClient, pkce_challenge
from af.framework.context import Principal
from af.infrastructure.authentication import SQLiteAuthenticationRepository


def test_token_secret_is_hashed_scoped_expiring_and_one_time_rotatable(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite3"
    now = [100.0]
    service = AuthenticationService(SQLiteAuthenticationRepository(path), clock=lambda: now[0])
    principal = Principal("alice", "human", scopes=("tasks:read", "tasks:write"))
    token = service.issue(principal, tenant_id="team-a", scopes=("tasks:read",), ttl_seconds=60)
    assert token.secret not in repr(token)
    assert token.secret.encode() not in path.read_bytes()
    restarted = AuthenticationService(SQLiteAuthenticationRepository(path), clock=lambda: now[0])
    assert (
        restarted.authenticate(
            token.secret, tenant_id="team-a", required_scopes=("tasks:read",)
        ).subject_id
        == "alice"
    )
    for tenant, scopes in (("team-b", ()), ("team-a", ("tasks:write",))):
        with pytest.raises(AuthenticationDenied):
            restarted.authenticate(token.secret, tenant_id=tenant, required_scopes=scopes)
    rotated = restarted.rotate(token.secret, tenant_id="team-a", ttl_seconds=30)
    with pytest.raises(AuthenticationDenied):
        restarted.authenticate(token.secret, tenant_id="team-a")
    assert restarted.authenticate(rotated.secret, tenant_id="team-a").scopes == ("tasks:read",)
    now[0] = 131
    with pytest.raises(AuthenticationDenied):
        restarted.authenticate(rotated.secret, tenant_id="team-a")


def test_rotation_is_atomic_and_cannot_increase_scopes(tmp_path: Path) -> None:
    path = tmp_path / "auth.sqlite3"
    stores = [
        AuthenticationService(SQLiteAuthenticationRepository(path), clock=lambda: 100)
        for _ in range(4)
    ]
    token = stores[0].issue(
        Principal("alice", "human", scopes=("read",)), tenant_id="a", ttl_seconds=100
    )
    with pytest.raises(AuthenticationDenied):
        stores[0].rotate(token.secret, tenant_id="a", scopes=("write",), ttl_seconds=10)

    def rotate(service: AuthenticationService) -> bool:
        try:
            service.rotate(token.secret, tenant_id="a", ttl_seconds=10)
            return True
        except AuthenticationDenied:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(rotate, stores)) == 1


def test_oauth_requires_exact_redirect_pkce_consent_and_rejects_replay(tmp_path: Path) -> None:
    store = SQLiteAuthenticationRepository(tmp_path / "auth.sqlite3")
    auth = AuthenticationService(store, clock=lambda: 100)
    oauth = OAuthAuthorizationService(store, auth, clock=lambda: 100)
    client = OAuthClient("desktop", "a", ("http://127.0.0.1:8080/callback",), ("read",))
    oauth.register_client(client)
    principal = Principal("alice", "human", scopes=("read", "write"))
    verifier = "x" * 43
    kwargs = {
        "client_id": "desktop",
        "tenant_id": "a",
        "principal": principal,
        "redirect_uri": client.redirect_uris[0],
        "state": "random-session-state",
        "challenge": pkce_challenge(verifier),
        "scopes": ("read",),
        "consented": True,
    }
    with pytest.raises(AuthenticationDenied):
        oauth.authorize(**{**kwargs, "consented": False})
    with pytest.raises(AuthenticationDenied):
        oauth.authorize(**{**kwargs, "redirect_uri": "http://127.0.0.1:8080/callback/"})
    code = oauth.authorize(**kwargs)
    assert code.code not in repr(code)
    with pytest.raises(AuthenticationDenied):
        oauth.exchange(
            code.code,
            client_id="desktop",
            tenant_id="a",
            redirect_uri=client.redirect_uris[0],
            verifier="y" * 43,
            state=code.state,
        )
    token = oauth.exchange(
        code.code,
        client_id="desktop",
        tenant_id="a",
        redirect_uri=client.redirect_uris[0],
        verifier=verifier,
        state=code.state,
    )
    assert auth.authenticate(token.secret, tenant_id="a").scopes == ("read",)
    with pytest.raises(AuthenticationDenied):
        oauth.exchange(
            code.code,
            client_id="desktop",
            tenant_id="a",
            redirect_uri=client.redirect_uris[0],
            verifier=verifier,
            state=code.state,
        )


def test_revocation_and_oauth_code_expiry_wrong_state_tenant_and_client(tmp_path: Path) -> None:
    now = [100.0]
    repository = SQLiteAuthenticationRepository(tmp_path / "auth.sqlite3")
    auth = AuthenticationService(repository, clock=lambda: now[0])
    principal = Principal("alice", "human", scopes=("read",))
    token = auth.issue(principal, tenant_id="a")
    assert auth.revoke(token.token_id, tenant_id="b") is False
    assert auth.revoke(token.token_id, tenant_id="a") is True
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(token.secret, tenant_id="a")
    oauth = OAuthAuthorizationService(repository, auth, clock=lambda: now[0])
    uri = "https://example.com/callback"
    oauth.register_client(OAuthClient("web", "a", (uri,), ("read",)))
    verifier = "a" * 43
    grant = oauth.authorize(
        client_id="web",
        tenant_id="a",
        principal=principal,
        redirect_uri=uri,
        state="original-state-123",
        challenge=pkce_challenge(verifier),
        scopes=("read",),
        consented=True,
    )
    for kwargs in (
        {"state": "wrong-state"},
        {"tenant_id": "b"},
        {"client_id": "other"},
        {"redirect_uri": uri + "/"},
    ):
        args = {
            "client_id": "web",
            "tenant_id": "a",
            "redirect_uri": uri,
            "state": grant.state,
            "verifier": verifier,
            **kwargs,
        }
        with pytest.raises(AuthenticationDenied):
            oauth.exchange(grant.code, **args)
    now[0] = 401
    with pytest.raises(AuthenticationDenied):
        oauth.exchange(
            grant.code,
            client_id="web",
            tenant_id="a",
            redirect_uri=uri,
            state=grant.state,
            verifier=verifier,
        )


def test_external_callback_is_state_tenant_bound_and_linking_requires_matching_principal(
    tmp_path: Path,
) -> None:
    from af.application.authentication import ExternalLoginService
    from af.framework.authentication import ExternalIdentity

    class Provider:
        provider_id = "example"
        issuer = "https://issuer.example"
        redirect_uris = ("https://af.example/callback",)
        calls = 0

        def authorization_url(self, *, state: str, challenge: str, redirect_uri: str) -> str:
            return f"https://issuer.example/authorize?state={state}"

        def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> ExternalIdentity:
            self.calls += 1
            return ExternalIdentity(self.issuer, "subject-123")

    repository = SQLiteAuthenticationRepository(tmp_path / "auth.sqlite3")
    provider = Provider()
    login = ExternalLoginService(repository, (provider,), clock=lambda: 100)
    owner = Principal("alice", "human", scopes=("read",))
    attempt = login.begin(
        "example", tenant_id="a", redirect_uri=provider.redirect_uris[0], link_to=owner
    )
    kwargs = {
        "state": attempt.state,
        "verifier": attempt.verifier,
        "code": "code",
        "provider_id": "example",
        "tenant_id": "a",
        "redirect_uri": provider.redirect_uris[0],
        "consented": True,
        "link_to": owner,
    }
    for override in (
        {"tenant_id": "b"},
        {"consented": False},
        {"link_to": Principal("bob", "human")},
        {"state": "bad"},
    ):
        with pytest.raises(AuthenticationDenied):
            login.complete(**{**kwargs, **override})
    assert provider.calls == 0
    principal = login.complete(**kwargs)
    assert principal.subject_id == "alice"
    with pytest.raises(AuthenticationDenied):
        login.complete(**kwargs)
    assert provider.calls == 1
    second = login.begin("example", tenant_id="a", redirect_uri=provider.redirect_uris[0])
    principal = login.complete(
        **{**kwargs, "state": second.state, "verifier": second.verifier, "link_to": None}
    )
    assert principal.subject_id == "alice"


def test_auth_boundaries_and_unregistered_or_revoked_oauth_clients(tmp_path: Path) -> None:
    from af.framework.authentication import (
        checked_scopes,
        identity_value,
        secret_digest,
        valid_redirect,
    )

    for value in ("", "x\nvalue", "x" * 201):
        with pytest.raises(ValueError):
            identity_value(value)
    for scopes in (("read", "read"), ("*",), ("read write",)):
        with pytest.raises(ValueError):
            checked_scopes(scopes)
    for secret in ("", "x" * 1025):
        with pytest.raises(AuthenticationDenied):
            secret_digest(secret)
    for verifier in ("short", "!" * 43):
        with pytest.raises(AuthenticationDenied):
            pkce_challenge(verifier)
    for uri in (
        "http://example.com/cb",
        "https://user:pass@example.com/cb",
        "https://example.com/cb#fragment",
        "https://example.com/a b",
    ):
        with pytest.raises(ValueError):
            valid_redirect(uri)
    with pytest.raises(ValueError):
        OAuthClient("c", "a", (), ("read",))
    with pytest.raises(ValueError):
        SQLiteAuthenticationRepository(":memory:")
    repo = SQLiteAuthenticationRepository(tmp_path / "auth.sqlite3")
    auth = AuthenticationService(repo, clock=lambda: 100)
    for ttl in (0, -1, float("nan"), 2592001, True):
        with pytest.raises(ValueError):
            auth.issue(Principal("alice", "human"), tenant_id="a", ttl_seconds=ttl)
    with pytest.raises(AuthenticationDenied):
        auth.authenticate("unknown", tenant_id="a")
    oauth = OAuthAuthorizationService(repo, auth, clock=lambda: 100)
    oauth.register_client(OAuthClient("web", "a", ("https://example.com/cb",), ("read",)))
    args = {
        "client_id": "web",
        "tenant_id": "a",
        "principal": Principal("alice", "human", scopes=("read",)),
        "redirect_uri": "https://example.com/cb",
        "state": "long-random-state",
        "challenge": pkce_challenge("a" * 43),
        "scopes": ("read",),
        "consented": True,
    }
    with pytest.raises(AuthenticationDenied):
        oauth.authorize(**{**args, "state": "short"})
    grant = oauth.authorize(**args)
    oauth.revoke_client("web", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        oauth.exchange(
            grant.code,
            client_id="web",
            tenant_id="a",
            redirect_uri="https://example.com/cb",
            verifier="a" * 43,
            state=grant.state,
        )
    with pytest.raises(AuthenticationDenied):
        oauth.exchange(
            "unknown-code",
            client_id="web",
            tenant_id="a",
            redirect_uri="https://example.com/cb",
            verifier="a" * 43,
            state=grant.state,
        )


def test_external_provider_errors_are_sanitized_and_link_takeovers_denied(tmp_path: Path) -> None:
    from af.application.authentication import ExternalLoginService
    from af.framework.authentication import ExternalIdentity

    class Provider:
        provider_id = "test"
        issuer = "https://identity.example"
        redirect_uris = ("https://af.example/cb",)
        failed = False

        def authorization_url(self, **kwargs: str) -> str:
            return "https://identity.example/auth"

        def exchange(self, **kwargs: str) -> ExternalIdentity:
            if self.failed:
                raise RuntimeError("private-token-secret")
            return ExternalIdentity(self.issuer, "subject")

    repo = SQLiteAuthenticationRepository(tmp_path / "auth.sqlite3")
    provider = Provider()
    with pytest.raises(ValueError):
        ExternalLoginService(repo, (provider, provider))
    service = ExternalLoginService(repo, (provider,), clock=lambda: 100)
    with pytest.raises(AuthenticationDenied):
        service.begin("missing", tenant_id="a", redirect_uri=provider.redirect_uris[0])
    for subject in ("alice", "bob"):
        principal = Principal(subject, "human")
        attempt = service.begin(
            "test", tenant_id="a", redirect_uri=provider.redirect_uris[0], link_to=principal
        )
        args = {
            "state": attempt.state,
            "verifier": attempt.verifier,
            "code": "code",
            "provider_id": "test",
            "tenant_id": "a",
            "redirect_uri": provider.redirect_uris[0],
            "consented": True,
            "link_to": principal,
        }
        if subject == "alice":
            assert service.complete(**args).subject_id == "alice"
        else:
            with pytest.raises(AuthenticationDenied):
                service.complete(**args)
    attempt = service.begin("test", tenant_id="a", redirect_uri=provider.redirect_uris[0])
    provider.failed = True
    with pytest.raises(AuthenticationDenied) as caught:
        service.complete(
            state=attempt.state,
            verifier=attempt.verifier,
            code="code",
            provider_id="test",
            tenant_id="a",
            redirect_uri=provider.redirect_uris[0],
            consented=True,
        )
    assert caught.value.__context__ is None and "private" not in str(caught.value)
