"""Optional headless OAuth grants; transports never own their authorization logic."""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

from af.application.authentication import AuthenticationService
from af.framework.authentication import (
    AuthenticationDenied,
    IssuedToken,
    checked_scopes,
    identity_value,
    secret_digest,
    valid_redirect,
)
from af.framework.context import Principal
from af.framework.oauth_grants import (
    ClientCredential,
    DeviceAuthorization,
    DeviceRecord,
    OAuthGrantError,
    OAuthGrantRepository,
    RefreshPair,
    RefreshRecord,
    ServiceClient,
)


class OAuthGrantService:
    def __init__(
        self,
        repository: OAuthGrantRepository,
        authentication: AuthenticationService,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._authentication = authentication
        self._clock = clock

    def register_service_client(
        self, client_id: str, *, tenant_id: str, scopes: tuple[str, ...]
    ) -> ClientCredential:
        identity_value(client_id)
        identity_value(tenant_id)
        allowed = checked_scopes(scopes)
        secret = secrets.token_urlsafe(32)
        self._repository.add_service_client(
            ServiceClient(client_id, tenant_id, secret_digest(secret), allowed)
        )
        return ClientCredential(client_id, secret)

    def revoke_service_client(self, client_id: str, *, tenant_id: str) -> None:
        self._repository.revoke_service_client(client_id, tenant_id)

    def client_credentials(
        self, client_id: str, secret: str, *, tenant_id: str, scopes: tuple[str, ...]
    ) -> IssuedToken:
        client = self._repository.get_service_client(client_id, tenant_id)
        selected = checked_scopes(scopes)
        digest = secret_digest(secret)
        if (
            client is None
            or not client.active
            or not hmac.compare_digest(client.secret_hash, digest)
            or not set(selected) <= set(client.scopes)
        ):
            raise AuthenticationDenied()
        token, issued = self._authentication.prepare_token(
            Principal(client_id, "service", scopes=selected), tenant_id=tenant_id
        )
        if not self._repository.insert_service_token(client, token):
            raise AuthenticationDenied()
        return issued

    def issue_refresh_pair(
        self,
        principal: Principal,
        *,
        tenant_id: str,
        client_id: str,
        scopes: tuple[str, ...] | None = None,
    ) -> RefreshPair:
        """Trusted host action after primary authentication and explicit client consent."""
        identity_value(client_id)
        client = self._repository.get_client(client_id, tenant_id)
        selected = checked_scopes(principal.scopes if scopes is None else scopes)
        if client is None or not client.active or not set(selected) <= set(client.scopes):
            raise AuthenticationDenied()
        token, issued = self._authentication.prepare_token(
            principal, tenant_id=tenant_id, scopes=selected
        )
        secret = secrets.token_urlsafe(32)
        refresh = RefreshRecord(
            secret_digest(secret),
            uuid4().hex,
            client_id,
            tenant_id,
            token.principal,
            self._clock() + 2_592_000,
        )
        self._repository.insert_refresh_pair(refresh, token)
        return RefreshPair(issued, secret, refresh.expires_at)

    def refresh(
        self, secret: str, *, tenant_id: str, client_id: str, scopes: tuple[str, ...] | None = None
    ) -> RefreshPair:
        old = self._repository.get_refresh(secret_digest(secret))
        client = self._repository.get_client(client_id, tenant_id)
        if (
            old is None
            or client is None
            or not client.active
            or old.tenant_id != tenant_id
            or old.client_id != client_id
            or old.revoked
            or old.expires_at <= self._clock()
        ):
            raise AuthenticationDenied()
        if old.consumed:
            self._repository.revoke_refresh_family(old.family_id, tenant_id)
            raise AuthenticationDenied()
        selected = old.principal.scopes if scopes is None else checked_scopes(scopes)
        if not set(selected) <= set(old.principal.scopes):
            raise AuthenticationDenied()
        token, issued = self._authentication.prepare_token(
            old.principal,
            tenant_id=tenant_id,
            scopes=selected,
            ttl_seconds=min(3600, old.expires_at - self._clock()),
        )
        new_secret = secrets.token_urlsafe(32)
        new = replace(old, secret_hash=secret_digest(new_secret), principal=token.principal)
        if not self._repository.rotate_refresh(old, new, token, self._clock()):
            raise AuthenticationDenied()
        return RefreshPair(issued, new_secret, new.expires_at)

    def revoke_refresh(self, secret: str, *, tenant_id: str, client_id: str) -> None:
        record = self._repository.get_refresh(secret_digest(secret))
        if record is None or record.tenant_id != tenant_id or record.client_id != client_id:
            raise AuthenticationDenied()
        self._repository.revoke_refresh_family(record.family_id, tenant_id)

    def start_device(
        self, client_id: str, *, tenant_id: str, scopes: tuple[str, ...], verification_uri: str
    ) -> DeviceAuthorization:
        client = self._repository.get_client(client_id, tenant_id)
        selected = checked_scopes(scopes)
        valid_redirect(verification_uri)
        if client is None or not client.active or not set(selected) <= set(client.scopes):
            raise AuthenticationDenied()
        device_code = secrets.token_urlsafe(32)
        alphabet = "BCDFGHJKLMNPQRSTVWXYZ23456789"
        user_code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
        self._repository.insert_device(
            DeviceRecord(
                secret_digest(device_code),
                secret_digest(user_code),
                client_id,
                tenant_id,
                selected,
                self._clock() + 600,
            )
        )
        return DeviceAuthorization(device_code, user_code, verification_uri)

    def approve_device(
        self, user_code: str, principal: Principal, *, tenant_id: str, consented: bool
    ) -> None:
        digest = secret_digest(user_code)
        record = self._repository.find_device(digest, user_code=True)
        if (
            record is None
            or record.tenant_id != tenant_id
            or not set(record.scopes) <= set(principal.scopes)
            or type(consented) is not bool
        ):
            raise AuthenticationDenied()
        if not self._repository.approve_device(
            digest, tenant_id, replace(principal, scopes=record.scopes), consented, self._clock()
        ):
            raise AuthenticationDenied()

    def poll_device(self, device_code: str, *, client_id: str, tenant_id: str) -> IssuedToken:
        digest = secret_digest(device_code)
        record = self._repository.find_device(digest)
        token = None
        issued = None
        if record is not None and record.status == "approved" and record.principal is not None:
            token, issued = self._authentication.prepare_token(
                record.principal, tenant_id=tenant_id
            )
        status = self._repository.poll_device(digest, client_id, tenant_id, self._clock(), token)
        if status != "issued" or issued is None:
            raise OAuthGrantError(status if status != "issued" else "invalid_grant")
        return issued
