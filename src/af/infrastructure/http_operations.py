"""SQLite request ledger; uncertain requests are retained, never automatically retried."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from af.application.http_operations import ApiRejection, StoredResponse


class SQLiteHttpOperations:
    def __init__(
        self,
        path: Path | str,
        *,
        retention_seconds: int = 86_400,
        max_records: int = 10_000,
        requests_per_window: int = 600,
        window_seconds: int = 60,
        max_principals: int = 10_000,
    ) -> None:
        for value, maximum in (
            (retention_seconds, 2_592_000),
            (max_records, 100_000),
            (requests_per_window, 100_000),
            (window_seconds, 3600),
            (max_principals, 100_000),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("HTTP ledger bounds must be positive bounded integers")
        if str(path) != ":memory:":
            resolved = Path(path)
            if resolved.is_symlink():
                raise ValueError("HTTP ledger must not be a symbolic link")
            resolved.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(resolved, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        self._connection = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        self._lock = RLock()
        self._retention = retention_seconds
        self._max_records = max_records
        self._rate = requests_per_window
        self._window = window_seconds
        self._max_principals = max_principals
        self._connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS af_http_replays (
                scope TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, expires REAL, status INTEGER, headers TEXT, body BLOB,
                PRIMARY KEY(scope,key));
            CREATE TABLE IF NOT EXISTS af_http_rates (
                principal TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
        """)
        self._connection.commit()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    @staticmethod
    def _validate(now: float, *values: str) -> None:
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("HTTP ledger clock must be finite")
        if any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in values):
            raise ValueError("HTTP ledger identifiers must be bounded nonempty strings")

    def admit(self, principal: str, *, now: float) -> None:
        self._validate(now, principal)
        with self._transaction() as db:
            db.execute("DELETE FROM af_http_rates WHERE started<=?", (now - self._window,))
            row = db.execute(
                "SELECT started,count FROM af_http_rates WHERE principal=?", (principal,)
            ).fetchone()
            if row is not None:
                if row[1] >= self._rate:
                    raise ApiRejection(
                        429,
                        "rate_limit_exceeded",
                        retry_after=max(1, math.ceil(row[0] + self._window - now)),
                    )
                db.execute("UPDATE af_http_rates SET count=count+1 WHERE principal=?", (principal,))
            else:
                if (
                    db.execute("SELECT COUNT(*) FROM af_http_rates").fetchone()[0]
                    >= self._max_principals
                ):
                    raise ApiRejection(503, "rate_limit_capacity")
                db.execute("INSERT INTO af_http_rates VALUES (?,?,1)", (principal, now))

    def claim(self, scope: str, key: str, fingerprint: str, *, now: float) -> StoredResponse | None:
        self._validate(now, scope, key, fingerprint)
        with self._transaction() as db:
            db.execute("DELETE FROM af_http_replays WHERE state='completed' AND expires<=?", (now,))
            row = db.execute(
                "SELECT fingerprint,state,status,headers,body FROM af_http_replays "
                "WHERE scope=? AND key=?",
                (scope, key),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise ApiRejection(409, "idempotency_key_request_mismatch")
                if row[1] != "completed":
                    raise ApiRejection(409, "idempotency_in_progress_or_uncertain")
                try:
                    headers = tuple(
                        (bytes.fromhex(k), bytes.fromhex(v)) for k, v in json.loads(row[3])
                    )
                    return StoredResponse(row[2], headers, row[4])
                except (ValueError, TypeError) as error:
                    raise ApiRejection(503, "idempotency_record_invalid") from error
            if (
                db.execute("SELECT COUNT(*) FROM af_http_replays").fetchone()[0]
                >= self._max_records
            ):
                raise ApiRejection(503, "idempotency_capacity")
            db.execute(
                "INSERT INTO af_http_replays(scope,key,fingerprint,state) VALUES (?,?,?,?)",
                (scope, key, fingerprint, "pending"),
            )
            return None

    def complete(
        self, scope: str, key: str, fingerprint: str, response: StoredResponse, *, now: float
    ) -> None:
        self._validate(now, scope, key, fingerprint)
        if not isinstance(response, StoredResponse):
            raise TypeError("completion requires a bounded response")
        headers = json.dumps([(k.hex(), v.hex()) for k, v in response.headers])
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE af_http_replays SET state='completed',expires=?,status=?,headers=?,body=? "
                "WHERE scope=? AND key=? AND fingerprint=? AND state='pending'",
                (
                    now + self._retention,
                    response.status,
                    headers,
                    response.body,
                    scope,
                    key,
                    fingerprint,
                ),
            ).rowcount
            if changed != 1:
                raise ApiRejection(409, "idempotency_completion_fenced")

    def close(self) -> None:
        with self._lock:
            self._connection.close()
