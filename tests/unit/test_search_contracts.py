from __future__ import annotations

from types import MappingProxyType

import pytest

from af.search.contracts import (
    ResourceEnvelope,
    ResourceKind,
    SearchFilter,
    SearchHit,
    SearchQuery,
)


@pytest.mark.parametrize("kind", tuple(ResourceKind))
def test_every_declared_source_enters_one_canonical_resource_envelope(kind: ResourceKind) -> None:
    envelope = ResourceEnvelope(
        resource_id=f"resource-{kind.value}",
        kind=kind,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{kind.value}",
        tenant_id="tenant-a",
        projection_version=3,
        acl=("group:research", "user:alex"),
        provenance=("sha256:" + "a" * 64,),
        text="bounded searchable content",
        attributes={"format": kind.value},
    )

    assert envelope.kind is kind
    assert envelope.acl == ("group:research", "user:alex")
    assert envelope.provenance == ("sha256:" + "a" * 64,)
    assert envelope.attributes == {"format": kind.value}
    assert isinstance(envelope.attributes, MappingProxyType)


def test_resource_envelope_snapshots_mutable_inputs() -> None:
    attributes = {"language": "en"}
    envelope = ResourceEnvelope(
        resource_id="resource-1",
        kind=ResourceKind.MARKDOWN,
        source_id="source-1",
        authoritative_locator="af://source-1/resource-1",
        tenant_id="tenant-a",
        projection_version=1,
        acl=("tenant:tenant-a",),
        provenance=("sha256:" + "b" * 64,),
        text="hello world",
        attributes=attributes,
    )

    attributes["language"] = "fr"

    assert envelope.attributes == {"language": "en"}
    with pytest.raises(TypeError):
        envelope.attributes["language"] = "es"  # type: ignore[index]


def test_resource_envelope_accepts_normal_document_whitespace() -> None:
    envelope = ResourceEnvelope(
        resource_id="resource-1",
        kind=ResourceKind.MARKDOWN,
        source_id="source-1",
        authoritative_locator="af://source-1/resource-1",
        tenant_id="tenant-a",
        projection_version=1,
        acl=("tenant:tenant-a",),
        provenance=("sha256:" + "b" * 64,),
        text="# Heading\r\n\r\nBody\twith indentation\n",
    )

    assert envelope.text == "# Heading\r\n\r\nBody\twith indentation\n"


def test_query_requires_policy_pushdown_before_top_k() -> None:
    query = SearchQuery(
        text="interface energy",
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research", "user:alex")),
        ),
        limit=25,
        deadline_ms=500,
    )

    assert query.required_filter("tenant_id") == ("tenant-a",)
    assert query.required_filter("acl") == ("group:research", "user:alex")
    assert query.required_filter("missing") is None


@pytest.mark.parametrize(
    "filters",
    [
        (SearchFilter("format", ("markdown",)),),
        (SearchFilter("tenant_id", ("tenant-a",)),),
        (SearchFilter("acl", ("group:research",)),),
    ],
)
def test_query_rejects_missing_policy_filters(
    filters: tuple[SearchFilter, ...],
) -> None:
    with pytest.raises(ValueError):
        SearchQuery(text="interface energy", filters=filters)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": ""},
        {"filters": ()},
        {"limit": 0},
        {"limit": 1_001},
        {"deadline_ms": 0},
        {"deadline_ms": 60_001},
    ],
)
def test_query_rejects_ambiguous_or_unbounded_inputs(kwargs: dict[str, object]) -> None:
    options: dict[str, object] = {
        "text": "interface energy",
        "filters": (
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
        ),
        "limit": 10,
        "deadline_ms": 100,
    }
    options.update(kwargs)

    with pytest.raises(ValueError):
        SearchQuery(**options)  # type: ignore[arg-type]


def test_hit_always_carries_identity_locator_acl_and_provenance() -> None:
    hit = SearchHit(
        resource_id="resource-1",
        source_id="source-1",
        authoritative_locator="af://source-1/resource-1",
        tenant_id="tenant-a",
        projection_version=4,
        acl_evidence=("tenant:tenant-a", "user:alex"),
        provenance=("sha256:" + "c" * 64,),
        score=0.75,
        rank=1,
        highlights=("interface <em>energy</em>",),
    )

    assert hit.rank == 1
    assert hit.score == 0.75
    assert hit.tenant_id == "tenant-a"
    assert hit.acl_evidence == ("tenant:tenant-a", "user:alex")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SearchFilter("not valid", ("tenant-a",)),
        lambda: SearchFilter("tenant_id", ()),
        lambda: ResourceEnvelope(
            resource_id="resource-1",
            kind=ResourceKind.TEXT,
            source_id="source-1",
            authoritative_locator="af://source-1/resource-1",
            tenant_id="tenant-a",
            projection_version=0,
            acl=("tenant:tenant-a",),
            provenance=("sha256:" + "d" * 64,),
            text="text",
        ),
        lambda: ResourceEnvelope(
            resource_id="resource-1",
            kind=ResourceKind.TEXT,
            source_id="source-1",
            authoritative_locator="af://source-1/resource-1",
            tenant_id="tenant-a",
            projection_version=1,
            acl=("tenant:tenant-a",),
            provenance=("sha256:" + "d" * 64,),
            text="unsafe\x00content",
        ),
        lambda: SearchHit(
            resource_id="resource-1",
            source_id="source-1",
            authoritative_locator="af://source-1/resource-1",
            tenant_id="tenant-a",
            projection_version=1,
            acl_evidence=(),
            provenance=("sha256:" + "e" * 64,),
            score=1.0,
            rank=1,
        ),
        lambda: SearchHit(
            resource_id="resource-1",
            source_id="source-1",
            authoritative_locator="af://source-1/resource-1",
            tenant_id="not a tenant",
            projection_version=1,
            acl_evidence=("tenant:tenant-a",),
            provenance=("sha256:" + "e" * 64,),
            score=1.0,
            rank=1,
        ),
    ],
)
def test_contracts_fail_closed_when_required_evidence_is_missing(factory) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        factory()
