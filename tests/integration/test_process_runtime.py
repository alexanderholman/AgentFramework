from __future__ import annotations

import hashlib
import os
import socket
import sys
import time
from contextlib import suppress
from dataclasses import replace
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


def test_cancellation_during_integrity_checks_prevents_process_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from af.infrastructure import process_runtime

    installed = spec(tmp_path)
    adapter = PinnedProcessRuntime((installed,))
    cancellation = Event()
    original_digest = process_runtime._file_digest

    def cancel_during_hash(path: Path) -> str:
        value = original_digest(path)
        cancellation.set()
        return value

    def forbidden_spawn(*args: object, **kwargs: object) -> None:
        pytest.fail("a cancelled load must not create a process")

    monkeypatch.setattr(process_runtime, "_file_digest", cancel_during_hash)
    monkeypatch.setattr(process_runtime.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(RuntimeRefused, match="cancelled"):
        adapter.load(installed.variant, cancellation, datetime.now(UTC) + timedelta(seconds=5))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process tree conformance")
def test_unload_kills_descendants_even_when_leader_exits_on_sigterm(tmp_path: Path) -> None:
    installed = spec(tmp_path)
    script = installed.support_files[0][0]
    child_pid = tmp_path / "child.pid"
    child_source = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_pid)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    code = script.read_text().replace(
        "HTTPServer((",
        f"import subprocess, time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {child_source!r}])\n"
        f"while not Path({str(child_pid)!r}).exists(): time.sleep(.01)\nHTTPServer((",
    )
    script.write_text(code)
    installed = replace(installed, support_files=((script, sha(script)),))
    adapter = PinnedProcessRuntime((installed,))
    adapter.load(installed.variant, Event(), datetime.now(UTC) + timedelta(seconds=5))
    pid = int(child_pid.read_text())
    try:
        adapter.unload(installed.variant)
        until = time.monotonic() + 1
        running = True
        while time.monotonic() < until:
            status = Path(f"/proc/{pid}/stat")
            running = status.exists() and status.read_text().split()[2] != "Z"
            if not running:
                break
            time.sleep(0.01)
        assert not running, "runtime descendant survived an acknowledged unload"
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, 9)
