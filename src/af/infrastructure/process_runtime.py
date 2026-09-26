"""Pinned, operator-installed local process adapter for the model residency broker.

No runtime pack or model is fetched. The operator registers exact launch arguments
and supporting files. Readiness uses authenticated loopback health and model IDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from af.application.model_runtime import RuntimeHealth, RuntimeRefused, RuntimeVariant
from af.framework.content import require_digest
from af.infrastructure.peers import _NoRedirects


def _file_digest(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeRefused("local_file_unavailable")
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True, slots=True)
class LocalRuntimeProcess:
    variant: RuntimeVariant
    executable: Path
    arguments: tuple[str, ...]
    model_path: Path
    endpoint: str
    served_model: str
    api_key: str = field(repr=False)
    environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    support_files: tuple[tuple[Path, str], ...] = ()

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("runtime endpoint must be explicit IPv4 loopback HTTP")
        if (
            type(self.arguments) is not tuple
            or len(self.arguments) > 256
            or "{model}" not in self.arguments
            or any(
                not isinstance(arg, str) or len(arg) > 8192 or "\x00" in arg
                for arg in self.arguments
            )
        ):
            raise ValueError("runtime arguments must be bounded and bind the exact local model")
        if not self.executable.is_absolute() or not self.model_path.is_absolute():
            raise ValueError("runtime files must use absolute local paths")
        if not self.api_key or len(self.api_key) > 4096 or any(ord(c) < 32 for c in self.api_key):
            raise ValueError("runtime authentication key is invalid")
        if not self.served_model or len(self.served_model) > 256:
            raise ValueError("runtime model identity must be bounded")
        if type(self.environment) is not tuple or len(self.environment) > 64:
            raise ValueError("runtime environment must be explicit and bounded")
        names: set[str] = set()
        for key, value in self.environment:
            if (
                re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", key) is None
                or len(value) > 8192
                or "\x00" in value
                or key in names
            ):
                raise ValueError("runtime environment is invalid")
            names.add(key)
        if type(self.support_files) is not tuple or len(self.support_files) > 4096:
            raise ValueError("runtime supporting files must be pinned and bounded")
        for path, digest in self.support_files:
            if not path.is_absolute():
                raise ValueError("runtime supporting files must use absolute paths")
            require_digest(digest)


class PinnedProcessRuntime:
    def __init__(self, installed: tuple[LocalRuntimeProcess, ...]) -> None:
        if len({spec.variant.digest for spec in installed}) != len(installed):
            raise ValueError("runtime process registrations must be unique")
        self._installed = {spec.variant.digest: spec for spec in installed}
        self._process: subprocess.Popen[bytes] | None = None
        self._variant: RuntimeVariant | None = None
        self._lock = RLock()

    def load(self, variant: RuntimeVariant, cancellation: Event, deadline: datetime) -> None:
        if deadline.utcoffset() is None:
            raise ValueError("runtime deadline must be timezone aware")
        with self._lock:
            self._check(cancellation, deadline)
            spec = self._installed.get(variant.digest)
            if spec is None:
                raise RuntimeRefused("runtime_not_installed")
            if _file_digest(spec.model_path) != variant.artifact_digest:
                raise RuntimeRefused("artifact_digest_mismatch")
            if _file_digest(spec.executable) != variant.runtime_digest:
                raise RuntimeRefused("runtime_digest_mismatch")
            for path, digest in spec.support_files:
                if _file_digest(path) != digest:
                    raise RuntimeRefused("support_file_digest_mismatch")
            if self._process is not None:
                if self._variant == variant and self.health(variant).ready_for(variant):
                    return
                raise RuntimeRefused("runtime_already_resident")
            self._check_endpoint_unused(spec.endpoint)
            environment = {
                "PATH": os.defpath,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "DO_NOT_TRACK": "1",
                **dict(spec.environment),
            }
            # Offline settings cannot be weakened by the registered environment.
            environment.update(
                {
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "HF_HUB_DISABLE_TELEMETRY": "1",
                    "DO_NOT_TRACK": "1",
                }
            )
            argv = [
                str(spec.executable),
                *(str(spec.model_path) if item == "{model}" else item for item in spec.arguments),
            ]
            self._check(cancellation, deadline)
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                cwd=spec.model_path.parent,
                start_new_session=True,
            )
            self._variant = variant
            try:
                while True:
                    self._check(cancellation, deadline)
                    remaining = (deadline - datetime.now(UTC)).total_seconds()
                    health = self._health(variant, timeout=min(1.0, max(0.001, remaining / 2)))
                    if health.ready_for(variant):
                        self._check(cancellation, deadline)
                        return
                    if not health.process_alive:
                        raise RuntimeRefused("runtime_exited")
                    cancellation.wait(timeout=min(0.05, max(0.001, remaining)))
            except BaseException:
                self.unload(variant)
                raise

    @staticmethod
    def _check(cancellation: Event, deadline: datetime) -> None:
        if cancellation.is_set():
            raise RuntimeRefused("cancelled")
        if datetime.now(UTC) >= deadline:
            raise RuntimeRefused("deadline_exceeded")

    @staticmethod
    def _check_endpoint_unused(endpoint: str) -> None:
        parsed = urlparse(endpoint)
        assert parsed.port is not None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", parsed.port))
            except OSError as error:
                raise RuntimeRefused("runtime_endpoint_in_use") from error

    def health(self, variant: RuntimeVariant) -> RuntimeHealth:
        with self._lock:
            return self._health(variant, timeout=1)

    def _health(self, variant: RuntimeVariant, *, timeout: float) -> RuntimeHealth:
        process = self._process
        if process is None or self._variant != variant or process.poll() is not None:
            return RuntimeHealth(False, False, False, False, variant.digest, "process_stopped")
        spec = self._installed[variant.digest]
        opener = build_opener(ProxyHandler({}), _NoRedirects())
        headers = {"Authorization": "Bearer " + spec.api_key}
        try:
            with opener.open(
                Request(spec.endpoint.rstrip("/") + "/health", headers=headers), timeout=timeout
            ) as response:
                ready = response.status == 200 and len(response.read(8193)) <= 8192
            with opener.open(
                Request(spec.endpoint.rstrip("/") + "/v1/models", headers=headers), timeout=timeout
            ) as response:
                body = response.read(65_537)
            if len(body) > 65_536:
                raise RuntimeRefused("runtime_response_size")
            payload: object = json.loads(body)
            models = payload.get("data") if isinstance(payload, dict) else None
            exact_model = isinstance(models, list) and any(
                isinstance(model, dict) and model.get("id") == spec.served_model for model in models
            )
            return RuntimeHealth(process.poll() is None, True, ready, exact_model, variant.digest)
        except (URLError, TimeoutError, OSError, ValueError, RuntimeRefused):
            return RuntimeHealth(
                process.poll() is None, False, False, False, variant.digest, "endpoint_not_ready"
            )

    def unload(self, variant: RuntimeVariant) -> None:
        with self._lock:
            if self._process is None:
                return
            if self._variant != variant:
                raise RuntimeRefused("runtime_variant_mismatch")
            process = self._process
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=2)
            if os.name == "posix":
                # A runtime leader may exit promptly while a worker ignores TERM.
                # Its original process group remains ours until all workers stop.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            self._process, self._variant = None, None
