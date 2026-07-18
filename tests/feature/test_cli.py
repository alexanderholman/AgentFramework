from __future__ import annotations

import json

from typer.testing import CliRunner

from af.framework.types import PROMPT_MAX_LENGTH
from af.interfaces.cli import create_cli

runner = CliRunner()


def test_cli_hello_uses_agent(application) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(create_cli(application), ["hello"])

    assert result.exit_code == 0
    assert result.stdout == "Hello, world!\n"


def test_cli_ask_strawberry(application) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(create_cli(application), ["ask", "how many rs in strawberry"])

    assert result.exit_code == 0
    assert result.stdout == "3\n"


def test_cli_task_crud(application) -> None:  # type: ignore[no-untyped-def]
    cli = create_cli(application)
    created_result = runner.invoke(cli, ["tasks", "create", "how many rs in strawberry"])
    assert created_result.exit_code == 0
    created = json.loads(created_result.stdout)

    assert created["output"] == "3"

    listed_result = runner.invoke(cli, ["tasks", "list"])
    assert listed_result.exit_code == 0
    listed = json.loads(listed_result.stdout)
    assert [task["id"] for task in listed] == [created["id"]]

    shown_result = runner.invoke(cli, ["tasks", "show", created["id"]])
    assert shown_result.exit_code == 0
    assert json.loads(shown_result.stdout) == created

    updated_result = runner.invoke(
        cli,
        ["tasks", "update", created["id"], "how many bs in blueberry"],
    )
    assert updated_result.exit_code == 0
    updated = json.loads(updated_result.stdout)
    assert updated["id"] == created["id"]
    assert updated["prompt"] == "how many bs in blueberry"
    assert updated["output"] == "2"
    assert updated["verified"] is True

    shown_updated_result = runner.invoke(cli, ["tasks", "show", created["id"]])
    assert shown_updated_result.exit_code == 0
    assert json.loads(shown_updated_result.stdout) == updated

    delete_result = runner.invoke(cli, ["tasks", "delete", created["id"]])
    assert delete_result.exit_code == 0

    listed_after_delete = runner.invoke(cli, ["tasks", "list"])
    assert listed_after_delete.exit_code == 0
    assert json.loads(listed_after_delete.stdout) == []


def test_cli_reports_expected_errors_without_stdout(application) -> None:  # type: ignore[no-untyped-def]
    cli = create_cli(application)

    cases = [
        (["ask", "   "], "prompt must not be blank\n"),
        (
            ["ask", "a" * (PROMPT_MAX_LENGTH + 1)],
            f"prompt must be at most {PROMPT_MAX_LENGTH} characters\n",
        ),
        (["ask", "make tea"], "no route found for prompt: 'make tea'\n"),
        (["tasks", "show", "missing"], "task not found: missing\n"),
        (
            ["tasks", "update", "missing", "how many rs in strawberry"],
            "task not found: missing\n",
        ),
        (["tasks", "delete", "missing"], "task not found: missing\n"),
    ]

    for arguments, expected_error in cases:
        result = runner.invoke(cli, arguments)

        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr == expected_error

    assert application.tasks.list() == []
