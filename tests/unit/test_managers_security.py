from __future__ import annotations

import gc
import warnings
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from af.framework.errors import AFError
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    CapabilityValidationError,
    ConnectionRef,
    DriverManifest,
    HealthStatus,
    Manager,
    ManagerConfigurationError,
)

_NOW = datetime(2026, 7, 23, 20, tzinfo=UTC)
_ARTIFACT_DIGEST = "a" * 64
_DEPENDENCY_DIGEST = "b" * 64
_EVIDENCE_DIGEST = "c" * 64
_CAPABILITIES = CapabilitySet(("read",))


@dataclass(slots=True)
class _Driver:
    identity: int
    closed: bool = False


class _StreamingDriver(_Driver):
    def __aiter__(self) -> _StreamingDriver:
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration


def _manifest() -> DriverManifest:
    return DriverManifest(
        driver_name="remote",
        version="1.0.0",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )


def _evidence(binding: CapabilityBinding) -> CapabilityEvidence:
    return CapabilityEvidence(
        binding=binding,
        capabilities=_CAPABILITIES,
        health=HealthStatus.HEALTHY,
        observed_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
        evidence_digest=_EVIDENCE_DIGEST,
    )


def _manager(
    *,
    connections: Mapping[str, object] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Manager[_Driver]:
    return Manager(
        manager_id="database",
        connections=connections
        or {
            "primary": {"driver": "remote"},
            "analytics": {"driver": "remote"},
        },
        default="primary",
        deployment_profile="production",
        policy_version="policy-v1",
        clock=clock or (lambda: _NOW),
    )


def _extend(
    manager: Manager[_Driver],
    factory: Callable[[ConnectionRef, Mapping[str, object]], _Driver],
    *,
    verifier: Callable[
        [ConnectionRef, _Driver, CapabilityBinding],
        CapabilityEvidence,
    ]
    | None = None,
    disposer: Callable[[_Driver], object] | None = None,
) -> None:
    manager.extend(
        "remote",
        factory,
        manifest=_manifest(),
        verifier=verifier or (lambda _ref, _driver, binding: _evidence(binding)),
        disposer=disposer,
    )


def _assert_no_unawaited_warnings(captured: list[warnings.WarningMessage]) -> None:
    assert not any("was never awaited" in str(item.message) for item in captured)


def test_close_is_terminal_and_blocks_reentrant_driver_resurrection() -> None:
    manager = _manager()
    created: list[_Driver] = []
    disposed: list[int] = []
    reentrant_failures: list[Exception] = []

    def factory(_ref: ConnectionRef, _config: Mapping[str, object]) -> _Driver:
        driver = _Driver(len(created) + 1)
        created.append(driver)
        return driver

    def dispose(driver: _Driver) -> None:
        disposed.append(driver.identity)
        driver.closed = True
        if driver.identity == 2:
            try:
                manager.connection("analytics")
            except Exception as error:
                reentrant_failures.append(error)

    _extend(manager, factory, disposer=dispose)
    manager.connection("primary")
    manager.connection("analytics")

    manager.close()

    assert disposed == [2, 1]
    assert reentrant_failures
    assert manager.resolved_connections == ()
    with pytest.raises(AFError):
        manager.connection()
    with pytest.raises(AFError):
        manager.extend(
            "other",
            factory,
            manifest=DriverManifest(
                driver_name="other",
                version="1.0.0",
                artifact_digest=_ARTIFACT_DIGEST,
                dependency_graph_digest=_DEPENDENCY_DIGEST,
                capabilities=_CAPABILITIES,
            ),
            verifier=lambda _ref, _driver, binding: _evidence(binding),
        )


def test_shared_driver_identity_is_disposed_at_most_once() -> None:
    manager = _manager()
    shared = _Driver(1)
    disposed: list[int] = []
    _extend(
        manager,
        lambda _ref, _config: shared,
        disposer=lambda driver: disposed.append(driver.identity),
    )

    assert manager.connection("primary") is shared
    try:
        secondary = manager.connection("analytics")
    except AFError:
        secondary = None
    if secondary is not None:
        assert secondary is shared

    manager.close()

    assert disposed == [1]


def test_synchronous_factory_may_return_an_async_iterable_driver() -> None:
    manager = _manager(connections={"primary": {"driver": "remote"}})
    driver = _StreamingDriver(1)
    _extend(
        manager,
        lambda _ref, _config: driver,
        disposer=lambda current: setattr(current, "closed", True),
    )

    assert manager.connection() is driver
    manager.close()
    assert driver.closed


def test_failed_shared_resolution_does_not_dispose_a_live_cached_driver() -> None:
    manager = _manager()
    shared = _Driver(1)
    disposed: list[int] = []

    def verifier(
        ref: ConnectionRef,
        _driver: _Driver,
        binding: CapabilityBinding,
    ) -> CapabilityEvidence:
        if ref.connection_name == "analytics":
            raise RuntimeError("analytics probe failed")
        return _evidence(binding)

    def dispose(driver: _Driver) -> None:
        driver.closed = True
        disposed.append(driver.identity)

    _extend(
        manager,
        lambda _ref, _config: shared,
        verifier=verifier,
        disposer=dispose,
    )
    assert manager.connection("primary") is shared

    with pytest.raises(AFError):
        manager.connection("analytics")

    assert not shared.closed
    assert disposed == []
    assert manager.connection("primary") is shared
    manager.close()
    assert disposed == [1]


def test_reentrant_cached_verifier_cannot_return_a_disposed_stale_driver() -> None:
    manager = _manager(connections={"primary": {"driver": "remote"}})
    created: list[_Driver] = []
    verifier_calls = 0

    def factory(_ref: ConnectionRef, _config: Mapping[str, object]) -> _Driver:
        driver = _Driver(len(created) + 1)
        created.append(driver)
        return driver

    def verifier(
        _ref: ConnectionRef,
        _driver: _Driver,
        binding: CapabilityBinding,
    ) -> CapabilityEvidence:
        nonlocal verifier_calls
        verifier_calls += 1
        if verifier_calls == 2:
            manager.purge("primary")
        return _evidence(binding)

    def dispose(driver: _Driver) -> None:
        driver.closed = True

    _extend(manager, factory, verifier=verifier, disposer=dispose)
    stale = manager.connection()

    try:
        resolved = manager.connection()
    except AFError:
        resolved = None

    if resolved is not None:
        assert resolved is not stale
        assert not resolved.closed
        assert manager.resolved_connections == ("primary",)
    assert stale.closed


@pytest.mark.parametrize("wrapped", [False, True], ids=("async-function", "awaitable-result"))
def test_async_factory_result_is_rejected_without_coroutine_leak(wrapped: bool) -> None:
    manager = _manager(connections={"primary": {"driver": "remote"}})
    error: BaseException | None = None
    result: object | None = None

    async def create(_ref: ConnectionRef, _config: Mapping[str, object]) -> _Driver:
        return _Driver(1)

    factory = (lambda ref, config: create(ref, config)) if wrapped else create
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        try:
            _extend(manager, factory)  # type: ignore[arg-type]
            result = manager.connection()
        except AFError as caught:
            error = caught
        manager.purge()
        del result
        gc.collect()

    assert error is not None
    _assert_no_unawaited_warnings(captured)


@pytest.mark.parametrize("wrapped", [False, True], ids=("async-function", "awaitable-result"))
def test_async_verifier_result_is_rejected_without_coroutine_leak(wrapped: bool) -> None:
    manager = _manager(connections={"primary": {"driver": "remote"}})
    driver = _Driver(1)
    error: BaseException | None = None

    async def verify(
        _ref: ConnectionRef,
        _driver: _Driver,
        binding: CapabilityBinding,
    ) -> CapabilityEvidence:
        return _evidence(binding)

    verifier = (lambda ref, current, binding: verify(ref, current, binding)) if wrapped else verify
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        try:
            _extend(
                manager,
                lambda _ref, _config: driver,
                verifier=verifier,  # type: ignore[arg-type]
                disposer=lambda current: setattr(current, "closed", True),
            )
            manager.connection()
        except AFError as caught:
            error = caught
        gc.collect()

    assert error is not None
    _assert_no_unawaited_warnings(captured)


@pytest.mark.parametrize("wrapped", [False, True], ids=("async-function", "awaitable-result"))
def test_async_disposer_result_is_rejected_without_coroutine_leak(wrapped: bool) -> None:
    manager = _manager(connections={"primary": {"driver": "remote"}})
    driver = _Driver(1)
    error: BaseException | None = None

    async def close(current: _Driver) -> None:
        current.closed = True

    dispose = (lambda current: close(current)) if wrapped else close
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        try:
            _extend(
                manager,
                lambda _ref, _config: driver,
                disposer=dispose,
            )
            manager.connection()
            manager.close()
        except AFError as caught:
            error = caught
        gc.collect()

    assert error is not None
    _assert_no_unawaited_warnings(captured)


@pytest.mark.parametrize(
    "wrapped",
    [False, True],
    ids=("async-generator-function", "async-generator-result"),
)
def test_async_generator_factory_is_never_admitted(wrapped: bool) -> None:
    manager = _manager(connections={"primary": {"driver": "remote"}})
    error: BaseException | None = None
    result: object | None = None

    async def generate(
        _ref: ConnectionRef,
        _config: Mapping[str, object],
    ) -> AsyncIterator[_Driver]:
        yield _Driver(1)

    factory = (lambda ref, config: generate(ref, config)) if wrapped else generate
    try:
        _extend(manager, factory)  # type: ignore[arg-type]
        result = manager.connection()
    except AFError as caught:
        error = caught
    manager.purge()
    del result
    gc.collect()

    assert error is not None
    assert manager.resolved_connections == ()


@pytest.mark.parametrize(
    "wrapped",
    [False, True],
    ids=("async-generator-function", "async-generator-result"),
)
def test_async_generator_verifier_is_never_admitted(wrapped: bool) -> None:
    manager = _manager(connections={"primary": {"driver": "remote"}})
    driver = _Driver(1)
    error: BaseException | None = None
    registered = False

    async def generate(
        _ref: ConnectionRef,
        _driver: _Driver,
        binding: CapabilityBinding,
    ) -> AsyncIterator[CapabilityEvidence]:
        yield _evidence(binding)

    verifier = (
        (lambda ref, current, binding: generate(ref, current, binding)) if wrapped else generate
    )
    try:
        _extend(
            manager,
            lambda _ref, _config: driver,
            verifier=verifier,  # type: ignore[arg-type]
            disposer=lambda current: setattr(current, "closed", True),
        )
        registered = True
        manager.connection()
    except AFError as caught:
        error = caught

    assert error is not None
    assert manager.resolved_connections == ()
    if registered:
        assert driver.closed


@pytest.mark.parametrize(
    "wrapped",
    [False, True],
    ids=("async-generator-function", "async-generator-result"),
)
def test_async_generator_disposer_cannot_report_success(wrapped: bool) -> None:
    manager = _manager(connections={"primary": {"driver": "remote"}})
    driver = _Driver(1)
    error: BaseException | None = None

    async def generate(current: _Driver) -> AsyncIterator[None]:
        current.closed = True
        yield None

    disposer = (lambda current: generate(current)) if wrapped else generate
    try:
        _extend(
            manager,
            lambda _ref, _config: driver,
            disposer=disposer,
        )
        manager.connection()
        manager.close()
    except AFError as caught:
        error = caught

    assert error is not None
    assert manager.resolved_connections == ()


@pytest.mark.parametrize(
    "clock",
    [
        object(),
        lambda: datetime(2026, 7, 23, 20),
        lambda: (_ for _ in ()).throw(RuntimeError("private clock failure")),
    ],
    ids=("not-callable", "naive-result", "raising"),
)
def test_invalid_clock_is_rejected_with_a_sanitized_manager_error(clock: object) -> None:
    try:
        manager = Manager[_Driver](
            manager_id="database",
            connections={"primary": {"driver": "remote"}},
            default="primary",
            deployment_profile="production",
            policy_version="policy-v1",
            clock=clock,  # type: ignore[arg-type]
        )
    except ManagerConfigurationError:
        return

    driver = _Driver(1)
    _extend(
        manager,
        lambda _ref, _config: driver,
        disposer=lambda current: setattr(current, "closed", True),
    )
    with pytest.raises((ManagerConfigurationError, CapabilityValidationError)):
        manager.connection()
    assert driver.closed
