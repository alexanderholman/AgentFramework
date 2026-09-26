from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from af.interfaces.console import create_cli


def test_capability_discovery_is_explicit_and_nonexecuting(application, tmp_path: Path) -> None:
    runner = CliRunner()
    cli = create_cli(application)
    result = runner.invoke(cli, ["capabilities", "--json"])
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["records"] == []
    assert snapshot["authorized_capability_ids"] == []
    assert snapshot["diagnostics"]
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text('{"schema_version":"1.0.0","records":[]}')
    result = runner.invoke(cli, ["capabilities", "--catalogue", str(catalogue), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["authorized_capability_ids"] == []
    request = tmp_path / "request.json"
    request.write_text('{"execute":"shell"}')
    result = runner.invoke(cli, ["capabilities", "--request", str(request)])
    assert result.exit_code == 2
    assert application.tasks.list() == []


def test_additional_contract_schemas_are_machine_readable(application) -> None:
    runner = CliRunner()
    cli = create_cli(application)
    for name in [
        "decision-request",
        "decision-result",
        "workflow-checkpoint",
        "capability-request",
        "evolution-snapshot",
    ]:
        result = runner.invoke(cli, ["schemas", name])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["type"] == "object"


def capability_request() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "request_id": "count-request",
        "capability_classes": ["procedural"],
        "modality": "text",
        "domain": "text",
        "intent": "count",
        "input_schema": "count-input.v1",
        "output_schema": "count-output.v1",
        "language": "en",
        "locale": "en-GB",
        "input_bytes": 10,
        "expected_output_bytes": 10,
        "context_items": 1,
        "required_verifiers": ["count-verifier"],
        "allowed_effects": ["read_only"],
        "max_risk": "low",
        "accepted_localities": ["local_only"],
        "accepted_licenses": ["MIT"],
        "accepted_lifecycles": ["benchmarked"],
        "require_authorized": True,
        "require_verified_quality": True,
        "required_runtime_id": None,
        "max_resources": {
            "schema_version": "1.0.0",
            "latency_ms": 100,
            "memory_mib": 100,
            "cpu_millis": 100,
            "accelerator_millis": 0,
            "estimated_cost_micros": 0,
        },
    }


def test_imported_operational_claims_cannot_authorize_capability_execution(
    application, tmp_path: Path
) -> None:
    requirements = capability_request()
    descriptor = {
        "schema_version": "1.0.0",
        "capability_id": "character-count",
        "asset_id": "count-asset",
        "provider_id": "local",
        "runtime_id": "python",
        "deployment_id": None,
        "procedure_id": "character-count",
        "workflow_id": None,
        "capability_class": "procedural",
        "modality": "text",
        "domain": "text",
        "intent": "count",
        "input_schema": "count-input.v1",
        "output_schema": "count-output.v1",
        "languages": ["en"],
        "locales": ["en-GB"],
        "max_input_bytes": 4096,
        "max_output_bytes": 256,
        "max_context_items": 8,
        "declared_quality_evidence": ["claimed-pass"],
        "verifier_requirements": ["count-verifier"],
        "effects": ["read_only"],
        "risk": "low",
        "locality": "local_only",
        "licence": "MIT",
        "declared_state": "benchmarked",
        "resource_envelope": requirements["max_resources"],
        "dependency_ids": [],
        "fallback_ids": [],
    }
    request, catalogue = tmp_path / "request.json", tmp_path / "catalogue.json"
    request.write_text(json.dumps(requirements))
    catalogue.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "records": [
                    {
                        "descriptor": descriptor,
                        "authorized": True,
                        "installed": True,
                        "healthy": True,
                        "quality_verified": True,
                    }
                ],
            }
        )
    )
    result = CliRunner().invoke(
        create_cli(application),
        ["capabilities", "--catalogue", str(catalogue), "--request", str(request)],
    )
    assert result.exit_code == 0, result.output
    preview = json.loads(result.output)
    assert preview["outcome"] == "policy_denied"
    assert preview["execution_performed"] is False
    assert preview["execution_authorized"] is False
    assert preview["completed"] is False
    assert preview["selected_capability_id"] is None
    assert preview["routes"][0]["reason_codes"] == ["policy_denied"]
    assert preview["routes"][0]["evidence"] == []
    assert application.tasks.list() == []


def test_valid_request_against_default_catalogue_reports_unsupported_without_execution(
    application, tmp_path: Path
) -> None:
    request = tmp_path / "request.json"
    request.write_text(json.dumps(capability_request()))
    result = CliRunner().invoke(
        create_cli(application), ["capabilities", "--request", str(request)]
    )
    assert result.exit_code == 0, result.output
    preview = json.loads(result.output)
    assert preview["outcome"] == "unsupported"
    assert preview["execution_performed"] is False
    assert application.tasks.list() == []


def test_capability_file_errors_are_bounded_sanitized_and_nonexecuting(application, tmp_path: Path):
    runner = CliRunner()
    cli = create_cli(application)
    path = tmp_path / "operator-secret.json"
    for content in (
        "[]",
        '{"schema_version":"1.0.0","records":[],"records":[]}',
        '{"schema_version":"unexpected","records":[]}',
        '{"schema_version":"1.0.0","records":[],"cost":1e9999}',
        "{" + "private" * 100,
    ):
        path.write_text(content)
        failed = runner.invoke(cli, ["capabilities", "--catalogue", str(path)])
        assert failed.exit_code == 2
        assert json.loads(failed.output) == {"error": "invalid_capability_request"}
    path.unlink()
    missing = runner.invoke(cli, ["capabilities", "--request", str(path)])
    assert missing.exit_code == 2
    assert json.loads(missing.output) == {"error": "invalid_capability_request"}
    path.write_text(" " * 128_001)
    oversized = runner.invoke(cli, ["capabilities", "--request", str(path)])
    assert oversized.exit_code == 2
    assert json.loads(oversized.output) == {"error": "invalid_capability_request"}
    assert application.tasks.list() == []
