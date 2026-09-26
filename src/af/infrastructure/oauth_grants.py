"""Durable grant state with transactional rotation, replay detection and device polling."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from af.framework.authentication import AuthenticationDenied, TokenRecord
from af.framework.context import Principal
from af.framework.oauth_grants import DeviceRecord, RefreshRecord, ServiceClient
from af.infrastructure.authentication import SQLiteAuthenticationRepository, _principal


class SQLiteOAuthGrantRepository(SQLiteAuthenticationRepository):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS af_oauth_services (
                    client_id TEXT NOT NULL, tenant_id TEXT NOT NULL, secret_hash TEXT NOT NULL,
                    scopes TEXT NOT NULL, active INTEGER NOT NULL,
                    PRIMARY KEY(client_id, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS af_oauth_service_tokens (
                    token_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, tenant_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_oauth_refresh (
                    secret_hash TEXT PRIMARY KEY, family_id TEXT NOT NULL, client_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL, principal TEXT NOT NULL, expires_at REAL NOT NULL,
                    consumed INTEGER NOT NULL, revoked INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_oauth_access_families (
                    token_id TEXT PRIMARY KEY, family_id TEXT NOT NULL, tenant_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS af_oauth_devices (
                    device_hash TEXT PRIMARY KEY, user_hash TEXT UNIQUE NOT NULL,
                    client_id TEXT NOT NULL, tenant_id TEXT NOT NULL, scopes TEXT NOT NULL,
                    expires_at REAL NOT NULL, interval INTEGER NOT NULL, last_poll REAL,
                    status TEXT NOT NULL, principal TEXT
                );
            """)

    def add_service_client(self, client: ServiceClient) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO af_oauth_services VALUES (?, ?, ?, ?, ?)",
                (
                    client.client_id,
                    client.tenant_id,
                    client.secret_hash,
                    json.dumps(client.scopes),
                    int(client.active),
                ),
            )

    def get_service_client(self, client_id: str, tenant_id: str) -> ServiceClient | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM af_oauth_services WHERE client_id=? AND tenant_id=?",
                (client_id, tenant_id),
            ).fetchone()
        if row is None:
            return None
        return ServiceClient(row[0], row[1], row[2], tuple(json.loads(row[3])), bool(row[4]))

    def revoke_service_client(self, client_id: str, tenant_id: str) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE af_oauth_services SET active=0 WHERE client_id=? AND tenant_id=?",
                (client_id, tenant_id),
            )
            rows = db.execute(
                "SELECT token_id FROM af_oauth_service_tokens WHERE client_id=? AND tenant_id=?",
                (client_id, tenant_id),
            ).fetchall()
            for row in rows:
                self._revoke_descendants(db, row[0])

    def insert_service_token(self, client: ServiceClient, token: TokenRecord) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT active FROM af_oauth_services WHERE client_id=? AND "
                "tenant_id=? AND secret_hash=?",
                (client.client_id, client.tenant_id, client.secret_hash),
            ).fetchone()
            if row is None or not row[0]:
                return False
            self._insert_token(db, token)
            db.execute(
                "INSERT INTO af_oauth_service_tokens VALUES (?, ?, ?)",
                (token.token_id, client.client_id, client.tenant_id),
            )
            return True

    def _insert_refresh(
        self, db: sqlite3.Connection, refresh: RefreshRecord, access: TokenRecord
    ) -> None:
        db.execute(
            "INSERT INTO af_oauth_refresh VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                refresh.secret_hash,
                refresh.family_id,
                refresh.client_id,
                refresh.tenant_id,
                json.dumps(asdict(refresh.principal), sort_keys=True),
                refresh.expires_at,
                int(refresh.consumed),
                int(refresh.revoked),
            ),
        )
        self._insert_token(db, access)
        db.execute(
            "INSERT INTO af_auth_client_tokens VALUES (?, ?, ?)",
            (access.token_id, refresh.client_id, refresh.tenant_id),
        )
        db.execute(
            "INSERT INTO af_oauth_access_families VALUES (?, ?, ?)",
            (access.token_id, refresh.family_id, refresh.tenant_id),
        )

    def insert_refresh_pair(self, refresh: RefreshRecord, access: TokenRecord) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT active FROM af_auth_clients WHERE client_id=? AND tenant_id=?",
                (refresh.client_id, refresh.tenant_id),
            ).fetchone()
            if row is None or not row[0]:
                raise AuthenticationDenied()
            self._insert_refresh(db, refresh, access)

    def get_refresh(self, digest: str) -> RefreshRecord | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM af_oauth_refresh WHERE secret_hash=?", (digest,)
            ).fetchone()
        if row is None:
            return None
        return RefreshRecord(
            row[0], row[1], row[2], row[3], _principal(row[4]), row[5], bool(row[6]), bool(row[7])
        )

    @staticmethod
    def _revoke_family(db: sqlite3.Connection, family_id: str, tenant_id: str) -> None:
        db.execute(
            "UPDATE af_oauth_refresh SET revoked=1 WHERE family_id=? AND tenant_id=?",
            (family_id, tenant_id),
        )
        rows = db.execute(
            "SELECT token_id FROM af_oauth_access_families WHERE family_id=? AND tenant_id=?",
            (family_id, tenant_id),
        ).fetchall()
        for row in rows:
            SQLiteAuthenticationRepository._revoke_descendants(db, row[0])

    def revoke_refresh_family(self, family_id: str, tenant_id: str) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._revoke_family(db, family_id, tenant_id)

    def rotate_refresh(
        self, old: RefreshRecord, new: RefreshRecord, access: TokenRecord, now: float
    ) -> bool:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            client = db.execute(
                "SELECT active FROM af_auth_clients WHERE client_id=? AND tenant_id=?",
                (old.client_id, old.tenant_id),
            ).fetchone()
            if client is None or not client[0]:
                return False
            row = db.execute(
                "SELECT consumed, revoked, expires_at FROM af_oauth_refresh WHERE secret_hash=?",
                (old.secret_hash,),
            ).fetchone()
            if row is None or row[1] or row[2] <= now:
                return False
            if row[0]:
                self._revoke_family(db, old.family_id, old.tenant_id)
                return False
            db.execute(
                "UPDATE af_oauth_refresh SET consumed=1 WHERE secret_hash=?", (old.secret_hash,)
            )
            self._insert_refresh(db, new, access)
            return True

    def insert_device(self, record: DeviceRecord) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT INTO af_oauth_devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.device_hash,
                    record.user_hash,
                    record.client_id,
                    record.tenant_id,
                    json.dumps(record.scopes),
                    record.expires_at,
                    record.interval,
                    record.last_poll,
                    record.status,
                    None,
                ),
            )

    def find_device(self, digest: str, *, user_code: bool = False) -> DeviceRecord | None:
        column = "user_hash" if user_code else "device_hash"
        with self._connection() as db:
            row = db.execute(
                f"SELECT * FROM af_oauth_devices WHERE {column}=?", (digest,)
            ).fetchone()
        if row is None:
            return None
        return DeviceRecord(
            row[0],
            row[1],
            row[2],
            row[3],
            tuple(json.loads(row[4])),
            row[5],
            row[6],
            row[7],
            row[8],
            None if row[9] is None else _principal(row[9]),
        )

    def approve_device(
        self, user_hash: str, tenant_id: str, principal: Principal, consented: bool, now: float
    ) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE af_oauth_devices SET status=?, principal=? WHERE "
                "user_hash=? AND tenant_id=? AND status='pending' AND expires_at>?",
                (
                    "approved" if consented else "denied",
                    json.dumps(asdict(principal), sort_keys=True),
                    user_hash,
                    tenant_id,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def poll_device(
        self,
        device_hash: str,
        client_id: str,
        tenant_id: str,
        now: float,
        token: TokenRecord | None,
    ) -> str:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT expires_at, interval, last_poll, status FROM "
                "af_oauth_devices WHERE device_hash=? AND client_id=? AND tenant_id=?",
                (device_hash, client_id, tenant_id),
            ).fetchone()
            client = db.execute(
                "SELECT active FROM af_auth_clients WHERE client_id=? AND tenant_id=?",
                (client_id, tenant_id),
            ).fetchone()
            if row is None or client is None or not client[0] or row[3] == "consumed":
                return "invalid_grant"
            if row[0] <= now:
                return "expired_token"
            if row[3] == "denied":
                return "access_denied"
            if row[2] is not None and now < row[2] + row[1]:
                db.execute(
                    "UPDATE af_oauth_devices SET interval=interval+5, "
                    "last_poll=? WHERE device_hash=?",
                    (now, device_hash),
                )
                return "slow_down"
            db.execute(
                "UPDATE af_oauth_devices SET last_poll=? WHERE device_hash=?", (now, device_hash)
            )
            if row[3] != "approved" or token is None:
                return "authorization_pending"
            db.execute(
                "UPDATE af_oauth_devices SET status='consumed' WHERE device_hash=?", (device_hash,)
            )
            self._insert_token(db, token)
            db.execute(
                "INSERT INTO af_auth_client_tokens VALUES (?, ?, ?)",
                (token.token_id, client_id, tenant_id),
            )
            return "issued"
