# Architectural and Design Decisions

**NOTE: This file is append-only. Do not modify or remove existing entries. Only add new decisions at the end.**

## Decision Record Format

Each decision entry MUST follow this format:

```
## [DEC-XXX] Decision Title
**Date**: YYYY-MM-DD
**Status**: Proposed | Accepted | Implemented | Superseded | Deprecated
**Decision Maker**: Name/Role

### Context
What is the situation and why do we need to make a decision?

### Decision
What did we decide to do and why?

### Alternatives Considered
- Alternative 1: Why it was not chosen
- Alternative 2: Why it was not chosen

### Consequences
- Positive consequence 1
- Positive consequence 2
- Negative consequence 1
- Trade-off 1

### Implementation Notes
How should this decision be implemented?

### Related Decisions
- Links to related decisions

### Related Specs
- Links to related specifications

---
```

## Purpose

This file maintains a record of all significant architectural and design decisions made in the Agent Factory project. This helps with:
- Understanding the rationale behind current design
- Avoiding revisiting settled questions
- Onboarding new team members
- Learning from past decisions

## Initial Decisions

---

## [DEC-001] Use Flat File Structure for Agent Repository
**Date**: 2026-01-28
**Status**: Accepted
**Decision Maker**: System Architect

### Context
The Agent Factory needs a file organization structure that is simple, maintainable, and easy to navigate. We must decide between a flat file structure vs. a hierarchical directory structure.

### Decision
Use a flat file structure where all agent files are stored at the root level or in a single `agents/` directory with no nested subdirectories.

### Alternatives Considered
- **Hierarchical Structure**: Organize agents in nested directories by category/type
  - Not chosen because it adds complexity and makes navigation harder
  - Can lead to debates about proper categorization
  - Makes file paths longer and more brittle
- **Database Storage**: Store agent data in a database
  - Not chosen because it requires additional infrastructure
  - Reduces transparency (can't easily browse in GitHub)
  - Makes version control more complex

### Consequences
- **Positive**: Simpler navigation and file discovery
- **Positive**: Easier to enforce naming conventions
- **Positive**: Works well with version control
- **Positive**: Reduces cognitive overhead
- **Negative**: May have many files in one directory as system grows
- **Trade-off**: Less organizational hierarchy, but compensated by tags and metadata

### Implementation Notes
- Store agent markdown files in flat `agents/` directory or root
- Use clear naming conventions for files
- Rely on tags in agents.yaml for categorization
- Enforce with validation tests

### Related Decisions
- DEC-002 (YAML Configuration)

### Related Specs
- SPEC-001 (File Structure)

---

## [DEC-002] Use agents.yaml for Agent Configuration
**Date**: 2026-01-28
**Status**: Accepted
**Decision Maker**: System Architect

### Context
We need a way to define agent metadata, configuration, and relationships. Options include YAML, JSON, TOML, or embedding in markdown frontmatter.

### Decision
Use a centralized `agents.yaml` file to define all agent metadata and configuration.

### Alternatives Considered
- **JSON Configuration**: Use agents.json
  - Not chosen because YAML is more human-readable
  - JSON doesn't support comments as easily
- **TOML Configuration**: Use agents.toml
  - Not chosen because YAML is more widely adopted in similar contexts
  - Team has more familiarity with YAML
- **Markdown Frontmatter**: Embed metadata in each agent file
  - Not chosen because it's harder to get a system-wide view
  - Makes validation and queries more complex

### Consequences
- **Positive**: Single source of truth for agent metadata
- **Positive**: Easy to validate schema
- **Positive**: Human-readable and editable
- **Positive**: Supports comments for documentation
- **Negative**: Requires keeping YAML in sync with markdown files
- **Trade-off**: Centralized vs. distributed metadata (chose centralized)

### Implementation Notes
- Define clear schema in agents.yaml
- Include validation rules in the file
- Reference agent markdown files via file_path
- Use YAML comments for inline documentation

### Related Decisions
- DEC-001 (Flat File Structure)
- DEC-003 (Required Headings)

### Related Specs
- SPEC-003 (Tags and Metadata)

---

## [DEC-003] Require Standardized Headings in Agent Files
**Date**: 2026-01-28
**Status**: Accepted
**Decision Maker**: System Architect

### Context
Agent documentation files need a consistent structure so users know what to expect and can find information easily.

### Decision
Require all agent files to include five specific headings in a defined order: Purpose, Inputs, Outputs, Behavior, and Constraints.

### Alternatives Considered
- **Freeform Documentation**: Let each agent define its own structure
  - Not chosen because it leads to inconsistency
  - Makes it harder to find information
  - Difficult to validate completeness
- **Minimal Requirements**: Only require one or two headings
  - Not chosen because it doesn't ensure sufficient documentation
  - Leaves too much ambiguity
- **More Extensive Requirements**: Require 10+ headings
  - Not chosen because it's too rigid
  - Creates unnecessary overhead for simple agents

### Consequences
- **Positive**: Consistent documentation structure
- **Positive**: Easy to validate
- **Positive**: Users know where to find information
- **Positive**: Forces thinking about key aspects of agent design
- **Negative**: May feel constraining for very simple agents
- **Trade-off**: Consistency vs. flexibility (chose consistency)

### Implementation Notes
- Document required headings in agents.md
- Create validation tests to check for headings
- Provide template in agents.md
- Allow additional headings beyond required ones

### Related Decisions
- DEC-002 (YAML Configuration)
- DEC-006 (Markdown Output)

### Related Specs
- SPEC-002 (Agent File Format)

---

## [DEC-004] Make specs.md, agent_runs.md, and decisions.md Append-Only
**Date**: 2026-01-28
**Status**: Accepted
**Decision Maker**: System Architect

### Context
Some files in the system need to maintain a complete historical record. We must decide whether to allow editing/deletion or enforce append-only behavior.

### Decision
Make specs.md, agent_runs.md, and decisions.md append-only files where content can only be added at the end, never modified or deleted.

### Alternatives Considered
- **Fully Editable Files**: Allow any modifications
  - Not chosen because it can lose historical context
  - Makes it harder to track what changed and why
  - Can lead to "rewriting history"
- **Version-Controlled Only**: Rely only on Git history
  - Not chosen because Git history can be complex to navigate
  - Having explicit append-only makes intent clearer
  - Provides an easy-to-read historical record
- **Separate Dated Files**: Create new files for each time period
  - Not chosen because it fragments information
  - Makes searching across time periods harder

### Consequences
- **Positive**: Preserves complete historical record
- **Positive**: Clear audit trail
- **Positive**: Prevents accidental loss of information
- **Positive**: Makes evolution of thinking visible
- **Negative**: Files will grow over time
- **Negative**: Can't fix typos in old entries
- **Trade-off**: Historical completeness vs. editability (chose history)

### Implementation Notes
- Add clear warnings at top of each append-only file
- Create validation tests to check git diffs
- Use horizontal rules to separate entries
- Include timestamps with each entry

### Related Decisions
- DEC-005 (No Fabrication)

### Related Specs
- SPEC-004 (Append-Only Files)

---

## [DEC-005] Enforce No Fabrication Rule
**Date**: 2026-01-28
**Status**: Accepted
**Decision Maker**: System Architect

### Context
Documentation and agent outputs must be trustworthy. We need to establish whether fabricated data or citations are acceptable.

### Decision
Prohibit fabrication of citations, results, or data. All information must be verifiable and accurate.

### Alternatives Considered
- **Allow Placeholders**: Permit example/dummy data
  - Partially accepted: placeholders OK if clearly marked as such
  - Not allowed for actual results or citations
- **Relaxed Policy**: Don't explicitly prohibit fabrication
  - Not chosen because it undermines trust
  - Can lead to confusion about what's real
  - Makes documentation less valuable

### Consequences
- **Positive**: Builds trust in documentation
- **Positive**: Ensures accuracy and reliability
- **Positive**: Makes information verifiable
- **Negative**: Requires more work to find real sources
- **Negative**: Can't use hypothetical examples as easily
- **Trade-off**: Convenience vs. accuracy (chose accuracy)

### Implementation Notes
- Document rule clearly in agents.md
- Include in validation requirements
- Manual review process for citations
- Mark placeholder data explicitly

### Related Decisions
- DEC-004 (Append-Only Files)

### Related Specs
- SPEC-005 (No Fabrication)

---

## [DEC-006] Prefer Markdown for All Output
**Date**: 2026-01-28
**Status**: Accepted
**Decision Maker**: System Architect

### Context
Agent outputs and documentation can use various formats. We need to standardize on a preferred format.

### Decision
Prefer Markdown format for all agent outputs, documentation, and reports.

### Alternatives Considered
- **Plain Text**: Use simple .txt files
  - Not chosen because it lacks formatting capabilities
  - Harder to structure complex information
- **HTML**: Use HTML for rich formatting
  - Not chosen because it's less human-readable in source form
  - More complex to write and maintain
- **Mixed Formats**: Allow each agent to choose format
  - Not chosen because it reduces consistency
  - Makes tooling more complex

### Consequences
- **Positive**: Consistent, readable format
- **Positive**: Great version control (text-based)
- **Positive**: Renders nicely on GitHub
- **Positive**: Wide tool support
- **Negative**: Limited formatting compared to rich formats
- **Trade-off**: Simplicity vs. rich formatting (chose simplicity)

### Implementation Notes
- Use .md extension for all documentation
- Follow CommonMark or GitHub Flavored Markdown
- Use code blocks with language specifiers
- Include examples in agents.md

### Related Decisions
- DEC-003 (Required Headings)

### Related Specs
- SPEC-006 (Markdown Preference)

---

## [DEC-007] Use MUST/SHOULD/MAY RFC 2119 Keywords
**Date**: 2026-01-28
**Status**: Accepted
**Decision Maker**: System Architect

### Context
Specifications need clear language to indicate requirement levels. We need a standard way to express mandatory vs. optional requirements.

### Decision
Use RFC 2119 keywords (MUST, SHOULD, MAY) to indicate requirement levels in all specifications and documentation.

### Alternatives Considered
- **Custom Keywords**: Create our own requirement levels
  - Not chosen because RFC 2119 is a well-known standard
  - Reinventing the wheel adds confusion
- **Natural Language Only**: Write requirements in plain English
  - Not chosen because it can be ambiguous
  - Harder to parse programmatically
- **Numeric Priorities**: Use P0, P1, P2 priority levels
  - Not chosen because it doesn't convey nature of requirement
  - Less intuitive for requirement specifications

### Consequences
- **Positive**: Clear, unambiguous requirement levels
- **Positive**: Industry-standard terminology
- **Positive**: Easy to parse and validate
- **Positive**: Well-understood by developers
- **Negative**: Requires familiarity with RFC 2119
- **Trade-off**: Precision vs. natural language (chose precision)

### Implementation Notes
- Use MUST for mandatory requirements
- Use SHOULD for recommended but not mandatory
- Use MAY for optional features
- Document usage in specs.md
- Create tests for all MUST requirements

### Related Decisions
- DEC-008 (Test Requirements)

### Related Specs
- SPEC-001 through SPEC-006 (all use MUST/SHOULD/MAY)

---

## [DEC-008] Require Pass/Fail Tests for All MUST Requirements
**Date**: 2026-01-28
**Status**: Accepted
**Decision Maker**: System Architect

### Context
MUST requirements need to be verifiable. We need to decide how to ensure requirements are actually met.

### Decision
Require every MUST requirement to have at least one associated pass/fail test with clear criteria.

### Alternatives Considered
- **Manual Verification Only**: Check requirements manually
  - Not chosen because it's not scalable
  - Prone to human error
  - Hard to maintain consistency
- **Optional Tests**: Make tests recommended but not required
  - Not chosen because requirements without tests are often ignored
  - Can't verify compliance
- **Integration Tests Only**: Use end-to-end tests
  - Not chosen because they don't test individual requirements clearly
  - Harder to debug when failures occur

### Consequences
- **Positive**: Requirements are verifiable
- **Positive**: Clear acceptance criteria
- **Positive**: Can automate validation
- **Positive**: Reduces ambiguity
- **Negative**: Requires effort to write tests
- **Negative**: Some requirements may be hard to test automatically
- **Trade-off**: Effort vs. verifiability (chose verifiability)

### Implementation Notes
- Include test definition with each MUST requirement in specs.md
- Define clear pass and fail criteria
- Document test ID for tracking
- Automate tests where possible
- Manual tests OK if automation isn't feasible

### Related Decisions
- DEC-007 (MUST/SHOULD/MAY keywords)

### Related Specs
- All specs include test definitions

---

## [DEC-009] Agent Analysis and Expansion Recommendations
**Date**: 2026-01-28
**Status**: Proposed
**Decision Maker**: System Analysis / GitHub Copilot Agent

### Context
The initial Agent Factory system was established with 7 core agents and 3 specialisms. After analyzing the system's coverage, gaps were identified in critical areas including testing, security, deployment, documentation, integration, data modeling, and performance optimization. A comprehensive analysis was needed to recommend additions that would enhance the system's capability without over-complicating it.

### Decision
Recommend a phased approach to expanding the agent system with 7 new agents and 7 new specialisms, prioritized by impact and necessity:

**Phase 1 (High Priority - Immediate):**
- Tester Agent - for systematic test creation and validation
- SecurityReviewer Agent - for security analysis and compliance
- Security Specialism - security standards
- Testing Specialism - testing standards

**Phase 2 (Medium Priority - Next Quarter):**
- Deployer Agent - for deployment readiness
- DocWriter Agent - for user-facing documentation
- Integrator Agent - for API and integration design
- DataModeler Agent - for data architecture
- API Design Specialism
- Deployment Specialism
- Documentation Specialism

**Phase 3 (Low Priority - As Needed):**
- Optimizer Agent - for performance optimization
- Data Specialism
- Performance Specialism

### Alternatives Considered
- **Expand Existing Agents**: Add responsibilities to current agents
  - Not chosen because it would violate single responsibility principle
  - Would make agents too complex and harder to use
  - Each specialty requires dedicated focus

- **Create Mega-Agent**: Create one "Quality" agent to handle testing, security, performance
  - Not chosen because specialization is more effective
  - Would be too broad and lack deep expertise
  - Goes against the factory pattern philosophy

- **Minimal Expansion**: Only add 1-2 most critical agents
  - Not chosen because it leaves too many gaps
  - Would require revisiting expansion soon
  - Better to have comprehensive plan even if phased

- **Maximum Expansion**: Add 15+ agents covering every niche
  - Not chosen because it adds unnecessary complexity
  - Many niches don't have sufficient use cases yet
  - Can lead to confusion about which agent to use

### Consequences
**Positive:**
- Comprehensive coverage of software development lifecycle
- Specialized expertise in critical areas (testing, security)
- Better quality outputs with systematic validation
- Clearer separation of concerns
- Production-ready artifacts
- Enhanced security posture
- Better deployment support

**Negative:**
- More agents to learn and understand
- Increased coordination complexity
- More maintenance burden
- Steeper learning curve for new users
- Need to update documentation and tooling

**Trade-offs:**
- Completeness vs. Simplicity (chose completeness with phased approach)
- Specialization vs. Generalization (chose specialization)
- Immediate implementation vs. Phased rollout (chose phased)

### Implementation Notes

**For Each New Agent:**
1. Create agent definition file in agents/ directory
2. Follow required structure (Purpose, Inputs, Outputs, Behavior, Constraints)
3. Add entry to agents.yaml with unique ID
4. Tag appropriately with existing or new tags
5. Run validation: ./validate_agents.sh
6. Update integration documentation

**For Each New Specialism:**
1. Create specialism file in specialisms/ directory
2. Define purpose, operating rules, outputs, quality gates
3. Include test acceptance checks
4. Reference from appropriate agent definitions

**New Tags to Add:**
- security (for SecurityReviewer)
- quality (for Tester)
- performance (for Optimizer)
- data (for DataModeler)
- api (for Integrator)

**Workflow Integration:**
Update orchestration to support optional agent invocation based on artifact type and requirements.

**Documentation Updates:**
- Update agents.md with new agent patterns
- Update README.md with expanded agent list
- Create workflow diagrams showing agent interactions
- Update COPILOT_INTEGRATION.md with new agent capabilities

### Phased Rollout Strategy

**Phase 1 Success Criteria:**
- Tester and SecurityReviewer agents created and validated
- At least 2 test cases using new agents successfully completed
- Security and Testing specialisms documented
- No regression in existing agent functionality

**Phase 2 Entry Criteria:**
- Phase 1 agents proven valuable in practice
- User feedback incorporated
- Clear use cases for Phase 2 agents identified

**Phase 3 Entry Criteria:**
- Performance or data-heavy projects emerge
- Demonstrated need for optimization or data modeling

### Benefits and Mitigation

**Key Benefits:**
- **Quality**: Systematic testing and security review
- **Completeness**: All lifecycle stages covered
- **Scalability**: Can handle more complex projects
- **Trust**: Better validation and security

**Complexity Mitigation:**
- Phased rollout reduces learning curve
- Clear documentation of when to use each agent
- ProjectManager orchestrates, users don't need to know all agents
- Optional agents - only invoke what's needed

### Related Decisions
- DEC-001 (Flat File Structure) - New agents follow same pattern
- DEC-002 (YAML Configuration) - New agents defined in agents.yaml
- DEC-003 (Required Headings) - New agents use standard structure
- DEC-008 (Test Requirements) - Tester agent enhances this capability

### Related Specs
- SPEC-001 (File Structure) - New agents maintain flat structure
- SPEC-002 (Agent File Format) - New agents follow format
- SPEC-003 (Tags and Metadata) - New agents properly tagged

### Output Reference
- Detailed analysis: agent_recommendations.md
- Run log: agent_runs.md #002

---

## [DEC-010] Phase 1 Agent Implementation - Tester and SecurityReviewer
**Date**: 2026-01-29
**Status**: Implemented
**Decision Maker**: GitHub Copilot Agent / User Request

### Context
Following DEC-009 (Agent Expansion Recommendations), the user requested implementation of the recommendations starting with Phase 1 high-priority agents. Phase 1 focused on addressing critical gaps in testing and security that are currently handled ad-hoc by existing agents.

### Decision
Implement Phase 1 agents and specialisms as specified in agent_recommendations.md:
1. **Tester Agent** - Systematic test creation and validation
2. **SecurityReviewer Agent** - Security analysis and compliance
3. **Testing Specialism** - Testing standards and best practices
4. **Security Specialism** - Security standards and OWASP compliance

### Alternatives Considered
- **Implement All Phases at Once**: Create all 7 recommended agents
  - Not chosen because phased approach allows for validation and feedback
  - Too many changes at once increases risk
  
- **Extend Existing Agents**: Add testing and security to Skeptic/Builder
  - Not chosen because violates single responsibility principle
  - Specialized agents provide deeper expertise
  - Would make existing agents too complex

- **Wait for More Feedback**: Delay implementation pending additional review
  - Not chosen because user explicitly requested implementation
  - Phase 1 agents address critical, well-understood gaps

### Consequences
**Positive:**
- Systematic testing now available (was ad-hoc before)
- Dedicated security expertise (was scattered across Skeptic)
- Clear quality gates for testing and security
- Testing specialism provides standards for test creation
- Security specialism provides OWASP Top 10 compliance framework
- Agents follow established patterns and conventions
- All validation requirements met (except known script bug)

**Negative:**
- Increased system complexity (9 agents vs 7)
- More files to maintain
- Users need to learn new agent capabilities
- Validation script bug discovered (TEST-003-2)

**Trade-offs:**
- Specialization vs. Simplicity (chose specialization)
- Immediate implementation vs. Extended review (chose immediate)
- Complete coverage vs. Focused delivery (chose focused Phase 1)

### Implementation Notes

**Agent Design:**
- Both agents follow required 5-heading structure
- Clear separation of concerns:
  - Tester: Validation that requirements are met
  - SecurityReviewer: Security vulnerabilities and compliance
  - Skeptic: Adversarial breaking and edge cases (unchanged)
- Agents integrate into workflow between Builder and Skeptic
- Both agents reference their respective specialisms

**Specialisms Created:**
- Testing.md: AAA pattern, test types, coverage metrics, naming conventions
- Security.md: OWASP Top 10, secure coding practices, severity classification

**Configuration Changes:**
- Added tags: security, quality
- Added 2 agent entries to agents.yaml
- Maintained flat file structure
- All files in appropriate directories (agents/, specialisms/)

**Validation:**
- Python YAML validation: All checks pass
- Agent files: All required headings present
- File structure: Flat structure maintained
- Tags: All from allowed list
- Known issue: Validation script TEST-003-2 bug (extracts "id:" literal instead of values)

**Workflow Integration:**
Enhanced workflow:
```
Architect → Builder → Tester → SecurityReviewer → Skeptic → Editor → ProjectManager
```

Tester validates requirements are met, SecurityReviewer checks security compliance, Skeptic finds edge cases.

### Future Considerations
- Phase 2 agents can build on this foundation
- Tester and SecurityReviewer can be referenced by Phase 2 agents
- Validation script bug should be fixed (use awk $3 instead of $2)
- Consider adding example test and security reports

### Related Decisions
- DEC-009 (Agent Expansion Recommendations) - parent decision
- DEC-003 (Required Headings) - followed
- DEC-001 (Flat File Structure) - maintained

### Related Specs
- SPEC-001 (File Structure) - compliant
- SPEC-002 (Agent File Format) - compliant
- SPEC-003 (Tags and Metadata) - compliant
- SPEC-004 (Append-Only Files) - followed for this entry

### Output References
- Agent: agents/Tester.md
- Agent: agents/SecurityReviewer.md
- Specialism: specialisms/Testing.md
- Specialism: specialisms/Security.md
- Configuration: agents.yaml (updated)
- Run log: agent_runs.md #003

---

## [DEC-011] Remove Flat File Structure Requirement
**Date**: 2026-01-29
**Status**: Accepted
**Decision Maker**: User Request / alexanderholman
**Supersedes**: DEC-001

### Context
The original AgentFactory design enforced a strict flat file structure (DEC-001) where all agent files had to be stored at a single directory level with no nested subdirectories. This was intended to keep things simple and easy to navigate.

However, this constraint was proving limiting as the system grew and as users wanted compatibility with organizational patterns used by major AI agent platforms including:
- GitHub Copilot
- OpenAI ChatGPT  
- Google Gemini
- Agent-based IDEs (OpenCode.ai)
- Google Colab

The user explicitly requested removal of the flat file structure requirement to enable more flexible organization.

### Decision
Remove the flat file structure requirement (DEC-001 superseded) and adopt a flexible directory structure approach that:
1. Allows nested subdirectories for logical organization
2. Maintains agents.yaml as the authoritative registry
3. Supports conventions from major AI agent platforms
4. Permits both flat and nested structures (user choice)
5. Remains compatible with existing flat structure

### Alternatives Considered
- **Keep Flat Structure**: Maintain the original constraint
  - Not chosen because it was too limiting
  - Didn't align with AI platform conventions
  - User explicitly requested change
  
- **Mandatory Nested Structure**: Require specific directory hierarchy
  - Not chosen because it's too prescriptive
  - Different projects have different organization needs
  - Flexibility is more important than uniformity

- **Separate Repositories**: Split agents into multiple repos
  - Not chosen because it adds complexity
  - Harder to maintain and coordinate
  - Single repo with flexible structure is simpler

### Consequences
**Positive:**
- More flexible organization options
- Can group related agents logically (by role, domain, priority)
- Better compatibility with AI agent platforms
- Supports growth without flat directory becoming cluttered
- Users can choose organization that fits their needs
- Easier migration from other AI agent systems

**Negative:**
- Slightly more complex file discovery (mitigated by agents.yaml)
- Need to update validation script
- Potential for inconsistent organization across projects
- Existing documentation needs updating

**Trade-offs:**
- Flexibility vs. Simplicity (chose flexibility)
- User control vs. Enforced consistency (chose user control)
- Platform compatibility vs. Custom patterns (chose compatibility)

### Implementation Notes

**Updated Files:**
- `.github/copilot-instructions.md`: Changed MUST to SHOULD, allowed nested directories
- `specs.md`: Added SPEC-007 (Flexible Structure), deprecated SPEC-001
- `decisions.md`: This entry (DEC-011), supersedes DEC-001
- `agents.yaml`: Validation rules updated (file existence check remains)
- `validate_agents.sh`: Remove flat structure test (TEST-001-1)

**Migration Path:**
- Existing flat structure remains valid - no forced migration
- New agents can use nested directories
- Projects can reorganize gradually
- agents.yaml is the single source of truth for agent locations

**Platform Compatibility:**
- Supports GitHub Copilot workspace conventions
- Compatible with OpenAI ChatGPT project structures
- Aligns with Google Gemini organization patterns
- Works with agent-based IDEs (OpenCode.ai)
- Compatible with Google Colab notebook structures

**Examples of Supported Structures:**
```
# Option 1: Flat (original, still valid)
agents/
├── Architect.md
├── Builder.md
└── Tester.md

# Option 2: Grouped by role
agents/
├── core/
│   └── Architect.md
├── quality/
│   └── Tester.md
└── security/
    └── SecurityReviewer.md

# Option 3: Mixed
agents/
├── Architect.md
├── testing/
│   └── Tester.md
└── security/
    └── SecurityReviewer.md
```

### Validation Changes
- Removed TEST-001-1 (flat structure check)
- Keep TEST-007-1 (file existence check via agents.yaml)
- agents.yaml file_path can now include subdirectories
- Validation focuses on agent quality, not structure

### Documentation Updates
- README.md: Update architecture description
- COPILOT_INTEGRATION.md: Note flexible structure
- agents.md: Remove flat structure guidance
- All references to "flat file structure" updated or removed

### Backward Compatibility
- Existing flat structure files work without changes
- agents.yaml schema unchanged (file_path supports subdirs)
- No breaking changes for existing agents
- Validation still passes for flat structures

### Related Decisions
- DEC-001 (Use Flat File Structure) - SUPERSEDED by this decision
- DEC-002 (YAML Configuration) - unchanged
- DEC-003 (Required Headings) - unchanged

### Related Specs
- SPEC-001 (Flat File Structure) - DEPRECATED
- SPEC-007 (Flexible Directory Structure) - NEW, replaces SPEC-001
- SPEC-002 (Agent File Format) - unchanged

### Output References
- Updated: .github/copilot-instructions.md
- Added: SPEC-007 in specs.md
- Deprecated: SPEC-001 in specs.md
- Decision: This entry (DEC-011)

---

## [DEC-012] Add AGENTS.md for OpenAI ChatGPT Compatibility
**Date**: 2026-01-29
**Status**: Implemented
**Decision Maker**: User Request / alexanderholman

### Context
The user requested review and alignment with the OpenAI ChatGPT agents.md standard (https://github.com/agentsmd/agents.md). This standard defines a simple markdown file format (AGENTS.md) that provides practical instructions for AI agents working on a project.

The AgentFactory project already had comprehensive documentation in `.github/copilot-instructions.md`, but this was GitHub Copilot-specific. The AGENTS.md format is a simpler, more universal format that works across multiple AI platforms including OpenAI ChatGPT.

### Decision
Create an AGENTS.md file following the OpenAI standard to complement existing documentation:
1. Simple, practical instructions for AI agents
2. Focus on common development tasks
3. Quick reference format
4. Compatible with OpenAI ChatGPT and other AI agents
5. Complement (not replace) existing `.github/copilot-instructions.md`

### Alternatives Considered
- **Replace copilot-instructions.md**: Replace with AGENTS.md only
  - Not chosen because copilot-instructions.md has valuable detail
  - Both formats serve different purposes (detailed vs. quick reference)
  
- **Ignore the Standard**: Don't add AGENTS.md
  - Not chosen because user explicitly requested alignment
  - OpenAI standard is gaining adoption
  - Improves compatibility across AI platforms

- **Merge into README**: Add instructions to README.md
  - Not chosen because README is for humans, AGENTS.md is for AI
  - Separation of concerns is clearer
  - Follows established pattern (README for humans, AGENTS.md for AI)

### Consequences
**Positive:**
- Better compatibility with OpenAI ChatGPT
- Simple, quick reference for AI agents
- Follows emerging standard in AI agent ecosystem
- Complements existing detailed documentation
- Easy for AI agents to find and parse
- Platform-agnostic instructions

**Negative:**
- Additional file to maintain
- Some duplication with copilot-instructions.md
- Need to keep both files in sync when making architectural changes

**Trade-offs:**
- Completeness vs. Simplicity (AGENTS.md is simpler)
- Detailed vs. Practical (AGENTS.md is more practical)
- GitHub-specific vs. Universal (AGENTS.md is universal)

### Implementation Notes

**AGENTS.md Structure:**
- Project Overview - Brief description
- Key Files and Structure - What files exist and their purpose
- Development Workflow - How to add agents, run validation
- Coding Conventions - Style guidelines
- Testing - How to validate changes
- Common Tasks - Frequently used commands
- Important Rules - MUST/MUST NOT lists
- Directory Structure - Examples of supported structures
- Troubleshooting - Common issues and solutions
- Further Documentation - Links to other docs
- Platform Compatibility - List of supported platforms

**Documentation Strategy:**
- `.github/copilot-instructions.md` - Comprehensive, GitHub Copilot-focused
- `AGENTS.md` - Simple, practical, platform-agnostic (NEW)
- `agents.md` - Agent definition template and guidelines
- `README.md` - Human-readable project overview
- `specs.md` - Technical specifications (append-only)
- `decisions.md` - Architectural decisions (this file)

**Key Differences from copilot-instructions.md:**
- Simpler, more concise format
- Focus on practical tasks over comprehensive rules
- More code examples and commands
- Platform-agnostic language
- Quick troubleshooting section

**Compatibility Benefits:**
- OpenAI ChatGPT can easily parse and follow instructions
- Google Gemini can reference the file
- Any AI agent looking for AGENTS.md will find it
- Follows emerging convention in AI agent ecosystem
- Simple markdown format works everywhere

### Related Decisions
- DEC-011 (Flexible Directory Structure) - AGENTS.md documents this
- DEC-001 (Flat File Structure) - Superseded, AGENTS.md reflects current state

### Related Specs
- SPEC-007 (Flexible Directory Structure) - Documented in AGENTS.md
- SPEC-002 (Agent File Format) - Explained in AGENTS.md
- SPEC-003 (Tags and Metadata) - Covered in AGENTS.md

### Output References
- Created: AGENTS.md (5694 bytes, 202 lines)
- Standards reference: https://github.com/agentsmd/agents.md

---

## [DEC-013] Add OpenCodeManager Agent for General Orchestration
**Date**: 2026-02-05
**Status**: Implemented
**Decision Maker**: User Request / OpenCodeManager setup

### Context
OpenCode is being used as a general-purpose CLI agent, and we need a dedicated manager role to coordinate session flow, enforce AgentFactory rules, and drive consistent handoffs between specialized agents.

### Decision
Add a new agent definition named OpenCodeManager to serve as the orchestration and session-control role for OpenCode usage. This agent focuses on intake, planning, safe execution, and role handoffs while enforcing repository constraints and append-only logging rules.

### Alternatives Considered
- **Reuse ProjectManager**: Not chosen because ProjectManager is optimized for packaging and handoffs at the end of the pipeline, not continuous session control.
- **Rely on ChatGPT Generalist**: Not chosen because it lacks an explicit orchestration contract and clear session-control responsibilities.

### Consequences
**Positive:**
- Clear, reusable orchestration contract for OpenCode sessions
- Better consistency in tool usage and repo hygiene
- Easier handoff between specialized roles

**Negative:**
- Additional agent to maintain
- Overlap risk with ProjectManager responsibilities

**Trade-offs:**
- Central control vs. flexibility (improves consistency but adds process)

### Implementation Notes
- Added agent file: `agents/opencode/OpenCodeManager.md`
- Registered in `agents.yaml` with ID `opencode-manager-001`
- Uses existing allowed tags to avoid schema changes

### Related Decisions
- DEC-011 (Flexible Directory Structure)
- DEC-012 (AGENTS.md for OpenAI compatibility)

### Related Specs
- SPEC-002 (Agent File Format)
- SPEC-003 (Tags and Metadata)
- SPEC-007 (Flexible Directory Structure)

---

## [DEC-018] Adopt Script-First "Only Write Once" Execution Policy
**Date**: 2026-02-07
**Status**: Implemented
**Decision Maker**: OpenCodeManager / User Request

### Context
The user requested a stricter execution model where agents should operate as natural language to script routing and parameterization, avoid repeated ad-hoc manual actions, and prioritize local model workflows when model-based execution is required.

### Decision
Adopt an explicit script-first automation policy across AgentFactory:
1. Agents MUST check for existing scripts/workflows before executing repeatable tasks.
2. If no script exists for a repeatable action, agents MUST create a reusable script first.
3. Script execution MUST be parameterized and replayable.
4. For model-required tasks, agents SHOULD prefer local model training/inference when feasibility gates pass; otherwise use deterministic scripted fallback.

Implementation:
1. Add trait: `traits/script-first-automation.md`.
2. Add workflow: `workflows/nl-to-script-routing.md`.
3. Add script registry: `scripts/registry.yaml`.
4. Update policy docs and agent behavior files to enforce routing and evidence capture.

### Alternatives Considered
- **Keep ad-hoc execution as default**
  - Not chosen because repeatability and consistency degrade over time.
- **Mandate local model training for every model-related task without gates**
  - Not chosen because it can be infeasible on constrained hardware/time budgets.

### Consequences
**Positive:**
- Stronger reproducibility and lower operational drift.
- Better human-computer interface for natural language task routing.
- Clear path to automation reuse and easier validation.

**Negative:**
- Initial overhead to script and register new repeatable actions.
- Additional maintenance for script registry metadata.

### Implementation Notes
- Feasibility gates for local model policy: data availability, hardware budget, runtime budget, acceptance metric definition.
- Fallback path is deterministic scripted non-ML execution where applicable.

### Related Decisions
- DEC-017 (Add `hlmn/memory` OpenCode Skill Documentation)
- DEC-016 (Add ProjectSpec Routing for Condensed Matter Interface Work)
- DEC-012 (Add AGENTS.md for OpenAI ChatGPT Compatibility)

### Related Specs
- SPEC-002 (Agent File Format)

### Output References
- Added: `traits/script-first-automation.md`
- Added: `workflows/nl-to-script-routing.md`
- Added: `scripts/registry.yaml`
- Updated: `AGENTS.md`
- Updated: `agents.md`
- Updated: `workflows/WORKFLOWS.md`
- Updated: `traits/TRAITS.md`
- Updated: `agents/ProjectSpec.md`
- Updated: `agents/ProjectManager.md`
- Updated: `agents/Builder.md`
- Updated: `agents/ChatGPT.md`
- Updated: `README.md`

---

---

## [DEC-019] Add NL Script Resolver and CI Enforcement for Repeatable Task References
**Date**: 2026-02-07
**Status**: Implemented
**Decision Maker**: OpenCodeManager / User Request

### Context
After adopting the script-first policy (DEC-018), enforcement and runtime ergonomics needed concrete tooling so agents could consistently resolve intents to scripts and CI could detect repeatable task documentation that omits script references.

### Decision
Implement two operational utilities and wire them into validation:
1. Add `scripts/resolve.py` to map natural language intents to `scripts/registry.yaml` entries.
2. Add `scripts/check_repeatable_script_refs.py` to fail validation when repeatable tasks are documented without nearby script references.
3. Add registry entries for both scripts and include the check in `validate_agents.sh` and CI triggers.

### Alternatives Considered
- **Manual script selection only**
  - Not chosen because it increases ambiguity and operator drift.
- **Advisory-only repeatable documentation guidance**
  - Not chosen because non-enforced policy degrades over time.

### Consequences
**Positive:**
- Better natural-language to script selection and parameterization UX.
- Enforceable policy guardrails for repeatable task documentation.
- Higher reproducibility and lower manual variance.

**Negative:**
- Slight additional maintenance burden for script metadata and checks.

### Implementation Notes
- Repeatable-reference check currently scans markdown task docs under `workflows/` and `tasks/`.
- Accepted script references include `scripts/...`, `scripts/registry.yaml`, or `[SCRIPT:<id>]`.

### Related Decisions
- DEC-018 (Adopt Script-First "Only Write Once" Execution Policy)
- DEC-017 (Add `hlmn/memory` OpenCode Skill Documentation)

### Related Specs
- SPEC-006 (Markdown Output)

### Output References
- Added: `scripts/resolve.py`
- Added: `scripts/check_repeatable_script_refs.py`
- Updated: `scripts/registry.yaml`
- Updated: `validate_agents.sh`
- Updated: `.github/workflows/validate-agents.yml`
- Updated: `AGENTS.md`

---

---

## [DEC-020] Add `route_and_run` Wrapper for NL-to-Script Execution
**Date**: 2026-02-07
**Status**: Implemented
**Decision Maker**: OpenCodeManager / User Request

### Context
The script-first policy now had resolver and CI checks, but execution still required separate manual steps (resolve, then run). A single wrapper was needed to improve natural-language HCI and reduce operator friction.

### Decision
Add `scripts/route_and_run.py` as a wrapper that:
1. Selects a script from `scripts/registry.yaml` via `--script-id` or NL routing (`--query` / `--intent`).
2. Applies parameter substitutions for placeholders like `<request>` via `--arg key=value`.
3. Defaults to dry-run and executes only with `--execute`.

### Alternatives Considered
- **Keep separate resolve + manual execute steps only**
  - Not chosen because it adds overhead and increases execution variance.
- **Execute by default without dry-run**
  - Not chosen because dry-run default is safer for command transparency.

### Consequences
**Positive:**
- More natural NL-to-script operation path.
- Safer and more transparent execution via rendered-command preview.
- Better consistency across operators.

**Negative:**
- Additional script to maintain.

### Implementation Notes
- Wrapper supports explicit script pinning (`--script-id`) or resolver mode.
- Missing placeholders fail closed with clear errors.

### Related Decisions
- DEC-019 (Add NL Script Resolver and CI Enforcement for Repeatable Task References)
- DEC-018 (Adopt Script-First "Only Write Once" Execution Policy)

### Related Specs
- SPEC-008 (Script-First Automation)

### Output References
- Added: `scripts/route_and_run.py`
- Updated: `scripts/registry.yaml`
- Updated: `AGENTS.md`

---

---

## [DEC-021] Strengthen Script-First Operations with Registry Schema, Capability Matching, and Dry-Run Policy Checks
**Date**: 2026-02-07
**Status**: Implemented
**Decision Maker**: OpenCodeManager / User Request

### Context
After rolling out script-first routing, we needed stronger operational guardrails to keep automation quality high as the registry grows: schema consistency, better resolver ranking, explicit dry-run execution safety, and reusable authoring templates.

### Decision
1. Add registry schema validation (`scripts/validate_registry.py`) and enforce it via validation suite.
2. Add dry-run policy validation (`scripts/check_dry_run_conventions.py`) for execute-intent scripts.
3. Add `capabilities` metadata to every registry entry and include it in resolver/routing scoring.
4. Extend repeatable reference checks to include `specs.md` sections marked with repeatable language.
5. Add script authoring templates and runbook docs (`scripts/templates/*`, `scripts/route-and-run.md`).
6. Add ML feasibility checklist template (`tasks/ml-feasibility-checklist.md`) for model-required tasks.
7. Add AGENTS operator quickstart with the top 3 session commands.

### Alternatives Considered
- **Keep soft guidance only**
  - Not chosen because policy drift tends to increase without executable checks.
- **Use strict external schema tooling only**
  - Not chosen to keep bootstrap simple with Python standard repo dependencies.

### Consequences
**Positive:**
- Better reproducibility and lower registry drift.
- Safer execution defaults through explicit dry-run policy checks.
- Improved natural-language routing quality via capabilities.
- Faster onboarding with templates and quickstart runbook.

**Negative:**
- Additional validation steps and maintenance overhead.

### Related Decisions
- DEC-020 (Add `route_and_run` Wrapper for NL-to-Script Execution)
- DEC-019 (Add NL Script Resolver and CI Enforcement for Repeatable Task References)
- DEC-018 (Adopt Script-First "Only Write Once" Execution Policy)

### Related Specs
- SPEC-008 (Script-First Automation)

### Output References
- Added: `scripts/validate_registry.py`
- Added: `scripts/check_dry_run_conventions.py`
- Added: `scripts/route-and-run.md`
- Added: `scripts/templates/new_script.py`
- Added: `scripts/templates/README.md`
- Added: `tasks/ml-feasibility-checklist.md`
- Updated: `scripts/registry.yaml`
- Updated: `scripts/resolve.py`
- Updated: `scripts/route_and_run.py`
- Updated: `scripts/check_repeatable_script_refs.py`
- Updated: `validate_agents.sh`
- Updated: `AGENTS.md`
- Updated: `README.md`

---

---

## [DEC-022] Adopt the AF Python Vertical Slice and MCP-First ChatGPT Boundary
**Date**: 2026-07-18
**Status**: Implemented
**Decision Maker**: Codex / User Request

### Context
AgentFactory previously governed Markdown and YAML agent definitions but did not provide
an executable framework. The requested AF way of working needed one tested application
core exposed consistently through CLI, HTML MVC, REST, GraphQL, and ChatGPT-compatible
tooling without making a language model synonymous with an agent.

### Decision
1. Target Python 3.12 with a `src` package, uv lockfile, Ruff, strict mypy, pytest, and a
   90 percent branch-aware coverage floor.
2. Implement `classify → route → execute → verify → record → test → reuse` in a
   transport-neutral kernel.
3. Use explicit service providers and singleton/transient container scopes as the
   Laravel-inspired composition vocabulary, while retaining Python protocols, dataclasses,
   modules, and constructor injection.
4. Put Task execution and CRUD behind one application controller used by Typer, FastAPI
   HTML/REST, Strawberry GraphQL, and FastMCP adapters.
5. Prefer static and procedural procedures. Install a generative procedure only when an
   embedding host supplies both a model provider and its verifier; never reuse generative
   output by default.
6. Treat MCP as the executable ChatGPT integration and the `.agents/skills/af-work/`
   repository skill as operating guidance. Local stdio and loopback HTTP are development
   transports; public ChatGPT use requires a real authenticated HTTPS deployment.
7. Configure GitHub Copilot with repository/path instructions, scoped `AGENTS.md` files,
   Python CI, and the required single-job `copilot-setup-steps.yml` workflow.

### Alternatives Considered
- **Use a Python web framework as the domain architecture**
  - Not chosen because transport frameworks would leak into the execution kernel and make
    CLI, GraphQL, and MCP parity harder to maintain.
- **Let unknown work silently fall through to an LLM**
  - Not chosen because it bypasses explicit routing and verification.
- **Implement ChatGPT behaviour only as a skill**
  - Not chosen because a skill provides instructions, not the executable and typed CRUD
    capabilities supplied by MCP.

### Consequences
**Positive:**
- One DRY, test-driven vertical slice serves every requested interface.
- Deterministic work is cheap, independently verified, traced, regression-gated, and
  reusable.
- Models, persistence, reuse storage, and transports are replaceable at explicit seams.
- GitHub Copilot receives scoped context and a deterministic development environment.

**Negative:**
- Remote ChatGPT access is not available until authentication and HTTPS deployment exist.
- The synchronous MVP still needs migrations, multi-user authorization, durable reuse,
  observability, and production operations.

### Related Specs
- SPEC-008 (Script-First Automation)
- SPEC-009 (Executable AF Python Runtime)

### Output References
- Added: `pyproject.toml`, `uv.lock`, `.python-version`
- Added: `src/af/`, `tests/`, `docs/`
- Added: `.agents/skills/af-work/`
- Added: `.github/instructions/`, `.github/workflows/python.yml`
- Added: `.github/workflows/copilot-setup-steps.yml`
- Added: `scripts/af_dev.py`
- Updated: `README.md`, `AGENTS.md`, `project_context_agent_factory.yaml`, `repo.yaml`

---

## [DEC-023] Establish AgentFramework as the Canonical AF Repository
**Date**: 2026-07-18
**Status**: Implemented
**Decision Maker**: Alexander Holman / Codex

### Context
The AF Python vertical slice was first assembled in the AgentFactory working tree because
that repository held the prior specifications, agent definitions, and governance rules.
The executable framework needs an independent identity without discarding that lineage.

### Decision
1. Use the private `alexanderholman/AgentFramework` repository as the canonical home for
   executable AF development.
2. Import the complete governed working snapshot, excluding Git history, environments,
   caches, coverage output, and runtime storage.
3. Preserve AgentFactory references inside append-only historical entries while changing
   active manifests, links, setup instructions, Copilot guidance, and task tracking to
   AgentFramework.
4. Track the initial Python vertical slice in AgentFramework issue #1 and develop it on
   `agent/af-python-vertical-slice`.

### Consequences
**Positive:**
- AF has a clear project boundary and clean repository history.
- The original AgentFactory repository remains untouched.
- Existing definitions, decisions, specifications, and validators remain available as
  inherited foundations.

**Negative:**
- Some historical documentation correctly retains the old AgentFactory name and links.
- The private repository requires explicit GitHub access for cloning and integrations.

### Related Specs
- SPEC-004 (Append-Only File Management)
- SPEC-009 (Executable AF Python Runtime)
- SPEC-010 (AgentFramework Repository Identity)

### Output References
- Renamed: `project_context_agent_factory.yaml` to
  `project_context_agent_framework.yaml`
- Updated: `repo.yaml`, `INSTALL.md`, active GitHub links, Copilot instructions, and
  `tasks/wip.md`
- Preserved: AgentFactory append-only history and governance assets

---

## [DEC-024] Remove Direct Generative Answers from the Active AF Runtime
**Date**: 2026-07-18
**Status**: Implemented
**Decision Maker**: Alexander Holman / Copilot

### Context
DEC-022 item 5 allowed a future generative execution branch when paired with a verifier.
The current product contract is stricter: AF is selector-only for model use, and user-facing
answers must come from installed deterministic procedures rather than direct generation.

### Decision
1. Supersede DEC-022 item 5 for the active runtime.
2. Remove the direct generative execution tier and any verifier-routing branch dedicated to
   generative output.
3. Keep models optional and constrained to proposing registered procedures only.
4. Require immutable catalogue validation, fixed reason codes, and pre-execution policy
   checks before any selector-backed procedure execution.
5. Keep remote selector execution denied unless a future explicit policy and adapter design
   is implemented.

### Consequences
**Positive:**
- AF now matches the released selector-only contract across code, docs, and MCP metadata.
- Deterministic procedures remain independently verified and reusable without answer
  generation seams.
- Selector failures and unsupported work fail closed to human handoff.

**Negative:**
- Unknown work stays unsupported until more deterministic procedures or a trusted selector
  adapter are added.
- Future remote or richer selector integrations now require an explicit follow-up design.

### Related Specs
- SPEC-009 (Executable AF Python Runtime)
- SPEC-011 (Human-First Constrained Selection Boundary)

### Output References
- Updated: `src/af/framework/types.py`, `src/af/domain/verification.py`,
  `src/af/interfaces/mcp.py`, `docs/architecture.md`
- Preserved: historical append-only references to the superseded generative design

---

## [DEC-025] Adopt the Capability Ladder and Scoped Reviewed Generation
**Date**: 2026-07-21
**Status**: Approved
**Decision Maker**: Alexander Holman

### Context
The AF design discussion defines a capability ladder that climbs only as high as necessary.
It also treats algorithms as functions and distinguishes simple functions from complex
systems of functions. DEC-024 correctly removed an unconstrained direct generative-answer
route from the active runtime, but a permanent absolute prohibition is stricter than the
intended product policy. Generative answers are plausible, should be rare, require review,
and may or may not be reusable depending on user acceptance scope and system requirements.
Children must receive deterministic procedural responses and must never use generative AI.

### Decision
1. Adopt the ordered ladder in SPEC-012.
2. Select the minimum-cost capability only after requirements and required quality are met.
3. Require every request to carry a typed acceptance scope that constrains maximum ladder
   level, release review, and caching/reuse.
4. Hard-limit child scope to cached or deterministic function systems at levels 0 through 2.
5. Permit generated answers only in an explicitly generative scope and only after review.
6. Permit planners to propose typed DAGs of registered capabilities, never unvalidated code,
   hidden capabilities, or direct policy bypasses.
7. Treat cacheability as a separate decision from correctness review; support `no-store`,
   expiry, provenance, and capability/model fingerprint requirements.
8. Keep DEC-024's current runtime denial in force until the scoped policy, review gate, cache
   gate, and complete regression tests are implemented.

### Consequences
**Positive:**
- AF matches the intended capability-first architecture rather than becoming an AI router.
- Simple deterministic work remains preferred while reviewed generation remains available
  for rare cases that genuinely require it.
- Child safety is enforceable as a hard execution policy rather than a prompt instruction.
- Reuse remains valuable without assuming every reviewed output is safe to cache.

**Negative:**
- Generative execution remains unavailable until acceptance scopes and gates are complete.
- Capability scoring now requires explicit quality, requirements, cost, and provenance data.
- Review and cache policy introduce additional state and lifecycle transitions.

### Related Specs
- SPEC-011 (Human-First Constrained Selection Boundary)
- SPEC-012 (Capability Ladder and Scoped Generative Execution)

### Output References
- Added: `docs/capability-ladder.md`
- Updated: append-only `specs.md`, `decisions.md`

---

## [DEC-026] Accept and Clarify Scoped Capability Selection
**Date**: 2026-07-21
**Status**: Accepted
**Decision Maker**: Alexander Holman / Zordon policy review

### Context
Review of DEC-025 identified ambiguity between cross-level cost minimization and climbing
only as high as necessary, incomplete scope authority and reuse definitions, an unreachable
human handoff under automated ceilings, and possible review bypass through caches or planner
arguments. DEC-025 also used `Approved`, which is not a recognized decision status in this
repository. Append-only history prevents rewriting that record.

### Decision
1. Treat this accepted record as the status correction and enforcement clarification for
   DEC-025; preserve DEC-025 as historical context.
2. Order selection first by the lowest qualifying ladder level, then by cost among
   capabilities at that level.
3. Compute effective scope from trusted authority intersected with caller preference;
   callers and delegated contexts may never widen authority.
4. Treat human clarification and handoff as available from every scope outside automated
   capability ceilings.
5. Preserve origin level and model-derived taint through caching, planning, composition,
   deterministic wrappers, and final output.
6. Require exact digest- and scope-bound review evidence for every user-visible model
   derivative.
7. Apply the complete scope gates and tests in SPEC-013 before enabling generation.

### Consequences
**Positive:**
- Cost optimization cannot skip a simpler capability that already satisfies quality.
- Child and deterministic policies cannot be bypassed through request metadata or caches.
- Planner-generated arguments cannot launder model text through deterministic procedures.
- Human-first handoff remains universally reachable.

**Negative:**
- Cache metadata and review evidence require richer provenance and policy state.
- Existing future generative designs must implement derivation taint before activation.

### Related Decisions
- DEC-024 (Remove Direct Generative Answers from the Active AF Runtime)
- DEC-025 (Adopt the Capability Ladder and Scoped Reviewed Generation)

### Related Specs
- SPEC-012 (Capability Ladder and Scoped Generative Execution)
- SPEC-013 (Acceptance Authority, Derivation Taint, and Ladder Selection)

### Output References
- Updated: `docs/capability-ladder.md`
- Appended: `specs.md`, `decisions.md`

---

## [DEC-027] Require Pre-Execution Authorization for Planner Side Effects
**Date**: 2026-07-21
**Status**: Accepted
**Decision Maker**: Alexander Holman / Zordon policy review

### Context
Review of scoped generation identified that answer review occurs too late to protect against
an irreversible action already executed by a model-generated plan. The current selector
correctly denies side-effecting, risky, and confirmation-requiring capabilities; future
planner support must not weaken that boundary.

### Decision
1. Keep planner execution pure, read-only, and no-risk by default.
2. Require independent authorization bound to the exact immutable plan and predicted effect
   before any side-effecting or elevated-risk node executes.
3. Invalidate authorization on every relevant plan or policy change.
4. Require adaptive planners to pause and obtain fresh authorization before changed effects.
5. Keep current runtime denials until SPEC-014 is implemented and tested.

### Consequences
**Positive:**
- Final-answer review cannot be used to justify an earlier unreviewed side effect.
- Planner adaptation cannot expand authorized effects silently.
- Current fail-closed selector behavior remains the minimum safety baseline.

**Negative:**
- Side-effecting agent workflows require additional authorization state and pauses.
- Some adaptive workflows cannot run unattended.

### Related Decisions
- DEC-024 (Remove Direct Generative Answers from the Active AF Runtime)
- DEC-025 (Adopt the Capability Ladder and Scoped Reviewed Generation)
- DEC-026 (Accept and Clarify Scoped Capability Selection)

### Related Specs
- SPEC-013 (Acceptance Authority, Derivation Taint, and Ladder Selection)
- SPEC-014 (Planner Side-Effect Authorization)

### Output References
- Updated: `docs/capability-ladder.md`
- Appended: `specs.md`, `decisions.md`

---

## [DEC-028] Adopt an Async-Capable Application and Provider Lifecycle
**Date**: 2026-07-22
**Status**: Accepted
**Decision Maker**: Alexander Holman / Zordon architecture review

### Context
AF is expanding from one synchronous vertical slice into a platform with web/API/CLI/TUI
kernels, workers, schedulers, messaging, communications, and local/remote data drivers.
Implicit event-loop bridges, process-global request state, and undefined provider teardown
would create deadlocks, leaks, and cross-request authority failures.

### Decision
1. Use `construct → register all → boot all → serve → drain → shutdown` lifecycle ordering.
2. Keep registration synchronous and free of external I/O; permit bounded asynchronous boot
   and shutdown.
3. Make I/O and long-running contracts async-first while preserving deterministic sync
   functions and the existing AF pipeline through explicit adapters.
4. Permit a sync-to-async bridge only at an owning process entry point; libraries do not
   create hidden or nested event loops.
5. Propagate cancellation and shutdown rather than converting them to ordinary failures.
6. Distinguish singleton, application, typed invocation, and transient scopes; dispose owned
   resources once in reverse creation order.
7. Store invocation context in immutable context-local state and prohibit request/message
   state in long-lived services.
8. Topologically validate provider dependencies and roll back partial boot in reverse order.

### Alternatives Considered
- **Synchronous-only core:** rejected because remote stores, brokers, and concurrent kernels
  would block or require hidden behavior.
- **Async-only rewrite:** rejected because deterministic CPU-only functions gain no safety.
- **Implicit bridges:** rejected because nested loops and cancellation loss are unsafe.

### Consequences
- Every kernel shares one application runtime and ownership model.
- Long-lived workers cannot retain principal, tenant, transaction, or trace state by design.
- Sync/async behavior, cancellation, ownership, and cleanup are testable contracts.
- Existing deterministic procedures remain valid.
- Providers/resources require lifecycle metadata and partial startup/shutdown rollback logic.
- Sync adapters and async drivers cannot be mixed through convenience magic.

### Implementation Notes
- Registration is declaration-only and resolution-disabled until the provider graph is
  validated and frozen.
- Boot follows topological dependency order and transactionally records resources as acquired.
- Rollback cleans the currently failing provider before earlier providers.
- Invocation-created tasks are joined/cancelled; detached work receives a new context and no
  scoped dependencies.
- Cleanup is bounded and protected from cancellation, then the original cancellation is
  re-raised after complete reverse-order unwinding.
- Contract metadata and tests identify every sync/async operation; only process entry points
  own bridges and active event loops are never blocked by hidden synchronous I/O.

### Related Decisions
- DEC-022 (Adopt the AF Python Vertical Slice and MCP-First ChatGPT Boundary)
- DEC-024 (Remove Direct Generative Answers from the Active AF Runtime)
- DEC-028 is complemented by DEC-029 (providers, drivers, and delivery semantics)

### Related Specs
- SPEC-009 (AF Executable Python Framework and ChatGPT MCP Adapter)
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)

### Output References
- Added: `docs/application-platform.md`
- Tracks: GitHub issue #7

---

## [DEC-029] Use Providers, Managers, Named Drivers, and Honest Delivery Semantics
**Date**: 2026-07-22
**Status**: Accepted
**Decision Maker**: Alexander Holman / Zordon architecture review

### Context
AF must support selectable databases, search engines, caches, queues, pub-sub systems,
graph/document stores, filesystems, and communications. Treating one engine as mandatory
would constrain deployments; pretending all engines are interchangeable would silently
weaken transactions, ordering, durability, search, or policy enforcement.

### Decision
1. Use constructor/factory injection and ordered service providers as canonical composition.
2. Resolve replaceable subsystems through `Manager → named connection → Driver` contracts.
3. Verify capabilities such as transactions, locks, delay, replay, aliases, vectors, and
   signed URLs instead of branching on vendor names.
4. Define specialized relational, document, graph, search/vector, cache, blob, event, and
   time-series contracts rather than a false universal query language.
5. Make native extensions explicit, capability-gated, observable, and policy-controlled.
6. Keep transaction claims local to one supporting connection; use outbox/inbox, idempotent
   projectors, or explicit sagas across stores.
7. Treat queues and durable consumers as at-least-once by default and never claim exactly-once
   delivery; exactly-once business effects require a declared idempotency boundary.
8. Keep jobs, events, pub-sub, broadcasts, and notifications as separate contracts.
9. Prohibit silent production fallback; explicit failover/fallback retains reason,
   capability, and degradation evidence.
10. Require package compatibility metadata and consumer control over discovery.

### Alternatives Considered
- **One engine per subsystem:** rejected because AF must fit local and distributed profiles.
- **Lowest-common-denominator API:** rejected because it erases important semantics.
- **Automatic fallback:** rejected because it can change privacy, durability, and correctness.

### Consequences
- PostgreSQL/MySQL/MariaDB/SQLite, OpenSearch/Elasticsearch, Redis/Memcached,
  RabbitMQ/Redis/database/SQS queues, and future providers coexist behind stable contracts.
- Application services depend on capabilities rather than vendor SDKs.
- Driver differences remain honest, testable, and visible.
- Packages extend AF without editing trusted core.
- Every driver needs typed config, health, lifecycle, independently verified capability and
  conformance evidence, plus a compatibility matrix.
- Some workflows require backend-specific query objects or explicit native APIs.
- Cross-store consistency is explicitly eventual and operational.

### Implementation Notes
- Discover inert package metadata before importing provider code; require compatibility and
  policy approval before import or construction.
- Validate critical driver claims through trusted manifests, runtime probes, and conformance
  evidence rather than self-report alone.
- Bind approval/evidence to artifact, dependency graph, configuration, deployment profile,
  policy, expiry, and revocation; revalidate on change or staleness.
- Persist state plus outbox atomically in one authoritative transaction, then relay after
  commit to idempotent inbox-backed consumers.
- Atomically commit consumer business state plus inbox where possible; represent external
  effects as durable transactional intents before acknowledgement.
- Managers resolve named drivers only after typed configuration and capability validation.

### Related Decisions
- DEC-018 (Adopt Script-First "Only Write Once" Execution Policy)
- DEC-025 (Adopt the Capability Ladder and Scoped Reviewed Generation)
- DEC-028 (Adopt an Async-Capable Application and Provider Lifecycle)

### Related Specs
- SPEC-008 (Script-First Automation and Only Write Once)
- SPEC-013 (Acceptance Authority, Derivation Taint, and Ladder Selection)
- SPEC-015 (Application Platform Lifecycle and Extension Contracts)

### Output References
- Added: `docs/application-platform.md`
- Tracks: GitHub epic #35 and issue #7

---
