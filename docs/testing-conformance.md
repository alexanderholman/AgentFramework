# AF Testing and Conformance Contracts

AF testing helpers are first-class extension APIs. They prove deterministic application behavior
without turning unit fakes or a local contract suite into evidence that a production backend is
safe, durable, available, or correctly deployed.

## Isolated application hosts

`TestApplicationBuilder` boots a fresh application, container, handler registry, persistence
configuration, providers, and overrides for each test. A test owns that application lifecycle and
must shut it down; `assert_clean()` rejects incomplete shutdown or recorded cleanup failures.
Multiple applications may run in one process, including concurrently, without sharing
application-scoped services, handlers, managers, drivers, or mutable fake journals.

## Deterministic fakes

The standard fake catalogue records bounded, snapshotted calls for events, queues, cache, locks,
files, search, mail, notifications, and transports. It also provides a controllable clock and a
finite deterministic ID source. Fake assertions cover payload, destination, attempt, count,
absence, subsystem order, and cross-subsystem order.

These records prove only application and test behavior. Every fake reports
`certifies_production = False` and provides no evidence of real timing, durability, isolation,
delivery, backend capability, or external I/O. A production driver must still pass its real
service, failure, security, compatibility, resilience, and release-profile evidence gates.

## Provider conformance

A `ProviderConformanceFixture` supplies a fresh provider factory and one or more `ProviderProbe`
values. `run_provider_conformance()`:

- constructs the provider twice rather than accepting a reusable provider instance;
- boots two independent applications concurrently;
- verifies stable provider identity and dependency declarations;
- resolves every declared probe through the application container;
- rejects shared application-scoped values unless the probe explicitly declares a safe immutable
  shared value;
- compares snapshotted probe observations for deterministic parity; and
- requires both application lifecycles to shut down without cleanup failures.

A provider fixture should probe every service contract that the extension expects consumers to
resolve. A probe exercise must be synchronous, bounded, side-effect controlled, and safe to run in
two applications at once.

## Driver conformance

A `DriverConformanceFixture` supplies a fresh `DriverRegistration`, one manager/connection
configuration, required capabilities, an optional deterministic exercise, and optional real-service
hooks. `run_driver_conformance()` installs the registration through normal provider registration
and resolves it through the real AF `Manager` contract. It proves:

- independent manager and driver identity across two concurrent applications;
- deterministic named-connection caching within each application;
- exact capability-evidence binding to driver, configuration, profile, and policy identity;
- required-capability enforcement through the manager;
- deterministic exercise observations; and
- one disposal operation for each resolved driver during application shutdown.

The harness does not bypass manager validation, inject a driver directly, or accept a driver's own
capability claim without the normal independent verifier and evidence value.

## Real-service hooks

`RealServiceHook` values are explicit opt-in integration checks. The default driver conformance run
records each hook as skipped and performs no external I/O. Hooks run only when the caller sets
`include_real_services=True` and has deliberately prepared the required service environment.
A hook is responsible for its own bounded setup, assertions, cleanup, and environment eligibility.
It must not silently fall back to a fake or weaker backend.

`ConformanceReport.certifies_production` is always `False`, including when opt-in hooks pass.
Production support requires independently governed, version/profile-bound evidence and the
cross-driver release certification work. Fakes and conformance fixtures are necessary test tools,
not deployment authority.
