"""Headless passkey ceremonies with durable challenges and atomic counter updates."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from uuid import uuid4

from af.application.accounts import AccountService
from af.application.authentication import AuthenticationService
from af.framework.accounts import AccountRecord, account_address
from af.framework.authentication import AuthenticationDenied, IssuedToken, secret_digest
from af.framework.context import Principal
from af.framework.passkeys import (
    PasskeyChallenge,
    PasskeyCredential,
    PasskeyOptions,
    PasskeyRepository,
    PasskeyVerifier,
)


class PasskeyService:
    def __init__(
        self,
        repository: PasskeyRepository,
        authentication: AuthenticationService,
        accounts: AccountService,
        verifier: PasskeyVerifier,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository, self._authentication, self._accounts = (
            repository,
            authentication,
            accounts,
        )
        self._verifier, self._clock = verifier, clock

    def _begin(self, account: AccountRecord, purpose: str) -> PasskeyOptions:
        existing = self._repository.list_passkeys(account.subject_id, account.tenant_id)
        if len(existing) >= 16 and purpose == "register":
            raise AuthenticationDenied()
        challenge = PasskeyChallenge(
            uuid4().hex,
            secrets.token_bytes(32),
            account.subject_id,
            account.tenant_id,
            purpose,
            self._clock() + 300,
        )
        if purpose == "register":
            options = self._verifier.registration_options(account, challenge.challenge, existing)
        else:
            options = self._verifier.authentication_options(challenge.challenge, existing)
        self._repository.add_passkey_challenge(challenge)
        return PasskeyOptions(challenge.challenge_id, options)

    def begin_registration(
        self, principal: Principal, password: str, *, tenant_id: str, factor: str | None = None
    ) -> PasskeyOptions:
        account = self._accounts.reauthenticate(
            principal, password, tenant_id=tenant_id, factor=factor
        )
        return self._begin(account, "register")

    def complete_registration(
        self, challenge_id: str, credential: str, principal: Principal, *, tenant_id: str
    ) -> None:
        challenge = self._repository.take_passkey_challenge(
            challenge_id, tenant_id, "register", self._clock()
        )
        if challenge is None or challenge.subject_id != principal.subject_id:
            raise AuthenticationDenied()
        proof = self._verifier.verify_registration(credential, challenge.challenge)
        if not self._repository.add_passkey(
            PasskeyCredential(
                proof.credential_id,
                principal.subject_id,
                tenant_id,
                proof.public_key,
                proof.sign_count,
            )
        ):
            raise AuthenticationDenied()

    def begin_authentication(self, address: str, *, tenant_id: str) -> PasskeyOptions:
        address = account_address(address)
        if not self._repository.take_attempt(
            secret_digest(tenant_id + "\0passkey\0" + address), self._clock(), limit=5, window=300
        ):
            raise AuthenticationDenied()
        account = self._repository.find_account(address, tenant_id)
        if account is None or not account.active or not account.verified:
            raise AuthenticationDenied()
        return self._begin(account, "authenticate")

    def complete_authentication(
        self, challenge_id: str, credential_id: str, credential: str, *, tenant_id: str
    ) -> IssuedToken:
        challenge = self._repository.take_passkey_challenge(
            challenge_id, tenant_id, "authenticate", self._clock()
        )
        stored = self._repository.get_passkey(credential_id, tenant_id)
        if challenge is None or stored is None or challenge.subject_id != stored.subject_id:
            raise AuthenticationDenied()
        new_count = self._verifier.verify_authentication(credential, challenge.challenge, stored)
        if (
            type(new_count) is not int
            or new_count < 0
            or ((new_count or stored.sign_count) and new_count <= stored.sign_count)
        ):
            raise AuthenticationDenied()
        account = self._repository.get_account(stored.subject_id, tenant_id)
        if account is None or not account.active or not account.verified:
            raise AuthenticationDenied()
        token, issued = self._authentication.prepare_token(
            Principal(account.subject_id, "user", scopes=account.scopes), tenant_id=tenant_id
        )
        if not self._repository.issue_passkey_token(stored, new_count, token):
            raise AuthenticationDenied()
        return issued
