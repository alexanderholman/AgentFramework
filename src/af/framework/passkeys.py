"""Provider-neutral WebAuthn ceremony, credential and durable replay contracts."""

from dataclasses import dataclass
from typing import Protocol

from af.framework.accounts import AccountRecord
from af.framework.authentication import TokenRecord


@dataclass(frozen=True, slots=True)
class PasskeyCredential:
    credential_id: str
    subject_id: str
    tenant_id: str
    public_key: bytes
    sign_count: int


@dataclass(frozen=True, slots=True)
class PasskeyChallenge:
    challenge_id: str
    challenge: bytes
    subject_id: str
    tenant_id: str
    purpose: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class PasskeyOptions:
    challenge_id: str
    options_json: str


@dataclass(frozen=True, slots=True)
class VerifiedPasskey:
    credential_id: str
    public_key: bytes
    sign_count: int


class PasskeyVerifier(Protocol):
    def registration_options(
        self, account: AccountRecord, challenge: bytes, existing: tuple[PasskeyCredential, ...]
    ) -> str: ...
    def authentication_options(
        self, challenge: bytes, existing: tuple[PasskeyCredential, ...]
    ) -> str: ...
    def verify_registration(self, credential: str, challenge: bytes) -> VerifiedPasskey: ...
    def verify_authentication(
        self, credential: str, challenge: bytes, stored: PasskeyCredential
    ) -> int: ...


class PasskeyRepository(Protocol):
    def find_account(self, address: str, tenant_id: str) -> AccountRecord | None: ...
    def get_account(self, subject_id: str, tenant_id: str) -> AccountRecord | None: ...
    def take_attempt(self, key: str, now: float, *, limit: int, window: float) -> bool: ...
    def list_passkeys(self, subject_id: str, tenant_id: str) -> tuple[PasskeyCredential, ...]: ...
    def get_passkey(self, credential_id: str, tenant_id: str) -> PasskeyCredential | None: ...
    def add_passkey_challenge(self, challenge: PasskeyChallenge) -> None: ...
    def take_passkey_challenge(
        self, challenge_id: str, tenant_id: str, purpose: str, now: float
    ) -> PasskeyChallenge | None: ...
    def add_passkey(self, credential: PasskeyCredential) -> bool: ...
    def issue_passkey_token(
        self, credential: PasskeyCredential, new_count: int, token: TokenRecord
    ) -> bool: ...
