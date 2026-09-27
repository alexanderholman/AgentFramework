"""Authenticated bounded native-worker IPC to the shared peer execution service.

The renderer never receives the random IPC token. Local operators provision
transport-key/signing-key bindings after checking an invite out of band.
This endpoint cannot issue leases, install procedures, or run source/commands.
"""

from __future__ import annotations

import hmac
import json
import math
import re
import secrets
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime
from types import TracebackType
from typing import cast

from af.application.peer_execution import PeerExecutionService
from af.framework.content import (
    ContentRejected,
    EnvelopeSigner,
    EnvelopeVerifier,
    SignedEnvelope,
    canonical_json,
    require_digest,
)

MAX_FRAME_BYTES = 1_048_576
_KEY = re.compile(r"^[0-9a-f]{64}$")


def create_transport_invite(
    identity: EnvelopeSigner,
    transport_key: str,
    *,
    expires_at: datetime,
) -> SignedEnvelope:
    """Operator-only binding of the native Noise identity to the signing identity."""
    if _KEY.fullmatch(transport_key) is None or expires_at.utcoffset() is None:
        raise ValueError("invalid transport invite")
    return identity.sign(
        "af.peer.transport-invite.v1",
        {
            "schema_version": "1.0.0",
            "signing_key_id": identity.key_id,
            "transport_key": transport_key,
            "expires_at": expires_at.isoformat(),
        },
    )


def accept_transport_invite(
    invitation: SignedEnvelope,
    *,
    expected_signing_key_id: str,
    trust: EnvelopeVerifier,
    now: datetime,
) -> tuple[str, str]:
    """Return an exact public binding after explicit fingerprint confirmation."""
    require_digest(expected_signing_key_id)
    payload = trust.verify(invitation, domain="af.peer.transport-invite.v1")
    if (
        set(payload) != {"schema_version", "signing_key_id", "transport_key", "expires_at"}
        or payload["schema_version"] != "1.0.0"
        or payload["signing_key_id"] != expected_signing_key_id
        or len(invitation.signatures) != 1
        or invitation.signatures[0].key_id != expected_signing_key_id
    ):
        raise ContentRejected("transport_invite_identity")
    key, expiry = payload["transport_key"], payload["expires_at"]
    if type(key) is not str or _KEY.fullmatch(key) is None or type(expiry) is not str:
        raise ContentRejected("transport_invite_schema")
    try:
        expires_at = datetime.fromisoformat(expiry)
    except ValueError as exc:
        raise ContentRejected("transport_invite_schema") from exc
    if now.utcoffset() is None or expires_at.utcoffset() is None or now >= expires_at:
        raise ContentRejected("transport_invite_expired")
    return key, expected_signing_key_id


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate IPC field")
        result[key] = value
    return result


def _receive(connection: socket.socket, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("IPC frame deadline")
        connection.settimeout(remaining)
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError("IPC disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(connection: socket.socket) -> dict[str, object]:
    timeout = connection.gettimeout()
    deadline = time.monotonic() + (timeout if timeout is not None else 10)
    try:
        size = int.from_bytes(_receive(connection, 4, deadline), "big")
        if not 1 <= size <= MAX_FRAME_BYTES:
            raise ValueError("IPC frame size")
        value: object = json.loads(_receive(connection, size, deadline), object_pairs_hook=_object)
        if not isinstance(value, dict):
            raise ValueError("IPC frame must be an object")
        return cast(dict[str, object], value)
    finally:
        connection.settimeout(timeout)


def write_frame(connection: socket.socket, payload: Mapping[str, object]) -> None:
    encoded = canonical_json(dict(payload)).encode()
    if not 1 <= len(encoded) <= MAX_FRAME_BYTES:
        raise ValueError("IPC frame size")
    connection.sendall(len(encoded).to_bytes(4, "big") + encoded)


def _disconnected(connection: socket.socket) -> bool:
    timeout = connection.gettimeout()
    connection.setblocking(False)
    try:
        return connection.recv(1, socket.MSG_PEEK) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True
    finally:
        connection.settimeout(timeout)


class PeerIPCDispatcher:
    def __init__(
        self, service: PeerExecutionService, bindings: Mapping[str, str], token: str
    ) -> None:
        if _KEY.fullmatch(token) is None or len(bindings) > 256:
            raise ValueError("invalid native IPC configuration")
        for key, signing_key in bindings.items():
            if _KEY.fullmatch(key) is None:
                raise ValueError("invalid transport key")
            require_digest(signing_key)
        self.service = service
        self.bindings = dict(bindings)
        self._token = token

    def dispatch(
        self,
        request: Mapping[str, object],
        *,
        disconnected: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        identifier = request.get("id")
        if type(identifier) is not str or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier) is None:
            identifier = "invalid"
        response: dict[str, object] = {"version": "1.0.0", "id": identifier, "ok": False}
        try:
            if (
                set(request) != {"version", "id", "token", "method", "payload"}
                or request["version"] != "1.0.0"
                or identifier == "invalid"
            ):
                raise ContentRejected("ipc_schema")
            token = request["token"]
            if (
                type(token) is not str
                or not token.isascii()
                or not hmac.compare_digest(token, self._token)
            ):
                raise ContentRejected("ipc_authentication")
            payload = request["payload"]
            if not isinstance(payload, dict):
                raise ContentRejected("ipc_schema")
            if request["method"] == "status" and not payload:
                result: object = {
                    "protocol": "af.peer.ipc.v1",
                    "host_key_id": self.service.identity.key_id,
                    "lease_issuance": False,
                    "source_execution": False,
                }
            elif request["method"] == "peer.execute" and set(payload) == {
                "transport_key",
                "envelope",
            }:
                key = payload["transport_key"]
                if type(key) is not str or key not in self.bindings:
                    raise ContentRejected("transport_unpaired")
                envelope = SignedEnvelope.from_dict(payload["envelope"])
                if (
                    len(envelope.signatures) != 1
                    or envelope.signatures[0].key_id != self.bindings[key]
                ):
                    raise ContentRejected("transport_identity_mismatch")
                result = self.service.execute(envelope, disconnected=disconnected).to_dict()
            else:
                raise ContentRejected("ipc_method_denied")
            response.update(ok=True, result=result)
        except ContentRejected as exc:
            response["reason"] = exc.reason_code
        except (ValueError, TypeError, KeyError):
            response["reason"] = "ipc_schema"
        except Exception:
            response["reason"] = "ipc_execution_failed"
        return response


class _IPCServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False
    block_on_close = False

    def __init__(self, dispatcher: PeerIPCDispatcher, timeout: float) -> None:
        self.dispatcher = dispatcher
        self.read_timeout = timeout
        self.slots = threading.BoundedSemaphore(16)
        self.connections: set[socket.socket] = set()
        self.connections_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _IPCHandler)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = super().get_request()
        connection.settimeout(self.read_timeout)
        with self.connections_lock:
            self.connections.add(connection)
        return connection, address

    def shutdown_request(self, request: socket.socket | tuple[bytes, socket.socket]) -> None:
        if isinstance(request, socket.socket):
            with self.connections_lock:
                self.connections.discard(request)
        super().shutdown_request(request)

    def disconnect_all(self) -> None:
        with self.connections_lock:
            connections = tuple(self.connections)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _IPCHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = cast(_IPCServer, self.server)
        connection = cast(socket.socket, self.request)
        try:
            request = read_frame(connection)
            response = server.dispatcher.dispatch(
                request, disconnected=lambda: _disconnected(connection)
            )
            write_frame(connection, response)
        except (OSError, EOFError, ValueError, RecursionError):
            return


class PeerIPCServer:
    """One authenticated request per local connection; no renderer credentials."""

    def __init__(
        self,
        service: PeerExecutionService,
        bindings: Mapping[str, str],
        *,
        token: str | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 600:
            raise ValueError("IPC deadline must be finite and bounded")
        self.token = token if token is not None else secrets.token_hex(32)
        self._server = _IPCServer(PeerIPCDispatcher(service, bindings, self.token), timeout_seconds)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def address(self) -> tuple[str, int]:
        return str(self._server.server_address[0]), int(self._server.server_address[1])

    def __enter__(self) -> PeerIPCServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.disconnect_all()
        self._server.server_close()
        self._thread.join(timeout=2)
