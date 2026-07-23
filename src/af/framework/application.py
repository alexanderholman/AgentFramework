"""Deterministic application and service-provider lifecycle."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from inspect import isawaitable, iscoroutinefunction
from threading import Event, Lock, RLock, Thread
from time import monotonic
from typing import cast

from af.framework.container import (
    AsyncFactory,
    Container,
    Factory,
    ProviderRegistrar,
    ProviderResourceRegistry,
    Qualifier,
    Scope,
    ServiceKey,
    ServiceProvider,
)
from af.framework.context import without_context
from af.framework.errors import (
    ApplicationBootError,
    ApplicationGraphError,
    ApplicationRegistrationError,
    ApplicationShutdownError,
    AsyncDisposalRequired,
    AsyncLifecycleRequired,
    InvalidBinding,
    ShutdownTimeout,
)

__all__ = (
    "Application",
    "ApplicationState",
    "ProviderBootContext",
)

_PROVIDER_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")


def _default_cleanup_time() -> float:
    return 30.0


class ApplicationState(StrEnum):
    CONSTRUCTED = "constructed"
    REGISTERED = "registered"
    BOOTING = "booting"
    BOOTED = "booted"
    SERVING = "serving"
    DRAINING = "draining"
    SHUTTING_DOWN = "shutting_down"
    SHUTDOWN = "shutdown"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _OwnedResource:
    value: object
    close: Callable[[], object] | None
    aclose: Callable[[], Awaitable[object]] | None


@dataclass(frozen=True, slots=True)
class _ProviderNode:
    provider_id: str
    dependencies: tuple[str, ...]
    deferred: bool
    provider: ServiceProvider


class ProviderBootContext(ProviderResourceRegistry):
    """Records resources as a provider acquires them during boot."""

    def __init__(
        self,
        *,
        allow_async: bool,
        claim_resource: Callable[[object], bool],
        release_resource: Callable[[object], None],
        remaining_cleanup_time: Callable[[], float] = _default_cleanup_time,
    ) -> None:
        self._resources: list[_OwnedResource] = []
        self._owned_ids: set[int] = set()
        self._allow_async = allow_async
        self._claim_resource = claim_resource
        self._release_resource = release_resource
        self._remaining_cleanup_time = remaining_cleanup_time
        self._active = True
        self._disposed = False
        self._disposal_failures: tuple[BaseException, ...] = ()

    def own(
        self,
        value: object,
        *,
        close: Callable[[], object] | None = None,
        aclose: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        if not self._active:
            raise InvalidBinding("provider boot resource context is inactive")
        if close is not None and not callable(close):
            raise InvalidBinding("provider resource close callback must be callable")
        if aclose is not None and not callable(aclose):
            raise InvalidBinding("provider resource aclose callback must be callable")
        if id(value) in self._owned_ids:
            return
        resolved_close = close
        resolved_aclose = aclose
        if resolved_close is None:
            candidate = getattr(value, "close", None)
            resolved_close = cast(
                Callable[[], object] | None, candidate if callable(candidate) else None
            )
        if resolved_aclose is None:
            candidate = getattr(value, "aclose", None)
            resolved_aclose = cast(
                Callable[[], Awaitable[object]] | None,
                candidate if callable(candidate) else None,
            )
        if resolved_close is None and resolved_aclose is None:
            raise InvalidBinding("provider boot resource must be disposable")
        if resolved_close is not None and iscoroutinefunction(resolved_close):
            raise InvalidBinding("asynchronous disposal must use aclose")
        if not self._claim_resource(value):
            raise InvalidBinding("provider resource is already owned by the application")
        self._owned_ids.add(id(value))
        self._resources.append(_OwnedResource(value, resolved_close, resolved_aclose))

    def finish_boot(self) -> None:
        self._active = False

    def requires_async_disposal(self) -> bool:
        return any(resource.close is None for resource in self._resources)

    @property
    def disposal_failures(self) -> tuple[BaseException, ...]:
        return self._disposal_failures

    def dispose(self) -> tuple[BaseException, ...]:
        if self._disposed:
            return ()
        if self.requires_async_disposal():
            raise AsyncLifecycleRequired(
                "provider owns asynchronous resources; use application ashutdown"
            )
        self._active = False
        self._disposed = True
        failures: list[BaseException] = []
        try:
            for resource in reversed(self._resources):
                try:
                    if resource.close is None:  # pragma: no cover - preflighted above
                        continue
                    result = _call_cleanup_bounded(
                        resource.close,
                        self._remaining_cleanup_time(),
                    )
                    if isawaitable(result):
                        _reject_awaitable(result)
                        raise AsyncLifecycleRequired(
                            "provider resource returned an awaitable; use application ashutdown"
                        )
                except BaseException as error:
                    failures.append(error)
        finally:
            for resource in self._resources:
                self._release_resource(resource.value)
            self._resources.clear()
            self._owned_ids.clear()
        self._disposal_failures = tuple(failures)
        return self._disposal_failures

    async def adispose(self) -> tuple[BaseException, ...]:
        if self._disposed:
            return ()
        self._active = False
        self._disposed = True
        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        try:
            for resource in reversed(self._resources):
                try:
                    with without_context():
                        if resource.aclose is not None:
                            await _await_cleanup_bounded(
                                resource.aclose(),
                                self._remaining_cleanup_time(),
                            )
                        elif resource.close is not None:
                            result = await _await_cleanup_bounded(
                                asyncio.to_thread(_invoke_without_context, resource.close),
                                self._remaining_cleanup_time(),
                            )
                            if isawaitable(result):
                                _reject_awaitable(result)
                                raise InvalidBinding(
                                    "provider resource close callback returned an awaitable"
                                )
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
                except BaseException as error:
                    failures.append(error)
        finally:
            for resource in self._resources:
                self._release_resource(resource.value)
            self._resources.clear()
            self._owned_ids.clear()
        self._disposal_failures = tuple(failures)
        if cancellation is not None:
            raise cancellation
        return self._disposal_failures


class _ContainerRegistrar(ProviderRegistrar):
    """Narrow capability; intentionally has no resolution or scope APIs."""

    def __init__(self, container: Container) -> None:
        self._container = container

    def bind(
        self,
        token: object,
        factory: Factory,
        *,
        qualifier: Qualifier | str | None = None,
        scope: Scope = Scope.TRANSIENT,
        owned: bool = True,
        replace: bool = False,
    ) -> None:
        self._container.bind(
            token,
            factory,
            qualifier=qualifier,
            scope=scope,
            owned=owned,
            replace=replace,
        )

    def bind_async(
        self,
        token: object,
        factory: AsyncFactory,
        *,
        qualifier: Qualifier | str | None = None,
        scope: Scope = Scope.TRANSIENT,
        owned: bool = True,
        replace: bool = False,
    ) -> None:
        self._container.bind_async(
            token,
            factory,
            qualifier=qualifier,
            scope=scope,
            owned=owned,
            replace=replace,
        )

    def alias(
        self,
        alias: object,
        target: object,
        *,
        alias_qualifier: Qualifier | str | None = None,
        target_qualifier: Qualifier | str | None = None,
        replace: bool = False,
    ) -> None:
        self._container.alias(
            alias,
            target,
            alias_qualifier=alias_qualifier,
            target_qualifier=target_qualifier,
            replace=replace,
        )

    def tag(self, name: str, *services: ServiceKey | object) -> None:
        self._container.tag(name, *services)


class Application:
    """Own one immutable provider graph and one container application scope."""

    def __init__(self, root: Container, providers: Iterable[ServiceProvider]) -> None:
        if not isinstance(root, Container):
            raise TypeError("application root must be a Container")
        self._root = root
        self._providers = tuple(providers)
        self._provider_by_id: dict[str, _ProviderNode] = {}
        self._provider_order: tuple[str, ...] = ()
        self._application_container: Container | None = None
        self._contexts: dict[str, ProviderBootContext] = {}
        self._provider_resources: dict[int, object] = {}
        self._booted: list[str] = []
        self._pending_async_cleanup: list[str] = []
        self._pending_async_shutdowns: set[str] = set()
        self._pending_async_contexts: set[str] = set()
        self._pending_shutdown_awaitables: dict[str, Awaitable[object]] = {}
        self._state = ApplicationState.CONSTRUCTED
        self._cleanup_complete = False
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._activation_lock: asyncio.Lock | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._cleanup_failures: tuple[BaseException, ...] = ()
        self._sync_lock = RLock()
        self._lifecycle_gate = Lock()
        self._sync_operations = 0
        self._sync_idle = Event()
        self._sync_idle.set()
        self._async_activation_count = 0
        self._async_shutdown_claimed = False
        self._cleanup_timeout = root.shutdown_timeout
        self._cleanup_deadline: float | None = None

    @property
    def state(self) -> ApplicationState:
        return self._state

    @property
    def container(self) -> Container:
        return self._application_container or self._root

    @property
    def provider_order(self) -> tuple[str, ...]:
        return self._provider_order

    @property
    def cleanup_failures(self) -> tuple[BaseException, ...]:
        return self._cleanup_failures

    def register(self) -> None:
        self._begin_sync_operation()
        try:
            with self._sync_lock:
                self._register_sync()
        finally:
            self._end_sync_operation()

    def _register_sync(self) -> None:
        if self._state is ApplicationState.REGISTERED:
            return
        if self._state is not ApplicationState.CONSTRUCTED:
            raise InvalidBinding("application cannot register from its current state")
        try:
            self._provider_order = self._validate_and_order_providers()
        except ApplicationGraphError:
            self._state = ApplicationState.FAILED
            self._close_root_after_failure()
            raise
        try:
            with without_context(), self._root.provider_registration():
                registrar = _ContainerRegistrar(self._root)
                for provider_id in self._provider_order:
                    try:
                        provider = self._provider_by_id[provider_id].provider
                        if iscoroutinefunction(provider.register):
                            raise InvalidBinding("provider registration must be synchronous")
                        result = cast(object, provider.register(registrar))
                        if isawaitable(result):
                            _reject_awaitable(result)
                            raise InvalidBinding("provider registration must be synchronous")
                    except asyncio.CancelledError:
                        raise
                    except BaseException as error:
                        raise ApplicationRegistrationError(
                            provider_id,
                            protected_cause=error,
                        ) from None
            self._root.freeze()
            self._application_container = self._root.application_scope()
            self._state = ApplicationState.REGISTERED
        except BaseException:
            self._state = ApplicationState.FAILED
            self._close_root_after_failure()
            raise

    def boot(self) -> None:
        self._begin_sync_operation()
        try:
            with self._sync_lock:
                self._boot_sync()
        finally:
            self._end_sync_operation()

    def _boot_sync(self) -> None:
        if self._state is ApplicationState.BOOTED:
            return
        if self._state is not ApplicationState.REGISTERED:
            raise InvalidBinding("application must be registered before boot")
        self._state = ApplicationState.BOOTING
        required = self._initial_boot_set()
        for provider_id in self._provider_order:
            if provider_id not in required:
                continue
            self._boot_one(provider_id, self._provider_by_id[provider_id].provider)
        self._state = ApplicationState.BOOTED

    async def aboot(self) -> None:
        if self._state is ApplicationState.BOOTED:
            return
        if self._state is not ApplicationState.REGISTERED:
            raise InvalidBinding("application must be registered before boot")
        self._bind_async_loop()
        self._state = ApplicationState.BOOTING
        required = self._initial_boot_set()
        for provider_id in self._provider_order:
            if provider_id not in required:
                continue
            await self._aboot_one(provider_id, self._provider_by_id[provider_id].provider)
        self._state = ApplicationState.BOOTED

    def serve(self) -> None:
        self._begin_sync_operation()
        try:
            with self._sync_lock:
                self._serve_sync()
        finally:
            self._end_sync_operation()

    def _serve_sync(self) -> None:
        if self._state is ApplicationState.SERVING:
            return
        if self._state is not ApplicationState.BOOTED:
            raise InvalidBinding("application must be booted before serving")
        if self._activation_lock is not None and self._activation_lock.locked():
            raise InvalidBinding("application cannot serve during provider activation")
        self._state = ApplicationState.SERVING

    def start(self) -> None:
        self._begin_sync_operation()
        try:
            with self._sync_lock:
                if self._state is ApplicationState.CONSTRUCTED:
                    self._register_sync()
                self._boot_sync()
                self._serve_sync()
        finally:
            self._end_sync_operation()

    async def astart(self) -> None:
        if self._state is ApplicationState.CONSTRUCTED:
            self.register()
        await self.aboot()
        self.serve()

    def activate(self, provider_id: str) -> None:
        self._begin_sync_operation(disallow_async_activation=True)
        try:
            with self._sync_lock:
                self._assert_activation_state()
                self._validate_requested_provider(provider_id)
                for dependency_id in self._activation_order(provider_id):
                    if dependency_id in self._booted:
                        continue
                    self._boot_one(
                        dependency_id,
                        self._provider_by_id[dependency_id].provider,
                    )
        finally:
            self._end_sync_operation()

    async def aactivate(self, provider_id: str) -> None:
        self._assert_activation_state()
        self._validate_requested_provider(provider_id)
        self._bind_async_loop()
        self._begin_async_activation()
        try:
            if self._activation_lock is None:
                self._activation_lock = asyncio.Lock()
            async with self._activation_lock:
                self._assert_activation_state()
                for dependency_id in self._activation_order(provider_id):
                    if dependency_id in self._booted:
                        continue
                    await self._aboot_one(
                        dependency_id,
                        self._provider_by_id[dependency_id].provider,
                    )
        finally:
            self._end_async_activation()

    def drain(self) -> None:
        self._begin_sync_operation()
        try:
            with self._sync_lock:
                self._drain_sync()
        finally:
            self._end_sync_operation()

    def _drain_sync(self) -> None:
        if self._state is ApplicationState.DRAINING:
            return
        if self._state is not ApplicationState.SERVING:
            raise InvalidBinding("only a serving application can drain")
        if self._activation_lock is not None and self._activation_lock.locked():
            raise InvalidBinding("application cannot drain during provider activation")
        self._state = ApplicationState.DRAINING

    def shutdown(self) -> None:
        if self._cleanup_complete or self._state is ApplicationState.SHUTDOWN:
            return
        self._begin_sync_operation()
        try:
            with self._sync_lock:
                if self._state is ApplicationState.SERVING:
                    self._drain_sync()
                self._shutdown_sync()
        finally:
            self._end_sync_operation()

    def _shutdown_sync(self) -> None:
        if self._cleanup_complete or self._state is ApplicationState.SHUTDOWN:
            return
        if self._state is ApplicationState.CONSTRUCTED:
            self._root.close()
            self._cleanup_complete = True
            self._state = ApplicationState.SHUTDOWN
            return
        if self._state not in {
            ApplicationState.REGISTERED,
            ApplicationState.BOOTED,
            ApplicationState.SERVING,
            ApplicationState.DRAINING,
            ApplicationState.FAILED,
        }:
            raise InvalidBinding("application cannot shut down from its current state")
        if self._activation_lock is not None and self._activation_lock.locked():
            raise AsyncLifecycleRequired("provider activation requires application ashutdown")
        if (
            self._pending_async_cleanup
            or any(
                iscoroutinefunction(self._provider_by_id[provider_id].provider.shutdown)
                or self._contexts[provider_id].requires_async_disposal()
                for provider_id in self._booted
            )
            or self._root.requires_async_disposal()
        ):
            raise AsyncLifecycleRequired("application shutdown requires ashutdown")
        self._state = ApplicationState.SHUTTING_DOWN
        provider_failures, cleanup_cancellation, async_required = self._shutdown_booted_sync()
        failures = list(provider_failures)
        if async_required or self._root.requires_async_disposal():
            self._cleanup_failures = tuple(failures)
            if cleanup_cancellation is not None:
                raise cleanup_cancellation from None
            raise AsyncLifecycleRequired("application shutdown requires ashutdown")
        try:
            self._root.close()
        except AsyncDisposalRequired:
            self._cleanup_failures = tuple(failures)
            if cleanup_cancellation is not None:
                raise cleanup_cancellation from None
            raise AsyncLifecycleRequired("application shutdown requires ashutdown") from None
        except asyncio.CancelledError as error:
            cleanup_cancellation = cleanup_cancellation or error
        except BaseException as error:
            failures.append(error)
        self._cleanup_failures = tuple(failures)
        self._cleanup_complete = True
        self._state = ApplicationState.SHUTDOWN
        if cleanup_cancellation is not None:
            raise cleanup_cancellation from None
        if failures:
            raise ApplicationShutdownError(tuple(failures))

    async def ashutdown(self) -> None:
        if self._cleanup_complete or self._state is ApplicationState.SHUTDOWN:
            return
        self._bind_async_loop()
        if self._state not in {
            ApplicationState.CONSTRUCTED,
            ApplicationState.REGISTERED,
            ApplicationState.BOOTED,
            ApplicationState.SERVING,
            ApplicationState.DRAINING,
            ApplicationState.FAILED,
            ApplicationState.SHUTTING_DOWN,
        }:
            raise InvalidBinding("application cannot shut down from its current state")
        if self._shutdown_task is None:
            self._claim_async_shutdown()
            self._shutdown_task = asyncio.create_task(self._ashutdown_impl())
            self._shutdown_task.add_done_callback(_consume_cleanup_future)
        task = self._shutdown_task
        caller_cancellation, completed = await _await_protected_bounded(
            task,
            self._cleanup_timeout,
        )
        if caller_cancellation is not None:
            raise caller_cancellation from None
        if not completed:
            raise ApplicationShutdownError((ShutdownTimeout(),))
        task.result()

    async def _ashutdown_impl(self) -> None:
        while not self._sync_idle.is_set():
            await asyncio.sleep(0.01)
        if self._cleanup_complete or self._state is ApplicationState.SHUTDOWN:
            return
        if self._state not in {
            ApplicationState.CONSTRUCTED,
            ApplicationState.REGISTERED,
            ApplicationState.BOOTED,
            ApplicationState.SERVING,
            ApplicationState.DRAINING,
            ApplicationState.FAILED,
            ApplicationState.SHUTTING_DOWN,
        }:
            raise InvalidBinding("application cannot shut down from its current state")
        if self._state is ApplicationState.SERVING:
            self._state = ApplicationState.DRAINING
        self._state = ApplicationState.SHUTTING_DOWN
        if self._activation_lock is not None:
            async with self._activation_lock:
                if self._cleanup_complete or self._state is ApplicationState.SHUTDOWN:
                    return
                await self._ashutdown_locked()
            return
        await self._ashutdown_locked()

    async def _ashutdown_locked(self) -> None:
        failures = [
            error
            for error in self._cleanup_failures
            if not isinstance(error, AsyncLifecycleRequired)
        ]
        cleanup_cancellation: asyncio.CancelledError | None = None
        if self._state is not ApplicationState.CONSTRUCTED:
            provider_failures, provider_cancellation = await self._shutdown_booted_async()
            failures.extend(provider_failures)
            cleanup_cancellation = provider_cancellation
        try:
            await self._root.aclose()
        except asyncio.CancelledError as error:
            cleanup_cancellation = cleanup_cancellation or error
        except BaseException as error:
            failures.append(error)
        self._cleanup_failures = tuple(failures)
        self._cleanup_complete = True
        self._state = ApplicationState.SHUTDOWN
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if failures:
            raise ApplicationShutdownError(tuple(failures))

    def _validate_and_order_providers(self) -> tuple[str, ...]:
        providers: dict[str, _ProviderNode] = {}
        for provider in self._providers:
            if not isinstance(provider, ServiceProvider):
                raise ApplicationGraphError("provider dependency graph is invalid")
            try:
                provider_id = provider.provider_id
                dependencies = provider.dependencies
                deferred = provider.deferred
            except BaseException:
                raise ApplicationGraphError("provider dependency graph is invalid") from None
            if (
                type(provider_id) is not str
                or _PROVIDER_ID.fullmatch(provider_id) is None
                or type(dependencies) is not tuple
                or any(
                    type(item) is not str or _PROVIDER_ID.fullmatch(item) is None
                    for item in dependencies
                )
                or len(set(dependencies)) != len(dependencies)
                or provider_id in dependencies
                or type(deferred) is not bool
                or provider_id in providers
            ):
                raise ApplicationGraphError("provider dependency graph is invalid")
            providers[provider_id] = _ProviderNode(
                provider_id,
                dependencies,
                deferred,
                provider,
            )
        for node in providers.values():
            if any(dependency not in providers for dependency in node.dependencies):
                raise ApplicationGraphError("provider dependency graph is invalid")
        dependants: dict[str, list[str]] = {provider_id: [] for provider_id in providers}
        indegree = {provider_id: 0 for provider_id in providers}
        for provider_id, node in providers.items():
            indegree[provider_id] = len(node.dependencies)
            for dependency in node.dependencies:
                dependants[dependency].append(provider_id)
        available = sorted(provider_id for provider_id, count in indegree.items() if count == 0)
        ordered: list[str] = []
        while available:
            provider_id = available.pop(0)
            ordered.append(provider_id)
            for dependant in sorted(dependants[provider_id]):
                indegree[dependant] -= 1
                if indegree[dependant] == 0:
                    available.append(dependant)
                    available.sort()
        if len(ordered) != len(providers):
            raise ApplicationGraphError("provider dependency graph is invalid")
        self._provider_by_id = providers
        return tuple(ordered)

    def _initial_boot_set(self) -> frozenset[str]:
        required: set[str] = set()

        def visit(provider_id: str) -> None:
            if provider_id in required:
                return
            for dependency in self._provider_by_id[provider_id].dependencies:
                visit(dependency)
            required.add(provider_id)

        for provider_id in self._provider_order:
            if not self._provider_by_id[provider_id].deferred:
                visit(provider_id)
        return frozenset(required)

    def _boot_one(self, provider_id: str, provider: ServiceProvider) -> None:
        context = ProviderBootContext(
            allow_async=False,
            claim_resource=self._claim_provider_resource,
            release_resource=self._release_provider_resource,
            remaining_cleanup_time=self._remaining_cleanup_time,
        )
        self._contexts[provider_id] = context
        try:
            if iscoroutinefunction(provider.boot):
                raise AsyncLifecycleRequired("provider boot requires application aboot")
            with without_context():
                result = provider.boot(self.container, context)
            if isawaitable(result):
                _reject_awaitable(result)
                raise AsyncLifecycleRequired("provider boot requires application aboot")
            context.finish_boot()
            self._booted.append(provider_id)
        except AsyncLifecycleRequired:
            _, cleanup_cancellation = self._rollback_sync(provider_id)
            if cleanup_cancellation is not None:
                raise cleanup_cancellation from None
            raise
        except asyncio.CancelledError as cancellation:
            self._rollback_sync(provider_id)
            raise cancellation from None
        except BaseException as error:
            failures, cleanup_cancellation = self._rollback_sync(provider_id)
            if cleanup_cancellation is not None:
                raise cleanup_cancellation from None
            raise ApplicationBootError(
                provider_id,
                protected_cause=error,
                cleanup_failures=failures,
            ) from None

    async def _aboot_one(self, provider_id: str, provider: ServiceProvider) -> None:
        context = ProviderBootContext(
            allow_async=True,
            claim_resource=self._claim_provider_resource,
            release_resource=self._release_provider_resource,
            remaining_cleanup_time=self._remaining_cleanup_time,
        )
        self._contexts[provider_id] = context
        try:
            if iscoroutinefunction(provider.boot):
                with without_context():
                    await provider.boot(self.container, context)
            else:
                boot_task = asyncio.create_task(
                    asyncio.to_thread(
                        _invoke_provider_boot,
                        provider,
                        self.container,
                        context,
                    )
                )
                caller_cancellation = await _await_protected(boot_task)
                if caller_cancellation is not None:
                    try:
                        result = boot_task.result()
                    except BaseException:
                        raise caller_cancellation from None
                    if isawaitable(result):
                        _reject_awaitable(result)
                    raise caller_cancellation from None
                result = boot_task.result()
                if isawaitable(result):
                    with without_context():
                        await result
            context.finish_boot()
            self._booted.append(provider_id)
        except asyncio.CancelledError as cancellation:
            cleanup_task = asyncio.create_task(self._rollback_async(provider_id))
            await _await_protected(cleanup_task)
            try:
                cleanup_failures, _ = cleanup_task.result()
                self._cleanup_failures = cleanup_failures
            except BaseException as cleanup_error:
                self._cleanup_failures = (cleanup_error,)
            raise cancellation from None
        except BaseException as error:
            cleanup_task = asyncio.create_task(self._rollback_async(provider_id))
            caller_cancellation = await _await_protected(cleanup_task)
            try:
                failures, cleanup_cancellation = cleanup_task.result()
            except BaseException as cleanup_error:
                failures = (cleanup_error,)
                cleanup_cancellation = None
            if caller_cancellation is not None:
                raise caller_cancellation from None
            if cleanup_cancellation is not None:
                raise cleanup_cancellation from None
            raise ApplicationBootError(
                provider_id,
                protected_cause=error,
                cleanup_failures=failures,
            ) from None

    def _rollback_sync(
        self,
        failing_provider_id: str,
    ) -> tuple[tuple[BaseException, ...], asyncio.CancelledError | None]:
        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        provider_ids = [failing_provider_id, *reversed(self._booted)]
        for index, provider_id in enumerate(provider_ids):
            provider = self._provider_by_id[provider_id].provider
            if iscoroutinefunction(provider.shutdown):
                self._queue_async_cleanup(provider_ids[index:])
                break
            else:
                try:
                    result = _call_cleanup_bounded(
                        partial(_invoke_provider_shutdown, provider, self.container),
                        self._remaining_cleanup_time(),
                    )
                    if isawaitable(result):
                        self._queue_async_cleanup(
                            provider_ids[index:],
                            first_awaitable=cast(Awaitable[object], result),
                        )
                        break
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
                except BaseException as error:
                    failures.append(error)
            context = self._contexts[provider_id]
            if context.requires_async_disposal():
                self._queue_async_cleanup(
                    provider_ids[index:],
                    first_shutdown_completed=True,
                )
                break
            try:
                for disposal_error in context.dispose():
                    if isinstance(disposal_error, asyncio.CancelledError):
                        cancellation = cancellation or disposal_error
                    else:
                        failures.append(disposal_error)
            except BaseException as disposal_error:
                failures.append(disposal_error)
        self._booted.clear()
        if self._pending_async_cleanup or self._root.requires_async_disposal():
            failures.append(AsyncLifecycleRequired("application rollback requires ashutdown"))
            self._cleanup_failures = tuple(failures)
            self._cleanup_complete = False
            self._state = ApplicationState.FAILED
            return tuple(failures), cancellation
        self._close_root_after_failure(failures)
        self._cleanup_failures = tuple(failures)
        self._cleanup_complete = True
        self._state = ApplicationState.FAILED
        return tuple(failures), cancellation

    async def _rollback_async(
        self,
        failing_provider_id: str,
    ) -> tuple[tuple[BaseException, ...], asyncio.CancelledError | None]:
        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        provider_ids = [failing_provider_id, *reversed(self._booted)]
        for provider_id in provider_ids:
            provider = self._provider_by_id[provider_id].provider
            try:
                await _shutdown_provider(
                    provider,
                    self.container,
                    self._remaining_cleanup_time,
                )
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except BaseException as error:
                failures.append(error)
            try:
                failures.extend(await self._contexts[provider_id].adispose())
            except asyncio.CancelledError as error:
                failures.extend(self._contexts[provider_id].disposal_failures)
                cancellation = cancellation or error
            except BaseException as error:
                failures.append(error)
        self._booted.clear()
        try:
            await self._root.aclose()
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except BaseException as error:
            failures.append(error)
        self._cleanup_failures = tuple(failures)
        self._cleanup_complete = True
        self._state = ApplicationState.FAILED
        return tuple(failures), cancellation

    def _shutdown_booted_sync(
        self,
    ) -> tuple[tuple[BaseException, ...], asyncio.CancelledError | None, bool]:
        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        provider_ids = list(reversed(self._booted))
        for index, provider_id in enumerate(provider_ids):
            provider = self._provider_by_id[provider_id].provider
            try:
                result = _call_cleanup_bounded(
                    partial(_invoke_provider_shutdown, provider, self.container),
                    self._remaining_cleanup_time(),
                )
                if isawaitable(result):
                    self._queue_async_cleanup(
                        provider_ids[index:],
                        first_awaitable=cast(Awaitable[object], result),
                    )
                    break
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except BaseException as error:
                failures.append(error)
            context = self._contexts[provider_id]
            if context.requires_async_disposal():
                self._queue_async_cleanup(
                    provider_ids[index:],
                    first_shutdown_completed=True,
                )
                break
            try:
                for disposal_error in context.dispose():
                    if isinstance(disposal_error, asyncio.CancelledError):
                        cancellation = cancellation or disposal_error
                    else:
                        failures.append(disposal_error)
            except BaseException as disposal_error:
                failures.append(disposal_error)
        self._booted.clear()
        return tuple(failures), cancellation, bool(self._pending_async_cleanup)

    async def _shutdown_booted_async(
        self,
    ) -> tuple[tuple[BaseException, ...], asyncio.CancelledError | None]:
        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        for provider_id in self._pending_async_cleanup:
            if provider_id in self._pending_async_shutdowns:
                try:
                    pending = self._pending_shutdown_awaitables.pop(provider_id, None)
                    if pending is not None:
                        with without_context():
                            await _await_cleanup_bounded(
                                pending,
                                self._remaining_cleanup_time(),
                            )
                    else:
                        await _shutdown_provider(
                            self._provider_by_id[provider_id].provider,
                            self.container,
                            self._remaining_cleanup_time,
                        )
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
                except BaseException as error:
                    failures.append(error)
            if provider_id in self._pending_async_contexts:
                try:
                    failures.extend(await self._contexts[provider_id].adispose())
                except asyncio.CancelledError as error:
                    failures.extend(self._contexts[provider_id].disposal_failures)
                    cancellation = cancellation or error
                except BaseException as error:
                    failures.append(error)
        self._pending_async_cleanup.clear()
        self._pending_async_shutdowns.clear()
        self._pending_async_contexts.clear()
        self._pending_shutdown_awaitables.clear()
        for provider_id in reversed(self._booted):
            provider = self._provider_by_id[provider_id].provider
            try:
                await _shutdown_provider(
                    provider,
                    self.container,
                    self._remaining_cleanup_time,
                )
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except BaseException as error:
                failures.append(error)
            try:
                failures.extend(await self._contexts[provider_id].adispose())
            except asyncio.CancelledError as error:
                failures.extend(self._contexts[provider_id].disposal_failures)
                cancellation = cancellation or error
            except BaseException as error:
                failures.append(error)
        self._booted.clear()
        return tuple(failures), cancellation

    def _close_root_after_failure(
        self,
        failures: list[BaseException] | None = None,
    ) -> None:
        try:
            self._root.close()
        except BaseException as error:
            if failures is not None:
                failures.append(error)

    def _assert_activation_state(self) -> None:
        if self._state not in {ApplicationState.BOOTED, ApplicationState.SERVING}:
            raise InvalidBinding("provider activation requires a booted application")

    def _validate_requested_provider(self, provider_id: str) -> None:
        if provider_id not in self._provider_by_id:
            raise ApplicationGraphError("provider dependency graph is invalid")

    def _activation_order(self, provider_id: str) -> tuple[str, ...]:
        required: set[str] = set()

        def visit(current_id: str) -> None:
            if current_id in required:
                return
            for dependency in self._provider_by_id[current_id].dependencies:
                visit(dependency)
            required.add(current_id)

        visit(provider_id)
        return tuple(item for item in self._provider_order if item in required)

    def _bind_async_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._async_loop is not None and self._async_loop is not loop:
            raise InvalidBinding("application lifecycle is bound to another event loop")
        self._async_loop = loop

    def _claim_provider_resource(self, value: object) -> bool:
        resource_id = id(value)
        existing = self._provider_resources.get(resource_id)
        if existing is not None:
            return False
        if not self.container.claim_external_resource(value):
            return False
        self._provider_resources[resource_id] = value
        return True

    def _release_provider_resource(self, value: object) -> None:
        resource_id = id(value)
        if self._provider_resources.get(resource_id) is value:
            self._provider_resources.pop(resource_id, None)
            self.container.release_external_resource(value)

    def _queue_async_cleanup(
        self,
        provider_ids: Iterable[str],
        *,
        first_shutdown_completed: bool = False,
        first_awaitable: Awaitable[object] | None = None,
    ) -> None:
        queued = tuple(provider_ids)
        for index, provider_id in enumerate(queued):
            if provider_id not in self._pending_async_cleanup:
                self._pending_async_cleanup.append(provider_id)
            if index > 0 or not first_shutdown_completed:
                self._pending_async_shutdowns.add(provider_id)
            self._pending_async_contexts.add(provider_id)
        if first_awaitable is not None and queued:
            self._pending_shutdown_awaitables[queued[0]] = first_awaitable

    def _remaining_cleanup_time(self) -> float:
        if self._cleanup_deadline is None:
            self._cleanup_deadline = monotonic() + self._cleanup_timeout
        return max(0.0, self._cleanup_deadline - monotonic())

    def _begin_sync_operation(self, *, disallow_async_activation: bool = False) -> None:
        with self._lifecycle_gate:
            if self._async_shutdown_claimed:
                raise InvalidBinding("asynchronous application shutdown is in progress")
            if disallow_async_activation and self._async_activation_count:
                raise InvalidBinding("asynchronous provider activation is in progress")
            self._sync_operations += 1
            self._sync_idle.clear()

    def _end_sync_operation(self) -> None:
        with self._lifecycle_gate:
            self._sync_operations -= 1
            if self._sync_operations == 0:
                self._sync_idle.set()

    def _begin_async_activation(self) -> None:
        with self._lifecycle_gate:
            if self._async_shutdown_claimed:
                raise InvalidBinding("asynchronous application shutdown is in progress")
            if self._sync_operations:
                raise InvalidBinding("synchronous application lifecycle is in progress")
            self._async_activation_count += 1

    def _end_async_activation(self) -> None:
        with self._lifecycle_gate:
            self._async_activation_count -= 1

    def _claim_async_shutdown(self) -> None:
        with self._lifecycle_gate:
            self._async_shutdown_claimed = True


async def _await_protected[T](
    task: asyncio.Task[T],
) -> asyncio.CancelledError | None:
    caller_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                caller_cancellation = caller_cancellation or error
                continue
            if task.done():
                break
            raise
        except BaseException:
            break
    return caller_cancellation


async def _await_protected_bounded[T](
    task: asyncio.Task[T],
    timeout: float,
) -> tuple[asyncio.CancelledError | None, bool]:
    caller_cancellation: asyncio.CancelledError | None = None
    deadline = monotonic() + timeout
    while not task.done():
        remaining = max(0.0, deadline - monotonic())
        if remaining == 0:
            return caller_cancellation, task.done()
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                caller_cancellation = caller_cancellation or error
                continue
            if task.done():
                break
            raise
        if not done:
            return caller_cancellation, task.done()
    return caller_cancellation, True


def _reject_awaitable(value: object) -> None:
    if isinstance(value, asyncio.Future):
        if not value.done():
            value.cancel()
        return
    close = getattr(value, "close", None)
    if callable(close):
        close()


def _invoke_provider_boot(
    provider: ServiceProvider,
    container: Container,
    context: ProviderBootContext,
) -> object:
    with without_context():
        return provider.boot(container, context)


async def _shutdown_provider(
    provider: ServiceProvider,
    container: Container,
    remaining_cleanup_time: Callable[[], float],
) -> None:
    if iscoroutinefunction(provider.shutdown):
        with without_context():
            await _await_cleanup_bounded(
                provider.shutdown(container),
                remaining_cleanup_time(),
            )
        return
    with without_context():
        result = await _await_cleanup_bounded(
            asyncio.to_thread(_invoke_provider_shutdown, provider, container),
            remaining_cleanup_time(),
        )
        if isawaitable(result):
            await _await_cleanup_bounded(
                cast(Awaitable[object], result),
                remaining_cleanup_time(),
            )


def _invoke_provider_shutdown(
    provider: ServiceProvider,
    container: Container,
) -> object:
    with without_context():
        return provider.shutdown(container)


def _invoke_without_context(callback: Callable[[], object]) -> object:
    with without_context():
        return callback()


def _call_cleanup_bounded(
    callback: Callable[[], object],
    timeout: float,
) -> object:
    if timeout <= 0:
        raise ShutdownTimeout
    outcome: Future[object] = Future()

    def invoke() -> None:
        try:
            outcome.set_result(_invoke_without_context(callback))
        except BaseException as error:
            outcome.set_exception(error)

    Thread(target=invoke, daemon=True).start()
    try:
        return outcome.result(timeout=timeout)
    except FutureTimeoutError:
        raise ShutdownTimeout from None


async def _await_cleanup_bounded[T](
    awaitable: Awaitable[T],
    timeout: float,
) -> T:
    if timeout <= 0:
        _reject_awaitable(awaitable)
        raise ShutdownTimeout
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.add_done_callback(_consume_cleanup_future)
        task.cancel()
        raise ShutdownTimeout
    return await task


def _consume_cleanup_future[T](future: asyncio.Future[T]) -> None:
    if future.cancelled():
        return
    with suppress(BaseException):
        future.exception()
