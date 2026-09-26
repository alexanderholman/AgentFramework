# Local machine initialization

`af init` offers a local setup wizard. `af init --plan` performs read-only discovery and prints the
versioned plan without booting an AF application, creating directories, starting services or making
network calls. Explicit automation uses `af init --non-interactive --apply --yes`; without `--apply`,
non-interactive initialization prints a plan only. Interactive confirmation defaults to no.

```bash
af init --plan --profile scheduler --state-dir storage
af init --non-interactive --apply --yes --profile scheduler --state-dir storage
af status --state-dir storage
af machine export --state-dir storage > machine-declaration.json
af init --config machine-declaration.json --state-dir another-machine --non-interactive --apply --yes
```

Profiles are independently versioned as `1.0.0` and expand into these explicit dependencies:

| Profile | Enabled local capabilities |
| --- | --- |
| core | Deterministic procedures and their migrated SQLite state |
| database | Core and local SQLite database capability |
| queue | Database and durable SQLite queue |
| cache | Database and SQLite cache/owner locks |
| scheduler | Queue, cache and durable scheduler history/configuration |

The installed Python/SQLite components are reused. No external database package or model runtime is
claimed to be installed. The existing MariaDB provisioning path remains the separate, explicit
`af db setup` operation. These profiles have no downloads, network egress, listeners, public ports,
boot services or background workers. Queue/cache managers are available on demand in the configured
application. Workers requiring a measured optimization profile remain a separate explicit setup.
Schedulers start with an empty definition file unless an existing valid file is supplied.

Discovery reports OS, architecture, CPU count, RAM where available, free storage, and bounded Linux
GPU device metadata where available. GPU qualification and VRAM eligibility for inference remain
unknown. Existing engine executable names are observed without running them. Model and engine
inventories use only explicit `--model-path` and `--engine-path` values; there are no incidental model
store scans. A recognized file extension or executable is labelled discovered/unverified and never
becomes an authorized model route. Missing paths, unsupported profiles, unsafe managed symlinks,
unrecognized databases and insufficient planning resources block application before provisioning.
Model inference, arbitrary remote routes and Windows host setup are currently unsupported here.

The plan lists data locations, dependency order, zero downloads/ports, the configured memory admission
budget and disk reserve. The memory value is a planning admission limit, not an OS memory quota.
Application resource leases and separately installed model runtimes retain their own execution limits.

Apply takes one exclusive local setup lock, migrates the real reference SQLite schema, probes queue
publication/reservation/acknowledgement and cache write/read/delete when selected, validates scheduler
configuration, and runs the deterministic strawberry request. Verification must return `3`, pass its
verifier, and record zero selector calls with provider routes disabled. Failed readiness does not
publish a completed declaration. Receipts contain completed/pending steps and retained-data recovery
instructions; they do not contain secret material. The private workflow key is represented only by a
local secret reference.

`machine.json` is the declarative configuration and `setup.sqlite3` holds durable setup receipts.
`af.sqlite3` retains core and optional backend data. Interrupted setup is recorded on the next locked
apply, which reconciles existing schema/state and verifies it again. Existing models, user tables,
configuration and data are never deleted because a profile no longer lists them. This is compensating
recovery across files and SQLite databases, not a distributed transaction. A failed setup can leave
unpublished local schema or diagnostic rows for the next reconciliation.

```bash
af database enable --plan
af queue enable --yes
af cache status
af scheduler disable --yes
af queue disable --yes
```

Enable shows a concrete plan and requires confirmation. Disable changes future configured use and
retains every data file; it refuses when an enabled capability still depends on the requested one.
It does not stop externally managed processes. `af status` and `af machine status` read the declaration,
local schema and recent receipts without mutating them. Degraded/misconfigured output includes a
repair instruction to review `af init --plan`.

For an explicitly selected setup directory, set `AF_MACHINE_ROOT` to it and use the installed factory
`af.bootstrap_setup:create_machine_application` via `--application-factory`. This factory loads the
managed declaration, binds owned queue/cache managers, and keeps remote inference and decisions off
regardless of unrelated remote-provider environment defaults. The default directory is `storage`.

Tests cover real SQLite provisioning, offline verification, repeat application, process-restart
receipts, interruption, retained user data, schema drift, competing setup ownership, declaration
reproduction, resource/path denials, interactive confirmation and independent capability lifecycle.
This finite reference setup does not claim physical GPU, native inference-engine, remote-service or
complete multi-platform host certification.
