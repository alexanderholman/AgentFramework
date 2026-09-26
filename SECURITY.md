# Security Policy

## Supported versions

AgentFramework is pre-release software. Security fixes are applied to the latest commit
on the default branch; no older release line is currently supported.

| Version | Supported |
| --- | --- |
| `0.1.x` | Yes |
| `< 0.1` | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
vulnerability reporting feature for `alexanderholman/AgentFramework` when it is enabled,
or contact the repository owner privately through an already established channel.

Include the affected component, reproduction steps, expected impact, and any suggested
mitigation. Do not include real credentials, personal data, or destructive proof-of-
concept payloads. Receipt should be acknowledged within seven days; disclosure and fix
timing will be agreed according to severity and available capacity.

Until the documented authentication and authorization work is complete, the MCP HTTP
transport is for loopback development only and must not be exposed publicly.

The same restriction applies to HTML, REST, GraphQL, and OpenAPI. Repository privacy,
trusted-host checks, and allowed-origin checks do not authenticate a caller. See the
[private production-MVP profile](docs/production-mvp.md) for the dedicated-account
MCP-stdio trust boundary, one-database/one-process limit, prohibition on concurrent CLI access,
required deployment evidence, and release-blocking security, recovery, and operations controls.
Every AF network listener, including loopback HTTP and Secure MCP Tunnel, is outside that first
profile.

## Candidate security evidence

The registered [release security tools](docs/release-security.md) produce a complete
Python lock SBOM, pinned source/dependency scan reports and explicit-key artifact
integrity envelopes. Missing tools, findings and unavailable advisory checks block
those checks; the tools never create maintainer approvals or production
certification. Follow the documented private-file and backup-data rules when
retaining reports, account exports, snapshots and signing keys.
