"""A local model proposes; AF still validates, grounds, executes and verifies."""

import json
from dataclasses import replace

import httpx
import pytest

from af.bootstrap import create_application
from af.domain.procedures import CharacterCountProcedure
from af.framework.errors import UnsupportedWork
from af.framework.routing import ProcedureRegistry
from af.framework.types import ProcedureDescriptor, SelectionHandoffReason, WorkRequest
from af.infrastructure.model_selectors import LocalModelSelector
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository


def catalogue() -> tuple[ProcedureDescriptor, ...]:
    registry = ProcedureRegistry()
    registry.register(CharacterCountProcedure())
    return registry.catalogue()


def proposal() -> dict[str, object]:
    item = catalogue()[0]
    return {
        "proposal": {
            "procedure_id": item.procedure_id,
            "version": item.version,
            "digest": item.digest,
            "intent": item.intent,
            "arguments": {"text": "strawberry", "character": "r"},
            "confidence": 0.95,
        },
        "reason": "proposed",
    }


def envelope(payload: object, protocol: str = "openai") -> dict[str, object]:
    message = {"role": "assistant", "content": json.dumps(payload)}
    if protocol == "ollama":
        return {"model": "qwen3:8b", "message": message, "done": True, "done_reason": "stop"}
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


@pytest.mark.parametrize(
    "protocol,path", [("openai", "/v1/chat/completions"), ("ollama", "/api/chat")]
)
def test_local_adapter_requests_bounded_schema_and_returns_only_proposal(
    protocol: str, path: str
) -> None:
    calls: list[httpx.Request] = []
    selector = LocalModelSelector(
        model="qwen3:8b",
        protocol=protocol,
        transport=httpx.MockTransport(
            lambda req: (
                calls.append(req) or httpx.Response(200, json=envelope(proposal(), protocol))
            )
        ),
    )
    result = selector.select("please count the letter r in strawberry", catalogue())
    assert result.proposal is not None and result.proposal.arguments == {
        "text": "strawberry",
        "character": "r",
    }
    assert calls[0].url.path == path
    body = json.loads(calls[0].content)
    assert body["stream"] is False
    assert "tools" not in body
    if protocol == "openai":
        assert body["response_format"]["type"] == "json_schema"
        assert body["max_tokens"] == 2048
    else:
        assert body["format"]["type"] == "object"
        assert body["options"]["num_predict"] == 2048


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com",
        "http://127.0.0.1@evil.com",
        "http://192.168.0.1",
        "http://127.0.0.1/?target=remote",
        "ftp://127.0.0.1",
        "http://127.0.0.1/foo",
        "http://127.0.0.1/#fragment",
    ],
)
def test_local_adapter_rejects_egress_or_ambiguous_destinations(endpoint: str) -> None:
    with pytest.raises(ValueError):
        LocalModelSelector(model="qwen3:8b", endpoint=endpoint)


@pytest.mark.parametrize("model", ["", "qwen:cloud", "qwen-cloud", "x\nkey", "x" * 129])
def test_local_adapter_rejects_unbounded_or_cloud_models(model: str) -> None:
    with pytest.raises(ValueError):
        LocalModelSelector(model=model, protocol="ollama")


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "999.0.0"),
        ("digest", "sha256:" + "0" * 64),
        ("procedure_id", "shell"),
        ("intent", "exec.shell"),
        ("confidence", True),
        ("confidence", 1.1),
        ("arguments", {"text": "x", "character": "rr"}),
        ("arguments", {"text": "x", "character": "r", "shell": "bad"}),
    ],
)
def test_malicious_or_stale_proposals_never_become_executable(field: str, value: object) -> None:
    payload = proposal()
    assert isinstance(payload["proposal"], dict)
    payload["proposal"][field] = value
    selector = LocalModelSelector(
        model="qwen3:8b",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=envelope(payload))),
    )
    assert selector.select("text", catalogue()).proposal is None


def test_local_adapter_rejects_extra_fields_duplicate_json_and_truncated_output() -> None:
    payload = proposal()
    payload["answer"] = "arbitrary generated answer"
    samples = [
        envelope(payload),
        {
            "choices": [
                {
                    "message": {
                        "content": '{"reason":"unsupported","reason":"proposed","proposal":null}'
                    },
                    "finish_reason": "stop",
                }
            ]
        },
        {"choices": [{"message": {"content": json.dumps(proposal())}, "finish_reason": "length"}]},
    ]
    for sample in samples:
        selector = LocalModelSelector(
            model="qwen3:8b",
            transport=httpx.MockTransport(
                lambda req, sample=sample: httpx.Response(200, json=sample)
            ),
        )
        assert selector.select("text", catalogue()).proposal is None


def test_local_timeout_unavailable_ambiguous_and_cancelled_have_zero_execution() -> None:
    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive provider information")

    selector = LocalModelSelector(model="qwen3:8b", transport=httpx.MockTransport(timeout))
    assert selector.select("text", catalogue()).proposal is None
    assert selector.select("text", catalogue(), cancelled=lambda: True).proposal is None
    ambiguous = LocalModelSelector(
        model="qwen3:8b",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json=envelope({"proposal": None, "reason": "ambiguous"})
            )
        ),
    )
    assert ambiguous.select("text", catalogue()).reason is SelectionHandoffReason.AMBIGUOUS


def test_empty_or_policy_ineligible_catalogue_never_calls_model() -> None:
    def forbidden(req: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected model call")

    selector = LocalModelSelector(model="qwen3:8b", transport=httpx.MockTransport(forbidden))
    assert selector.select("text", ()).reason is SelectionHandoffReason.UNSUPPORTED
    assert (
        selector.select("text", (replace(catalogue()[0], pure=False),)).reason
        is SelectionHandoffReason.UNSUPPORTED
    )


def test_local_adapter_pipeline_executes_and_verifies_but_deterministic_routes_skip_model() -> None:
    calls: list[httpx.Request] = []
    selector = LocalModelSelector(
        model="qwen3:8b",
        transport=httpx.MockTransport(
            lambda req: calls.append(req) or httpx.Response(200, json=envelope(proposal()))
        ),
    )
    app = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )
    try:
        assert app.agent.handle(WorkRequest("hello")).verified
        assert not calls
        outcome = app.agent.handle(WorkRequest("please count the letter r in strawberry"))
        assert outcome.verified and outcome.output == "3"
        assert len(calls) == 1
        with pytest.raises(UnsupportedWork):
            app.agent.handle(WorkRequest("please delete everything"))
        assert len(calls) == 2
    finally:
        app.shutdown()


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "2.0.0"),
        ("digest", "sha256:" + "0" * 64),
        ("version", True),
        ("digest", "not-a-digest"),
    ],
)
def test_core_rejects_stale_or_malformed_descriptor_bindings(field: str, value: object) -> None:
    from typing import cast

    from af.framework.types import SelectionDecision, SelectionProposal, SelectorLocality

    values: dict[str, object] = {"version": "1.0.0", "digest": catalogue()[0].digest}
    values[field] = value

    class BoundSelector:
        name = "bound-selector"
        locality = SelectorLocality.LOCAL

        def select(
            self, prompt: str, catalogue: tuple[ProcedureDescriptor, ...]
        ) -> SelectionDecision:
            return SelectionDecision(
                proposal=SelectionProposal(
                    "character-count",
                    "text.character_count",
                    {"text": "strawberry", "character": "r"},
                    0.95,
                    version=cast(str, values["version"]),
                    digest=cast(str, values["digest"]),
                )
            )

    app = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=BoundSelector(),
    )
    try:
        with pytest.raises(UnsupportedWork):
            app.agent.handle(WorkRequest("please count the letter r in strawberry"))
        trace = app.traces.list()[-1]
        assert not trace.verified and trace.output is None
        assert any(
            "stale_procedure" in event.detail or "invalid_decision" in event.detail
            for event in trace.events
        )
    finally:
        app.shutdown()


@pytest.mark.parametrize(
    "reason",
    [
        SelectionHandoffReason.CANCELLED,
        SelectionHandoffReason.TIMEOUT,
        SelectionHandoffReason.MALFORMED,
    ],
)
def test_core_records_typed_provider_handoff_without_execution(
    reason: SelectionHandoffReason,
) -> None:
    from af.framework.types import SelectionDecision, SelectorLocality

    class HandoffSelector:
        name = "handoff-selector"
        locality = SelectorLocality.LOCAL

        def select(
            self, prompt: str, catalogue: tuple[ProcedureDescriptor, ...]
        ) -> SelectionDecision:
            return SelectionDecision(reason=reason)

    app = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=HandoffSelector(),
    )
    try:
        with pytest.raises(UnsupportedWork):
            app.agent.handle(WorkRequest("unknown semantic request"))
        trace = app.traces.list()[-1]
        assert not trace.verified and trace.output is None
        assert any(f"code={reason.value}" in event.detail for event in trace.events)
    finally:
        app.shutdown()


def test_local_selector_sends_optional_credential_only_in_authorization_header() -> None:
    requests: list[httpx.Request] = []
    selector = LocalModelSelector(
        model="local",
        api_key="local-private-key",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request) or httpx.Response(200, json=envelope(proposal()))
            )
        ),
    )
    assert selector.select("count r in strawberry", catalogue()).proposal is not None
    assert requests[0].headers["Authorization"] == "Bearer local-private-key"
    assert b"local-private-key" not in requests[0].content
    assert "local-private-key" not in repr(selector)
    with pytest.raises(ValueError):
        LocalModelSelector(model="local", api_key="newline\ninjection")
