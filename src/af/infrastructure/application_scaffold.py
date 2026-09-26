"""Finite application-owned starter generation, with no overwrite or code ingestion."""

from __future__ import annotations

import hashlib
import json
import keyword
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from pathlib import Path

from af import __version__


class StarterTemplate(StrEnum):
    BASIC_WEB = "basic-web"
    PRETTY_WEB = "pretty-web"
    REST_API = "rest-api"
    WORKER = "worker"
    TUI = "tui"
    FULL_STACK = "full-stack"


@dataclass(frozen=True, slots=True)
class StarterManifest:
    root: str
    template: StarterTemplate
    package: str
    application_factory: str
    paths: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "template": self.template.value,
            "package": self.package,
            "application_factory": self.application_factory,
            "files": self.paths,
        }


def _render(resource: str, replacements: dict[str, str]) -> str:
    text = (
        files("af.infrastructure")
        .joinpath("starter_templates", resource + ".tmpl")
        .read_text(encoding="utf-8")
    )
    for key, value in replacements.items():
        text = text.replace(f"__{key}__", value)
    return text


def scaffold_application(*, root: Path, package: str, template: StarterTemplate) -> StarterManifest:
    if (
        not isinstance(package, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", package) is None
        or keyword.iskeyword(package)
        or package in sys.stdlib_module_names
        or package in {"af", "tests", "fastapi", "typer", "pydantic", "pytest", "uvicorn", "httpx"}
    ):
        raise ValueError("starter package must be a nonreserved lowercase Python identifier")
    if not isinstance(template, StarterTemplate):
        raise ValueError("choose an installed starter template")
    if root.exists() or root.is_symlink():
        raise ValueError("starter destination must not already exist")
    web = template in {
        StarterTemplate.BASIC_WEB,
        StarterTemplate.PRETTY_WEB,
        StarterTemplate.REST_API,
        StarterTemplate.FULL_STACK,
    }
    worker = template in {StarterTemplate.WORKER, StarterTemplate.FULL_STACK}
    replacements = {
        "PACKAGE": package,
        "PROVIDER_IMPORT": "from .provider import StarterTuiProvider"
        if template is StarterTemplate.TUI
        else "",
        "PROVIDERS": "(StarterTuiProvider(),)" if template is StarterTemplate.TUI else "()",
        "WEB_PROFILE": "pretty"
        if template in {StarterTemplate.PRETTY_WEB, StarterTemplate.FULL_STACK}
        else "basic",
        "REST_ONLY": str(template is StarterTemplate.REST_API),
    }
    resources = {
        f"{package}/application.py": "application.py",
        f"{package}/cli.py": "cli.py",
        f"{package}/services.py": "services.py",
        "tests/test_cli.py": "test_cli.py",
        ".github/workflows/ci.yml": "ci.yml",
    }
    entry = "cli"
    if web:
        entry = "web"
        resources.update({f"{package}/web.py": "web.py", "tests/test_web.py": "test_web.py"})
        if template is not StarterTemplate.REST_API:
            resources.update(
                {
                    f"{package}/frontend/index.html": "index.html",
                    f"{package}/frontend/app.js": "app.js",
                    f"{package}/frontend/style.css": "basic.css"
                    if template is StarterTemplate.BASIC_WEB
                    else "style.css",
                }
            )
    if worker:
        resources.update(
            {f"{package}/worker.py": "worker.py", "tests/test_worker.py": "test_worker.py"}
        )
        if not web:
            entry = "worker"
    if template is StarterTemplate.TUI:
        resources.update(
            {
                f"{package}/tui.py": "tui.py",
                "tests/test_tui.py": "test_tui.py",
                f"{package}/tui_app.py": "tui_app.py",
                f"{package}/provider.py": "tui_provider.py",
            }
        )
        entry = "tui"
    content = {path: _render(resource, replacements) for path, resource in resources.items()}
    content[f"{package}/__init__.py"] = '"""Application-owned AF starter package."""\n'
    content[f"{package}/__main__.py"] = f"from .{entry} import main\n\nmain()\n"
    content[".gitignore"] = "var/\n__pycache__/\n.pytest_cache/\n*.egg-info/\n.venv/\n"
    content["pyproject.toml"] = f'''[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"

[project]
name = "{package.replace("_", "-")}-application"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["af-agent-framework=={__version__}"]

[project.optional-dependencies]
dev = ["pytest>=8,<10", "httpx>=0.28,<1"]

[project.scripts]
{package} = "{package}.{entry}:main"
{package}-cli = "{package}.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["{package}"]

[tool.pytest.ini_options]
testpaths = ["tests"]
'''
    content["README.md"] = f"""# {package}

Application-owned `{template.value}` starter for AF {__version__}.
Install the approved matching AF distribution/wheel, then `python -m pip install -e '.[dev]'`.
Run `python -m pytest tests`. The generated CI runs these same primary-entrypoint tests.
The framework CI also generates and boots every starter without external services.

Start with `python -m {package}`; the shared CLI is `python -m {package}.cli ask hello`.
"""
    if web:
        content["README.md"] += (
            "\nThe server binds loopback port 8000. Its REST API is `/api/v1/tasks`. "
        )
        if template is not StarterTemplate.REST_API:
            content["README.md"] += "Your editable frontend is `/app`, with source in `frontend/`. "
        content["README.md"] += (
            "Configure authenticated ingress before exposing it beyond loopback.\n"
        )
    if worker:
        content["README.md"] += (
            f'\nQueue a request: `python -m {package}.worker enqueue "hello"`.\n'
            f"Process one delivery: `python -m {package}.worker once`. "
            "The SQLite queue survives restart.\nDelivery is at least once; "
            "do not add non-idempotent effects without an inbox/outbox design.\n"
        )
    content["README.md"] += (
        "\nLocal databases live in `var/`. Composition and lifecycle belong to `application.py`.\n"
        "Templates install no remote providers or credentials. Generated source is yours to edit;\n"
        "this is a starting composition, not a deployed/operated production service "
        "or a new licence grant.\n"
    )
    for path, source in content.items():
        if path.endswith(".py"):
            compile(source, path, "exec")
    manifest = StarterManifest(
        str(root.absolute()),
        template,
        package,
        f"{package}.application:build_application",
        tuple(sorted((*content, "af-starter.json"))),
    )
    metadata = {
        "schema": "af.starter@1",
        **manifest.to_dict(),
        "framework_version": __version__,
        "sha256": {
            path: hashlib.sha256(source.encode()).hexdigest()
            for path, source in sorted(content.items())
        },
    }
    content["af-starter.json"] = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.mkdir()  # Exclusive claim: never overwrite or merge into an existing destination.
    except FileExistsError as exc:
        raise ValueError("starter destination already exists") from exc
    for relative, source in sorted(content.items()):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            handle.write(source)
    return manifest
