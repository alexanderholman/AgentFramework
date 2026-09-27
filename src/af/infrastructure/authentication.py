"""SQLite identity authority: only credential hashes are written to disk."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from af.framework.authentication import (
    AuthenticationDenied,
    AuthorizationCode,
    ExternalIdentity,
    ExternalLoginState,
    OAuthClient,
    TokenRecord,
)
from af.framework.context import Principal


def _principal(data: str) -> Principal:
    value = json.loads(data)
    return Principal(
        value["subject_id"],
        value["principal_type"],
        tuple(value["roles"]),
        tuple(value["scopes"]),
        tuple(tuple(item) for item in value["claims"]),
    )


class SQLiteAuthenticationRepository:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path == ":memory:":
            raise ValueError("authentication requires a persistent database path")
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS af_auth_external_states (
                    state_hash TEXT PRIMARY KEY, challenge TEXT NOT NULL,
                    provider_id TEXT NOT NULL, tenant_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
                    link_subject_id TEXT, expires_at REAL NOT NULL, consumed INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_auth_external_links (
                    tenant_id TEXT NOT NULL, provider_id TEXT NOT NULL, issuer TEXT NOT NULL,
                    external_subject TEXT NOT NULL, subject_id TEXT NOT NULL,
                    PRIMARY KEY(tenant_id, provider_id, issuer, external_subject)
                );
                CREATE TABLE IF NOT EXISTS af_auth_tokens (
                    token_id TEXT PRIMARY KEY, secret_hash TEXT UNIQUE NOT NULL,
                    principal TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    issued_at REAL NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_auth_clients (
                    client_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    redirects TEXT NOT NULL, scopes TEXT NOT NULL, active INTEGER NOT NULL,
                    PRIMARY KEY(client_id, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS af_auth_token_rotations (
                    parent_token_id TEXT NOT NULL, child_token_id TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS af_auth_client_tokens (
                    token_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, tenant_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_auth_codes (
                    code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    principal TEXT NOT NULL, redirect_uri TEXT NOT NULL, state_hash TEXT NOT NULL,
                    challenge TEXT NOT NULL, expires_at REAL NOT NULL, consumed INTEGER NOT NULL
                );
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=30)) as db, db:
            yield db

    @staticmethod
    def _insert_token(db: sqlite3.Connection, record: TokenRecord) -> None:
        db.execute(
            "INSERT INTO af_auth_tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.token_id,
                record.secret_hash,
                json.dumps(asdict(record.principal), sort_keys=True),
                record.tenant_id,
                record.issued_at,
                record.expires_at,
                int(record.revoked),
            ),
        )

    def insert_token(self, record: TokenRecord) -> None:
        with self._connection() as db:
            self._insert_token(db, record)

    def find_token(self, digest: str) -> TokenRecord | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM af_auth_tokens WHERE secret_hash=?", (digest,)
            ).fetchone()
        if row is None:
            return None
        return TokenRecord(row[0], row[1], _principal(row[2]), row[3], row[4], row[5], bool(row[6]))

    def revoke_token(self, token_id: str, tenant_id: str) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE af_auth_tokens SET revoked=1 "
                "WHERE token_id=? AND tenant_id=? AND revoked=0",
                (token_id, tenant_id),
            )
            return cursor.rowcount == 1

    def rotate_token(self, old: TokenRecord, new: TokenRecord, now: float) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "UPDATE af_auth_tokens SET revoked=1 "
                "WHERE token_id=? AND secret_hash=? AND tenant_id=? AND revoked=0 AND expires_at>?",
                (old.token_id, old.secret_hash, old.tenant_id, now),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_token(db, new)
            db.execute(
                "INSERT INTO af_auth_token_rotations VALUES (?, ?)",
                (old.token_id, new.token_id),
            )
            return True

    @staticmethod
    def _revoke_descendants(db: sqlite3.Connection, token_id: str) -> None:
        db.execute(
            "WITH RECURSIVE descendants(token_id) AS (VALUES (?) UNION "
            "SELECT child_token_id FROM af_auth_token_rotations JOIN descendants "
            "ON parent_token_id=descendants.token_id) "
            "UPDATE af_auth_tokens SET revoked=1 WHERE token_id IN "
            "(SELECT token_id FROM descendants)",
            (token_id,),
        )

    def add_client(self, client: OAuthClient) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO af_auth_clients VALUES (?, ?, ?, ?, ?)",
                (
                    client.client_id,
                    client.tenant_id,
                    json.dumps(client.redirect_uris),
                    json.dumps(client.scopes),
                    int(client.active),
                ),
            )

    def get_client(self, client_id: str, tenant_id: str) -> OAuthClient | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM af_auth_clients WHERE client_id=? AND tenant_id=?",
                (client_id, tenant_id),
            ).fetchone()
        if row is None:
            return None
        return OAuthClient(
            row[0], row[1], tuple(json.loads(row[2])), tuple(json.loads(row[3])), bool(row[4])
        )

    def revoke_client(self, client_id: str, tenant_id: str) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE af_auth_clients SET active=0 WHERE client_id=? AND tenant_id=?",
                (client_id, tenant_id),
            )
            rows = db.execute(
                "SELECT token_id FROM af_auth_client_tokens WHERE client_id=? AND tenant_id=?",
                (client_id, tenant_id),
            ).fetchall()
            for row in rows:
                self._revoke_descendants(db, row[0])

    def insert_code(self, code: AuthorizationCode) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO af_auth_codes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    code.code_hash,
                    code.client_id,
                    code.tenant_id,
                    json.dumps(asdict(code.principal), sort_keys=True),
                    code.redirect_uri,
                    code.state_hash,
                    code.challenge,
                    code.expires_at,
                    int(code.consumed),
                ),
            )

    def find_code(self, digest: str) -> AuthorizationCode | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM af_auth_codes WHERE code_hash=?", (digest,)).fetchone()
        if row is None:
            return None
        return AuthorizationCode(
            row[0], row[1], row[2], _principal(row[3]), row[4], row[5], row[6], row[7], bool(row[8])
        )

    def redeem_code(self, code: AuthorizationCode, token: TokenRecord, now: float) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            client = db.execute(
                "SELECT active FROM af_auth_clients WHERE client_id=? AND tenant_id=?",
                (code.client_id, code.tenant_id),
            ).fetchone()
            if client is None or not client[0]:
                return False
            cursor = db.execute(
                "UPDATE af_auth_codes SET consumed=1 "
                "WHERE code_hash=? AND consumed=0 AND expires_at>?",
                (code.code_hash, now),
            )
            if cursor.rowcount != 1:
                return False
            self._insert_token(db, token)
            db.execute(
                "INSERT INTO af_auth_client_tokens VALUES (?, ?, ?)",
                (token.token_id, code.client_id, code.tenant_id),
            )
            return True

    def insert_login_state(self, state: ExternalLoginState) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO af_auth_external_states VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    state.state_hash,
                    state.challenge,
                    state.provider_id,
                    state.tenant_id,
                    state.redirect_uri,
                    state.link_subject_id,
                    state.expires_at,
                    int(state.consumed),
                ),
            )

    def find_login_state(self, digest: str) -> ExternalLoginState | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM af_auth_external_states WHERE state_hash=?", (digest,)
            ).fetchone()
        if row is None:
            return None
        return ExternalLoginState(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], bool(row[7])
        )

    def consume_login_state(self, digest: str, now: float) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE af_auth_external_states SET consumed=1 "
                "WHERE state_hash=? AND consumed=0 AND expires_at>?",
                (digest, now),
            )
            return cursor.rowcount == 1

    def resolve_external_subject(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        identity: ExternalIdentity,
        link_subject_id: str | None,
    ) -> str:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT subject_id FROM af_auth_external_links "
                "WHERE tenant_id=? AND provider_id=? AND issuer=? AND external_subject=?",
                (tenant_id, provider_id, identity.issuer, identity.subject),
            ).fetchone()
            if row is not None:
                subject: str = row[0]
                if link_subject_id is not None and subject != link_subject_id:
                    raise AuthenticationDenied()
                return subject
            subject = "external-" + uuid4().hex if link_subject_id is None else link_subject_id
            db.execute(
                "INSERT INTO af_auth_external_links VALUES (?, ?, ?, ?, ?)",
                (tenant_id, provider_id, identity.issuer, identity.subject, subject),
            )
            return subject
