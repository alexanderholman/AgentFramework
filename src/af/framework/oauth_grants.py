"""Typed optional OAuth client-credential, refresh and device grants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from af.framework.authentication import IssuedToken, OAuthClient, TokenRecord
from af.framework.context import Principal
from af.framework.errors import AFError


class OAuthGrantError(AFError):
    def __init__(self, code: str) -> None:
        if code not in {
            "authorization_pending",
            "slow_down",
            "access_denied",
            "expired_token",
            "invalid_grant",
        }:
            raise ValueError("invalid OAuth grant error")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ClientCredential:
    client_id: str
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class ServiceClient:
    client_id: str
    tenant_id: str
    secret_hash: str
    scopes: tuple[str, ...]
    active: bool = True


@dataclass(frozen=True, slots=True)
class RefreshPair:
    access: IssuedToken
    refresh_secret: str = field(repr=False)
    refresh_expires_at: float


@dataclass(frozen=True, slots=True, repr=False)
class RefreshRecord:
    secret_hash: str
    family_id: str
    client_id: str
    tenant_id: str
    principal: Principal
    expires_at: float
    consumed: bool = False
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    verification_uri: str
    expires_in: int = 600
    interval: int = 5


@dataclass(frozen=True, slots=True, repr=False)
class DeviceRecord:
    device_hash: str
    user_hash: str
    client_id: str
    tenant_id: str
    scopes: tuple[str, ...]
    expires_at: float
    interval: int = 5
    last_poll: float | None = None
    status: str = "pending"
    principal: Principal | None = None


class OAuthGrantRepository(Protocol):
    def add_service_client(self, client: ServiceClient) -> None: ...
    def get_service_client(self, client_id: str, tenant_id: str) -> ServiceClient | None: ...
    def revoke_service_client(self, client_id: str, tenant_id: str) -> None: ...
    def insert_service_token(self, client: ServiceClient, token: TokenRecord) -> bool: ...
    def insert_refresh_pair(self, refresh: RefreshRecord, access: TokenRecord) -> None: ...
    def get_refresh(self, digest: str) -> RefreshRecord | None: ...
    def rotate_refresh(
        self, old: RefreshRecord, new: RefreshRecord, access: TokenRecord, now: float
    ) -> bool: ...
    def revoke_refresh_family(self, family_id: str, tenant_id: str) -> None: ...
    def get_client(self, client_id: str, tenant_id: str) -> OAuthClient | None: ...
    def insert_device(self, record: DeviceRecord) -> None: ...
    def find_device(self, digest: str, *, user_code: bool = False) -> DeviceRecord | None: ...
    def approve_device(
        self, user_hash: str, tenant_id: str, principal: Principal, consented: bool, now: float
    ) -> bool: ...
    def poll_device(
        self,
        device_hash: str,
        client_id: str,
        tenant_id: str,
        now: float,
        token: TokenRecord | None,
    ) -> str: ...
