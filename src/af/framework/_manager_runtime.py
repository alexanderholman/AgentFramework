"""Deterministic lazy resolution and ownership for named drivers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from inspect import isawaitable, iscoroutinefunction
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from af.framework._manager_contracts import (
    AsyncDriverFactory,
    CapabilitySet,
    CompositeMode,
    ConnectionConfig,
    ConnectionRef,
    DriverCapabilityUnavailable,
    DriverConstructionFailed,
    DriverFactory,
    DriverQuarantined,
    DriverUnavailable,
    HealthStatus,
    InvalidManagerConfiguration,
    ManagedDriver,
    ManagerDisposalError,
    ManagerError,
    UnknownConnection,
    UnknownDriver,
    _canonical_identifier,
    _canonical_reason,
    _MAX_CONNECTIONS,
)
from af.framework.errors import AsyncDisposalRequired, AsyncResolutionRequired

if TYPE_CHECKING:
    from af.framework._manager_composite import CompositeDriver

__all__ = ("Manager",)

DriverT = TypeVar("DriverT", bound=ManagedDriver)


@dataclass(frozen=True, slots=True)
class _FactoryRegistration(Generic[DriverT]):
    factory: DriverFactory[DriverT] | AsyncDriverFactory[DriverT]
    asynchronous: bool


@dataclass(slots=True)
class _CachedConnection(Generic[DriverT]):
    driver: DriverT
    admitted_capabilities: CapabilitySet
    health: HealthStatus


@dataclass(frozen=True, slots=True)
class _Quarantine:
    reason_code: str


class Manager(Generic[DriverT]):
    """Resolve immutable named connection declarations through registered factories."""

    def __init__(
        self,
        manager_id: str,
        connections: Mapping[str, ConnectionConfig],
        *,
        default: str | None = None,
    ) -> None:
        self._manager_id = _canonical_identifier(manager_id, "manager id")
        if not isinstance(connections, Mapping) or len(connections) > _MAX_CONNECTIONS:
            raise InvalidManagerConfiguration("manager connections must be a bounded mapping")
        normalized: dict[str, ConnectionConfig] = {}
        for name, config in connections.items():
            canonical_name = _canonical_identifier(name, "connection name")
            if not isinstance(config, ConnectionConfig):
                raise InvalidManagerConfiguration("connection configuration is invalid")
            normalized[canonical_name] = config
        if default is not None:
            _canonical_identifier(default, "default connection")
            if default not in normalized:
                raise InvalidManagerConfiguration("default connection is not configured")
        self._connections = MappingProxyType(dict(sorted(normalized.items())))
        self._default = default
        self._factories: dict[str, _FactoryRegistration[DriverT]] = {}
        self._cache: dict[str, _CachedConnection[DriverT]] = {}
        self._flights: dict[str, Future[DriverT]] = {}
        self._quarantined: dict[str, _Quarantine] = {}
        self._retired: list[DriverT] = []
        self._creation_order: list[DriverT] = []
        self._lock = RLock()
        self._closed = False

    @property
    def manager_id(self) -> str:
        return self._manager_id

    @property
    def default_connection(self) -> str | None:
        return self._default

    @property
    def configured_connections(self) -> tuple[str, ...]:
        return tuple(self._connections)

    @property
    def cached_connections(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._cache))

    def extend(
        self,
        driver_id: str,
        factory: DriverFactory[DriverT],
        *,
        replace: bool = False,
    ) -> None:
        if iscoroutinefunction(factory):
            raise InvalidManagerConfiguration("asynchronous driver factories use extend_async")
        self._register_factory(driver_id, factory, asynchronous=False, replace=replace)

    def extend_async(
        self,
        driver_id: str,
        factory: AsyncDriverFactory[DriverT],
        *,
        replace: bool = False,
    ) -> None:
        self._register_factory(driver_id, factory, asynchronous=True, replace=replace)

    def _register_factory(
        self,
        driver_id: str,
        factory: DriverFactory[DriverT] | AsyncDriverFactory[DriverT],
        *,
        asynchronous: bool,
        replace: bool,
    ) -> None:
        canonical = _canonical_identifier(driver_id, "driver id")
        if not callable(factory) or type(replace) is not bool:
            raise InvalidManagerConfiguration("driver factory registration is invalid")
        with self._lock:
            self._assert_open()
            existing = self._factories.get(canonical)
            if existing is not None and not replace:
                raise InvalidManagerConfiguration("driver factory is already registered")
            affected = {
                name
                for name, config in self._connections.items()
                if config.driver == canonical
                and (name in self._cache or name in self._flights)
            }
            if affected:
                raise InvalidManagerConfiguration("an active driver factory cannot be replaced")
            self._factories[canonical] = _FactoryRegistration(factory, asynchronous)

    def connection(
        self,
        name: str | None = None,
        *,
        required: CapabilitySet | None = None,
    ) -> DriverT:
        required = CapabilitySet() if required is None else required
        if not isinstance(required, CapabilitySet):
            raise TypeError("required must be a CapabilitySet")
        with self._lock:
            self._assert_open()
            connection_name, config = self._select_config(name)
            self._assert_not_quarantined(connection_name)
            cached = self._cache.get(connection_name)
            if cached is not None:
                return self._validate_cached(connection_name, cached, required)
            if connection_name in self._flights:
                raise AsyncResolutionRequired("driver construction is already asynchronous")
            registration = self._factories.get(config.driver)
            if registration is None:
                raise UnknownDriver("configured driver factory is unavailable")
            if registration.asynchronous:
                raise AsyncResolutionRequired("driver factory requires aconnection")
            ref = ConnectionRef(connection_name, config.required_capabilities)
            try:
                driver = cast(DriverFactory[DriverT], registration.factory)(ref, config.options)
                if isawaitable(driver):
                    _reject_awaitable(driver)
                    raise AsyncResolutionRequired("driver factory requires aconnection")
            except AsyncResolutionRequired:
                raise
            except BaseException as error:
                raise DriverConstructionFailed(protected_cause=error) from None
            return self._admit_new(connection_name, config, driver, required)

    async def aconnection(
        self,
        name: str | None = None,
        *,
        required: CapabilitySet | None = None,
    ) -> DriverT:
        required = CapabilitySet() if required is None else required
        if not isinstance(required, CapabilitySet):
            raise TypeError("required must be a CapabilitySet")
        flight: Future[DriverT] | None = None
        creator = False
        sync_resolution = False
        with self._lock:
            self._assert_open()
            connection_name, config = self._select_config(name)
            self._assert_not_quarantined(connection_name)
            cached = self._cache.get(connection_name)
            if cached is not None:
                return self._validate_cached(connection_name, cached, required)
            registration = self._factories.get(config.driver)
            if registration is None:
                raise UnknownDriver("configured driver factory is unavailable")
            existing_flight = self._flights.get(connection_name)
            if existing_flight is None and not registration.asynchronous:
                sync_resolution = True
            else:
                sync_resolution = False
            if existing_flight is None and registration.asynchronous:
                flight = Future()
                self._flights[connection_name] = flight
                creator = True
            else:
                flight = existing_flight
                creator = False

        if sync_resolution:
            return await asyncio.to_thread(self.connection, connection_name, required=required)
        if not creator:
            assert flight is not None
            driver = await asyncio.wrap_future(flight)
            with self._lock:
                cached = self._cache.get(connection_name)
                if cached is None or cached.driver is not driver:
                    raise DriverUnavailable("driver construction did not produce a live connection")
                return self._validate_cached(connection_name, cached, required)

        assert flight is not None
        ref = ConnectionRef(connection_name, config.required_capabilities)
        try:
            produced = cast(AsyncDriverFactory[DriverT], registration.factory)(ref, config.options)
            if not isawaitable(produced):
                raise InvalidManagerConfiguration(
                    "asynchronous driver factory returned no awaitable"
                )
            driver = await produced
            with self._lock:
                self._assert_open()
                admitted = self._admit_new(connection_name, config, driver, required)
                self._flights.pop(connection_name, None)
                flight.set_result(admitted)
                return admitted
        except asyncio.CancelledError as error:
            with self._lock:
                self._flights.pop(connection_name, None)
                if not flight.done():
                    flight.set_exception(error)
            raise
        except BaseException as error:
            failure = error if isinstance(error, ManagerError) else DriverConstructionFailed(
                protected_cause=error
            )
            with self._lock:
                self._flights.pop(connection_name, None)
                if not flight.done():
                    flight.set_exception(failure)
            raise failure from None

    def _admit_new(
        self,
        name: str,
        config: ConnectionConfig,
        driver: DriverT,
        requested: CapabilitySet,
    ) -> DriverT:
        status = self._read_health(driver)
        if not status.serving:
            self._retire_and_quarantine(name, driver, status.reason_code or "health.not_serving")
        configured_missing = config.required_capabilities.missing_from(status.capabilities)
        if configured_missing:
            self._retire_and_quarantine(name, driver, "capability.configured_missing")
        record = _CachedConnection(driver, config.required_capabilities, status)
        self._cache[name] = record
        self._creation_order.append(driver)
        requested_missing = requested.missing_from(status.capabilities)
        if requested_missing:
            raise DriverCapabilityUnavailable(requested_missing)
        record.admitted_capabilities = record.admitted_capabilities.union(requested)
        return driver

    def _validate_cached(
        self,
        name: str,
        cached: _CachedConnection[DriverT],
        requested: CapabilitySet,
    ) -> DriverT:
        status = self._read_health(cached.driver)
        if status.revision < cached.health.revision:
            self._quarantine_cached(name, "health.revision_regressed")
        if status.revision == cached.health.revision and status != cached.health:
            self._quarantine_cached(name, "health.revision_conflict")
        if not status.serving:
            self._quarantine_cached(name, status.reason_code or "health.not_serving")
        admitted_missing = cached.admitted_capabilities.missing_from(status.capabilities)
        if admitted_missing:
            self._quarantine_cached(name, "capability.runtime_regression")
        requested_missing = requested.missing_from(status.capabilities)
        if requested_missing:
            raise DriverCapabilityUnavailable(requested_missing)
        cached.health = status
        cached.admitted_capabilities = cached.admitted_capabilities.union(requested)
        return cached.driver

    @staticmethod
    def _read_health(driver: DriverT) -> HealthStatus:
        try:
            callback = getattr(driver, "health")
            if not callable(callback) or iscoroutinefunction(callback):
                raise TypeError("driver health must be a synchronous callable")
            status = callback()
        except ManagerError:
            raise
        except BaseException as error:
            raise DriverUnavailable(
                "driver health check failed",
                protected_cause=error,
            ) from None
        if isawaitable(status):
            _reject_awaitable(status)
            raise DriverUnavailable("driver health check must be synchronous")
        if not isinstance(status, HealthStatus):
            raise DriverUnavailable("driver returned invalid health evidence")
        return status

    def _select_config(self, name: str | None) -> tuple[str, ConnectionConfig]:
        selected = self._default if name is None else name
        if selected is None:
            raise UnknownConnection("default connection is not configured")
        try:
            canonical = _canonical_identifier(selected, "connection name")
        except InvalidManagerConfiguration:
            raise UnknownConnection("connection is not configured") from None
        config = self._connections.get(canonical)
        if config is None:
            raise UnknownConnection("connection is not configured")
        return canonical, config

    def _assert_not_quarantined(self, name: str) -> None:
        quarantined = self._quarantined.get(name)
        if quarantined is not None:
            raise DriverQuarantined(quarantined.reason_code)

    def _quarantine_cached(self, name: str, reason_code: str) -> None:
        cached = self._cache.pop(name, None)
        if cached is not None:
            self._retired.append(cached.driver)
        self._quarantined[name] = _Quarantine(_canonical_reason(reason_code, "quarantine reason"))
        raise DriverQuarantined(reason_code)

    def _retire_and_quarantine(self, name: str, driver: DriverT, reason_code: str) -> None:
        self._retired.append(driver)
        self._creation_order.append(driver)
        self._quarantined[name] = _Quarantine(_canonical_reason(reason_code, "quarantine reason"))
        raise DriverQuarantined(reason_code)

    def health(self, name: str | None = None) -> HealthStatus:
        """Return validated current evidence, applying quarantine rules to cached drivers."""
        with self._lock:
            self._assert_open()
            connection_name, _ = self._select_config(name)
            self._assert_not_quarantined(connection_name)
            cached = self._cache.get(connection_name)
            if cached is None:
                raise DriverUnavailable("driver connection has not been resolved")
            self._validate_cached(connection_name, cached, CapabilitySet())
            return cached.health

    def quarantine_reason(self, name: str) -> str | None:
        with self._lock:
            selected, _ = self._select_config(name)
            value = self._quarantined.get(selected)
            return None if value is None else value.reason_code

    def restore(self, name: str) -> None:
        with self._lock:
            self._assert_open()
            selected, _ = self._select_config(name)
            self._quarantined.pop(selected, None)

    def invalidate(self, name: str) -> None:
        with self._lock:
            self._assert_open()
            selected, _ = self._select_config(name)
            if selected in self._flights:
                raise DriverUnavailable("driver construction is in progress")
            cached = self._cache.pop(selected, None)
            if cached is not None:
                self._retired.append(cached.driver)

    def composite(
        self,
        mode: CompositeMode,
        connections: Iterable[ConnectionRef],
        *,
        required: CapabilitySet | None = None,
    ) -> CompositeDriver[DriverT]:
        from af.framework._manager_composite import CompositeDriver

        return CompositeDriver(
            self,
            mode,
            tuple(connections),
            required=CapabilitySet() if required is None else required,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._flights:
                raise DriverUnavailable("driver construction is in progress")
            drivers = self._owned_drivers()
            for driver in drivers:
                close = getattr(driver, "close", None)
                aclose = getattr(driver, "aclose", None)
                if iscoroutinefunction(close) or (not callable(close) and callable(aclose)):
                    raise AsyncDisposalRequired("manager owns asynchronous drivers")
            self._closed = True
            self._cache.clear()
            self._retired.clear()
            self._flights.clear()
        failures: list[BaseException] = []
        for driver in reversed(drivers):
            close = getattr(driver, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if isawaitable(result):
                    _reject_awaitable(result)
                    raise AsyncDisposalRequired("driver close returned an awaitable")
            except BaseException as error:
                failures.append(error)
        if failures:
            raise ManagerDisposalError(tuple(failures))

    async def aclose(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._flights:
                raise DriverUnavailable("driver construction is in progress")
            self._closed = True
            drivers = self._owned_drivers()
            self._cache.clear()
            self._retired.clear()
            self._flights.clear()
        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        for driver in reversed(drivers):
            try:
                aclose = getattr(driver, "aclose", None)
                close = getattr(driver, "close", None)
                if callable(aclose):
                    result = aclose()
                    if not isawaitable(result):
                        raise TypeError("driver aclose returned no awaitable")
                    await result
                elif callable(close):
                    result = await asyncio.to_thread(close)
                    if isawaitable(result):
                        _reject_awaitable(result)
                        raise TypeError("driver close returned an awaitable")
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except BaseException as error:
                failures.append(error)
        if cancellation is not None:
            raise cancellation
        if failures:
            raise ManagerDisposalError(tuple(failures))

    def _owned_drivers(self) -> tuple[DriverT, ...]:
        ordered = [*self._creation_order, *self._retired]
        unique: list[DriverT] = []
        seen: set[int] = set()
        for driver in ordered:
            if id(driver) in seen:
                continue
            seen.add(id(driver))
            unique.append(driver)
        return tuple(unique)

    def _assert_open(self) -> None:
        if self._closed:
            raise DriverUnavailable("driver manager is closed")


def _reject_awaitable(value: object) -> None:
    if isinstance(value, asyncio.Future):
        if not value.done():
            value.cancel()
        return
    close = getattr(value, "close", None)
    if callable(close):
        close()
