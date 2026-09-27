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
python3 scripts/af_dev.py --action release-validate --execute

# Report current status. A valid but blocked report exits zero.
python3 scripts/af_dev.py --action release-report --execute
python3 scripts/af_dev.py --action release-report-json --execute

# Check declaration completeness. This does not verify evidence or authorize production.
python3 scripts/af_dev.py --action release-check --execute

# Preview, then execute the temporary wheel-from-sdist package smoke.
python3 scripts/af_dev.py --action package
python3 scripts/af_dev.py --action package --execute
```

Malformed manifest input or an internal validation failure exits `2`. The package action
uses `1` for a failed build, inspection, installation, or smoke and `2` when it cannot access
its inputs. The checker never executes a command supplied by YAML. It pins the canonical
profile and gate policy by digest so gates cannot be removed, demoted, or widened silently.
A `met` declaration means the manifest points to claimed evidence; even `DECLARATIONS
COMPLETE` neither verifies that evidence nor approves deployment. Final release still requires fresh commit-, artifact-,
configuration-, and deployment-bound evidence plus explicit maintainer authorization.

## Why this profile is narrow

The complete v0.1 roadmap includes provider packs, deterministic science packs,
localization, peer-to-peer execution, broad driver support, and resource leasing. Those are
valuable platform goals, but making all of them prerequisites for the first operated AF
instance would hide the smaller set of controls needed to run the existing deterministic
vertical slice responsibly.

This profile therefore covers only the application that exists today. It is separate
from cross-driver certification in issue
[#34](https://github.com/alexanderholman/AgentFramework/issues/34) and must not be used to
claim completion of the wider roadmap.

## Target deployment shape

The first candidate production deployment is all of the following:

- one dedicated Linux host or virtual machine;
- CPython 3.12;
- one trusted operator and one dedicated, non-root operating-system account;
- one SQLite database and one supervised AF MCP-stdio process in total;
- an absolute SQLite path on a local filesystem outside the release directory;
- deterministic built-in routes only;
- no selector adapter, model execution, remote inference, planner, or generated answer;
- MCP stdio as the only live application surface;
- CLI maintenance only while the supervised process is stopped and the database is
  quiesced;
- no HTML, REST, GraphQL, OpenAPI, Streamable HTTP, Secure MCP Tunnel, or other AF network
  listener;
- access through the dedicated local account and its restricted stdio/IPC boundary; and
- immutable wheel installation with persistent state and configuration outside that
  installation.

Repository privacy, `allowed_hosts`, and `allowed_origins` are not authentication. Any AF
network listener or concurrent CLI/database process places a deployment outside this
profile and must fail production preflight.

## Explicit exclusions

The production-MVP claim excludes:

- HTML, REST, GraphQL, OpenAPI, MCP Streamable HTTP, Secure MCP Tunnel, and every public,
  LAN, or loopback AF listener;
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
3. Verify source and wheel identity, runtime dependency metadata, console targets, complete
   `RECORD` hashes, `WHEEL`, `py.typed`, templates, and the absence of foreign/runtime state.
4. Install and smoke the wheel outside the checkout.
5. Repeat installation on a clean supported host with only declared runtime dependencies.
6. Scan the exact source and lock for vulnerable dependencies, secrets, and high-confidence
   static findings.
7. Record artifact SHA-256 values, produce provenance/signatures under an approved key
   policy, and preserve accepted artifacts for rollback.
8. Keep `0.1.0a0` until roadmap issue #3 authorizes one exact candidate version; rebuilding
   that version invalidates earlier artifact evidence and must precede final approval.

The package action introduced with this profile covers steps 1 through 4. It builds from a
clean commit-derived snapshot, uses an uncredentialed temporary home, disables inherited uv
configuration, keeps uv resolution offline, and uses temporary paths. It exports runtime-only
requirements from the exact lock and installs hash-verified wheels from the primed uv cache
with source builds disabled. Python `-I -S` smoke checks use only that temporary target, without
development site packages. This is a runtime-only installation on the current host. Those controls do not
create an operating-system network sandbox for the backend or installed code; the separate
no-egress gate remains unmet. Clean-host installation, signatures/provenance, deployment
evidence, publication, and final version authorization also remain blocked.

### Security and privacy

Before release:

1. Review a threat model for hostile input, local callers, egress, secrets, supply chain,
   exhaustion, backup media, and rollback.
2. Prove the dedicated-account MCP-stdio boundary, restrictive local process/IPC access,
   quiesced CLI maintenance, and absence of every AF listener.
3. Keep all model and remote inference paths absent or denied.
4. Define which prompts, outputs, traces, errors, paths, identities, and database details are
   sensitive.
5. Test redaction and retention at every error, trace, log, database, and backup boundary.
6. Resolve all current high/critical security and actionable independent-review findings.

Direct exposure and Secure MCP Tunnel are outside this profile. Either requires a separate
canonical profile and its own authentication, transport, configuration, and deployment
evidence.

### Data durability and recovery

Before release:

1. Reject relative production database paths and paths inside an installation directory.
2. Verify owner, mode, local-filesystem placement, free space, and the one-process invariant.
3. Define SQLite durability, busy-timeout, journal, integrity, and supported-concurrency
   policy.
4. Introduce a versioned schema fingerprint and tested migration/compatibility policy before
   any schema-changing upgrade.
5. Stop AF, checkpoint WAL, capture the database and required sidecars atomically, record
   checksums/permissions, fsync the copy, and place it off the primary failure domain.
6. Restore representative records into an isolated path, validate backup checksums, run
   SQLite integrity checks, and verify them through AF.
7. Declare and measure recovery-point and recovery-time objectives.
8. Define retention and verified deletion for primary data, traces, logs, SQLite
   freelist/journal/WAL remnants, and backup copies.

A stopped-service backup is sufficient for this profile. Online backup and general
multi-version migration automation may follow, but the first restore must be demonstrated
before release.

### Operations

Before release:

1. Add a non-mutating production preflight (`af doctor` or an equivalent registered script)
   that fails closed on unsafe exposure, state paths, secrets, schema, permissions, and
   profile values.
2. Add distinct liveness and readiness checks that do not create Tasks or traces.
3. Provide one versioned non-root MCP-stdio service definition with explicit environment,
   IPC, working directory, restart, timeout, resource, and state-directory policy.
4. Prove startup, SIGTERM, drain, resource disposal, crash restart, and partial-failure
   behavior within documented bounds.
5. Emit bounded structured operational logs with correlation, redaction, rotation, and local
   disk/error alerts.
6. Enforce hard default/maximum Task list sizes, request/time/concurrency limits, database
   growth thresholds, and resource limits; a documented cap is not sufficient.
7. Measure normal load, overload, sustained soak, recovery, and leak behavior for the
   single-process profile.
8. Retain the prior signed artifact/configuration, prove schema compatibility, and complete
   separate timed artifact-rollback and data-restore drills.

The scoped-lifetime foundation completed in issue
[#9](https://github.com/alexanderholman/AgentFramework/issues/9) and the remaining application
lifecycle work are prerequisites for honest drain and shutdown evidence. This readiness
profile does not duplicate that implementation.

### Accessibility outside this profile

The source repository retains and tests HTML behavior, but this MCP-stdio deployment profile
does not start the HTTP application and makes no HTML accessibility certification. Any future
profile that enables HTML must add blocking automated and manual WCAG 2.2 AA evidence for
keyboard, focus, reflow/zoom, screen-reader naming/status/error behavior, and destructive
flows before admission.

## Evidence contract

The manifest is versioned requirements data, not self-authenticating proof. The checker can
prove that the canonical policy is intact, bind output to the complete manifest digest, and
report whether its declarations are complete; it does not validate the truth or freshness of
referenced evidence and always reports `certifies_production: false`. A release
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
and blocks external release admission even when the implementation manifest says `met`. CI
configuration proves that a check is defined; only a current run bound to the release
candidate proves that it passed.

## Recommended implementation order

1. **Keep the release contract green:** validate this manifest and package artifacts in the
   normal source gate while the readiness check remains honestly blocked.
2. **Finish the application foundation:** build on completed issue #9 and complete issues #10
   and #14 for lifecycle, health, and observability.
3. **Close the data and identity boundary:** complete the profile-relevant portions of issues
   #17 and #31, including migrations, backup/restore, retention, and operator access.
4. **Complete candidate prerequisites:** service definition, preflight, hard capacity limits,
   clean-host install, signing policy, rollback, recovery, and no-egress tests.
5. **Create the authorized candidate:** let roadmap issue #3 select the exact version, update
   metadata/changelog, and build/sign that clean commit. Earlier artifact evidence is stale.
6. **Certify that exact candidate:** run issue #34's applicable security/resilience and
   target-host deployment/recovery gates, produce the immutable evidence bundle, resolve
   findings, and obtain maintainer approval bound to its signed digest.
7. **Tag without rebuilding:** tag only the approved commit/artifact identity; any change
   restarts candidate evidence and approval.

Broader drivers and peer/model capabilities should proceed after this baseline or under
separate profiles. They must not weaken the deterministic, stdio-only, evidence, or fail-closed
properties required here.

## Local security tooling added after the initial profile

[Release security tooling](release-security.md) now supplies an offline complete
Python lock SBOM, pinned Bandit/detect-secrets/pip-audit runner, and explicit-key
Ed25519 artifact statement signing/verification. Tool availability does not change
manifest declarations to `met`: current candidate scans, reviewed key policy,
independent review, target-host evidence and final maintainer approval are still
required. Missing prerequisites and findings block; no approval is synthesized.
