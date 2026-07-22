# Private production MVP

## Status

**Not ready for production.** This document defines the first production profile and the
evidence required to change that status. It does not certify the current checkout, waive an
unmet gate, or replace the wider roadmap in issue
[#3](https://github.com/alexanderholman/AgentFramework/issues/3).

The machine-readable source of truth is
[`release/production-mvp.yaml`](../release/production-mvp.yaml). Its checker reports the
current gaps without executing commands from the manifest:

```bash
# [SCRIPT:release-gates] Validate the manifest itself.
python3 scripts/release_gates.py --action validate

# Report current status. A valid but blocked report exits zero.
python3 scripts/release_gates.py --action report
python3 scripts/release_gates.py --action report --json

# Enforce readiness. This exits one until every blocking gate is met.
python3 scripts/release_gates.py --action check

# Preview, then execute the temporary wheel-from-sdist package smoke.
python3 scripts/release_gates.py --action package
python3 scripts/release_gates.py --action package --execute
```

Malformed manifest input or an internal validation failure exits `2`. The package action
uses `1` for a failed build, inspection, installation, or smoke and `2` when it cannot access
its inputs. The checker never executes a command supplied by YAML. A `met` declaration means
the referenced implementation or test exists; final release still requires fresh commit-
and artifact-bound evidence.

## Why this profile is narrow

The complete v0.1 roadmap includes provider packs, deterministic science packs,
localization, peer-to-peer execution, broad driver support, and resource leasing. Those are
valuable platform goals, but making all of them prerequisites for the first operated AF
instance would hide the smaller set of controls needed to run the existing deterministic
vertical slice responsibly.

This profile therefore certifies only the application that exists today. It is separate
from cross-driver certification in issue
[#34](https://github.com/alexanderholman/AgentFramework/issues/34) and must not be used to
claim completion of the wider roadmap.

## Certified deployment shape

The first candidate production deployment is all of the following:

- one dedicated Linux host or virtual machine;
- CPython 3.12;
- one trusted operator and one dedicated, non-root operating-system account;
- one AF process and one worker for each SQLite database;
- an absolute SQLite path on a local filesystem outside the release directory;
- deterministic built-in routes only;
- no selector adapter, model execution, remote inference, planner, or generated answer;
- CLI and MCP stdio, with HTML, REST, GraphQL, OpenAPI, or Streamable HTTP bound literally
  to loopback when enabled;
- access through the local account, SSH/stdio, or a separately authenticated and tested
  proxy/tunnel; and
- immutable wheel installation with persistent state and configuration outside that
  installation.

Repository privacy, `allowed_hosts`, and `allowed_origins` are not authentication. If a
listener is reachable beyond loopback, the deployment is outside this profile until a
non-bypassable authenticated TLS boundary covers every enabled route.

## Explicit exclusions

The production-MVP claim excludes:

- public or LAN listeners without a separately reviewed authenticated boundary;
- multiple tenants, untrusted users, or hostile co-resident processes;
- multiple AF processes sharing one SQLite database;
- PostgreSQL, MySQL, queues, caches, search, graphs, workers, schedulers, and other planned
  drivers;
- model-backed selection, local or remote model serving, generative output, and planners;
- peer-to-peer execution, remote resource leases, arbitrary code, and hardware control;
- high availability, zero-downtime migration, clustered failover, and disaster recovery
  beyond the stated single-node RPO/RTO;
- public PyPI publication, public MCP exposure, and public package support guarantees; and
- claims of complete WCAG, security, or regulatory certification.

An excluded capability must be absent or unreachable, not merely undocumented.

## Existing foundations

The manifest currently recognizes these foundations without declaring the deployment ready:

- the locked Ruff, strict mypy, pytest/coverage, protocol, and governance source gate;
- deterministic selector-bypassing procedures with independent verification;
- shared application behavior across the existing interfaces;
- loopback network defaults and remote-inference denial;
- committed dependency locking; and
- SQLite Task persistence across application reconstruction.

These are implementation facts, not operational evidence. In particular, persistence across
restart is not a backup, `create_all()` is not a migration, an execution trace is not an
audit log, and a green source test run is not a production deployment test.

## Release-blocking work

### Artifact and supply chain

Before release:

1. Build one source distribution from the exact clean commit.
2. Build the wheel from that source distribution rather than from the checkout.
3. Verify project/runtime versions, console entry points, `py.typed`, templates, and the
   absence of repository/runtime state.
4. Install and smoke the wheel outside the checkout.
5. Repeat installation on a clean supported host with only declared runtime dependencies.
6. Scan the exact source and lock for vulnerable dependencies, secrets, and high-confidence
   static findings.
7. Record artifact SHA-256 values and preserve the accepted artifacts for rollback.
8. Move from `0.1.0a0` to `0.1.0` only in the final release-candidate PR.

The package action introduced with this profile covers steps 1 through 4. It deliberately
uses temporary paths and leaves clean-host installation, signing, publication, and final
versioning blocked.

### Security and privacy

Before release:

1. Review a threat model for hostile input, local callers, egress, secrets, supply chain,
   exhaustion, backup media, and rollback.
2. Prove either the dedicated-account loopback/stdio trust boundary or authenticated TLS
   proxy coverage for every enabled route, including mutation and documentation endpoints.
3. Keep all model and remote inference paths absent or denied.
4. Define which prompts, outputs, traces, errors, paths, identities, and database details are
   sensitive.
5. Test redaction and retention at every error, trace, log, database, and backup boundary.
6. Resolve all current high/critical security and actionable independent-review findings.

Direct public exposure is not an acceptable shortcut. Secure MCP Tunnel is an optional
deployment extension and needs its own configuration-bound evidence.

### Data durability and recovery

Before release:

1. Reject relative production database paths and paths inside an installation directory.
2. Verify owner, mode, local-filesystem placement, free space, and the one-process invariant.
3. Define SQLite durability, busy-timeout, journal, integrity, and supported-concurrency
   policy.
4. Introduce a versioned schema fingerprint and tested migration/compatibility policy before
   any schema-changing upgrade.
5. Create a quiesced backup outside the release directory and off the primary failure domain.
6. Restore representative records into an isolated path, run SQLite integrity checks, and
   verify them through AF.
7. Declare and measure recovery-point and recovery-time objectives.
8. Define retention and verified deletion for primary data, traces, logs, and backup copies.

A stopped-service backup is sufficient for this profile. Online backup and general
multi-version migration automation may follow, but the first restore must be demonstrated
before release.

### Operations

Before release:

1. Add a non-mutating production preflight (`af doctor` or an equivalent registered script)
   that fails closed on unsafe exposure, state paths, secrets, schema, permissions, and
   profile values.
2. Add distinct liveness and readiness checks that do not create Tasks or traces.
3. Provide one versioned non-root service definition with explicit environment, working
   directory, restart, timeout, resource, and state-directory policy.
4. Prove startup, SIGTERM, drain, resource disposal, crash restart, and partial-failure
   behavior within documented bounds.
5. Emit bounded structured operational logs with correlation, redaction, rotation, and local
   disk/error alerts.
6. Bound Task listing, request size, concurrency, database growth, and resource consumption.
7. Measure normal load, overload, sustained soak, recovery, and leak behavior for the
   single-process profile.
8. Retain the prior artifact/configuration and complete a timed rollback or restore drill.

Issue [#9](https://github.com/alexanderholman/AgentFramework/issues/9) and the following
application lifecycle work are prerequisites for honest drain and shutdown evidence. This
readiness profile does not duplicate that implementation.

### Accessibility

If HTML is enabled, create, list, read, update, delete, validation-error, and destructive
confirmation flows must have:

- no serious or critical automated accessibility finding;
- complete keyboard operation and visible focus;
- sensible focus placement after errors and navigation;
- zoom and reflow behavior without lost actions or content;
- screen-reader names, status announcements, and error association; and
- a recorded manual review against the applicable WCAG 2.2 AA criteria.

If that evidence is unavailable, HTML must be disabled and removed from the deployed-surface
claim rather than silently waived.

## Evidence contract

The manifest is versioned requirements data, not self-authenticating proof. A release
evidence bundle must bind every result to:

- the full Git commit SHA and clean-tree assertion;
- source-distribution and wheel SHA-256 values;
- the production-profile manifest digest;
- the effective non-secret configuration digest;
- operating-system and Python identities;
- exact checker/tool versions;
- test or review identity and independence class;
- UTC observation time, expiry, and any revocation state; and
- the relevant deployment or backup subject.

Missing, expired, manually edited, differently scoped, or mismatched evidence is `UNKNOWN`
and blocks release. CI configuration proves that a check is defined; only a current run bound
to the release candidate proves that it passed.

## Recommended implementation order

1. **Keep the release contract green:** validate this manifest and package artifacts in the
   normal source gate while the readiness check remains honestly blocked.
2. **Finish the application foundation:** complete issues #9, #10, and #14 for scopes,
   lifecycle, health, and observability.
3. **Close the data and identity boundary:** complete the profile-relevant portions of issues
   #17 and #31, including migrations, backup/restore, retention, and operator access.
4. **Add deployment evidence:** service definition, preflight, capacity limits, accessibility,
   clean-host install, rollback, and no-egress tests.
5. **Certify the release candidate:** run issue #34's applicable security/resilience gates,
   produce the immutable evidence bundle, resolve findings, and obtain explicit maintainer
   approval.
6. **Release separately:** bump to `0.1.0`, update the changelog, rebuild the exact artifacts,
   rerun every gate, and tag only the approved commit.

Broader drivers and peer/model capabilities should proceed after this baseline or under
separate profiles. They must not weaken the deterministic, loopback, evidence, or fail-closed
properties certified here.
