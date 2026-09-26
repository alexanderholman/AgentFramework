"""Explicit pairing, durable replay protection, isolated work and loopback transport.

This transport is deliberately loopback-only. Cross-device deployments must provide
an authenticated encrypted transport; there is no implicit relay or public listener.
"""

from __future__ import annotations

import json
import inspect
import math
import multiprocessing
import os
import socket
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing.connection import Connection
from pathlib import Path
from threading import BoundedSemaphore, Thread
from time import monotonic
from types import TracebackType
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from af.application.peer_execution import PeerExecutionService
from af.framework.content import (
    ContentRejected,
    SignedEnvelope,
    canonical_json,
    content_digest,
    require_text,
)
from af.framework.contracts import Procedure, Verifier
from af.framework.peers import PeerCancellation, PeerLease
from af.framework.types import Classification, ProcedureResult, WorkRequest
from af.infrastructure.trusted_content import Ed25519Identity, Ed25519Trust


def create_pairing_invite(identity: Ed25519Identity) -> SignedEnvelope:
    return identity.sign(
        "af.peer.invite.v1",
        {
            "schema_version": "1.0.0",
            "key_id": identity.key_id,
            "public_key": identity.public_key_bytes().hex(),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        },
    )


def accept_pairing_invite(
    invite: SignedEnvelope,
    *,
    expected_key_id: str,
    trust: Ed25519Trust,
) -> None:
    """expected_key_id must be supplied through an independently trusted channel."""
    raw: object = json.loads(invite.payload_json)
    if not isinstance(raw, dict) or raw.get("key_id") != expected_key_id:
        raise ContentRejected("pairing_fingerprint")
    try:
        public_key = bytes.fromhex(require_text(raw.get("public_key")))
        provisional = Ed25519Trust({expected_key_id: public_key})
        payload = provisional.verify(invite, domain="af.peer.invite.v1")
        expires_at = datetime.fromisoformat(require_text(payload.get("expires_at")))
        if expires_at.utcoffset() is None or expires_at <= datetime.now(UTC):
            raise ContentRejected("pairing_expired")
        if payload.get("schema_version") != "1.0.0":
            raise ContentRejected("pairing_version")
    except ValueError as error:
        raise ContentRejected("pairing_fingerprint") from error
    trust.trust(expected_key_id, public_key)


class SQLitePeerLedger:
    """One grant is one durable nonce. Restart never makes a used grant reusable."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS peer_grants (
                lease_id TEXT PRIMARY KEY, digest TEXT NOT NULL, nonce TEXT UNIQUE NOT NULL,
                expires_at REAL NOT NULL, status TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                request_digest TEXT, envelope TEXT NOT NULL
            )""")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def grant(self, lease: PeerLease, envelope: SignedEnvelope) -> None:
        with self._transaction() as connection:
            old = connection.execute(
                "SELECT digest FROM peer_grants WHERE lease_id=?", (lease.lease_id,)
            ).fetchone()
            if old is not None:
                if old[0] != lease.digest:
                    raise ContentRejected("lease_identity_conflict")
                return
            try:
                connection.execute(
                    """INSERT INTO peer_grants
                    (lease_id,digest,nonce,expires_at,status,envelope) VALUES (?,?,?,?,?,?)""",
                    (
                        lease.lease_id,
                        lease.digest,
                        lease.nonce,
                        lease.expires_at.timestamp(),
                        "granted",
                        canonical_json(envelope.to_dict()),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ContentRejected("nonce_reused") from error

    def admit(self, lease: PeerLease, request_digest: str, now: datetime) -> bool:
        if not lease.issued_at <= now < lease.expires_at:
            return False
        with self._transaction() as connection:
            return (
                connection.execute(
                    """UPDATE peer_grants SET status='running', request_digest=?
                WHERE lease_id=? AND digest=? AND nonce=? AND status='granted'
                AND revoked=0 AND expires_at>?""",
                    (request_digest, lease.lease_id, lease.digest, lease.nonce, now.timestamp()),
                ).rowcount
                == 1
            )

    def current(self, lease: PeerLease, now: datetime) -> bool:
        with self._transaction() as connection:
            return (
                connection.execute(
                    """SELECT 1 FROM peer_grants WHERE lease_id=? AND digest=?
                AND status IN ('granted','running','completed') AND revoked=0 AND expires_at>?""",
                    (lease.lease_id, lease.digest, now.timestamp()),
                ).fetchone()
                is not None
            )

    def finish(self, lease: PeerLease, status: str, now: datetime) -> bool:
        if status not in {"completed", "failed", "timed_out", "cancelled", "resource_exhausted"}:
            raise ValueError("invalid peer terminal state")
        with self._transaction() as connection:
            accepted = (
                connection.execute(
                    """UPDATE peer_grants SET status=? WHERE lease_id=?
                AND digest=? AND status='running' AND revoked=0 AND expires_at>?""",
                    (status, lease.lease_id, lease.digest, now.timestamp()),
                ).rowcount
                == 1
            )
            if not accepted:
                connection.execute(
                    "UPDATE peer_grants SET status='cancelled' "
                    "WHERE lease_id=? AND status='running'",
                    (lease.lease_id,),
                )
            return accepted

    def revoke(self, lease_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("UPDATE peer_grants SET revoked=1 WHERE lease_id=?", (lease_id,))

    def recover_interrupted(self) -> tuple[str, ...]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT lease_id FROM peer_grants WHERE status='running' ORDER BY lease_id"
            ).fetchall()
            connection.execute("UPDATE peer_grants SET status='interrupted' WHERE status='running'")
            return tuple(str(row[0]) for row in rows)


def _deny_effects(event: str, arguments: tuple[object, ...]) -> None:
    """Defense in depth for trusted installed pure code, not an arbitrary-code sandbox."""
    if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn")) or event in {
        "os.system",
        "os.fork",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.mkdir",
    }:
        raise PermissionError("peer read-only scope")
    if event == "open" and len(arguments) >= 3:
        mode, flags = arguments[1], arguments[2]
        if (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        ):
            raise PermissionError("peer read-only scope")


def _procedure_worker(
    pipe: Connection,
    procedure: Procedure,
    classification: Classification,
    verifier: Verifier,
    lease: PeerLease,
) -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (lease.max_memory_mib * 1_048_576,) * 2)
        cpu_seconds = max(1, math.ceil(lease.max_cpu_ms / 1000))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        sys.addaudithook(_deny_effects)
        procedure.validate(classification)
        result = procedure.execute(classification)
        verdict = verifier.verify(WorkRequest("peer invocation"), classification, procedure, result)
        if not verdict.passed or len(result.output.encode()) > lease.max_output_bytes:
            raise ContentRejected("verification_failed")
        pipe.send_bytes(canonical_json({"output": result.output, "verified": True}).encode())
    except Exception:
        pipe.send_bytes(b'{"verified":false}')
    finally:
        pipe.close()


class SubprocessPeerExecutor:
    """Run installed Python procedures in a bounded child; no peer source is imported."""

    def execute(
        self,
        procedure: Procedure,
        classification: Classification,
        verifier: Verifier,
        lease: PeerLease,
        cancelled: PeerCancellation,
    ) -> ProcedureResult:
        if sys.platform == "win32":
            raise ContentRejected("resource_limits_unavailable")
        source_path = Path(inspect.getfile(type(procedure)))
        if source_path.is_symlink() or content_digest(source_path.read_bytes()) != lease.implementation_digest:
            raise ContentRejected("procedure_digest_mismatch")
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(
            target=_procedure_worker,
            args=(sending, procedure, classification, verifier, lease),
            daemon=True,
        )
        until = monotonic() + lease.max_wall_ms / 1000
        try:
            process.start()
            sending.close()
            while True:
                if cancelled():
                    raise ContentRejected("execution_cancelled")
                remaining = until - monotonic()
                if remaining <= 0:
                    raise ContentRejected("execution_timeout")
                if receiving.poll(min(0.025, remaining)):
                    try:
                        encoded = receiving.recv_bytes(maxlength=lease.max_output_bytes + 128)
                    except (EOFError, OSError) as error:
                        raise ContentRejected("execution_failed") from error
                    payload: object = json.loads(encoded)
                    if not isinstance(payload, dict) or payload.get("verified") is not True:
                        raise ContentRejected("verification_failed")
                    output = payload.get("output")
                    if not isinstance(output, str) or len(output.encode()) > lease.max_output_bytes:
                        raise ContentRejected("output_budget")
                    return ProcedureResult(output, {"peer_verification": True})
                if not process.is_alive():
                    raise ContentRejected("execution_failed")
        finally:
            receiving.close()
            sending.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
                process.close()


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


class _BoundedPeerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        self.slots = BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]
    ) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class LoopbackPeerServer:
    def __init__(self, service: PeerExecutionService) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.connection.settimeout(10)
                try:
                    if self.path != "/execute":
                        raise ContentRejected("unknown_endpoint")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2 * 1_048_576:
                        raise ContentRejected("request_size")
                    request = SignedEnvelope.from_dict(json.loads(self.rfile.read(length)))

                    def disconnected() -> bool:
                        previous_timeout = self.connection.gettimeout()
                        try:
                            self.connection.setblocking(False)
                            return bool(self.connection.recv(1, socket.MSG_PEEK) == b"")
                        except (BlockingIOError, TimeoutError):
                            return False
                        except OSError:
                            return True
                        finally:
                            self.connection.settimeout(previous_timeout)

                    payload = service.execute(request, disconnected=disconnected).to_dict()
                    status = 200
                except ContentRejected as error:
                    payload, status = {"error": error.reason_code}, 403
                except Exception:
                    payload, status = {"error": "invalid_request"}, 400
                body = canonical_json(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = _BoundedPeerHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/execute"

    def __enter__(self) -> LoopbackPeerServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class PeerHTTPClient:
    def __init__(self, endpoint: str, *, timeout: float = 30) -> None:
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/execute"
            or not 0 < timeout <= 600
        ):
            raise ValueError("peer transport requires an explicit bounded loopback endpoint")
        self.endpoint, self.timeout = endpoint, timeout

    def execute(self, request: SignedEnvelope) -> SignedEnvelope:
        opener = build_opener(ProxyHandler({}), _NoRedirects())
        http_request = Request(
            self.endpoint,
            data=canonical_json(request.to_dict()).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(http_request, timeout=self.timeout) as response:
                encoded = response.read(2 * 1_048_576 + 1)
        except HTTPError as error:
            payload: object = json.loads(error.read(4096))
            reason = payload.get("error") if isinstance(payload, dict) else None
            raise ContentRejected(reason if isinstance(reason, str) else "peer_denied") from error
        except (URLError, TimeoutError) as error:
            raise ContentRejected("peer_disconnected") from error
        if len(encoded) > 2 * 1_048_576:
            raise ContentRejected("response_size")
        return SignedEnvelope.from_dict(json.loads(encoded))
