from __future__ import annotations

import hashlib
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from af.application.model_runtime import RuntimeRefused, RuntimeVariant
from af.framework.content import content_digest
from af.infrastructure.process_runtime import LocalRuntimeProcess, PinnedProcessRuntime


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def spec(tmp_path: Path) -> LocalRuntimeProcess:
    script = tmp_path / "installed_runtime_fixture.py"
    script.write_text("""import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + os.environ["AF_RUNTIME_TOKEN"]:
            self.send_error(401)
            return
        data = {"data": [{"id": "fixture"}]} if self.path == "/v1/models" else {"status":"ok"}
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
""")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture bytes, not a real model")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    executable = Path(sys.executable).resolve()
    variant = RuntimeVariant(
        "fixture",
        sha(model),
        sha(executable),
        content_digest(b"adapter"),
        content_digest(b"cpu"),
        100,
    )
    return LocalRuntimeProcess(
        variant=variant,
        executable=executable,
        arguments=(str(script), str(port), "{model}"),
        model_path=model,
        endpoint=f"http://127.0.0.1:{port}",
        served_model="fixture",
        api_key="local-conformance-key",
        environment=(("AF_RUNTIME_TOKEN", "local-conformance-key"),),
        support_files=((script, sha(script)),),
    )


def test_pinned_runtime_starts_checks_exact_model_readiness_and_terminates(tmp_path: Path) -> None:
    installed = spec(tmp_path)
    adapter = PinnedProcessRuntime((installed,))
    assert not adapter.health(installed.variant).process_alive
    adapter.load(installed.variant, Event(), datetime.now(UTC) + timedelta(seconds=5))
    try:
        assert adapter.health(installed.variant).ready_for(installed.variant)
        adapter.load(installed.variant, Event(), datetime.now(UTC) + timedelta(seconds=5))
    finally:
        adapter.unload(installed.variant)
    assert not adapter.health(installed.variant).process_alive


def test_runtime_refuses_missing_or_changed_local_artifact_without_fetch(tmp_path: Path) -> None:
    installed = spec(tmp_path)
    adapter = PinnedProcessRuntime((installed,))
    installed.model_path.write_bytes(b"changed")
    with pytest.raises(RuntimeRefused, match="artifact_digest_mismatch"):
        adapter.load(installed.variant, Event(), datetime.now(UTC) + timedelta(seconds=5))
    assert not adapter.health(installed.variant).process_alive


def test_runtime_cancellation_and_wrong_model_never_reach_ready(tmp_path: Path) -> None:
    from dataclasses import replace

    installed = spec(tmp_path)
    adapter = PinnedProcessRuntime((installed,))
    cancelled = Event()
    cancelled.set()
    with pytest.raises(RuntimeRefused, match="cancelled"):
        adapter.load(installed.variant, cancelled, datetime.now(UTC) + timedelta(seconds=1))
    mismatch = replace(installed, served_model="different-model")
    adapter = PinnedProcessRuntime((mismatch,))
    with pytest.raises(RuntimeRefused, match="deadline_exceeded"):
        adapter.load(installed.variant, Event(), datetime.now(UTC) + timedelta(seconds=0.3))
    assert not adapter.health(installed.variant).process_alive
