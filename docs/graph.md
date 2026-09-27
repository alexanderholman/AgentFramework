# Graph adapters

`GraphManager` requires both a declared capability and driver verification before it dispatches a
query or mutation. A database URI alone grants no capability and every operation fails explicitly
until a client is injected. Drivers never simulate external graph persistence in dictionaries.

`Neo4jCommunityDriver` accepts an official-driver-compatible client. Each operation opens an explicit
transaction with a server timeout; queries also apply a server-side row limit. Node/edge values are
bound parameters. Only validated schema identifiers are interpolated. Native query projections must
provide `node_id`, `labels`, and optional `score`. The adapter stores nodes under the internal `AFNode`
label, relationships under their portable relation type, and external identifiers in reserved
`af_id` properties. The deployment must provision suitable uniqueness constraints and use property
values supported by Neo4j. Node and relationship creation use `MERGE`; properties are merged.

`OrientDBDriver` accepts an HTTP client with configured authentication, using the official parameterized
JSON command endpoint. Live transport requires an HTTP(S) URI. Provision `AFNode EXTENDS V` and
`AFEdge EXTENDS E` with suitable uniqueness indexes on `af_id`. Portable labels and property maps use
`af_labels` and `af_properties`; relationship types use `af_relation`. Portable `$name` placeholders
are translated to native `:name` bindings without changing string literals or comments. Queries apply
SQL LIMIT and TIMEOUT EXCEPTION and bound the client timeout. Creation uses native CREATE commands.

For both adapters, query text is trusted application code and parameter values are data, including
strings containing punctuation or SQL keywords. Configure database privileges appropriate to the
application; parameter binding is not a read-only authorization mechanism. Each adapter accepts an
independent `capability_verifier` returning `CapabilitySet`; without that evidence the manager cannot
advertise or invoke query/persistence/traversal capabilities. Index management and cancellation are not
implemented or advertised. Closing a driver closes its owned client and invalidates capabilities.

`GraphManager.traverse` executes portable breadth-first discovery through native adjacency queries in
both adapters. It returns one shortest discovery `GraphPath` per reachable node, excluding the start
node. Cycles and repeated destinations are suppressed; edge identifiers determine deterministic tie
ordering. Direction and optional relation filters are bound into each native query. The traversal
has explicit depth, result-count, scanned-edge and wall-deadline bounds; each backend call receives
its remaining deadline and row budget. Exceeding an edge or time budget raises an error rather than
returning a partial result as complete. Reaching `max_results` returns that bounded result set.

Each hop uses a separate backend read. Concurrent graph changes are not observed through a single
snapshot, and this API does not enumerate every possible path. The default bounds are depth 3,
100 result paths, 10,000 scanned edges and 5 seconds. Provider/network operations must honor their
configured timeouts; Python cannot preempt a blocked synchronous client call. A missing start node
produces an empty traversal because it has no matching adjacency rows.

Current evidence is network-free protocol testing of binding, bounded result conversion, lifecycle,
CRUD dispatch, actual traversal control flow and failure propagation. Real Neo4j/OrientDB persistence,
topology and cross-service traversal equivalence still require service-backed conformance. Schema
migration and multioperation graph transactions remain unimplemented; `GraphSchema` and
`GraphTransaction` are data contracts only.

`GraphProjectionManager` adds operator-controlled rebuilds from a caller-verified
`GraphProjectionSnapshot` of authoritative `ResourceEnvelope` records and typed `GraphRelationship`
links. It preserves resource identities, tenant, ACL, provenance, source locator, projection version,
attributes and a source-text digest. Relationship endpoints must exist within one tenant and share
an ACL principal. Full source text remains at the authoritative locator.

Rebuild assigns new opaque generation-specific node and edge IDs, writes the real graph driver, and
reads every node and edge back for exact validation. Failed/interrupted builds stay unpublished.
The private SQLite catalog persists manifests, source digests, phases and active-generation aliases.
Cutover revalidates graph content and atomically compares the base generation plus alias revision;
competing or stale builds cannot replace a newer generation. Rollback revalidates the previous
generation and performs another fenced alias update. Catalog reconstruction after a process restart
retains active and rollback state. Read-back and cutover are separate operations: deployments must
keep generation data immutable and restrict raw graph writes to their projection owner.

The catalog and graph have no shared transaction. An interrupted rebuild can leave unpublished graph
records for operator cleanup; these records never become an active alias automatically. The source
snapshot is supplied by trusted application composition, not reconstructed from graph data. Snapshot
limits are 1,000 resources, 5,000 relationships and a 16 MiB metadata manifest. Empty-source rebuilds
are currently rejected explicitly.

`get_resource` resolves one active, tenant-scoped resource identity, checks its projected ACL, and
verifies the backend node before returning it. Applications must still revalidate current source
permissions and authorize raw native traversal; the generation catalog is not an authorization
service. Lifecycle tests use a clearly labelled graph protocol double plus real SQLite transactions;
they do not claim Neo4j/OrientDB service-backed projection certification.

References: [Neo4j transactions](https://neo4j.com/docs/python-manual/current/transactions/) and
[OrientDB REST commands](https://orientdb.dev/docs/develop/misc/OrientDB-REST.html).
