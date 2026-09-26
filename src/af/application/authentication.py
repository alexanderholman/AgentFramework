"""Scoped personal tokens and authorization-code S256 PKCE without a starter UI.

Trusted hosts own token issuance and client registration. Authentication never
converts a claimed identity, tenant, scope or role into authority by itself.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import replace
from math import isfinite
from uuid import uuid4

from af.framework.authentication import (
    AuthenticationDenied,
    AuthenticationRepository,
    AuthorizationCode,
    AuthorizationGrant,
    ExternalIdentity,
    ExternalIdentityProvider,
    ExternalIdentityRepository,
    ExternalLoginAttempt,
    ExternalLoginState,
    IssuedToken,
    OAuthClient,
    TokenRecord,
    checked_scopes,
    identity_value,
    pkce_challenge,
    secret_digest,
    valid_redirect,
)
from af.framework.context import Principal


class AuthenticationService:
    def __init__(
        self, repository: AuthenticationRepository, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._repository = repository
        self._clock = clock

    def prepare_token(
        self,
        principal: Principal,
        *,
        tenant_id: str,
        scopes: tuple[str, ...] | None = None,
        ttl_seconds: float = 3600,
    ) -> tuple[TokenRecord, IssuedToken]:
        identity_value(tenant_id)
        selected = checked_scopes(principal.scopes if scopes is None else scopes)
        if not set(selected) <= set(principal.scopes):
            raise AuthenticationDenied()
        if (
            type(ttl_seconds) not in (int, float)
            or not isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 2_592_000
        ):
            raise ValueError("token lifetime must be between zero and 30 days")
        now = self._clock()
        token_id = uuid4().hex
        secret = "af_" + token_id + "." + secrets.token_urlsafe(32)
        record = TokenRecord(
            token_id,
            secret_digest(secret),
            replace(principal, scopes=selected),
            tenant_id,
            now,
            now + ttl_seconds,
        )
        return record, IssuedToken(token_id, secret, record.expires_at, selected)

    def issue(
        self,
        principal: Principal,
        *,
        tenant_id: str,
        scopes: tuple[str, ...] | None = None,
        ttl_seconds: float = 3600,
    ) -> IssuedToken:
        record, issued = self.prepare_token(
            principal, tenant_id=tenant_id, scopes=scopes, ttl_seconds=ttl_seconds
        )
        self._repository.insert_token(record)
        return issued

    def _record(self, secret: str, tenant_id: str) -> TokenRecord:
        record = self._repository.find_token(secret_digest(secret))
        if (
            record is None
            or record.revoked
            or record.expires_at <= self._clock()
            or not hmac.compare_digest(record.tenant_id.encode(), tenant_id.encode())
        ):
            raise AuthenticationDenied()
        return record

    def authenticate(
        self, secret: str, *, tenant_id: str, required_scopes: tuple[str, ...] = ()
    ) -> Principal:
        record = self._record(secret, tenant_id)
        required = checked_scopes(required_scopes)
        if not set(required) <= set(record.principal.scopes):
            raise AuthenticationDenied()
        return record.principal

    def revoke(self, token_id: str, *, tenant_id: str) -> bool:
        return self._repository.revoke_token(token_id, tenant_id)

    def rotate(
        self,
        secret: str,
        *,
        tenant_id: str,
        scopes: tuple[str, ...] | None = None,
        ttl_seconds: float = 3600,
    ) -> IssuedToken:
        old = self._record(secret, tenant_id)
        remaining = old.expires_at - self._clock()
        record, issued = self.prepare_token(
            old.principal,
            tenant_id=tenant_id,
            scopes=scopes,
            ttl_seconds=min(ttl_seconds, remaining),
        )
        # prepare_token reads the clock again; retain the original absolute bound.
        expiry = min(record.expires_at, old.expires_at)
        record = replace(record, expires_at=expiry)
        issued = replace(issued, expires_at=expiry)
        if not self._repository.rotate_token(old, record, self._clock()):
            raise AuthenticationDenied()
        return issued


class OAuthAuthorizationService:
    """Explicitly registered public clients; authorization-code + S256 only.

    Exact registered redirect matching, scoped consent, tenant binding, short
    code expiry and atomic redemption follow RFC 7636 / RFC 9700. This is not an
    implicit/password grant or a blanket claim of an OAuth/OIDC server product.
    """

    def __init__(
        self,
        repository: AuthenticationRepository,
        authentication: AuthenticationService,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._authentication = authentication
        self._clock = clock

    def register_client(self, client: OAuthClient) -> None:
        self._repository.add_client(client)

    def revoke_client(self, client_id: str, *, tenant_id: str) -> None:
        self._repository.revoke_client(client_id, tenant_id)

    def authorize(
        self,
        *,
        client_id: str,
        tenant_id: str,
        principal: Principal,
        redirect_uri: str,
        state: str,
        challenge: str,
        scopes: tuple[str, ...],
        consented: bool,
    ) -> AuthorizationGrant:
        client = self._repository.get_client(client_id, tenant_id)
        selected = checked_scopes(scopes)
        if (
            client is None
            or not client.active
            or consented is not True
            or redirect_uri not in client.redirect_uris
            or not set(selected) <= set(client.scopes)
            or not set(selected) <= set(principal.scopes)
        ):
            raise AuthenticationDenied()
        if (
            type(state) is not str
            or not 16 <= len(state) <= 512
            or type(challenge) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) is None
        ):
            raise AuthenticationDenied()
        code = secrets.token_urlsafe(32)
        expires = self._clock() + 300
        self._repository.insert_code(
            AuthorizationCode(
                secret_digest(code),
                client_id,
                tenant_id,
                replace(principal, scopes=selected),
                redirect_uri,
                secret_digest(state),
                challenge,
                expires,
            )
        )
        return AuthorizationGrant(code, state, redirect_uri, expires)

    def exchange(
        self,
        code: str,
        *,
        client_id: str,
        tenant_id: str,
        redirect_uri: str,
        verifier: str,
        state: str,
    ) -> IssuedToken:
        stored = self._repository.find_code(secret_digest(code))
        client = self._repository.get_client(client_id, tenant_id)
        if (
            stored is None
            or stored.consumed
            or stored.expires_at <= self._clock()
            or client is None
            or not client.active
            or stored.client_id != client_id
            or stored.tenant_id != tenant_id
            or stored.redirect_uri != redirect_uri
            or not hmac.compare_digest(stored.challenge, pkce_challenge(verifier))
            or not hmac.compare_digest(stored.state_hash, secret_digest(state))
        ):
            raise AuthenticationDenied()
        token, issued = self._authentication.prepare_token(stored.principal, tenant_id=tenant_id)
        if not self._repository.redeem_code(stored, token, self._clock()):
            raise AuthenticationDenied()
        return issued


class ExternalLoginService:
    """Provider registry and callback/account-linking policy with no email matching.

    Provider adapters must verify their own token/assertion signatures, issuer
    and audience. A callback never inherits roles/scopes from remote claims;
    the host's local authorization policy decides access after identity lookup.
    """

    def __init__(
        self,
        repository: ExternalIdentityRepository,
        providers: tuple[ExternalIdentityProvider, ...],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._providers: dict[str, ExternalIdentityProvider] = {}
        for provider in providers:
            identity_value(provider.provider_id)
            identity_value(provider.issuer)
            if provider.provider_id in self._providers:
                raise ValueError("duplicate identity provider")
            for redirect in provider.redirect_uris:
                valid_redirect(redirect)
            self._providers[provider.provider_id] = provider

    def begin(
        self,
        provider_id: str,
        *,
        tenant_id: str,
        redirect_uri: str,
        link_to: Principal | None = None,
    ) -> ExternalLoginAttempt:
        identity_value(tenant_id)
        provider = self._providers.get(provider_id)
        if provider is None or redirect_uri not in provider.redirect_uris:
            raise AuthenticationDenied()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        challenge = pkce_challenge(verifier)
        expires = self._clock() + 300
        url = provider.authorization_url(
            state=state, challenge=challenge, redirect_uri=redirect_uri
        )
        self._repository.insert_login_state(
            ExternalLoginState(
                secret_digest(state),
                challenge,
                provider_id,
                tenant_id,
                redirect_uri,
                None if link_to is None else link_to.subject_id,
                expires,
            )
        )
        return ExternalLoginAttempt(state, verifier, url, expires)

    def complete(
        self,
        *,
        state: str,
        verifier: str,
        code: str,
        provider_id: str,
        tenant_id: str,
        redirect_uri: str,
        consented: bool,
        link_to: Principal | None = None,
    ) -> Principal:
        stored = self._repository.find_login_state(secret_digest(state))
        provider = self._providers.get(provider_id)
        requested_link = None if link_to is None else link_to.subject_id
        if (
            stored is None
            or stored.consumed
            or stored.expires_at <= self._clock()
            or provider is None
            or consented is not True
            or stored.provider_id != provider_id
            or stored.tenant_id != tenant_id
            or stored.redirect_uri != redirect_uri
            or stored.link_subject_id != requested_link
            or not hmac.compare_digest(stored.challenge, pkce_challenge(verifier))
        ):
            raise AuthenticationDenied()
        if type(code) is not str or not 1 <= len(code) <= 4096:
            raise AuthenticationDenied()
        if not self._repository.consume_login_state(stored.state_hash, self._clock()):
            raise AuthenticationDenied()
        # No raw provider exception may escape this boundary; consumed state
        # stays consumed even if the remote exchange times out or fails.
        failed = False
        identity: ExternalIdentity | None = None
        try:
            identity = provider.exchange(code=code, verifier=verifier, redirect_uri=redirect_uri)
        except Exception:
            failed = True
        if failed or type(identity) is not ExternalIdentity or identity.issuer != provider.issuer:
            raise AuthenticationDenied()
        subject = self._repository.resolve_external_subject(
            tenant_id=tenant_id,
            provider_id=provider_id,
            identity=identity,
            link_subject_id=requested_link,
        )
        return Principal(subject, "external")
