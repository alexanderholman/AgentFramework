from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from types import ModuleType

import typer
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.domain.procedures import BaseProcedure
from af.framework.application import ApplicationState
from af.framework.container import Container, ProviderRegistrar, Scope, ServiceProvider
from af.framework.context import current_context
from af.framework.errors import InvalidPrompt, InvalidSelectionProposal
from af.framework.procedure_contributors import (
    InstalledProcedureContribution,
    bind_procedure_contributor,
)
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
    WorkTier,
)
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository
from af.interfaces import cli as cli_module
from af.interfaces.cli import (
    create_cli,
    current_application_from_context,
    current_container_from_context,
    current_interaction_from_context,
)
from af.interfaces.cli_commands import bind_cli_command_contributor
from af.interfaces.cli_interactions import PromptEvent, ScriptedCliInteraction

runner = CliRunner()


@dataclass(frozen=True, slots=True)
class _UppercaseRegistration:
    procedure: BaseProcedure
    classifier: object


class _UppercaseClassifier:
    def classify(self, request) -> Classification | None:  # type: ignore[no-untyped-def]
        text = request.prompt
        prefix = "uppercase "
        if not isinstance(text, str) or not text.casefold().startswith(prefix):
            return None
        value = text[len(prefix) :].strip()
        if not value:
            return None
        return Classification(
            intent="text.uppercase",
            arguments={"text": value},
            confidence=1.0,
            classifier=type(self).__name__,
        )


class _UppercaseProcedure(BaseProcedure):
    id = "uppercase"
    version = "2.1.0"
    tier = WorkTier.PROCEDURAL
    pure = True
    replay_safe = True
    metrics = RouteMetrics(cost=2, latency=1, intelligence=1)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=version,
        intent="text.uppercase",
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
        return classification.intent == "text.uppercase"

    def execute(self, classification: Classification) -> ProcedureResult:
        text = classification.arguments["text"]
        return ProcedureResult(output=text.upper(), evidence={"text": text})

    def validate(self, classification: Classification) -> None:
        if classification.intent != "text.uppercase":
            raise InvalidSelectionProposal("unsupported intent for uppercase")
        if set(classification.arguments) != {"text"}:
            raise InvalidSelectionProposal("invalid arguments for uppercase")
        if not classification.arguments["text"]:
            raise InvalidSelectionProposal("invalid arguments for uppercase")


class _UppercaseProvider(ServiceProvider):
    provider_id = "test.procedure.uppercase"

    def register(self, registrar: ProviderRegistrar) -> None:
        contributor = InstalledProcedureContribution(
            classifier=_UppercaseClassifier(),
            procedure=_UppercaseProcedure(),
            verifier=_UppercaseVerifier(),
        )
        registrar.bind(
            _UppercaseRegistration,
            lambda _: _UppercaseRegistration(
                procedure=_UppercaseProcedure(),
                classifier=_UppercaseClassifier(),
            ),
            scope=Scope.APPLICATION,
        )
        bind_procedure_contributor(registrar, contributor)


class _UppercaseVerifier:
    def verify(
        self,
        request,
        classification: Classification,
        procedure,
        result: ProcedureResult,
    ) -> VerificationResult | None:  # type: ignore[no-untyped-def]
        del request, procedure
        if classification.intent != "text.uppercase":
            return None
        expected = classification.arguments["text"].upper()
        passed = result.output == expected
        return VerificationResult(
            passed,
            "independent uppercase matched" if passed else "independent uppercase mismatch",
        )


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


def test_cli_routes_lists_installed_deterministic_procedures(application) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(create_cli(application), ["routes"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    assert payload == [
        {
            "procedure_id": "catalogue-execute",
            "version": "1.0.0",
            "digest": payload[0]["digest"],
            "intent": "catalogue.execute",
            "arguments": [
                {
                    "name": "request",
                    "value_type": "text",
                    "min_length": 1,
                    "max_length": 10000,
                    "allow_blank": False,
                }
            ],
            "effect": "read_only",
            "risk": "none",
            "requires_confirmation": False,
            "tier": "procedural",
            "pure": True,
            "replay_safe": True,
            "has_selection_grounding": False,
            "metrics": {"cost": 1, "latency": 1, "intelligence": 0},
        },
        {
            "procedure_id": "character-count",
            "version": "1.0.0",
            "digest": payload[1]["digest"],
            "intent": "text.character_count",
            "arguments": [
                {
                    "name": "text",
                    "value_type": "text",
                    "min_length": 1,
                    "max_length": 10000,
                    "allow_blank": True,
                },
                {
                    "name": "character",
                    "value_type": "text",
                    "min_length": 1,
                    "max_length": 1,
                    "allow_blank": True,
                },
            ],
            "effect": "read_only",
            "risk": "none",
            "requires_confirmation": False,
            "tier": "procedural",
            "pure": True,
            "replay_safe": True,
            "has_selection_grounding": True,
            "metrics": {"cost": 1, "latency": 1, "intelligence": 1},
        },
        {
            "procedure_id": "current-time",
            "version": "1.1.0",
            "digest": payload[2]["digest"],
            "intent": "system.current_time",
            "arguments": [
                {
                    "name": "timezone",
                    "value_type": "text",
                    "min_length": 1,
                    "max_length": 64,
                    "allow_blank": False,
                },
            ],
            "effect": "read_only",
            "risk": "none",
            "requires_confirmation": False,
            "tier": "static",
            "pure": False,
            "replay_safe": False,
            "has_selection_grounding": False,
            "metrics": {"cost": 0, "latency": 0, "intelligence": 0},
        },
        {
            "procedure_id": "hello",
            "version": "1.0.0",
            "digest": payload[3]["digest"],
            "intent": "system.hello",
            "arguments": [],
            "effect": "read_only",
            "risk": "none",
            "requires_confirmation": False,
            "tier": "static",
            "pure": True,
            "replay_safe": True,
            "has_selection_grounding": False,
            "metrics": {"cost": 0, "latency": 0, "intelligence": 0},
        },
        {
            "procedure_id": "science-calculate",
            "version": "1.0.0",
            "digest": payload[4]["digest"],
            "intent": "science.calculate",
            "arguments": [
                {
                    "name": "expression",
                    "value_type": "text",
                    "min_length": 1,
                    "max_length": 1024,
                    "allow_blank": False,
                },
            ],
            "effect": "read_only",
            "risk": "none",
            "requires_confirmation": False,
            "tier": "procedural",
            "pure": True,
            "replay_safe": True,
            "has_selection_grounding": False,
            "metrics": {"cost": 1, "latency": 1, "intelligence": 0},
        },
    ]
    assert all(item["digest"].startswith("sha256:") for item in payload)
    assert all(len(item["digest"]) == len("sha256:") + 64 for item in payload)


def test_cli_routes_include_provider_contributed_procedures_with_stable_identity() -> None:
    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(_UppercaseProvider(),),
    )
    try:
        result = runner.invoke(create_cli(application), ["routes"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        uppercase = next(item for item in payload if item["procedure_id"] == "uppercase")
        assert uppercase == {
            "procedure_id": "uppercase",
            "version": "2.1.0",
            "digest": uppercase["digest"],
            "intent": "text.uppercase",
            "arguments": [
                {
                    "name": "text",
                    "value_type": "text",
                    "min_length": 1,
                    "max_length": 10000,
                    "allow_blank": False,
                }
            ],
            "effect": "read_only",
            "risk": "none",
            "requires_confirmation": False,
            "tier": "procedural",
            "pure": True,
            "replay_safe": True,
            "has_selection_grounding": False,
            "metrics": {"cost": 2, "latency": 1, "intelligence": 1},
        }
        assert uppercase["digest"].startswith("sha256:")
        assert len(uppercase["digest"]) == len("sha256:") + 64
    finally:
        application.shutdown()


def test_provider_contributed_procedure_runs_through_cli_and_rest() -> None:
    from af.interfaces.http import create_http_app

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(_UppercaseProvider(),),
    )
    try:
        cli_result = runner.invoke(create_cli(application), ["ask", "uppercase hello world"])

        assert cli_result.exit_code == 0
        assert cli_result.stdout == "HELLO WORLD\n"

        with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
            response = client.post("/api/v1/tasks", json={"prompt": "uppercase hello world"})

        assert response.status_code == 201
        payload = response.json()
        assert payload["output"] == "HELLO WORLD"
        assert payload["intent"] == "text.uppercase"
        assert payload["route"] == "uppercase"
        assert payload["verified"] is True
    finally:
        application.shutdown()


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


def test_cli_discovers_provider_contributed_commands_without_central_registration() -> None:
    class HelloContributor:
        def __init__(self) -> None:
            self.registrations = 0

        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            self.registrations += 1

            @cli.command("provider-hello")
            def provider_hello(context: typer.Context) -> None:
                typer.echo(current_application_from_context(context).tasks.execute("hello").output)

    class SettingsContributor:
        def __init__(self) -> None:
            self.registrations = 0

        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            self.registrations += 1

            @cli.command("provider-settings")
            def provider_settings(context: typer.Context) -> None:
                typer.echo(current_application_from_context(context).settings.database_url)

    class HelloProvider(ServiceProvider):
        provider_id = "test.cli-provider.hello"

        def __init__(self, contributor: HelloContributor) -> None:
            self._contributor = contributor

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, self._contributor)

    class SettingsProvider(ServiceProvider):
        provider_id = "test.cli-provider.settings"

        def __init__(self, contributor: SettingsContributor) -> None:
            self._contributor = contributor

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, self._contributor)

    hello = HelloContributor()
    settings = SettingsContributor()
    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(HelloProvider(hello), SettingsProvider(settings)),
    )
    try:
        cli = create_cli(application)

        help_result = runner.invoke(cli, ["--help"])
        hello_result = runner.invoke(cli, ["provider-hello"])
        settings_result = runner.invoke(cli, ["provider-settings"])

        assert help_result.exit_code == 0
        assert "provider-hello" in help_result.stdout
        assert "provider-settings" in help_result.stdout
        assert hello_result.exit_code == 0
        assert hello_result.stdout == "Hello, world!\n"
        assert settings_result.exit_code == 0
        assert settings_result.stdout == "sqlite+pysqlite:///:memory:\n"
        assert hello.registrations == 1
        assert settings.registrations == 1
    finally:
        application.shutdown()


def test_installed_cli_loads_provider_commands_from_explicit_application_factory(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    class HelloContributor:
        def __init__(self) -> None:
            self.registrations = 0

        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            self.registrations += 1

            @cli.command("provider-hello")
            def provider_hello(context: typer.Context) -> None:
                typer.echo(current_application_from_context(context).tasks.execute("hello").output)

    class HelloProvider(ServiceProvider):
        provider_id = "test.cli-provider.installed"

        def __init__(self, contributor: HelloContributor) -> None:
            self._contributor = contributor

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, self._contributor)

    module = ModuleType("test_cli_provider_operator_profile")
    contributor = HelloContributor()

    def build_application():  # type: ignore[no-untyped-def]
        return create_application(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
            providers=(HelloProvider(contributor),),
        )

    module.build_application = build_application  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    cli = create_cli()

    loaded = runner.invoke(
        cli,
        [
            "--application-factory",
            "test_cli_provider_operator_profile:build_application",
            "provider-hello",
        ],
    )
    unloaded = runner.invoke(cli, ["provider-hello"])

    assert loaded.exit_code == 0
    assert loaded.stdout == "Hello, world!\n"
    assert unloaded.exit_code == 2
    assert contributor.registrations == 1


def test_provider_commands_run_inside_a_fresh_cli_invocation_scope() -> None:
    class InvocationSnapshot:
        def __init__(
            self,
            *,
            sequence: int,
            tenant_id: str | None,
            requested_scope: str,
            effective_scope: str,
            invocation_kind: str,
        ) -> None:
            self.sequence = sequence
            self.tenant_id = tenant_id
            self.requested_scope = requested_scope
            self.effective_scope = effective_scope
            self.invocation_kind = invocation_kind

    class ScopeContributor:
        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            @cli.command("provider-scope")
            def provider_scope(context: typer.Context) -> None:
                application = current_application_from_context(context)
                container = current_container_from_context(context)
                first = container.resolve(InvocationSnapshot)
                second = container.resolve(InvocationSnapshot)
                typer.echo(
                    json.dumps(
                        {
                            "same_application_container": container is application.container,
                            "same_instance_within_invocation": first is second,
                            "sequence": first.sequence,
                            "tenant_id": first.tenant_id,
                            "requested_scope": first.requested_scope,
                            "effective_scope": first.effective_scope,
                            "invocation_kind": first.invocation_kind,
                        }
                    )
                )

    class ScopeProvider(ServiceProvider):
        provider_id = "test.cli-provider.invocation-scope"

        def __init__(self, contributor: ScopeContributor) -> None:
            self._contributor = contributor
            self._sequence = 0

        def register(self, registrar: ProviderRegistrar) -> None:
            registrar.bind(
                InvocationSnapshot,
                self._create_snapshot,
                scope=Scope.INVOCATION,
            )
            bind_cli_command_contributor(registrar, self._contributor)

        def _create_snapshot(self, current: Container) -> InvocationSnapshot:
            del current
            context = current_context()
            self._sequence += 1
            return InvocationSnapshot(
                sequence=self._sequence,
                tenant_id=context.tenant_id,
                requested_scope=context.requested_scope.value,
                effective_scope=context.effective_scope.value,
                invocation_kind="interaction",
            )

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(ScopeProvider(ScopeContributor()),),
    )
    try:
        cli = create_cli(application)

        first = runner.invoke(cli, ["provider-scope"])
        second = runner.invoke(cli, ["provider-scope"])

        assert first.exit_code == 0
        assert second.exit_code == 0
        assert json.loads(first.stdout) == {
            "same_application_container": False,
            "same_instance_within_invocation": True,
            "sequence": 1,
            "tenant_id": None,
            "requested_scope": "deterministic",
            "effective_scope": "deterministic",
            "invocation_kind": "interaction",
        }
        assert json.loads(second.stdout) == {
            "same_application_container": False,
            "same_instance_within_invocation": True,
            "sequence": 2,
            "tenant_id": None,
            "requested_scope": "deterministic",
            "effective_scope": "deterministic",
            "invocation_kind": "interaction",
        }
    finally:
        application.shutdown()


def test_provider_commands_can_use_scripted_cli_interactions() -> None:
    class PromptContributor:
        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            @cli.command("provider-prompt")
            def provider_prompt(context: typer.Context) -> None:
                interaction = current_interaction_from_context(context)
                typer.echo(
                    json.dumps(
                        {
                            "name": interaction.text(message="Name"),
                            "confirmed": interaction.confirm(message="Continue?", default=True),
                        }
                    )
                )

    class PromptProvider(ServiceProvider):
        provider_id = "test.cli-provider.prompt"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, PromptContributor())

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(PromptProvider(),),
    )
    interactions: list[ScriptedCliInteraction] = []

    def build_interaction() -> ScriptedCliInteraction:
        interaction = ScriptedCliInteraction(
            text_responses=("Ralph",),
            confirm_responses=(True,),
        )
        interactions.append(interaction)
        return interaction

    try:
        result = runner.invoke(
            create_cli(application, interaction_factory=build_interaction),
            ["provider-prompt"],
        )

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"name": "Ralph", "confirmed": True}
        assert tuple(interactions[0].events) == (
            PromptEvent(kind="text", message="Name"),
            PromptEvent(kind="confirm", message="Continue?"),
        )
    finally:
        application.shutdown()


def test_provider_commands_refuse_interaction_without_a_scripted_adapter() -> None:
    class PromptContributor:
        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            @cli.command("provider-prompt")
            def provider_prompt(context: typer.Context) -> None:
                interaction = current_interaction_from_context(context)
                typer.echo(interaction.text(message="Name"))

    class PromptProvider(ServiceProvider):
        provider_id = "test.cli-provider.prompt-refusal"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, PromptContributor())

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(PromptProvider(),),
    )
    try:
        result = runner.invoke(create_cli(application), ["provider-prompt"])

        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr == "interactive input is unavailable for this CLI invocation\n"
    finally:
        application.shutdown()


def test_provider_command_keyboard_interrupt_returns_cancelled_exit_and_restores_interaction() -> (
    None
):
    class InterruptingInteraction(ScriptedCliInteraction):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def text(self, *, message: str) -> str:
            self.events.append(PromptEvent(kind="text", message=message))
            raise KeyboardInterrupt

        def close(self) -> None:
            self.closed = True

    class PromptContributor:
        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            @cli.command("provider-prompt")
            def provider_prompt(context: typer.Context) -> None:
                interaction = current_interaction_from_context(context)
                typer.echo(interaction.text(message="Name"))

    class PromptProvider(ServiceProvider):
        provider_id = "test.cli-provider.prompt-cancel"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, PromptContributor())

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(PromptProvider(),),
    )
    interactions: list[InterruptingInteraction] = []

    def build_interaction() -> InterruptingInteraction:
        interaction = InterruptingInteraction()
        interactions.append(interaction)
        return interaction

    try:
        result = runner.invoke(
            create_cli(application, interaction_factory=build_interaction),
            ["provider-prompt"],
        )

        assert result.exit_code == 130
        assert result.stdout == ""
        assert result.stderr == "operation was cancelled\n"
        assert tuple(interactions[0].events) == (PromptEvent(kind="text", message="Name"),)
        assert interactions[0].closed is True
    finally:
        application.shutdown()


def test_provider_command_keyboard_interrupt_is_not_suppressed_by_close_failure() -> None:
    class InterruptingInteraction(ScriptedCliInteraction):
        def text(self, *, message: str) -> str:
            self.events.append(PromptEvent(kind="text", message=message))
            raise KeyboardInterrupt

        def close(self) -> None:
            raise RuntimeError("restore failed")

    class PromptContributor:
        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            @cli.command("provider-prompt")
            def provider_prompt(context: typer.Context) -> None:
                interaction = current_interaction_from_context(context)
                typer.echo(interaction.text(message="Name"))

    class PromptProvider(ServiceProvider):
        provider_id = "test.cli-provider.prompt-cancel-close-failure"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, PromptContributor())

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(PromptProvider(),),
    )

    try:
        result = runner.invoke(
            create_cli(application, interaction_factory=InterruptingInteraction),
            ["provider-prompt"],
        )

        assert result.exit_code == 130
        assert result.stdout == ""
        assert result.stderr == "operation was cancelled\n"
    finally:
        application.shutdown()


def test_provider_command_cancellation_clears_interaction_for_next_invocation() -> None:
    class InterruptingThenRecordingInteraction(ScriptedCliInteraction):
        created = 0

        def __init__(self) -> None:
            super().__init__(text_responses=("Ralph",))
            type(self).created += 1
            self.instance_number = type(self).created

        def text(self, *, message: str) -> str:
            self.events.append(PromptEvent(kind="text", message=message))
            if self.instance_number == 1:
                raise KeyboardInterrupt
            return super().text(message=message)

    class PromptContributor:
        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            @cli.command("provider-prompt")
            def provider_prompt(context: typer.Context) -> None:
                interaction = current_interaction_from_context(context)
                typer.echo(interaction.text(message="Name"))

    class PromptProvider(ServiceProvider):
        provider_id = "test.cli-provider.prompt-cancel-reset"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, PromptContributor())

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(PromptProvider(),),
    )
    InterruptingThenRecordingInteraction.created = 0
    cli = create_cli(application, interaction_factory=InterruptingThenRecordingInteraction)

    try:
        cancelled = runner.invoke(cli, ["provider-prompt"])
        succeeded = runner.invoke(cli, ["provider-prompt"])

        assert cancelled.exit_code == 130
        assert cancelled.stdout == ""
        assert cancelled.stderr == "operation was cancelled\n"
        assert succeeded.exit_code == 0
        assert succeeded.stdout == "Ralph\n"
        assert succeeded.stderr == ""
        assert InterruptingThenRecordingInteraction.created == 2
    finally:
        application.shutdown()


def test_cli_sanitizes_interaction_close_failures_and_still_shuts_down_owned_application(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    class ClosingInteraction(ScriptedCliInteraction):
        def text(self, *, message: str) -> str:
            self.events.append(PromptEvent(kind="text", message=message))
            return "Ralph"

        def close(self) -> None:
            raise RuntimeError("restore failed")

    class PromptContributor:
        def register(self, cli) -> None:  # type: ignore[no-untyped-def]
            @cli.command("provider-prompt")
            def provider_prompt(context: typer.Context) -> None:
                interaction = current_interaction_from_context(context)
                typer.echo(interaction.text(message="Name"))

    class PromptProvider(ServiceProvider):
        provider_id = "test.cli-provider.prompt-close-failure"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cli_command_contributor(registrar, PromptContributor())

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(PromptProvider(),),
    )
    monkeypatch.setattr(cli_module, "create_application", lambda: application)

    result = runner.invoke(
        create_cli(interaction_factory=ClosingInteraction),
        ["provider-prompt"],
    )

    assert result.exit_code == 2
    assert result.stdout == "Ralph\n"
    assert result.stderr == "transport operation failed\n"
    assert application.lifecycle.state is ApplicationState.SHUTDOWN


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
    observed_http_options: list[dict[str, object]] = []

    def run(app: object, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    def fake_http_app(application_value, **kwargs):  # type: ignore[no-untyped-def]
        observed_http_options.append({"application": application_value, **kwargs})
        return object()

    monkeypatch.setattr("uvicorn.run", run)
    monkeypatch.setattr("af.interfaces.http.create_http_app", fake_http_app)
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
            "--web-profile",
            "experimental",
            "--experimental-web-features",
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0][1:] == ("127.0.0.1", 9000)
    assert observed_http_options == [
        {
            "application": application,
            "bind_host": "127.0.0.1",
            "allowed_hosts": ("localhost",),
            "web_profile": "experimental",
            "enable_experimental_web_features": True,
        }
    ]


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
