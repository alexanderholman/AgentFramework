"""Installed terminal actions use real task services and opt-in scripted input."""

import json
from pathlib import Path

from typer.testing import CliRunner

from af.framework.tui import KeyEvent, MouseEvent, TextEvent
from af.infrastructure.tui import ScriptedTuiDriver
from af.interfaces.console import create_cli
from af.interfaces.tui import create_tui_kernel, parse_script


def test_headless_task_application_creates_reads_updates_and_confirms_deletion(application):
    driver = ScriptedTuiDriver(
        (
            TextEvent("hello"),
            KeyEvent("enter"),
            KeyEvent("backspace"),
            KeyEvent("backspace"),
            KeyEvent("backspace"),
            KeyEvent("backspace"),
            KeyEvent("backspace"),
            TextEvent("math add a=1 b=2"),
            KeyEvent("enter"),
            MouseEvent("delete"),
            KeyEvent("enter"),
            KeyEvent("escape"),
            MouseEvent("quit"),
            KeyEvent("enter"),
        )
    )
    result = create_tui_kernel(application).run("tasks", driver)
    assert result.exit_code == 0 and driver.closed
    task = application.tasks.list()[0]
    assert json.loads(task.output)["quantity"]["exact"] == "3"
    # Cancellation leaves the saved record intact; exact task identity is selected anew.
    second = ScriptedTuiDriver(
        (
            MouseEvent("task." + task.id),
            KeyEvent("enter"),
            MouseEvent("delete"),
            KeyEvent("enter"),
            MouseEvent("confirm-delete"),
            KeyEvent("enter"),
            MouseEvent("quit"),
            KeyEvent("enter"),
        )
    )
    result = create_tui_kernel(application).run("tasks", second)
    assert result.exit_code == 0
    assert application.tasks.list() == []


def test_invalid_work_stays_in_accessible_terminal_view_without_task_creation(application):
    driver = ScriptedTuiDriver(
        (TextEvent("unsupported command"), KeyEvent("enter"), KeyEvent("escape"))
    )
    result = create_tui_kernel(application).run("tasks", driver)
    assert result.exit_code == 0
    assert any("denied or unsupported" in frame.status for frame in driver.frames)
    assert application.tasks.list() == []


def test_tui_cli_script_plain_and_noninteractive_modes(application, tmp_path: Path):
    runner, cli = CliRunner(), create_cli(application)
    assert runner.invoke(cli, ["tui", "--help"]).exit_code == 0
    listed = runner.invoke(cli, ["tui", "list"])
    assert listed.exit_code == 0 and json.loads(listed.output)[0]["id"] == "tasks"
    denied = runner.invoke(cli, ["tui", "run"])
    assert denied.exit_code == 2
    plain = runner.invoke(cli, ["tui", "run", "--plain"])
    assert plain.exit_code == 0 and "Tasks" in plain.output
    script = tmp_path / "turns.json"
    script.write_text(
        json.dumps(
            {
                "events": [
                    {"type": "text", "text": "hello"},
                    {"type": "key", "key": "enter"},
                    {"type": "mouse", "widget_id": "quit"},
                    {"type": "key", "key": "enter"},
                ]
            }
        )
    )
    executed = runner.invoke(
        cli, ["tui", "run", "--script", str(script), "--language", "fr", "--territory", "CA"]
    )
    assert executed.exit_code == 0, executed.output
    assert json.loads(executed.output)["reason"] == "completed"
    assert len(application.tasks.list()) == 1
    script.write_text('{"events":[{"type":"command","shell":"rm -rf /"}]}')
    bad = runner.invoke(cli, ["tui", "run", "--script", str(script)])
    assert bad.exit_code == 2 and json.loads(bad.output) == {"error": "invalid_tui_request"}
    assert len(application.tasks.list()) == 1


def test_tui_script_parser_rejects_unknown_fields_types_and_bounds():
    import pytest

    for payload in (
        [],
        {},
        {"events": {}},
        {"events": [1]},
        {"events": [{"type": "key", "key": "enter", "shell": "x"}]},
        {"events": [{"type": "text", "text": 3}]},
        {"events": [{"type": "resize", "columns": 0, "rows": 24}]},
    ):
        with pytest.raises((ValueError, TypeError)):
            parse_script(payload)
    assert (
        parse_script({"events": [{"type": "resize", "columns": 80, "rows": 24}]})[0].columns == 80
    )
