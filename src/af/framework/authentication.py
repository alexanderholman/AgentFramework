"""Headless identity and scoped credential contracts independent of transports."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from af.framework.context import Principal
from af.framework.errors import AFError


class AuthenticationDenied(AFError):
    def __init__(self) -> None:
        super().__init__("authentication or authorization denied")


def identity_value(value: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("invalid identity value")


def checked_scopes(scopes: tuple[str, ...]) -> tuple[str, ...]:
    if type(scopes) is not tuple or len(scopes) > 64 or len(set(scopes)) != len(scopes):
        raise ValueError("invalid token scopes")
    for scope in scopes:
        if (
            type(scope) is not str
            or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9:._-]{0,63}", scope) is None
        ):
            raise ValueError("invalid token scope")
    return tuple(sorted(scopes))


def secret_digest(secret: str) -> str:
    if type(secret) is not str or not 1 <= len(secret) <= 1024:
        raise AuthenticationDenied()
    return hashlib.sha256(secret.encode()).hexdigest()


def pkce_challenge(verifier: str) -> str:
    if type(verifier) is not str or re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) is None:
        raise AuthenticationDenied()
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def valid_redirect(uri: str) -> None:
    if type(uri) is not str or len(uri) > 2048 or any(char.isspace() for char in uri):
        raise ValueError("invalid OAuth redirect")
    parsed = urlsplit(uri)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.hostname
    ):
        raise ValueError("invalid OAuth redirect")
    if parsed.scheme == "https":
        return
    try:
        if parsed.scheme == "http" and ipaddress.ip_address(parsed.hostname).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("OAuth redirects require HTTPS or an explicit loopback HTTP address")


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token_id: str
    secret: str = field(repr=False)
    expires_at: float
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class TokenRecord:
    token_id: str
    secret_hash: str
    principal: Principal
    tenant_id: str
    issued_at: float
    expires_at: float
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    tenant_id: str
    redirect_uris: tuple[str, ...]
    scopes: tuple[str, ...]
    active: bool = True

    def __post_init__(self) -> None:
        identity_value(self.client_id)
        identity_value(self.tenant_id)
        checked_scopes(self.scopes)
        if (
            type(self.redirect_uris) is not tuple
            or not 1 <= len(self.redirect_uris) <= 16
            or len(set(self.redirect_uris)) != len(self.redirect_uris)
        ):
            raise ValueError("invalid registered OAuth redirects")
        for uri in self.redirect_uris:
            valid_redirect(uri)
        if type(self.active) is not bool:
            raise ValueError("invalid OAuth client status")


@dataclass(frozen=True, slots=True)
class AuthorizationGrant:
    code: str = field(repr=False)
    state: str = field(repr=False)
    redirect_uri: str
    expires_at: float


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizationCode:
    code_hash: str
    client_id: str
    tenant_id: str
    principal: Principal
    redirect_uri: str
    state_hash: str
    challenge: str
    expires_at: float
    consumed: bool = False


class AuthenticationRepository(Protocol):
    def insert_token(self, record: TokenRecord) -> None: ...
    def find_token(self, digest: str) -> TokenRecord | None: ...
    def revoke_token(self, token_id: str, tenant_id: str) -> bool: ...
    def rotate_token(self, old: TokenRecord, new: TokenRecord, now: float) -> bool: ...
    def add_client(self, client: OAuthClient) -> None: ...
    def get_client(self, client_id: str, tenant_id: str) -> OAuthClient | None: ...
    def revoke_client(self, client_id: str, tenant_id: str) -> None: ...
    def insert_code(self, code: AuthorizationCode) -> None: ...
    def find_code(self, digest: str) -> AuthorizationCode | None: ...
    def redeem_code(self, code: AuthorizationCode, token: TokenRecord, now: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """Verified assertion returned by an explicitly installed identity adapter."""

    issuer: str
    subject: str

    def __post_init__(self) -> None:
        identity_value(self.issuer)
        identity_value(self.subject)


@dataclass(frozen=True, slots=True, repr=False)
class ExternalLoginState:
    state_hash: str
    challenge: str
    provider_id: str
    tenant_id: str
    redirect_uri: str
    link_subject_id: str | None
    expires_at: float
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class ExternalLoginAttempt:
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    authorization_url: str = field(repr=False)
    expires_at: float


class ExternalIdentityProvider(Protocol):
    provider_id: str
    issuer: str
    redirect_uris: tuple[str, ...]

    def authorization_url(self, *, state: str, challenge: str, redirect_uri: str) -> str: ...
    def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> ExternalIdentity: ...


class ExternalIdentityRepository(Protocol):
    def insert_login_state(self, state: ExternalLoginState) -> None: ...
    def find_login_state(self, digest: str) -> ExternalLoginState | None: ...
    def consume_login_state(self, digest: str, now: float) -> bool: ...
    def resolve_external_subject(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        identity: ExternalIdentity,
        link_subject_id: str | None,
    ) -> str: ...
