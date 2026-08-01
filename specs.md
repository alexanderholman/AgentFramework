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

## [SPEC-025] Queue Capability Boundaries and Deterministic Ordering Declarations
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
- **TEST-025-1**: Queue contract validation accepts canonical FIFO-group and boolean transactional
  flags and rejects malformed values
- **TEST-025-2**: Sync and database drivers fail closed for unsupported FIFO-group and
  transactional-dispatch requests
- **TEST-025-3**: The database queue reserves jobs in deterministic
  `available_at`/`occurred_at`/`message_id` order
- **TEST-025-4**: The synchronous queue driver preserves direct dispatch order for accepted jobs

### Related Specs
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)
- Issue #20
