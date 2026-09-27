"""Real loopback sockets must not retain server slots on incomplete headers."""

import socket
from http.server import BaseHTTPRequestHandler
from threading import Thread

import pytest

from af.infrastructure.peers import _BoundedPeerHTTPServer


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def test_incomplete_headers_expire_before_endpoint_dispatch():
    server = _BoundedPeerHTTPServer(("127.0.0.1", 0), QuietHandler, request_timeout=0.05)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(server.server_address, timeout=1) as client:
            client.sendall(b"GET /manifest HTTP/1.1\r\nHost:")
            assert client.recv(1) == b""
        # The timed-out request has relinquished its bounded worker slot.
        acquired = [server.slots.acquire(timeout=1) for _ in range(16)]
        assert all(acquired)
        for held in acquired:
            if held:
                server.slots.release()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_transport_deadline_is_rejected(timeout):
    with pytest.raises(ValueError):
        _BoundedPeerHTTPServer(("127.0.0.1", 0), QuietHandler, request_timeout=timeout)
