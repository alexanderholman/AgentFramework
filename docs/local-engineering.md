# Bounded local capability engineering

The explicit `engineer` command implements a real, finite missing-component path
for `text.nonblank-line-total`. It counts nonblank LF-separated Unicode lines in
one to 128 input strings. A blank line contains only Unicode whitespace. It reuses
the installed `number.sum-counts` component and creates only the missing batch
line counter from a bundled source template. It does not accept arbitrary source,
model-generated programs, shell commands, repository paths, or routing authority
from request JSON.

Configure the local Git repository through `AF_ENGINEERING_REPOSITORY`. The
repository must have an existing commit, attached branch and configured origin.
AF uses the existing local coordination repository and a separate worktree. No
remote fetch, push, merge into the user's branch, or public publication occurs.
The engineering environment needs the `dev` dependencies for actual Ruff, mypy
and coverage gates. A missing gate tool rejects the candidate.

```sh
export AF_ENGINEERING_REPOSITORY=/absolute/path/to/repository
# request.json contains {"texts":["one\n\ntwo","three"]}
af engineer --input request.json
af engineering status
af ask 'count nonblank lines:one

two'
af engineering invalidate --reason 'Evidence requiring route rollback'
```

The factory `create_local_engineering_service` takes the configured database,
repository, authentication key and a trusted `EngineeringRouteProbe`. Bootstrap
supplies `InstalledEngineeringRouteProbe` over the actual classifier and procedure
registry. Its callbacks are lazy so the admitted contribution can depend on the
same engineering service. Caller-supplied `lower_status` and route history are
rejected. An existing route, missing installed implementation, cancelled request,
or configured selector whose exhaustion has not been established prevents
engineering. The built-in probe currently proves exhaustion of the deterministic
installed catalogue; a configured model selector needs its own trusted exhaustion
adapter and otherwise fails closed.

The first request records an explicit typed composition gap, specification,
requirements, non-goals, effects, provenance and rollback plan. It writes tests
before source and records their expected missing-implementation failure. The
isolated worktree then runs tests, branch coverage of at least 90%, formatting,
lint, strict typing, security checks, compatibility checks, repository-governance
checks and independent finite-template review. Candidate test execution is confined
to the engineering worktree. Production request execution starts after admission.
All subprocesses use bounded argument vectors, capture and deadlines; cancellation
terminates their process groups. Python tools run in isolated mode and Git runs
with a cleared environment, disabled external configuration, disabled hooks and
file-only protocol access.

One transaction admits the digest-bound component, precompiled two-node workflow
and exact classifier family. The manifest includes the original lower-route
history, Git heads, test evidence digests and review identity. Current catalogue
state and append-only history are HMAC authenticated; stale row-only rollback,
wrong keys and changed artifacts fail closed. Validation and execution use the
same admitted source digest; runtime copies the checked bytes into a private
invocation directory to prevent substitution between checking and execution.

`engineering_contribution(service)` provides an installed classifier, procedure
and independent verifier. Before admission its classifier returns no route. After
admission it recognizes exactly `count nonblank lines:` and pins the admission
digest in its typed arguments. Later requests execute the admitted workflow
without repeating composition, tests or engineering. The response separates
engineering and execution time and reports zero model calls. This is measured
local latency, not an energy or monetary-cost estimate.

Concurrent first requests coalesce behind one durable owner-fenced claim. A waiter
revalidates its cancellation/deadline and has a bounded wait. Interrupted or failed
engineering remains visible and requires explicit operator recovery; it is not
silently repeated. Invalidation atomically removes the route for new requests,
retaining historical evidence. Already accepted runs retain their immutable
admission snapshot. Admitted procedures bypass general result caching so a cache
hit cannot suppress the current catalogue check.

The feature is an executable finite contract for issue #55, not a general-purpose
software engineering agent or evidence of arbitrary code admission. Multiple
novel component families, model-selector exhaustion and human approval systems
need additional explicitly configured adapters. Local Git validation and tests
prove the bundled contract; they do not grant deployment or publication authority.
