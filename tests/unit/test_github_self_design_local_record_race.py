from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from af.framework.self_design_publication import (
    DesignPublicationError,
    DesignPublicationRequest,
)
from af.infrastructure.github_self_design import GitHubDesignPublisher


class ExactRecord:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def canonical_bytes(self) -> bytes:
        return self._content


def test_local_record_rejects_path_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = b'{"exact":true}\n'
    record_path = tmp_path / "record.json"
    replacement_path = tmp_path / "replacement.json"
    displaced_path = tmp_path / "displaced.json"
    record_path.write_bytes(expected)
    replacement_path.write_bytes(expected)
    request = cast(
        DesignPublicationRequest,
        SimpleNamespace(
            recorded=SimpleNamespace(
                record=ExactRecord(expected),
                record_path=str(record_path),
            )
        ),
    )
    original_read = os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not replaced:
            replaced = True
            try:
                record_path.rename(displaced_path)
                replacement_path.rename(record_path)
            except OSError:
                pytest.skip("open-file replacement is unavailable on this platform")
        return chunk

    monkeypatch.setattr(os, "read", replacing_read)

    with pytest.raises(DesignPublicationError, match="changed during verification"):
        GitHubDesignPublisher._verify_local_record(request)
