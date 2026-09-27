import json

from typer.testing import CliRunner

from af.interfaces.console import create_cli


def test_default_commands_use_initialized_machine_and_doctor_is_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "configured"
    monkeypatch.setenv("AF_MACHINE_ROOT", str(root))
    runner = CliRunner()
    initialized = runner.invoke(create_cli(), ["init", "--non-interactive", "--apply", "--yes"])
    assert initialized.exit_code == 0, initialized.output
    created = runner.invoke(create_cli(), ["tasks", "create", "hello"])
    assert created.exit_code == 0, created.output
    restarted = runner.invoke(create_cli(), ["tasks", "list"])
    assert restarted.exit_code == 0, restarted.output
    assert "Hello, world!" in restarted.output
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    doctor = runner.invoke(create_cli(), ["doctor", "--json"])
    assert doctor.exit_code == 0, doctor.output
    assert json.loads(doctor.output)["machine"]["state"] == "healthy"
    after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert after == before
