from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.interfaces.console import create_cli


def test_installed_decision_preview_and_persistent_consent(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"kind": "binary", "question": "Is 2 even?", "state": "2"}))
    preview = tmp_path / "preview.json"
    consent = tmp_path / "consent.json"
    runner = CliRunner()
    first = create_application(settings=settings)
    try:
        cli = create_cli(first)
        result = runner.invoke(cli, ["decisions", "preview", "--request", str(request)])
        assert result.exit_code == 0, result.output
        disclosure = json.loads(result.output)
        assert disclosure["destination"] == "https://api.typesafe.ai/v1/systemone"
        preview.write_text(result.output)
        approved = runner.invoke(
            cli,
            [
                "decisions",
                "approve",
                "--request",
                str(request),
                "--preview",
                str(preview),
                "--max-cost-usd",
                "0.01",
                "--output",
                str(consent),
            ],
        )
        assert approved.exit_code == 0, approved.output
        assert consent.stat().st_mode & 0o077 == 0
        assert json.loads(consent.read_text())["token"] not in approved.output
    finally:
        first.shutdown()
    second = create_application(settings=settings)
    try:
        cli = create_cli(second)
        result = runner.invoke(
            cli, ["decisions", "evaluate", "--request", str(request), "--consent", str(consent)]
        )
        assert result.exit_code == 2
        assert json.loads(result.output)["status"] == "denied"
        result = runner.invoke(cli, ["decisions", "revoke", "--consent", str(consent)])
        assert result.exit_code == 0
    finally:
        second.shutdown()


def test_locale_cli_keeps_canonical_output(application) -> None:
    runner = CliRunner()
    cli = create_cli(application)
    plain = runner.invoke(cli, ["ask", "hello"])
    assert plain.exit_code == 0
    localized = runner.invoke(
        cli, ["ask", "hello", "--language", "fr", "--territory", "CA", "--json"]
    )
    assert localized.exit_code == 0, localized.output
    payload = json.loads(localized.output)
    assert payload["output"] == plain.output.strip()
    assert payload["presentation"]["resolved_language"] == "fr"
    assert payload["presentation"]["territory"] == "CA"
    assert payload["verified"] is True
