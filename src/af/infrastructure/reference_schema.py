"""Immutable reference schema v1; append migrations rather than editing this baseline."""

from __future__ import annotations

from af.framework.migrations import Migration, MigrationProbe
from af.framework.relational import DatabaseRow, DatabaseStatement

_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS af_tasks (\n"
    "\tid VARCHAR(36) NOT NULL, \n"
    "\tprompt TEXT NOT NULL, \n"
    "\toutput TEXT NOT NULL, \n"
    "\tintent VARCHAR(128) NOT NULL, \n"
    "\troute VARCHAR(128) NOT NULL, \n"
    "\ttier VARCHAR(32) NOT NULL, \n"
    "\tstatus VARCHAR(32) NOT NULL, \n"
    "\tverified BOOLEAN NOT NULL, \n"
    "\treused BOOLEAN NOT NULL, \n"
    "\ttrace_id VARCHAR(36) NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tupdated_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (id)\n"
    ")",
    "CREATE INDEX IF NOT EXISTS ix_af_tasks_trace_id ON af_tasks (trace_id)",
    "CREATE TABLE IF NOT EXISTS af_run_traces (\n"
    "\tid VARCHAR(36) NOT NULL, \n"
    "\tprompt TEXT NOT NULL, \n"
    "\tintent VARCHAR(128), \n"
    "\targuments_json TEXT NOT NULL, \n"
    "\troute VARCHAR(128), \n"
    "\ttier VARCHAR(32), \n"
    "\toutput TEXT, \n"
    "\tverified BOOLEAN NOT NULL, \n"
    "\treused BOOLEAN NOT NULL, \n"
    "\tregression_passed BOOLEAN NOT NULL, \n"
    "\tpromoted BOOLEAN NOT NULL, \n"
    "\tstatus VARCHAR(32) NOT NULL, \n"
    "\tevents_json TEXT NOT NULL, \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (id)\n"
    ")",
    "CREATE TABLE IF NOT EXISTS af_job_events (\n"
    "\tjob_id VARCHAR(36) NOT NULL, \n"
    "\tsequence INTEGER NOT NULL, \n"
    "\tstate VARCHAR(32) NOT NULL, \n"
    "\tstage VARCHAR(64), \n"
    "\tprogress FLOAT, \n"
    "\tdetail VARCHAR(256), \n"
    "\tcreated_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (job_id, sequence)\n"
    ")",
    "CREATE INDEX IF NOT EXISTS ix_af_job_events_created_at ON af_job_events (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_af_job_events_state ON af_job_events (state)",
    "CREATE TABLE IF NOT EXISTS af_worker_measurement_replays (\n"
    "\tissuer VARCHAR(127) NOT NULL, \n"
    "\tevidence_digest VARCHAR(64) NOT NULL, \n"
    "\texpires_at DATETIME NOT NULL, \n"
    "\tconsumed_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (issuer, evidence_digest)\n"
    ")",
    "CREATE INDEX IF NOT EXISTS ix_af_worker_measurement_replays_expires_at ON "
    "af_worker_measurement_replays (expires_at)",
    "CREATE TABLE IF NOT EXISTS af_program_job_executions (\n"
    "\tjob_id VARCHAR(36) NOT NULL, \n"
    "\tclaimed_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (job_id)\n"
    ")",
    "CREATE TABLE IF NOT EXISTS af_program_job_results (\n"
    "\tjob_id VARCHAR(36) NOT NULL, \n"
    "\tstate VARCHAR(32) NOT NULL, \n"
    "\texit_code INTEGER, \n"
    "\tstdout BLOB NOT NULL, \n"
    "\tstderr BLOB NOT NULL, \n"
    "\telapsed_seconds FLOAT NOT NULL, \n"
    "\tfinished_at DATETIME NOT NULL, \n"
    "\tPRIMARY KEY (job_id)\n"
    ")",
)

_PROBES = (
    (
        'PRAGMA table_info("af_tasks")',
        ("cid", "name", "type", "notnull", "dflt_value", "pk"),
        (
            (0, "id", "VARCHAR(36)", 1, None, 1),
            (1, "prompt", "TEXT", 1, None, 0),
            (2, "output", "TEXT", 1, None, 0),
            (3, "intent", "VARCHAR(128)", 1, None, 0),
            (4, "route", "VARCHAR(128)", 1, None, 0),
            (5, "tier", "VARCHAR(32)", 1, None, 0),
            (6, "status", "VARCHAR(32)", 1, None, 0),
            (7, "verified", "BOOLEAN", 1, None, 0),
            (8, "reused", "BOOLEAN", 1, None, 0),
            (9, "trace_id", "VARCHAR(36)", 1, None, 0),
            (10, "created_at", "DATETIME", 1, None, 0),
            (11, "updated_at", "DATETIME", 1, None, 0),
        ),
    ),
    (
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'af_tasks' AND sql IS "
        "NOT NULL ORDER BY name",
        ("name",),
        (("ix_af_tasks_trace_id",),),
    ),
    ('PRAGMA index_info("ix_af_tasks_trace_id")', ("seqno", "cid", "name"), ((0, 9, "trace_id"),)),
    (
        'PRAGMA table_info("af_run_traces")',
        ("cid", "name", "type", "notnull", "dflt_value", "pk"),
        (
            (0, "id", "VARCHAR(36)", 1, None, 1),
            (1, "prompt", "TEXT", 1, None, 0),
            (2, "intent", "VARCHAR(128)", 0, None, 0),
            (3, "arguments_json", "TEXT", 1, None, 0),
            (4, "route", "VARCHAR(128)", 0, None, 0),
            (5, "tier", "VARCHAR(32)", 0, None, 0),
            (6, "output", "TEXT", 0, None, 0),
            (7, "verified", "BOOLEAN", 1, None, 0),
            (8, "reused", "BOOLEAN", 1, None, 0),
            (9, "regression_passed", "BOOLEAN", 1, None, 0),
            (10, "promoted", "BOOLEAN", 1, None, 0),
            (11, "status", "VARCHAR(32)", 1, None, 0),
            (12, "events_json", "TEXT", 1, None, 0),
            (13, "created_at", "DATETIME", 1, None, 0),
        ),
    ),
    (
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'af_run_traces' "
        "AND sql "
        "IS NOT NULL ORDER BY name",
        ("name",),
        (),
    ),
    (
        'PRAGMA table_info("af_job_events")',
        ("cid", "name", "type", "notnull", "dflt_value", "pk"),
        (
            (0, "job_id", "VARCHAR(36)", 1, None, 1),
            (1, "sequence", "INTEGER", 1, None, 2),
            (2, "state", "VARCHAR(32)", 1, None, 0),
            (3, "stage", "VARCHAR(64)", 0, None, 0),
            (4, "progress", "FLOAT", 0, None, 0),
            (5, "detail", "VARCHAR(256)", 0, None, 0),
            (6, "created_at", "DATETIME", 1, None, 0),
        ),
    ),
    (
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'af_job_events' "
        "AND sql "
        "IS NOT NULL ORDER BY name",
        ("name",),
        (("ix_af_job_events_created_at",), ("ix_af_job_events_state",)),
    ),
    (
        'PRAGMA index_info("ix_af_job_events_created_at")',
        ("seqno", "cid", "name"),
        ((0, 6, "created_at"),),
    ),
    ('PRAGMA index_info("ix_af_job_events_state")', ("seqno", "cid", "name"), ((0, 2, "state"),)),
    (
        'PRAGMA table_info("af_worker_measurement_replays")',
        ("cid", "name", "type", "notnull", "dflt_value", "pk"),
        (
            (0, "issuer", "VARCHAR(127)", 1, None, 1),
            (1, "evidence_digest", "VARCHAR(64)", 1, None, 2),
            (2, "expires_at", "DATETIME", 1, None, 0),
            (3, "consumed_at", "DATETIME", 1, None, 0),
        ),
    ),
    (
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = "
        "'af_worker_measurement_replays' AND sql IS NOT NULL ORDER BY name",
        ("name",),
        (("ix_af_worker_measurement_replays_expires_at",),),
    ),
    (
        'PRAGMA index_info("ix_af_worker_measurement_replays_expires_at")',
        ("seqno", "cid", "name"),
        ((0, 2, "expires_at"),),
    ),
    (
        'PRAGMA table_info("af_program_job_executions")',
        ("cid", "name", "type", "notnull", "dflt_value", "pk"),
        ((0, "job_id", "VARCHAR(36)", 1, None, 1), (1, "claimed_at", "DATETIME", 1, None, 0)),
    ),
    (
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = "
        "'af_program_job_executions' AND sql IS NOT NULL ORDER BY name",
        ("name",),
        (),
    ),
    (
        'PRAGMA table_info("af_program_job_results")',
        ("cid", "name", "type", "notnull", "dflt_value", "pk"),
        (
            (0, "job_id", "VARCHAR(36)", 1, None, 1),
            (1, "state", "VARCHAR(32)", 1, None, 0),
            (2, "exit_code", "INTEGER", 0, None, 0),
            (3, "stdout", "BLOB", 1, None, 0),
            (4, "stderr", "BLOB", 1, None, 0),
            (5, "elapsed_seconds", "FLOAT", 1, None, 0),
            (6, "finished_at", "DATETIME", 1, None, 0),
        ),
    ),
    (
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = "
        "'af_program_job_results' AND sql IS NOT NULL ORDER BY name",
        ("name",),
        (),
    ),
)


def reference_migrations() -> tuple[Migration, ...]:
    return (
        Migration(
            "20260926000100_reference_schema_v1",
            up=tuple(DatabaseStatement(sql) for sql in _STATEMENTS),
            verify=tuple(
                MigrationProbe(
                    DatabaseStatement(sql, read_only=True),
                    tuple(DatabaseRow(columns, values) for values in rows),
                )
                for sql, columns, rows in _PROBES
            ),
        ),
    )
