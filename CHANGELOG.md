<!-- Written by Alexander Holman 19/08/2019 -->
<!--
This file contains a list of all changes made to a system since it's introduction. It is written to be used with most 
internal projects, but as per the other documents feel free to adapt it to fit your needs, adding, modifying and 
removing as necessary.
In the event that it is added to an existing repository, (unless a full desscription is feasable) simply start with the 
current version.
-->

# Changelog
A rough overview of changes to over time.

## Unreleased — `0.1.0a0` — 2026-09-26

The package remains alpha. These implemented capabilities do not declare roadmap closure,
external-service certification, accessibility certification, or a production release.

* [Added] Explicit providers, scoped application lifetimes, typed drivers and conformance contracts.
* [Added] Durable workflows, scheduling, local machine initialization, SQLite lifecycle and streams.
* [Added] Finite verified science, algorithms/problems, controlled Human Interpreter and human decisions.
* [Added] Six application-owned starter kits and typed terminal/headless applications.
* [Added] Headless accounts, MFA, passkeys, tokens and optional identity-provider adapters.
* [Added] Consent-aware notifications, SMTP submission, document/object/SFTP adapters and support matrices.
* [Added] Bounded content/runtime/peer foundations and explicit local capability engineering.
* [Added] Optional inspector/metrics/admin products and locale-aware HTML/REST presentation.
* [Fixed] Local REST/GraphQL explorers work under the same-origin CSP without CDN assets.
* [Fixed] Malformed CSRF/session signatures and Origin values fail without exposing server errors.
* [Improved] Runtime-only package smoke, security tooling, provenance and qualification documentation.

## [v0.1.0] 2026-07-18
Initial AgentFramework Python vertical slice.
* [Added] - Typed AF kernel implementing classify, route, execute, verify, record, test, and reuse
* [Added] - Deterministic Hello World and parameterized character-count procedures
* [Added] - Shared Task CRUD over CLI, HTML MVC, REST, GraphQL, and MCP
* [Added] - SQLite and in-memory persistence adapters
* [Added] - Repository skill, Copilot instructions, CI, and ChatGPT MCP guidance
* [Imported] - AgentFactory definitions and append-only governance history as the foundation

## Imported AgentFactory history

## [v1.1.0] 2026-01-28
GitHub Copilot Agent Readiness Release
* [Added] - `.github/copilot-instructions.md` - Comprehensive workspace instructions for GitHub Copilot
* [Added] - `COPILOT_INTEGRATION.md` - Complete guide for using AgentFactory with GitHub Copilot
* [Added] - `.github/workflows/validate-agents.yml` - Automated validation workflow for CI/CD
* [Fixed] - Updated all 7 core agent files (Architect, Builder, Skeptic, Editor, ProjectManager, CitationOfficer, ChatGPT) to include required heading structure
* [Fixed] - All agents now have proper Purpose, Inputs, Outputs, Behavior, and Constraints sections in correct order
* [Updated] - README.md with GitHub Copilot integration information and validation details
* [Validated] - All 22 validation tests now pass successfully
* [Enhanced] - Repository is now fully ready for GitHub Copilot Workspace integration

## [v1.0.0](https://github.com/alexanderholman/AgentFactory/releases/tag/v1.0.0) 2026-01-28
Initial release of AgentFactory - a framework for defining, documenting, and managing AI agents.
* [Added] - Core agent definitions (Architect, Builder, Skeptic, Editor, ProjectManager, CitationOfficer, ChatGPT)
* [Added] - Specialism definitions (Researcher, Coder, CitationManager)
* [Added] - Agent documentation framework with standardized headings and YAML frontmatter
* [Added] - Validation tools (validate_agents.sh) for MUST requirements
* [Added] - Append-only logs for specs, runs, and decisions
* [Added] - Project structure and configuration files (agents.yaml, repo.yaml)
* [Added] - Initial project documentation templates
* [Added] - Issue templates for bugs, tasks, features, questions, and brainstorming
* [Added] - Contributing guidelines and code of conduct
* [Added] - Support documentation and installation instructions
