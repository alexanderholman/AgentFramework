# AF testing and conformance contracts

AF testing helpers are first-class extension APIs. They prove bounded application and contract
behaviour without turning a fake, fixture, or local conformance run into evidence that a production
backend is safe, durable, available, or correctly deployed.

## Isolated application hosts

`TestApplicationBuilder` creates a fresh application, container, handler registry, persistence
configuration, providers, and overrides. Tests own the returned lifecycle and must shut it down.
`assert_test_application_clean()` verifies both the public lifecycle outcome and count-only internal
evidence that scopes, resources, tasks, construction flights, and pending cleanup work were released.
It never includes service tokens, resource values, configuration, or failure details in its report.

Multiple applications may run concurrently in one process. Application-scoped services, handlers,
persistence, manager connections, and fake journals must remain independent.

## Process-state isolation

`ProcessStateGuard` snapshots a deliberately bounded set of process-global surfaces before a test
case and restores them afterward:

- current working directory;
- environment variables;
- Python warning filters, default action, and once registry; and
- newly live threads.

A changed surface fails the case as `ProcessStateLeak`. Reports contain only stable surface names;
paths, environment values, warning contents, and thread names are redacted. A live thread cannot be
forcibly and safely stopped by the guard, so the extension remains responsible for joining it.
Extension-owned module globals and external resources remain the extension's responsibility and
should be covered by explicit conformance cases.

Provider and driver conformance suites run every case inside this guard. A case therefore cannot
pass merely because its return value is correct while it mutates shared process state.

## Deterministic fakes

The standard fake catalogue records bounded, snapshotted calls for events, queues, cache, locks,
files, search, mail, notifications, and transports. It also provides a controllable clock and a
finite deterministic ID source. Fake assertions cover payload, destination, attempt, count,
absence, subsystem order, and cross-subsystem order.

These records prove only application and test behaviour. Every fake and every conformance report
has `certifies_production = False`. They provide no evidence of real timing, durability, isolation,
delivery, backend capability, external I/O, security posture, or deployed configuration.

## Provider conformance

`ProviderConformanceSuite` accepts bounded `ConformanceCase` values. For every case it:

1. constructs a fresh provider and `TestApplicationBuilder`;
2. boots one isolated application through the normal provider lifecycle;
3. executes the synchronous case;
4. shuts the application down even when the case fails;
5. verifies lifecycle, scope, resource, task, and process-state cleanup.

An external-style fixture package should expose its provider factory and suite so the same contracts
can run in the framework repository and in the extension repository.

## Driver conformance

`DriverConformanceSuite` opens a fresh context-managed driver fixture for every case. Fixture
cleanup runs on success and failure, and the surrounding process-state guard rejects shared-state
leakage. Production drivers should additionally be exercised through their normal manager/provider
registration and the independently governed real-service certification matrix.

## Real-service hooks

`RealServiceHook` is an explicit opt-in wrapper around a caller-owned context-managed integration.
Disabled hooks perform no I/O. Enabled hooks still produce only `REAL_SERVICE` conformance evidence;
`ConformanceReport.certifies_production` remains `False`.

Production support requires separately governed, version- and profile-bound compatibility,
security, resilience, performance, and release evidence. Fakes and conformance fixtures are
necessary test tools, not deployment authority.
