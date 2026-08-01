"""Reusable file scaffolding for a deterministic procedure example."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from af.framework.types import is_valid_procedure_id

_PACKAGE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass(frozen=True, slots=True)
class ProcedureScaffoldResult:
    procedure_id: str
    package: str
    application_factory: str
    root: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "procedure_id": self.procedure_id,
            "package": self.package,
            "application_factory": self.application_factory,
            "root": self.root,
            "files": list(self.files),
        }


def scaffold_procedure(*, root: Path, procedure_id: str, package: str) -> ProcedureScaffoldResult:
    """Write a minimal provider-based deterministic procedure example into ``root``."""

    if not is_valid_procedure_id(procedure_id):
        raise ValueError(f"invalid procedure id: {procedure_id!r}")
    if _PACKAGE_PATTERN.fullmatch(package) is None:
        raise ValueError(f"invalid package name: {package!r}")

    slug = procedure_id.replace("-", "_")
    class_name = "".join(part.capitalize() for part in re.split(r"[-_]", procedure_id) if part)
    intent = f"text.{slug}"
    provider_id = f"generated.procedure.{procedure_id}"
    application_factory = f"{package}.application:build_application"
    package_directory = root.joinpath(*package.split("."))
    files = (
        package_directory / "__init__.py",
        package_directory / "application.py",
        package_directory / "provider.py",
        package_directory / "procedure.py",
        root / "tests" / "__init__.py",
        root / "tests" / f"test_{slug}_behavior.py",
        root / "tests" / f"test_{slug}_contract.py",
        root / "tests" / f"test_{slug}_failure.py",
        root / "tests" / f"test_{slug}_unit.py",
    )
    for path in files:
        if path.exists():
            relative = path.relative_to(root).as_posix()
            raise ValueError(f"refusing to overwrite existing path: {relative}")

    rendered = {
        package_directory / "__init__.py": _package_init(),
        package_directory / "application.py": _application_module(
            package=package,
            class_name=class_name,
        ),
        package_directory / "provider.py": _provider_module(
            class_name=class_name,
            provider_id=provider_id,
        ),
        package_directory / "procedure.py": _procedure_module(
            class_name=class_name,
            intent=intent,
            procedure_id=procedure_id,
        ),
        root / "tests" / "__init__.py": "",
        root / "tests" / f"test_{slug}_behavior.py": _behavior_test(
            package=package,
            procedure_id=procedure_id,
            slug=slug,
        ),
        root / "tests" / f"test_{slug}_contract.py": _contract_test(
            package=package,
            procedure_id=procedure_id,
            intent=intent,
        ),
        root / "tests" / f"test_{slug}_failure.py": _failure_test(
            package=package,
            procedure_id=procedure_id,
        ),
        root / "tests" / f"test_{slug}_unit.py": _unit_test(
            package=package,
            class_name=class_name,
            intent=intent,
        ),
    }
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    return ProcedureScaffoldResult(
        procedure_id=procedure_id,
        package=package,
        application_factory=application_factory,
        root=str(root),
        files=tuple(path.relative_to(root).as_posix() for path in files),
    )


def _package_init() -> str:
    return '"""Generated deterministic procedure example package."""\n'


def _application_module(*, package: str, class_name: str) -> str:
    return f'''"""Generated AF composition root for one deterministic procedure example."""

from __future__ import annotations

from af.bootstrap import AFApplication, create_application
from af.config import Settings
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository

from {package}.provider import {class_name}Provider


def build_application() -> AFApplication:
    return create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=({class_name}Provider(),),
    )
'''


def _provider_module(*, class_name: str, provider_id: str) -> str:
    return f'''"""Generated provider that registers one deterministic procedure bundle."""

from __future__ import annotations

from af.framework.container import ProviderRegistrar, ServiceProvider
from af.framework.procedure_contributors import (
    InstalledProcedureContribution,
    bind_procedure_contributor,
)

from .procedure import {class_name}Classifier, {class_name}Procedure, {class_name}Verifier


class {class_name}Provider(ServiceProvider):
    provider_id = "{provider_id}"

    def register(self, registrar: ProviderRegistrar) -> None:
        bind_procedure_contributor(
            registrar,
            InstalledProcedureContribution(
                classifier={class_name}Classifier(),
                procedure={class_name}Procedure(),
                verifier={class_name}Verifier(),
            ),
        )
'''


def _procedure_module(*, class_name: str, intent: str, procedure_id: str) -> str:
    return f'''"""Generated deterministic procedure example."""

from __future__ import annotations

from af.domain.procedures import BaseProcedure
from af.framework.errors import InvalidSelectionProposal
from af.framework.types import (
    PROMPT_MAX_LENGTH,
    ArgumentValueType,
    Classification,
    ProcedureArgumentDescriptor,
    ProcedureDescriptor,
    ProcedureEffect,
    ProcedureResult,
    ProcedureRisk,
    RouteMetrics,
    VerificationResult,
    WorkRequest,
    WorkTier,
)


class {class_name}Classifier:
    def classify(self, request: WorkRequest) -> Classification | None:
        prefix = "{procedure_id} "
        if not request.prompt.casefold().startswith(prefix):
            return None
        text = request.prompt[len(prefix) :].strip()
        if not text:
            return None
        return Classification(
            intent="{intent}",
            arguments={{"text": text}},
            confidence=1.0,
            classifier=type(self).__name__,
        )


class {class_name}Procedure(BaseProcedure):
    id = "{procedure_id}"
    version = "1.0.0"
    tier = WorkTier.PROCEDURAL
    pure = True
    replay_safe = True
    metrics = RouteMetrics(cost=1, latency=1, intelligence=1)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=version,
        intent="{intent}",
        arguments=(
            ProcedureArgumentDescriptor(
                name="text",
                value_type=ArgumentValueType.TEXT,
                min_length=1,
                max_length=PROMPT_MAX_LENGTH,
                allow_blank=False,
            ),
        ),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
        requires_confirmation=False,
    )

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "{intent}"

    def execute(self, classification: Classification) -> ProcedureResult:
        text = classification.arguments["text"]
        return ProcedureResult(output=text.upper(), evidence={{"text": text}})

    def validate(self, classification: Classification) -> None:
        if classification.intent != "{intent}":
            raise InvalidSelectionProposal("unsupported intent for {procedure_id}")
        if set(classification.arguments) != {{"text"}}:
            raise InvalidSelectionProposal("invalid arguments for {procedure_id}")
        if not classification.arguments["text"]:
            raise InvalidSelectionProposal("invalid arguments for {procedure_id}")


class {class_name}Verifier:
    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: {class_name}Procedure,
        result: ProcedureResult,
    ) -> VerificationResult | None:
        del request, procedure
        if classification.intent != "{intent}":
            return None
        expected = classification.arguments["text"].upper()
        passed = result.output == expected
        return VerificationResult(
            passed,
            "independent uppercase matched" if passed else "independent uppercase mismatch",
        )
'''


def _unit_test(*, package: str, class_name: str, intent: str) -> str:
    return f'''from __future__ import annotations

from af.framework.types import Classification

from {package}.procedure import {class_name}Classifier, {class_name}Procedure


def test_classifier_extracts_text_argument() -> None:
    request = type("Request", (), {{"prompt": "uppercase hello world"}})()
    classification = {class_name}Classifier().classify(request)

    assert classification == Classification(
        intent="{intent}",
        arguments={{"text": "hello world"}},
        confidence=1.0,
        classifier="{class_name}Classifier",
    )


def test_procedure_uppercases_text() -> None:
    result = {class_name}Procedure().execute(
        Classification(
            intent="{intent}",
            arguments={{"text": "hello world"}},
            confidence=1.0,
            classifier="manual",
        )
    )

    assert result.output == "HELLO WORLD"
    assert result.evidence == {{"text": "hello world"}}
'''


def _contract_test(*, package: str, procedure_id: str, intent: str) -> str:
    return f'''from __future__ import annotations

import json

from typer.testing import CliRunner

from af.interfaces.console import create_cli

runner = CliRunner()


def test_routes_expose_stable_procedure_identity() -> None:
    result = runner.invoke(
        create_cli(),
        ["--application-factory", "{package}.application:build_application", "routes"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    descriptor = next(item for item in payload if item["procedure_id"] == "{procedure_id}")
    assert descriptor["procedure_id"] == "{procedure_id}"
    assert descriptor["version"] == "1.0.0"
    assert descriptor["intent"] == "{intent}"
    assert descriptor["effect"] == "read_only"
    assert descriptor["risk"] == "none"
    assert descriptor["digest"].startswith("sha256:")
'''


def _behavior_test(*, package: str, procedure_id: str, slug: str) -> str:
    return f'''from __future__ import annotations

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.interfaces.console import create_cli
from af.interfaces.http import create_http_app

from {package}.application import build_application

runner = CliRunner()


def test_{slug}_runs_through_cli_and_rest() -> None:
    application = build_application()
    try:
        cli = runner.invoke(create_cli(application), ["ask", "{procedure_id} hello world"])

        assert cli.exit_code == 0
        assert cli.stdout == "HELLO WORLD\\n"

        with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
            response = client.post("/api/v1/tasks", json={{"prompt": "{procedure_id} hello world"}})

        assert response.status_code == 201
        payload = response.json()
        assert payload["output"] == "HELLO WORLD"
        assert payload["route"] == "{procedure_id}"
        assert payload["intent"] == "text.{slug}"
        assert payload["verified"] is True
    finally:
        application.shutdown()
'''


def _failure_test(*, package: str, procedure_id: str) -> str:
    return f'''from __future__ import annotations

import pytest

from af.framework.errors import UnsupportedWork, VerificationFailed

from {package}.application import build_application


def test_blank_payload_has_no_route() -> None:
    application = build_application()
    try:
        with pytest.raises(UnsupportedWork):
            application.tasks.execute("{procedure_id}   ")
    finally:
        application.shutdown()


def test_verifier_failure_fails_closed() -> None:
    application = build_application()
    try:
        application.verifier.force_failure = True
        with pytest.raises(VerificationFailed):
            application.tasks.execute("{procedure_id} hello world")
    finally:
        application.shutdown()
'''
