# Specifications

**NOTE: This file is append-only. Do not modify or remove existing entries. Only add new specifications at the end.**

---

## Specification Format

Each specification entry MUST follow this format:

```
## [SPEC-XXX] Specification Title
**Date**: YYYY-MM-DD
**Status**: Draft | Approved | Implemented | Deprecated
**Priority**: MUST | SHOULD | MAY

### Description
Detailed description of the specification.

### Requirements
- MUST requirement 1
- MUST requirement 2
- SHOULD requirement 1
- MAY requirement 1

### Tests
- **Test ID**: TEST-XXX-1
  - **Description**: What this test validates
  - **Pass Criteria**: Specific conditions for pass
  - **Fail Criteria**: Specific conditions for fail

### Related Specs
- Links to related specifications

---
```

## Initial Specifications

---

## [SPEC-001] Agent Factory File Structure
**Date**: 2026-01-28
**Status**: Approved
**Priority**: MUST

### Description
The Agent Factory repository MUST maintain a flat file structure for all agent-related files. This ensures simplicity, ease of navigation, and prevents over-engineering of the directory hierarchy.

### Requirements
- MUST use flat file structure (single level depth maximum)
- MUST NOT create nested subdirectories for agent files
- MUST store all agent definition files in root or single `agents/` directory
- SHOULD use consistent naming conventions for agent files

### Tests
- **Test ID**: TEST-001-1
  - **Description**: Verify no nested directories beyond one level
  - **Pass Criteria**: `find agents/ -mindepth 2 -type f` returns no results
  - **Fail Criteria**: Any files found at depth > 1

- **Test ID**: TEST-001-2
  - **Description**: Verify agent files exist at correct location
  - **Pass Criteria**: All agent file paths in agents.yaml resolve to existing files
  - **Fail Criteria**: Any file_path in agents.yaml does not exist

### Related Specs
- SPEC-002 (Agent File Format)

---

## [SPEC-002] Agent File Format and Required Headings
**Date**: 2026-01-28
**Status**: Approved
**Priority**: MUST

### Description
All agent files MUST follow a standardized format with required headings to ensure consistency and completeness of documentation.

### Requirements
- MUST include heading: "## Purpose"
- MUST include heading: "## Inputs"
- MUST include heading: "## Outputs"
- MUST include heading: "## Behavior"
- MUST include heading: "## Constraints"
- MUST have headings in the order listed above
- SHOULD include examples and usage documentation
- MAY include additional headings for supplementary information

### Tests
- **Test ID**: TEST-002-1
  - **Description**: Verify all required headings exist in agent file
  - **Pass Criteria**: All five required headings found in correct order
  - **Fail Criteria**: Any required heading missing or out of order

- **Test ID**: TEST-002-2
  - **Description**: Verify heading format follows markdown H2 convention
  - **Pass Criteria**: All required headings use `## ` prefix
  - **Fail Criteria**: Incorrect heading level used

### Related Specs
- SPEC-001 (File Structure)
- SPEC-003 (Tags and Metadata)

---

## [SPEC-003] Agent Tags and Metadata
**Date**: 2026-01-28
**Status**: Approved
**Priority**: MUST

### Description
All agents MUST be tagged with at least one tag from the approved list in agents.yaml to enable categorization and discovery.

### Requirements
- MUST have at least one tag in agents.yaml definition
- MUST use tags from the allowed_tags list in agents.yaml
- MUST have unique agent ID
- SHOULD have version number following semantic versioning
- MAY include custom metadata fields

### Tests
- **Test ID**: TEST-003-1
  - **Description**: Verify each agent has at least one tag
  - **Pass Criteria**: All agents in agents.yaml have tags array with length >= 1
  - **Fail Criteria**: Any agent has empty or missing tags array

- **Test ID**: TEST-003-2
  - **Description**: Verify agent IDs are unique
  - **Pass Criteria**: No duplicate IDs in agents.yaml
  - **Fail Criteria**: Duplicate IDs found

- **Test ID**: TEST-003-3
  - **Description**: Verify tags are from allowed list
  - **Pass Criteria**: All tags used are in allowed_tags list
  - **Fail Criteria**: Any tag not in allowed_tags list

### Related Specs
- SPEC-002 (Agent File Format)

---

## [SPEC-004] Append-Only File Management
**Date**: 2026-01-28
**Status**: Approved
**Priority**: MUST

### Description
Certain files in the Agent Factory (specs.md, agent_runs.md, decisions.md) MUST be append-only to maintain historical record and prevent loss of information.

### Requirements
- MUST NOT delete content from append-only files
- MUST NOT modify existing entries in append-only files
- MUST only add new entries at the end of append-only files
- SHOULD include timestamp with each new entry
- SHOULD include clear separation between entries

### Tests
- **Test ID**: TEST-004-1
  - **Description**: Verify no content deletion in append-only files
  - **Pass Criteria**: Git diff shows only additions to specs.md, agent_runs.md, decisions.md
  - **Fail Criteria**: Git diff shows deletions or modifications to existing content

- **Test ID**: TEST-004-2
  - **Description**: Verify new entries are added at end of file
  - **Pass Criteria**: All changes are additions after the last existing entry
  - **Fail Criteria**: Changes appear in middle of file

### Related Specs
- SPEC-005 (No Fabrication Rule)

---

## [SPEC-005] No Fabrication of Citations and Results
**Date**: 2026-01-28
**Status**: Approved
**Priority**: MUST

### Description
Agent documentation and results MUST NOT contain fabricated citations, data, or results. All information MUST be verifiable and accurate.

### Requirements
- MUST NOT fabricate citations or references
- MUST NOT invent test results or data
- MUST provide verifiable sources for all claims
- SHOULD link to original sources when referencing external information
- MAY include placeholder text if data is pending, clearly marked as such

### Tests
- **Test ID**: TEST-005-1
  - **Description**: Manual review of citations and references
  - **Pass Criteria**: All citations can be verified and traced to source
  - **Fail Criteria**: Any citation cannot be verified or is fabricated

- **Test ID**: TEST-005-2
  - **Description**: Verify test results match actual test output
  - **Pass Criteria**: Documented test results match actual test execution output
  - **Fail Criteria**: Results do not match or appear fabricated

### Related Specs
- SPEC-004 (Append-Only Files)

---

## [SPEC-006] Markdown Output Preference
**Date**: 2026-01-28
**Status**: Approved
**Priority**: SHOULD

### Description
All agent outputs, documentation, and reports SHOULD use Markdown format for consistency and readability.

### Requirements
- SHOULD use Markdown for all documentation
- SHOULD use standard Markdown syntax (CommonMark or GitHub Flavored Markdown)
- SHOULD use code blocks with language specification for code examples
- MAY use extended Markdown features where supported

### Tests
- **Test ID**: TEST-006-1
  - **Description**: Verify documentation files use .md extension
  - **Pass Criteria**: All agent documentation files use .md extension
  - **Fail Criteria**: Documentation files use other formats

- **Test ID**: TEST-006-2
  - **Description**: Verify Markdown syntax validity
  - **Pass Criteria**: Files parse correctly with Markdown parser
  - **Fail Criteria**: Syntax errors in Markdown files

### Related Specs
- SPEC-002 (Agent File Format)

---

## [SPEC-007] Flexible Directory Structure
**Date**: 2026-01-29
**Status**: Approved
**Priority**: SHOULD
**Supersedes**: SPEC-001

### Description
The Agent Factory repository SHOULD use a flexible directory structure that allows for logical organization while maintaining compatibility with major AI platforms including GitHub Copilot, OpenAI ChatGPT, Google Gemini, agent-based IDEs (OpenCode.ai), and Google Colab.

### Requirements
- SHOULD organize agent files in the `agents/` directory or subdirectories
- MAY use nested subdirectories for logical grouping (e.g., by role, domain, priority)
- SHOULD use self-documenting directory names
- MUST still use agents.yaml as the central registry for all agents
- SHOULD follow conventions compatible with major AI agent platforms
- MAY organize specialisms in nested subdirectories under `specialisms/`

### Examples of Allowed Structures
```
agents/
├── core/
│   ├── Architect.md
│   ├── Builder.md
│   └── ProjectManager.md
├── quality/
│   ├── Tester.md
│   ├── SecurityReviewer.md
│   └── Skeptic.md
└── documentation/
    ├── Editor.md
    └── DocWriter.md
```

Or flat structure (still allowed):
```
agents/
├── Architect.md
├── Builder.md
├── Tester.md
└── ...
```

Or mixed:
```
agents/
├── Architect.md
├── Builder.md
├── testing/
│   ├── Tester.md
│   └── test_utilities/
│       └── helpers.md
└── security/
    └── SecurityReviewer.md
```

### Tests
- **Test ID**: TEST-007-1
  - **Description**: Verify all agent files referenced in agents.yaml exist
  - **Pass Criteria**: All file_path entries in agents.yaml resolve to existing files
  - **Fail Criteria**: Any file_path in agents.yaml does not exist

- **Test ID**: TEST-007-2
  - **Description**: Verify agents/ directory exists
  - **Pass Criteria**: agents/ directory exists in repository root
  - **Fail Criteria**: agents/ directory is missing

### Related Specs
- SPEC-001 (Deprecated by this spec)
- SPEC-002 (Agent File Format) - still applies
- SPEC-003 (Tags and Metadata) - still applies

### Migration Notes
- Existing flat structure is still valid and allowed
- Projects may gradually reorganize into nested structures
- agents.yaml remains the authoritative registry
- No breaking changes to existing agent files

---

## [SPEC-001] Agent Factory File Structure (DEPRECATED)
**Date**: 2026-01-28
**Status**: Deprecated
**Deprecated Date**: 2026-01-29
**Superseded By**: SPEC-007
**Priority**: N/A (was MUST)

### Deprecation Notice
This specification has been superseded by SPEC-007 (Flexible Directory Structure). The flat file structure requirement has been removed to allow for better organization and compatibility with major AI agent platforms including GitHub Copilot, OpenAI ChatGPT, Google Gemini, agent-based IDEs (OpenCode.ai), and Google Colab.

**Original Description**: The Agent Factory repository MUST maintain a flat file structure for all agent-related files.

**Why Deprecated**: The rigid flat structure constraint was limiting organization flexibility and was not aligned with conventions used by major AI agent platforms. The new flexible structure (SPEC-007) allows for both flat and nested organizations while maintaining compatibility and discoverability through agents.yaml.

---

## [SPEC-008] Script-First Automation
**Date**: 2026-02-07
**Status**: Implemented
**Priority**: MUST

### Description
Repeatable work MUST be resolved to registered, parameterized automation before it is
performed manually. This specification formalizes the script-first policy already
implemented by DEC-018 through DEC-021 and the NL-to-script workflow.

### Requirements
- MUST query `scripts/registry.yaml` before repeatable execution
- MUST create and register a reusable script when no suitable route exists
- MUST parameterize variable inputs rather than hard-code one request
- MUST default mutating automation to dry-run unless execution is explicit
- MUST avoid shell interpolation of untrusted input in new automation
- SHOULD make validation and idempotency checks executable

### Tests
- **Test ID**: TEST-008-1
  - **Description**: Verify repeatable task documentation references a registered script
  - **Pass Criteria**: `scripts/check_repeatable_script_refs.py` exits zero
  - **Fail Criteria**: A repeatable workflow has no script or registry reference
- **Test ID**: TEST-008-2
  - **Description**: Verify registry schema and required capabilities
  - **Pass Criteria**: `scripts/validate_registry.py` exits zero
  - **Fail Criteria**: A registry entry is incomplete or malformed
- **Test ID**: TEST-008-3
  - **Description**: Verify explicit execution gating
  - **Pass Criteria**: `scripts/check_dry_run_conventions.py` exits zero
  - **Fail Criteria**: An execute-intent script runs mutations without an explicit gate

### Related Specs
- SPEC-005 (No Fabrication of Citations and Results)
- SPEC-007 (Flexible Directory Structure)

---

## [SPEC-009] Executable AF Python Runtime
**Date**: 2026-07-18
**Status**: Approved
**Priority**: MUST

### Description
AgentFactory SHALL provide an executable AF runtime in idiomatic Python. AF is
classification-heavy and inference-light: agents coordinate replaceable capabilities;
models are dependency-injected providers and are never the agent itself.

### Requirements
- MUST target Python 3.12 or newer with a PEP 517 `pyproject.toml` and `src` layout
- MUST implement `classify → route → execute → verify → record → test → reuse`
- MUST route registered static and procedural work before any explicitly installed
  generative provider
- MUST expose an explicit routing policy optimized for one or two of cheapest, fastest,
  and smartest; it MUST reject a policy claiming all three equally
- MUST express agent, classifier, procedure, verifier, recorder, repository, reuse, and
  model boundaries as typed contracts
- MUST use explicit constructor or composition-root dependency injection
- MUST demonstrate class inheritance, Python mixin traits, public/private naming, and
  singleton/transient service scopes without deep inheritance or implicit autowiring
- MUST implement a static Hello World route
- MUST parse `how many rs in strawberry` into a parameterized character-count intent and
  return `3` by iterating through the word; it MUST NOT call a model or hard-code that answer
- MUST independently verify character counts and fail closed on verification failure
- MUST regression-replay pure, safe procedures before promoting their results for reuse
- MUST verify reused results before returning them
- MUST provide a shared Task CRUD application service and thin CLI, HTML MVC, REST,
  GraphQL, and MCP adapters
- MUST support SQLite persistence and in-memory test doubles behind repository contracts
- MUST support MCP over stdio and Streamable HTTP; public ChatGPT use requires a separately
  deployed HTTPS endpoint and MUST NOT be implied by local execution alone
- MUST provide test-first behavioural coverage, formatting, linting, typing, and legacy
  AgentFactory validation
- MUST preserve append-only governance files and the existing agent registry

### Tests
- **Test ID**: TEST-009-1
  - **Description**: Verify deterministic strawberry execution
  - **Pass Criteria**: CLI and core tests return `3`, verified, without model invocation
  - **Fail Criteria**: Output differs, is unverified, is hard-coded, or invokes a model
- **Test ID**: TEST-009-2
  - **Description**: Verify the AF lifecycle and reuse gate
  - **Pass Criteria**: Trace stages are ordered and a second identical request is reused
    only after replay testing; both results are independently verified
  - **Fail Criteria**: A stage is skipped, reordered, or untested output is reused
- **Test ID**: TEST-009-3
  - **Description**: Verify transport parity and DRY boundaries
  - **Pass Criteria**: CLI, HTML, REST, GraphQL, and MCP delegate to the shared controller;
    REST and GraphQL operate on the same Task repository
  - **Fail Criteria**: A transport duplicates counting, routing, verification, or persistence
- **Test ID**: TEST-009-4
  - **Description**: Verify CRUD semantics
  - **Pass Criteria**: Create, list, get, update/re-execute, and delete pass for REST,
    GraphQL, MCP, and repository contract tests
  - **Fail Criteria**: Any adapter has divergent persisted state or status semantics
- **Test ID**: TEST-009-5
  - **Description**: Verify all quality gates
  - **Pass Criteria**: pytest, Ruff formatting/linting, mypy, and `validate_agents.sh` pass
  - **Fail Criteria**: Any required quality command exits non-zero

### Related Specs
- SPEC-004 (Append-Only File Management)
- SPEC-005 (No Fabrication of Citations and Results)
- SPEC-007 (Flexible Directory Structure)
- SPEC-008 (Script-First Automation)

---

## [SPEC-010] AgentFramework Repository Identity
**Date**: 2026-07-18
**Status**: Approved
**Priority**: MUST

### Description
The executable AF runtime SHALL live in the independent `AgentFramework` repository.
The original AgentFactory definitions, validators, and append-only records are imported
foundations, not the canonical repository identity for new framework work.

### Requirements
- MUST use `alexanderholman/AgentFramework` as the canonical GitHub repository
- MUST use `agent-framework` as the project and repository manifest identifier
- MUST preserve imported AgentFactory entries in append-only history
- MUST link new issues, branches, pull requests, and installation instructions to
  AgentFramework
- MUST remain private until the owner explicitly changes repository visibility

### Tests
- **Test ID**: TEST-010-1
  - **Description**: Validate the canonical repository manifest and project context
  - **Pass Criteria**: Both identify `agent-framework` and reference existing files
  - **Fail Criteria**: Active manifests identify the legacy repository
- **Test ID**: TEST-010-2
  - **Description**: Validate active setup links
  - **Pass Criteria**: Installation and contribution links target AgentFramework
  - **Fail Criteria**: New work is directed to AgentFactory

### Related Specs
- SPEC-004 (Append-Only File Management)
- SPEC-009 (Executable AF Python Runtime)

---

## [SPEC-011] Human-First Constrained Selection Boundary
**Date**: 2026-07-18
**Status**: Approved
**Priority**: MUST

### Description
AF SHALL remain deterministic-first and human-first. Static and procedural routes execute
before any selector. A selector may only propose one installed typed procedure from the
trusted registered catalogue; it never provides the user-facing answer.

### Requirements
- MUST execute deterministic classifiers and routes before consulting any selector
- MUST keep selector output constrained to a registered procedure ID, supported intent,
  typed arguments, and finite confidence
- MUST validate selector proposals against the immutable registered catalogue and
  per-procedure argument schema before execution
- MUST record only fixed reason codes for selector handoff and policy-denial traces
- MUST enforce pre-execution policy checks, including remote denial and no silent fallback
- MUST keep direct model-generated-answer execution disabled in the active runtime
- MUST enforce the shared 10,000-character input ceiling across CLI, HTML, REST, GraphQL,
  MCP, and typed controller entry points
- MUST independently verify deterministic procedure output before return or reuse promotion

### Tests
- **Test ID**: TEST-011-1
  - **Description**: Validate deterministic routes bypass selectors
  - **Pass Criteria**: `hello` and strawberry character-count execute without selector calls
  - **Fail Criteria**: Deterministic requests invoke selector infrastructure
- **Test ID**: TEST-011-2
  - **Description**: Validate constrained selector proposals fail closed
  - **Pass Criteria**: Invalid metadata, IDs, intents, arguments, confidence, grounding,
    and policy checks record fixed reason codes with zero execution
  - **Fail Criteria**: Malformed or unsupported selector proposals execute work or leak raw
    provider data
- **Test ID**: TEST-011-3
  - **Description**: Validate typed character-count entry points preserve arguments
  - **Pass Criteria**: MCP/controller character counting preserves exact text/character data
    up to 10,000 characters and bypasses selector invocation
  - **Fail Criteria**: The adapter reconstructs natural language, corrupts typed data, or
    bypasses shared limits

### Related Specs
- SPEC-004 (Append-Only File Management)
- SPEC-009 (Executable AF Python Runtime)
- SPEC-010 (AgentFramework Repository Identity)

---

## [SPEC-012] Capability Ladder and Scoped Generative Execution
**Date**: 2026-07-21
**Status**: Approved
**Priority**: MUST

### Description
AF SHALL select the least costly available capability that satisfies declared requirements
and the required quality threshold. Algorithms are functions; ladder levels distinguish
simple functions from increasingly complex systems and compositions of functions.

### Capability Ladder
0. cached result or exact verified reuse
1. simple deterministic function
2. complex deterministic function system, including traditional algorithms and
   deterministic DAGs
3. specialist model
4. adaptive agent workflow composed from registered capabilities
5. large reasoning or generative model
6. human clarification or external expertise

### Requirements
- MUST carry a typed user-acceptance scope on every execution request
- MUST enforce the scope's maximum ladder level, review policy, and reuse policy before
  capability selection and again before result release or promotion
- MUST select minimum cost only among capabilities meeting required quality and all hard
  requirements
- MUST include latency, compute/energy, monetary cost, privacy exposure, and operational
  risk in capability cost evidence
- MUST include accuracy, availability, provenance, and verified execution history in
  capability quality evidence
- MUST treat cached results as reusable only when their capability fingerprint, inputs,
  provenance, review evidence, expiry, and scope remain valid
- MUST constrain planner output to a typed DAG of installed registered capabilities and
  validate every node, edge, argument, requirement, and policy before execution
- MUST require review before releasing a model-generated answer
- MUST NOT equate review approval with cache permission; scope and system requirements may
  require `no-store`
- MUST keep the active runtime's direct generative route disabled until these controls and
  their tests are implemented

### Initial Acceptance Scopes
- `child`: maximum level 2; deterministic procedural responses only; no generative model,
  generative planner, or generated answer
- `deterministic`: maximum level 2 unless the caller explicitly selects a more permissive
  scope
- `reviewed-generative`: levels 3 through 5 may be considered only when lower levels cannot
  satisfy quality and requirements; generated answers require review before release
- `operator-evaluation`: explicit model or hybrid evaluation without implicit production
  escalation

### Tests
- **Test ID**: TEST-012-1
  - **Description**: Enforce ladder ordering and quality constraints
  - **Pass Criteria**: AF selects the lowest-cost qualifying capability and never climbs
    when a lower level satisfies requirements and required quality
  - **Fail Criteria**: AF selects by cost without quality or skips a valid lower level
- **Test ID**: TEST-012-2
  - **Description**: Enforce child scope
  - **Pass Criteria**: Child requests execute only level 0 through 2 deterministic
    procedures and reject all generative and planner paths
  - **Fail Criteria**: Any child request invokes or returns generative AI output
- **Test ID**: TEST-012-3
  - **Description**: Enforce reviewed generative release
  - **Pass Criteria**: Generated answers remain withheld until review evidence is valid
  - **Fail Criteria**: Unreviewed generated text reaches the user
- **Test ID**: TEST-012-4
  - **Description**: Enforce scoped reuse
  - **Pass Criteria**: `no-store`, expiry, provenance, fingerprint, and review requirements
    prevent invalid cache promotion or reuse
  - **Fail Criteria**: Review alone causes unconditional caching or stale reuse

### Related Specs
- SPEC-009 (Executable AF Python Runtime)
- SPEC-011 (Human-First Constrained Selection Boundary)

---
