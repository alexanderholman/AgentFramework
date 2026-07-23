"""Framework-free in-memory implementation of the Ideon store boundary."""

from __future__ import annotations

from threading import RLock

from af.domain.ideons.canonical import (
    canonical_bytes,
    identity_signature,
    revision_id,
)
from af.domain.ideons.models import IdeonGraph, IdeonIdentity, IdeonKind, IdeonRef, Property
from af.domain.ideons.numeric import natural_to_hex
from af.domain.ideons.registry import (
    CanonicalKeyPolicy,
    IdentityConflictError,
    IdeonKeyPolicy,
    IdeonNotFoundError,
    NamespaceConflictError,
    RevisionConflictError,
    StableKeyConflictError,
    validate_constructed_key,
    validate_prime_key,
    validate_stable_key,
)


def _graph_references(graph: IdeonGraph) -> tuple[IdeonRef, ...]:
    references: set[IdeonRef] = set()

    def collect_properties(properties: tuple[Property, ...]) -> None:
        for prop in properties:
            if isinstance(prop.value, IdeonRef):
                references.add(prop.value)

    collect_properties(graph.identity.context)
    collect_properties(graph.annotations)
    for node in graph.nodes:
        if node.ideon is not None:
            references.add(node.ideon)
        if node.type_ref is not None:
            references.add(node.type_ref)
        collect_properties(node.properties)
    for edge in graph.edges:
        if isinstance(edge.relation, IdeonRef):
            references.add(edge.relation)
        collect_properties(edge.context)
    for hyperedge in graph.hyperedges:
        if isinstance(hyperedge.relation, IdeonRef):
            references.add(hyperedge.relation)
        collect_properties(hyperedge.context)
    return tuple(sorted(references))


def _key_label(key: int) -> str:
    return "0x" + natural_to_hex(key)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


class InMemoryIdeonRegistry:
    """Atomic in-memory interning with immutable, exact revision resolution.

    The registry owns pairings, never semantic identity. Graph canonical bytes are
    retained alongside their SHA-256 revisions so even a digest collision becomes
    an explicit conflict instead of silent reuse.
    """

    def __init__(
        self,
        version: str,
        *,
        namespace: str | None = None,
        key_policy: IdeonKeyPolicy | None = None,
    ) -> None:
        _require_text(version, "registry version")
        if namespace is not None:
            _require_text(namespace, "registry namespace")
        self._version = version
        self._namespace = namespace
        self._key_policy = key_policy or CanonicalKeyPolicy()
        self._by_ref: dict[IdeonRef, IdeonGraph] = {}
        self._revision_bytes: dict[str, bytes] = {}
        self._revision_ref: dict[str, IdeonRef] = {}
        self._identity_to_key: dict[bytes, int] = {}
        self._key_to_identity: dict[int, bytes] = {}
        self._key_to_signature: dict[int, str] = {}
        self._key_to_kind: dict[int, IdeonKind] = {}
        self._revisions: dict[int, list[IdeonRef]] = {}
        self._lock = RLock()

    @property
    def version(self) -> str:
        return self._version

    @property
    def namespace(self) -> str | None:
        return self._namespace

    def __len__(self) -> int:
        """Return the number of immutable graph revisions, not stable concepts."""

        with self._lock:
            return len(self._by_ref)

    def _check_namespace(self, identity: IdeonIdentity) -> None:
        if self._namespace is not None and identity.namespace != self._namespace:
            raise NamespaceConflictError(
                f"graph namespace {identity.namespace!r} does not match "
                f"registry namespace {self._namespace!r}"
            )

    @staticmethod
    def _validate_key_lane(identity: IdeonIdentity, key: int) -> None:
        if identity.kind is IdeonKind.PRIME:
            validate_prime_key(key)
        else:
            validate_constructed_key(key)

    def intern(self, graph: IdeonGraph, *, stable_key: int | None = None) -> IdeonRef:
        if not isinstance(graph, IdeonGraph):
            raise TypeError("graph must be an IdeonGraph")
        self._check_namespace(graph.identity)
        if stable_key is not None:
            self._validate_key_lane(graph.identity, stable_key)

        graph_bytes = canonical_bytes(graph)
        identity_bytes = canonical_bytes(graph.identity)
        revision = revision_id(graph)
        signature = identity_signature(graph.identity)

        with self._lock:
            missing_references = tuple(
                ref for ref in _graph_references(graph) if ref not in self._by_ref
            )
            if missing_references:
                missing = missing_references[0]
                raise IdeonNotFoundError(
                    "graph contains an unresolved exact Ideon reference: "
                    f"key={_key_label(missing.key)}, revision={missing.revision}"
                )
            recorded_revision_bytes = self._revision_bytes.get(revision)
            if recorded_revision_bytes is not None and recorded_revision_bytes != graph_bytes:
                raise RevisionConflictError(f"revision digest collision detected for {revision}")

            existing_revision_ref = self._revision_ref.get(revision)
            if existing_revision_ref is not None:
                if stable_key is not None and stable_key != existing_revision_ref.key:
                    raise StableKeyConflictError(
                        "canonical graph is already paired with stable key "
                        f"{_key_label(existing_revision_ref.key)}, "
                        f"not {_key_label(stable_key)}"
                    )
                return existing_revision_ref

            known_key = self._identity_to_key.get(identity_bytes)
            if known_key is not None:
                if stable_key is not None and stable_key != known_key:
                    raise StableKeyConflictError(
                        "identity is already paired with stable key "
                        f"{_key_label(known_key)}, not {_key_label(stable_key)}"
                    )
                key = known_key
            else:
                if stable_key is not None:
                    key = stable_key
                elif graph.identity.kind is IdeonKind.PRIME:
                    raise ValueError("new prime Ideons require an explicit manifested stable key")
                else:
                    key = self._key_policy.key_for(graph.identity)
                self._validate_key_lane(graph.identity, key)
                paired_identity = self._key_to_identity.get(key)
                if paired_identity is not None and paired_identity != identity_bytes:
                    raise StableKeyConflictError(
                        f"stable key {_key_label(key)} is already paired with another identity"
                    )
                self._identity_to_key[identity_bytes] = key
                self._key_to_identity[key] = identity_bytes
                self._key_to_signature[key] = signature
                self._key_to_kind[key] = graph.identity.kind
                self._revisions[key] = []

            ref = IdeonRef(key=key, revision=revision)
            existing_graph = self._by_ref.get(ref)
            if existing_graph is not None:
                if canonical_bytes(existing_graph) != graph_bytes:
                    raise RevisionConflictError(
                        f"exact reference {ref} resolves to different graph bytes"
                    )
                return ref

            self._by_ref[ref] = graph
            self._revision_bytes[revision] = graph_bytes
            self._revision_ref[revision] = ref
            self._revisions[key].append(ref)
            return ref

    def register(self, graph: IdeonGraph, *, stable_key: int | None = None) -> IdeonRef:
        return self.intern(graph, stable_key=stable_key)

    def revise(self, stable_key: int, graph: IdeonGraph) -> IdeonRef:
        validate_stable_key(stable_key)
        if not isinstance(graph, IdeonGraph):
            raise TypeError("graph must be an IdeonGraph")
        self._check_namespace(graph.identity)
        identity_bytes = canonical_bytes(graph.identity)

        with self._lock:
            recorded_identity = self._key_to_identity.get(stable_key)
            if recorded_identity is None:
                raise IdeonNotFoundError(f"unknown stable key: {_key_label(stable_key)}")
            if recorded_identity != identity_bytes:
                expected = self._key_to_signature[stable_key]
                received = identity_signature(graph.identity)
                raise IdentityConflictError(
                    "a revision cannot change stable identity: "
                    f"expected {expected}, received {received}"
                )
            return self.intern(graph, stable_key=stable_key)

    def resolve(self, ref: IdeonRef) -> IdeonGraph:
        if not isinstance(ref, IdeonRef):
            raise TypeError("resolve requires an exact IdeonRef")
        with self._lock:
            try:
                return self._by_ref[ref]
            except KeyError as error:
                raise IdeonNotFoundError(
                    f"unknown Ideon reference: key={_key_label(ref.key)}, revision={ref.revision}"
                ) from error

    def revisions(self, stable_key: int) -> tuple[IdeonRef, ...]:
        validate_stable_key(stable_key)
        with self._lock:
            try:
                return tuple(self._revisions[stable_key])
            except KeyError as error:
                raise IdeonNotFoundError(f"unknown stable key: {_key_label(stable_key)}") from error

    def latest(self, stable_key: int) -> IdeonRef:
        """Return the most recently registered revision without weakening resolve."""

        return self.revisions(stable_key)[-1]

    def manifest(self) -> tuple[tuple[int, str, IdeonKind], ...]:
        """Return the version-scoped stable-key manifest in numeric key order."""

        with self._lock:
            return tuple(
                (key, self._key_to_signature[key], self._key_to_kind[key])
                for key in sorted(self._key_to_identity)
            )

    def key_for_identity(self, identity: IdeonIdentity) -> int:
        """Return the registered key for an exact canonical identity."""

        if not isinstance(identity, IdeonIdentity):
            raise TypeError("identity must be an IdeonIdentity")
        self._check_namespace(identity)
        with self._lock:
            try:
                return self._identity_to_key[canonical_bytes(identity)]
            except KeyError as error:
                raise IdeonNotFoundError(
                    f"identity is not registered: {identity_signature(identity)}"
                ) from error
