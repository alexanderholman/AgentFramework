# Source-controlled schedules

The scheduler admits installed tasks to a durable queue. Schedule files contain
no shell commands, Python, imports, or executable expressions. The JSON schema
is `af.schedules/1`:

```json
{
  "schema": "af.schedules/1",
  "schedules": [
    {
      "id": "daily-check",
      "task_id": "task.create",
      "arguments": {"prompt": "math add a=1/3 b=1/6"},
      "starts_at": "2026-09-26T00:00:00Z",
      "rule": {
        "kind": "cron",
        "expression": "0 9 * * 1-5",
        "timezone": "Europe/London",
        "fold_policy": "first"
      },
      "misfire": "coalesce",
      "grace_seconds": 5,
      "jitter_seconds": 10,
      "max_catch_up": 32,
      "one_server": true,
      "without_overlap": true,
      "max_runtime_seconds": 300,
      "max_queue_wait_seconds": 300,
      "version": "1.0.0"
    }
  ]
}
```

An interval rule is `{"kind":"interval","seconds":60,"origin":"2026-09-26T00:00:00Z"}`.
`load_schedule_definitions(Path)` validates the bounded file before constructing
immutable definitions. Files are limited to 1 MiB and 1,000 schedules; duplicate
keys/IDs, unknown fields, mistyped values and executable fields fail closed.
Each definition has a digest covering the installed task identity, arguments,
temporal rules and admission policies.

Cron has five numeric fields, lists, ranges, `*`, and steps. Weekdays use
0/7=Sunday. Restricted day-of-month and weekday fields use OR semantics;
a wildcard day field combines with the other field. UTC minute enumeration
skips nonexistent local times. Repeated civil times use explicit `first`, `second`,
or `both` fold policy. Date searches are bounded to 366 days; schedules without
an occurrence inside that horizon report an error. Interval arithmetic preserves
its source-controlled origin. Datetimes must be timezone aware.

Jitter is reproducible from schedule ID and nominal occurrence, from zero through
`jitter_seconds - 1` seconds. It must be below 60 seconds and below an interval's
length. `skip` records a missed occurrence without dispatch; `coalesce` admits the
latest missed occurrence; `catch_up` admits at most the configured latest 1–32
occurrences in chronological order. Maintenance records due work as skipped.
Dry-run computes the same plan without changing history, locks or the queue.

The command contributor exposes:

```sh
af schedule list
af schedule next-due daily-check
af schedule dry-run
af schedule tick
af schedule work-once
af schedule history
```

`register_scheduling_commands(cli, factory)` creates no runtime resources while
showing help. The factory supplies `SchedulingCommands(service, history, work_once)`;
its optional worker callback returns the processed run ID or null. The integrated
host supplies configuration, cleanup and the installed `task.create` binding.

`SchedulerService` receives definitions, history, locks, a durable queue adapter,
clock, instance identity and optional observational hooks. `DurableSchedulerQueue`
requires verified reservation and renewal support, so an inline/sync queue cannot
run scheduled work during dispatch. The existing database queue does not claim
native uniqueness or per-job timeout capabilities; scheduler occurrence history
and execution deadlines enforce those boundaries without requesting unsupported
queue features.

`SQLiteScheduleHistory` uses atomic occurrence claims and compare-and-set lifecycle
transitions. Instances sharing that history cannot admit the same occurrence twice.
`one_server=false` gives each configured instance its own occurrence identity.
Execution failures are terminal for that occurrence; known admission failures may
be retried. `CacheSchedulerLocks` wraps the existing fenced CacheManager lock API.
`without_overlap` retains the schedule lock across queue waiting and execution;
an enqueue acknowledgement does not release it. Late queued deliveries fail lease
renewal and cannot run under a successor's lease.

`ScheduledJobHandler` only invokes installed task callbacks using arguments from
the trusted definition, validates the definition digest, records running/terminal
state, renews the lock, passes cancellation/deadline/fencing context, and releases
its own lease on settlement. Duplicate deliveries of settled/running occurrences
are ignored. Unknown or tampered job envelopes do not select another task.
Use the existing `WorkerKernel.run_once(handler, lane="scheduler")` for queue
reservation renewal and acknowledgement. `shutdown()` stops scheduler admissions;
worker drain/cancellation belongs to the worker lifecycle.

The local durable profile uses shared SQLite history plus a lock-capable database
cache and durable queue. Memory locks are appropriate for isolated tests only.
Multiple hosts require shared coordination backends; copying independent local
SQLite files does not establish one-server semantics. Installed callbacks must
honor cancellation and use the supplied fencing token for externally visible
transactions. Lease expiry cannot preempt arbitrary noncooperative code, and the
scheduler does not claim exactly-once external business effects. Observation hooks
cannot turn a completed enqueue into a replay.

Tests: `tests/unit/test_scheduling.py` covers fake-clock due calculations, DST,
misfires, bounded input, concurrent claims, failed admission, cancellation, stale
leases and tampering. `tests/feature/test_scheduling_journey.py` runs CLI planning,
actual SQLite queue dispatch, an installed worker through WorkerKernel, queue
acknowledgement and durable history, and checks lazy help.
