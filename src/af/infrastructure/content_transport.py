"""Explicit, bounded loopback origin for signed immutable chunk transfer.

This is a local qualification/IPC transport. It never discovers a swarm, opens a
public listener, donates resources silently, or claims a physical Pear deployment.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import Event, Lock, Thread
from types import TracebackType
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from af.framework.content import ArtifactManifest, ContentRejected, SignedEnvelope, canonical_json
from af.infrastructure.peers import _BoundedPeerHTTPServer, _NoRedirects
from af.infrastructure.trusted_content import VerifiedContentStore


class LoopbackContentServer:
    def __init__(
        self,
        store: VerifiedContentStore,
        handle: str,
        *,
        serving_enabled: bool,
        max_served_bytes: int = 64 * 1_048_576,
    ) -> None:
        if not serving_enabled or type(max_served_bytes) is not int or max_served_bytes < 1:
            raise ContentRejected("serving_not_enabled")
        envelope = store.manifest_envelope(handle)
        self._paused = Event()
        self._lock = Lock()
        self._served = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                try:
                    if outer._paused.is_set():
                        raise ContentRejected("serving_paused")
                    if self.path == "/manifest":
                        # Refresh revocation/trust rather than serving a cached authorization.
                        body = canonical_json(
                            store.manifest_envelope(envelope.digest).to_dict()
                        ).encode()
                    elif self.path.startswith("/chunks/"):
                        index = self.path.removeprefix("/chunks/")
                        if not index.isascii() or not index.isdecimal() or len(index) > 6:
                            raise ContentRejected("chunk_index")
                        body = store.read_chunk(handle, int(index))
                    else:
                        raise ContentRejected("unknown_endpoint")
                    with outer._lock:
                        if outer._served + len(body) > max_served_bytes:
                            raise ContentRejected("serving_quota")
                        outer._served += len(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (ContentRejected, ValueError):
                    self.send_error(403, "content unavailable")

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = _BoundedPeerHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def served_bytes(self) -> int:
        with self._lock:
            return self._served

    def pause(self, *, paused: bool = True) -> None:
        self._paused.set() if paused else self._paused.clear()

    def __enter__(self) -> LoopbackContentServer:
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


def fetch_local_artifact(
    endpoint: str,
    *,
    expected_manifest_digest: str,
    destination: VerifiedContentStore,
    maximum_download_bytes: int,
    cancellation: Event | None = None,
    timeout: float = 10,
) -> Path:
    """Explicit transfer only; already verified chunks are resumed without refetching."""
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not 0 < timeout <= 600
        or type(maximum_download_bytes) is not int
        or maximum_download_bytes < 1
    ):
        raise ValueError("content origin must be an explicit bounded loopback endpoint")
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    downloaded = 0

    def fetch(path: str, bound: int) -> bytes:
        nonlocal downloaded
        if cancellation is not None and cancellation.is_set():
            raise ContentRejected("transfer_cancelled")
        if downloaded >= maximum_download_bytes:
            raise ContentRejected("download_quota")
        bound = min(bound, maximum_download_bytes - downloaded)
        try:
            with opener.open(Request(endpoint.rstrip("/") + path), timeout=timeout) as response:
                data = response.read(bound + 1)
        except (URLError, TimeoutError) as error:
            raise ContentRejected("origin_unavailable") from error
        if not isinstance(data, bytes) or len(data) > bound:
            raise ContentRejected("download_quota")
        downloaded += len(data)
        return data

    envelope = SignedEnvelope.from_dict(json.loads(fetch("/manifest", 2 * 1_048_576)))
    if envelope.digest != expected_manifest_digest:
        raise ContentRejected("manifest_substitution")
    handle = destination.begin(envelope)
    manifest = ArtifactManifest.from_dict(json.loads(envelope.payload_json))
    for index in destination.missing_chunks(handle):
        data = fetch(f"/chunks/{index}", manifest.chunks[index].byte_size)
        destination.write_chunk(handle, index, data)
    if cancellation is not None and cancellation.is_set():
        raise ContentRejected("transfer_cancelled")
    return destination.materialize(handle)
