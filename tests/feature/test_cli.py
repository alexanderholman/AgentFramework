from __future__ import annotations

import json

from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.framework.application import ApplicationState
from af.framework.container import Container, ProviderRegistrar, ServiceProvider
from af.framework.errors import InvalidPrompt
from af.framework.types import PROMPT_MAX_LENGTH
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository
from af.interfaces import cli as cli_module
from af.interfaces.cli import create_cli

runner = CliRunner()


def _search_application():  # type: ignore[no-untyped-def]
    return create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )


def test_cli_hello_uses_agent(application) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(create_cli(application), ["hello"])

    assert result.exit_code == 0
    assert result.stdout == "Hello, world!\n"


def test_cli_ask_strawberry(application) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(create_cli(application), ["ask", "how many rs in strawberry"])

    assert result.exit_code == 0
    assert result.stdout == "3\n"


def test_cli_route_agent_returns_structured_decision(application) -> None:  # type: ignore[no-untyped-def]
    cli = create_cli(application)

    routed = runner.invoke(cli, ["route-agent", "Implement the endpoint"])
    ambiguous = runner.invoke(cli, ["route-agent", "Design and implement the endpoint"])

    assert routed.exit_code == 0
    assert json.loads(routed.stdout) == {
        "prompt": "Implement the endpoint",
        "status": "routed",
        "agent": "builder",
        "reason": "specialist_match",
        "candidates": ["builder"],
    }
    assert ambiguous.exit_code == 0
    assert json.loads(ambiguous.stdout) == {
        "prompt": "Design and implement the endpoint",
        "status": "handoff",
        "agent": None,
        "reason": "ambiguous",
        "candidates": ["architect", "builder"],
    }


def test_cli_ideon_command_uses_the_composed_reference_service(application) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(
        create_cli(application),
        ["ideon", "interpret", "sat on the bank", "--actor", "alice", "--domain", "river"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["decision"]["decision"] == "answer"
    assert payload["selected_senses"][-1] == "bank.river-edge"


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
    assert json.loads(delete_result.stdout) == {"id": created["id"], "deleted": True}

    listed_after_delete = runner.invoke(cli, ["tasks", "list"])
    assert listed_after_delete.exit_code == 0
    assert json.loads(listed_after_delete.stdout) == []


def test_cli_search_indexes_and_queries_task_and_trace_history() -> None:
    application = _search_application()
    try:
        application.tasks.create("how many qs in qqqq")
        application.tasks.execute("hello")

        cli = create_cli(application)
        indexed_result = runner.invoke(cli, ["search", "index"])

        assert indexed_result.exit_code == 0
        assert json.loads(indexed_result.stdout) == {
            "indexed": 3,
            "sources": [
                {"source": "tasks", "indexed": 1},
                {"source": "traces", "indexed": 2},
            ],
        }

        queried_result = runner.invoke(cli, ["search", "query", "qqqq"])

        assert queried_result.exit_code == 0
        payload = json.loads(queried_result.stdout)
        assert payload["total"] == 2
        assert {item["source_id"] for item in payload["items"]} == {
            "af_run_traces",
            "af_tasks",
        }
        assert {item["tenant_id"] for item in payload["items"]} == {"af-local"}
        assert all(
            item["authoritative_locator"].startswith("af://relational/")
            for item in payload["items"]
        )
    finally:
        application.shutdown()


def test_cli_search_index_limit_bounds_total_backfill() -> None:
    application = _search_application()
    try:
        application.tasks.create("how many qs in qqqq")
        application.tasks.execute("hello")

        result = runner.invoke(create_cli(application), ["search", "index", "--limit", "2"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "indexed": 2,
            "sources": [
                {"source": "tasks", "indexed": 1},
                {"source": "traces", "indexed": 1},
            ],
        }
    finally:
        application.shutdown()


def test_cli_search_index_limit_includes_trace_history_when_tasks_exceed_limit() -> None:
    application = _search_application()
    try:
        application.tasks.create("hello")
        application.tasks.create("how many qs in qqqq")
        application.tasks.create("how many rs in strawberry")
        application.tasks.execute("hello")

        result = runner.invoke(create_cli(application), ["search", "index", "--limit", "4"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "indexed": 4,
            "sources": [
                {"source": "tasks", "indexed": 2},
                {"source": "traces", "indexed": 2},
            ],
        }
    finally:
        application.shutdown()


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
        (["search", "query", "   "], "prompt must not be blank\n"),
    ]

    for arguments, expected_error in cases:
        result = runner.invoke(cli, arguments)

        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr == expected_error

    assert application.tasks.list() == []


def test_cli_lazily_creates_and_closes_the_application(
    application,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    shutdown_calls = 0

    class OwnedApplication:
        tasks = application.tasks

        def shutdown(self) -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1

    monkeypatch.setattr(cli_module, "create_application", OwnedApplication)

    result = runner.invoke(create_cli(), ["hello"])

    assert result.exit_code == 0
    assert result.stdout == "Hello, world!\n"
    assert shutdown_calls == 1


def test_cli_closes_owned_application_after_command_error(
    application,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli_module, "create_application", lambda: application)

    result = runner.invoke(create_cli(), ["ask", "   "])

    assert result.exit_code == 2
    assert application.lifecycle.state is ApplicationState.SHUTDOWN


def test_cli_sync_host_bridges_owned_async_shutdown(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    events: list[str] = []

    class Provider(ServiceProvider):
        provider_id = "test.cli-async-shutdown"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(Provider(),),
    )
    monkeypatch.setattr(cli_module, "create_application", lambda: application)

    result = runner.invoke(create_cli(), ["hello"])

    assert result.exit_code == 0
    assert application.lifecycle.state is ApplicationState.SHUTDOWN
    assert events == ["shutdown"]


def test_cli_does_not_close_injected_application(application) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(create_cli(application), ["hello"])

    assert result.exit_code == 0
    assert application.lifecycle.state is ApplicationState.SERVING


def test_cli_serve_passes_explicit_host_options(
    application,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[object, str, int]] = []

    def run(app: object, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr("uvicorn.run", run)
    result = runner.invoke(
        create_cli(application),
        [
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "9000",
            "--allowed-host",
            "localhost",
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][1:] == ("127.0.0.1", 9000)


def test_cli_command_specific_error_paths_are_sanitized(application) -> None:  # type: ignore[no-untyped-def]
    class BrokenTasks:
        def execute(self, _: str) -> None:
            raise InvalidPrompt("private hello failure")

    class BrokenRoutes:
        def route(self, _: str) -> None:
            raise InvalidPrompt("private route failure")

    class BrokenApplication:
        tasks = BrokenTasks()
        agent_routes = BrokenRoutes()

    hello = runner.invoke(create_cli(BrokenApplication()), ["hello"])  # type: ignore[arg-type]
    route = runner.invoke(
        create_cli(BrokenApplication()),  # type: ignore[arg-type]
        ["route-agent", "Implement this"],
    )
    create = runner.invoke(create_cli(application), ["tasks", "create", "   "])

    assert hello.exit_code == 2
    assert route.exit_code == 2
    assert create.exit_code == 2
