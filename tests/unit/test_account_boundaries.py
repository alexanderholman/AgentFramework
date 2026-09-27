"""Failure, replay and disclosure boundaries for headless accounts and delivery."""

from dataclasses import replace
from pathlib import Path

import pytest

from af.application.account_controller import AccountController
from af.application.accounts import AccountService
from af.application.authentication import AuthenticationService
from af.communications.contracts import Delivery, DeliveryReceipt, DeliveryState
from af.communications.delivery import MailManager, RecordingChannel
from af.framework.accounts import account_address, checked_password, totp_code, totp_counter
from af.framework.authentication import AuthenticationDenied
from af.framework.context import Principal
from af.infrastructure.account_notifications import SQLiteAccountNotificationOutbox
from af.infrastructure.accounts import (
    FernetSecretCipher,
    ScryptPasswordHasher,
    SQLiteAccountRepository,
)


class FastHasher:
    def hash(self, value: str) -> str:
        return "test:" + value

    def verify(self, value: str, digest: str) -> bool:
        return digest == self.hash(value)


def test_account_boundaries_and_controller_mfa(tmp_path: Path) -> None:
    now = [1000.0]
    repo = SQLiteAccountRepository(tmp_path / "db")
    auth = AuthenticationService(repo, clock=lambda: now[0])
    cipher = FernetSecretCipher.generate()
    outbox = SQLiteAccountNotificationOutbox(tmp_path / "db", cipher, tenant_id="a")
    service = AccountService(repo, auth, FastHasher(), cipher, outbox, clock=lambda: now[0])
    control = AccountController(service, auth, outbox, tenant_id="a")
    password = "a sufficiently long password"
    pending = service.register("alice@example.com", password, tenant_id="a")
    assert service.register("alice@example.com", password, tenant_id="a") == pending
    assert len(outbox.pending()) == 1
    service.request_reset("unknown@example.com", tenant_id="a")
    service.request_verification("unknown@example.com", tenant_id="a")
    code = outbox.export(outbox.pending()[0].message_id).secret
    with pytest.raises(AuthenticationDenied):
        service.verify(code, tenant_id="wrong")
    service.verify(code, tenant_id="a")
    token = service.login("alice@example.com", password, tenant_id="a")
    principal = auth.authenticate(token.secret, tenant_id="a")
    totp = control.run("totp-begin", {"principal_secret": token.secret, "password": password})[
        "totp_secret"
    ]
    assert isinstance(totp, str)
    for code in ("bad", "999999"):
        with pytest.raises(AuthenticationDenied):
            service.confirm_totp(principal, code, tenant_id="a")
    recovered = control.run(
        "totp-confirm",
        {"principal_secret": token.secret, "code": totp_code(totp, int(now[0] // 30))},
    )
    assert len(recovered["recovery_codes"]) == 10
    with pytest.raises(AuthenticationDenied):
        service.begin_totp(principal, password, tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.confirm_totp(principal, "000000", tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.reauthenticate(Principal("missing", "user"), password, tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.login(
            "alice@example.com", password, tenant_id="a", factor=totp_code(totp, int(now[0] // 30))
        )
    now[0] += 301
    with pytest.raises(AuthenticationDenied):
        service.login("missing@example.com", password, tenant_id="a")
    with pytest.raises(AuthenticationDenied):
        service.login("alice@example.com", "x" * 129, tenant_id="a")
    for action, payload in (
        ("unknown", {}),
        ("verify", {"secret": True}),
        ("notification-send", {"message_id": "x", "consented": False}),
        ("passkey-login-begin", {"address": "alice@example.com"}),
    ):
        with pytest.raises((AuthenticationDenied, ValueError)):
            control.run(action, payload)
    control.cancel_output("login", {"token_id": token.token_id})
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(token.secret, tenant_id="a")


def test_account_notification_submission_requires_explicit_transport_and_records_uncertainty(
    tmp_path: Path,
) -> None:
    cipher = FernetSecretCipher.generate()
    path = tmp_path / "db"
    with pytest.raises(ValueError):
        SQLiteAccountNotificationOutbox(path, cipher, tenant_id="a", mail_driver="smtp")
    outbox = SQLiteAccountNotificationOutbox(path, cipher, tenant_id="a")
    with pytest.raises(ValueError):
        outbox.deliver("a@example.com", "other", "secret")
    outbox.deliver("a@example.com", "verify", "one-time-private")
    item = outbox.pending()[0]
    with pytest.raises(AuthenticationDenied):
        outbox.dispatch(item.message_id)
    other = SQLiteAccountNotificationOutbox(path, cipher, tenant_id="b")
    with pytest.raises(AuthenticationDenied):
        other.export(item.message_id)
    recorder = RecordingChannel()
    send = SQLiteAccountNotificationOutbox(
        path,
        cipher,
        tenant_id="a",
        mail=MailManager({"smtp": recorder}, default="smtp"),
        mail_driver="smtp",
    )
    assert send.dispatch(item.message_id) == "accepted"
    assert recorder.deliveries[0].body.endswith("one-time-private")
    with pytest.raises(AuthenticationDenied):
        send.export(item.message_id)

    class Broken:
        def send(self, delivery: Delivery) -> DeliveryReceipt:
            raise OSError("provider secret must not escape")

    unknown = SQLiteAccountNotificationOutbox(
        path,
        cipher,
        tenant_id="a",
        mail=MailManager({"smtp": Broken()}, default="smtp"),
        mail_driver="smtp",
    )
    unknown.deliver("a@example.com", "reset", "second-private")
    pending = next(row for row in unknown.pending() if row.status == "pending")
    assert unknown.dispatch(pending.message_id) == DeliveryState.UNKNOWN.value
    assert b"one-time-private" not in path.read_bytes()


def test_password_cipher_validation_and_repository_compare_and_swap(tmp_path: Path) -> None:
    for address in ("", "a", "a@b", "a\n@b.com"):
        with pytest.raises(ValueError):
            account_address(address)
    for password in ("short", "x" * 129, "\0" * 20):
        with pytest.raises(ValueError):
            checked_password(password)
    hasher = ScryptPasswordHasher()
    for malformed in ("bad", "scrypt1$!", "scrypt1$YQ=="):
        assert not hasher.verify("password", malformed)
    cipher = FernetSecretCipher.generate()
    with pytest.raises(AuthenticationDenied):
        cipher.decrypt("invalid ciphertext")
    repo = SQLiteAccountRepository(tmp_path / "db")
    auth = AuthenticationService(repo)
    outbox = SQLiteAccountNotificationOutbox(tmp_path / "db", cipher, tenant_id="a")
    accounts = AccountService(repo, auth, FastHasher(), cipher, outbox)
    accounts.register("a@example.com", "a sufficiently long password", tenant_id="a")
    accounts.verify(outbox.export(outbox.pending()[0].message_id).secret, tenant_id="a")
    account = repo.find_account("a@example.com", "a")
    assert account is not None
    token, _ = auth.prepare_token(Principal(account.subject_id, "user"), tenant_id="a")
    assert not repo.issue_account_token(replace(account, password_hash="stale"), token)
    assert not repo.enable_totp(replace(account, pending_totp="stale"), 1, (), 0)
    assert not repo.set_pending_totp("missing", "a", "ciphertext", 100)
    assert not repo.consume_challenge("unknown", "a", "reset", 0)
    with pytest.raises(AuthenticationDenied):
        totp_counter("JBSWY3DPEHPK3PXP", "000000", 1000)
