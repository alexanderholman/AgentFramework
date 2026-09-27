"""Headless account lifecycle ports, bounded credentials and RFC 6238 factors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
from dataclasses import dataclass
from typing import Protocol

from af.framework.authentication import AuthenticationDenied, TokenRecord


def account_address(value: str) -> str:
    if (
        type(value) is not str
        or not 3 <= len(value) <= 254
        or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is None
    ):
        raise ValueError("invalid account address")
    return value.casefold()


def checked_password(value: str) -> str:
    if type(value) is not str or not 15 <= len(value) <= 128 or "\0" in value:
        raise ValueError("password must contain 15 to 128 characters")
    return value


def totp_code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret, casefold=False)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_counter(secret: str, code: str, now: float, last: int = -1) -> int:
    if type(code) is not str or re.fullmatch(r"[0-9]{6}", code) is None:
        raise AuthenticationDenied()
    current = int(now // 30)
    for counter in (current, current - 1, current + 1):
        if (
            counter >= 0
            and counter > last
            and hmac.compare_digest(totp_code(secret, counter), code)
        ):
            return counter
    raise AuthenticationDenied()


@dataclass(frozen=True, slots=True)
class AccountResponse:
    status: str = "pending"


@dataclass(frozen=True, slots=True, repr=False)
class AccountRecord:
    subject_id: str
    tenant_id: str
    address: str
    password_hash: str
    scopes: tuple[str, ...]
    verified: bool = False
    active: bool = True
    totp_ciphertext: str | None = None
    last_totp_counter: int = -1
    pending_totp: str | None = None
    pending_expires: float = 0


@dataclass(frozen=True, slots=True, repr=False)
class AccountChallenge:
    digest: str
    subject_id: str
    tenant_id: str
    purpose: str
    expires_at: float


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, password: str, digest: str) -> bool: ...


class SecretCipher(Protocol):
    def encrypt(self, value: str) -> str: ...
    def decrypt(self, value: str) -> str: ...


class AccountNotifier(Protocol):
    def deliver(self, address: str, kind: str, secret: str) -> None: ...


class AccountRepository(Protocol):
    def add_account(self, record: AccountRecord) -> bool: ...
    def find_account(self, address: str, tenant_id: str) -> AccountRecord | None: ...
    def get_account(self, subject_id: str, tenant_id: str) -> AccountRecord | None: ...
    def take_attempt(self, key: str, now: float, *, limit: int, window: float) -> bool: ...
    def add_challenge(self, challenge: AccountChallenge) -> None: ...
    def consume_challenge(
        self,
        digest: str,
        tenant_id: str,
        purpose: str,
        now: float,
        password_hash: str | None = None,
    ) -> bool: ...
    def set_pending_totp(
        self, subject_id: str, tenant_id: str, ciphertext: str, expires_at: float
    ) -> bool: ...
    def enable_totp(
        self, account: AccountRecord, counter: int, recovery_hashes: tuple[str, ...], now: float
    ) -> bool: ...
    def consume_totp(self, account: AccountRecord, counter: int) -> bool: ...
    def consume_recovery(self, subject_id: str, tenant_id: str, digest: str) -> bool: ...

    def issue_account_token(self, account: AccountRecord, token: TokenRecord) -> bool: ...


@dataclass(frozen=True, slots=True)
class AccountNotification:
    message_id: str
    kind: str
    status: str
    created_at: float


@dataclass(frozen=True, slots=True, repr=False)
class AccountNotificationContent:
    address: str
    kind: str
    secret: str


class AccountNotificationStore(Protocol):
    def pending(self) -> tuple[AccountNotification, ...]: ...
    def export(self, message_id: str) -> AccountNotificationContent: ...
    def dispatch(self, message_id: str) -> str: ...
