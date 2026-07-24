# PostgreSQL relational driver

AF's PostgreSQL adapter implements the same bounded synchronous relational contract as the
reference SQLite adapter while preserving PostgreSQL's actual transaction semantics.

## Runtime contract

- The connection URL must use the explicit `postgresql+psycopg` SQLAlchemy dialect.
- The adapter opens one physical connection and serializes transactions on that connection.
  Application-level concurrency uses the existing bounded `DatabasePool`, whose members are
  independent driver instances.
- Transactions, savepoints, streaming cursors, and cancellation are advertised only when the
  runtime exposes and passes the corresponding checks.
- A read-only transaction is enforced by PostgreSQL with `SET TRANSACTION READ ONLY`; caller
  statement metadata cannot turn a write into an authorized read.
- SQLAlchemy and psycopg details remain behind the driver boundary. Public failures contain
  stable AF reason codes and never include SQL, parameters, connection URLs, or credentials.
- JSON, full-text search, advisory locks, and vendor-native access are not claimed by this
  baseline adapter. They require separate explicit capability contracts and evidence.

## Registration and dependencies

`postgresql_registration()` is inert until the application resolves a configured connection.
It binds the driver manifest and runtime evidence to the installed psycopg and SQLAlchemy
versions, implementation bytes, configuration digest, deployment profile, policy version, and
observation time. Missing psycopg fails closed; AF never installs it or falls back to SQLite.

The repository's real-service workflow installs an exact ephemeral psycopg build and runs the
shared relational conformance suite against a digest-pinned PostgreSQL service. Production
applications must install and govern their chosen psycopg runtime explicitly.

## Certification boundary

Unit tests use a non-certifying SQLAlchemy fixture to exercise lifecycle and failure boundaries.
Production evidence comes only from the real PostgreSQL workflow, which covers the shared
commit, rollback, savepoint, stream-lifetime, read-only, error-redaction, and shutdown suite,
plus cancellation recovery, pooling, runtime registration, and connection failure.
