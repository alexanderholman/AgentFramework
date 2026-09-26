# Release inventory, scans and artifact integrity

These repository tools implement local prerequisites for issues #3 and #34. They
never change readiness declarations, create approvals, publish, tag, or deploy.
The production manifest remains the release contract. A successful tool invocation
always reports `certifies_production: false`.

## Inventory and security checks

Install the locked optional tools with `uv sync --locked --extra dev --extra security`.
`security` pins pip-audit 2.10.1, Bandit 1.9.4 and detect-secrets 1.5.0. The runner
refuses other versions and never installs tools, resolves dependencies or applies
fixes. Use a clean committed checkout and an evidence directory outside it:

```bash
.venv/bin/python scripts/release_inventory.py --output /private/evidence/sbom.json --execute
.venv/bin/python scripts/release_security.py --output /private/evidence/security.json --execute
```

Without `--execute`, both commands preview their operation. Inventory is offline.
The SBOM uses CycloneDX 1.6 and includes **every Python lock resolution**, including
development tools, optional extras and other platforms. Each package has its exact
version/PURL and distribution references with SHA-256 values. This is a pre-build
inventory, not an assertion that all packages are installed on the target host.
It does not cover native operating-system libraries or optional JavaScript workers.
Local/VCS dependencies, missing distribution hashes and credential-bearing URLs
are rejected rather than silently omitted. An installed-host SBOM and applicable
native/JavaScript dependency audits remain separate profile evidence.

The security runner scans immutable Git blobs, including files marked
`export-ignore`. Links and submodules are rejected. Bandit examines `src` and
`scripts`, rejects parsing errors and ignores inline `nosec` suppressions. It blocks
on high-severity/high-confidence findings. detect-secrets examines the entire
committed tree, disables online secret verification and inline allowlist filtering,
and blocks on every unreviewed candidate. Reports retain locations, rule IDs and
secret fingerprints; source snippets and raw secrets are discarded. Reports are
exclusive new `0600` files; existing files are never overwritten.

`release/secret-reviews.json` records exact false-positive decisions for synthetic
fixtures, ephemeral localhost CI passwords, public signatures/checksums and public
format labels. Each review binds a filename, detector type, secret fingerprint and
SHA-256 of the complete source line, with a stated reason, reviewer and expiry of
at most 180 days. Source edits, unmatched entries, duplicate entries and expired
reviews fail closed. Public hash bytes use JSON byte arrays; the ledger itself is
still scanned. No folder, wildcard, inline, advisory or static-analysis exemption
is applied. Every reviewed candidate remains in the report. Current automated
source reviews are labeled as such and are not independent human security approval.

Dependency auditing requires the additional explicit flag:

```bash
.venv/bin/python scripts/release_security.py --output /private/evidence/security-online.json --allow-advisory-network --execute
```

This sends locked package names/versions to PyPI's advisory API. It uses an empty
cache, hashed exact requirements, `--no-deps`, `--disable-pip`, and strict collection.
Skipped/missing packages, malformed results, scanner failures, timeouts and missing
tools block the check. All reported dependency advisories block, irrespective of
severity. Without network authorization the dependency result is `unknown` and the
combined check exits 1. The runner has no automatic baseline, ignore list or risk
acceptance path. Findings require correction or a separately reviewed policy
change; test-fixture false positives are findings for review, not automatic passes.
Exit 0 means all three checks passed; 1 means blocked; 2 means invalid inputs or no
report could be issued. Each report binds the clean full commit, lock SHA-256,
exact scanner versions, time and host/Python identity. Scanner configuration is
not proof a candidate was scanned. A new vulnerability disclosure can invalidate
a prior clean report.

The pinned `security.yml` workflow performs these checks on push/PR and weekly,
retains redacted reports for 14 days and fails on findings or unavailable checks.
Its advisory-network flag is explicit. It does not have a signing credential.
Source scanners use process isolation and disabled network-verification options;
this is not an operating-system sandbox or proof of absence of vulnerabilities.

## Signing and verification

`release_signing.py` accepts an operator-provided Ed25519 PKCS#8 PEM private key.
It never generates a release key or certificate. The key and optional password file
must be owner-controlled regular files with no group/other permissions; symlinks
are rejected. Use a separately approved custody/rotation/revocation policy and
supply the trusted public key independently of the envelope.

A key-policy JSON object has exactly these fields:

- `schema`: `af-signing-key-policy/v1`;
- `key_id`: SHA-256 of the 32-byte raw Ed25519 public key;
- `valid_from`, `valid_until`: timezone-aware ISO dates;
- `revoked`: Boolean, which must be false;
- `owner`, `review_reference`: nonempty references to the real key owner and review.

The tool checks identity, time and revocation, but cannot authenticate a reviewer
from a string. Do not create a fictional review reference to make it pass. Keeping
this policy current and distributing trusted keys are operator responsibilities.

After building and inspecting the exact candidate with the package gate, pass
all material inputs explicitly:

```bash
.venv/bin/python scripts/release_signing.py sign --root . \
  --sdist /private/evidence/candidate.tar.gz --wheel /private/evidence/candidate.whl \
  --sbom /private/evidence/sbom.json --scan /private/evidence/security-online.json \
  --profile release/production-mvp.yaml --configuration /private/evidence/nonsecret-config.json \
  --key-policy /private/keys/policy.json --key /private/keys/release.pem \
  --envelope /private/evidence/provenance.json --execute
```

For encrypted PEM keys add `--password-file /private/keys/password`; the password
never appears in arguments or stdout. Sign previews omit `--execute`. Verification
uses the same material arguments, action `verify` and the independently trusted
public PEM key in `--key`; it does not require `--execute`.

The domain-separated custom AF envelope binds artifact names and SHA-256 values,
clean full source commit, SBOM, fresh passing security report, lock, project metadata,
profile, non-secret configuration, key policy, builder identity and expiry. The
security report must match the source/lock/pins and be no more than 24 hours old.
Statements default to 24 hours and cannot exceed seven days. Verification fails on
wrong keys, tampering, changed/missing materials, expired/revoked policies, expired
statements or a different source commit. This is artifact integrity tooling, not an
in-toto/SLSA certification, transparency log, authorization to release, or proof that
claimed build inputs actually produced the artifacts. Preserve build gate evidence
and obtain independent review and real maintainer approval for the exact signed
candidate. Rebuilding requires new digests, scans, signatures and approval.

## Sensitive release and backup files

Keep release keys, account credential exports, account requests containing passwords,
local notification exports, database files and key sidecars out of source archives
and public CI artifacts. Evidence reports omit key/credential contents, but package
names, source paths and finding locations can still be sensitive. Review the
non-secret configuration file before signing it; only its digest is recorded.

SQLite snapshots include account identifiers, password/token hashes, passkey public
credentials, encrypted TOTP/outbox data and other durable application records. A
private `0600` snapshot and authenticated manifest do **not** encrypt the entire
database. Protect backup media and the separately retained `.workflow-key`; that key
also protects authenticated workflow records and derived account encryption. Test
recovery with both, enforce separate backup retention, and retire obsolete copies
through the storage policy. Live-database deletion cannot erase older snapshots.
See [data lifecycle](data-lifecycle.md) for the actual backup/restore commands.

Target-host account permissions, no listeners/egress, filesystem/encryption policy,
clean installation, service supervision, backup recovery and rollback timings,
real-driver failures, accessibility review and maintainer signoff remain distinct
evidence. None is satisfied by these scripts or their fixture tests.

Primary tool contracts: [CycloneDX schema](https://github.com/CycloneDX/specification/blob/1.6/schema/bom-1.6.schema.json),
[pip-audit](https://github.com/pypa/pip-audit),
[Bandit](https://bandit.readthedocs.io/en/latest/man/bandit.html),
and [detect-secrets](https://github.com/Yelp/detect-secrets).
