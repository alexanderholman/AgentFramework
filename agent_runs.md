# Agent Runs Log

**NOTE: This file is append-only. Do not modify or remove existing entries. Only add new run logs at the end.**

---

## Log Entry Format

Each agent run entry MUST follow this format:

```
## Run #XXX - [Agent-ID] Agent Name
**Date**: YYYY-MM-DD HH:MM:SS UTC
**Status**: Success | Failure | Partial | Aborted
**Duration**: XXm XXs
**Executor**: User/System identifier

### Inputs
- Input parameter 1: value
- Input parameter 2: value

### Outputs
- Output 1: description/value
- Output 2: description/value

### Result Summary
Brief description of what happened during the run.

### Issues Encountered
- Issue 1: description (if any)
- Issue 2: description (if any)

### Actions Taken
- Action 1: description
- Action 2: description

### Related References
- Decision: DEC-XXX
- Spec: SPEC-XXX
- Commit: [commit-hash]

---
```

## Purpose

This log maintains a historical record of all agent executions in the Agent Factory. This helps with:
- Debugging and troubleshooting
- Performance tracking
- Audit trail
- Learning from past runs

## Initial Entry

---

## Run #001 - System Initialization
**Date**: 2026-01-28 17:54:00 UTC
**Status**: Success
**Duration**: 0m 0s
**Executor**: System

### Inputs
- Action: Initialize Agent Factory structure
- Configuration: Default settings

### Outputs
- Created agents.yaml configuration file
- Created agents.md documentation file
- Created specs.md specifications file
- Created agent_runs.md log file (this file)
- Created decisions.md decisions file

### Result Summary
Successfully initialized the Agent Factory repository structure with all required files and documentation.

### Issues Encountered
None

### Actions Taken
- Created base configuration files
- Established documentation structure
- Defined validation rules and tests
- Set up append-only file system

### Related References
- Spec: SPEC-001 (File Structure)
- Spec: SPEC-002 (Agent File Format)
- Spec: SPEC-003 (Tags and Metadata)
- Spec: SPEC-004 (Append-Only Files)
- Spec: SPEC-005 (No Fabrication)
- Spec: SPEC-006 (Markdown Preference)

---

## Run #002 - Agent Analysis and Recommendations
**Date**: 2026-01-28 19:48:00 UTC
**Status**: Success
**Duration**: 15m 30s
**Executor**: GitHub Copilot Agent

### Inputs
- Action: Analyze existing agents and recommend new agents and specialisms
- Configuration: Review all existing agent definitions, specialisms, and repository structure
- Context: 7 existing agents, 3 existing specialisms

### Outputs
- Created agent_recommendations.md with comprehensive analysis
- Identified 7 recommended new agents (3 high priority, 4 medium, 1 low)
- Identified 7 recommended new specialisms (2 high priority, 3 medium, 2 low)
- Analyzed gaps in current agent coverage
- Proposed phased implementation plan

### Result Summary
Successfully analyzed the existing agent system and produced comprehensive recommendations for new agents and specialisms. Analysis identified key gaps in testing, security, deployment, documentation, integration, data modeling, and performance optimization. Recommendations are prioritized and include implementation guidance.

### Issues Encountered
None

### Actions Taken
- Reviewed all 7 existing agent definitions in agents/ directory
- Reviewed all 3 existing specialisms in specialisms/ directory
- Analyzed agents.yaml configuration and validation rules
- Reviewed specs.md for technical requirements
- Reviewed decisions.md for design rationale
- Identified gaps in lifecycle coverage
- Developed prioritized recommendations
- Created comprehensive recommendations document
- Proposed integration with existing workflow
- Defined success metrics

### Recommendations Summary
**High Priority:**
- Tester Agent (Test Creator + Quality Validator)
- SecurityReviewer Agent (Security Analyst + Compliance Auditor)
- Security Specialism
- Testing Specialism

**Medium Priority:**
- Deployer Agent (Deployment Engineer + Operations Specialist)
- DocWriter Agent (Technical Writer + UX Documentation Specialist)
- Integrator Agent (Integration Architect + API Designer)
- DataModeler Agent (Data Architect + Schema Designer)
- API Design Specialism
- Deployment Specialism
- Documentation Specialism

**Low Priority:**
- Optimizer Agent (Performance Engineer + Efficiency Analyst)
- Data Specialism
- Performance Specialism

### Related References
- Output: agent_recommendations.md
- Spec: SPEC-001 (File Structure)
- Spec: SPEC-002 (Agent File Format)
- Spec: SPEC-003 (Tags and Metadata)
- Decision: DEC-009 (Agent Analysis Recommendations - see decisions.md)

---

## Run #003 - Phase 1 Agent Implementation
**Date**: 2026-01-29 01:32:00 UTC
**Status**: Success
**Duration**: 25m 15s
**Executor**: GitHub Copilot Agent

### Inputs
- Action: Implement Phase 1 high-priority agent recommendations
- Configuration: Based on agent_recommendations.md Phase 1 specifications
- Context: 7 existing agents, 3 existing specialisms

### Outputs
- Created agents/Tester.md (6,408 bytes, 217 lines)
- Created agents/SecurityReviewer.md (7,290 bytes, 239 lines)
- Created specialisms/Testing.md (3,888 bytes, 147 lines)
- Created specialisms/Security.md (5,957 bytes, 221 lines)
- Updated agents.yaml with 2 new agent entries
- Added 2 new tags to allowed_tags: security, quality

### Result Summary
Successfully implemented Phase 1 high-priority recommendations by creating Tester and SecurityReviewer agents with their supporting specialisms. Both agents follow the required structure with all five headings (Purpose, Inputs, Outputs, Behavior, Constraints). New agents integrated into agents.yaml with unique IDs and appropriate tags.

### Issues Encountered
- Validation script TEST-003-2 has a bug: uses `awk '{print $2}'` which extracts "id:" literal instead of actual ID values (should use $3)
- Python YAML validation confirms all agent IDs are unique
- All agent files pass required heading validation
- All tags are from allowed list
- Flat file structure maintained

### Actions Taken
- Designed Tester agent based on Testing specialism standards
  - Comprehensive test planning and execution
  - Coverage analysis and quality metrics
  - Unit, integration, and acceptance testing
- Designed SecurityReviewer agent based on Security specialism standards
  - OWASP Top 10 compliance checking
  - Vulnerability assessment with severity classification
  - Security best practices enforcement
- Created Testing specialism defining:
  - Test types and naming conventions
  - Quality gates for testing
  - Coverage metrics
  - AAA pattern and test independence
- Created Security specialism defining:
  - Security principles (least privilege, defense in depth)
  - OWASP Top 10 checklist
  - Secure coding practices
  - Severity classification
- Added new tags: security, quality
- Updated agents.yaml with proper YAML formatting
- Validated with Python YAML parser

### Quality Metrics
- Agent files: 2 created
- Specialism files: 2 created
- Total lines added: ~824 lines
- Validation: 25/26 tests pass (1 known script bug)
- Python validation: All checks pass
- Required headings: All present in correct order
- File structure: Flat (no nested directories)
- Tag compliance: All tags from allowed list

### Related References
- Recommendation: agent_recommendations.md (Phase 1)
- Decision: DEC-009 (Agent Expansion Strategy)
- Previous Run: Run #002 (Recommendations Analysis)
- Spec: SPEC-001 (File Structure) - maintained
- Spec: SPEC-002 (Agent File Format) - followed
- Spec: SPEC-003 (Tags and Metadata) - compliant

---

## Run #004 - Add AGENTS.md for OpenAI ChatGPT Compatibility
**Date**: 2026-01-29 01:44:00 UTC
**Status**: Success
**Duration**: 8m 30s
**Executor**: GitHub Copilot Agent

### Inputs
- Action: Review and align with OpenAI ChatGPT agents.md standard
- Reference: https://github.com/agentsmd/agents.md
- Context: User requested alignment with OpenAI AGENTS.md format

### Outputs
- Created AGENTS.md (5,694 bytes, 202 lines)
- Added DEC-012 to decisions.md
- Added Run #004 to agent_runs.md

### Result Summary
Successfully created AGENTS.md file following the OpenAI ChatGPT agents.md standard. The file provides simple, practical instructions for AI agents working on the AgentFactory project, complementing the existing comprehensive documentation in .github/copilot-instructions.md.

### Key Features of AGENTS.md
- **Project Overview**: Brief description of AgentFactory
- **Key Files Structure**: Quick reference to important files
- **Development Workflow**: Step-by-step agent creation process
- **Coding Conventions**: Style guidelines for markdown, YAML, append-only files
- **Testing**: Validation commands and checks
- **Common Tasks**: Frequently used commands and operations
- **Important Rules**: Clear MUST/MUST NOT lists
- **Directory Structure**: Examples of flat and nested structures
- **Troubleshooting**: Solutions to common issues
- **Platform Compatibility**: List of supported AI platforms

### Platform Compatibility
The AGENTS.md format is recognized by:
- OpenAI ChatGPT (primary target)
- GitHub Copilot (reads AGENTS.md as fallback)
- Google Gemini
- Google Colab
- Agent-based IDEs (OpenCode.ai)
- Any AI agent following the agents.md convention

### Documentation Strategy
The project now has layered documentation:
1. **AGENTS.md** - Simple, practical quick reference (NEW)
2. **.github/copilot-instructions.md** - Comprehensive GitHub Copilot guide
3. **agents.md** - Agent definition template and guidelines
4. **README.md** - Human-readable project overview
5. **specs.md** - Technical specifications (append-only)
6. **decisions.md** - Architectural decisions (append-only)

Each serves a different purpose and audience, with minimal duplication.

### Design Choices
- **Practical over Comprehensive**: Focus on common tasks
- **Code Examples**: Include actual commands to run
- **Quick Reference**: Easy to scan and find information
- **Troubleshooting Section**: Address known issues upfront
- **Platform-Agnostic**: Language works for any AI agent

### Issues Encountered
None

### Actions Taken
- Reviewed OpenAI agents.md standard and examples
- Created AGENTS.md following the format
- Structured content for AI agent consumption
- Included practical examples and commands
- Documented common workflows
- Added troubleshooting section
- Listed platform compatibility
- Documented decision in DEC-012
- Added this run log entry

### Quality Metrics
- File created: 1
- Lines added: 202
- Size: 5.7 KB
- Sections: 11 main sections
- Code examples: Multiple bash and Python snippets
- Validation: File follows standard markdown format

### Related References
- Standard: https://github.com/agentsmd/agents.md
- Decision: DEC-012 (AGENTS.md Addition)
- Previous Run: Run #003 (Phase 1 Implementation)
- Related Decision: DEC-011 (Flexible Directory Structure)

---

## Run #005 - Implement the AF Python Vertical Slice
**Date**: 2026-07-18
**Status**: Success (local implementation); publication pending
**Duration**: Not recorded
**Executor**: Codex with delegated architecture, MCP, Copilot, and acceptance-test agents

### Inputs
- Build a Laravel-inspired but idiomatic Python AF framework.
- Demonstrate Hello World and `how many rs in strawberry` without model inference.
- Use TDD, DRY boundaries, and MVC.
- Expose CLI, HTML, REST, GraphQL CRUD, and ChatGPT-compatible skill/MCP surfaces.
- Prepare the repository for GitHub Copilot cloud-agent work.

### Outputs
- Implemented typed framework, domain, application, infrastructure, and interface layers.
- Added static, procedural, and explicitly installed generative routing seams.
- Added Task CRUD with in-memory and SQLAlchemy/SQLite repositories.
- Added Typer CLI, Jinja MVC, REST, Strawberry GraphQL, and FastMCP adapters.
- Added repository skill, ChatGPT/MCP guide, architecture guide, Copilot instructions,
  setup workflow, and Python CI.
- Added `[SCRIPT:af-dev]` as the repeatable dependency, smoke, test, coverage, format,
  lint, type, and legacy-validation runner.
- Opened GitHub issue #14 for the vertical slice.

### Test-First Evidence
- Initial Python acceptance collection failed with `ModuleNotFoundError: af` before the
  package was implemented.
- Model-provider tests failed before provider/verifier pairing and generative routing were
  implemented.
- MCP annotation and repeatable-reference boundary tests failed before their fixes.
- The coverage gate initially failed at 88.28 percent before public error and persistence
  behaviours received regression coverage.

### Verified Results
- `python3 scripts/af_dev.py --action check --execute`: success.
- Pytest: 38 passed.
- Coverage: 94.59 percent; required minimum 90 percent.
- Ruff formatting: 42 files formatted.
- Ruff lint: all checks passed.
- Mypy: no issues in 31 source files.
- Legacy AgentFactory validation: 33 of 33 passed.
- CLI smoke: `Hello, world!` and `3`.
- MCP: official in-memory SDK client/server session exercised tool catalogue and Task CRUD.

### Issues Encountered
- uv initially attempted to use an unwritable global cache; `[SCRIPT:af-dev]` now defaults
  `UV_CACHE_DIR` to the repository-local cache.
- The existing repeatable-reference validator excluded a script reference exactly five
  lines after its trigger; the boundary and a regression test were corrected.
- `af_hello` initially claimed read-only/idempotent MCP effects despite writing a trace;
  annotations and a protocol regression assertion now reflect the real effect.
- GitHub CLI is not installed/authenticated in the environment, so the required publishing
  workflow has not yet committed, pushed, opened a draft PR, or requested Copilot review.

### Related References
- Issue: https://github.com/alexanderholman/AgentFactory/issues/14
- Decision: DEC-022
- Spec: SPEC-009
- Script: `[SCRIPT:af-dev]`

---

## Run #006 - Establish and Publish the Private AgentFramework Repository
**Date**: 2026-07-18
**Status**: Success; draft review in progress
**Duration**: Not recorded
**Executor**: Codex using the authenticated GitHub app

### Inputs
- Create a new repository named `AgentFramework`.
- Start with private visibility.
- Move the completed AF implementation out of AgentFactory without modifying the
  original repository.
- Use GitHub Copilot on the published work.

### Outputs
- Confirmed private repository `alexanderholman/AgentFramework` with owner admin/push
  permissions.
- Copied the governed working snapshot into an independent local directory, excluding
  Git history, virtual environments, caches, coverage artefacts, and runtime storage.
- Changed active repository identity, setup links, contribution/security/support
  guidance, issue templates, task tracking, and Copilot guidance to AgentFramework.
- Preserved historical AgentFactory references in append-only records and marked other
  imported snapshot/proposal documents as historical.
- Removed inherited placeholder owners, greeting automation, stale deployment guidance,
  and a redundant validation workflow.
- Opened issue #1 and applied the standard `enhancement` label.
- Initialized `master` with final Copilot instructions so the initial review can use
  base-branch repository guidance.
- Published the full source tree in commit `5b337271f04687d3e799e604d16ee8082469adca`
  on `agent/af-python-vertical-slice`.
- Opened draft pull request #2 and successfully requested the GitHub Copilot reviewer.

### Verified Results
- Ruff format: 42 files already formatted.
- Ruff lint: all checks passed.
- Strict mypy: no issues in 31 source files.
- Pytest: 38 passed.
- Coverage: 94.59 percent; required minimum 90 percent.
- AgentFramework governance validation: 33 of 33 passed.
- GitHub reported Copilot as a requested reviewer on pull request #2.

### Issues Encountered
- The connected GitHub app can mutate existing repositories but does not expose repository
  creation; the owner created the empty private repository and granted app access.
- GitHub CLI was unavailable. At the owner's direction, publication used authenticated
  connector commit APIs without exposing credentials to the shell.
- Workspace-local uv build-interpreter links were rewritten by the sandbox. Validation
  succeeded with generated uv cache and virtual-environment paths under `/tmp`; source
  and results remained in AgentFramework.

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Issue: https://github.com/alexanderholman/AgentFramework/issues/1
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Decision: DEC-023
- Specs: SPEC-009, SPEC-010
- Script: `[SCRIPT:af-dev]`

---

## Run #007 - Final Selector-Only Cleanup and Governance Pass
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `5457fcd`
- Requested final cleanup across the trusted selector boundary, typed MCP counting,
  append-only governance, active documentation, release status, and Actions hardening
- Requirement to preserve prior append-only history byte-for-byte and append new records only

### Outputs
- Hardened selector metadata, catalogue validation, descriptor validation, and grounding
  failure handling with fixed reason codes and fail-closed traces
- Removed the active generative execution tier and verifier-routing seam
- Added a typed `count_characters` controller path used by MCP without natural-language
  reconstruction or selector calls
- Replaced `validate_agents.sh` shell evaluation with fixed command/function execution and
  added byte-prefix append-only validation
- Expanded Ruff checks to maintained Python scripts, pinned workflow actions by immutable
  SHA, fetched sufficient history for append-only checks, and disabled persisted checkout
  credentials
- Updated active documentation to the selector-only, deterministic-first pre-release
  contract and appended SPEC-011 and DEC-024

### Verified Results
- Ruff format check: 51 files already formatted
- Ruff lint: all checks passed
- Strict mypy: no issues in 31 source files
- Pytest: 112 passed, 1 warning
- Coverage: 91.24 percent; required minimum 90 percent
- Governance validation: 36 of 36 passed, including append-only byte-prefix checks
- Smoke: `uv run af hello` → `Hello, world!`
- Smoke: `uv run af ask 'how many rs in strawberry'` → `3`

### Issues Encountered
- The sandbox initially lacked `uv`; validation proceeded after installing it locally and
  regenerating the committed lockfile for the pre-release version bump.
- FastAPI/Starlette emitted a deprecation warning about `httpx` TestClient usage; the suite
  remained green and no `httpx2` dependency was added.
- The latest visible remote AF Python workflow run for this PR head was `action_required`
  with zero jobs, indicating owner/manual approval is still required before GitHub Actions
  can execute on the branch.

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Decision: DEC-024
- Specs: SPEC-011
- Script: `[SCRIPT:af-dev]`

---

## Run #008 - Final GraphQL GET and Trusted-Boundary Hardening Pass
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `ab9e604`
- Requested immediate correction for GraphQL GET mutation execution, bounded GET parsing,
  loopback trusted-host and CSRF cookie policy, and public `WorkRequest` hardening
- Requirement to keep the PR in draft, preserve append-only history, and append this record once

### Outputs
- Enforced GraphQL-over-HTTP query-only execution for GET requests before `schema.execute`,
  including named multi-operation documents and fragment-backed query reads
- Added bounded and sanitized GraphQL GET query-parameter parsing for query, variables, and
  operation names, plus malformed Unicode and deep-JSON failure coverage
- Added loopback trusted-host enforcement in the FastAPI HTTP kernel and strengthened HTML
  CSRF cookie assertions for host-only loopback HTTP and Secure HTTPS behavior
- Hardened the public `PipelineAgent.handle()` and typed `handle_classification()` boundaries
  to require exact `WorkRequest` objects and bounded plain attributes before tracing or routing

### Verified Results
- Ruff format check: 52 files already formatted
- Ruff lint: all checks passed
- Strict mypy: no issues in 31 source files
- Pytest: 174 passed, 1 warning
- Coverage: 90.65 percent; required minimum 90 percent
- Governance validation: 36 of 36 passed

### Issues Encountered
- The sandbox image did not have `uv` on `PATH`; validation proceeded after installing it for
  the current user
- The repository clone was shallow, so the first-pr topology governance test initially could
  not resolve `4e27a97f7887dfb4554ff2d74f8262c23deb25a4`; validation proceeded after
  `git fetch --unshallow origin` and `git fetch origin master:refs/remotes/origin/master`
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning;
  the suite remained green and no new dependency was added

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Script: `[SCRIPT:af-dev]`

---
## Run #009 - Final HTTP and GraphQL Standards Correction Pass
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `4d8dd52`
- Requested bounded HTTP/GraphQL standards correction for GraphQL complexity traversal, HTTP body ceilings, trusted host configuration, and Streamable HTTP MCP transport trust
- Requirement to keep the PR in draft, preserve Run #008 byte-for-byte, and append this record only after source changes with exact gate and smoke evidence

### Outputs
- Replaced recursive GraphQL fragment-expansion pre-checks with one bounded, saturating traversal that enforces a fixed traversal budget, rejects fragment cycles, and fails closed before any resolver
- Raised the shared HTTP request-body ceiling to 131072 bytes, enforced it through a bounded ASGI receive/replay boundary for HTML, REST, and GraphQL, and kept the 10000-character domain limit authoritative for Unicode prompts
- Separated HTTP bind addresses from trusted host configuration with strict loopback defaults, explicit non-loopback allowed hosts, and matching CLI/configuration support
- Completed GraphQL-over-HTTP media-type and Accept negotiation, sanitized Task-to-GraphQL presentation failures, and required explicit transport trust settings for non-loopback Streamable HTTP MCP

### Verified Results
- Ruff format check: 54 files already formatted
- Ruff lint: all checks passed
- Strict mypy: no issues in 32 source files
- Pytest: 194 passed, 1 warning
- Coverage: 91.14 percent; required minimum 90 percent
- Governance validation: 36 of 36 passed
- Smoke: `.venv/bin/python -m af hello` -> `Hello, world!`
- Smoke: `.venv/bin/python -m af ask 'how many rs in strawberry'` -> `3`

### Issues Encountered
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning; the suite remained green and no new dependency was added
- The latest visible AF Python workflow run for this PR head remained `action_required` with zero jobs, so owner/manual approval is still required before GitHub Actions can execute on the branch

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Workflow run: https://github.com/alexanderholman/AgentFramework/actions/runs/29654399448
- Script: `[SCRIPT:af-dev]`

---
## Run #010 - Final HTTP/MCP Post-Pass Correction
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `4d046bd`
- Requested executable post-pass correction for HTTP media negotiation, bounded ASGI body framing, IPv6 loopback trusted-host handling, and end-to-end Streamable HTTP MCP transport trust
- Requirement to keep the PR in draft, preserve Run #009 byte-for-byte, and append this record only if source changes landed

### Outputs
- Hardened GraphQL Accept and Content-Type parsing to honor media parameters and `q=0`, reject malformed quality values and structural parameter garbage, and deterministically prefer the GraphQL response media type only when equally acceptable
- Tightened the shared bounded-body boundary to reject duplicate or malformed `Content-Length`, incomplete ASGI disconnects, and declared-versus-observed length mismatches before any HTML, REST, or GraphQL handler work
- Replaced the HTTP trusted-host dependency with AF-owned canonical host parsing/matching so loopback IPv4/IPv6, bracketed IPv6, and configured host-or-host:port allowlists behave correctly while malformed or unrelated hosts fail closed
- Canonicalized and validated explicit Streamable HTTP MCP allowed hosts and origins, expanded non-loopback direct-host entries to the configured AF port for exact SDK matching, and anchored governance validation helpers to the repository project plus dev extras for repeatable script execution from temporary working directories

### Verified Results
- Ruff format check: 55 files already formatted
- Ruff lint: all checks passed
- Strict mypy: no issues in 33 source files
- Pytest: 199 passed, 1 warning
- Coverage: 90.93 percent; required minimum 90 percent
- Governance validation: 36 of 36 passed
- Smoke: `uv run af hello` -> `Hello, world!`
- Smoke: `uv run af ask 'how many rs in strawberry'` -> `3`

### Issues Encountered
- The sandbox clone was initially shallow, so the first-PR append-only topology check could not resolve `4e27a97f7887dfb4554ff2d74f8262c23deb25a4`; validation proceeded after `git fetch --unshallow origin` and `git fetch origin master:refs/remotes/origin/master`
- The sandbox image did not initially provide `uv` on `PATH`; validation proceeded after installing `uv` for the current user
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning; the suite remained green and no new dependency was added

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Workflow run: https://github.com/alexanderholman/AgentFramework/actions/runs/29655304393
- Script: `[SCRIPT:af-dev]`

---
## Run #011 - Media Negotiation Precedence Correction
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `c8a153e`
- Requested standards correction for GraphQL Accept media-range precedence and strict Content-Type parameter validation
- Requirement to keep the PR in draft, preserve Run #010 byte-for-byte, and append this record only because source changes landed

### Outputs
- Updated GraphQL Accept negotiation to apply per-representation media-range precedence before quality, so exact `q=0` rejections cannot be revived by less-specific wildcards
- Rejected malformed Accept media ranges with duplicate parameters such as repeated `q` values using strict token and quoted-string parsing
- Hardened Content-Type parsing to reject malformed quoted strings, invalid unquoted parameter tokens, and duplicate parameter names while preserving valid `application/json` charset parameters
- Added unit and feature regressions covering exact-precedence fallback, duplicate Accept parameters, and malformed or duplicated Content-Type parameters

### Verified Results
- Ruff format check: 55 files already formatted
- Ruff lint: all checks passed
- Strict mypy: no issues in 33 source files
- Pytest: 199 passed, 1 warning
- Coverage: 90.04 percent; required minimum 90 percent
- Governance validation: 36 of 36 passed
- Smoke: `uv run af hello` -> `Hello, world!`
- Smoke: `uv run af ask 'how many rs in strawberry'` -> `3`

### Issues Encountered
- The sandbox clone was initially shallow, so the first-PR append-only topology check could not resolve `4e27a97f7887dfb4554ff2d74f8262c23deb25a4`; validation proceeded after `git fetch --unshallow origin` and `git fetch origin master:refs/remotes/origin/master`
- The sandbox image did not initially provide `uv` on `PATH`; validation proceeded after installing `uv` for the current user
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning; the suite remained green and no new dependency was added
- The latest visible AF Python workflow run for the last committed head remained `action_required` with zero jobs, so owner/manual approval is still required before GitHub Actions can execute on the branch

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Workflow run: https://github.com/alexanderholman/AgentFramework/actions/runs/29655742961
- Script: `[SCRIPT:af-dev]`

---

## Run #012 - Supplemental MCP Origin Validation Correction
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `e93052e`
- Supplemental request to preserve Run #011 byte-for-byte, append a new record only if source changes landed, and keep the PR in draft
- Requested corrections for malformed MCP origin configuration rejection, loopback-time validation of explicit allowed hosts/origins, documented default-port canonicalization, and `*/*` Accept-precedence coverage with quote-aware parser regressions

### Outputs
- Hardened `parse_origin_value()` to reject malformed Origin spellings with whitespace, control characters, path/query/fragment delimiters, and ambiguous empty ports before URL normalization can erase them
- Canonicalized default origin ports so `http://host:80` and `https://host:443` normalize to the same stored value as the browser-compatible default-port Origin form, while preserving non-default ports exactly
- Validated explicit Streamable HTTP MCP `allowed_hosts` and `allowed_origins` even on loopback binds before preserving the SDK’s automatic loopback transport-security defaults
- Added GraphQL regressions covering `*/*` precedence so less-specific wildcards cannot revive more-specific `q=0` rejections, plus a quoted-parameter parsing regression and focused MCP startup/parser tests

### Verified Results
- Ruff format check: 55 files already formatted
- Ruff lint: all checks passed
- Strict mypy: no issues in 33 source files
- Pytest: 203 passed, 1 warning
- Coverage: 90.15 percent; required minimum 90 percent
- Governance validation: 36 of 36 passed
- Smoke: `uv run af hello` -> `Hello, world!`
- Smoke: `uv run af ask 'how many rs in strawberry'` -> `3`

### Issues Encountered
- The sandbox clone started shallow, so the reviewed first-PR append-only topology test could not resolve `4e27a97f7887dfb4554ff2d74f8262c23deb25a4`; validation proceeded after `git fetch --unshallow origin` and `git fetch origin master:refs/remotes/origin/master`
- The sandbox image did not initially provide `uv` on `PATH`; validation proceeded after installing `uv` for the current user
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning; the suite remained green and no new dependency was added
- The latest visible workflow run for the last committed head remained `in_progress` at the time of this record: `https://github.com/alexanderholman/AgentFramework/actions/runs/29656142417`

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Workflow run: https://github.com/alexanderholman/AgentFramework/actions/runs/29656142417
- Script: `[SCRIPT:af-dev]`

---
## Run #013 - CLI Delete and Push-Range Append-Only Correction
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `9683aed`
- Requested test-first correction to restore the stable CLI `tasks delete` JSON contract and to validate append-only governance files against the full pushed commit range
- Requirement to preserve Runs #001 through #012 byte-for-byte, append this record only because source changes landed, and keep the PR in draft

### Outputs
- Restored CLI `tasks delete` stdout to the stable machine-readable JSON tombstone payload `{"id": ..., "deleted": true}` after successful shared-controller deletion
- Added a CLI feature regression that parses delete stdout as JSON and asserts the exact deletion payload
- Updated the AF Python workflow to pass `github.event.before` as `AF_APPEND_ONLY_BASE_REF` on push while preserving the existing pull-request base SHA behavior
- Hardened `scripts/check_append_only.py` to fail closed on GitHub's all-zero push base and added governance regressions covering the multi-commit push mutation case plus the all-zero new-branch topology

### Verified Results
- Ruff format check: 55 files already formatted
- Ruff lint: all checks passed
- Strict mypy: no issues in 33 source files
- Pytest: 205 passed, 1 warning
- Coverage: 90.17 percent; required minimum 90 percent
- Governance validation: 36 of 36 passed
- Smoke: `uv run af hello` -> `Hello, world!`
- Smoke: `uv run af ask 'how many rs in strawberry'` -> `3`

### Issues Encountered
- The sandbox clone started shallow, so the reviewed first-PR append-only topology test could not resolve `4e27a97f7887dfb4554ff2d74f8262c23deb25a4`; validation proceeded after `git fetch --unshallow origin` and `git fetch origin master:refs/remotes/origin/master`
- The sandbox image did not initially provide `uv` on `PATH`; validation proceeded after installing `uv` for the current user
- The latest visible AF Python workflow run for committed head `9683aed` remained `action_required` with zero jobs, so repository-owner approval is still required before hosted CI can execute on the branch

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Workflow run: https://github.com/alexanderholman/AgentFramework/actions/runs/29656484230
- Script: `[SCRIPT:af-dev]`

---
## Run #014 - Exact Push Head Append-Only Correction
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `3a0138c`
- Requested test-first correction to preserve `03964ee` and Run #013 byte-for-byte while validating push append-only checks against the exact `github.event.after` commit instead of assuming the checked-out `HEAD`
- Requirement to keep pull-request merge-base semantics unchanged, reject all-zero push heads, preserve the PR draft state, and append only this new record because source changes landed

### Outputs
- Added explicit append-only head resolution in `scripts/check_append_only.py` through `--head-ref` and `AF_APPEND_ONLY_HEAD_REF`, with fail-closed rejection for unresolved or all-zero push heads
- Required the checked-out `HEAD` to match the expected exact push `after` commit before validation and kept exact push ancestry checks against that validated head instead of a merge-base substitute
- Updated the AF Python workflow to pass `github.event.after` into append-only validation and to verify both push endpoints are available before the locked gate runs
- Added focused governance regressions for a divergent checked-out head versus expected push `after`, all-zero push `after` rejection, and the new workflow environment contract

### Verified Results
- Ruff format check: 55 files already formatted
- Ruff lint: all checks passed
- Strict mypy: no issues in 33 source files
- Pytest: 208 passed, 1 warning
- Coverage: 90.17 percent; required minimum 90 percent
- Governance validation: 36 of 36 passed
- Smoke: `uv run af hello` -> `Hello, world!`
- Smoke: `uv run af ask 'how many rs in strawberry'` -> `3`

### Issues Encountered
- The sandbox clone started shallow, so the existing first-PR append-only topology test initially could not resolve `4e27a97f7887dfb4554ff2d74f8262c23deb25a4`; validation proceeded after `git fetch --unshallow origin` and `git fetch origin master:refs/remotes/origin/master`
- The sandbox image did not initially provide `uv` on `PATH`; validation proceeded after installing `uv` for the current user
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning; the suite remained green and no dependency changes were introduced

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Script: `[SCRIPT:af-dev]`

---
## Run #015 - Exact Event Head Workflow and Strategy Validation Correction
**Date**: 2026-07-18
**Agent**: GitHub Copilot Coding Agent
**Status**: Completed

### Inputs
- Pull request #2 follow-up on head `56899b8`
- Requested final surgical correction to bind append-only validation to the exact event head on both push and non-push runs, replace inline push-base fetch logic with pinned `actions/checkout`, reject invalid `AF_APPEND_ONLY_BASE_STRATEGY` values, and preserve earlier run records byte-for-byte
- Requirement to keep the PR in draft, preserve Run #014 exactly as already appended on the current head, and append only a new run record because source changes landed again

### Outputs
- Changed the AF Python workflow to set `AF_APPEND_ONLY_HEAD_REF` to `github.event.after` on push and `github.sha` otherwise
- Replaced the inline token-bearing push-base fetch heredoc with pinned `actions/checkout` steps that first materialize a nonzero push `before` commit and then check out the exact event head with `persist-credentials: false`
- Hardened `scripts/check_append_only.py` to reject invalid environment-derived base strategies, resolve commits with `git rev-parse --verify --end-of-options`, and apply bootstrap/tree append-only checks to the validated exact head revision
- Added focused governance regressions for missing push heads, invalid strategy values, and the exact workflow before/after checkout contract

### Verified Results
- Targeted governance regressions: `6 passed` for the exact-head, invalid-strategy, mismatch, non-ancestor, and all-zero cases
- Full governance script unit suite: `19 passed`
- `python3 scripts/af_dev.py --action check --execute`: success with Ruff format check passed, Ruff lint passed, strict mypy passed in 33 source files, `210 passed`, `90.17%` coverage, and `36/36` governance checks passed
- `python3 scripts/af_dev.py --action smoke --execute`: `Hello, world!` and `3`

### Issues Encountered
- The sandbox clone started shallow, so the repository-level first-PR topology test could not initially resolve `4e27a97f7887dfb4554ff2d74f8262c23deb25a4`; validation proceeded after `git fetch --unshallow origin` and `git fetch origin master:refs/remotes/origin/master`
- The sandbox image did not initially provide `uv` on `PATH`; validation proceeded after installing `uv` for the current user
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning during pytest; the suite remained green and no dependency changes were introduced

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
- Script: `[SCRIPT:af-dev]`

---

## Run #016 - Production-MVP Readiness Contract and Artifact Gate
**Date**: 2026-07-23
**Agent**: Zordon (`pr.command.zordon`)
**Status**: Completed

### Inputs
- Request to prepare the focused pull request needed to define a safe production MVP
- Existing roadmap authority in issue #3, release-certification scope in issue #34, and active
  lifecycle implementation in issue #9 that this work was required not to duplicate
- Independent security, package, automation, and documentation reviews of the complete branch

### Outputs
- Defined a canonical, explicitly not-ready private profile with one Linux node, tenant,
  SQLite database, live MCP-stdio process, deterministic routes, quiesced CLI maintenance, and
  no AF network listener or model execution
- Added 33 blocking release declarations spanning scope, quality, runtime, artifacts, security,
  SQLite durability/recovery, operations, accessibility boundaries, evidence, and approval
- Added declaration-only deterministic reporting with canonical-policy and full-manifest
  digests, explicit non-certification output, strict YAML/path handling, and stable exit codes
- Added a clean-commit package gate that builds wheel-from-sdist with locked uv-offline tooling,
  strictly validates metadata, licenses, entry points, archive members, source bytes, and
  `RECORD`, installs the exact wheel, loads both installed targets, and smokes deterministic
  routes outside the checkout
- Added SPEC-016 through SPEC-018 and DEC-030 through DEC-032 to preserve the narrowed scope,
  candidate sequencing, external evidence boundary, and honest no-egress limitations

### Verified Results
- `python scripts/af_dev.py --action check --execute`: passed
- Ruff format and lint: passed across 69 files
- Strict mypy: no issues in 36 source files
- Pytest: 332 passed with 90.67 percent coverage
- Node protocol test: 1 passed
- AgentFramework governance validation: 36 of 36 passed, including append-only enforcement
- Clean commit-snapshot package validation: passed for `0.1.0a0`
- Sdist SHA-256: `e17700db491717e0eadb5edceacc181ec3f236b62a924f0ee11f251cb7201fe0`
- Wheel SHA-256: `3c2747275e2c68aed95b42a021976b341dbc9fc00fac2f0853cbd31c107c5f8f`

### Issues Encountered
- Final review found that the first policy encoded loopback despite excluding every listener;
  the canonical profile was corrected to stdio-only with one database/process in total
- Hatchling emits optional dependencies and explicit extras in core metadata; package identity
  validation was extended to compare those normalized declarations and explicit license files
- Build isolation language initially overstated no-egress guarantees; documentation now limits
  the implemented claim to uv-offline/no-config execution and keeps OS no-egress blocked
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning;
  all tests and coverage requirements remained green

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Roadmap: https://github.com/alexanderholman/AgentFramework/issues/3
- Release certification: https://github.com/alexanderholman/AgentFramework/issues/34
- Script: `[SCRIPT:af-dev]`

---

## Run #017 - Final Evidence-Binding and Launcher Correction
**Date**: 2026-07-23
**Agent**: Zordon (`pr.command.zordon`)
**Status**: Completed

### Inputs
- Blocker-only rereview of the clean production-MVP readiness branch after Run #016
- Findings covering manifest read/hash consistency, digest visibility, outer uv configuration,
  malformed archive member types, and precise Node test-count reporting

### Outputs
- Bound manifest parsing and SHA-256 reporting to the same file-descriptor byte buffer and
  printed both manifest and canonical-policy digests in validate, report, and check text output
- Added no-config/no-env-file flags plus an allowlisted environment for every locked release
  launcher while preserving only append-only CI context required by governance validation
- Rejected dot-path sdist members and wheel files whose archive mode claims they are directories,
  with focused negative regressions for both malformed cases
- Clarified Run #016's Node result: Node's TAP summary reports one passing test file, and that
  file contains nine named `test()` cases; all nine completed without failure

### Verified Results
- `python scripts/af_dev.py --action check --execute`: passed
- Ruff format and lint: passed across 69 files
- Strict mypy: no issues in 36 source files
- Pytest: 335 passed with 90.67 percent coverage
- Node protocol suite: one passing file containing nine named test cases
- AgentFramework governance validation: 36 of 36 passed, including append-only enforcement
- Clean commit-snapshot package validation: passed for `0.1.0a0`
- Sdist SHA-256: `fe0921018f97c38e504f746911ebeb41a57b73f919ef99c2ef5405a168545247`
- Wheel SHA-256: `97860cb20124483cf12b3169674c7f7b89f4eceedfbd92d07628d4ae141e07f8`

### Issues Encountered
- The Node 22 TAP reporter aggregates the explicit file argument as one top-level subtest even
  though the file defines nine named cases; this entry records both facts instead of treating
  the file-level count as the case count
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning;
  all tests and coverage requirements remained green

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Roadmap: https://github.com/alexanderholman/AgentFramework/issues/3
- Release certification: https://github.com/alexanderholman/AgentFramework/issues/34
- Script: `[SCRIPT:af-dev]`

---

## Run #018 - Canonical Runner and Tar-Type Correction
**Date**: 2026-07-23
**Agent**: Zordon (`pr.command.zordon`)
**Status**: Completed

### Inputs
- Package-gate rereview after Run #017
- Findings that direct documented uv commands bypassed the sanitized runner and that legacy
  tar contiguous-file members satisfy Python's broad `TarInfo.isfile()` predicate

### Outputs
- Routed every documented and registered release action through `[SCRIPT:af-dev]`, including a
  dedicated JSON report action, so locked commands receive the bounded launcher environment
- Added an executed regression proving inherited `UV_CONFIG_FILE`, `UV_NO_SYNC`, `UV_PYTHON`,
  and `UV_PROJECT_ENVIRONMENT` cannot redirect canonical release validation
- Required exact tar regular-file or directory type bytes for source snapshots and sdists and
  added a negative contiguous-file archive regression

### Verified Results
- `python scripts/af_dev.py --action check --execute`: passed
- Ruff format and lint: passed across 69 files
- Strict mypy: no issues in 36 source files
- Pytest: 338 passed with 90.67 percent coverage
- Node protocol suite: one passing file containing nine named test cases
- AgentFramework governance validation: 36 of 36 passed, including append-only enforcement
- Clean commit-snapshot package validation: passed for `0.1.0a0`
- Sdist SHA-256: `451791bcc306751e90092303561dfc7258750e96b35ebb5e080b930cdb6c4dfd`
- Wheel SHA-256: `e7c9906984faa44e37717ebd7584dda3c0b322f28ab3b4e022ad3f0ce384e57a`

### Issues Encountered
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning;
  all tests and coverage requirements remained green

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Roadmap: https://github.com/alexanderholman/AgentFramework/issues/3
- Release certification: https://github.com/alexanderholman/AgentFramework/issues/34
- Script: `[SCRIPT:af-dev]`

---

## Run #019 - Latest Master Integration Verification
**Date**: 2026-07-23
**Agent**: Zordon (`pr.command.zordon`)
**Status**: Completed

### Inputs
- Newly fetched `origin/master` at `06cfdda`, five commits beyond the branch base
- Merged Copilot workflow documentation and deterministic scoped-container lifecycle work from
  pull requests #45 and #51

### Outputs
- Merged the latest canonical master into the production-MVP readiness branch without conflict
- Revalidated the complete readiness policy and package workflow against the expanded scoped-
  container implementation and test suite

### Verified Results
- `python scripts/af_dev.py --action check --execute`: passed
- Ruff format and lint: passed across 72 files
- Strict mypy: no issues in 36 source files
- Pytest: 544 passed with 90.07 percent coverage
- Node protocol suite: one passing file containing nine named test cases
- AgentFramework governance validation: 36 of 36 passed, including append-only enforcement
- Clean commit-snapshot package validation: passed for `0.1.0a0`
- Sdist SHA-256: `53799ec87ae37f6e06bc6b3e34a05dfb6c21b2c49211a8154b3638924513f943`
- Wheel SHA-256: `8fb5124dfbe508c5392fe46ff7c25977460a32f2cc60f453a3f7611380ba3eb6`

### Issues Encountered
- The remote base advanced after final review; a merge was required before publication, but no
  content or append-only conflict occurred
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning;
  all tests and coverage requirements remained green

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Integrated pull request: https://github.com/alexanderholman/AgentFramework/pull/45
- Integrated pull request: https://github.com/alexanderholman/AgentFramework/pull/51
- Roadmap: https://github.com/alexanderholman/AgentFramework/issues/3
- Script: `[SCRIPT:af-dev]`

---

## Run #020 - Closed Scope-Issue Status Refresh
**Date**: 2026-07-23
**Agent**: Zordon (`pr.command.zordon`)
**Status**: Completed

### Inputs
- Issue #9 closed by the scoped-container work integrated from pull request #51
- Final pre-publication review of release blocker descriptions against current GitHub state

### Outputs
- Updated the graceful-shutdown next action and implementation order to treat issue #9 as the
  completed scoped-lifetime foundation while keeping issue #10 lifecycle work blocked
- Preserved the canonical gate policy digest because only current state/next-action guidance
  changed; the complete manifest digest now binds the refreshed text

### Verified Results
- `python scripts/af_dev.py --action check --execute`: passed
- Ruff format and lint: passed across 72 files
- Strict mypy: no issues in 36 source files
- Pytest: 544 passed with 90.07 percent coverage
- Node protocol suite: one passing file containing nine named test cases
- AgentFramework governance validation: 36 of 36 passed, including append-only enforcement
- Manifest SHA-256: `0c961d5c7ed6309c567876fbf2ccba54bdd7a01f197a6b84a629e65c8ee4ca10`
- Policy SHA-256: `9f9dfb0d3621390654253c60aae169a76fb35f28f0f6ced69fa772f03592ae50`
- Sdist SHA-256: `00a30cb265817c3dcc0823a5e1da115d4906e6d596e39279478625546ab32f0b`
- Wheel SHA-256: `8fb5124dfbe508c5392fe46ff7c25977460a32f2cc60f453a3f7611380ba3eb6`

### Issues Encountered
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning;
  all tests and coverage requirements remained green

### Related References
- Repository: https://github.com/alexanderholman/AgentFramework
- Closed scope issue: https://github.com/alexanderholman/AgentFramework/issues/9
- Remaining lifecycle issue: https://github.com/alexanderholman/AgentFramework/issues/10
- Script: `[SCRIPT:af-dev]`

---

## Run #021 - CI Governance Environment Correction
**Date**: 2026-07-23
**Agent**: Zordon (`pr.command.zordon`)
**Status**: Completed

### Inputs
- Pull request #52 Python workflow run `29970858948`
- CI-only failure in the temporary-repository shell-injection regression because two governance
  helpers invoked unanchored `uv run` and selected a Python environment without PyYAML

### Outputs
- Prepended the locked project environment to the bounded release-runner `PATH`
- Routed the remaining agent-count and file-existence Python snippets through the existing
  repository-anchored `run_uv_python` helper
- Added regressions for nested Python dependency availability and inherited `PATH` values that
  omit the project environment

### Verified Results
- Focused development/governance tests: 30 passed
- `python scripts/af_dev.py --action check --execute`: passed
- Ruff format and lint: passed across 72 files
- Strict mypy: no issues in 36 source files
- Pytest: 544 passed with 90.07 percent coverage
- Node protocol suite: one passing file containing nine named test cases
- AgentFramework governance validation: 36 of 36 passed, including append-only enforcement
- Sdist SHA-256: `7af3ca564e87781322b756bc19ec61b58ef89c90d01835e6bd9aeb71f1399e88`
- Wheel SHA-256: `8fb5124dfbe508c5392fe46ff7c25977460a32f2cc60f453a3f7611380ba3eb6`

### Issues Encountered
- Local shells already placed `.venv/bin` first, masking the missing repository anchor; the CI
  runner exposed the discrepancy by starting with only setup-Python and uv on `PATH`
- FastAPI/Starlette continued to emit the existing `httpx` TestClient deprecation warning;
  all tests and coverage requirements remained green

### Related References
- Pull request: https://github.com/alexanderholman/AgentFramework/pull/52
- Failed workflow run: https://github.com/alexanderholman/AgentFramework/actions/runs/29970858948
- Script: `[SCRIPT:af-dev]`

---
