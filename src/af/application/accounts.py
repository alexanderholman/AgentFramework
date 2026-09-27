"""Account registration, verification, password recovery and MFA as headless actions."""

from __future__ import annotations

import base64
import secrets
import time
from collections.abc import Callable
from uuid import uuid4

from af.application.authentication import AuthenticationService
from af.framework.accounts import (
    AccountChallenge,
    AccountNotifier,
    AccountRecord,
    AccountRepository,
    AccountResponse,
    PasswordHasher,
    SecretCipher,
    account_address,
    checked_password,
    totp_counter,
)
from af.framework.authentication import (
    AuthenticationDenied,
    IssuedToken,
    checked_scopes,
    identity_value,
    secret_digest,
)
from af.framework.context import Principal


class AccountService:
    def __init__(
        self,
        repository: AccountRepository,
        authentication: AuthenticationService,
        passwords: PasswordHasher,
        cipher: SecretCipher,
        notifier: AccountNotifier,
        *,
        default_scopes: tuple[str, ...] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository, self._authentication = repository, authentication
        self._passwords, self._cipher, self._notifier = passwords, cipher, notifier
        self._scopes, self._clock = checked_scopes(default_scopes), clock
        # Same expensive verification path for absent and present accounts.
        self._dummy_hash = passwords.hash(secrets.token_urlsafe(32))

    def _limit(self, address: str, tenant_id: str, action: str) -> None:
        identity_value(tenant_id)
        key = secret_digest(tenant_id + "\0" + action + "\0" + address)
        if not self._repository.take_attempt(key, self._clock(), limit=5, window=300):
            raise AuthenticationDenied()

    def _notify(self, account: AccountRecord, purpose: str) -> None:
        secret = secrets.token_urlsafe(32)
        self._repository.add_challenge(
            AccountChallenge(
                secret_digest(secret),
                account.subject_id,
                account.tenant_id,
                purpose,
                self._clock() + 900,
            )
        )
        failed = False
        try:
            self._notifier.deliver(account.address, purpose, secret)
        except Exception:
            failed = True
        if failed:
            raise AuthenticationDenied()

    def register(self, address: str, password: str, *, tenant_id: str) -> AccountResponse:
        address = account_address(address)
        self._limit(address, tenant_id, "register")
        record = AccountRecord(
            uuid4().hex,
            tenant_id,
            address,
            self._passwords.hash(checked_password(password)),
            self._scopes,
        )
        if self._repository.add_account(record):
            self._notify(record, "verify")
        return AccountResponse()

    def verify(self, secret: str, *, tenant_id: str) -> AccountResponse:
        if not self._repository.consume_challenge(
            secret_digest(secret), tenant_id, "verify", self._clock()
        ):
            raise AuthenticationDenied()
        return AccountResponse("verified")

    def request_reset(self, address: str, *, tenant_id: str) -> AccountResponse:
        address = account_address(address)
        self._limit(address, tenant_id, "reset")
        record = self._repository.find_account(address, tenant_id)
        if record is not None and record.active and record.verified:
            self._notify(record, "reset")
        return AccountResponse()

    def reset_password(self, secret: str, password: str, *, tenant_id: str) -> AccountResponse:
        digest = self._passwords.hash(checked_password(password))
        if not self._repository.consume_challenge(
            secret_digest(secret), tenant_id, "reset", self._clock(), digest
        ):
            raise AuthenticationDenied()
        return AccountResponse("reset")

    def _password_account(self, address: str, password: str, tenant_id: str) -> AccountRecord:
        address = account_address(address)
        self._limit(address, tenant_id, "login")
        if type(password) is not str or len(password) > 128:
            raise AuthenticationDenied()
        account = self._repository.find_account(address, tenant_id)
        valid = self._passwords.verify(
            password, self._dummy_hash if account is None else account.password_hash
        )
        if not valid or account is None or not account.active or not account.verified:
            raise AuthenticationDenied()
        return account

    def _factor(self, account: AccountRecord, factor: str | None) -> None:
        if account.totp_ciphertext is None:
            return
        if factor is None or type(factor) is not str or len(factor) > 128:
            raise AuthenticationDenied()
        if len(factor) == 6 and factor.isdecimal():
            counter = totp_counter(
                self._cipher.decrypt(account.totp_ciphertext),
                factor,
                self._clock(),
                account.last_totp_counter,
            )
            accepted = self._repository.consume_totp(account, counter)
        else:
            accepted = self._repository.consume_recovery(
                account.subject_id, account.tenant_id, secret_digest(factor)
            )
        if not accepted:
            raise AuthenticationDenied()

    def login(
        self, address: str, password: str, *, tenant_id: str, factor: str | None = None
    ) -> IssuedToken:
        account = self._password_account(address, password, tenant_id)
        self._factor(account, factor)
        token, issued = self._authentication.prepare_token(
            Principal(account.subject_id, "user", scopes=account.scopes), tenant_id=tenant_id
        )
        if not self._repository.issue_account_token(account, token):
            raise AuthenticationDenied()
        return issued

    def begin_totp(self, principal: Principal, password: str, *, tenant_id: str) -> str:
        account = self._repository.get_account(principal.subject_id, tenant_id)
        if account is None or account.totp_ciphertext is not None:
            raise AuthenticationDenied()
        self._password_account(account.address, password, tenant_id)
        secret = base64.b32encode(secrets.token_bytes(20)).decode()
        if not self._repository.set_pending_totp(
            account.subject_id, tenant_id, self._cipher.encrypt(secret), self._clock() + 300
        ):
            raise AuthenticationDenied()
        return secret

    def confirm_totp(self, principal: Principal, code: str, *, tenant_id: str) -> tuple[str, ...]:
        account = self._repository.get_account(principal.subject_id, tenant_id)
        if (
            account is None
            or account.pending_totp is None
            or account.pending_expires <= self._clock()
        ):
            raise AuthenticationDenied()
        self._limit(account.address, tenant_id, "totp-enroll")
        counter = totp_counter(self._cipher.decrypt(account.pending_totp), code, self._clock())
        recovery = tuple(secrets.token_urlsafe(16) for _ in range(10))
        if not self._repository.enable_totp(
            account, counter, tuple(secret_digest(value) for value in recovery), self._clock()
        ):
            raise AuthenticationDenied()
        return recovery

    def reauthenticate(
        self, principal: Principal, password: str, *, tenant_id: str, factor: str | None = None
    ) -> AccountRecord:
        account = self._repository.get_account(principal.subject_id, tenant_id)
        if account is None:
            raise AuthenticationDenied()
        account = self._password_account(account.address, password, tenant_id)
        self._factor(account, factor)
        return account

    def request_verification(self, address: str, *, tenant_id: str) -> AccountResponse:
        address = account_address(address)
        self._limit(address, tenant_id, "verify-send")
        record = self._repository.find_account(address, tenant_id)
        if record is not None and record.active and not record.verified:
            self._notify(record, "verify")
        return AccountResponse()
