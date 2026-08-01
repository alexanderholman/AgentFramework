# AF Testing and Conformance

AF provides first-class helpers for isolated application tests, deterministic subsystem fakes,
and reusable extension conformance suites. These APIs prove bounded test behavior. They do not
certify a driver, provider, backend, service, or deployment for production use.

## Isolated application tests

`TestApplicationBuilder` creates a fresh application, container, handler registry, and persistence
configuration for each test. Providers and binding overrides are applied before boot. The returned
`TestApplication` owns its lifecycle and should be shut down by the test; `assert_clean()` rejects
incomplete shutdown or recorded cleanup failures.

Multiple applications may run in one process. Application-scoped services, invocation state,
handlers, repositories, and owned resources must not leak between them.

### Leak evidence

`assert_test_application_clean()` extends the public lifecycle assertion with count-only evidence
that lifecycle-owned scopes, resource registries, tasks, construction and resolution flights, and
pending cleanup work were released. Its report never includes service tokens, resource values,
configuration, or failure details.

`ProcessStateGuard` snapshots and restores a bounded set of process-global surfaces around a test
case: the working directory, environment, Python warning policy, and newly live threads. Mutations
fail as `ProcessStateLeak`; diagnostics contain only stable surface names and never disclose paths,
environment values, warning contents, or thread names. A live thread cannot be safely stopped by the
guard, so extensions remain responsible for joining it. Extension-owned module globals and external
resources remain the extension's responsibility and require explicit conformance cases.

Provider and driver conformance suites apply both application and process leak checks to every case.

## Deterministic subsystem fakes

The standard fake catalogue includes controllable clock and identifier sources plus recording fakes
for events, queues, cache, locks, files, search, mail, notifications, and transports.

Recording fakes snapshot bounded inputs and support assertions for:

- payload and destination;
- attempt number and exact count;
- absence of a matching dispatch;
- order within one subsystem; and
- order across the shared fake subsystem journal.

Fake records establish only what the test double observed. They do not prove real delivery,
durability, isolation, atomicity, timing, availability, security, capability, or external I/O.
Every fake therefore reports `certifies_production = False`.

## Reusable conformance cases

A `ConformanceCase[T]` names one synchronous contract check over a fixture of type `T`. Cases are
bounded and uniquely named. A case must execute its assertions before returning: awaitables,
generators, and async generators are rejected so a suite cannot report success for work that was
never actually evaluated.

```python
from af.testing import ConformanceCase


def reads_value(driver: ExampleDriver) -> None:
    assert driver.read("key") == "value"


case = ConformanceCase("reads-value", reads_value)
```

Callbacks and fixture metadata are redacted from representations so diagnostics do not expose
connection details, credentials, or implementation objects.

## Provider conformance

`ProviderConformanceSuite` runs every case in a newly constructed `TestApplication`. The caller
supplies factories for both the provider and builder; instances are never reused across cases.
Each application is shut down and checked for clean resource disposal even when a case fails.

```python
from af.testing import (
    ConformanceCase,
    ProviderConformanceSuite,
    TestApplication,
    TestApplicationBuilder,
)


def resolves_service(app: TestApplication) -> None:
    assert isinstance(app.resolve(ExampleService), ExampleService)


suite = ProviderConformanceSuite(
    "example-provider",
    (ConformanceCase("resolves-service", resolves_service),),
)

report = suite.run(
    ExampleProvider,
    builder_factory=TestApplicationBuilder,
)
```

The report uses `ConformanceEvidence.ISOLATED`. It proves that the declared checks passed in fresh
AF test applications, not that an installed package or deployment has production approval.

## Driver conformance

`DriverConformanceSuite[T]` runs each case against a fresh context-managed driver fixture. A
fixture must acquire and release all resources inside its context manager. The default `run()`
path produces `ConformanceEvidence.FAKE` and is intended for fast deterministic contract tests.

```python
from contextlib import contextmanager

from af.testing import ConformanceCase, DriverConformanceSuite


@contextmanager
def fake_driver():
    driver = ExampleDriver.in_memory()
    try:
        yield driver
    finally:
        driver.close()


def writes_and_reads(driver: ExampleDriver) -> None:
    driver.write("key", "value")
    assert driver.read("key") == "value"


def starts_empty(driver: ExampleDriver) -> None:
    assert driver.read("missing") is None


suite = DriverConformanceSuite(
    "example-driver",
    (
        ConformanceCase("writes-and-reads", writes_and_reads),
        ConformanceCase("starts-empty", starts_empty),
    ),
)

report = suite.run(fake_driver)
```

Each case receives a fresh fixture. Cleanup runs when a check passes or raises, preventing one case
from contaminating another through retained driver state.

## Explicit real-service hooks

A `RealServiceHook[T]` is a caller-owned, explicit opt-in connection to an actual service fixture.
Disabled hooks require a bounded reason and raise `IntegrationUnavailable`; they never silently
fall back to a fake or weaker backend. Enabled hooks must return a context manager that performs
bounded setup and cleanup.

```python
from af.testing import RealServiceHook

hook = RealServiceHook(
    "postgres-local",
    postgres_fixture,
    enabled=postgres_is_available,
    disabled_reason=None if postgres_is_available else "local service is not configured",
)

real_report = suite.run_real(hook)
```

A successful real-service run produces `ConformanceEvidence.REAL_SERVICE` bound to the named
integration. Its `ConformanceReport.certifies_production` property remains `False`.

Production support additionally requires governed version and capability declarations, independent
runtime evidence, security and failure testing, supported-service matrices, release-profile
binding, and deployment approval. Unit fakes and reusable conformance suites are necessary evidence
inputs, never deployment authority.

## Governed benchmark plugins

`af.testing.benchmarks` defines the first bounded benchmark SPI for executable plugins and governed
fixture eligibility. It is intentionally narrower than a full benchmark catalogue or evidence store:
the goal is to make unsupported capability, policy denial, attempted failure, cancellation, and
inconclusive verification distinguishable before broader routing and certification work lands.

The runner is deterministic and local:

- plugin discovery is an explicit in-process registry, not ambient package scanning;
- compatibility is checked before setup so missing or incompatible plugins return
  `BenchmarkOutcome.UNSUPPORTED` without attempted workload execution;
- fixture manifests are fail-closed: missing licence, provenance, consent/legal basis, access tier,
  intended use, intended audience, approval class, reviewer authority, retention, deletion, or
  export policy make the fixture ineligible before setup;
- authorization is checked both before setup and again before evidence acceptance so expiry,
  revocation, or scope narrowing cannot silently pass on stale authority;
- teardown runs after success, failure, timeout, cancellation, crash, and verifier failure.

Each `BenchmarkRunResult` binds evidence to the plugin, procedure, runtime, adapter, verifier,
fixture manifest/content digests, environment, hardware class, precision, configuration, threshold,
effective scope, and authorization digest. Changing any of those inputs changes the evidence digest.

The exported view is deliberately minimized. It names only the immutable governed references,
permitted audiences/uses, score, metrics, and outcome metadata. Raw governed content and detailed
provenance fields stay out of exports, telemetry, caches, and general evidence summaries.

Like the rest of `af.testing`, this API does not certify production isolation, deletion, domain
governance, or physical no-egress behavior by itself. Fakes and local contract tests are necessary
evidence inputs, never sufficient authority.
