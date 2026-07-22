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
