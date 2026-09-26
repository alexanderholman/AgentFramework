"""Actual framed local sockets and Node-to-Python trusted peer IPC."""

import json
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_trusted_peers import ARGS, nodes

from af.application.peer_execution import make_peer_request, verify_peer_result
from af.framework.content import ContentRejected, SignedEnvelope
from af.infrastructure.peer_ipc import (
    PeerIPCDispatcher,
    PeerIPCServer,
    accept_transport_invite,
    create_transport_invite,
    read_frame,
    write_frame,
)

TOKEN = "a" * 64
TRANSPORT = "b" * 64


def wire(method: str, payload: object, *, token: str = TOKEN) -> dict[str, object]:
    return {
        "version": "1.0.0",
        "id": "request-1",
        "token": token,
        "method": method,
        "payload": payload,
    }


def test_ipc_dispatches_signed_installed_procedure_and_denies_authority_injection(
    tmp_path: Path,
) -> None:
    service, peer, trust, lease, _ = nodes(tmp_path)
    request = make_peer_request(peer, service.issue_lease(lease), ARGS)
    with PeerIPCServer(service, {TRANSPORT: peer.key_id}, token=TOKEN) as server:
        with socket.create_connection(server.address) as connection:
            write_frame(
                connection,
                wire("peer.execute", {"transport_key": TRANSPORT, "envelope": request.to_dict()}),
            )
            response = read_frame(connection)
        assert response["ok"] is True
        result = verify_peer_result(
            SignedEnvelope.from_dict(response["result"]),
            request=request,
            trust=trust,
            lease_current=lambda: True,
        )
        assert result["output"] == "3"
        for bad in (
            wire("lease.issue", {}),
            wire("status", {}, token="c" * 64),
            wire("peer.execute", {"transport_key": "c" * 64, "envelope": request.to_dict()}),
        ):
            with socket.create_connection(server.address) as connection:
                write_frame(connection, bad)
                denied = read_frame(connection)
            assert denied["ok"] is False
            assert TOKEN not in json.dumps(denied)


def test_node_client_crosses_real_ipc_and_does_not_receive_secrets(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node conformance runtime is unavailable")
    service, peer, _, _, _ = nodes(tmp_path)
    with PeerIPCServer(service, {TRANSPORT: peer.key_id}, token=TOKEN) as server:
        script = """
const { requestLocal } = require('./native/peers/local-ipc.cjs');
const net = require('node:net');
requestLocal(net, JSON.parse(process.argv[1]), 'status', {}).then(
  result => process.stdout.write(JSON.stringify(result)),
  () => { process.exitCode = 1 }
);
"""
        result = subprocess.run(
            [
                node,
                "-e",
                script,
                json.dumps(
                    {
                        "host": server.address[0],
                        "port": server.address[1],
                        "token": TOKEN,
                    }
                ),
            ],
            cwd=Path(__file__).parents[2],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    public = json.loads(result.stdout)
    assert public["protocol"] == "af.peer.ipc.v1"
    assert public["host_key_id"] == service.identity.key_id
    assert TOKEN not in result.stdout and "private" not in result.stdout


def test_ipc_rejects_oversized_and_partial_frames(tmp_path: Path) -> None:
    service, peer, _, _, _ = nodes(tmp_path)
    with PeerIPCServer(
        service, {TRANSPORT: peer.key_id}, token=TOKEN, timeout_seconds=0.05
    ) as server:
        for fragment in (b"\xff\xff\xff\xff", b"\x00"):
            with socket.create_connection(server.address) as connection:
                connection.settimeout(1)
                connection.sendall(fragment)
                assert connection.recv(4) == b""


def test_ipc_total_frame_deadline_stops_slow_drip(tmp_path: Path) -> None:
    service, peer, _, _, _ = nodes(tmp_path)
    with (
        PeerIPCServer(
            service, {TRANSPORT: peer.key_id}, token=TOKEN, timeout_seconds=0.05
        ) as server,
        socket.create_connection(server.address) as connection,
    ):
        connection.settimeout(1)
        frame = (100).to_bytes(4, "big") + b"{" + b" " * 95
        started = time.monotonic()
        for index in range(20):
            try:
                connection.sendall(frame[index : index + 1])
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.01)
        assert connection.recv(4) == b""
        assert time.monotonic() - started < 0.5


@pytest.mark.parametrize("body", [b"[]", b'{"a":1,"a":2}', b"{bad}", b"[" * 2000 + b"]" * 2000])
def test_ipc_malformed_frames_close_without_traceback(
    tmp_path: Path, body: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    service, peer, _, _, _ = nodes(tmp_path)
    with (
        PeerIPCServer(service, {TRANSPORT: peer.key_id}, token=TOKEN) as server,
        socket.create_connection(server.address) as connection,
    ):
        connection.settimeout(1)
        connection.sendall(len(body).to_bytes(4, "big") + body)
        assert connection.recv(1) == b""
    assert "Traceback" not in capsys.readouterr().err


def test_ipc_unknown_schema_signer_and_failed_execution_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, peer, _, lease, _ = nodes(tmp_path)
    dispatcher = PeerIPCDispatcher(service, {TRANSPORT: peer.key_id}, TOKEN)
    request = make_peer_request(peer, service.issue_lease(lease), ARGS)
    for bad in (
        {},
        {**wire("status", {}), "id": 1},
        wire("status", []),
        wire("status", {}, token="秘密"),
        wire("peer.execute", {"transport_key": TRANSPORT, "envelope": {}}),
    ):
        assert dispatcher.dispatch(bad)["ok"] is False
    mismatch = PeerIPCDispatcher(service, {TRANSPORT: "sha256:" + "c" * 64}, TOKEN)
    assert (
        mismatch.dispatch(
            wire("peer.execute", {"transport_key": TRANSPORT, "envelope": request.to_dict()})
        )["reason"]
        == "transport_identity_mismatch"
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private host path/token")

    monkeypatch.setattr(service, "execute", fail)
    failure = dispatcher.dispatch(
        wire("peer.execute", {"transport_key": TRANSPORT, "envelope": request.to_dict()})
    )
    assert failure["reason"] == "ipc_execution_failed"
    assert "private" not in json.dumps(failure)
    for bindings, token in (({}, "bad"), ({"bad": peer.key_id}, TOKEN)):
        with pytest.raises(ValueError):
            PeerIPCDispatcher(service, bindings, token)
    with pytest.raises(ValueError):
        PeerIPCServer(service, {}, timeout_seconds=float("nan"))
    with socket.socket() as connection, pytest.raises(ValueError):
        write_frame(connection, {"large": "a" * (2 * 1024 * 1024)})


def test_transport_invite_binds_noise_key_to_confirmed_signer_and_expires(tmp_path: Path) -> None:
    service, peer, trust, _, _ = nodes(tmp_path)
    now = datetime.now(UTC)
    invitation = create_transport_invite(peer, TRANSPORT, expires_at=now + timedelta(minutes=1))
    assert accept_transport_invite(
        invitation, expected_signing_key_id=peer.key_id, trust=trust, now=now
    ) == (TRANSPORT, peer.key_id)
    for expected, when in (
        (service.identity.key_id, now),
        (peer.key_id, now + timedelta(minutes=2)),
    ):
        with pytest.raises(ContentRejected):
            accept_transport_invite(
                invitation, expected_signing_key_id=expected, trust=trust, now=when
            )
    with pytest.raises(ValueError):
        create_transport_invite(peer, "bad", expires_at=now)
    payload = json.loads(invitation.payload_json)
    for expiry in ("bad", "2026-01-01T00:00:00", 42):
        altered = peer.sign("af.peer.transport-invite.v1", {**payload, "expires_at": expiry})
        with pytest.raises(ContentRejected):
            accept_transport_invite(
                altered, expected_signing_key_id=peer.key_id, trust=trust, now=now
            )


def test_ipc_connection_capacity_and_shutdown_disconnect_existing_clients(tmp_path: Path) -> None:
    service, peer, _, _, _ = nodes(tmp_path)
    connections: list[socket.socket] = []
    try:
        with PeerIPCServer(service, {TRANSPORT: peer.key_id}) as server:
            for _ in range(16):
                connection = socket.create_connection(server.address)
                connection.settimeout(1)
                connection.sendall(b"\x00")
                connections.append(connection)
            with socket.create_connection(server.address) as overflow:
                overflow.settimeout(1)
                assert overflow.recv(1) == b""
        for connection in connections:
            assert connection.recv(1) == b""
    finally:
        for connection in connections:
            connection.close()
