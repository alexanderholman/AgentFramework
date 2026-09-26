"""Each application-owned starter boots its public entry point in a fresh process."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from af.infrastructure.application_scaffold import StarterTemplate, scaffold_application
from af.interfaces.starters import register_starter_commands


@pytest.mark.parametrize("template", list(StarterTemplate))
def test_generated_starter_primary_tests_boot_offline(tmp_path, template):
    destination = tmp_path / template.value
    manifest = scaffold_application(root=destination, package="starter_app", template=template)
    assert manifest.template == template
    assert manifest.application_factory == "starter_app.application:build_application"
    payload = json.loads((destination / "af-starter.json").read_text())
    for relative, expected in payload["sha256"].items():
        assert hashlib.sha256((destination / relative).read_bytes()).hexdigest() == expected
    source = Path(__import__("af").__file__).parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(destination), str(source)))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=destination,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout
    assert (destination / "README.md").is_file()
    assert (destination / ".github/workflows/ci.yml").is_file()
    assert "af-agent-framework==" in (destination / "pyproject.toml").read_text()


def test_scaffold_refuses_overwrite_symlinks_and_invalid_package_before_writing(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep")
    with pytest.raises(ValueError):
        scaffold_application(root=occupied, package="app", template=StarterTemplate.REST_API)
    assert list(occupied.iterdir()) == [occupied / "keep.txt"]
    link = tmp_path / "link"
    link.symlink_to(occupied, target_is_directory=True)
    with pytest.raises(ValueError):
        scaffold_application(root=link, package="app", template=StarterTemplate.REST_API)
    for package in ("../escape", "bad-name", "class", "af", "x.y", "json", "fastapi"):
        with pytest.raises(ValueError):
            scaffold_application(
                root=tmp_path / "invalid", package=package, template=StarterTemplate.REST_API
            )
        assert not (tmp_path / "invalid").exists()


def test_new_command_exposes_typed_template_choices_and_manifest(tmp_path):
    cli = typer.Typer()

    @cli.callback()
    def root():
        pass

    register_starter_commands(cli)
    result = CliRunner().invoke(
        cli, ["new", str(tmp_path / "project"), "--template", "basic-web", "--package", "app"]
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["template"] == "basic-web"
    duplicate = CliRunner().invoke(
        cli, ["new", str(tmp_path / "project"), "--template", "basic-web"]
    )
    assert duplicate.exit_code == 2
