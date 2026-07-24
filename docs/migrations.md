# Relational migration contracts

AF migration state is explicit application data. The framework does not infer migrations from
ORM models, package imports, filesystem timestamps, installed drivers, or database inspection.
Every migration has a canonical timestamped identifier, an immutable ordered sequence of
parameter-free statements, optional reversal statements, exact verification probes, and a
SHA-256 checksum over those declared values.

## Installation and planning

`MigrationRunner.bootstrap()` creates only the fixed migration ledger and lease tables. It is
idempotent and deliberately separate from planning so operators can provision migration
metadata before asking for a dry run.

The applied ledger must be an exact prefix of the current sorted catalogue. Unknown applied
identifiers, missing earlier migrations, duplicate identifiers, non-contiguous applied order,
invalid batches, or checksum drift fail closed. A plan exposes migration identifiers and
direction without exposing SQL.

`apply_up(dry_run=True)` and `apply_down(dry_run=True)` only return a plan after bootstrap.
They do not execute schema statements or alter the ledger. Limits and down-step counts are
finite and validated before database work.

## Application and reversal

A database-backed ownership lease serializes schema mutation across processes. The lease has a
bounded duration, an explicit owner, canonical UTC acquisition/expiry values, and stale-lease
reclamation. Concurrent owners fail with a typed transient lock error. Operators must choose a
lease duration longer than the largest individual migration until lease renewal is introduced.

Each migration runs in one driver transaction. Its statements execute in declared order,
verification probes run before the ledger row is inserted, and the ledger record commits with
the schema change when the selected backend supports transactional DDL. A failed migration
does not claim that earlier migrations from the same invocation were reversed.

Down migration proceeds in reverse applied order. A migration without declared reversal
statements fails before that migration or its ledger entry is changed. Reversal remains an
explicit supported operation, not an assumption that arbitrary DDL can be reconstructed.

## Verification and portability

Verification probes are parameter-free read-only statements with exact expected rows. They
prove declared postconditions and detect later schema drift without printing SQL or protected
database details through public errors.

The framework's transaction contract is driver-neutral, but DDL atomicity is a backend
capability rather than a universal SQL property. SQLite interruption and rollback behavior is
covered by the initial migration-core suite. PostgreSQL, MySQL, and MariaDB migration
certification must be added before claiming equivalent fresh-install, interruption, lock, and
upgrade guarantees on those services.

The migration core does not yet provide CLI commands, package migration discovery, online
schema-change orchestration, lease renewal, or deployment coordination. Those layers must use
the same ledger, checksums, lock ownership, plans, and verification results rather than
reimplementing migration truth.
