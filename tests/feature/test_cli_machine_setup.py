import json

from typer.testing import CliRunner

from af.interfaces.cli import create_cli


def test_installed_init_plan_does_not_boot_application_or_create_files(tmp_path):
    def forbidden():
        raise AssertionError("plan must not initialize application services")

    cli = create_cli(application_factory=forbidden)
    root = tmp_path / "machine"
    result = CliRunner().invoke(
        cli, ["init", "--plan", "--state-dir", str(root), "--profile", "scheduler"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["capabilities"] == ["core", "database", "queue", "cache", "scheduler"]
    assert not root.exists()


def test_explicit_apply_status_disable_and_version_init_coexist(tmp_path):
    cli = create_cli()
    root = tmp_path / "machine"
    result = CliRunner().invoke(
        cli,
        [
            "init",
            "--non-interactive",
            "--apply",
            "--yes",
            "--state-dir",
            str(root),
            "--profile",
            "queue",
        ],
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout.splitlines()[-1])
    assert receipt["state"] == "completed" and receipt["verification"]["verified"]
    status = CliRunner().invoke(cli, ["status", "--state-dir", str(root)])
    assert status.exit_code == 0 and json.loads(status.stdout)["state"] == "healthy"
    disabled = CliRunner().invoke(cli, ["queue", "disable", "--yes", "--state-dir", str(root)])
    assert disabled.exit_code == 0 and (root / "af.sqlite3").exists()
    preview = CliRunner().invoke(cli, ["cache", "enable", "--plan", "--state-dir", str(root)])
    assert preview.exit_code == 0
    assert json.loads(preview.stdout)["capabilities"] == ["core", "database", "cache"]


def test_noninteractive_without_confirmation_cannot_apply(tmp_path):
    root = tmp_path / "machine"
    result = CliRunner().invoke(
        create_cli(), ["init", "--non-interactive", "--apply", "--state-dir", str(root)]
    )
    assert result.exit_code == 2 and not root.exists()


def test_exported_declaration_reproduces_noninteractively(tmp_path):
    cli = create_cli()
    runner = CliRunner()
    first, second = tmp_path / "first", tmp_path / "second"
    result = runner.invoke(
        cli,
        [
            "init",
            "--non-interactive",
            "--apply",
            "--yes",
            "--state-dir",
            str(first),
            "--profile",
            "cache",
        ],
    )
    assert result.exit_code == 0, result.output
    exported = runner.invoke(cli, ["machine", "export", "--state-dir", str(first)])
    declaration = tmp_path / "machine-export.json"
    declaration.write_text(exported.stdout)
    reproduced = runner.invoke(
        cli,
        [
            "init",
            "--config",
            str(declaration),
            "--non-interactive",
            "--apply",
            "--yes",
            "--state-dir",
            str(second),
        ],
    )
    assert reproduced.exit_code == 0, reproduced.output
    assert json.loads(reproduced.stdout.splitlines()[-1])["state"] == "completed"


def test_interactive_wizard_uses_same_plan_and_requires_confirmation(tmp_path):
    root = tmp_path / "machine"
    result = CliRunner().invoke(
        create_cli(), ["init", "--state-dir", str(root)], input="core\n\n\nn\n"
    )
    assert result.exit_code == 0, result.output
    assert "Apply this local setup plan?" in result.stdout and not root.exists()


def test_capability_enable_confirmation_status_and_failure_paths(tmp_path):
    root = tmp_path / "machine"
    cli, runner = create_cli(), CliRunner()
    denied = runner.invoke(cli, ["database", "enable", "--state-dir", str(root)], input="n\n")
    assert denied.exit_code == 0 and not root.exists()
    enabled = runner.invoke(cli, ["database", "enable", "--yes", "--state-dir", str(root)])
    assert enabled.exit_code == 0, enabled.output
    for command in (["database", "status"], ["machine", "status"]):
        result = runner.invoke(cli, [*command, "--state-dir", str(root)])
        assert result.exit_code == 0 and "healthy" in result.stdout
    denied_disable = runner.invoke(
        cli, ["database", "disable", "--state-dir", str(root)], input="n\n"
    )
    assert denied_disable.exit_code == 0
    invalid = runner.invoke(
        cli, ["database", "disable", "--yes", "--state-dir", str(tmp_path / "missing")]
    )
    assert invalid.exit_code == 2
    exported = runner.invoke(cli, ["machine", "export", "--state-dir", str(tmp_path / "missing")])
    assert exported.exit_code == 2


def test_invalid_plan_and_declaration_options_do_not_mutate(tmp_path):
    cli, runner = create_cli(), CliRunner()
    root = tmp_path / "machine"
    conflicting = runner.invoke(cli, ["init", "--plan", "--apply", "--state-dir", str(root)])
    assert conflicting.exit_code == 2
    declaration = tmp_path / "declaration.json"
    for content, args in [("[]", []), ("x" * 1_048_577, []), ("{}", ["--profile", "core"])]:
        declaration.write_text(content)
        result = runner.invoke(
            cli, ["init", "--plan", "--config", str(declaration), "--state-dir", str(root), *args]
        )
        assert result.exit_code == 2 and not root.exists()


def test_interactive_explicit_paths_and_failed_apply_exit_status(tmp_path, monkeypatch):
    from af.infrastructure.machine_setup import LocalMachineSetup

    cli, runner = create_cli(), CliRunner()
    root = tmp_path / "machine"
    monkeypatch.setattr(
        LocalMachineSetup, "_verify", lambda root: (_ for _ in ()).throw(ValueError("failure"))
    )
    result = runner.invoke(
        cli, ["init", "--profile", "core", "--state-dir", str(root)], input="y\n"
    )
    assert result.exit_code == 1 and json.loads(result.stdout.splitlines()[-1])["state"] == "failed"
    enabled = runner.invoke(cli, ["cache", "enable", "--yes", "--state-dir", str(root)])
    assert enabled.exit_code == 1
