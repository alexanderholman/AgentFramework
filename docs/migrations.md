# Relational migration contracts

AF migration state is explicit application data. The framework does not infer migrations from
ORM models, package imports, filesystem timestamps, installed drivers, or database inspection.
Every migration has a canonical timestamped identifier, an explicit schema-or-data mode, an
immutable ordered sequence of parameter-free statements, optional reversal statements, exact
verification probes, and a SHA-256 checksum over those declared values. Schema is the
fail-closed default. The default schema mode preserves the checksum identity of catalogues
created before modes were introduced; changing a migration to data mode changes its checksum.
An already-applied pre-mode DML migration can opt into the exact compatibility digest with
`accept_pre_mode_checksum=True`. The opt-in accepts only the checksum recomputed from the same
identifier, statements, reversals, and probes with no mode field; fresh applications still
write the current data-mode checksum. Keep the opt-in while any deployed ledger contains that
legacy digest.

## Installation and planning

`MigrationRunner.bootstrap()` creates only the fixed migration ledger and lease tables. It is
idempotent and deliberately separate from planning so operators can provision migration
metadata before asking for a dry run. On MySQL and MariaDB this fixed metadata DDL is
idempotent but is not claimed to be atomic: those engines can implicitly commit DDL.

The applied ledger must be an exact prefix of the current sorted catalogue. Unknown applied
identifiers, missing earlier migrations, duplicate identifiers, non-contiguous applied order,
invalid batches, or checksum drift fail closed. A plan exposes migration identifiers and
direction without exposing SQL.

`apply_up(dry_run=True)` and `apply_down(dry_run=True)` return a plan only after checking that
the selected migration modes can execute on the driver. They do not execute migration
statements or alter the ledger. `plan_up()` and `plan_down()` remain pure inventory operations.
Limits and down-step counts are finite and validated before database work.

## Application and reversal

A database-backed ownership lease serializes migration mutation across processes. The lease has
a bounded duration, an explicit owner, canonical UTC acquisition/expiry values, stale-lease
reclamation, and ownership-fenced renewal around each migration. Concurrent owners fail with a
typed transient lock error. Operators must still choose a lease duration longer than one
individual statement because a running database statement cannot be interrupted merely to
renew the lease.

Each migration runs in one driver transaction. Its statements execute in declared order,
verification probes run before the ledger row is inserted, and the ledger record commits with
the declared change. Before executing any selected migration, the runner checks the entire plan:
if any selected migration is in schema mode, the driver must advertise verified transactional
DDL support. This prevents an earlier data migration in a mixed plan from committing before a
later unsupported schema migration is discovered. A failed migration does not claim that
earlier migrations from the same invocation were reversed.

Down migration proceeds in reverse applied order. A migration without declared reversal
statements fails before that migration or its ledger entry is changed. Reversal remains an
explicit supported operation, not an assumption that arbitrary DDL can be reconstructed.

## Verification and portability

Verification probes are parameter-free read-only statements with exact expected rows. They
prove declared postconditions and detect later schema drift without printing SQL or protected
database details through public errors.

The framework's transaction contract is driver-neutral, but DDL atomicity is a backend
capability rather than a universal SQL property.

| Driver | Schema mode | Data mode |
| --- | --- | --- |
| SQLite | Transactional DDL certified | Transactional data certified |
| PostgreSQL | Transactional DDL accepted by PostgreSQL inside the runner transaction is certified; statements PostgreSQL forbids in a transaction fail without a ledger commit | Transactional data certified |
| MySQL | Rejected before migration statements execute | Transactional data certified |
| MariaDB | Rejected before migration statements execute | Transactional data certified |

Data mode is an explicit trusted declaration, not a SQL parser. A data migration must not
contain DDL or other implicit-commit statements such as MySQL or MariaDB `TRUNCATE`. MySQL and
MariaDB data migrations must also target a transactional storage engine such as InnoDB. Their
AF-owned `af_migrations` and `af_migration_locks` tables must use InnoDB as well; real-service
certification verifies both metadata tables and the migrated application table rather than
assuming the server default. Support does not claim rollback of multi-statement DDL, and no
migration mode provides an exactly-once guarantee.

The pre-mode checksum opt-in is likewise a reviewed attestation that the unchanged historical
migration contains only transactional data operations. It is not valid for schema mode and
must not be used to relabel legacy DDL or mixed DDL/data migrations.

The CLI exposes bootstrap, plan, status, up, down, and verification only when an application
binds a `MigrationRunner`. An embedded host may inject that application or its factory.
The installed `af` console instead requires an explicit
`--application-factory module:callable` before any migration command can construct an
application. The synchronous no-argument factory must return a fully composed `AFApplication`
whose `MigrationProvider` owns the immutable catalogue and explicit named database connection.

Selecting that option is authorization by a trusted local operator to import and execute a
trusted project composition root in-process. It is not package discovery, package approval, or
a sandbox, and it must not be populated from untrusted or implicitly discovered input. A
project factory that consumes AF extension packages must still use the reviewed
discover/plan/approve/load package boundary. Help, invalid command lines, and migration
commands without the option do not import the factory or boot the ambient default application.

Package migration discovery and online schema-change orchestration remain separate deployment
concerns. Those layers must use the same ledger, checksums, lock ownership, plans, and
verification results rather than reimplementing migration truth.
