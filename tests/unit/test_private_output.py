"""Failed credential output compensates authority without removing a replaced path."""

from pathlib import Path

import pytest

from af.interfaces.private_output import write_private_json


def test_private_output_preserves_replaced_path_and_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "credential"
    compensated: list[dict[str, object]] = []

    def sync(descriptor: int) -> None:
        output.unlink()
        output.write_text("replacement")
        raise OSError("disk failed")

    def compensate(result: dict[str, object]) -> None:
        compensated.append(result)
        raise ValueError("secondary failure")

    monkeypatch.setattr("af.interfaces.private_output.os.fsync", sync)
    with pytest.raises(OSError, match="disk failed"):
        write_private_json(output, lambda: {"secret": "private"}, compensate)
    assert compensated == [{"secret": "private"}]
    assert output.read_text() == "replacement"


def test_private_output_cleanup_success_and_exclusive_claim(tmp_path: Path) -> None:
    output = tmp_path / "credential"
    result = write_private_json(output, lambda: {"value": 1}, lambda value: None)
    assert result == {"value": 1}
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_private_json(output, lambda: {"value": 2}, lambda value: None)
    output.unlink()

    def fail() -> dict[str, object]:
        raise ValueError("invalid")

    with pytest.raises(ValueError):
        write_private_json(output, fail, lambda value: None)
    assert not output.exists()
