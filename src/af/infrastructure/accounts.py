"""Persistent accounts, fixed-cost password hashing and encrypted MFA seeds."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from af.framework.accounts import AccountChallenge, AccountRecord, checked_password
from af.framework.authentication import AuthenticationDenied, TokenRecord
from af.infrastructure.authentication import SQLiteAuthenticationRepository


class ScryptPasswordHasher:
    """Fixed OWASP scrypt N=2^17,r=8,p=1; stored values cannot alter work factors."""

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode(), salt=salt, n=131_072, r=8, p=1, dklen=32, maxmem=160 * 1024 * 1024
        )

    def hash(self, password: str) -> str:
        checked_password(password)
        salt = secrets.token_bytes(16)
        return "scrypt1$" + base64.b64encode(salt + self._derive(password, salt)).decode()

    def verify(self, password: str, digest: str) -> bool:
        if type(password) is not str or len(password) > 128 or not digest.startswith("scrypt1$"):
            return False
        try:
            stored = base64.b64decode(digest[8:], validate=True)
        except ValueError:
            return False
        if len(stored) != 48:
            return False
        return hmac.compare_digest(self._derive(password, stored[:16]), stored[16:])


class FernetSecretCipher:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def generate(cls) -> FernetSecretCipher:
        """Ephemeral test convenience; deployments persist a separately protected host key."""
        return cls(Fernet.generate_key())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except (InvalidToken, UnicodeError):
            raise AuthenticationDenied() from None


class SQLiteAccountRepository(SQLiteAuthenticationRepository):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS af_accounts (
                    subject_id TEXT NOT NULL, tenant_id TEXT NOT NULL, address TEXT NOT NULL,
                    password_hash TEXT NOT NULL, scopes TEXT NOT NULL, verified INTEGER NOT NULL,
                    active INTEGER NOT NULL, totp_ciphertext TEXT,
                    last_totp_counter INTEGER NOT NULL,
                    pending_totp TEXT, pending_expires REAL NOT NULL,
                    PRIMARY KEY(subject_id, tenant_id), UNIQUE(address, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS af_account_challenges (
                    digest TEXT PRIMARY KEY, subject_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    purpose TEXT NOT NULL, expires_at REAL NOT NULL, consumed INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_account_throttles (
                    attempt_key TEXT PRIMARY KEY, started_at REAL NOT NULL,
                    attempts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_account_recovery (
                    digest TEXT PRIMARY KEY, subject_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    consumed INTEGER NOT NULL
                );
            """)

    def add_account(self, record: AccountRecord) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO af_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.subject_id,
                    record.tenant_id,
                    record.address,
                    record.password_hash,
                    json.dumps(record.scopes),
                    int(record.verified),
                    int(record.active),
                    record.totp_ciphertext,
                    record.last_totp_counter,
                    record.pending_totp,
                    record.pending_expires,
                ),
            )
            return cursor.rowcount == 1

    def _find(self, column: str, value: str, tenant_id: str) -> AccountRecord | None:
        with self._connection() as db:
            row = db.execute(
                f"SELECT * FROM af_accounts WHERE {column}=? AND tenant_id=?", (value, tenant_id)
            ).fetchone()
        if row is None:
            return None
        return AccountRecord(
            row[0],
            row[1],
            row[2],
            row[3],
            tuple(json.loads(row[4])),
            bool(row[5]),
            bool(row[6]),
            row[7],
            row[8],
            row[9],
            row[10],
        )

    def find_account(self, address: str, tenant_id: str) -> AccountRecord | None:
        return self._find("address", address, tenant_id)

    def get_account(self, subject_id: str, tenant_id: str) -> AccountRecord | None:
        return self._find("subject_id", subject_id, tenant_id)

    def take_attempt(self, key: str, now: float, *, limit: int, window: float) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT started_at, attempts FROM af_account_throttles WHERE attempt_key=?", (key,)
            ).fetchone()
            if row is None or row[0] + window <= now:
                db.execute(
                    "INSERT OR REPLACE INTO af_account_throttles VALUES (?, ?, 1)", (key, now)
                )
                return True
            if row[1] >= limit:
                return False
            db.execute(
                "UPDATE af_account_throttles SET attempts=attempts+1 WHERE attempt_key=?", (key,)
            )
            return True

    def add_challenge(self, challenge: AccountChallenge) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE af_account_challenges SET consumed=1 WHERE subject_id=? AND "
                "tenant_id=? AND purpose=?",
                (challenge.subject_id, challenge.tenant_id, challenge.purpose),
            )
            db.execute(
                "INSERT INTO af_account_challenges VALUES (?, ?, ?, ?, ?, 0)",
                (
                    challenge.digest,
                    challenge.subject_id,
                    challenge.tenant_id,
                    challenge.purpose,
                    challenge.expires_at,
                ),
            )

    def consume_challenge(
        self,
        digest: str,
        tenant_id: str,
        purpose: str,
        now: float,
        password_hash: str | None = None,
    ) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT subject_id FROM af_account_challenges WHERE digest=? AND tenant_id=? "
                "AND purpose=? AND consumed=0 AND expires_at>?",
                (digest, tenant_id, purpose, now),
            ).fetchone()
            if row is None:
                return False
            db.execute("UPDATE af_account_challenges SET consumed=1 WHERE digest=?", (digest,))
            if purpose == "verify":
                return (
                    db.execute(
                        "UPDATE af_accounts SET verified=1 WHERE subject_id=? AND tenant_id=? "
                        "AND active=1",
                        (row[0], tenant_id),
                    ).rowcount
                    == 1
                )
            if purpose != "reset" or password_hash is None:
                return False
            cursor = db.execute(
                "UPDATE af_accounts SET password_hash=?, pending_totp=NULL, pending_expires=0 "
                "WHERE subject_id=? AND tenant_id=? AND active=1",
                (password_hash, row[0], tenant_id),
            )
            db.execute(
                "UPDATE af_auth_tokens SET revoked=1 WHERE tenant_id=? AND "
                "json_extract(principal, '$.subject_id')=?",
                (tenant_id, row[0]),
            )
            db.execute(
                "UPDATE af_auth_codes SET consumed=1 WHERE tenant_id=? AND "
                "json_extract(principal, '$.subject_id')=?",
                (tenant_id, row[0]),
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='af_oauth_refresh'"
            ).fetchone():
                db.execute(
                    "UPDATE af_oauth_refresh SET revoked=1 WHERE tenant_id=? AND "
                    "json_extract(principal, '$.subject_id')=?",
                    (tenant_id, row[0]),
                )
            return cursor.rowcount == 1

    def set_pending_totp(
        self, subject_id: str, tenant_id: str, ciphertext: str, expires_at: float
    ) -> bool:
        with self._connection() as db:
            return (
                db.execute(
                    "UPDATE af_accounts SET pending_totp=?, pending_expires=? WHERE "
                    "subject_id=? AND tenant_id=? AND totp_ciphertext IS NULL AND active=1 "
                    "AND verified=1",
                    (ciphertext, expires_at, subject_id, tenant_id),
                ).rowcount
                == 1
            )

    def enable_totp(
        self, account: AccountRecord, counter: int, recovery_hashes: tuple[str, ...], now: float
    ) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE af_accounts SET totp_ciphertext=pending_totp, pending_totp=NULL, "
                "pending_expires=0, last_totp_counter=? WHERE subject_id=? AND tenant_id=? "
                "AND pending_totp=? AND pending_expires>? AND totp_ciphertext IS NULL AND "
                "active=1 AND verified=1",
                (counter, account.subject_id, account.tenant_id, account.pending_totp, now),
            )
            if cursor.rowcount != 1:
                return False
            db.executemany(
                "INSERT INTO af_account_recovery VALUES (?, ?, ?, 0)",
                ((digest, account.subject_id, account.tenant_id) for digest in recovery_hashes),
            )
            return True

    def consume_totp(self, account: AccountRecord, counter: int) -> bool:
        with self._connection() as db:
            return (
                db.execute(
                    "UPDATE af_accounts SET last_totp_counter=? WHERE subject_id=? AND "
                    "tenant_id=? AND last_totp_counter<? AND totp_ciphertext=? AND active=1",
                    (
                        counter,
                        account.subject_id,
                        account.tenant_id,
                        counter,
                        account.totp_ciphertext,
                    ),
                ).rowcount
                == 1
            )

    def consume_recovery(self, subject_id: str, tenant_id: str, digest: str) -> bool:
        with self._connection() as db:
            return (
                db.execute(
                    "UPDATE af_account_recovery SET consumed=1 WHERE digest=? AND "
                    "subject_id=? AND tenant_id=? AND consumed=0",
                    (digest, subject_id, tenant_id),
                ).rowcount
                == 1
            )

    def issue_account_token(self, account: AccountRecord, token: TokenRecord) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT 1 FROM af_accounts WHERE subject_id=? AND tenant_id=? AND "
                "password_hash=? AND active=1 AND verified=1 AND totp_ciphertext IS ?",
                (
                    account.subject_id,
                    account.tenant_id,
                    account.password_hash,
                    account.totp_ciphertext,
                ),
            ).fetchone()
            if row is None:
                return False
            self._insert_token(db, token)
            return True
