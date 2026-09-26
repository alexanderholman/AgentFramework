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
advertise or invoke query/persistence capabilities. Index management and cancellation are not
implemented or advertised. Closing a driver closes its owned client and invalidates capabilities.

Current evidence is network-free protocol testing of binding, bounded result conversion, lifecycle,
CRUD dispatch and failure propagation. Real Neo4j/OrientDB persistence, topology, schema migration,
portable traversal execution and cross-service traversal equivalence still require service-backed
conformance. The `GraphTraversal`, `GraphPath`, `GraphSchema`, and `GraphTransaction` data contracts do
not themselves implement those operations.

References: [Neo4j transactions](https://neo4j.com/docs/python-manual/current/transactions/) and
[OrientDB REST commands](https://orientdb.dev/docs/develop/misc/OrientDB-REST.html).
