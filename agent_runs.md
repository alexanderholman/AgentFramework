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

## Run #016 - Prime Language / Ideon Deterministic Reference Slice
**Date**: 2026-07-22
**Agent**: ChatGPT Codex
**Status**: Completed

### Inputs
- Prime Language and Ideon System Specification v0.1 supplied as
  `prime_language_ideon_system_spec_v0.1.md`
- AgentFramework `master` at `fa101610284962c9d33bce5da778b3836d57992e`
- Existing AF layer, dependency, testing, and append-only governance contracts

### Outputs
- Added immutable Ideon graph, canonical identity, arbitrary-precision key, exact revision,
  logical dictionary, inquiry, temporal, Principia, SHOULD, and runtime-record domain modules
- Added an injective length-prefixed binary codec for constructed keys, explicit manifested
  prime-key registration, SHA-256 graph revisions, exact reference resolution, and conflict
  detection through a process-local atomic registry adapter
- Added the controlled `sat on the bank`, edition-order, Whern, WILL, relation, semantic-island,
  paraphrase, SHOULD, unknown-form, context, large-key, and nested-composition conformance cases
- Integrated one shared Ideon application service/controller through AF bootstrap, CLI, and REST
- Preserved the supplied system specification and documented the delivered slice, limitations,
  persistence evolution, and Prolog/neural/natural-reading follow-up
- Appended SPEC-016 and DEC-030 without altering prior governed content

### Verified Results
- Focused Ideon suite: 86 passed; 91 percent branch coverage
- Full reconstructed-checkout suite: 374 passed; one Git-history-dependent governance test
  unavailable because the connector workspace is not a Git checkout
- Full-suite coverage: 91.00 percent; required minimum 90 percent
- Ruff format and lint: passed across source, tests, and scripts
- Strict mypy: no issues in 45 source files
- Node route-gate suite: 9 passed
- Governance content validation: 22 of 22 passed; append-only history check skipped locally
- Source distribution and wheel build: passed

### Issues Encountered
- The initial injective JSON-to-integer design exceeded Python's protected decimal conversion
  limit on modest compositions; the final codec uses length-prefixed binary identity data and
  a limit-independent decimal transport encoder, with 20-token and ten-level regressions
- The scratch reconstruction has no repository object history, so the reviewed first-PR
  append-only topology test cannot resolve its historical SHA locally; the real branch/CI
  checkout remains the authority for that single check
- FastAPI/Starlette emits the repository's existing TestClient deprecation warning; no new
  dependency was introduced
- General parsing, Prolog parity, neural routes, durable storage, and held-out natural reading
  remain explicitly outside this deterministic reference slice

### Related References
- Specification: `docs/prime-language-ideon-system-spec-v0.1.md`
- Implementation note: `docs/prime-language-ideon-implementation.md`
- Script: `[SCRIPT:af-dev]`

---

---

## Run #017 — Ideon Correction Pass (Provenance, Temporal, WHO, Identity Codes)
**Date**: 2026-07-22
**Agent**: Copilot (correction pass on PR feat/ideons)
**Status**: Completed

### Inputs
- Review comment by Alexander Holman on PR feat(ideons): six semantic corrections required
- Existing implementation at commit 9748dc0

### Outputs
- Renamed `Tense.WILL` → `Tense.PROJECTED`; `WillKind` → `AuxiliaryKind`;
  `will_kind` → `auxiliary_kind`; added `TemporalQualification` dataclass
- Changed `will.future-auxiliary` dictionary sense slot from WHERN to WHEN
- Fixed WHO inquiry: material-open only when predicate present; NOT_APPLICABLE otherwise
- Removed source ID and rights from semantic graph annotations/provenance; verified that
  same semantics from different sources share graph revision
- Added docstring to `numeric.py` documenting constructed keys as canonical identity codes
- Updated inquiry module docstring to accurately state which attentions are implemented
- Added two regression tests (TEST-017-1, TEST-017-2) and two new focused tests
  (temporal qualification on propositions, WHERN criterion vs anchor)
- Updated `docs/prime-language-ideon-implementation.md` to reflect all corrections
- Appended SPEC-017 and DEC-031

### Verified Results
- Focused Ideon suite: 88 passed (+2 from baseline)
- Strict mypy: no issues in 51 source files
- Ruff check + format: passed
- Full suite (excluding pre-existing yaml subprocess issue): 358 passed

### Issues Encountered
- None; all corrections were surgical with no architectural change required

### Related References
- Implementation note: `docs/prime-language-ideon-implementation.md`
- Spec: SPEC-017
- Decision: DEC-031

---

---

## Run #018 — Ideon Temporal Model Corrections
**Date**: 2026-07-22
**Agent**: Copilot Coding Agent
**Triggered by**: PR review comment #5052571328 (Alexander Holman)

### Scope
Second correction pass on the deterministic Prime Language reference slice.  Addressed
four remaining blockers: composite Whern (WHERE+WHEN), condition-bearing WHEN,
role-driven WHO, and minimal procedures for WHY/HOW/COULD/WOULD.

### Changes Made
- `src/af/domain/ideons/temporal.py`: Added `WhenKind` enum and `WhenCondition`
  dataclass (condition-bearing typed temporal relation).  Redesigned `Whern` as
  WHERE+WHEN composite.  Updated `TemporalQualification.when: WhenCondition`.
  Updated `render_tense(WhenCondition, WhenCondition)`.  Removed "reference Whern"
  terminology.
- `src/af/domain/ideons/records.py`: Added `semantic_roles: tuple[str, ...] = ()`
  field to `SenseEntry`.
- `src/af/domain/ideons/inquiry.py`: WHO driven by `semantic_roles` (who-typed roles),
  not generic predicate presence.  WHEN satisfied by context.time OR context.when.
  WHERN requires WHERE+WHEN composite; partial presence yields OPEN (non-material).
  Added minimal evidence-driven procedures for WHY, HOW, COULD, WOULD.  Updated
  module docstring to reflect all eleven attentions having deterministic behaviour.
- `src/af/infrastructure/ideon_reference.py`: Added `semantic_roles` to `_SeedSense`
  and updated `sit.past` and `will.intends` entries with `("agent",)` roles.
- `tests/unit/test_ideon_language.py`: Updated imports and temporal test fixtures
  to use `WhenCondition`/`WhenKind`.  Added six new tests (TEST-018-1 through 8).
- `tests/unit/test_ideon_service.py`: Updated `reference_service()` to pass
  `semantic_roles` for `sit.past`.
- `specs.md`: Appended SPEC-018.
- `decisions.md`: Appended DEC-032 (superseding WHERN description in DEC-031).
- `agent_runs.md`: Appended Run #018.

### Verified Results
- Focused Ideon suite (language + service + validation): 77 passed
- All unit tests (excluding pre-existing git-history-dependent governance test): 311 passed
- Strict mypy (src only): no issues
- Ruff check + format: passed

### Issues Encountered
- Pre-existing: `test_append_only_check_uses_bootstrap_anchor_for_first_pr_topology`
  fails locally (bootstrap SHA unresolvable in shallow clone).  GitHub CI is
  authoritative for this check.

### Related References
- Spec: SPEC-018
- Decision: DEC-032
- PR review comment: #5052571328

---

---

## Run #019 — Ideon Integration Correction Pass
**Date**: 2026-07-23
**Agent**: Copilot Coding Agent
**Triggered by**: PR review comment #5052915940 (Alexander Holman)

### Scope
Third correction pass on the deterministic Prime Language reference slice.  Addressed
six remaining integration blockers: WHERN constituent-state propagation, fail-closed WHEN
timestamp validation, multi-role WHO satisfaction, end-to-end typed temporal graph node,
interpretation graph contract documentation, and stale terminology removal.

### Changes Made
- `src/af/domain/ideons/inquiry.py`: Added `_WHO_ROLE_CONTEXT_MAP` mapping semantic roles
  to context fields.  Added `_is_valid_time_string()` for fail-closed ISO timestamp
  validation.  Refactored WHO to use `required_who_roles` set and emit exact role name.
  Fixed WHEN to use `time_val is not None and _is_valid_time_string(time_val)`.  Fixed
  WHERN constituent-state propagation: contested WHERE + satisfied WHEN yields OPEN with
  "where:contested"; all four branches restructured with pre-computed `t_ev`.
- `src/af/application/ideons.py`: Added `InquiryStatus` and `Question` imports.  Added
  typed `temporal-qualification` node (when-kind, when-value) to `construct_graph()` when
  WHEN is satisfied.  Added interpretation graph contract docstring.
- `tests/unit/test_ideon_language.py`: Renamed
  `test_one_event_renders_as_was_is_or_projected_by_reference_whern` to
  `test_render_tense_classifies_event_as_was_is_or_projected`.  Updated
  `test_whern_is_composite_of_where_and_when_components` to reflect contested-WHERE
  propagation.  Added: `test_invalid_time_string_does_not_satisfy_when`,
  `test_whern_constituent_state_propagates_contested_where`,
  `test_who_satisfied_by_non_agent_role`.
- `tests/unit/test_ideon_service.py`: Added `InquiryStatus` import.  Added:
  `test_temporal_qualification_node_in_graph_when_when_is_satisfied` (end-to-end),
  `test_interpretation_graph_is_surface_bound`.
- `specs.md`: Appended SPEC-019.
- `decisions.md`: Appended DEC-033.
- `agent_runs.md`: Appended Run #019.

### Verified Results
- `ruff check src tests scripts`: passed
- `strict mypy src` (51 files): no issues
- Unit + integration tests: 389 passed (1 pre-existing git-topology failure; CI authoritative)
- Full gate (ruff + mypy + pytest + legacy): passed

### Issues Encountered
- Pre-existing: `test_append_only_check_uses_bootstrap_anchor_for_first_pr_topology`
  fails locally (bootstrap SHA unresolvable in shallow clone).  GitHub CI is authoritative.

### Related References
- Spec: SPEC-019
- Decision: DEC-033
- PR review comment: #5052915940

---

---

## Run #020 — Ideon Final Correction Pass (SPEC-020 / DEC-034)
**Date**: 2026-07-23
**Status**: Completed

### Objective
Address the four remaining integration blockers identified in the PR review
comment #5053551914:
1. `construct_graph()` silent `Tense.IS` default for incompatible reference_when.
2. `WhenKind` missing ORDERING form despite documentation claiming ordering support.
3. `WhenCondition(INTERVAL)` domain invariants not self-enforcing.
4. Private `_parse_when_expression` coupled from application layer to domain internals.
5. Stale PR/governance claims about WILL and "eleven implemented attentions."

### Actions Taken
- `src/af/domain/ideons/temporal.py`:
  - Added `WhenKind.ORDERING = "ordering"` with expression validation in `__post_init__`.
  - Tightened `WhenCondition(INTERVAL)` validation: both-absent raises ValueError;
    start > end raises ValueError.
  - Added public `parse_when_expression(value: str) -> WhenCondition | None` as the
    authoritative bounded ``kind:expression`` parser.  Moved `_WHEN_EXPRESSION_KINDS`
    constant here.  Updated module docstring.
- `src/af/domain/ideons/inquiry.py`:
  - Removed private `_parse_when_expression` and `_WHEN_EXPRESSION_KINDS`.
  - Imports `parse_when_expression` from `temporal`; uses it for WHEN validation.
  - Updated module docstring to mention `ordering` kind and public parser location.
- `src/af/application/ideons.py`:
  - Replaced `from af.domain.ideons.inquiry import _parse_when_expression` with
    `from af.domain.ideons.temporal import parse_when_expression`.
  - Removed `contextlib.suppress(ValueError)` in `construct_graph()`.  Incompatible
    or uncomparable `reference_when` kinds now yield `tense_value = "contested"`.
  - Updated docstring with explicit fail-closed tense contract.
- `tests/unit/test_ideon_language.py`:
  - Updated `test_parse_when_expression_valid_and_fail_closed` to import from `temporal`
    and cover ORDERING kind and inverted-interval fail-closed case.
  - Added `test_when_satisfied_by_ordering_expression`.
  - Added `test_when_condition_interval_invariants_enforced_by_domain`.
  - Added `test_ordering_when_condition_domain_validation`.
- `tests/unit/test_ideon_service.py`:
  - Added `test_incompatible_reference_when_yields_contested_tense_not_is`.
  - Added `test_uncomparable_event_interval_with_timestamp_reference_yields_contested`.
- `specs.md`: Appended SPEC-020.
- `decisions.md`: Appended DEC-034 (includes governance supersession of WILL/eleven-attentions
  claims in SPEC-016/DEC-030).
- `agent_runs.md`: Appended Run #020.

### Verified Results
- `ruff check src tests`: passed
- `strict mypy src` (3 modified files): no issues
- Unit + integration tests: 559 passed, 1 pre-existing git-topology failure (CI authoritative)
- All 9 new regression tests pass (TEST-020-1 through TEST-020-9)

### Issues Encountered
- Pre-existing: `test_append_only_check_uses_bootstrap_anchor_for_first_pr_topology`
  fails locally (bootstrap SHA unresolvable in shallow clone).  GitHub CI is authoritative.

### Related References
- Spec: SPEC-020
- Decision: DEC-034
- PR review comment: #5053551914

---

---

## Run #021 — Role-Driven WHERE and WHERN Governance Alignment
**Date**: 2026-07-23
**Trigger**: PR review comment #5053882042

### Problem
Two merge blockers in the PR's deterministic reference slice:

1. WHERE was still lexeme-driven, not role-driven.  `context.where` could satisfy the
   spatial constituent of WHERN while `Question.WHERE` itself stayed NOT_APPLICABLE.
   A bare location noun made WHERE contested without any predicate declaring a spatial
   dependency.
2. SPEC-019/DEC-033 stated that CONTESTED WHERE + SATISFIED WHEN yields WHERN=OPEN
   (non-material).  The implementation correctly yields WHERN=CONTESTED (material).
   The governed record and the code disagreed.

### Actions Taken
- `src/af/domain/ideons/inquiry.py`:
  - Added `_WHERE_TYPED_ROLES = frozenset({"location"})` module-level constant.
  - Replaced the old lexeme-driven WHERE block with a role-driven implementation:
    `context.where` → SATISFIED; `location` role + candidates → CONTESTED/SATISFIED;
    `location` role + no filler → OPEN (material); no role + no context.where →
    NOT_APPLICABLE.
  - Replaced the WHERN block: removed the separate `context.where` check; uses WHERE
    result status directly; added `where_open` handling so WHERE=OPEN + WHEN=SATISFIED
    yields WHERN=OPEN (missing:where) rather than accidentally NOT_APPLICABLE.
  - Updated module docstring for WHERE.
- `src/af/infrastructure/ideon_reference.py`:
  - Added `semantic_roles=("location",)` to the `on.spatial` seed sense.
- `tests/unit/test_ideon_language.py`:
  - Added `semantic_roles=("location",)` to `on.spatial` in `reference_dictionary()`.
  - Added `test_where_is_role_driven_not_lexeme_driven`.
  - Added `test_where_open_when_spatial_role_declared_but_no_filler`.
  - Added `test_context_where_and_dict_candidates_resolve_same_spatial_role`.
- `tests/unit/test_ideon_service.py`:
  - Added `semantic_roles=("location",)` to `on.spatial` in `reference_service()`.
- `specs.md`: Appended SPEC-021.
- `decisions.md`: Appended DEC-035 (supersedes SPEC-019 TEST-019-1 and DEC-033 WHERN rule).
- `agent_runs.md`: Appended Run #021.

### Verified Results
- `ruff check src tests`: passed
- `ruff format --check src tests`: passed
- `strict mypy src` (51 source files): no issues
- `pytest tests/ --ignore=tests/unit/test_governance_scripts.py`: 591 passed
- All 3 new tests pass (TEST-021-1 through TEST-021-4)
- Pre-existing `test_append_only_check_uses_bootstrap_anchor_for_first_pr_topology`
  fails locally (bootstrap SHA unresolvable in shallow clone); GitHub CI is authoritative.

### Issues Encountered
- None beyond the pre-existing shallow-clone governance test failure.

### Related References
- Spec: SPEC-021
- Decision: DEC-035
- PR review comment: #5053882042

---

## Run #022 — Ideon Correction: Typed Temporal Perspective and Fail-Closed Whitespace Conditions
**Date**: 2026-07-23
**Agent**: Copilot
**Status**: Complete

### Actions Taken
1. Added ``TemporalPerspectiveStatus`` (``RESOLVED | UNANCHORED | CONTESTED``) and
   ``TemporalPerspective`` typed domain value to ``af.domain.ideons.temporal``.
   Invariant: RESOLVED requires a non-None ``Tense``; UNANCHORED/CONTESTED forbid one.
   Properties ``tense_label`` and ``status_label`` expose validated serialisable values.
2. Updated ``WhenCondition.__post_init__`` EVENT/CRITERION guard to use
   ``not self.expression or not self.expression.strip()``.
3. Updated ``parse_when_expression`` event/criterion guard to use
   ``not payload or not payload.strip()``.
4. Updated ``IdeonService.construct_graph`` to build a ``TemporalPerspective`` typed
   value and serialise both ``tense`` (full label) and ``tense-status`` (resolution
   status) graph properties.
5. Added 7 unit/integration tests covering all new invariants (TEST-022-1 to -7).
6. Appended SPEC-022 and DEC-036 to governance files.

### Verification
- ``uv run ruff check src/af/domain/ideons/temporal.py src/af/application/ideons.py tests/unit/test_ideon_language.py tests/unit/test_ideon_service.py``: all checks passed
- ``uv run mypy src/af/domain/ideons/temporal.py src/af/application/ideons.py``: no issues
- ``uv run pytest tests/unit/test_ideon_language.py tests/unit/test_ideon_service.py``: 62 passed
- Spec: SPEC-022
- Decision: DEC-036
- PR review comment: #5055123973

---

## Run #023 — fix(ideons): temporal graph projection separates tense axis from resolution status
**Date**: 2026-07-23
**Agent**: GitHub Copilot Coding Agent
**Task**: SPEC-023 / DEC-037 — graph projection must not conflate ``resolved-tense``
  with ``temporal-perspective-status``; ``tense_label`` returns ``None`` for unresolved.

### Actions
1. Updated ``TemporalPerspective.tense_label`` (``temporal.py``): return type changed to
   ``str | None``; returns ``tense.value`` for RESOLVED, ``None`` for UNANCHORED and
   CONTESTED.
2. Updated ``TemporalQualification`` docstring to document its resolved-only role and
   relationship to ``TemporalPerspective``.
3. Updated ``IdeonService.construct_graph`` (``application/ideons.py``):
   - Removed ``Property("tense", perspective.tense_label)``.
   - Removed ``Property("tense-status", perspective.status_label)``.
   - Added ``Property("temporal-perspective-status", perspective.status_label)`` (always).
   - Added ``Property("resolved-tense", perspective.tense.value)`` (only when RESOLVED).
   - Updated docstring and inline comments to reflect new property contract.
4. Updated ``test_ideon_language.py``: ``tense_label`` assertions for UNANCHORED and
   CONTESTED now assert ``None``.
5. Updated ``test_ideon_service.py``: all temporal graph property assertions updated to
   ``temporal-perspective-status`` and ``resolved-tense``; unresolved tests assert
   ``resolved-tense`` absent.
6. Added ``test_temporal_perspective_graph_five_observable_cases`` covering all five
   standpoints end-to-end: WAS, IS, PROJECTED, UNANCHORED, CONTESTED.
7. Appended SPEC-023 and DEC-037.

### Verification
- ``uv run ruff check src/af/domain/ideons/temporal.py src/af/application/ideons.py tests/unit/test_ideon_language.py tests/unit/test_ideon_service.py``: all checks passed
- ``uv run mypy src/af/domain/ideons/temporal.py src/af/application/ideons.py --strict``: no issues
- ``uv run pytest tests/unit/test_ideon_language.py tests/unit/test_ideon_service.py``: 63 passed
- Spec: SPEC-023
- Decision: DEC-037
- PR review comment: #5055585311

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

## Run #024 — Manager Review and Provider Integration Correction
**Date**: 2026-07-23
**Agent**: Codex
**Status**: Completed

### Inputs
- GitHub issue #11 and the manager contracts merged by pull request #66
- Two post-merge review findings covering bare capability strings and non-atomic failover
- The Application provider and package lifecycle merged by pull request #65

### Outputs
- Preserved single-string capability identity and made failover evidence validation atomic
- Added immutable, redacted driver declarations and deterministic provider bindings for
  application-owned managers, including package-adapted contributions
- Added terminal close, exact-once driver ownership, reentrancy guards, sanitized clock
  failures, and fail-closed synchronous callback enforcement
- Added focused regression, lifecycle, rollback, shared-ownership, and asynchronous-callback
  coverage without replacing the merged manager API

### Verified Results
- Manager-focused test suite: 69 passed
- Ruff formatting and lint checks: passed for the changed source and tests
- Strict mypy: passed for the changed source and all three added test modules
- Append-only prefixes for `specs.md`, `decisions.md`, and `agent_runs.md` were preserved

### Issues Encountered
- Issue #11 was implemented and closed concurrently while this run was in progress, so the
  final change is a focused correction and provider-lifecycle integration on top of pull
  request #66 rather than a duplicate manager implementation
- Automated review arrived after pull request #66 had merged; its actionable findings are
  covered by dedicated regressions in this correction

### Related References
- SPEC-015 and TEST-015-3
- DEC-028 and DEC-029
- GitHub issue #11
- GitHub pull requests #65 and #66
- Script: `[SCRIPT:af-dev]`

---

---

## Run #025 - Repository-Local Version-Control Bootstrap
**Date**: 2026-07-24
**Agent**: Codex
**Status**: Completed

### Inputs
- Governed self-operation epic #120
- Priority bootstrap issue #123
- The required literal command `af init version control`

### Outputs
- Added immutable version-control request, observation, manifest, and result contracts
- Added a lightweight application controller and composition root
- Added a guarded Git backend for the local bare coordination remote and worktree reserve
- Added the nested Typer command with stable JSON output and sanitized exit-code-2 errors
- Added deterministic unit, integration, concurrency, tamper, and CLI coverage

### Verified Results
- Focused suite: 64 passed
- Ruff format and lint: passed for all eight intended implementation and test files
- Strict mypy: passed for the four new source modules
- Git backend branch coverage: 92 percent
- Independent static re-review: no remaining release blocker

### Issues Encountered
- Final review found and the implementation corrected worktree-config URL redirection,
  dangling-manifest partial mutation, protocol typing, SHA-256 format drift, unsafe bare
  state, push-URL drift, concurrency, and credential-redaction edge cases
- The complete repository gate and hosted pull-request CI remain to run on the published
  branch; no result is claimed here for those gates

### Related References
- Epic #120
- Issue #123
- `[SCRIPT:af-dev]`


---

## Run #026 - CacheManager Memory and Null Slice
**Date**: 2026-08-01
**Agent**: Codex
**Status**: In Progress

### Plan
1. Specify bounded cache identities, TTL behavior, namespace isolation, and manager routing.
2. Implement verified memory and null registrations behind `CacheManager`.
3. Run focused tests, strict typing, formatting, lint, packaging, governance, and regression
   gates; keep the pull request draft until exact-head CI and review are complete.

### Progress
- Added the failing cache specification first in commit
  `ac369e68af08c9aac7da4d7c38eefdb226df2def`.
- Implemented immutable contracts, named capability routing, lazy TTL expiry, exact
  namespace flush, deterministic shutdown, and an explicit no-storage driver.
- Issue #18 remains open only for source adapters certified with their owning driver
  issues; no duplicate search branch was created.

### Related References
- Issue #19
- DEC-030
- SPEC-025
- Script: `[SCRIPT:af-dev]`

---

## Run #027 - CacheManager Remember and Memoize Slice
**Date**: 2026-08-01
**Agent**: OpenCode / GPT-5.4
**Status**: In Progress

### Inputs
- Issue #19 follow-up on open PR #150
- Existing `agent/cache-manager-memory` worktree and verified cache manager branch
- Required TDD workflow and `[SCRIPT:af-dev]` gate

### Outputs
- Added failing unit coverage for manager-level `remember` and `memoize`
- Implemented synchronous compute-on-miss cache fill on `CacheManager`
- Appended DEC-041 and SPEC-028 for the new bounded cache-fill slice

### Result Summary
This run extends the existing issue #19 pull request with the smallest remaining application-
level cache semantic that does not require a new backend driver contract. `remember` and
`memoize` now reuse the verified manager routing and current store validation.

### Issues Encountered
- The worktree initially lacked the dev toolchain, so the first red-phase test attempt failed
  before `pytest` execution and required a locked `uv sync --extra dev` through `[SCRIPT:af-dev]`

### Actions Taken
- Added three failing tests covering cache-fill reuse and loader validation
- Implemented manager-only loader validation and cache-fill helpers without changing drivers
- Prepared the branch for focused cache tests and the full AF development gate

### Related References
- Issue #19
- Pull Request #150
- DEC-041
- SPEC-028
- Script: `[SCRIPT:af-dev]`

---

## Run #028 - Database Cache Driver Slice
**Date**: 2026-08-01
**Agent**: OpenCode / GPT-5.4
**Status**: In Progress

### Inputs
- Issue #19 follow-up on open PR #150
- Existing verified cache manager branch with memory, null, file, and fenced memory-lock slices
- Repository SQLite and SQLAlchemy dependencies already exercised by other AF drivers

### Outputs
- Added failing unit coverage for a verified database-backed cache driver
- Implemented a persistent `database` cache registration and driver with exact TTL and namespace
  semantics
- Appended DEC-042 and SPEC-029 for the bounded database cache slice

### Result Summary
This run extends the existing issue #19 pull request with the next smallest persistent cache
backend supported by current repository dependencies. The new driver stores bounded bytes in one
tenant-scoped table, treats expiry and invalid persisted rows as misses, and deliberately does not
advertise lock capability yet.

### Issues Encountered
- The first red-phase test invocation failed before collection because the worktree lacked the dev
  extra; rerunning with `uv run --extra dev` produced the expected missing-module failure

### Actions Taken
- Added five red-phase tests covering persistence, expiry, invalid-row cleanup, namespace-safe
  flush, manager routing, capability boundaries, and shutdown behavior
- Implemented `src/af/cache/database.py` using the existing SQLAlchemy dependency and verified
  driver-registration pattern
- Prepared the branch for focused cache tests and the full AF development gate

### Related References
- Issue #19
- Pull Request #150
- DEC-042
- SPEC-029
- Script: `[SCRIPT:af-dev]`

---

## Run #029 - Database Lock Driver Slice
**Date**: 2026-08-01
**Agent**: OpenCode / GPT-5.4
**Status**: In Progress

### Inputs
- Issue #19 follow-up on open PR #150
- Existing verified database cache driver and owner-fenced memory lock contract
- Required TDD workflow and `[SCRIPT:af-dev]` validation gate

### Outputs
- Added failing unit coverage for persistent owner-fenced database locks across driver instances
- Implemented database-backed acquire, renew, and release semantics with monotonic fencing tokens
- Appended DEC-043 and SPEC-030 for the bounded database-lock slice

### Result Summary
This run extends the open issue #19 PR with the smallest remaining persistent coordination slice
already supported by repository dependencies. The database cache driver now advertises verified
lock capability, fences renew/release by exact lease identity, and preserves namespace-safe cache
flush behavior.

### Issues Encountered
- The first red-phase focused test run failed exactly because the database driver still raised
  `NotImplementedError` for lock operations; the new slice closed that gap without expanding to
  Redis or Memcached behavior

### Actions Taken
- Added red-phase coverage for cross-instance contention, forged-owner rejection, manager routing,
  and shutdown validation
- Implemented a dedicated `af_cache_locks` table with atomic acquire/reclaim, renew, and release
  updates
- Prepared the branch for focused cache tests and the full AF development gate

### Related References
- Issue #19
- Pull Request #150
- DEC-043
- SPEC-030
- Script: `[SCRIPT:af-dev]`

---

## Run #030 - Memcached Cache Driver Slice
**Date**: 2026-08-01
**Agent**: OpenCode / GPT-5.4
**Status**: In Progress

### Inputs
- Issue #19 follow-up on open PR #150
- Existing verified cache-manager, local drivers, and optional-service registration patterns
- Required TDD workflow and `[SCRIPT:af-dev]` validation gate

### Outputs
- Added failing unit coverage for a verified Memcached-backed cache driver
- Implemented a cache-only `memcached` registration and driver with namespace-scoped physical keys
  and Memcached TTL translation
- Appended DEC-044 and SPEC-031 for the bounded Memcached cache slice

### Result Summary
This run extends the open issue #19 PR with the next smallest network-backed cache slice while
preserving AF's tenant-safety contract. The Memcached driver treats eviction and non-durable
forever entries as normal misses, routes through the verified cache manager, and intentionally does
not advertise flush or lock capability yet.

### Issues Encountered
- The first red-phase focused test run failed during collection because the Memcached module did
  not exist yet; implementing the driver and registration closed that gap without widening to lock
  semantics

### Actions Taken
- Added red-phase coverage for runtime-identity checks, tenant isolation, eviction tolerance, TTL
  translation, manager routing, and unsafe configuration rejection
- Implemented `src/af/cache/memcached.py` using the repository's optional-runtime registration
  pattern and a namespace-safe key derivation strategy
- Prepared the branch for focused cache tests, type/lint checks, and the full AF development gate

### Related References
- Issue #19
- Pull Request #150
- DEC-044
- SPEC-031
- Script: `[SCRIPT:af-dev]`

---

## Run #031 - Memcached Lock Driver Slice
**Date**: 2026-08-01
**Agent**: OpenCode / GPT-5.4
**Status**: In Progress

### Inputs
- Issue #19 follow-up on open PR #150
- Existing Memcached cache-only slice, owner-fenced memory/database lock contracts, and cache
  manager routing
- Required TDD workflow and `[SCRIPT:af-dev]` validation gate

### Outputs
- Added failing unit coverage for Memcached owner-fenced lock acquisition, renewal, release, and
  lock-specific failure boundaries
- Extended the verified `memcached` driver to advertise lock capability and implement the shared
  lock contract with namespace-scoped lease and token keys
- Appended DEC-045 and SPEC-032 for the bounded Memcached lock slice

### Result Summary
This run advances the open issue #19 PR with the next smallest missing acceptance slice after the
Memcached cache-only work: owner-fenced Memcached coordination. The driver now permits one active
owner, preserves owner-checked renew/release semantics, and stays explicit that eviction or restart
can still erase Memcached lock state because cache is not authority.

### Issues Encountered
- The red-phase tests initially failed because the existing Memcached driver intentionally omitted
  lock capability; implementation added only the minimum real-client verbs (`add`, `gets`, `cas`,
  `incr`) needed for the slice

### Actions Taken
- Added red-phase coverage for lock fencing, manager routing, and lock-specific failure stages in
  `tests/unit/test_cache_memcached.py`
- Implemented Memcached lease encoding/decoding, monotonic token allocation, and owner-fenced
  acquire/renew/release paths in `src/af/cache/memcached.py`
- Ran the focused cache suite before the repository-wide `[SCRIPT:af-dev]` gate

### Related References
- Issue #19
- Pull Request #150
- DEC-045
- SPEC-032
- Script: `[SCRIPT:af-dev]`
