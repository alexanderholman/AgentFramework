# AgentFramework v1 — Lovelace Multi-Node Runtime

**Status:** Accepted product direction; implementation and release evidence in progress

**Primary use:** Choice and Charlotte's private, Tailscale-connected agent network

## Product outcome

AgentFramework v1 is the installable runtime behind a mobile-first client and a web client.
Lovelace is the single access point and is also a compute node. It routes work to agents on
Lovelace, Alien, and Bender. Charlotte's macOS agent on an M1 MacBook Pro is a later node using
the same host contract. The application and clients use AF's shared application boundary; they
do not implement separate agent routing or execution rules.

The v1 runtime accepts a user request, performs useful work across local capabilities and
trusted agents, returns a verified result and artifacts, and keeps enough progress evidence to
resume, notify, and reuse successful work.

## Product decisions

1. **One access point, several nodes.** Clients connect to Lovelace. Lovelace can execute work
   itself and route bounded work to connected nodes. A node's advertised abilities are not
   treated as live until its installed capability, health, and available budget are checked.
2. **Use the least costly capable route.** Check verified cache and reusable artifacts first,
   then deterministic functions and programs, then specialist capabilities and agents, then
   models ordered by measured projected cost. Select only a route that meets the request's
   stated quality, modality, locality, and output requirements. Unknown cost stays unknown.
3. **Minimize unnecessary inference.** Prefer code, templates, deterministic transformations,
   and cache hits where they satisfy the request. Track model and token use where a provider
   reports it. Do not spend model tokens to repeat a result already verified and reusable.
4. **Do real requested work.** When the user's task requires a program, template, analysis, or
   host operation, an agent may create and execute it through the node's host executor, including
   local files, shell commands, installed tools, and runtimes. The task runs as the configured AF
   service identity and is bounded by the active budget. Do not add a per-command approval step
   to work the user has requested. Report partial work, failures, and exhausted budgets truthfully.
5. **Reuse successful work.** Store reusable programs, templates, tests, and verified results
   with version, inputs, provenance, and invalidation information. Keep personal or otherwise
   ineligible content out of shared reuse.
6. **Collaborate through typed work.** A multi-agent task is represented as a bounded workflow
   of typed steps with declared inputs, outputs, dependencies, capability needs, and budget
   reservations. Agents may contribute specialist knowledge (for example OpenCV) while AF
   coordinates and verifies the combined result.
7. **Make self-improvement executable.** Work on AF itself can produce code, tests, procedures,
   and templates as ordinary work artifacts. AF should execute relevant tests, retain evidence,
   and make successful work available for later reuse. A generated change is not evidence that
   the change works; it must pass its declared checks before being reported as verified.
8. **Wait visibly for slow work.** Cold model startup, old HDD reads, node wake-up, and long
   tasks are normal operating conditions. Jobs must support configurable deadlines including
   waits of at least 600 seconds, cancellation, durable stage/progress events, reconnection, and
   completion/failure notifications for subscribed clients.
9. **Use one AF kernel.** CLI, local HTTP, mobile/web clients, agent nodes, and later provider
   integrations delegate to the same typed application services, policy, provenance, and
   lifecycle contracts.

## Runtime request lifecycle

```text
receive request
  -> bind caller, node, task and budget context
  -> normalize and classify deterministically
  -> reuse eligible verified result or execute a matching deterministic capability
  -> inspect remaining capable routes and measured projected cost
  -> route to the lowest-cost available local agent/model or trusted node
  -> execute one bounded workflow, recording progress and actual resource use
  -> verify outputs and artifacts against the requested result
  -> persist result, trace, budget use and reusable candidates
  -> notify connected clients and report the outcome
```

Agents may write a new program or compose existing functions when that is the most efficient
way to complete a request. The host executor supplies the node's actual local machine
capabilities; AF does not require every task-specific program to have existed in a preloaded
catalog. Every dispatch carries the task identity and remaining budget. Model calls, file
changes, subprocesses, node requests, and emitted artifacts are attributable to the task and
agent that caused them, and the result is registered for reuse only after its declared checks
pass.

## Budgets and cost evidence

Budgets are configured by the operator for the relevant users, nodes, providers, and requests.
AF must not invent a universal budget or treat a missing measurement as zero. The runtime must
reserve scarce resources before dispatch where the driver supports reservation, account for
actual use where available, and stop or settle work when a hard limit is reached.

The accounting contract must support at least elapsed time/deadline, input and output tokens
when reported, provider money, CPU time, memory, accelerator time, storage, network transfer,
and concurrent work. Cost and feasibility projections must include cold start, warm residency,
model swap, verification, fallback, and node availability when evidence exists. Unknown values
remain explicitly unknown. Work must not silently switch to a remote provider or another node.
Host execution is performed by the configured operating-system service identity; its filesystem
and command access are part of the operator-configured node budget and are not narrowed by an
additional AF per-command approval policy.

## Long-running work and notifications

Long work is a durable job, not a synchronous mobile request held open until a model wakes.
The runtime must provide stable job/task identifiers, ordered state transitions, heartbeat or
meaningful progress events, bounded retry semantics, cancellation, result retrieval, and
reconnect-safe notification delivery. Client notification content is a projection over
persisted AF events; the transport must not become a second source of job state.

At minimum, clients can distinguish queued, waiting for a node, loading a model, running,
verifying, completed, failed, cancelled, and budget-exhausted work. Progress reports must not
claim completion based only on a running process or a model response.

## Node and capability contract

Each node runs an installable AF package and reports a signed or otherwise authenticated node
identity, AF/runtime version, available capability IDs and versions, health, supported media,
and current resource availability. A node performs only work routed to it by the configured
access point or explicitly submitted to that node. Capability descriptions distinguish
declared, installed, runtime-ready, measured, deployed, unavailable, degraded, and revoked
states.

The first operational topology is:

| Role | Host | Initial agent |
| --- | --- | --- |
| Access point and compute node | Lovelace (Windows 11 Pro with WSL2 Linux) | Alpha |
| Compute node | Alien | Zordon |
| Compute node | Bender | Billy |
| Later compute node | Charlotte's M1 MacBook Pro (macOS) | To be named/configured |

The local llama.cpp/OpenAI-compatible endpoint on Lovelace is an optional local capability.
AF stores model aliases, runtime configuration, file locations, artifact identities, measured
resources, and lifecycle evidence in its catalog. Model weights remain installed on the host
or in its configured artifact storage; they are not packaged in the Python wheel or GitHub
source repository.

## Delivery boundary

This document defines AgentFramework's runtime contract, not the mobile interface or the
`project_cowabunga` repository layout. AF v1 must expose reusable application services and
stable typed interfaces that those projects can consume. Work proceeds in this order:

1. Establish the v1 contracts and package/runtime baseline in AgentFramework.
2. Implement host agents, capability registration, node routing, provider adapters, cost and
   budget accounting, durable long jobs, progress, and result delivery in the AF kernel.
3. Demonstrate local execution and trusted-node routing with deterministic fakes, then qualify
   llama.cpp and the actual Lovelace/Alien/Bender deployments.
4. Build the mobile-first client and authenticated Lovelace access service on AF's stable
   application boundary.

The `project_cowabunga` repository is the intended home for the client and its Lovelace access
service unless a later architecture decision places them elsewhere.

## V1 release acceptance

- A clean Python 3.12+ install on supported Linux hosts can start and stop AF as a supervised
  non-root service, persist state outside the package directory, and pass `af doctor`.
- Lovelace is a single client entry point and can execute a deterministic/local route or route
  a typed task to an authenticated Alien or Bender node; a disconnected or stale node is never
  reported as available.
- A deterministic/cache route makes zero model calls. When several model routes can satisfy a
  request, AF chooses the lowest measured projected cost within the configured budget and
  explains its choice.
- The local model adapter can connect to the configured llama.cpp endpoint, distinguish
  warm/cold readiness, account for provider-reported use, and fail without remote fallback.
- Provider adapters can connect ChatGPT, GitHub Copilot, and Gemini agents where each service
  offers a supported programmatic agent interface; unavailable interfaces remain unavailable
  rather than being imitated through an unapproved hidden route.
- A task can run for at least ten minutes, expose durable progress and terminal state, survive
  client disconnect/reconnect, and deliver completion, failure, cancellation, and budget
  notifications without holding one HTTP request open.
- Agent output can include files, images, audio, and video-derived artifacts through typed
  contracts; limits are capability-specific and budgeted.
- A multi-step OpenCV-style workflow can invoke registered functions and specialist agents,
  verify each stage, and return artifact provenance and the final result.
- A framework-development run can create a program/test/template artifact, execute its
  declared checks, persist the evidence, and make a passing reusable capability discoverable.
- Every enabled interface uses shared AF application semantics and emits correlated, redacted
  trace/progress evidence. Authentication and authorization are tested on the actual Tailscale
  network boundary before clients can access host capabilities.
- Clean-host install, migration, backup/restore, package provenance, security, accessibility,
  failure recovery, and supported-platform evidence are recorded against the release commit.

This checklist is not a declaration that any item is complete. Release status changes only
when the corresponding implementation and exact-version evidence exist.
