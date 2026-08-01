"""Shared transport-neutral presentation mappings."""

from __future__ import annotations

from af.application.search import SearchIndexResult, SearchIndexSourceResult, SearchResult
from af.domain.agent_routing import AgentRouteResult
from af.domain.tasks import Task
from af.framework.types import ExecutionOutcome, ProcedureDescriptor
from af.search.contracts import SearchHit


def task_to_dict(task: Task) -> dict[str, object]:
    return {
        "id": task.id,
        "prompt": task.prompt,
        "output": task.output,
        "intent": task.intent,
        "route": task.route,
        "tier": task.tier,
        "status": task.status,
        "verified": task.verified,
        "reused": task.reused,
        "trace_id": task.trace_id,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def delete_to_dict(task_id: str) -> dict[str, object]:
    return {"id": task_id, "deleted": True}


def outcome_to_dict(outcome: ExecutionOutcome) -> dict[str, object]:
    return {
        "prompt": outcome.prompt,
        "output": outcome.output,
        "intent": outcome.intent,
        "route": outcome.route,
        "tier": outcome.tier,
        "status": outcome.status,
        "verified": outcome.verified,
        "reused": outcome.reused,
        "trace_id": outcome.trace_id,
    }


def agent_route_to_dict(route: AgentRouteResult) -> dict[str, object]:
    return {
        "prompt": route.prompt,
        "status": route.status.value,
        "agent": route.agent.value if route.agent is not None else None,
        "reason": route.reason.value,
        "candidates": [candidate.value for candidate in route.candidates],
    }


def procedure_descriptor_to_dict(descriptor: ProcedureDescriptor) -> dict[str, object]:
    return {
        "procedure_id": descriptor.procedure_id,
        "version": descriptor.version,
        "intent": descriptor.intent,
        "arguments": [
            {
                "name": argument.name,
                "value_type": argument.value_type.value,
                "min_length": argument.min_length,
                "max_length": argument.max_length,
                "allow_blank": argument.allow_blank,
            }
            for argument in descriptor.arguments
        ],
        "effect": descriptor.effect.value,
        "risk": descriptor.risk.value,
        "requires_confirmation": descriptor.requires_confirmation,
        "tier": descriptor.tier.name.lower(),
        "pure": descriptor.pure,
        "replay_safe": descriptor.replay_safe,
        "has_selection_grounding": descriptor.has_selection_grounding,
        "metrics": {
            "cost": descriptor.metrics.cost,
            "latency": descriptor.metrics.latency,
            "intelligence": descriptor.metrics.intelligence,
        },
    }


def search_hit_to_dict(hit: SearchHit) -> dict[str, object]:
    return {
        "resource_id": hit.resource_id,
        "source_id": hit.source_id,
        "authoritative_locator": hit.authoritative_locator,
        "tenant_id": hit.tenant_id,
        "projection_version": hit.projection_version,
        "acl_evidence": list(hit.acl_evidence),
        "provenance": list(hit.provenance),
        "score": hit.score,
        "rank": hit.rank,
        "highlights": list(hit.highlights),
    }


def search_result_to_dict(result: SearchResult) -> dict[str, object]:
    return {
        "items": [search_hit_to_dict(hit) for hit in result.items],
        "total": result.total,
    }


def search_index_source_to_dict(result: SearchIndexSourceResult) -> dict[str, object]:
    return {"source": result.source, "indexed": result.indexed}


def search_index_result_to_dict(result: SearchIndexResult) -> dict[str, object]:
    return {
        "indexed": result.indexed,
        "sources": [search_index_source_to_dict(item) for item in result.sources],
    }
