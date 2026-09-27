"""Account lifecycle is headless, tenant-scoped, durable and replay-resistant."""

from pathlib import Path

import pytest

from af.application.accounts import AccountService
from af.application.authentication import AuthenticationService
from af.framework.accounts import totp_code
from af.framework.authentication import AuthenticationDenied
from af.infrastructure.accounts import (
    FernetSecretCipher,
    ScryptPasswordHasher,
    SQLiteAccountRepository,
)


class Mail:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def deliver(self, address: str, kind: str, secret: str) -> None:
        self.messages.append((address, kind, secret))


def test_account_verification_login_reset_throttle_and_mfa(tmp_path: Path) -> None:
    now = [1000.0]
    repo = SQLiteAccountRepository(tmp_path / "db")
    mail = Mail()
    cipher = FernetSecretCipher.generate()
    auth = AuthenticationService(repo, clock=lambda: now[0])
    service = AccountService(
        repo,
        auth,
        ScryptPasswordHasher(),
        cipher,
        mail,
        clock=lambda: now[0],
        default_scopes=("af:read",),
    )
    assert (
        service.register("Alice@Example.com", "correct horse battery staple", tenant_id="a").status
        == "pending"
    )
    with pytest.raises(AuthenticationDenied):
        service.login("alice@example.com", "correct horse battery staple", tenant_id="a")
    verify = mail.messages[-1][2]
    service.verify(verify, tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.verify(verify, tenant_id="a")
    token = service.login("alice@example.com", "correct horse battery staple", tenant_id="a")
    principal = auth.authenticate(token.secret, tenant_id="a")
    secret = service.begin_totp(principal, "correct horse battery staple", tenant_id="a")
    recovery = service.confirm_totp(principal, totp_code(secret, int(now[0] // 30)), tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.login("alice@example.com", "correct horse battery staple", tenant_id="a")
    now[0] += 30
    second = service.login(
        "alice@example.com",
        "correct horse battery staple",
        tenant_id="a",
        factor=totp_code(secret, int(now[0] // 30)),
    )
    assert auth.authenticate(second.secret, tenant_id="a").scopes == ("af:read",)
    now[0] += 301
    service.request_reset("alice@example.com", tenant_id="a")
    reset = mail.messages[-1][2]
    service.reset_password(reset, "new strong password phrase", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(second.secret, tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.reset_password(reset, "new strong password phrase", tenant_id="a")
    assert service.login(
        "alice@example.com", "new strong password phrase", tenant_id="a", factor=recovery[0]
    ).secret
    with pytest.raises(AuthenticationDenied):
        service.login(
            "alice@example.com", "new strong password phrase", tenant_id="a", factor=recovery[0]
        )
    for _ in range(8):
        with pytest.raises(AuthenticationDenied):
            service.login("alice@example.com", "wrong password", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.login(
            "alice@example.com", "new strong password phrase", tenant_id="a", factor=recovery[1]
        )
    assert b"correct horse battery staple" not in (tmp_path / "db").read_bytes()
    assert secret.encode() not in (tmp_path / "db").read_bytes()


def test_password_reset_invalidates_oauth_refresh_authority(tmp_path: Path) -> None:
    from af.application.oauth_grants import OAuthGrantService
    from af.framework.authentication import OAuthClient
    from af.infrastructure.oauth_grants import SQLiteOAuthGrantRepository

    class FastHasher:
        def hash(self, value: str) -> str:
            return "test:" + value

        def verify(self, value: str, digest: str) -> bool:
            return digest == self.hash(value)

    path = tmp_path / "db"
    repo = SQLiteAccountRepository(path)
    auth = AuthenticationService(repo)
    mail = Mail()
    accounts = AccountService(
        repo, auth, FastHasher(), FernetSecretCipher.generate(), mail, default_scopes=("read",)
    )
    accounts.register("alice@example.com", "a sufficiently long password", tenant_id="a")
    accounts.verify(mail.messages[-1][2], tenant_id="a")
    token = accounts.login("alice@example.com", "a sufficiently long password", tenant_id="a")
    principal = auth.authenticate(token.secret, tenant_id="a")
    oauth = SQLiteOAuthGrantRepository(path)
    oauth.add_client(OAuthClient("web", "a", ("https://af.example/cb",), ("read",)))
    grants = OAuthGrantService(oauth, auth)
    pair = grants.issue_refresh_pair(principal, tenant_id="a", client_id="web")
    accounts.request_reset("alice@example.com", tenant_id="a")
    accounts.reset_password(
        mail.messages[-1][2], "a different sufficiently long password", tenant_id="a"
    )
    with pytest.raises(AuthenticationDenied):
        grants.refresh(pair.refresh_secret, tenant_id="a", client_id="web")
