from __future__ import annotations

import asyncio

import pytest

from af.framework.context import ExecutionContext, Principal, TraceContext
from af.observability import TelemetryContext

_TENANT_REF = "sha256:" + ("a" * 64)


def _execution_context() -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(
            subject_id="private-subject",
            principal_type="user",
            roles=("private-role",),
        ),
        tenant_id="private-tenant",
        correlation_id="correlation-1",
        causation_id="causation-1",
        trace=TraceContext("trace-1", "span-1", 1),
    )


def test_telemetry_context_projects_only_safe_execution_metadata() -> None:
    context = TelemetryContext.from_execution(
        _execution_context(),
        operation="bus.dispatch",
        tenant_ref=_TENANT_REF,
    )

    assert context == TelemetryContext(
        operation="bus.dispatch",
        correlation_id="correlation-1",
        causation_id="causation-1",
        trace_id="trace-1",
        span_id="span-1",
        trace_flags=1,
        tenant_ref=_TENANT_REF,
    )
    rendered = repr(context)
    assert "private-subject" not in rendered
    assert "private-tenant" not in rendered
    assert "correlation-1" not in rendered
    assert "[REDACTED]" in rendered


def test_telemetry_context_survives_async_and_process_envelope_round_trips() -> None:
    projected = TelemetryContext.from_execution(
        _execution_context(),
        operation="queue.dispatch",
        tenant_ref=_TENANT_REF,
    )

    async def cross_boundary() -> TelemetryContext:
        await asyncio.sleep(0)
        return TelemetryContext.from_envelope(dict(projected.to_envelope()))

    assert asyncio.run(cross_boundary()) == projected
    assert projected.to_envelope() == (
        ("version", "af.telemetry.v1"),
        ("operation", "queue.dispatch"),
        ("correlation_id", "correlation-1"),
        ("causation_id", "causation-1"),
        ("trace_id", "trace-1"),
        ("span_id", "span-1"),
        ("trace_flags", "1"),
        ("tenant_ref", _TENANT_REF),
    )


@pytest.mark.parametrize(
    "values",
    [
        {"operation": "Bad Operation"},
        {"trace_flags": -1},
        {"trace_flags": 256},
        {"span_id": "span-without-trace"},
        {"tenant_ref": "private-tenant"},
    ],
)
def test_telemetry_context_rejects_unsafe_values(values: dict[str, object]) -> None:
    arguments: dict[str, object] = {"operation": "valid"}
    arguments.update(values)
    with pytest.raises(ValueError):
        TelemetryContext(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"version": "af.telemetry.v2", "operation": "valid"},
        {"version": "af.telemetry.v1", "operation": "valid", "principal": "private"},
        {"version": "af.telemetry.v1", "operation": "valid", "trace_flags": "-1"},
        {"version": "af.telemetry.v1", "operation": "valid", "trace_flags": "1.0"},
        {
            "version": "af.telemetry.v1",
            "operation": "valid",
            "correlation_id": "x" * 201,
        },
    ],
)
def test_telemetry_envelope_fails_closed(envelope: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        TelemetryContext.from_envelope(envelope)


def test_telemetry_projection_rejects_invalid_context_and_raw_tenant_identity() -> None:
    with pytest.raises(TypeError):
        TelemetryContext.from_execution(object(), operation="valid")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="opaque sha256"):
        TelemetryContext.from_execution(
            _execution_context(),
            operation="valid",
            tenant_ref="private-tenant",
        )
