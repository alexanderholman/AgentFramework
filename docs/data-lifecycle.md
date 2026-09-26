# Reference data lifecycle

SQLite application startup applies the immutable reference schema catalogue with
AF's existing `MigrationRunner`. Its ledger records the exact migration checksum;
column and index probes detect schema drift. Existing alpha databases are adopted
only when they match the declared schema. Add future changes as ordered migrations
rather than modifying the baseline. Extension repositories own their explicitly
named schemas; the core catalogue does not assume ownership of their tables.

```bash
af data init
af data backup /private/backup.db
af data restore /private/backup.db /private/restored.db
af data prune --before 2026-01-01T00:00:00+00:00
af data prune --before 2026-01-01T00:00:00+00:00 --execute
```

Backup uses SQLite's consistent online backup API, verifies database and
foreign-key integrity, and publishes a private `0600` database plus an authenticated
manifest with its SHA-256 checksum. Existing destination files are never replaced.
The reference backup bound is 1 GiB.

Keep the source `.workflow-key` separately with the same access protection as
the database. Restore authenticates the manifest with that configured key, checks
the file checksum and SQLite integrity, and publishes a new database plus the
same private key identity at the new `.workflow-key` location. Existing key files
are never replaced. This preserves authenticated workflow and operation history
when the restored application starts. A restore does not change the application's
configured database URL or replace a running database.

Pruning previews expired record counts by default. `--execute` applies core
retention transactionally: expired tasks, unreferenced traces, and terminal
program-job outputs/claims/events. Active jobs and traces referenced by retained
tasks survive. Workflow, authentication, admission and migration histories require
their own explicit retention policies. Backups remain independent copies and must
be retained or removed separately under the applicable data policy.
