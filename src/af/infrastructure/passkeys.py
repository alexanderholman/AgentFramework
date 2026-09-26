"""Optional maintained WebAuthn library adapter and SQLite ceremony persistence."""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlsplit

from af.framework.accounts import AccountRecord
from af.framework.authentication import AuthenticationDenied, TokenRecord, identity_value
from af.framework.passkeys import PasskeyChallenge, PasskeyCredential, VerifiedPasskey
from af.infrastructure.accounts import SQLiteAccountRepository
from af.infrastructure.provider_http import ProviderFailure, decode_json, object_map


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class WebAuthnVerifier:
    def __init__(self, *, rp_id: str, origin: str, rp_name: str = "AgentFramework") -> None:
        identity_value(rp_id)
        identity_value(rp_name)
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or parsed.hostname != rp_id
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("WebAuthn requires an exact HTTPS relying-party origin")
        self._rp_id, self._origin, self._rp_name = rp_id, origin.rstrip("/"), rp_name

    @staticmethod
    def _credential(value: str) -> str:
        if type(value) is not str or len(value.encode()) > 65_536:
            raise AuthenticationDenied()
        try:
            object_map(decode_json(value.encode()))
        except ProviderFailure:
            raise AuthenticationDenied() from None
        return value

    def registration_options(
        self, account: AccountRecord, challenge: bytes, existing: tuple[PasskeyCredential, ...]
    ) -> str:
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )

        return options_to_json(
            generate_registration_options(
                rp_id=self._rp_id,
                rp_name=self._rp_name,
                user_id=account.subject_id.encode(),
                user_name=account.address,
                challenge=challenge,
                timeout=300_000,
                exclude_credentials=[
                    PublicKeyCredentialDescriptor(id=_decode(item.credential_id))
                    for item in existing
                ],
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
            )
        )

    def authentication_options(
        self, challenge: bytes, existing: tuple[PasskeyCredential, ...]
    ) -> str:
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import (
            PublicKeyCredentialDescriptor,
            UserVerificationRequirement,
        )

        return options_to_json(
            generate_authentication_options(
                rp_id=self._rp_id,
                challenge=challenge,
                timeout=300_000,
                allow_credentials=[
                    PublicKeyCredentialDescriptor(id=_decode(item.credential_id))
                    for item in existing
                ],
                user_verification=UserVerificationRequirement.REQUIRED,
            )
        )

    def verify_registration(self, credential: str, challenge: bytes) -> VerifiedPasskey:
        from webauthn import verify_registration_response
        from webauthn.helpers.exceptions import WebAuthnException

        try:
            result = verify_registration_response(
                credential=self._credential(credential),
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError):
            raise AuthenticationDenied() from None
        return VerifiedPasskey(
            _encode(result.credential_id), result.credential_public_key, result.sign_count
        )

    def verify_authentication(
        self, credential: str, challenge: bytes, stored: PasskeyCredential
    ) -> int:
        from webauthn import verify_authentication_response
        from webauthn.helpers.exceptions import WebAuthnException

        try:
            result = verify_authentication_response(
                credential=self._credential(credential),
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                credential_public_key=stored.public_key,
                credential_current_sign_count=stored.sign_count,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError):
            raise AuthenticationDenied() from None
        if _encode(result.credential_id) != stored.credential_id:
            raise AuthenticationDenied()
        return result.new_sign_count


class SQLitePasskeyRepository(SQLiteAccountRepository):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS af_passkeys (
                    credential_id TEXT NOT NULL, subject_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    public_key BLOB NOT NULL, sign_count INTEGER NOT NULL,
                    PRIMARY KEY(credential_id, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS af_passkey_challenges (
                    challenge_id TEXT PRIMARY KEY, challenge BLOB NOT NULL,
                    subject_id TEXT NOT NULL, tenant_id TEXT NOT NULL, purpose TEXT NOT NULL,
                    expires_at REAL NOT NULL, consumed INTEGER NOT NULL
                );
            """)

    def list_passkeys(self, subject_id: str, tenant_id: str) -> tuple[PasskeyCredential, ...]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM af_passkeys WHERE subject_id=? AND tenant_id=?",
                (subject_id, tenant_id),
            ).fetchall()
        return tuple(PasskeyCredential(*row) for row in rows)

    def get_passkey(self, credential_id: str, tenant_id: str) -> PasskeyCredential | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM af_passkeys WHERE credential_id=? AND tenant_id=?",
                (credential_id, tenant_id),
            ).fetchone()
        return None if row is None else PasskeyCredential(*row)

    def add_passkey_challenge(self, challenge: PasskeyChallenge) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO af_passkey_challenges VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    challenge.challenge_id,
                    challenge.challenge,
                    challenge.subject_id,
                    challenge.tenant_id,
                    challenge.purpose,
                    challenge.expires_at,
                ),
            )

    def take_passkey_challenge(
        self, challenge_id: str, tenant_id: str, purpose: str, now: float
    ) -> PasskeyChallenge | None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM af_passkey_challenges WHERE challenge_id=? AND tenant_id=? AND "
                "purpose=? AND consumed=0 AND expires_at>?",
                (challenge_id, tenant_id, purpose, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE af_passkey_challenges SET consumed=1 WHERE challenge_id=?", (challenge_id,)
            )
            return PasskeyChallenge(*row[:6])

    def add_passkey(self, credential: PasskeyCredential) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT 1 FROM af_accounts WHERE subject_id=? AND tenant_id=? AND active=1 "
                "AND verified=1",
                (credential.subject_id, credential.tenant_id),
            ).fetchone()
            if row is None:
                return False
            return (
                db.execute(
                    "INSERT OR IGNORE INTO af_passkeys VALUES (?, ?, ?, ?, ?)",
                    (
                        credential.credential_id,
                        credential.subject_id,
                        credential.tenant_id,
                        credential.public_key,
                        credential.sign_count,
                    ),
                ).rowcount
                == 1
            )

    def issue_passkey_token(
        self, credential: PasskeyCredential, new_count: int, token: TokenRecord
    ) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT 1 FROM af_accounts WHERE subject_id=? AND tenant_id=? AND active=1 "
                "AND verified=1",
                (credential.subject_id, credential.tenant_id),
            ).fetchone()
            if row is None:
                return False
            cursor = db.execute(
                "UPDATE af_passkeys SET sign_count=? WHERE credential_id=? AND tenant_id=? "
                "AND sign_count=?",
                (new_count, credential.credential_id, credential.tenant_id, credential.sign_count),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_token(db, token)
            return True
