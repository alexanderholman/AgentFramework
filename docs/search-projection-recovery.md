# Search projection recovery

AF uses the shared `TransactionalProjectionGuard` for durable inbox and contiguous
checkpoint evidence. Search content remains a derived index; the source is authoritative.

`af.search.projector.SearchProjector` accepts the ordinary manager/sink upsert and
delete boundary. A failed acknowledgement or relational commit can repeat a stable
mutation. This provides at-least-once delivery and does not detect a separately
restored search target that has lost content behind its relational checkpoint.

For a target that can atomically store content and a delivery receipt,
`af.search.projection_receipts.TransactionalSearchProjector` adds recovery fences:

- A physical name, immutable store generation and projection version bind each
  checkpoint. `LifecycleSearchProjectionTarget` checks an existing `IndexBuild`.
- The caller supplies the deterministic source owner of each tenant/resource
  identity. A different partition cannot mutate that identity through this projector.
- Target mutation and receipt commit together before the caller commits its
  relational transaction. Retry reconciles a target one delivery ahead without
  applying changed replay content. A target behind a committed checkpoint fails
  closed and requires rebuilding from the authoritative source.
- Only one advancing delivery per source/target is allowed in an outer relational
  transaction, including through wrappers and after savepoint rollback. Process
  deliveries in separate transactions. Wrappers must delegate operation claims.
- An apply failure rolls back checkpoint writes to a savepoint. Failed savepoint
  cleanup marks the outer transaction rollback-only even if the caller catches it.
- Unique-key and optimistic checkpoint races become typed retryable conflicts;
  other integrity failures retain their meaning. Retry with a fresh transaction.

`EmbeddedSearchDriver(path, physical_name=..., projection_version=...)` implements
the atomic receipt contract using SQLite FTS5. The metadata pair is explicit and
immutable; existing unbound content cannot be silently adopted. Store identity
includes file identity, so file replacement requires a fresh bound projector and
replay/rebuild. For this contract, all mutations of a managed target must use
`apply_projection`; mixing ordinary upsert/delete calls bypasses receipt evidence.
This is replay-safe at-least-once convergence, not a distributed transaction or an
exactly-once claim. Initial positions beyond the first event require a separately
established complete snapshot.

The ordinary OpenSearch/Elasticsearch adapters do not claim atomic receipt support.
Their existing live-service qualification remains separate. The relational receipt
suite uses real PostgreSQL/MySQL/MariaDB checkpoint stores with a real embedded
SQLite search target; it does not certify receipts in an external search service.

Focused verification:

```bash
uv run pytest tests/unit/test_search_projection_receipts.py \
  tests/unit/test_projection_guard.py tests/unit/test_relational_unique_conflict.py \
  tests/integration/test_search_embedded.py
uv run pytest tests/integration/test_search_projection_receipts_relational.py
```

The second command needs the corresponding `AF_POSTGRESQL_TEST_URL`,
`AF_MYSQL_TEST_URL` or `AF_MARIADB_TEST_URL`; missing profiles are explicit skips.
Each relational CI profile runs its configured real-service cases and the canonical
gate before enforcement.
