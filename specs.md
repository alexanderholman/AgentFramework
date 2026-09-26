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

## [SPEC-013] Acceptance Authority, Derivation Taint, and Ladder Selection
**Date**: 2026-07-21
**Status**: Approved
**Priority**: MUST

### Description
This specification clarifies SPEC-012. Capability level is the primary ordering criterion;
cost is optimized only within the lowest level that satisfies quality, requirements, and
effective acceptance policy. Acceptance scope is trusted policy, not caller-controlled
metadata. Caching never lowers the authority requirements of the capability or content from
which a result was derived.

### Requirements
- MUST select capabilities lexicographically by `(lowest qualifying level, cost)` after
  applying quality, requirements, availability, and effective-scope constraints
- MUST derive effective acceptance scope as the intersection of the caller's requested
  scope and trusted server-side identity, session, tenant, parental, and delegation policy
- MUST NOT permit a caller, child context, delegated request, planner, or cached artifact to
  widen its effective scope
- MUST permit level-6 human clarification or handoff from every scope as a non-automated
  safety outcome, without treating level 6 as permission for automated output
- MUST retain originating capability level, model-derived taint, audience/tenant boundary,
  policy version, and child eligibility on every cached or composed result
- MUST NOT reclassify a level-3 through level-5 derived artifact as unrestricted level-0
  content merely because it was cached
- MUST propagate model-derived taint through planner arguments, DAG nodes, deterministic
  wrappers, transformations, summaries, and final responses
- MUST review every user-visible derivative of model-generated content unless a stricter
  scope rejects it outright
- MUST bind review evidence to the exact output digest, derivation graph, effective scope,
  authorized reviewer identity and independence class, policy version, timestamp, expiry,
  and revocation state
- MUST reject changed, expired, revoked, cross-scope, cross-tenant, or insufficiently
  independent review evidence

### Scope Gate Details
- `child`: automated levels 0 through 2 only; origin and derivation must also be levels 0
  through 2; no model-derived cached artifact; human handoff remains available; reuse is
  limited to child-eligible deterministic artifacts within the same authorized audience
- `deterministic`: automated levels 0 through 2 only; no model-derived cached artifact;
  human handoff remains available; reuse requires deterministic provenance and scope match
- `reviewed-generative`: automated levels up to 5 after lower-level insufficiency is proven;
  user-visible model derivatives require bound review evidence; default reuse is `no-store`
  unless an explicit policy permits promotion
- `operator-evaluation`: automated levels up to 5 for explicit testing; no production
  release, no cross-user reuse, and `no-store` by default; human handoff remains available

### Tests
- **Test ID**: TEST-013-1
  - **Description**: Prevent scope escalation
  - **Pass Criteria**: Effective scope never exceeds trusted authority and delegation can
    only preserve or narrow policy
  - **Fail Criteria**: Caller input, child context, planner, or delegation widens scope
- **Test ID**: TEST-013-2
  - **Description**: Prevent cache laundering
  - **Pass Criteria**: Cached results retain origin level and derivation taint and remain
    unavailable to ineligible child, tenant, or audience contexts
  - **Fail Criteria**: Caching converts model-derived content into unrestricted level 0
- **Test ID**: TEST-013-3
  - **Description**: Enforce derivative review
  - **Pass Criteria**: Planner arguments and deterministic wrappers preserve model taint and
    every user-visible derivative requires valid digest-bound review evidence
  - **Fail Criteria**: A wrapper or planner path bypasses review
- **Test ID**: TEST-013-4
  - **Description**: Preserve human-first handoff
  - **Pass Criteria**: Every scope can return a non-automated human clarification or handoff
    without enabling a higher automated level
  - **Fail Criteria**: A scope ceiling blocks safe handoff or handoff enables automation

### Related Specs
- SPEC-011 (Human-First Constrained Selection Boundary)
- SPEC-012 (Capability Ladder and Scoped Generative Execution)

---

## [SPEC-014] Planner Side-Effect Authorization
**Date**: 2026-07-21
**Status**: Approved
**Priority**: MUST

### Description
Reviewing a final answer cannot undo a side effect already caused by a model-generated plan.
Planner execution is therefore pure, read-only, and no-risk by default. Any side-effecting,
elevated-risk, or confirmation-requiring node needs independent authorization before that
node executes.

### Requirements
- MUST classify every planned node for purity, side effects, risk level, reversibility,
  required authority, resource budget, and confirmation requirements before execution
- MUST permit an unreviewed planner proposal to execute only pure, read-only, no-risk nodes
  whose registered capability policy explicitly allows planner invocation
- MUST require independent pre-execution authorization for every side-effecting,
  elevated-risk, irreversible, externally visible, or confirmation-requiring node
- MUST bind authorization to the immutable plan digest, exact node and arguments, predicted
  effects, effective scope, authorizer identity and authority, policy version, expiry, and
  revocation state
- MUST invalidate authorization when a planner changes a node, argument, edge, effect,
  capability version, scope, or execution order
- MUST pause adaptive replanning before every newly introduced or changed side effect and
  require fresh authorization
- MUST preserve existing selector denial for side-effecting, risky, or
  confirmation-requiring capabilities until this gate is implemented and tested

### Tests
- **Test ID**: TEST-014-1
  - **Description**: Deny unreviewed planner side effects
  - **Pass Criteria**: Unreviewed plans execute only pure read-only no-risk nodes
  - **Fail Criteria**: A planner triggers any side effect before authorization
- **Test ID**: TEST-014-2
  - **Description**: Bind authorization to an immutable plan
  - **Pass Criteria**: Any change to plan, node, arguments, effects, scope, capability
    version, or policy invalidates authorization
  - **Fail Criteria**: Modified or replayed authorization permits execution
- **Test ID**: TEST-014-3
  - **Description**: Reauthorize adaptive changes
  - **Pass Criteria**: Adaptive replanning pauses before a changed or new side effect
  - **Fail Criteria**: Previously authorized plans can introduce unreviewed effects

### Related Specs
- SPEC-011 (Human-First Constrained Selection Boundary)
- SPEC-012 (Capability Ladder and Scoped Generative Execution)
- SPEC-013 (Acceptance Authority, Derivation Taint, and Ladder Selection)

---

## [SPEC-015] Application Platform Lifecycle and Extension Contracts
**Date**: 2026-07-22
**Status**: Approved
**Priority**: MUST

### Description
AF is a Python-native, Laravel-inspired application platform. Providers, packages, managers,
drivers, kernels, and units of work share explicit lifecycle, ownership, context, error,
capability, data, and delivery contracts.

### Requirements

#### Sync, async, and lifecycle
- MUST declare public operations as synchronous or asynchronous and MUST NOT start hidden
  event loops or block an active event loop
- MUST make external I/O, broker, remote-data, worker, streaming, boot, and shutdown contracts
  asynchronous by default while preserving deterministic synchronous functions behind
  explicit adapters
- MUST permit one sync-to-async bridge only at an owning process entry point
- MUST propagate cancellation and shutdown rather than sanitize or retry them as normal errors
- MUST use `construct → register all → boot all → serve → drain → shutdown` ordering
- MUST make provider registration declaration-only: no external I/O, service resolution,
  construction, or side effects beyond recording bindings and metadata
- MUST topologically validate provider dependencies, freeze the validated graph, and boot in
  topological dependency order
- MUST require the currently booting provider to register acquired resources transactionally
  as they are acquired so its own partial failure can be cleaned before earlier providers
- MUST roll back partial boot and dispose the failing provider plus previously booted providers
  and resources exactly once in reverse order
- MUST make shutdown idempotent and cancellation-safe: bounded protected cleanup continues
  after cancellation or one cleanup failure, aggregates evidence, then re-raises the original
  cancellation after unwinding

#### Lifetimes, context, and errors
- MUST distinguish container singleton, application, invocation, and transient lifetimes
- MUST type invocation scopes as request, message, command, job, event, schedule,
  notification, or interaction turn while retaining common ownership rules
- MUST create and dispose one fresh invocation scope per unit of work
- MUST reject resolution or context access through a disposed scope
- MUST own tasks spawned by an invocation and join or cancel them before scope disposal;
  explicitly detached work receives a newly derived context and no scoped dependencies
- MUST carry immutable principal, tenant, requested/effective acceptance scope, correlation,
  causation, trace, deadline, locale, and cancellation through context-local state
- MUST NOT retain invocation context, transactions, authentication state, or mutable driver
  state in singleton/application services
- MUST distinguish configuration, resolution, capability, validation, authorization,
  transport, transient, permanent, integrity, cancellation, and shutdown failures internally
- MUST expose stable sanitized reason codes while retaining typed protected evidence

#### Providers, managers, drivers, and packages
- MUST use constructor/factory injection as the canonical dependency mechanism
- MUST keep static facades and service-location convenience APIs optional and outside domain
  code
- MUST discover inert package metadata without importing provider/package modules
- MUST let reviewable metadata declare providers, compatibility, dependencies, configuration,
  resources, migrations, commands, routes, events, and claimed capabilities
- MUST let consumers disable discovery and MUST verify compatibility and explicit approval
  before importing or instantiating package code
- MUST bind package approval, manifests, probes, and conformance evidence to the immutable
  artifact digest, version, dependency-graph digest, configuration digest, deployment profile,
  policy version, evidence timestamp, expiry, and revocation state
- MUST revalidate package/driver evidence before import and use when identity, configuration,
  dependencies, profile, policy, expiry, revocation, or probed runtime state changes
- MUST resolve replaceable subsystems through `Manager → named connection → Driver` contracts
- MUST validate typed configuration and capabilities before use through trusted static
  manifests plus independent runtime probes and conformance evidence; driver self-report alone
  MUST NOT authorize safety-, tenancy-, durability-, or consistency-critical semantics
- MUST represent optional transactions, locks, tags, delay, priorities, replay, aliases,
  vectors, signed URLs, and streaming as capabilities rather than vendor-name checks
- MUST NOT silently fall back to a weaker/local production driver; explicit composite
  fallback/failover/load-balancing policy retains visible degradation evidence

#### Data and delivery semantics
- MUST define capability-specific relational, document, graph, search, vector, cache, blob,
  event, and time-series contracts rather than one false universal query abstraction
- MUST gate native extensions explicitly and preserve stable identity, provenance, tenancy,
  policy, and authoritative-source references across projections
- MUST limit ACID claims to one supporting connection and use outbox/inbox, idempotent
  projection, or explicit saga/compensation across stores
- MUST persist an outbox record atomically in the same authoritative transaction as its state
  change before a post-commit relay publishes it
- MUST commit a consumer's business state plus inbox/deduplication state atomically when they
  share a transaction; external effects require a durable transactional intent/outbox before
  acknowledgement
- MUST distinguish jobs, domain/application events, integration events, broker pub-sub,
  client broadcast, and notification intents
- MUST treat queue handlers and durable consumers as at-least-once unless explicitly weaker
- MUST NOT claim exactly-once execution/delivery; exactly-once business effects require a
  declared idempotency and deduplication boundary
- MUST acknowledge work after required durable effects and publish observable work only after
  the authoritative transaction commits
- MUST document ordering, replay, retention, consumer groups, acknowledgement, durability,
  and loss semantics per driver; ephemeral pub-sub is never an authoritative event log

### Tests
- **Test ID**: TEST-015-1
  - **Description**: Validate lifecycle ordering and rollback
  - **Pass Criteria**: Declaration-only registration precedes topological boot; partial failure
    cleans the failing and prior providers once in reverse with cancellation-safe unwinding
  - **Fail Criteria**: Registration resolves/constructs, boot order violates dependencies, or
    cleanup leaks, doubles, or stops on cancellation
- **Test ID**: TEST-015-2
  - **Description**: Validate lifetime, context, and cancellation isolation
  - **Pass Criteria**: Identity, cleanup, context, owned-child-task, detached-context, and
    cancellation contracts hold concurrently
  - **Fail Criteria**: State/tasks leak, detached work retains scoped dependencies, disposed
    scopes resolve, or cancellation becomes a normal error
- **Test ID**: TEST-015-3
  - **Description**: Validate providers, inert packages, managers, drivers, and capabilities
  - **Pass Criteria**: Package metadata is checked before import and only independently verified,
    compatible, approved, capability-satisfying named drivers resolve; evidence is bound to
    artifact/dependency/configuration/profile/policy identity and rejected after expiry,
    revocation, stale probes, or any bound-value change
  - **Fail Criteria**: Discovery imports early, self-report is trusted alone, capabilities are
    assumed, stale/mismatched evidence authorizes use, or fallback silently changes semantics
- **Test ID**: TEST-015-4
  - **Description**: Validate heterogeneous data boundaries
  - **Pass Criteria**: Specialized contracts and explicit native extensions preserve semantics
  - **Fail Criteria**: A universal query silently approximates unsupported behavior
- **Test ID**: TEST-015-5
  - **Description**: Validate delivery and commit ordering
  - **Pass Criteria**: Producer state/outbox and consumer business-state/inbox each commit
    atomically where co-located; external effects persist transactional intents before ack;
    duplicate/redelivery, idempotency, relay, replay, and ephemeral loss are tested
  - **Fail Criteria**: Outbox/inbox/intent insertion can be lost, consumer effects separate
    from dedup state, AF claims exactly once, acknowledges early, or publishes pre-commit
- **Test ID**: TEST-015-6
  - **Description**: Validate error taxonomy and policy evidence
  - **Pass Criteria**: Every internal failure class retains lifecycle stage and protected cause
    while untrusted interfaces receive stable sanitized reason codes
  - **Fail Criteria**: Cancellation is misclassified, policy evidence is absent, or secrets leak
- **Test ID**: TEST-015-7
  - **Description**: Validate extension and composition restrictions
  - **Pass Criteria**: Constructor/factory injection is canonical; domain code has no service
    locator/static facade dependency; discovery controls and package metadata are enforced
  - **Fail Criteria**: Domain code resolves global services or packages bypass approval
- **Test ID**: TEST-015-8
  - **Description**: Validate explicit sync/async boundaries
  - **Pass Criteria**: Public contract metadata identifies sync/async behavior, I/O contracts
    are async-first, only the process entry point bridges, and active loops remain unblocked
  - **Fail Criteria**: A library starts a hidden/nested loop, blocks active I/O, creates another
    bridge, or converts cancellation into a retryable/normal result

### Related Specs
- SPEC-008 (Script-First Automation and Only Write Once)
- SPEC-009 (AF Executable Python Framework and ChatGPT MCP Adapter)
- SPEC-011 (Human-First Constrained Selection Boundary)
- SPEC-012 (Capability Ladder and Scoped Generative Execution)
- SPEC-013 (Acceptance Authority, Derivation Taint, and Ladder Selection)
- SPEC-014 (Planner Side-Effect Authorization)

---

## [SPEC-016] Deterministic Prime Language and Ideon Reference Contract
**Date**: 2026-07-22
**Status**: Implemented
**Priority**: MUST

### Description
AF represents a resolved or deliberately open utterance as an authoritative, typed Ideon
graph. A positive natural-number stable key addresses the contained idea, while a separate
SHA-256 revision identifies one immutable recorded topology. The key is not the graph and
must never be treated as its meaning.

### Requirements
- MUST keep graph identity, stable key, and immutable revision as distinct values.
- MUST reserve zero for structural/control use.
- MUST allocate actual odd-prime numeric keys to registered prime Ideons and MUST record them
  in a versioned registry manifest.
- MUST construct non-prime keys from injective canonical identity bytes in a disjoint even
  key lane.
- MUST include namespace, ruleset, ordered roles/children, and declared identity-bearing
  context in canonical identity.
- MUST preserve original UTF-8 and source spans while treating normalization as an added view.
- MUST retain zero, one, or many dictionary candidates and MUST NOT convert confidence into
  forced semantic closure.
- MUST emit actual structured results for WHO, WHAT, WHERE, WHEN, WHERN, WHY, HOW, IS, COULD,
  WOULD, and SHOULD.
- MUST decide ask, answer, act, stop, or continue-reading from explicit openings, closure, and
  SHOULD rules.
- MUST resolve exact (stable_key, revision) references and fail closed on key, identity, or
  revision conflicts.
- MUST serialize arbitrary-precision keys as decimal strings at JSON boundaries.
- MUST keep the typed Ideon record outside the current string-oriented generic procedure
  pipeline; later pipeline routes may carry only a record reference or canonical document.

### Tests
- **Test ID**: TEST-016-1
  - **Description**: Validate key and revision separation
  - **Pass Criteria**: Prime and constructed key lanes, zero reservation, immutable revision
    history, exact resolution, canonical deduplication, and explicit conflicts all pass
  - **Fail Criteria**: A key is treated as a graph, a lane overlaps, or identity/revision data
    is overwritten
- **Test ID**: TEST-016-2
  - **Description**: Validate ambiguity and all eleven inquiries
  - **Pass Criteria**: "sat on the bank" retains both bank senses, reports missing actor and
    contested location, emits eleven fields, and asks without guessing
  - **Fail Criteria**: A candidate disappears without traced context or the system closes early
- **Test ID**: TEST-016-3
  - **Description**: Validate contextual closure and deterministic replay
  - **Pass Criteria**: Actor plus river context selects only the declared river sense; identical
    inputs produce the same reference and byte-identical record
  - **Fail Criteria**: Operational timing changes semantic bytes or context is silently ambient
- **Test ID**: TEST-016-4
  - **Description**: Validate order, Whern, WILL, SHOULD, and relation representations
  - **Pass Criteria**: Ordered compositions differ, was/is/will follows perspective, temporal
    and volitional will differ, high-harm SHOULD stops/offers a safer route, and compact plus
    reified relations round-trip
  - **Fail Criteria**: Order is lost, modality is conflated, or normative risk is ignored
- **Test ID**: TEST-016-5
  - **Description**: Validate large/nested keys, unresolved forms, and context preservation
  - **Pass Criteria**: Twenty-token and ten-level nested composites serialize and resolve;
    unrelated unknown forms receive different keys; non-identity context changes revisions while
    declared identity context changes keys
  - **Fail Criteria**: Integer conversion limits escape to an interface, unknown forms collapse,
    context disappears from the exact graph, or interpreter-wide limits are weakened

### Related Specs
- SPEC-009 (AF Executable Python Framework and ChatGPT MCP Adapter)
- SPEC-012 (Capability Ladder and Scoped Generative Execution)
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)

---

---

## [SPEC-017] Ideon Correction: Provenance Separation, Temporal Qualification, and Inquiry Scope
**Date**: 2026-07-22
**Status**: Implemented
**Priority**: MUST

### Description
Correction pass on the deterministic Prime Language reference slice.  Six semantic
errors identified in review are addressed without changing the overall architecture.

### Requirements
- MUST rename `Tense.WILL` to `Tense.PROJECTED`; temporal perspective is expressed as
  `WAS < IS > PROJECTED` attached to propositions/relations, not as a privileged inquiry
  attention.
- MUST classify the surface token "will" via dictionary disambiguation into temporal
  (WHEN slot, resolved relation `future-state`/`future-event`) or volitional (WHAT slot,
  resolved relation `intends`/`wants`/`commits`).  `WHERN` is reserved for
  criterion-conditioned temporality, not simple future anchoring.
- MUST rename `WillKind` to `AuxiliaryKind` and `will_kind()` to `auxiliary_kind()`.
- MUST make WHO inquiry material-open only when a predicate (action/state/relation) is
  present in viable candidates.  Utterances with no predicate leave WHO as NOT_APPLICABLE.
- MUST NOT write source ID or rights declaration into semantic graph annotations or
  provenance.  Both belong in `InterpretationRecord` only.  Requests with identical
  semantics but different source IDs MUST resolve to the same graph revision.
- MUST document constructed arbitrary-precision keys as canonical identity codes, not
  compact operational or database IDs.
- MUST accurately describe inquiry coverage: seven attentions (WHO, WHAT, WHERE, WHEN,
  WHERN, IS, SHOULD) have conservative deterministic behaviour; WHY, HOW, COULD, and
  WOULD are structurally represented but not yet evaluated.

### Tests
- **Test ID**: TEST-017-1
  - **Description**: WHO not-applicable when no predicate present
  - **Pass Criteria**: "the bank" (entity only, no action) yields WHO=NOT_APPLICABLE
  - **Fail Criteria**: WHO is material-open despite no agent-requiring predicate
- **Test ID**: TEST-017-2
  - **Description**: Same semantics from different sources share graph revision
  - **Pass Criteria**: Two requests with identical surface/context but different source_ids
    produce the same ideon_key and ideon_revision while retaining distinct record_ids
  - **Fail Criteria**: Source ID leaks into graph canonical bytes and changes the revision
- **Test ID**: TEST-017-3
  - **Description**: Temporal qualification attaches to proposition, not isolated inquiry
  - **Pass Criteria**: WAS/IS/PROJECTED wraps a Whern in TemporalQualification; three
    propositions under different temporal perspectives differ only in that qualification
  - **Fail Criteria**: Temporal modality is encoded as a standalone inquiry question
- **Test ID**: TEST-017-4
  - **Description**: WHERN satisfied by criterion, not by temporal anchor
  - **Pass Criteria**: Criterion-satisfied Whern renders IS without a datetime anchor
  - **Fail Criteria**: WHERN is treated as equivalent to WHEN with a future anchor

### Related Specs
- SPEC-016 (Deterministic Prime Language and Ideon Reference Contract)

---

---

## [SPEC-018] Ideon Correction: Composite Whern, Condition-Bearing WHEN, Role-Driven WHO, and Full Inquiry Procedures
**Date**: 2026-07-22
**Status**: Implemented
**Priority**: MUST

### Description
Second correction pass on the deterministic Prime Language reference slice addressing
four remaining semantic errors identified in review of SPEC-017.

### Requirements
- MUST model WHEN as a condition-bearing temporal relation (WhenCondition with typed
  alternatives: TIMESTAMP, INTERVAL, EVENT, CRITERION) rather than a scalar datetime.
  A timestamp is one possible witness; WHEN supports anchors, intervals, event/state
  predicates, and condition clauses.
- MUST model WHERN as the conjunction of a WHERE (spatial/domain) component and a WHEN
  (temporal/conditional) component.  WHERN(P) is satisfied only when both components
  are present; partial presence yields OPEN (non-material) with the missing role
  identified.  WHERN must not be used as a synonym for temporal standpoint alone.
- MUST drive WHO from declared unfilled who-typed semantic roles on viable candidates
  (agent, subject, experiencer, source, possessor, affected).  An impersonal state or
  process with semantic_roles=() leaves WHO NOT_APPLICABLE even when a predicate is
  present.  Agentive predicates carry semantic_roles=("agent",) and leave WHO
  materially open when no actor is declared.
- MUST attach global tense qualification (WAS/IS/PROJECTED) to propositions/relations
  via TemporalQualification(tense, when: WhenCondition) rather than using Whern as the
  host type.
- MUST implement minimal evidence-driven procedures for WHY, HOW, COULD, and WOULD:
    - WHY: OPEN (non-material) when a predicate is present; NOT_APPLICABLE otherwise.
    - HOW: OPEN (non-material) when a predicate is present; NOT_APPLICABLE otherwise.
    - COULD: OPEN (non-material) when both a predicate and a declared actor are present.
    - WOULD: OPEN (non-material) when a predicate is present; NOT_APPLICABLE otherwise.
  All four are non-blocking (material=False) so they do not prevent closure.
- MUST rename 'reference Whern' terminology to 'temporal perspective' wherever used
  to describe the observer's standpoint.
- MUST NOT retain the old Whern(temporal_anchor, reference_perspective, criterion)
  API; it is replaced by WhenCondition (typed temporal/conditional relation) and
  Whern (WHERE+WHEN composite).

### Tests
- **TEST-018-1**: WHEN satisfied by EVENT/CRITERION WhenCondition without a datetime
- **TEST-018-2**: WHERN satisfied only when both WHERE and WHEN components are present
- **TEST-018-3**: WHERN open (non-material) with missing:where when only WHEN provided
- **TEST-018-4**: WHERN open (non-material) with missing:when when only WHERE provided
- **TEST-018-5**: WHO NOT_APPLICABLE for impersonal state predicate (semantic_roles=())
- **TEST-018-6**: WHO OPEN (material) for agentive predicate (semantic_roles=("agent",))
- **TEST-018-7**: WHY/HOW/WOULD non-material open when predicate present
- **TEST-018-8**: COULD non-material open only when predicate and actor both present

### Related Specs
- SPEC-016 (Deterministic Prime Language and Ideon Reference Contract)
- SPEC-017 (Ideon Correction: Provenance Separation, Temporal Qualification, and Inquiry Scope)

---

---

## [SPEC-019] Ideon Correction: WHERN Constituent Propagation, Fail-Closed WHEN Validation, Multi-Role WHO, End-to-End Temporal Graph, Interpretation Graph Contract
**Date**: 2026-07-23
**Status**: Implemented
**Priority**: MUST

### Description
Third correction pass on the deterministic Prime Language reference slice, addressing
six integration blockers identified in review of SPEC-018.

### Requirements
- MUST propagate WHERN constituent state from a contested WHERE: CONTESTED WHERE + satisfied
  WHEN yields WHERN OPEN (non-material) with "where:contested" evidence, not NOT_APPLICABLE.
  WHERN is satisfied only when WHERE is fully resolved (SATISFIED) and WHEN is satisfied.
- MUST validate context.time as a parseable, timezone-aware ISO 8601 timestamp before
  satisfying WHEN.  An invalid or timezone-naive string fails closed (WHEN remains
  NOT_APPLICABLE).  Any non-empty context.when expression still satisfies WHEN as a
  condition clause.
- MUST expand WHO satisfaction beyond context.actor to all mapped who-typed context fields:
  context.actor (agent), context.subject (subject), context.experiencer (experiencer),
  context.source (source), context.possessor (possessor), context.affected (affected).
  The emitted WHO role must be the actual context field name, not always "actor".
  context.actor alone must NOT satisfy an experiencer-role candidate.
- MUST attach a typed temporal-qualification node (when-kind, when-value properties) to
  the interpretation graph when WHEN is satisfied, proving the WhenCondition model is
  applied globally end-to-end rather than in domain tests alone.  Invalid timestamps must
  not produce this node (fail-closed confirmed in graph output).
- MUST document the interpretation graph contract explicitly: the graph is surface-bound;
  same surface from different sources yields the same graph revision; different surfaces
  produce different revisions unless equivalence is declared via IdeonIdentity.
- MUST rename the stale test name containing "reference_whern" to remove the rejected
  terminology.

### Tests
- **TEST-019-1**: WHERN OPEN (non-material, "where:contested") when WHERE contested + WHEN satisfied
- **TEST-019-2**: WHERN SATISFIED when WHERE fully resolved + WHEN satisfied (matrix)
- **TEST-019-3**: WHERN OPEN (missing:when) when WHERE contested + WHEN not satisfied
- **TEST-019-4**: Invalid context.time string (non-parseable) → WHEN NOT_APPLICABLE
- **TEST-019-5**: Timezone-naive context.time string → WHEN NOT_APPLICABLE
- **TEST-019-6**: Valid timezone-aware ISO timestamp → WHEN SATISFIED
- **TEST-019-7**: WHO satisfied by context.experiencer for experiencer-role candidate
- **TEST-019-8**: context.actor alone does NOT satisfy an experiencer-role candidate
- **TEST-019-9**: End-to-end: valid context.time produces temporal-qualification node in graph
- **TEST-019-10**: End-to-end: invalid context.time produces no temporal-qualification node
- **TEST-019-11**: Interpretation graph contract: same surface + different sources → same revision
- **TEST-019-12**: Interpretation graph contract: different surfaces → different revisions

### Related Specs
- SPEC-016, SPEC-017, SPEC-018

---

---

## [SPEC-020] Ideon Correction: Fail-Closed Tense, WhenKind.ORDERING, INTERVAL Invariants, Public parse_when_expression
**Date**: 2026-07-23
**Status**: Implemented
**Priority**: MUST

### Description
Fourth and final correction pass on the deterministic Prime Language reference slice.
Four remaining integration blockers identified in review of SPEC-019 are resolved.

### Requirements
- MUST NOT silently default to `Tense.IS` when `reference_when` is provided but
  `render_tense()` raises `ValueError` due to incompatible or uncomparable
  `WhenCondition` kinds.  An incompatible or unresolved comparison must yield a
  `tense="contested"` property in the temporal-qualification node rather than
  silently asserting present-tense IS.
- MUST add `WhenKind.ORDERING` — a bounded ordering relation (before, after, during)
  relative to a named event or anchor.  `WhenCondition(ORDERING)` requires a non-empty
  expression.  `parse_when_expression` and `InquiryEngine` accept the ``ordering:``
  prefix.  This closes the gap between the documentation claim "WHEN supports orderings"
  and the absence of an ordering form in `WhenKind`.
- MUST enforce `WhenCondition(INTERVAL)` domain invariants in `__post_init__` directly:
  both bounds absent must raise `ValueError`; `interval_start > interval_end` (when both
  present) must raise `ValueError`.  The parser may provide early protection, but the
  domain value must enforce its own invariant unconditionally.
- MUST move WHEN-expression parsing from a private `_parse_when_expression` helper in
  `inquiry.py` into a public `parse_when_expression` function in
  `af.domain.ideons.temporal`.  Both `InquiryEngine` and `construct_graph()` must
  consume this single authoritative parser; no private copy or application-to-private-
  domain coupling is permitted.
- The PR description and governance records must not claim `WILL` as a privileged inquiry
  attention or assert "eleven implemented attentions" unless true.  The corrected model
  is: eleven questions are structurally represented; seven have conservative deterministic
  procedures (WHO, WHAT, WHERE, WHEN, WHERN, IS, SHOULD); four are open/non-material
  by evidence (WHY, HOW, COULD, WOULD).  Global tense qualification follows the
  ``WAS < IS > PROJECTED`` axis attached to propositions/relations, not a standalone WILL.

### Tests
- **TEST-020-1**: `tense="contested"` when TIMESTAMP event vs CRITERION reference (end-to-end
  through `interpret()` and `resolve()`)
- **TEST-020-2**: `tense="contested"` when CRITERION event vs TIMESTAMP reference
- **TEST-020-3**: `tense="contested"` when INTERVAL event vs TIMESTAMP reference
- **TEST-020-4**: `WhenKind.ORDERING` parsed by `parse_when_expression`; WHEN satisfied
  by `ordering:before:X`, `ordering:after:X`, `ordering:during:X`
- **TEST-020-5**: Empty `ordering:` expression fails closed (WHEN NOT_APPLICABLE)
- **TEST-020-6**: `WhenCondition(INTERVAL)` with both bounds absent raises ValueError
- **TEST-020-7**: `WhenCondition(INTERVAL)` with `interval_start > interval_end` raises ValueError
- **TEST-020-8**: `WhenCondition(ORDERING)` with empty expression raises ValueError
- **TEST-020-9**: `parse_when_expression` is importable from `af.domain.ideons.temporal`
  (public API); `ordering:before:release-v1` and inverted interval fail closed

### Related Specs
- SPEC-016, SPEC-017, SPEC-018, SPEC-019

---

---

## [SPEC-021] Ideon Correction: Role-Driven WHERE and WHERN Contested-State Governance Alignment
**Date**: 2026-07-23
**Status**: Implemented
**Priority**: MUST

### Description
Fifth correction pass on the deterministic Prime Language reference slice.  Two
remaining blockers identified in review of SPEC-020 are resolved.

### Requirements
- MUST drive WHERE from declared unfilled spatial roles (``location`` in
  ``SenseEntry.semantic_roles``).  A bare location noun (``question_slot=WHERE``,
  ``semantic_type=entity/place``) without a corresponding predicate/relation declaring
  a ``location`` role must NOT make WHERE open or contested.  WHERE activates only when:
    a. ``context.where`` is provided (explicit direct WHERE) — SATISFIED regardless of
       whether any candidate carries a spatial role; or
    b. a viable candidate declares ``"location"`` in ``semantic_roles``:
         - exactly one viable location-typed dictionary candidate → SATISFIED
         - multiple viable location-typed candidates → CONTESTED (material)
         - no viable location-typed candidate and no ``context.where`` → OPEN (material)
  WHERE is NOT_APPLICABLE when neither of the above conditions holds.
- MUST declare ``on.spatial`` (and equivalent spatial relations) with
  ``semantic_roles=("location",)`` in every dictionary, fixture, seed, and reference
  table so the role-driven contract is enforced consistently.
- MUST compute WHERN constituent status from the WHERE result directly (no separate
  ``context.where`` check in the WHERN block); ``context.where`` is already incorporated
  into the WHERE result.  WHERN transitions:
    - WHERE=SATISFIED + WHEN=SATISFIED → WHERN=SATISFIED
    - WHERE=CONTESTED + WHEN=SATISFIED → WHERN=CONTESTED (material)
    - WHERE=any present (SATISFIED/CONTESTED/OPEN) + WHEN absent → WHERN=OPEN (non-material)
    - WHEN=SATISFIED + WHERE not fully satisfied → WHERN=OPEN (non-material)
    - WHERE=NOT_APPLICABLE + WHEN=NOT_APPLICABLE → WHERN=NOT_APPLICABLE
- MUST supersede the SPEC-019/DEC-033 claim that ``CONTESTED WHERE + SATISFIED WHEN``
  yields WHERN=OPEN; the correct and implemented outcome is WHERN=CONTESTED.  Append
  DEC-035 to document the correction without editing prior governed history.

### Tests
- **TEST-021-1**: Bare location lexeme (entity, question_slot=WHERE) without a spatial
  predicate → WHERE NOT_APPLICABLE; context.where alone → WHERE SATISFIED (role=location)
- **TEST-021-2**: Spatial predicate with ``location`` role + multiple candidates → WHERE
  CONTESTED; resolved candidates → WHERE SATISFIED (role=location-sense)
- **TEST-021-3**: Spatial predicate with ``location`` role + no dictionary filler + no
  context.where → WHERE OPEN (material, evidence contains "missing:location")
- **TEST-021-4**: context.where and dictionary candidates produce equivalent WHERN
  SATISFIED outcomes when WHEN is also satisfied
- **TEST-021-5** (governance): SPEC-019/DEC-033 WHERN contested-state rule corrected by
  DEC-035; code and tests confirm CONTESTED WHERE + SATISFIED WHEN → WHERN=CONTESTED

### Related Specs
- SPEC-016, SPEC-017, SPEC-018, SPEC-019, SPEC-020

---

## [SPEC-022] Ideon Correction: Typed Temporal Perspective and Fail-Closed Whitespace Conditions
**Date**: 2026-07-23
**Status**: Implemented
**Priority**: MUST

### Description
Sixth correction pass on the deterministic Prime Language reference slice.  Two
remaining blockers identified in review of SPEC-021 are resolved.

### Requirements
- MUST introduce a finite typed representation that separates resolution status from
  resolved tense.  Add ``TemporalPerspectiveStatus`` (``RESOLVED | UNANCHORED |
  CONTESTED``) and ``TemporalPerspective`` (``status``, ``when: WhenCondition``,
  ``tense: Tense | None``) to ``af.domain.ideons.temporal``.  Invariant: ``RESOLVED``
  requires a non-None ``Tense``; ``UNANCHORED`` and ``CONTESTED`` must not carry a
  tense.  Construct the typed value in ``IdeonService.construct_graph`` and serialize
  only its validated finite projection into graph properties.  The graph node must carry
  both a ``tense`` property (full label: ``was | is | projected | unanchored |
  contested``) and a ``tense-status`` property (resolution status:
  ``resolved | unanchored | contested``).  Do not add unresolved states to the ``Tense``
  axis itself.
- MUST reject whitespace-only expressions for ``WhenKind.EVENT`` and
  ``WhenKind.CRITERION`` in both ``WhenCondition.__post_init__`` and
  ``parse_when_expression``.  Test truthiness AND ``str.strip()`` in both layers.
  A payload of ``event:   `` or ``criterion: \t`` must fail closed.

### Tests
- **TEST-022-1**: ``TemporalPerspective(RESOLVED)`` requires non-None ``Tense``; missing
  tense raises ``ValueError``.
- **TEST-022-2**: ``TemporalPerspective(UNANCHORED/CONTESTED)`` must not carry a tense;
  supplying one raises ``ValueError``.
- **TEST-022-3**: ``TemporalPerspective.tense_label`` returns ``was | is | projected``
  for RESOLVED and ``unanchored | contested`` for unresolved.
- **TEST-022-4**: Invalid ``status`` / ``when`` types raise ``TypeError``.
- **TEST-022-5**: ``WhenCondition(EVENT/CRITERION, expression="   ")`` raises
  ``ValueError`` (whitespace-only, direct constructor).
- **TEST-022-6**: ``parse_when_expression("event:   ")`` returns ``None``; likewise for
  ``criterion:   `` (fail-closed at parser layer).
- **TEST-022-7**: End-to-end graph carries ``tense-status="resolved"`` +
  ``tense="was"`` for a compatible timestamp reference; ``tense-status="unanchored"`` +
  ``tense="unanchored"`` with no reference; ``tense-status="contested"`` + ``tense=
  "contested"`` with incompatible kinds.

### Related Specs
- SPEC-016, SPEC-017, SPEC-018, SPEC-019, SPEC-020, SPEC-021

---

## [SPEC-023] Ideon Correction: Separate Tense Axis from Resolution Status in Graph Projection
**Date**: 2026-07-23
**Status**: Implemented
**Priority**: MUST

### Description
Seventh correction pass on the deterministic Prime Language reference slice.  SPEC-022
and DEC-036 introduced ``TemporalPerspective`` with a ``tense_label`` that correctly
separated the domain model but then re-conflated resolution status with the tense axis
in the graph projection: ``Property("tense", perspective.tense_label)`` emitted
``"unanchored"`` and ``"contested"`` as values of a tense-named property.

### Requirements
- MUST emit exactly two temporal perspective graph properties with unambiguous names:
  - ``temporal-perspective-status``: always present; one of
    ``resolved | unanchored | contested``.
  - ``resolved-tense``: present ONLY when ``temporal-perspective-status="resolved"``;
    value is exactly one of ``was | is | projected``.  For unresolved states
    (``unanchored``, ``contested``) this property MUST be absent from the graph node;
    the strings ``"unanchored"`` and ``"contested"`` MUST NOT appear in any
    tense-valued field.
- MUST update ``TemporalPerspective.tense_label`` to return ``str | None``: the
  resolved tense value (``was | is | projected``) for ``RESOLVED`` perspectives, and
  ``None`` for ``UNANCHORED`` and ``CONTESTED`` perspectives.  This removes the
  conflation of epistemic resolution status with the tense axis in the domain API.
- MUST update ``TemporalQualification`` docstring to document its resolved-only role
  (``tense: Tense`` is always required) and its relationship to ``TemporalPerspective``
  (the broader graph-construction path that includes unresolved states).
- MUST update the ``construct_graph`` inline comments and docstring to reflect the new
  ``temporal-perspective-status`` / ``resolved-tense`` property contract.

### Tests
- **TEST-023-1**: ``TemporalPerspective(UNANCHORED/CONTESTED).tense_label`` returns
  ``None`` (was ``"unanchored"`` / ``"contested"``); ``RESOLVED.tense_label`` still
  returns the correct ``was | is | projected`` string.
- **TEST-023-2** (end-to-end, five cases): WAS, IS, PROJECTED → status=``resolved``,
  ``resolved-tense`` present and correct, not in ``{unanchored, contested}``.
  UNANCHORED, CONTESTED → status correct, ``resolved-tense`` absent from the graph node.
- **TEST-023-3**: All prior regression tests updated to assert
  ``temporal-perspective-status`` instead of ``tense-status`` and ``resolved-tense``
  (when resolved) / absent (when unresolved) instead of the old merged ``tense`` property.

### Related Specs
- SPEC-016, SPEC-017, SPEC-018, SPEC-019, SPEC-020, SPEC-021, SPEC-022

---

## [SPEC-016] Private Single-Node Production-MVP Release Contract
**Date**: 2026-07-22
**Status**: Approved
**Priority**: MUST

### Description
AF SHALL define a bounded first production profile independently of the wider v0.1 platform
roadmap. The profile is one private Linux node, one trusted operator/tenant, one process per
SQLite database, deterministic routes only, and loopback or stdio access. Release admission
is fail-closed and based on versioned requirements plus current subject-bound evidence.

### Requirements
- MUST keep the profile, supported surfaces, exclusions, gate state, evidence references,
  and next actions in a strict versioned machine-readable manifest
- MUST reject duplicate keys and gate IDs, aliases, unknown fields, path traversal, symlinked
  evidence, unregistered script references, unsupported states, and a `met` gate without
  evidence
- MUST treat manifest data as declarative and MUST NOT execute a command supplied by YAML
- MUST report a valid but blocked manifest with exit `0`, readiness-enforcement failure with
  exit `1`, and malformed input/internal validation failure with exit `2`
- MUST report package build, inspection, installation, or smoke failure with exit `1` and
  invalid package-action usage or inaccessible inputs with exit `2`
- MUST calculate readiness only when every blocking gate is `met`; mandatory gates cannot be
  silently waived or treated as not applicable
- MUST preserve an explicit `not ready` status while any security, durability, recovery,
  operations, accessibility, artifact, or release gate remains unmet or pending
- MUST build the wheel from the source distribution in temporary paths and verify project and
  runtime versions, Python requirement, console entry points, typing marker, templates, and
  absence of repository/runtime state
- MUST install the wheel outside the checkout without dependencies and smoke deterministic
  routes using the already locked development environment
- MUST keep final clean-host installation, production deployment, release signing,
  publication, migration, backup/restore, rollback, and maintainer approval as separate
  blocking evidence rather than inferring them from a package build
- MUST bind final release evidence to the full source SHA, artifact and profile digests,
  effective configuration, platform/tool identities, observation time, expiry, and subject
- MUST keep public/LAN exposure, multiple tenants, multiple SQLite writers, selector/model
  execution, cross-driver certification, peer execution, and public package publication
  outside this profile
- MUST NOT close roadmap issue #3 or release-certification issue #34 merely because the
  production-profile contract and checker exist

### Tests
- **Test ID**: TEST-016-1
  - **Description**: Validate strict manifest parsing and evidence references
  - **Pass Criteria**: Valid data is accepted while duplicates, aliases, unknown fields,
    invalid state transitions, escapes, symlinks, and missing references fail with exit `2`
  - **Fail Criteria**: Ambiguous or unsafe manifest content is accepted
- **Test ID**: TEST-016-2
  - **Description**: Validate deterministic readiness reporting
  - **Pass Criteria**: Text/JSON reports contain stable counts and sorted blocker IDs; report
    exits `0`, blocked check exits `1`, and ready check exits `0`
  - **Fail Criteria**: A blocked profile reports ready or exit semantics vary by ordering
- **Test ID**: TEST-016-3
  - **Description**: Validate wheel-from-sdist identity and contents
  - **Pass Criteria**: One source distribution and pure-Python wheel agree with checkout
    metadata and include entry points, typing, templates, and no forbidden state
  - **Fail Criteria**: Direct-checkout-only builds, version drift, missing runtime files, or
    repository/runtime state pass inspection
- **Test ID**: TEST-016-4
  - **Description**: Validate installed-wheel smoke isolation
  - **Pass Criteria**: AF imports from a temporary installation outside the checkout and both
    deterministic examples pass using an in-memory database
  - **Fail Criteria**: Smoke imports the checkout, writes repository state, or skips either
    route
- **Test ID**: TEST-016-5
  - **Description**: Preserve honest production status
  - **Pass Criteria**: The repository manifest validates but remains blocked until every
    release gate has current evidence
  - **Fail Criteria**: Adding the checker or package smoke alone declares production ready

### Related Specs
- SPEC-008 (Script-First Automation and Only Write Once)
- SPEC-009 (AF Executable Python Framework and ChatGPT MCP Adapter)
- SPEC-011 (Human-First Constrained Selection Boundary)
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)

---

## [SPEC-017] Canonical Production Policy and External Admission Evidence
**Date**: 2026-07-22
**Status**: Approved
**Priority**: MUST

### Description
This specification clarifies and narrows SPEC-016. The repository checker proves canonical
policy integrity and declared implementation-gate completeness; it never authenticates
external evidence or authorizes production. The first profile has one coherent live surface:
one dedicated-account MCP-stdio process per SQLite database, with CLI maintenance only while
that process is stopped.

### Requirements
- MUST bind the exact profile and every mandatory gate's ID, category, title, requirement,
  blocking status, and verification mode to a reviewed canonical policy digest
- MUST reject removed, added, demoted, weakened, or profile-widening policy content until a
  reviewed schema/policy revision intentionally replaces the digest
- MUST report `requirements_complete` and `certifies_production: false`; MUST NOT use
  `READY` or treat checker exit `0` as deployment authorization
- MUST keep issue #3 authoritative for any final package version and MUST NOT independently
  authorize `0.1.0` from the narrower profile
- MUST permit only one live MCP-stdio process, one tenant/operator boundary, one SQLite
  database, and deterministic routes in this profile
- MUST require CLI maintenance to occur only while the service is quiesced and MUST exclude
  HTML, REST, GraphQL, OpenAPI, MCP HTTP, Secure MCP Tunnel, and every AF listener
- MUST include separate blocking gates for candidate identity, provenance/signing,
  clean-host install, target deployment/recovery evidence, current findings, and maintainer
  approval
- MUST inspect and hash the sdist before using its build backend, verify it remains unchanged,
  and build the wheel offline with hash-locked build tooling from the development lock
- MUST compare checkout, sdist, and wheel project identity, dependencies, scripts, runtime
  source, filenames, platform tags, required files, and complete wheel `RECORD` hashes
- MUST reject foreign packages, relocation data, duplicate/unsafe/archive-special members,
  forbidden runtime state, oversized archives/members, source drift, and metadata drift
- MUST install only the exact inspected wheel outside the checkout, verify its digest again,
  import both console entry-point targets, and exercise deterministic CLI routes
- MUST execute build and smoke stages with an allowlisted uncredentialed environment,
  temporary home, no inherited package-manager configuration, and no network access
- MUST distinguish policy/input errors (`2`) from artifact/build/install/smoke failure (`1`)
- MUST create the final candidate before candidate-bound evidence, signatures, deployment
  drills, approval, and tag; any rebuild or metadata change invalidates that evidence
- MUST bind external evidence to clean source, candidate/signature/profile/configuration
  digests, platform/tools, reviewer independence, deployment subject, observation time,
  expiry, and revocation; the static manifest alone cannot prove these facts

### Tests
- **Test ID**: TEST-017-1
  - **Description**: Protect the canonical policy
  - **Pass Criteria**: Removing/demoting a gate or widening network, process, tenant, route,
    or inference constraints fails validation with exit `2`
  - **Fail Criteria**: A reduced or wider manifest can report complete
- **Test ID**: TEST-017-2
  - **Description**: Separate completeness from production authorization
  - **Pass Criteria**: An all-met synthetic canonical manifest reports implementation
    completeness while `certifies_production` remains false and no `READY` claim appears
  - **Fail Criteria**: Manifest state alone authorizes deployment
- **Test ID**: TEST-017-3
  - **Description**: Validate complete artifact identity and provenance inputs
  - **Pass Criteria**: Dependency drift, missing/invalid `RECORD`, foreign content, source
    mismatch, filename/tag mismatch, or entry-point target loss blocks the package
  - **Fail Criteria**: Structurally plausible but unrelated/tampered artifacts pass
- **Test ID**: TEST-017-4
  - **Description**: Validate bounded hostile input handling
  - **Pass Criteria**: Excessive YAML/archive depth, members, size, duplicates, links, and
    path escapes fail deterministically without traceback or unbounded expansion
  - **Fail Criteria**: Malformed policy or artifacts exhaust or escape validation
- **Test ID**: TEST-017-5
  - **Description**: Validate isolated offline package execution
  - **Pass Criteria**: A clean checkout builds with locked offline tooling, inherited secrets
    are absent, exact artifact digests remain stable, and installed CLI/MCP targets import
  - **Fail Criteria**: Build/smoke requires egress, credentials, checkout imports, or mutable
    artifacts

### Related Specs
- SPEC-003 (MUST/SHOULD/MAY Requirements)
- SPEC-008 (Script-First Automation and Only Write Once)
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)
- SPEC-016 (Private Single-Node Production-MVP Release Contract)

---

## [SPEC-018] Stdio-Only Declaration and Commit-Snapshot Package Contract
**Date**: 2026-07-23
**Status**: Approved
**Priority**: MUST

### Description
This specification supersedes SPEC-017 where that specification calls manifest state
"requirements completeness," permits one process per database, or claims that package
execution has operating-system-level network denial. The first production-MVP profile has
exactly one SQLite database, one live MCP-stdio process, and no AF network exposure. The
repository checker validates canonical declarations; only later candidate-bound admission
can verify evidence and authorize deployment.

### Requirements
- MUST encode `network_exposure: none`, exactly one database, exactly one live process, and
  MCP stdio as the sole live application surface
- MUST report `declarations_complete`, `manifest_sha256`, `policy_sha256`, and
  `certifies_production: false`; every text result MUST state that production certification
  was not performed
- MUST treat checker exit `0` as declaration completeness only and MUST NOT describe it as
  evidence verification, readiness, certification, admission, or deployment approval
- MUST include a blocking SQLite runtime-policy gate covering journal and synchronous modes,
  busy timeout, foreign keys, integrity, and the one-database/one-process invariant
- MUST build the sdist from a clean commit-derived source snapshot rather than the mutable
  checkout and MUST fail if checkout HEAD or cleanliness changes during package execution
- MUST parse sdist and wheel core metadata strictly, compare runtime and optional dependency
  metadata, extras, explicit license files, and exact console entry-point groups, and load the
  installed target of every declared console entry point
- MUST require valid `PKG-INFO`, `uv.lock`, complete wheel `RECORD` coverage, regular archive
  members, and byte identity for every non-generated sdist file against the commit snapshot
- MUST disable inherited uv configuration and resolution network access for package commands,
  while accurately stating that this is not an OS network sandbox for backend or package code
- MUST keep the independent no-egress, clean-host installation, provenance, deployment,
  evidence-verification, and maintainer-approval gates unmet until their separate controls run

### Tests
- **Test ID**: TEST-018-1
  - **Description**: Validate the exact stdio-only profile
  - **Pass Criteria**: The canonical policy fixes one database/process and no network exposure;
    widening either process count or exposure fails validation
  - **Fail Criteria**: A loopback listener or additional database/process remains in profile
- **Test ID**: TEST-018-2
  - **Description**: Separate declarations from verified admission
  - **Pass Criteria**: Synthetic all-met state says declarations are complete while every
    format says production certification was not performed and binds the full manifest digest
  - **Fail Criteria**: Self-declared evidence yields a readiness or authorization claim
- **Test ID**: TEST-018-3
  - **Description**: Bind package artifacts to a clean commit snapshot
  - **Pass Criteria**: Package execution builds from the archived commit, rechecks live checkout
    identity/cleanliness, and compares generated artifacts with an unexecuted reference snapshot
  - **Fail Criteria**: Build-time mutation of the working checkout becomes accepted evidence
- **Test ID**: TEST-018-4
  - **Description**: Reject malformed metadata, entry points, archives, and RECORD rows
  - **Pass Criteria**: Duplicate/missing core metadata, unsafe licenses, undeclared entry-point
    groups, missing attributes, special members, bad hashes/sizes, duplicate rows, self-hashed
    RECORD, and incomplete coverage all fail
  - **Fail Criteria**: Structurally invalid or undeclared package metadata passes inspection

### Related Specs
- SPEC-008 (Script-First Automation and Only Write Once)
- SPEC-016 (Private Single-Node Production-MVP Release Contract)
- SPEC-017 (Canonical Production Policy and External Admission Evidence)

---

---

## [SPEC-024] Repository-Local Version-Control Coordination Bootstrap
**Date**: 2026-07-24
**Status**: Implemented
**Priority**: MUST

### Description
Provide the literal `af init version control` command as the bounded foundation for
parallel AF development. The command records the current committed branch and sanitized
upstream identity, then establishes a private bare coordination repository in the Git
common directory without contacting or changing the upstream.

### Requirements
- MUST require a Git worktree, committed attached HEAD, and supported SHA-1 or SHA-256
  object format
- MUST deterministically select an explicit, tracked, or sole non-AF upstream and fail on
  absent, ambiguous, multi-URL, or unsupported configuration
- MUST redact credentials and URL query or fragment material from all persisted and public
  observations
- MUST create `<git-common-dir>/af/remotes/coordination.git`, reserve
  `<git-common-dir>/af/worktrees`, register the exact local `af-local` URL by default, and
  seed only the current branch
- MUST serialize concurrent initialization and emit a canonical digest-bound manifest
- MUST treat existing managed state as untrusted and reject missing ownership, path or
  object-format drift, push URLs, symlinks, alternates, custom hooks, unsupported local or
  worktree configuration, and malformed storage
- MUST run Git without a shell, interactive credentials, ambient Git redirection, or
  non-file transport and MUST bound command time and captured output
- MUST be idempotent and grant no publish, pull-request, merge, issue, or admission
  authority

### Tests
- **TEST-024-1**: A normal clone initializes stable JSON, a same-format bare remote, the
  exact current branch, reserved worktree root, and canonical manifest
- **TEST-024-2**: Exact and concurrent reruns are idempotent; a new local commit refreshes
  only the local seed and manifest
- **TEST-024-3**: Detached, unborn, non-repository, ambiguous, conflicting, excessive, and
  malformed Git states fail closed before unintended mutation
- **TEST-024-4**: Credential forms, ambient Git variables, worktree URL rewrites, dangerous
  hooks, alternates, and internal or dangling symlinks cannot escape or redirect the
  managed operation
- **TEST-024-5**: The nested CLI command emits canonical JSON and maps sanitized expected
  failures to exit code 2

### Related Specs
- SPEC-008 (Script-First Automation and Only Write Once)
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)
- Epic #120 and bootstrap issue #123


---

## SPEC-025: Verified Memory and Null Cache Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST validate bounded application, tenant, namespace, and key identities.
- MUST accept only bounded non-empty byte values and positive TTL durations.
- MUST make expired entries unavailable at or after their expiry instant.
- MUST scope delete and flush to the exact namespace and preserve unrelated tenant data.
- MUST verify declared read, write, TTL, namespace, and flush capabilities before dispatch.
- MUST make the null store retain no value while preserving the same validation contract.
- MUST fail closed after driver shutdown and dispose manager-owned drivers deterministically.

### Tests
- **TEST-025-1**: Invalid identities, values, and TTLs are rejected.
- **TEST-025-2**: Memory entries expire exactly and cannot be deleted after expiry.
- **TEST-025-3**: Delete and flush preserve another tenant's same-named key.
- **TEST-025-4**: Named memory/null connections route only after capability verification.
- **TEST-025-5**: Closed drivers reject new operations with a stable shutdown reason.

## [SPEC-026] Owner-Fenced Memory Lock Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST scope locks to an exact application, tenant, namespace, and key.
- MUST issue a positive fencing token that increases for every successive owner.
- MUST permit at most one unexpired owner under concurrent acquisition.
- MUST reject renew and release from a different, expired, or superseded lease.
- MUST retain the fencing token across renewal and fail closed after shutdown.
- MUST make the null driver validate requests without claiming a lock.

### Tests
- **TEST-026-1**: Concurrent memory acquisition produces exactly one lease.
- **TEST-026-2**: A forged owner cannot renew or release another owner's lease.
- **TEST-026-3**: Expiry permits a new owner with a greater fencing token.
- **TEST-026-4**: Manager dispatch requires verified lock, namespace, and TTL capabilities.
- **TEST-026-5**: Memory and null lock operations validate inputs and fail closed.

## [SPEC-027] Persistent File Cache Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST accept only an absolute canonical cache root.
- MUST derive tenant, namespace, and key paths from validated identity digests.
- MUST verify bounded frame identity, schema, expiry, and value before returning bytes.
- MUST publish replacements atomically and leave no successful-operation temporary files.
- MUST treat expiry and corruption as misses without damaging another namespace.
- MUST preserve values across driver recreation and fail closed after shutdown.
- MUST advertise cache capabilities honestly and exclude unsupported locks.

### Tests
- **TEST-027-1**: Replacement persists the final bytes across driver recreation.
- **TEST-027-2**: Exact expiry and malformed frames return misses and remove the entry.
- **TEST-027-3**: Flush removes only the selected tenant namespace.
- **TEST-027-4**: Verified manager routing supports get, set, TTL, and delete.
- **TEST-027-5**: Non-canonical roots and post-shutdown operations fail closed.

---

## [SPEC-028] Manager-Level Remember and Memoize Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST let `CacheManager.remember` return an existing cached value without rerunning its loader.
- MUST call the supplied loader exactly once for a miss in the covered single-threaded path,
  then persist the returned bytes through the selected verified connection.
- MUST let `CacheManager.memoize` expose the same behavior behind a reusable zero-argument
  callable.
- MUST validate that remember and memoize loaders are callable.
- MUST reuse the existing driver read/write contracts instead of expanding backend contracts in
  this slice.

### Tests
- **TEST-028-1**: `remember` stores and reuses the first loaded bytes for the same key.
- **TEST-028-2**: `memoize` wraps a loader and returns cached bytes on subsequent calls.
- **TEST-028-3**: Non-callable loaders are rejected before cache-fill execution.

---

## [SPEC-029] Database Cache Driver Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST provide a verified `database` cache driver that routes through `CacheManager` using the
  existing cache read/write/TTL/namespace/flush contracts.
- MUST persist bounded cache bytes across driver recreation when configured with a persistent
  database URL.
- MUST treat expiry at the exact TTL boundary as a cache miss and remove only the expired row.
- MUST scope delete and flush to one exact application, tenant, namespace, and key boundary.
- MUST treat malformed persisted values as cache misses and remove only the exact invalid row.
- MUST NOT advertise lock capability in this slice.

### Tests
- **TEST-029-1**: Replacement persists the final bytes across driver recreation.
- **TEST-029-2**: Exact expiry and invalid persisted rows return misses and remove the row.
- **TEST-029-3**: Flush removes only the selected tenant namespace.
- **TEST-029-4**: Verified manager routing supports get, set, TTL, and delete, but rejects lock
  capability requests.
- **TEST-029-5**: Invalid URLs and post-shutdown operations fail closed.

---

## [SPEC-030] Database Lock Driver Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST provide a verified `database` lock-capable cache driver through the existing `CacheManager`
  lock routing.
- MUST scope every lease to the exact application, tenant, namespace, and key identity.
- MUST permit at most one unexpired owner across driver instances using a monotonic fencing token.
- MUST reject renew and release from a different, expired, or superseded lease.
- MUST preserve cache flush boundaries so deleting one namespace never erases unrelated lock state.
- MUST fail closed after shutdown and validate owner and TTL inputs before lock mutation.

### Tests
- **TEST-030-1**: Two driver instances contend for one database-backed lease and only one active
  owner succeeds at a time.
- **TEST-030-2**: A forged owner cannot renew or release another owner's persisted lease.
- **TEST-030-3**: Expiry permits a new owner with a greater fencing token.
- **TEST-030-4**: Verified manager routing supports acquire, renew, and release for the database
  driver.
- **TEST-030-5**: Invalid lock inputs and post-shutdown operations fail closed.

---

## [SPEC-031] Memcached Cache Driver Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST provide a verified `memcached` cache driver that routes through `CacheManager` using the
  existing read/write/TTL/namespace contracts.
- MUST scope cache keys to the exact application, tenant, and namespace without relying on a
  destructive global flush.
- MUST treat Memcached eviction and non-durable forever entries as normal cache-miss behavior.
- MUST translate TTLs into Memcached's relative/absolute expiry rules without widening AF's exact
  positive-duration validation contract.
- MUST advertise only the capabilities it can verify in this slice and exclude flush and locks.

### Tests
- **TEST-031-1**: Tenant-scoped keys isolate same-named entries and eviction becomes a normal miss.
- **TEST-031-2**: Forever, short relative TTL, and absolute Memcached TTL translations stay
  bounded and deterministic.
- **TEST-031-3**: Verified manager routing supports get, set, TTL, and delete, but rejects flush.
- **TEST-031-4**: Unsafe host, port, timeout, or extra configuration values are rejected.
- **TEST-031-5**: Missing Memcached runtime and post-shutdown operations fail closed.

---

## [SPEC-032] Memcached Lock Driver Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST provide Memcached lock acquisition through the verified `memcached` cache driver using the
  shared `CacheManager` lock contract.
- MUST permit at most one active lock owner per namespace/key at a time.
- MUST reject renew and release requests from a different owner or stale lease.
- MUST advance fencing tokens monotonically for newly claimed Memcached lock leases while the token
  key remains scoped to the exact application, tenant, namespace, and logical key.
- MUST fail closed on invalid lock entries, Memcached client failures, and post-shutdown access.
- MUST continue to omit flush capability and MUST NOT claim durable lock retention across eviction
  or restart.

### Tests
- **TEST-032-1**: One Memcached lock owner acquires the lease, a contending owner is rejected, and
  a later claim after expiry advances the fencing token.
- **TEST-032-2**: Renew and release succeed only for the current matching lease and fail for forged
  or stale owners.
- **TEST-032-3**: Verified manager routing exposes Memcached lock capability without widening flush
  capability.
- **TEST-032-4**: Invalid lock entries, lock-key derivation failures, client-operation failures,
  and post-shutdown lock operations fail closed with lock-specific stages.

---

## [SPEC-033] Redis Cache And Lock Driver Slice

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST provide a verified `redis` cache driver that routes through `CacheManager` using the
  existing read/write/TTL/namespace/lock contracts.
- MUST scope cache keys, lock keys, and fencing-token keys to the exact application, tenant,
  namespace, and logical key identities.
- MUST translate AF TTLs into Redis millisecond expiry without widening AF's exact
  positive-duration validation contract.
- MUST permit at most one active Redis lock owner per namespace/key at a time and reject renew or
  release from a different or stale lease.
- MUST treat eviction or restart loss as normal cache or lock misses and MUST continue to omit
  flush capability.
- MUST fail closed on invalid runtime identity, unsafe configuration, client-operation failures,
  and post-shutdown access.

### Tests
- **TEST-033-1**: Tenant-scoped keys isolate same-named entries and eviction becomes a normal miss.
- **TEST-033-2**: Redis cache writes use millisecond TTLs and expire at the exact boundary.
- **TEST-033-3**: One Redis lock owner acquires the lease, a contending owner is rejected, and a
  later claim after expiry advances the fencing token.
- **TEST-033-4**: Renew and release succeed only for the current matching lease and fail for forged
  or stale owners.
- **TEST-033-5**: Verified manager routing supports get, set, delete, acquire, renew, and release,
  but still rejects flush.
- **TEST-033-6**: Missing Redis runtime, unsafe host/port/db/timeout settings, client failures,
  invalid namespace versions, and post-shutdown access fail closed with Redis-specific stages.


---

## [SPEC-034] Bounded Cache-Fill Funnel

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST preserve existing `remember` and `memoize` behavior when no lock TTL is supplied.
- MUST require verified read, write, TTL, namespace, and lock capabilities when coalescing is
  requested.
- MUST permit one active loader for a contended namespace/key while waiters reuse the published
  bytes.
- MUST bound contention by a positive wait timeout and surface expiry as a transient platform
  failure.
- MUST double-check the cache after acquiring ownership.
- MUST renew ownership after loading and MUST NOT publish when the lease was lost.
- MUST validate lock and wait durations before cache access.

### Tests
- **TEST-034-1**: Eight concurrent remembers for one key return identical bytes while the loader
  runs once.
- **TEST-034-2**: A continuously busy funnel fails with a sanitized transient timeout.
- **TEST-034-3**: A loader that outlives its lease cannot publish stale bytes.
- **TEST-034-4**: Missing or non-positive funnel durations fail before cache execution.
- **TEST-034-5**: `memoize` forwards the same bounded coalescing contract.

---

## [SPEC-035] Stale-While-Revalidate Cache Fill

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST preserve raw payload bytes and distinguish fresh lifetime from the bounded retained-stale
  lifetime without requiring drivers to return expired entries.
- MUST require verified read, write, TTL, namespace, and lock capabilities.
- MUST return stale bytes without waiting for the loader and MUST schedule at most one refresh for
  a namespace/key while a current lease exists.
- MUST renew ownership before publishing refreshed bytes and MUST preserve stale data after loader,
  scheduler, or lease-renewal failure.
- MUST cold-fill through one owner after hard expiry and MUST bound contention by a positive wait
  timeout.
- MUST validate all durations, the loader, and the scheduler before cache mutation.
- MUST scope freshness markers to the exact namespace and a bounded digest of the logical key.

### Tests
- **TEST-035-1**: Fresh data is returned directly; after fresh expiry repeated callers receive the
  same stale bytes while only one refresh is scheduled.
- **TEST-035-2**: Successful refresh replaces the retained value and restores the fresh interval.
- **TEST-035-3**: Loader, scheduler, and expired-refresh-lease failures preserve stale bytes and
  permit a later retry.
- **TEST-035-4**: Hard expiry performs a new bounded cold fill; cold lease loss and persistent
  contention fail transiently without publishing.
- **TEST-035-5**: Non-positive durations, non-callable scheduling, and an overflowing hard horizon
  fail before cache mutation.

---

## [SPEC-036] Typed Capability and Request-Requirement Contracts

**Date**: 2026-08-01
**Status**: Active

### Requirements
- MUST define immutable, versioned, bounded, canonical `CapabilityDescriptor`,
  `RequestRequirements`, `CapabilityCandidate`, and `CapabilityRoute` values in trusted core.
- MUST distinguish declared capability facts from measured state, authorization, and verified
  quality evidence.
- MUST represent asset, provider, runtime, deployment, procedure, workflow, capability class,
  lifecycle state, effects, risk, locality, schema, languages/locales, resource envelope,
  dependencies, and fallbacks without vendor-specific core types.
- MUST reject unknown fields, invalid bounds, impossible class bindings, self-referential or
  overlapping relationships, unknown relationship targets, and cyclic dependency or fallback
  graphs.
- MUST expose stable canonical JSON-ready serialization and SHA-256 digests for replay,
  comparison, and evidence binding.
- MUST match exact typed requirements deterministically and fail closed with stable reason codes
  `match`, `partial_match`, `excessive_cost`, `unavailable`, `unverified`, `prohibited`,
  `ambiguous`, or `unsupported`.
- MUST NOT let a declared capability claim imply deployment readiness, authorization, or verified
  quality.

### Tests
- **TEST-036-1**: Static, procedural, model, workflow, and human fixtures round-trip canonically,
  produce stable digests, and satisfy exact requirements without model inference.
- **TEST-036-2**: Candidate ordering does not change the route digest, and an exact best-score tie
  fails closed as `ambiguous`.
- **TEST-036-3**: Missing accepted lifecycle, missing authorization, and missing verified quality
  prevent selection even when the declared descriptor matches.
- **TEST-036-4**: Unknown fields, invalid bounds, impossible state combinations, unknown
  relationships, and fallback cycles fail closed before selection.
- **TEST-036-5**: Over-budget candidates surface `excessive_cost`, and structurally compatible but
  non-exact candidates surface `partial_match`.

---

## [SPEC-037] Queue Capability Boundaries and Deterministic Ordering Declarations
**Date**: 2026-08-01
**Status**: Implemented
**Priority**: MUST

### Description
Require AF queue contracts to express capability-sensitive delivery requests explicitly and fail
closed when a selected driver cannot prove them. The first queue slices must also document and
test each built-in driver's real ordering semantics instead of implying broker-generic FIFO or
transaction guarantees.

### Requirements
- MUST allow a `QueueJob` to express requested FIFO grouping and transactional dispatch semantics
  independently of the selected backend
- MUST reject unsupported FIFO-group and transactional-dispatch requests through capability
  validation or explicit driver failure before silently enqueuing the job
- MUST document each built-in driver's actual ordering semantics and delivery boundary
- MUST state the database queue driver's deterministic reservation order exactly as
  `available_at`, then `occurred_at`, then `message_id`
- MUST state that the synchronous queue driver preserves only direct in-process dispatch order and
  provides no durability, worker isolation, or broker transaction guarantee

### Tests
- **TEST-037-1**: Queue contract validation accepts canonical FIFO-group and boolean transactional
  flags and rejects malformed values
- **TEST-037-2**: Sync and database drivers fail closed for unsupported FIFO-group and
  transactional-dispatch requests
- **TEST-037-3**: The database queue reserves jobs in deterministic
  `available_at`/`occurred_at`/`message_id` order
- **TEST-037-4**: The synchronous queue driver preserves direct dispatch order for accepted jobs

### Related Specs
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)
- Issue #20

---

## [SPEC-038] Shared Bounded Operations Dashboard Projection
**Date**: 2026-08-03
**Status**: Implemented
**Priority**: SHOULD

### Description
Provide one read-only AgentFramework operations projection through the application bus and expose
it consistently through server-rendered HTML, versioned REST, and terminal presentations without
moving aggregation, persistence, or health semantics into transport adapters.

### Requirements
- MUST read a fixed maximum of 25 recent Tasks and label activity as a bounded window rather than
  an all-time total.
- MUST exclude prompts, outputs, trace identifiers, configuration values, credentials, and raw
  telemetry payloads from every operations representation.
- MUST report unobserved component readiness as `unknown`; absence of a recorded failure MUST NOT
  imply readiness.
- MUST expose the same versioned projection through HTML, REST, plain terminal output, and stable
  terminal JSON.
- MUST preserve applications with repositories that predate the recent-Task capability by marking
  activity unavailable rather than failing or reading unbounded history.
- MUST remain read-only and MUST NOT execute routes, create Tasks or traces, initialize lazy search
  connections, or acquire external resources.
- MUST describe the unauthenticated HTTP view as loopback/development-only until AF has a verified
  authentication and authorization boundary.

### Tests
- **TEST-038-1**: The application service requests only the fixed window and emits deterministic,
  immutable, redacted status and recent-task values.
- **TEST-038-2**: REST, HTML, and terminal JSON expose the same projection without sensitive Task
  fields or read-side effects.
- **TEST-038-3**: OpenAPI names the explicit operations response model and the HTML view uses
  accessible headings and table semantics.
- **TEST-038-4**: The SQL repository returns the bounded recent Task window in deterministic
  descending creation order.

### Related Specs
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)
- Issues #27, #29, #33, and #178

---

## [SPEC-039] Lovelace Multi-Node AgentFramework v1 Runtime
**Date**: 2026-09-24
**Status**: Accepted — implementation pending
**Priority**: MUST

### Description
Provide an installable Python AgentFramework runtime for a central Lovelace access point that
is also a compute node, with authenticated agents on Alien and Bender and a later macOS node.
The runtime executes requested work within configured budgets, routes through verified reuse
and deterministic/programmatic capabilities before more expensive inference, and persists
progress and results for long-running multi-agent jobs.

### Requirements
- MUST use one AF application kernel for all interfaces and agent-node operations.
- MUST register typed capabilities with identity/version, schemas, modality/domain/intent,
  effects/risk, locality, lifecycle, verifiers, provenance, measured resources, and invalidation.
- MUST prefer eligible verified reuse and deterministic functions before specialist/model routes.
- MUST choose the lowest-cost route that satisfies the request and configured budget, with cost
  evidence including measured startup, inference, verification, locality, and energy/resource
  use when available; unknown cost MUST remain unknown.
- MUST support configured local llama.cpp/OpenAI-compatible adapters without implicit remote
  fallback, and MUST keep model weights outside the Python distribution and source repository.
- MUST permit agents to create and execute requested programs/templates, shell commands, local
  file operations, and installed tools through the node host executor. The active budget is the
  hard limit for requested work; do not add a per-command confirmation gate.
- MUST support typed multi-step specialist workflows and provenance-bound returned artifacts.
- MUST model long work as durable jobs with ordered status/progress events, cancellation,
  reconnect-safe result retrieval, and deadlines of at least 600 seconds.
- MUST expose notifications as projections of persisted AF job state; clients MUST NOT be the
  authoritative source of progress or completion state.
- MUST retain failed, partial, cancelled, and budget-exhausted work as truthful outcomes and
  MUST NOT promote it as a verified reusable capability.
- MUST support cache invalidation and reuse checks based on version, input, audience, provenance,
  verification, expiry, and budget.
- MUST qualify Linux nodes first and define cross-platform tests for the later macOS runtime.

### Tests and Release Evidence
- **TEST-039-1**: Deterministic and verified-cache routes execute with zero model calls.
- **TEST-039-2**: A multi-candidate route selects the lowest-cost capable route under budget and
  reports excessive cost, unknown cost, or no route without executing during feasibility checks.
- **TEST-039-3**: Local llama.cpp adapter fake-conformance covers warm/cold readiness, malformed
  responses, cancellation, timeout, and no remote fallback.
- **TEST-039-4**: A durable task can exceed ten minutes, survive client reconnect, publish ordered
  progress/terminal events, and produce the same terminal result through supported interfaces.
- **TEST-039-5**: A typed multi-agent workflow verifies stage inputs/outputs and reports partial
  completion without fabricating a final result.
- **TEST-039-6**: Budget exhaustion stops further dispatch, records actual use, settles active
  work according to the driver contract, and emits a stable reason code.
- **TEST-039-7**: A self-improvement run stores program/template/test artifacts and promotes only
  those whose declared verification evidence passes.
- **TEST-039-8**: Linux install, supervised lifecycle, storage recovery, Tailscale node
  authentication, and actual-host model-runtime qualification are tied to the release commit.

### Related Specifications
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)
- SPEC-038 (Shared Bounded Operations Dashboard Projection)
- Capability Ladder and Acceptance Scopes
- Issues #3, #4, #21, #38–#43, #47, #49, and #120


---

## [SPEC-072] Fail-Closed Queue Worker Resource Admission Boundary
**Date**: 2026-08-17
**Status**: Implemented
**Priority**: MUST

### Description
Require the synchronous queue worker kernel to consult an injected host-resource admission gate
before acquiring a queue reservation. The gate is a policy and capacity boundary; it is not a
resource allocator, lease repository, or authorization source.

### Requirements
- MUST invoke the gate before `QueueDriver.reserve` and before incrementing a delivery attempt.
- MUST expose the requested lane, current active-job count, and configured maximum concurrency.
- MUST accept only the literal boolean decisions `True` and `False`.
- MUST treat denial, gate failure, and malformed decisions as zero-reservation outcomes.
- MUST recheck closed, draining, and concurrency state after an allowed decision so a concurrent
  lifecycle transition cannot admit new work.
- MUST NOT let the gate widen scope, authorize work, execute a handler, or mutate a queued job.

### Tests
- **TEST-039-1**: Denial leaves the job queued, consumes no attempt, and executes no handler.
- **TEST-039-2**: Gate failure and non-boolean output fail closed before reservation.
- **TEST-039-3**: A drain that begins while the gate is evaluating prevents later reservation.
- **TEST-039-4**: A later allowed decision can process the untouched queued job normally.

### Related Specs
- SPEC-037 (Queue Capability Boundaries and Deterministic Ordering Declarations)
- Issue #21

## [SPEC-076] Atomic Fenced Resource-Lease Worker Admission
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Acquire an authorization-bound resource lease before queue reservation, retain its latest fencing
capability for the delivery lifetime, and release and reconcile the capacity after settlement.

### Requirements
- MUST create leases atomically and reject duplicate identities without overwriting the prior
  capability.
- MUST accept only the exact current fencing token for every lifecycle transition; both older and
  future tokens fail closed.
- MUST persist transitions by atomic compare-and-swap so concurrent uses of one fence cannot both
  succeed.
- MUST bind activation and renewal to the lease authorization digest and prevent scope widening.
- MUST renew active finite-duration leases using the latest owned fence while a permit is held.
- MUST stop renewal, release the active lease, and reconcile it when the worker releases the permit.
- MUST expire and reconcile a reserved lease if activation fails, preserving simultaneous
  activation and cleanup failures.
- MUST bind an explicitly selected queue lane into the lease scope and reject conflicting scope.
- MUST retain the worker's literal boolean denial behavior when concurrency capacity is exhausted.

### Tests
- **TEST-043-1**: Duplicate creation and non-exact fencing tokens fail without mutating the lease.
- **TEST-043-2**: Two concurrent renewals using one fence produce one success and one stale-fence
  rejection.
- **TEST-043-3**: Worker settlement releases and reconciles its lane-bound lease.
- **TEST-043-4**: Manual renewal advances expiration and the permit releases using the renewed
  fence.
- **TEST-043-5**: Existing resource-lease lifecycle and worker-kernel behaviors remain passing.

### Related Specs
- SPEC-072 (fail-closed queue worker resource admission)
- SPEC-075 (held worker resource admission permits)
- Issues #21 and #46

### SPEC-076 Addendum: Permit-Startup Cleanup
- MUST release and reconcile an already activated lease if constructing or starting its renewal
  permit fails.
- MUST preserve both the permit-startup failure and cleanup failure when both occur.
- **TEST-043-6**: Renewal-thread construction failure propagates and leaves the activated lease
  reconciled rather than leaking capacity.

---

## [SPEC-075] Resource Permit Lifecycle at Worker Admission
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Allow a fail-closed worker admission gate to return an acquired host-resource permit and make the
worker kernel own its exact release across every reservation and delivery outcome.

### Requirements
- MUST acquire a permit before queue reservation and hold it until delivery settlement completes.
- MUST release the permit exactly once when the queue is empty or a post-gate drain, shutdown, or
  concurrency decision prevents reservation.
- MUST release the permit after success, retry, rejection, duplicate acknowledgement, cancellation,
  renewal failure, or settlement failure.
- MUST reject malformed permit objects before reserving work.
- MUST preserve both the delivery failure and the permit-release failure when they occur together.
- MUST retain literal boolean admission decisions for policy-only gates while permitting a fenced
  backend adapter to supply a lifecycle-bearing capability.

### Tests
- **TEST-042-1**: A permit remains held during handler execution and releases after settlement.
- **TEST-042-2**: Empty-queue admission releases its permit without consuming work.
- **TEST-042-3**: A malformed permit reserves no job.
- **TEST-042-4**: Handler and permit-release failures survive as one exception group.
- **TEST-042-5**: Existing denial, drain-race, cancellation, renewal, acknowledgement, and shutdown
  worker behaviors remain passing.

### Related Specs
- SPEC-072 (fail-closed queue worker resource admission)
- Issue #21
- Issue #46

---

## [SPEC-074] Durable Context-Bound Queue Job Tags
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Attach typed worker identities to queue jobs without allowing tags to invent or diverge from the
message envelope's correlation and tenant security context, and preserve those tags through durable
relational delivery and recovery.

### Requirements
- MUST support correlation, opaque tenant, agent, task, and capability identifiers as a typed,
  immutable queue-job contract.
- MUST derive correlation and tenant tags from envelope metadata without inspecting the payload.
- MUST reject correlation or tenant tags that diverge from the envelope security context.
- MUST redact all tag values from contract representations.
- MUST persist tags through relational dispatch, restart, reservation, dead-letter inspection, and
  replay while remaining compatible with pre-tag queue tables and untagged jobs.
- MUST record the consumed attempt when explicit rejection or retry exhaustion dead-letters a job.

### Tests
- **TEST-041-1**: Context-derived tags validate, remain accessible to authorized code, and redact
  every value from `repr`.
- **TEST-041-2**: Divergent correlation and tenant contexts are rejected before dispatch.
- **TEST-041-3**: Relational restart preserves agent, task, and capability tags.
- **TEST-041-4**: Direct rejection records one consumed attempt and tagged dead-letter replay retains
  tags while rotating the reservation fence.

### Related Specs
- SPEC-016 (versioned message envelope contracts)
- SPEC-037 (queue capability boundaries)
- SPEC-073 (durable queue dead-letter inspection and replay)
- Issue #21
- Issue #46

---

## [SPEC-073] Durable Queue Dead-Letter Inspection and Replay
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Expose deterministic inspection and explicit replay of terminal relational queue deliveries while
preserving payload redaction and reservation fencing.

### Requirements
- MUST inspect dead letters in deterministic terminal-time and message-ID order with a bounded
  result count.
- MUST retain the immutable job for authorized recovery code while keeping payloads out of contract
  representations.
- MUST atomically replay only a currently dead-lettered message and fail closed for live, missing,
  or corrupt records.
- MUST reset delivery attempts for the new replay series while retaining the monotonically
  increasing reservation generation so stale settlement capabilities cannot become valid again.
- MUST clear prior reservation ownership and make replay immediately eligible at the recorded replay
  time.

### Tests
- **TEST-040-1**: Inspection returns terminal metadata and the recoverable job without exposing its
  payload in `repr`.
- **TEST-040-2**: Replay produces a fresh first attempt with a higher fencing generation.
- **TEST-040-3**: Replay rejects live and missing identifiers without mutating the live queue.

### Related Specs
- SPEC-037 (Queue Capability Boundaries and Deterministic Ordering Declarations)
- Issue #21

---

## [SPEC-077] Post-Settlement Worker Connection Recycling
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Recycle connections owned exclusively by a worker after each reserved delivery settles, while
keeping shutdown and resource-permit ownership aligned with the full cleanup lifetime.

### Requirements
- MUST expose one synchronous connection-recycler port on the worker kernel.
- MUST recycle exactly once after a reserved delivery is acknowledged, retried, rejected, or safely
  released, and MUST NOT recycle when no delivery was reserved.
- MUST run recycling after the job scope exits and before releasing the delivery's resource permit.
- MUST keep the delivery active until recycling and permit cleanup finish so shutdown cannot close
  the queue driver during cleanup.
- MUST attempt permit release even when connection recycling fails.
- MUST preserve the delivery, recycling, and permit-release failures when they occur together.
- MUST provide a concrete adapter that purges every explicitly worker-owned AF manager and preserves
  all manager purge failures.
- MUST NOT imply that application-shared managers are safe to purge from one concurrent worker;
  ownership remains an explicit composition requirement.

### Tests
- **TEST-044-1**: Successful execution orders scope exit, acknowledgement, recycling, and permit
  release without overlap.
- **TEST-044-2**: Delivery, recycling, and permit-release failures are retained in deterministic
  order and every cleanup is attempted.
- **TEST-044-3**: Empty-queue polling does not invoke the recycler.
- **TEST-044-4**: Shutdown waits while connection recycling is blocked.
- **TEST-044-5**: The owned-manager adapter purges every configured manager in order.
- **TEST-044-6**: One manager purge failure does not prevent later managers from being purged.

### Related Specs
- SPEC-075 (held worker resource admission permits)
- SPEC-076 (atomic fenced resource-lease worker admission)
- Issue #21

---

## [SPEC-078] Bounded Worker-Runtime Generation Supervision
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Detect a worker loop that disappears without a requested shutdown and replace failed runtime
generations only within an explicit, interruptible restart budget.

### Requirements
- MUST classify a kernel loop that returns before shutdown as a sticky worker-loss failure.
- MUST stop sibling intake and initiate active cancellation when any owned worker is lost.
- MUST create every replacement from a runtime factory so kernels, stop events, scopes, and owned
  connections are not reused across generations.
- MUST cap replacements with a non-negative restart budget and preserve every generation failure in
  chronological order when that budget is exhausted.
- MUST close a failed generation before starting its replacement and MUST fail closed without
  restarting when bounded cleanup cannot prove that the prior kernel closed.
- MUST make restart delay interruptible by the external stop event and close the active generation
  with a bounded shutdown timeout.
- MUST expose generation, restart, failure-history, and terminal-state evidence without implying
  process isolation; spawning and crash certification remain separate deployment work.

### Tests
- **TEST-045-1**: An unexpected kernel-loop return becomes a sticky worker-loss failure and starts
  draining.
- **TEST-045-2**: One failed generation is closed and replaced when one restart is available.
- **TEST-045-3**: Exhausting the budget fails closed with both generation failures in order.
- **TEST-045-4**: A kernel-close failure prevents an overlapping replacement generation.
- **TEST-045-5**: A stop request during restart delay prevents another factory invocation.
- **TEST-045-6**: The supervisor and its lifecycle state are exported from the public queue API.

### Related Specs
- SPEC-077 (post-settlement worker connection recycling)
- Issue #21

---

## [SPEC-079] Spawn-Based Worker Process Boundary and Signal Translation
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Run a worker supervisor behind an explicit operating-system process boundary without inheriting
parent threads or live connection ownership, and translate normal process stop signals into the
existing cooperative shutdown contract.

### Requirements
- MUST use the multiprocessing `spawn` context rather than `fork` so child composition begins from
  a fresh interpreter.
- MUST invoke the supervisor factory only inside the child process and MUST create a non-daemon
  child.
- MUST accept one structural stop-signal contract that works for both thread and multiprocessing
  events.
- MUST translate `SIGTERM` and `SIGINT` into cooperative stop requests inside the child and restore
  every previous handler after supervision exits.
- MUST request parent-driven shutdown through the shared stop signal and perform a bounded join.
- MUST leave a timed-out child intact for explicit retry rather than terminating or killing it
  implicitly.
- MUST expose PID, exact exit code, lifecycle state, and a sticky sanitized failure for nonzero or
  missing child exit status.
- MUST NOT claim crash restart, stale-heartbeat fencing, or cross-process resource certification;
  those require a parent-level reconciliation policy and real-service evidence.

### Tests
- **TEST-046-1**: Starting a process does not call the supervisor factory in the parent, and the
  child target calls it exactly once.
- **TEST-046-2**: The process is non-daemon and a real spawn child composes, runs, and exits cleanly.
- **TEST-046-3**: Cooperative close sets the shared stop signal and joins within the supplied bound.
- **TEST-046-4**: A close timeout is retryable and does not invoke destructive termination.
- **TEST-046-5**: A nonzero child exit retains the exact exit code and produces sticky failed state.
- **TEST-046-6**: `SIGTERM` and `SIGINT` handlers set the stop signal and restore prior handlers in
  reverse order.
- **TEST-046-7**: Process supervision contracts are exported from the public queue API.

### Related Specs
- SPEC-078 (bounded worker-runtime generation supervision)
- Issue #21

### SPEC-079 Verification Addendum
- Injected process contexts MUST report the `spawn` start method; test seams cannot weaken the
  fresh-interpreter boundary.
- **TEST-046-8**: A context reporting `fork` is rejected before an event or child is created.

---

## [SPEC-080] Bounded Parent-Level Worker Process Reconciliation
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Detect an unexpectedly exited worker child at the parent process boundary and create replacement
process generations only within an explicit, interruptible restart budget and only after the prior
child is observably dead.

### Requirements
- MUST create each process generation from a factory and require it to return a
  `SpawnWorkerProcess` synchronously.
- MUST treat both nonzero child exit and an unexpected clean exit as worker-process loss when no
  external stop was requested.
- MUST use explicit non-negative restart budget, restart delay, watch interval, and shutdown timeout
  bounds.
- MUST retire the failed child through bounded cooperative close before starting a replacement.
- MUST prove the prior child reports `alive == False` before invoking the factory for a replacement;
  a start or cleanup failure that leaves a possibly-live child MUST fail closed without restart.
- MUST preserve process-generation failures in chronological order and expose generation count,
  completed-restart count, and terminal lifecycle state.
- MUST make restart delay interruptible by the external stop event so a stop request cannot race a
  new factory invocation.
- MUST NOT claim stale-heartbeat fencing, resource-ownership fencing, or real-service crash
  qualification; those remain separate issue #21 work.

### Tests
- **TEST-047-1**: A dead failed child is retired and replaced without overlapping process
  generations when one restart is available.
- **TEST-047-2**: Exhausting the restart budget fails closed and preserves ordered failures from
  every process generation.
- **TEST-047-3**: A start failure that leaves the prior child alive prevents another factory call.
- **TEST-047-4**: An external stop during restart delay prevents creation of another process.
- **TEST-047-5**: Parent reconciliation contracts are exported from the public queue API.

### Related Specs
- SPEC-079 (spawn-based worker process boundary and signal translation)
- Issue #21

---

## [SPEC-081] Stale Worker Heartbeat and Settlement Fencing
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Prevent an obsolete or paused worker generation from extending expired resource ownership or
settling queue work after a replacement generation has acquired it.

### Requirements
- A resource lease heartbeat MUST fail at and after its recorded expiry even when its persisted
  lifecycle state remains `active`.
- Rejected post-expiry renewal MUST NOT rotate the fence, extend expiry, or otherwise mutate the
  persisted lease.
- Queue replacement MUST continue to use the durable reservation generation, owner identity, and
  rotating acquisition identity as its authoritative work fence rather than a second process-local
  generation counter.
- Once a replacement reservation is acquired, every renewal and settlement attempted with the
  predecessor reservation MUST fail as lost ownership.
- The worker kernel MUST propagate renewal loss without acknowledging, retrying, rejecting, or
  releasing through the stale reservation.
- This slice MUST NOT claim durable cross-process resource-capacity reconciliation or physical-host
  crash qualification.

### Tests
- **TEST-048-1**: A fenced resource permit cannot renew at the exact heartbeat-expiry boundary and
  the stored lease remains byte-for-byte equivalent to the prior active lease.
- **TEST-048-2**: A paused SQLite-backed worker whose reservation expires cannot renew when it
  resumes after a replacement worker completes the delivery.
- **TEST-048-3**: The stale worker surfaces `QueueReservationLost` and performs no terminal
  settlement through its obsolete reservation.

### Related Specs
- SPEC-080 (bounded parent-level worker process reconciliation)
- Issue #21

---

## [SPEC-082] Atomic Aggregate Resource-Lease Admission Contract
**Date**: 2026-08-17
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Make capacity reservation one atomic repository operation so two worker threads or processes cannot
both pass separate read/check/insert steps for the same resource identity.

### Requirements
- `reserved` and `active` leases for the same complete resource identity MUST consume aggregate
  quantity and concurrency; terminal lifecycle states MUST NOT consume either limit.
- Duplicate identity, available quantity, current concurrency, and insertion MUST be evaluated as
  one repository operation.
- A duplicate lease ID MUST return a distinct disposition so the application can retain exact fence
  conflict behavior.
- Aggregate quantity rejection MUST return the available quantity observed by the atomic decision
  and MUST NOT persist the candidate lease.
- Aggregate concurrency rejection MUST return the concurrent count observed by the atomic decision
  and MUST NOT persist the candidate lease.
- The application service MUST translate repository dispositions to the existing AF-specific budget,
  concurrency, and stale-fence errors.
- Every durable repository implementation MUST provide equivalent transaction or compare-and-swap
  isolation. The lock-backed in-memory reference proves only same-process behavior and MUST NOT be
  presented as durable cross-process certification.

### Tests
- **TEST-049-1**: Two simultaneous full-capacity reservations produce exactly one reservation and
  one capacity rejection, with only one capacity-consuming lease stored.
- **TEST-049-2**: A second reservation beyond `max_concurrent` is rejected with exact observed
  concurrency evidence and is not stored.

### Related Specs
- SPEC-081 (stale worker heartbeat and settlement fencing)
- Issue #21

### SPEC-082 Verification Addendum
- A candidate MUST NOT widen the quantity or concurrency ceiling while another capacity-consuming
  lease for that resource identity exists. The atomic decision MUST enforce the most restrictive
  applicable ceiling among the candidate and consuming records.
- **TEST-049-3**: A candidate with wider declared limits cannot exceed a consuming identity's
  stricter concurrency ceiling, reports that effective ceiling, and is not stored.

---

## [SPEC-083] Durable SQLite Resource-Lease Repository
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Persist the complete fenced resource-lease lifecycle in SQLite and enforce aggregate admission
across independent connections and spawned worker processes.

### Requirements
- MUST persist every `Lease`, `ResourceBudget`, `ResourceIdentity`, scope, authorization, digest,
  timestamp, renewal, and fence field without weakening immutable application semantics.
- MUST retain scalar identity, state, capacity, concurrency, fence, and creation columns alongside a
  deterministic JSON lease snapshot so admission and compare-and-swap remain transactional.
- MUST install its private schema idempotently and leave connection ownership with the caller.
- MUST evaluate duplicate ID, all capacity-consuming rows, the most restrictive live budget, and
  candidate insertion within one SQLite serialized-writer transaction.
- MUST update lifecycle state only when the persisted fencing value exactly matches the expected
  value and MUST report lost compare-and-swap ownership without overwriting the winner.
- MUST fail closed with a sanitized relational integrity failure for malformed stored values or
  impossible affected-row counts.
- MUST remain explicitly SQLite-specific. Other relational drivers require their own proven locking
  or serializable transaction strategy before sharing this certification.

### Tests
- **TEST-050-1**: A fully populated active lease round-trips exactly after closing and reopening the
  database through an independent driver.
- **TEST-050-2**: Two independent SQLite connections contending for one full budget produce exactly
  one reservation and one capacity rejection.
- **TEST-050-3**: Two freshly spawned processes contending for one full budget produce exactly one
  reservation and one capacity rejection, and both exit cleanly.

### Related Specs
- SPEC-082 (atomic aggregate resource-lease admission contract)
- Issue #21

---

## [SPEC-084] Explicit Durable Resource-Lease Composition
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Allow deployments to opt into the durable SQLite lease repository through typed configuration while
retaining the in-memory repository as the compatibility default and assigning driver ownership to
the application lifecycle.

### Requirements
- The default resource-lease backend MUST remain `memory`; durable persistence MUST NOT be selected
  implicitly from the task or trace database configuration.
- `AF_RESOURCE_LEASE_BACKEND` MUST accept only a declared backend value, and the SQLite selection
  MUST use an explicit bounded filesystem path rather than `:memory:`.
- A caller-supplied `LeaseRepository` MUST take precedence over configured composition and MUST
  remain externally owned.
- Configured SQLite composition MUST create one application-scoped `SQLiteDriver`, construct the
  lease repository from that driver, and eagerly materialize it during boot so schema failures are
  reported at the lifecycle boundary.
- Application shutdown MUST close each boot-owned SQLite lease driver exactly once.
- Recreating an application with the same configured lease path MUST recover the exact previously
  persisted lease through the public resource-lease service.

### Tests
- **TEST-051-1**: Configuration defaults to memory, accepts the explicit SQLite backend and path,
  and rejects an unknown backend or an ephemeral SQLite durable path.
- **TEST-051-2**: Two sequential application lifecycles recover the same reserved lease from one
  configured SQLite path and each closes its owned driver exactly once.

### Related Specs
- SPEC-083 (durable SQLite resource-lease repository)
- Issue #21

---

## [SPEC-085] Restart-Safe Expired Resource-Lease Sweeping
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Recover capacity abandoned by a crashed lease owner without allowing that owner to reactivate,
renew, or overwrite the replacement generation through an obsolete fence.

### Requirements
- A finite-duration reservation MUST record an expiry deadline when it is created, and activation
  MUST fail without mutation at or after that exact deadline.
- A persisted legacy reservation without an explicit expiry MUST use `created_at + max_duration` as
  its effective deadline; an unbounded lease MUST NOT be swept merely because it is old.
- A sweep MUST consider only due `reserved` and `active` leases plus already persisted `expired`
  leases, with optional exact-host filtering and deterministic result ordering.
- Each due capacity-consuming lease MUST move to `expired` only through compare-and-swap against its
  current fence. The transition MUST rotate the fence before the old capacity stops counting.
- A lost compare-and-swap MUST be reported as a conflict and MUST NOT overwrite a concurrent renewal
  or other newer lifecycle transition.
- After a successful expiry, the sweep MUST reconcile the lease through a second fenced transition.
  A later sweep MUST finish an `expired` transition left durable by a crash between those steps.
- The result MUST separately identify expired, reconciled, and conflicted lease IDs without claiming
  that a conflicted transition succeeded.

### Tests
- **TEST-052-1**: At the exact deadline after restart, a stale reservation cannot activate; one
  active lease and one legacy reservation are fenced, reconciled, reject the stale active fence,
  and release their complete budget to a replacement lease.
- **TEST-052-2**: A restart after durable expiry but before reconciliation finishes the remaining
  transition without repeating the expiry claim.

### Related Specs
- SPEC-081 (stale worker heartbeat and settlement fencing)
- SPEC-083 (durable SQLite resource-lease repository)
- SPEC-084 (explicit durable resource-lease composition)
- Issue #21

---

## [SPEC-086] Supervised Resource-Lease Maintenance Cadence
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Run expired resource-lease reconciliation as an owned part of worker supervision before admission
starts and at a declared periodic cadence, with fail-closed lifecycle behavior and bounded telemetry.

### Requirements
- Worker-supervisor maintenance MUST be a synchronous, no-result capability with a strictly positive
  cadence visible through the supervisor API.
- A configured maintenance pass MUST complete successfully before the first runtime factory call for
  every worker generation; a startup failure MUST create no worker or kernel.
- While a generation is running, maintenance MUST repeat no later than its declared cadence, while
  retaining the supervisor's existing stop, watch, shutdown, and restart bounds.
- A periodic maintenance failure MUST enter the existing failed-generation retirement path so the
  runtime is closed before any replacement generation is created.
- A pre-set stop signal MUST stop without running maintenance or constructing a worker generation.
- Resource-lease maintenance MUST retain the exact last sweep result and completion time locally for
  operator inspection, while telemetry MUST expose only bounded `none|one|multiple` count buckets
  and a canonical clean, reconciled, conflicted, or failed status.
- Telemetry construction or delivery failure MUST NOT change reconciliation semantics. A sweep
  failure MUST propagate and prevent unsafe worker startup.

### Tests
- **TEST-053-1**: Maintenance completes before the first worker enters its loop, repeats at the
  configured cadence, and exposes its successful run count and interval.
- **TEST-053-2**: Startup maintenance failure fails the supervisor before runtime construction and
  records no successful maintenance or worker generation.
- **TEST-053-3**: The resource adapter retains exact sweep evidence, reconciles the lease, and emits
  bounded first-pass and clean-pass telemetry without lease identity data.

### Related Specs
- SPEC-080 (bounded parent-level worker process reconciliation)
- SPEC-085 (restart-safe expired resource-lease sweeping)
- Issue #21

---

## [SPEC-087] Application-Composed Worker Lease Maintenance
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Expose a deployment-facing worker-supervisor factory from every bootstrapped application so a host
cannot accidentally construct a supervised worker without the resource-lease maintenance required
to recover crashed-owner capacity.

### Requirements
- Configuration MUST expose an optional exact maintenance host scope and a finite strictly positive
  cadence no greater than one day; the default scope MUST include all hosts and the default cadence
  MUST be 30 seconds.
- Environment configuration MUST parse the cadence as a typed number and reject zero, non-finite,
  malformed, or out-of-range values through the existing sanitized configuration boundary.
- Application bootstrap MUST bind one application-scoped supervisor factory to the same
  `ResourceLeaseService` used by the public application handle.
- Every supervisor produced by that factory MUST receive a fresh resource-lease maintenance adapter,
  the configured host scope, and the configured cadence; callers MUST NOT be able to omit that
  maintenance accidentally.
- The deployment host MUST retain ownership of the runtime factory, optional telemetry publisher,
  restart policy, and stop signal. Constructing the application or factory MUST NOT start a thread,
  process, worker runtime, or sweep.
- Maintenance MUST complete before the caller's runtime factory is invoked and MUST retain the
  fail-closed and bounded-telemetry semantics of SPEC-086.

### Tests
- **TEST-054-1**: Defaults and environment overrides expose the typed host scope and cadence, while
  zero, non-finite, and over-one-day cadences fail through sanitized configuration.
- **TEST-054-2**: A bootstrapped factory applies the configured exact host scope and cadence, emits
  clean bounded telemetry, and completes maintenance before a deliberately failing runtime factory.

### Related Specs
- SPEC-084 (explicit durable resource-lease composition)
- SPEC-086 (supervised resource-lease maintenance cadence)
- Issue #21

---

## [SPEC-088] Capability-Routed Failed-Job Inspection and Replay
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Expose the relational queue driver's existing failed-delivery inspection and atomic replay behavior
through the named queue control plane without allowing an unverified backend to receive either
operation.

### Requirements
- Dead-letter inspection and dead-letter replay MUST be independent declared queue capabilities;
  neither capability may be inferred from reservation or retry support.
- `QueueManager` MUST resolve the requested/default named connection with the exact required
  capability before invoking failed-job inspection or replay.
- Inspection MUST preserve the driver's bounded lane filter, `1..1000` limit, deterministic failure
  order, immutable job evidence, and payload-redacting representation.
- Manager inspection MUST reject a malformed driver result rather than returning objects that are
  not validated `QueueDeadLetter` values.
- Replay MUST return a validated `QueueDeadLetterReplayReceipt` and retain the relational driver's
  atomic reset to a fresh attempt series while preserving monotonically increasing reservation
  generations and rotating the acquisition identity.
- A connection without the applicable certified capability MUST fail closed before its driver
  operation is called.
- The database registration's manifest and runtime evidence MUST declare both capabilities only
  while the concrete database driver exposes the matching operations.

### Tests
- **TEST-055-1**: Through `QueueManager`, inspect one lane deterministically, replay its failed job,
  retain the other failure, and reserve the replay as attempt one with a rotated acquisition.
- **TEST-055-2**: A sync connection fails both inspection and replay with the stable unavailable-
  capability reason.

### Related Specs
- SPEC-020 (named queue manager and verified drivers)
- SPEC-086 (supervised resource-lease maintenance cadence)
- Issue #21

---

## [SPEC-089] Edge-Triggered Queue-Wait Alerts
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Bound queue-wait alert volume to worker-generation state transitions while retaining the existing
payload-blind lag evidence and best-effort telemetry boundary.

### Requirements
- A configured positive queue-wait threshold MUST continue to classify lag from immutable envelope
  occurrence time to reservation time; runtime and handler duration MUST NOT inflate queue wait.
- The first delivery above the threshold MUST emit an `exceeded` alert and consecutive above-
  threshold deliveries in the same worker generation MUST NOT emit duplicate alerts.
- The first subsequent delivery at or below the threshold MUST emit one `recovered` transition;
  consecutive recovered deliveries MUST remain silent, and a later breach MUST alert again.
- Transition decisions MUST be concurrency-safe across all threads sharing one worker kernel.
- Alert attributes MUST remain bounded to lag bucket, threshold bucket, and transition status; job,
  payload, lane, tenant, and correlation identifiers MUST NOT be copied into attributes.
- Alert delivery MUST remain best-effort and MUST NOT change queue settlement semantics. Transition
  state is generation-local and intentionally resets when supervision constructs a fresh kernel.

### Tests
- **TEST-056-1**: Four deliveries across exceeded, repeated-exceeded, recovered, and re-exceeded
  states emit exactly the three corresponding edge transitions.
- **TEST-056-2**: The existing first-breach contract retains bounded buckets and no delivery
  identifier or payload attribute.

### Related Specs
- SPEC-078 (bounded worker-runtime generation supervision)
- SPEC-088 (capability-routed failed-job inspection and replay)
- Issue #21

---

## [SPEC-090] Authorized Payload-Blind Failed-Job Control
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Provide an application-layer boundary for operator failed-job inspection and replay that authorizes
the exact selected queue resource before protected manager access and never returns the recoverable
job envelope or payload to presentation adapters.

### Requirements
- Inspection and replay MUST use the existing central default-deny `Gate` and MUST require operator-
  evaluation scope before invoking the queue backend.
- Authorization MUST bind inspection to the selected connection and lane scope, and replay to the
  selected connection and message identity; denial MUST occur before existence disclosure or queue
  mutation.
- Successful inspection MUST project only message identity, lane, attempt count, and terminal time;
  it MUST NOT return an envelope, payload, tag values, principal identity, or raw tenant identity.
- Successful replay MUST project only message identity, prior attempts, replay time, and the exact
  allowing gate decision.
- Result representations MUST redact message and lane identities while retaining count/time and
  authorization evidence that is already safe by contract.
- The service MUST validate backend result shapes even though `QueueManager` already validates its
  verified driver boundary.
- Authorized successes and backend failures MUST emit only bounded status, count/attempt bucket,
  and lane-scope telemetry. Identifiers, payloads, errors, and connection names MUST NOT become
  telemetry attributes.
- Telemetry delivery MUST remain best-effort. Backend failures MUST be re-raised unchanged, and no
  telemetry failure may change inspection or replay semantics.

### Tests
- **TEST-057-1**: A default-deny gate rejects inspection and replay before any backend access.
- **TEST-057-2**: An allowing gate returns payload-blind projections, retains authorization
  evidence, and emits bounded success telemetry without identities.
- **TEST-057-3**: The service composes with the real verified database `QueueManager` inspection and
  atomic replay path while omitting job payloads.
- **TEST-057-4**: Backend inspection and replay failures emit bounded failure telemetry and preserve
  the original exception objects.

### Related Specs
- SPEC-088 (capability-routed failed-job inspection and replay)
- SPEC-089 (edge-triggered queue-wait alerts)
- Issue #21

---

## [SPEC-091] Verified Backend-Specific Worker Generation
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Compose each worker generation from a freshly owned verified queue manager and an exact backend-
specific optimization provider without inventing unprofiled defaults or violating driver ownership.

### Requirements
- A worker-generation factory MUST receive a synchronous factory for a fresh unresolved
  `QueueManager`; a pre-resolved manager MUST be rejected without being closed or otherwise mutated.
- The selected named queue connection MUST verify reservation capability and expose current
  capability evidence before any optimization provider is invoked.
- Provider selection MUST use the exact configured queue driver identity. A missing, asynchronous,
  failing, or malformed provider MUST fail closed and close the newly owned queue manager.
- Runtime values MUST explicitly bound worker count, maximum concurrency, polling, heartbeats, idle
  lifetime, wait alerts, and renewal cadence. Worker count MUST NOT exceed maximum concurrency.
- A pinned provider MUST match both its declared backend identity and the complete qualified
  `CapabilityBinding`; it MUST reject configuration, artifact, dependency, deployment, policy, or
  driver drift. Its profiling artifact MUST be identified by a lowercase SHA-256 digest.
- The worker kernel MUST use an injectable synchronous close boundary so a manager-owned driver is
  closed through its owning manager exactly once rather than directly behind that manager.
- Handler-side recyclable managers MUST be passed through the existing
  `OwnedManagerConnectionRecycler`. Any `QueueManager` MUST be rejected from that set so post-job
  cleanup cannot purge the active worker transport.
- Composition failures after ownership transfer MUST attempt manager rollback and preserve both the
  primary and cleanup failures when both occur.

### Tests
- **TEST-058-1**: Verified evidence reaches the exact backend provider and its bounded values configure
  the returned runtime; repeated runtime close disposes the manager-owned driver exactly once.
- **TEST-058-2**: A missing provider fails closed and rolls back the newly owned manager.
- **TEST-058-3**: A pre-resolved borrowed manager is rejected without being closed.
- **TEST-058-4**: Explicit handler-manager recycling rejects every queue manager.
- **TEST-058-5**: A pinned optimization rejects backend identity and capability-binding drift.

### Related Specs
- SPEC-078 (bounded worker-runtime generation supervision)
- SPEC-086 (supervised resource-lease maintenance cadence)
- SPEC-090 (authorized failed-job control)
- Issue #21

---

## [SPEC-092] Exact-Binding Worker Profile Bootstrap
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Make the verified worker-generation factory deployable through explicit application configuration
while preventing startup from treating unqualified values or a changed queue store as benchmark
evidence.

### Requirements
- Default application bootstrap MUST NOT bind a worker-generation factory or create queue storage.
- SQLite worker queue storage and a worker optimization profile path MUST be configured together;
  partial configuration and in-memory worker storage MUST fail configuration validation.
- The profile loader MUST accept at most 65,536 bytes of UTF-8 JSON with an exact version-one
  schema. Unknown, missing, malformed, non-positive, or incorrectly typed values MUST fail closed
  behind a bounded provider-configuration error.
- A profile MUST declare its backend identity, complete `CapabilityBinding`, and explicit bounded
  worker count, concurrency, poll, heartbeat, idle, queue-wait, and renewal values in milliseconds.
- The profile's SHA-256 MUST be computed from its exact bytes and retained by the pinned provider;
  startup MUST NOT generate or substitute an allegedly qualified profile.
- The database queue registration MUST optionally bind a lowercase SHA-256 storage identity into
  the manager connection configuration and MUST reject a different or undeclared identity. This
  contract is versioned as database queue registration 1.5.0.
- SQLite bootstrap MUST derive a non-reversible identity from the resolved storage target, include
  it in registration validation and capability binding, and compare the configured binding to the
  profile before opening the queue database.
- Backend, driver artifact, dependency graph, queue storage, deployment profile, or policy drift
  MUST fail application boot without creating or opening the configured queue store.
- A successful opt-in MUST expose one application-scoped `WorkerGenerationFactory`; each generated
  runtime MUST continue to receive a fresh manager and the exact qualified runtime values.

### Tests
- **TEST-059-1**: Default bootstrap has no worker factory; complete two-path configuration opts in,
  while partial and in-memory configurations fail with the stable configuration reason.
- **TEST-059-2**: An exact profile boots a worker factory and creates a runtime with the declared
  worker count, concurrency, poll interval, and lane.
- **TEST-059-3**: Capability-binding drift fails application boot before queue creation.
- **TEST-059-4**: Changing only the SQLite storage target under the same profile fails application
  boot before either queue file is created.
- **TEST-059-5**: Malformed, incomplete, and oversized profile documents fail application boot.
- **TEST-059-6**: The database registration rejects malformed and mismatched storage identities.

### Related Specs
- SPEC-020 (named queue manager and verified drivers)
- SPEC-091 (verified backend-specific worker generation)
- Issue #21

---

## [SPEC-093] Measured Worker Qualification Recording
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Record one backend-specific worker runtime profile from explicit external qualification evidence,
without synthesizing measurements or allowing bootstrap to consume stale, synthetic, or
failure-bearing evidence.

### Requirements
- A qualification record MUST bind a canonical driver identity, complete `CapabilityBinding`,
  explicit `WorkerRuntimeOptimization`, evidence and environment SHA-256 digests, observation and
  expiry times, positive sample count, real-service status, and correctness, settlement, and
  reservation-loss counts.
- Recording MUST reject non-real-service evidence, any non-zero correctness, settlement, or
  reservation-loss count, and evidence that is expired at recording time.
- Runtime durations MUST be representable as positive whole milliseconds; recording MUST NOT round
  or silently alter optimizer-selected values.
- Profiles MUST use bounded canonical UTF-8 JSON with a trailing newline, exact version-two fields,
  and an embedded qualification record. Version one is superseded and MUST be rejected because it
  cannot prove the provenance or freshness of tuning values.
- Recording MUST use a same-directory mode-0600 temporary file, flush it before atomic promotion,
  remove failed temporary files, and preserve an existing profile when promotion fails. Symlinks,
  directories, missing parents, and oversized output MUST fail behind a bounded provider error.
- Loading MUST revalidate the exact document, qualification safety findings, real-service flag, and
  expiry before returning a provider. The provider MUST retain both the exact profile digest and
  the external qualification evidence digest.
- This recorder MUST accept externally produced evidence only. It MUST NOT execute a benchmark,
  infer that unit-test timings qualify production, or claim that a profile is optimal.

### Tests
- **TEST-060-1**: Recording emits canonical mode-0600 version-two JSON which round-trips through the
  loader with exact binding, profile digest, and qualification digest.
- **TEST-060-2**: Synthetic, correctness-failing, settlement-failing, reservation-losing, and expired
  qualifications are rejected without replacing an existing profile.
- **TEST-060-3**: A failed atomic promotion preserves the prior profile and removes its temporary
  artifact.
- **TEST-060-4**: Legacy version-one and stale version-two profiles are rejected on load.

### Related Specs
- SPEC-091 (verified backend-specific worker generation)
- SPEC-092 (exact-binding worker profile bootstrap; version-one format superseded here)
- Issue #21

---

## [SPEC-094] Explicit Worker Optimization Objective Policy
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Select one already qualified worker profile through explicit performance, safety, and cost
priorities without inventing measurements, comparing unlike targets, or hiding tie-breaking policy.

### Requirements
- Operator priorities MUST be integer percentages from zero through one hundred and MUST total
  exactly one hundred across performance, safety, and cost.
- Each candidate MUST pair a `WorkerOptimizationQualification` with externally normalized integer
  performance, safety, and cost-efficiency indices from zero through one million, where larger is
  better. Selection MUST NOT produce or infer those indices.
- A selection MUST contain between one and 256 candidates with unique evidence digests. Every
  candidate MUST bind the same driver, complete `CapabilityBinding`, and environment digest.
- Before scoring, every candidate MUST carry current, non-future, real-service evidence with zero
  correctness, settlement, and reservation-loss findings. Any unsafe candidate MUST fail the
  complete selection rather than merely receive a lower score.
- Weighted scoring MUST use integer arithmetic. Equal weighted scores MUST resolve by safety index,
  then performance index, then cost-efficiency index, then evidence digest so input order cannot
  change the result.
- The selection result MUST expose the chosen qualification, weighted score, candidate count, and a
  lowercase SHA-256 digest committing the priorities, every candidate's exact qualification and
  runtime values, all objective indices, and the selected evidence identity.
- This policy MUST NOT run a profiler, authenticate evidence, claim production certification, or
  expose itself as an operator interface. Those boundaries require separate composition and
  applicable real-service evidence.

### Tests
- **TEST-061-1**: Pure performance, safety, and cost priorities each select the corresponding best
  candidate from the same qualified target.
- **TEST-061-2**: Selection is input-order independent and resolves an equal weighted score through
  the documented safety-first tie policy.
- **TEST-061-3**: The audit digest changes when an exact selected runtime value changes.
- **TEST-061-4**: Synthetic, future-dated, expired, correctness-failing, settlement-failing, and
  reservation-losing candidates fail closed before scoring.
- **TEST-061-5**: Driver, capability-binding, or environment drift prevents comparison.
- **TEST-061-6**: Priorities and normalized indices reject booleans, out-of-range values, and totals
  other than one hundred.

### Related Specs
- SPEC-093 (measured worker qualification recording)
- SPEC-091 (verified backend-specific worker generation)
- Issue #21

---

## [SPEC-095] Authorized Exact-Target Worker Optimization Promotion
**Date**: 2026-08-18
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Authorize one exact worker backend/environment target before inspecting candidates, then select and
hand the complete auditable result to an injected synchronous recorder.

### Requirements
- Promotion MUST require operator-evaluation scope through the central default-deny `Gate` before
  candidate iteration, selection, or recording.
- Authorization MUST bind the canonical driver name and exact environment SHA-256 digest.
- Selection MUST reuse `WorkerOptimizationSelector`; the control service MUST NOT synthesize
  measurements, indices, priorities, or qualification evidence.
- The selected qualification MUST match the authorized driver and environment before recording.
- The recorder MUST receive the complete selection, exact priorities, selection digest, and one
  aware UTC operation time, and MUST return synchronously with no result.
- Recorder failure MUST propagate unchanged; coroutine, awaitable, async-generator, or non-`None`
  results MUST fail closed and MUST NOT produce promotion evidence.
- Successful promotion evidence MUST retain the allowing decision while redacting the selected
  qualification from its representation.

### Tests
- **TEST-062-1**: Default denial occurs before a deliberately uninspectable candidate iterable and
  makes no recorder call.
- **TEST-062-2**: Allowing exact-target authorization selects the deterministic winner and records
  the exact selection, priorities, digest, and UTC time once.
- **TEST-062-3**: Declared driver or environment drift fails before recording.
- **TEST-062-4**: Recorder failure and invalid synchronous-boundary results produce no success.
- **TEST-062-5**: Composition, time, target, and promotion value boundaries reject malformed input.

### Related Specs
- SPEC-094 (explicit worker optimization objective policy)
- SPEC-093 (measured worker qualification recording)
- Issue #21

---

## [SPEC-096] Authenticated Worker Measurement Ingress and Normalization
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Authorize one exact backend/environment evidence target, authenticate raw real-service measurement
documents from configured issuers, and derive deterministic bounded optimization indices without
accepting caller-supplied scores.

### Requirements
- Ingress MUST require operator-evaluation scope through the central default-deny `Gate` before
  iterating, authenticating, or disclosing any measurement.
- Authorization MUST bind the canonical driver name and exact environment SHA-256 digest.
- Each measurement MUST contain a qualification, positive completed-job and elapsed-microsecond
  counts, a non-negative total cost in integer microunits, a canonical issuer, and a lowercase
  HMAC-SHA256 signature.
- Canonical authenticated bytes MUST commit every qualification field except the derived evidence
  digest, every exact runtime value, the issuer, and all raw measurement counts.
- A configured issuer key MUST contain at least 256 bits. Unknown issuers, invalid signatures, and
  evidence-digest drift MUST fail with one non-disclosing authentication error.
- Accepted evidence MUST match the authorized target, remain current and non-future, declare a
  real service, contain zero correctness, settlement, and reservation-loss findings, and have a
  unique evidence digest.
- Performance MUST be derived from completed jobs per elapsed microsecond, safety confidence from
  the count of zero-finding samples, and cost efficiency from completed jobs per cost microunit.
  Each dimension MUST use exact integer/rational arithmetic relative to the best authenticated
  measurement and produce an integer from zero through one million.
- A zero-cost authenticated measurement MUST receive maximum cost efficiency while non-zero-cost
  measurements receive zero whenever such a candidate is present; no division-by-zero or invented
  cost is permitted.
- Authenticator coroutine, awaitable, asynchronous-generator, or non-`None` results MUST fail
  closed; coroutine results MUST be closed.

### Tests
- **TEST-063-1**: Default denial occurs before a deliberately uninspectable measurement iterable.
- **TEST-063-2**: Two signed raw observations derive exact performance, safety-confidence, and
  cost-efficiency indices and record the exact authorization target.
- **TEST-063-3**: Payload, signature, issuer, evidence digest, and target drift fail closed.
- **TEST-063-4**: Stale, synthetic, future-dated, and failure-bearing evidence is rejected.
- **TEST-063-5**: Raw counts, signatures, key strength, composition, and synchronous authenticator
  boundaries reject malformed input.

### Related Specs
- SPEC-095 (authorized exact-target worker optimization promotion)
- SPEC-094 (explicit worker optimization objective policy)
- SPEC-093 (measured worker qualification recording)
- Issue #21

---

## [SPEC-097] Observable Long-Running Worker Heartbeats
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Emit bounded health evidence from the long-running worker polling lifecycle so operators can
distinguish idle, busy, saturated, draining, stopped, and stale workers without inspecting jobs.

### Requirements
- A long-running worker configured with telemetry MUST emit a health heartbeat immediately and at
  every configured heartbeat interval until the polling lifecycle stops.
- Heartbeat production MUST remain independent of handler execution so a blocked or long-running
  job continues to produce liveness evidence.
- Each live heartbeat MUST declare a freshness deadline two configured intervals after observation.
  Consumers MUST treat evidence older than that deadline as stale rather than live.
- Readiness MUST distinguish ready, busy, saturated, and draining states. A terminal heartbeat MUST
  declare stopped liveness and not-ready readiness.
- Attributes MUST remain bounded and low cardinality: worker state, active-job count bucket, and
  saturation bucket only. Job identities, payloads, tenants, and other sensitive data MUST NOT be
  included.
- Heartbeat construction or publication failure MUST NOT change reservation, execution, settlement,
  or polling semantics.

### Tests
- **TEST-064-1**: An idle long-running worker emits repeated fresh live health signals and one
  terminal stopped signal.
- **TEST-064-2**: A blocked long-running job continues to emit live saturated health evidence.
- **TEST-064-3**: A partially occupied worker reports busy and then draining readiness.
- **TEST-064-4**: A failing telemetry sink cannot stop worker polling.
- **TEST-064-5**: Heartbeat signals contain only bounded state and saturation attributes and do not
  disclose a job identity.

### Related Specs
- SPEC-084 (process-isolated queue worker supervision)
- SPEC-080 (owned worker runtime and cooperative cancellation)
- Issue #21

---

## [SPEC-098] Monotonic Worker Runtime Measurement
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 slice
**Priority**: MUST

### Description
Measure worker execution duration with a monotonic clock so wall-clock correction cannot corrupt
cumulative runtime or duration telemetry.

### Requirements
- Worker delivery duration MUST be the elapsed monotonic time from admitted execution start through
  terminal cleanup.
- UTC wall time MUST remain the observation timestamp for emitted telemetry and MUST NOT determine
  elapsed duration.
- Backward and forward wall-clock corrections MUST NOT produce negative, zeroed, or inflated runtime
  when monotonic elapsed time is unchanged.
- The same monotonic duration MUST feed both cumulative `WorkerStats.total_runtime` and the bounded
  terminal-delivery runtime bucket.
- Queue execution, acknowledgement, retry, release, and cleanup semantics MUST remain unchanged.

### Tests
- **TEST-065-1**: A one-hour backward wall-clock correction preserves a 250 ms monotonic runtime.
- **TEST-065-2**: A one-day forward wall-clock correction preserves the same monotonic runtime.
- **TEST-065-3**: Cumulative statistics and delivery telemetry derive from the same duration.
- **TEST-065-4**: Existing worker kernel, runtime, process, and stale-worker recovery suites remain
  green.

### Related Specs
- SPEC-097 (observable long-running worker heartbeats)
- SPEC-081 (bounded worker delivery telemetry)
- Issue #21

---

## [SPEC-099] Fail-Closed Worker Lane Admission
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 repair
**Priority**: MUST

### Description
Validate a requested queue lane before resource admission or retained-permit cleanup can perform
side effects.

### Requirements
- A non-`None` worker lane MUST satisfy the queue contract's canonical identifier rules before the
  admission gate is invoked.
- An invalid lane MUST NOT acquire resource capacity, retry retained permit cleanup, reserve a
  delivery, consume an attempt, or execute a handler.
- Valid canonical lanes and the unfiltered `None` lane MUST retain their existing behavior.

### Tests
- **TEST-066-1**: An invalid lane fails before the resource-admission gate is called and leaves the
  queued delivery and its attempt count unchanged.
- **TEST-066-2**: Existing worker, fenced-resource-admission, and issue-boundary suites remain green.

### Related Specs
- SPEC-072 (fail-closed queue worker resource admission)
- SPEC-075 (resource permit lifecycle at worker admission)
- Issue #21

---

## [SPEC-100] Fail-Closed Worker Capacity Facts
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 repair
**Priority**: MUST

### Description
Validate caller-supplied worker capacity facts before fenced resource admission can perform a
privileged lease side effect.

### Requirements
- The active-job count MUST be a non-negative integer and MUST reject booleans.
- Maximum concurrency MUST be a positive integer and MUST reject booleans.
- Invalid capacity facts MUST fail before lease identity allocation, creation, activation, or
  renewal can occur.
- Valid capacity facts MUST retain the existing local and aggregate saturation behavior.

### Tests
- **TEST-067-1**: Negative and boolean active-job counts raise a validation error without creating
  a lease.
- **TEST-067-2**: Zero and boolean maximum-concurrency values raise a validation error without
  creating a lease.
- **TEST-067-3**: Existing worker kernel, fenced-resource-admission, and issue-boundary suites
  remain green.

### Related Specs
- SPEC-072 (fail-closed queue worker resource admission)
- SPEC-075 (resource permit lifecycle at worker admission)
- SPEC-099 (fail-closed worker lane admission)
- Issue #21

---

## [SPEC-101] Terminal Resource-Permit Release Boundary
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 repair
**Priority**: MUST

### Description
Make resource-permit release initiation a terminal boundary for explicit renewal so cleanup cannot
lose ownership of its captured fence.

### Requirements
- Once permit release begins, a concurrent explicit renewal MUST fail before calling the lease
  service.
- A renewal already holding the permit lock before release begins MAY finish; release MUST then use
  its resulting latest fence.
- Release MUST still stop and join the owned renewal coordinator before terminal lease cleanup.
- Rejected renewal MUST NOT advance the fence, prevent reconciliation, or retain the permit.

### Tests
- **TEST-068-1**: A renewal attempted after release has loaded its persisted lease fails with the
  releasing state and does not invalidate cleanup's fence.
- **TEST-068-2**: The release completes without an error and leaves the lease reconciled.
- **TEST-068-3**: Existing worker kernel, fenced-resource-admission, and issue-boundary suites
  remain green.

### Related Specs
- SPEC-072 (fail-closed queue worker resource admission)
- SPEC-075 (resource permit lifecycle at worker admission)
- SPEC-100 (fail-closed worker capacity facts)
- Issue #21

---

## [SPEC-102] Uncertain Renewal-Coordinator Startup Cleanup
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 repair
**Priority**: MUST

### Description
Contain renewal-coordinator startup failures that occur after the underlying thread has actually
started, so admission cannot lose ownership of a live daemon thread.

### Requirements
- Renewal-thread startup failure MUST signal the permit stop event before construction fails.
- If startup assigned a thread identity, construction MUST join that thread before propagating the
  startup failure.
- A simultaneous startup and thread-cleanup failure MUST preserve both failures.
- The admission gate MUST still release and reconcile the activated lease after contained permit
  construction failure.

### Tests
- **TEST-069-1**: A thread that starts and then reports an uncertain failure is stopped and joined
  before the admission call returns its error.
- **TEST-069-2**: The corresponding active lease is released and reconciled.
- **TEST-069-3**: Existing worker kernel, fenced-resource-admission, and issue-boundary suites
  remain green.

### Related Specs
- SPEC-072 (fail-closed queue worker resource admission)
- SPEC-075 (resource permit lifecycle at worker admission)
- SPEC-101 (terminal resource-permit release boundary)
- Issue #21

---

## [SPEC-103] Uncertain Resource-Lease Activation Cleanup
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 repair
**Priority**: MUST

### Description
Recover resource capacity when lease activation commits but reports an uncertain failure, without
reusing the stale reservation fence held by the admission call.

### Requirements
- Activation failure cleanup MUST reload the persisted lease before selecting a terminal
  transition.
- A persisted reserved or active lease MUST be expired with its current fence and reconciled.
- A persisted expired, released, or revoked lease MUST resume directly at reconciliation.
- An already reconciled lease MUST be accepted as completed cleanup.
- The original activation failure MUST be propagated after successful cleanup; simultaneous
  activation and cleanup failures MUST preserve both causes.

### Tests
- **TEST-070-1**: Activation that commits `ACTIVE` and then raises is recovered with the persisted
  active fence.
- **TEST-070-2**: The admission call propagates the original uncertain activation error and leaves
  the lease reconciled at fence four.
- **TEST-070-3**: Existing worker kernel, fenced-resource-admission, and issue-boundary suites
  remain green.

### Related Specs
- SPEC-072 (fail-closed queue worker resource admission)
- SPEC-075 (resource permit lifecycle at worker admission)
- SPEC-102 (uncertain renewal-coordinator startup cleanup)
- Issue #21

---

## [SPEC-104] Operator Revocation and Permit Renewal Race
**Date**: 2026-08-19
**Status**: Implemented as a bounded issue #21 repair
**Priority**: MUST

### Description
Allow an authoritative operator revocation to win a concurrent permit renewal without retaining a
fully reconciled permit, while preserving renewal failures that are unrelated to the revocation.

### Requirements
- Permit release MUST reload persisted lease state after stopping the renewal coordinator.
- A persisted revoked lease MUST be reconciled with its current fence.
- A stale-fence renewal failure caused by that revocation MUST be treated as completed external
  cleanup after reconciliation.
- A renewal failure of any other type MUST remain observable even when the lease is subsequently
  revoked and reconciled.
- Successful revocation cleanup MUST leave the permit and persisted lease reconciled.

### Tests
- **TEST-071-1**: Revocation that advances the fence before renewal produces the intended stale
  renewal result, then permit release completes without retaining the reconciled permit.
- **TEST-071-2**: An unrelated renewal transport failure remains the exact propagated failure after
  revoked-lease reconciliation.
- **TEST-071-3**: Existing worker kernel, fenced-resource-admission, and issue-boundary suites
  remain green.

### Related Specs
- SPEC-072 (fail-closed queue worker resource admission)
- SPEC-075 (resource permit lifecycle at worker admission)
- SPEC-101 (terminal resource-permit release boundary)
- Issue #21

---

## 2026-09-26 — Autonomous completion integration checkpoint

The private completion branch adds executable bounded workflows, consented decision
providers, typed science/locales, durable scheduling, scoped headless tokens/OAuth,
verified content/peer execution, and concrete storage/queue/communication adapters.
Shared application controllers remain authoritative across transports. Imported
capability declarations, fixture evidence, provider acceptance and protocol conformance
MUST NOT confer execution authority, verified completion or real-service certification.
Authenticated backup restoration MUST retain the checkpoint signing identity. Runtime
admission and self-operation review are separate from repository engineering autonomy.
This checkpoint is an engineering increment, not completion of the v0.1 release gate.

## 2026-09-26 — Durable decisions, admission, and installed reference operations

Human responses MUST narrow a stored request to one pinned installed operation,
retain expiry/authority/policy bounds, reject replay and corrupted history, and bind
the revalidated context during execution. Interrupted accepted responses remain
inconclusive rather than silently repeating work. Finite interpretation preserves
ambiguity and cannot itself authorize execution.

The confined local engineering path MUST derive route exhaustion from installed
services, run test-first generation and independent gates, authenticate admission,
and expose a direct reusable route that immediately honors invalidation. A resolver
lease MUST NOT escape a dependency factory or cross execution owners implicitly.
Current-time results MUST NOT enter exact deterministic reuse caches.

Machine setup MUST offer a read-only plan, explicit apply, actual bounded readiness
probes, durable recovery receipts and data-retaining disable operations. Optional
observational products MUST be removable without changing task semantics and redact
before persistence. Network host allowlists are not authentication authority.

## 2026-09-26 — Stabilization and candidate qualification

Every deterministic or selected Task route MUST enforce the same descriptor effect
and risk baseline before execution or reuse. Untyped principal claims and locale
presentation MUST NOT become routing or authorization signals. Execution traces
MUST minimize request/argument/output content before invoking a persistence port;
Task records remain explicit user content. Conditional API writes MUST compare
atomic versions; idempotency receipts MUST bind authority and preserve uncertain
outcomes without replay. Package smoke MUST execute with only hash-verified locked
runtime dependencies and no development or user-site imports. Exact expiring source
reviews may classify proven synthetic secret candidates, but MUST retain findings,
reject changed lines and never suppress dependency or static security findings.
