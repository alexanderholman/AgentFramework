"""Reusable real-service conformance cases for search drivers."""

from __future__ import annotations

import hashlib

from af.framework.errors import PlatformError, PlatformReason
from af.search.contracts import (
    ResourceEnvelope,
    ResourceKind,
    SearchFilter,
    SearchQuery,
)
from af.search.manager import SearchDriver
from af.testing.conformance import ConformanceCase, DriverConformanceSuite

__all__ = ("SearchDriverConformanceSuite", "search_driver_cases")


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _resource(
    resource_id: str,
    *,
    kind: ResourceKind = ResourceKind.TEXT,
    tenant_id: str = "tenant-a",
    acl: tuple[str, ...] = ("group:research",),
    text: str = "bounded searchable energy content",
    projection_version: int = 1,
) -> ResourceEnvelope:
    digest = hashlib.sha256(resource_id.encode()).hexdigest()
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=kind,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{resource_id}",
        tenant_id=tenant_id,
        projection_version=projection_version,
        acl=acl,
        provenance=(f"sha256:{digest}",),
        text=text,
        attributes={"language": "en"},
    )


def _query(
    text: str = "energy",
    *,
    tenant_id: str = "tenant-a",
    acl: tuple[str, ...] = ("group:research",),
    limit: int = 20,
) -> SearchQuery:
    return SearchQuery(
        text=text,
        filters=(
            SearchFilter("tenant_id", (tenant_id,)),
            SearchFilter("acl", acl),
        ),
        limit=limit,
        deadline_ms=2_000,
    )


def _assert_canonical_sources(driver: SearchDriver) -> None:
    resources = tuple(_resource(f"resource-{kind.value}", kind=kind) for kind in ResourceKind)
    _check(
        driver.upsert(resources) == len(resources),
        "search driver did not acknowledge every canonical source",
    )
    hits = driver.search(_query(limit=len(resources)))
    expected = {resource.resource_id: resource for resource in resources}
    _check(
        {hit.resource_id for hit in hits} == set(expected),
        "search driver did not return every canonical source",
    )
    for hit in hits:
        resource = expected[hit.resource_id]
        _check(
            hit.source_id == resource.source_id
            and hit.authoritative_locator == resource.authoritative_locator
            and hit.tenant_id == resource.tenant_id
            and hit.projection_version == resource.projection_version
            and hit.acl_evidence == resource.acl
            and hit.provenance == resource.provenance,
            "search hit lost canonical identity or evidence",
        )


def _assert_policy_pushdown(driver: SearchDriver) -> None:
    allowed = _resource(
        "resource-allowed",
        acl=("group:allowed",),
        text="energy",
    )
    denied = tuple(
        _resource(
            f"resource-denied-{index}",
            acl=("group:denied",),
            text="energy " * 32,
        )
        for index in range(32)
    )
    foreign_tenant = tuple(
        _resource(
            f"resource-foreign-{index}",
            tenant_id="tenant-b",
            acl=("group:allowed",),
            text="energy " * 32,
        )
        for index in range(32)
    )
    driver.upsert((allowed, *denied, *foreign_tenant))
    hits = driver.search(_query(acl=("group:allowed",), limit=1))
    _check(
        tuple(hit.resource_id for hit in hits) == ("resource-allowed",),
        "search driver applied policy after top-k ranking",
    )
    _check(
        hits[0].acl_evidence == ("group:allowed",),
        "search driver returned invalid ACL evidence",
    )


def _assert_replacement_and_tombstone(driver: SearchDriver) -> None:
    first = _resource("resource-replaced", text="initial energy", projection_version=1)
    replacement = _resource(
        "resource-replaced",
        text="updated energy",
        projection_version=2,
    )
    driver.upsert((first,))
    driver.upsert((replacement,))
    hits = driver.search(_query("updated"))
    _check(
        len(hits) == 1 and hits[0].projection_version == 2,
        "search driver did not replace the exact projection identity",
    )
    _check(
        not driver.search(_query("initial")),
        "search driver retained replaced projection text",
    )
    _check(
        driver.delete(
            tenant_id=first.tenant_id,
            source_id=first.source_id,
            resource_id=first.resource_id,
        ),
        "search driver did not acknowledge the first tombstone",
    )
    _check(
        not driver.delete(
            tenant_id=first.tenant_id,
            source_id=first.source_id,
            resource_id=first.resource_id,
        ),
        "search driver did not make tombstone deletion idempotent",
    )
    _check(not driver.search(_query("updated")), "search tombstone left a visible projection")


def _assert_tenant_scoped_identity_and_delete(driver: SearchDriver) -> None:
    first = _resource("resource-shared", tenant_id="tenant-a")
    second = _resource("resource-shared", tenant_id="tenant-b")
    driver.upsert((first, second))

    first_hits = driver.search(_query(tenant_id="tenant-a"))
    second_hits = driver.search(_query(tenant_id="tenant-b"))
    _check(
        len(first_hits) == 1 and first_hits[0].tenant_id == "tenant-a",
        "search driver collapsed the first tenant identity",
    )
    _check(
        len(second_hits) == 1 and second_hits[0].tenant_id == "tenant-b",
        "search driver collapsed the second tenant identity",
    )
    _check(
        driver.delete(
            tenant_id=first.tenant_id,
            source_id=first.source_id,
            resource_id=first.resource_id,
        ),
        "search driver did not delete the tenant-scoped identity",
    )
    _check(
        not driver.search(_query(tenant_id="tenant-a")),
        "search driver left the deleted tenant identity visible",
    )
    surviving = driver.search(_query(tenant_id="tenant-b"))
    _check(
        len(surviving) == 1 and surviving[0].tenant_id == "tenant-b",
        "search driver deleted a different tenant identity",
    )


def _assert_deterministic_order(driver: SearchDriver) -> None:
    first = _resource("resource-a", text="shared deterministic energy")
    second = _resource("resource-b", text="shared deterministic energy")
    driver.upsert((second, first))
    expected = ("resource-a", "resource-b")
    for _ in range(3):
        hits = driver.search(_query("shared deterministic energy"))
        _check(
            tuple(hit.resource_id for hit in hits) == expected,
            "search driver returned nondeterministic equal-score ordering",
        )


def _assert_error_redaction(driver: SearchDriver) -> None:
    secret = "private-search-filter-7f2d"
    query = SearchQuery(
        text="energy",
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
            SearchFilter("secret_filter", (secret,)),
        ),
    )
    captured: PlatformError | None = None
    try:
        driver.search(query)
    except PlatformError as error:
        captured = error
    _check(captured is not None, "search driver accepted an unsupported policy filter")
    if captured is not None:
        _check(
            captured.reason is PlatformReason.CAPABILITY_UNAVAILABLE,
            "unsupported search filter failed with the wrong stable reason",
        )
        _check(
            secret not in str(captured) and secret not in repr(captured),
            "search driver disclosed unsupported filter content",
        )


def _assert_shutdown(driver: SearchDriver) -> None:
    driver.close()
    driver.close()
    captured: PlatformError | None = None
    try:
        driver.search(_query())
    except PlatformError as error:
        captured = error
    _check(captured is not None, "closed search driver accepted a new query")
    if captured is not None:
        _check(
            captured.reason is PlatformReason.SHUTDOWN_FAILED,
            "closed search driver failed with the wrong stable reason",
        )


def search_driver_cases() -> tuple[ConformanceCase[SearchDriver], ...]:
    """Return the shared, driver-neutral search conformance cases."""

    return (
        ConformanceCase("canonical-sources", _assert_canonical_sources),
        ConformanceCase("policy-pushdown", _assert_policy_pushdown),
        ConformanceCase("replacement-and-tombstone", _assert_replacement_and_tombstone),
        ConformanceCase("tenant-scoped-identity", _assert_tenant_scoped_identity_and_delete),
        ConformanceCase("deterministic-order", _assert_deterministic_order),
        ConformanceCase("error-redaction", _assert_error_redaction),
        ConformanceCase("shutdown", _assert_shutdown),
    )


class SearchDriverConformanceSuite(DriverConformanceSuite[SearchDriver]):
    """Certify one real search driver against the shared AF contract."""

    def __init__(self, name: str = "search-driver") -> None:
        super().__init__(name, search_driver_cases())
