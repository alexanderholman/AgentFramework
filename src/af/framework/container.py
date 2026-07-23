"""A small explicit service container: Laravel-inspired, Python-oriented."""

from __future__ import annotations

import asyncio
import builtins
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AbstractContextManager, nullcontext, suppress
from contextvars import (
    Context as ContextVarsContext,
)
from contextvars import (
    ContextVar,
    Token,
    copy_context,
)
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from inspect import isawaitable, iscoroutinefunction
from math import isfinite
from threading import Condition, RLock, Thread, get_ident
from time import monotonic
from types import TracebackType
from typing import Any, Protocol, TypeVar, cast

from af.framework.context import (
    ContextLease,
    ExecutionContext,
    maybe_current_context,
    use_context,
    without_context,
)
from af.framework.errors import (
    AsyncDisposalRequired,
    AsyncResolutionRequired,
    CircularDependency,
    InvalidBinding,
    ScopeDisposalError,
    ScopeDisposed,
    ScopeUnavailable,
    ServiceNotBound,
    ShutdownTimeout,
)

__all__ = (
    "AsyncFactory",
    "Container",
    "Factory",
    "InvocationKind",
    "Qualifier",
    "Resolver",
    "Scope",
    "ServiceKey",
    "ServiceProvider",
)

T = TypeVar("T")
U = TypeVar("U")
Factory = Callable[["Resolver"], object]
AsyncFactory = Callable[["Resolver"], Awaitable[object]]
_UNSET = object()
_BUILTIN_EXCEPTION_TYPES = frozenset(
    value
    for value in vars(builtins).values()
    if isinstance(value, type) and issubclass(value, BaseException)
)


def _instance_factory(_: Resolver) -> object:
    return _UNSET


class Scope(StrEnum):
    SINGLETON = "singleton"
    APPLICATION = "application"
    INVOCATION = "invocation"
    TRANSIENT = "transient"


class InvocationKind(StrEnum):
    REQUEST = "request"
    MESSAGE = "message"
    COMMAND = "command"
    JOB = "job"
    EVENT = "event"
    SCHEDULE = "schedule"
    NOTIFICATION = "notification"
    INTERACTION = "interaction"


@dataclass(frozen=True, slots=True)
class Qualifier:
    name: str

    def __post_init__(self) -> None:
        if (
            type(self.name) is not str
            or not self.name
            or len(self.name) > 200
            or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", self.name)
        ):
            raise ValueError("qualifier must be a bounded non-empty single-line value")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class ServiceKey:
    token: object
    qualifier: Qualifier | None = None

    def __post_init__(self) -> None:
        if self.qualifier is not None and type(self.qualifier) is not Qualifier:
            raise InvalidBinding("service key qualifier must be a Qualifier")
        if type(self.token) is not str:
            return
        if (
            not self.token
            or len(self.token) > 200
            or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", self.token)
        ):
            raise InvalidBinding("string service token must be a bounded single-line value")

    def __hash__(self) -> int:
        token_key = ("string", self.token) if type(self.token) is str else ("id", id(self.token))
        return hash((token_key, self.qualifier))

    def __eq__(self, other: object) -> bool:
        if type(other) is not ServiceKey or self.qualifier != other.qualifier:
            return False
        if type(self.token) is str and type(other.token) is str:
            return self.token == other.token
        return self.token is other.token

    def __repr__(self) -> str:
        qualifier = "'[REDACTED]'" if self.qualifier is not None else "None"
        return f"ServiceKey(token='[REDACTED]', qualifier={qualifier})"


class Resolver(Protocol):
    """Resolution-only capability available to service factories."""

    def resolve(self, token: type[T], *, qualifier: Qualifier | str | None = None) -> T: ...

    def resolve_token(self, token: object, *, qualifier: Qualifier | str | None = None) -> Any: ...

    async def resolve_async(
        self, token: type[T], *, qualifier: Qualifier | str | None = None
    ) -> T: ...

    async def resolve_token_async(
        self, token: object, *, qualifier: Qualifier | str | None = None
    ) -> Any: ...

    def resolve_tagged(self, name: str) -> tuple[object, ...]: ...

    async def resolve_tagged_async(self, name: str) -> tuple[object, ...]: ...


class _ResolverLease:
    """Lease-bound implementation passed only by the container runtime."""

    def __init__(self, container: Container, frame: _ResolutionFrame) -> None:
        self._container: Container | None = container
        self._frame: _ResolutionFrame | None = frame
        self._active = True

    def _current(self) -> Container:
        frame = self._frame
        if not self._active or self._container is None or frame is None or not frame.active:
            raise ScopeDisposed("factory resolver lease has expired")
        current_frame = next(
            (current for current in reversed(_RESOLUTION_STACK.get()) if current.active),
            None,
        )
        if current_frame is not frame:
            raise InvalidBinding("factory resolver requires its active resolution frame")
        return self._container

    def resolve(self, token: type[T], *, qualifier: Qualifier | str | None = None) -> T:
        return self._current().resolve(token, qualifier=qualifier)

    def resolve_token(self, token: object, *, qualifier: Qualifier | str | None = None) -> Any:
        return self._current().resolve_token(token, qualifier=qualifier)

    async def resolve_async(self, token: type[T], *, qualifier: Qualifier | str | None = None) -> T:
        return await self._current().resolve_async(token, qualifier=qualifier)

    async def resolve_token_async(
        self, token: object, *, qualifier: Qualifier | str | None = None
    ) -> Any:
        return await self._current().resolve_token_async(token, qualifier=qualifier)

    def resolve_tagged(self, name: str) -> tuple[object, ...]:
        return self._current().resolve_tagged(name)

    async def resolve_tagged_async(self, name: str) -> tuple[object, ...]:
        return await self._current().resolve_tagged_async(name)

    def application_scope(self) -> Container:
        raise InvalidBinding("factory resolver cannot create lifecycle scopes")

    def invocation_scope(self, *, context: ExecutionContext | None = None) -> Container:
        del context
        raise InvalidBinding("factory resolver cannot create lifecycle scopes")

    def invalidate(self) -> None:
        self._active = False
        self._container = None
        self._frame = None


@dataclass(slots=True)
class _ResolutionFrame:
    key: ServiceKey
    registry: object
    active: bool = True


@dataclass(slots=True)
class _ConstructionFlight:
    owner: Container | None
    cache_key: object | None
    service_key: ServiceKey | None
    node: object | None
    active: bool = True

    def invalidate(self) -> None:
        self.active = False
        self.owner = None
        self.cache_key = None
        self.service_key = None
        self.node = None


_RESOLUTION_STACK: ContextVar[tuple[_ResolutionFrame, ...]] = ContextVar(
    "af_container_resolution_stack", default=()
)
_CURRENT_FLIGHT: ContextVar[_ConstructionFlight | None] = ContextVar(
    "af_container_construction_flight", default=None
)
_SYNC_FACTORY_ORIGIN_LOOP: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "af_container_sync_factory_origin_loop", default=None
)
_DISPOSAL_STACK: ContextVar[tuple[Container, ...]] = ContextVar(
    "af_container_disposal_stack", default=()
)
_CURRENT_INVOCATION_SCOPE: ContextVar[Container | None] = ContextVar(
    "af_container_invocation_scope", default=None
)


@dataclass(slots=True)
class _Binding:
    factory: Factory | AsyncFactory
    scope: Scope
    instance: object = _UNSET
    owned: bool = True
    is_async: bool = False
    is_instance: bool = False


@dataclass(frozen=True, slots=True)
class _ResourceRecord:
    value: object
    close: Callable[[], object] | None
    aclose: Callable[[], Awaitable[object]] | None
    loop: asyncio.AbstractEventLoop | None = None


class _ResolutionGraph:
    def __init__(self) -> None:
        self.lock = RLock()
        self.services: dict[object, ServiceKey] = {}
        self.edges: dict[object, dict[object, int]] = {}


class _ShutdownCoordinator:
    def __init__(self) -> None:
        self.lock = RLock()
        self.deadline: float | None = None
        self.participants = 0

    def enter(self, timeout: float) -> tuple[float, bool]:
        candidate = monotonic() + timeout
        with self.lock:
            new_operation = self.participants == 0 or self.deadline is None
            if new_operation:
                self.deadline = candidate
            else:
                assert self.deadline is not None
                self.deadline = min(self.deadline, candidate)
            self.participants += 1
            resolved_deadline = self.deadline
            assert resolved_deadline is not None
            return resolved_deadline, new_operation

    def leave(self) -> None:
        with self.lock:
            self.participants -= 1
            if self.participants == 0:
                self.deadline = None


class Container:
    """Resolve explicit bindings; deliberately does not inspect constructors."""

    def __init__(
        self,
        *,
        _bindings: dict[ServiceKey, _Binding] | None = None,
        _aliases: dict[ServiceKey, ServiceKey] | None = None,
        _contextual: dict[tuple[ServiceKey, ServiceKey], _Binding] | None = None,
        _tags: dict[str, tuple[ServiceKey, ...]] | None = None,
        _parent: Container | None = None,
        _scope: Scope | None = None,
        _invocation_kind: InvocationKind | None = None,
        _execution_context: ExecutionContext | None = None,
        _registry_identity: object | None = None,
        _registry_lock: RLock | None = None,
        _shutdown_lock: RLock | None = None,
        _resolution_graph: _ResolutionGraph | None = None,
        _shutdown_coordinator: _ShutdownCoordinator | None = None,
        shutdown_timeout: float = 30.0,
    ) -> None:
        if (
            type(shutdown_timeout) not in {int, float}
            or not isfinite(shutdown_timeout)
            or shutdown_timeout <= 0
        ):
            raise ValueError("shutdown timeout must be a positive finite number")
        self._bindings = {} if _bindings is None else _bindings
        self._aliases = {} if _aliases is None else _aliases
        self._contextual = {} if _contextual is None else _contextual
        self._tags = {} if _tags is None else _tags
        self._parent = _parent
        self._scope = _scope
        self._invocation_kind = _invocation_kind
        self._execution_context = _execution_context
        self._registry_identity = object() if _registry_identity is None else _registry_identity
        self._registry_lock = RLock() if _registry_lock is None else _registry_lock
        self._shutdown_lock = RLock() if _shutdown_lock is None else _shutdown_lock
        self._resolution_graph = (
            _ResolutionGraph() if _resolution_graph is None else _resolution_graph
        )
        self._shutdown_coordinator = (
            _ShutdownCoordinator() if _shutdown_coordinator is None else _shutdown_coordinator
        )
        self._shutdown_timeout = float(shutdown_timeout)
        self._shutdown_deadline: float | None = None
        self._context_binding: AbstractContextManager[object] | None = None
        self._context_lease: ContextLease | None = None
        self._invocation_token: Token[Container | None] | None = None
        self._entry_task: asyncio.Task[Any] | None = None
        self._entry_thread_id: int | None = None
        self._instances: dict[object, object] = {}
        self._children: list[Container] = []
        self._resources: list[_ResourceRecord] = []
        self._owned_resource_ids: set[int] = set()
        self._state = "active"
        self._condition = Condition(RLock())
        self._inflight: set[object] = set()
        self._sync_flights: dict[object, Future[object]] = {}
        self._async_inflight: dict[object, asyncio.Task[object]] = {}
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._frozen = False
        self._active_factories = 0
        self._active_constructions = 0
        self._close_future: Future[None] = Future()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._sync_cleanup_active = False
        self._cleanup_snapshot_taken = False
        self._late_async_resources: list[_ResourceRecord] = []
        self._late_owned_resources: dict[int, object] = {}
        self._late_cleanup_task: asyncio.Task[None] | None = None
        self._disposal_failures: tuple[BaseException, ...] = ()
        self._disposal_cancelled = False
        self._flight_services = self._resolution_graph.services
        self._wait_edges = self._resolution_graph.edges
        self._flight_nodes: dict[object, object] = {}
        self._entered = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._internal_authority_tasks: dict[asyncio.Task[Any], int] = {}
        self._async_constructions: set[asyncio.Task[Any]] = set()
        self._active_resolutions = 0
        self._resolution_tasks: dict[asyncio.Task[Any], int] = {}

    def application_scope(self) -> Container:
        self._assert_active()
        self._assert_scope_creation_allowed()
        if self._parent is not None:
            raise ScopeUnavailable("application scopes must be created from the root")
        self._root().freeze()
        child = Container(
            _bindings=self._bindings,
            _aliases=self._aliases,
            _contextual=self._contextual,
            _tags=self._tags,
            _parent=self,
            _scope=Scope.APPLICATION,
            _registry_identity=self._registry_identity,
            _registry_lock=self._registry_lock,
            _shutdown_lock=self._shutdown_lock,
            _resolution_graph=self._resolution_graph,
            _shutdown_coordinator=self._shutdown_coordinator,
            shutdown_timeout=self._shutdown_timeout,
        )
        with self._condition:
            if self._state != "active":
                raise ScopeDisposed("container scope is disposed")
            self._children.append(child)
        return child

    def invocation_scope(
        self,
        *,
        context: ExecutionContext | None = None,
        kind: InvocationKind = InvocationKind.INTERACTION,
    ) -> Container:
        self._assert_active()
        self._assert_scope_creation_allowed()
        if context is not None and not isinstance(context, ExecutionContext):
            raise InvalidBinding("invocation context must be an ExecutionContext")
        if not isinstance(kind, InvocationKind):
            raise InvalidBinding("invocation kind must be an InvocationKind")
        if self._scope is not Scope.APPLICATION:
            raise ScopeUnavailable("invocation scope requires an application scope")
        child = Container(
            _bindings=self._bindings,
            _aliases=self._aliases,
            _contextual=self._contextual,
            _tags=self._tags,
            _parent=self,
            _scope=Scope.INVOCATION,
            _invocation_kind=kind,
            _execution_context=context,
            _registry_identity=self._registry_identity,
            _registry_lock=self._registry_lock,
            _shutdown_lock=self._shutdown_lock,
            _resolution_graph=self._resolution_graph,
            _shutdown_coordinator=self._shutdown_coordinator,
            shutdown_timeout=self._shutdown_timeout,
        )
        with self._condition:
            if self._state != "active":
                raise ScopeDisposed("container scope is disposed")
            self._children.append(child)
        return child

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
        self._assert_registry_owner()
        self._validate_factory_scope(factory, scope, expect_async=False)
        key = self._key(token, qualifier)
        with self._registry_lock, self._condition:
            self._assert_registry_mutable_locked()
            if (key in self._bindings or key in self._aliases) and not replace:
                raise InvalidBinding("binding already exists")
            self._aliases.pop(key, None)
            self._bindings[key] = _Binding(factory=factory, scope=scope, owned=owned)

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
        self._assert_registry_owner()
        self._validate_factory_scope(factory, scope, expect_async=True)
        key = self._key(token, qualifier)
        with self._registry_lock, self._condition:
            self._assert_registry_mutable_locked()
            if (key in self._bindings or key in self._aliases) and not replace:
                raise InvalidBinding("binding already exists")
            self._aliases.pop(key, None)
            self._bindings[key] = _Binding(factory=factory, scope=scope, owned=owned, is_async=True)

    def instance(
        self,
        token: object,
        value: object,
        *,
        qualifier: Qualifier | str | None = None,
        owned: bool = False,
        replace: bool = False,
    ) -> None:
        self._assert_registry_owner()
        key = self._key(token, qualifier)
        binding = _Binding(
            factory=_instance_factory,
            scope=Scope.SINGLETON,
            instance=value,
            owned=owned,
            is_instance=True,
        )
        with self._registry_lock, self._condition:
            self._assert_registry_mutable_locked()
            if (key in self._bindings or key in self._aliases) and not replace:
                raise InvalidBinding("binding already exists")
        with without_context():
            resource = self._discover_resource(
                value,
                binding,
                origin_loop=self._current_running_loop(),
            )
        with self._registry_lock, self._condition:
            self._assert_registry_mutable_locked()
            if (key in self._bindings or key in self._aliases) and not replace:
                raise InvalidBinding("binding already exists")
            self._record_resource_locked(resource)
            self._aliases.pop(key, None)
            self._bindings[key] = binding

    def alias(
        self,
        alias_token: object,
        target_token: object,
        *,
        alias_qualifier: Qualifier | str | None = None,
        target_qualifier: Qualifier | str | None = None,
        replace: bool = False,
    ) -> None:
        self._assert_registry_owner()
        alias_key = self._key(alias_token, alias_qualifier)
        target_key = self._key(target_token, target_qualifier)
        with self._registry_lock, self._condition:
            self._assert_registry_mutable_locked()
            if (alias_key in self._aliases or alias_key in self._bindings) and not replace:
                raise InvalidBinding("alias already exists")
            self._bindings.pop(alias_key, None)
            self._aliases[alias_key] = target_key

    def bind_contextual(
        self,
        consumer: object,
        token: object,
        factory: Factory,
        *,
        consumer_qualifier: Qualifier | str | None = None,
        qualifier: Qualifier | str | None = None,
        scope: Scope = Scope.TRANSIENT,
        owned: bool = True,
        replace: bool = False,
    ) -> None:
        self._assert_registry_owner()
        self._validate_factory_scope(factory, scope, expect_async=False)
        contextual_key = (
            self._canonical_key(self._key(consumer, consumer_qualifier)),
            self._canonical_key(self._key(token, qualifier)),
        )
        with self._registry_lock, self._condition:
            self._assert_registry_mutable_locked()
            if contextual_key in self._contextual and not replace:
                raise InvalidBinding("contextual binding already exists")
            self._contextual[contextual_key] = _Binding(factory=factory, scope=scope, owned=owned)

    def bind_contextual_async(
        self,
        consumer: object,
        token: object,
        factory: AsyncFactory,
        *,
        consumer_qualifier: Qualifier | str | None = None,
        qualifier: Qualifier | str | None = None,
        scope: Scope = Scope.TRANSIENT,
        owned: bool = True,
        replace: bool = False,
    ) -> None:
        self._assert_registry_owner()
        self._validate_factory_scope(factory, scope, expect_async=True)
        contextual_key = (
            self._canonical_key(self._key(consumer, consumer_qualifier)),
            self._canonical_key(self._key(token, qualifier)),
        )
        with self._registry_lock, self._condition:
            self._assert_registry_mutable_locked()
            if contextual_key in self._contextual and not replace:
                raise InvalidBinding("contextual binding already exists")
            self._contextual[contextual_key] = _Binding(
                factory=factory, scope=scope, owned=owned, is_async=True
            )

    def key(self, token: object, *, qualifier: Qualifier | str | None = None) -> ServiceKey:
        return self._key(token, qualifier)

    def tag(self, name: str, *services: ServiceKey | object) -> None:
        self._assert_registry_owner()
        name = self._validated_tag_name(name)
        keys = tuple(
            service if type(service) is ServiceKey else self._key(service, None)
            for service in services
        )
        with self._registry_lock, self._condition:
            self._assert_registry_mutable_locked()
            self._tags[name] = keys

    def resolve_tagged(self, name: str) -> tuple[object, ...]:
        self._assert_active()
        name = self._validated_tag_name(name)
        with self._registry_lock:
            keys = self._tags.get(name)
        if keys is None:
            raise ServiceNotBound(name, tag=True)
        return tuple(self.resolve_token(key) for key in keys)

    async def resolve_tagged_async(self, name: str) -> tuple[object, ...]:
        self._assert_active()
        name = self._validated_tag_name(name)
        with self._registry_lock:
            keys = self._tags.get(name)
        if keys is None:
            raise ServiceNotBound(name, tag=True)
        values: list[object] = []
        for key in keys:
            values.append(await self.resolve_token_async(key))
        return tuple(values)

    def create_task(
        self,
        coroutine: Coroutine[Any, Any, U],
        *,
        name: str | None = None,
    ) -> asyncio.Task[U]:
        self._assert_active()
        if (
            self._scope is not Scope.INVOCATION
            or not self._entered
            or not self._has_invocation_authority()
        ):
            coroutine.close()
            raise ScopeUnavailable("tasks require the active invocation scope")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coroutine.close()
            raise ScopeUnavailable("tasks require an active event loop") from None
        start = asyncio.Event()
        wrapped = self._await_owned_task_start(start, coroutine)
        try:
            task = loop.create_task(wrapped, name=name)
        except BaseException:
            wrapped.close()
            coroutine.close()
            raise
        with self._condition:
            if (
                not self._hierarchy_is_active()
                or not self._entered
                or not self._has_invocation_authority()
            ):
                task.cancel()
                coroutine.close()
                raise ScopeDisposed("container scope is disposed")
            loop = task.get_loop()
            if self._async_loop is not None and self._async_loop is not loop:
                task.cancel()
                coroutine.close()
                raise InvalidBinding("scope is bound to another event loop")
            self._async_loop = loop
            self._tasks.add(task)
        start.set()
        return task

    @staticmethod
    async def _await_owned_task_start(start: asyncio.Event, coroutine: Coroutine[Any, Any, U]) -> U:
        await start.wait()
        return await coroutine

    def with_test_overrides(self, overrides: dict[object, object]) -> Container:
        self._assert_active()
        if self._parent is not None:
            raise InvalidBinding("test overrides must fork a root container")
        with self._registry_lock, self._condition:
            override_entries = tuple(
                (self._canonical_key(self._key(token, None)), value)
                for token, value in overrides.items()
            )
            for key, _ in override_entries:
                if key not in self._bindings:
                    raise InvalidBinding("test override target is not bound")
            override_keys = {key for key, _ in override_entries}
            bindings = {
                key: _Binding(
                    binding.factory,
                    binding.scope,
                    binding.instance if binding.is_instance else _UNSET,
                    binding.owned and not binding.is_instance,
                    binding.is_async,
                    binding.is_instance,
                )
                for key, binding in self._bindings.items()
            }
            contextual = {
                key: _Binding(
                    binding.factory,
                    binding.scope,
                    binding.instance if binding.is_instance else _UNSET,
                    binding.owned,
                    binding.is_async,
                    binding.is_instance,
                )
                for key, binding in self._contextual.items()
                if key[1] not in override_keys
            }
            aliases = dict(self._aliases)
            tags = dict(self._tags)
        fork = Container(
            _bindings=bindings,
            _aliases=aliases,
            _contextual=contextual,
            _tags=tags,
            shutdown_timeout=self._shutdown_timeout,
        )
        for key, value in override_entries:
            fork.instance(key, value, replace=True)
        return fork

    def freeze(self) -> None:
        self._assert_registry_mutation_outside_factory()
        if self._parent is not None:
            raise InvalidBinding("only the root container can freeze declarations")
        with self._registry_lock, self._condition:
            if self._frozen:
                return
            self._assert_registry_mutable_locked()
            for alias in self._aliases:
                target = self._canonical_key(alias)
                if target not in self._bindings:
                    raise InvalidBinding("alias target is not bound")
            for keys in self._tags.values():
                for key in keys:
                    if self._canonical_key(key) not in self._bindings:
                        raise InvalidBinding("tag target is not bound")
            canonical_contextual: dict[tuple[ServiceKey, ServiceKey], _Binding] = {}
            for (consumer, dependency), binding in self._contextual.items():
                canonical_key = (
                    self._canonical_key(consumer),
                    self._canonical_key(dependency),
                )
                if canonical_key in canonical_contextual:
                    raise InvalidBinding("contextual aliases create a duplicate binding")
                canonical_contextual[canonical_key] = binding
            self._contextual.clear()
            self._contextual.update(canonical_contextual)
            self._frozen = True

    def resolve(self, token: type[T], *, qualifier: Qualifier | str | None = None) -> T:
        self._begin_resolution()
        try:
            key = self._canonical_key(self._key(token, qualifier))
            binding = self._binding_for(key, token)
            if binding.is_async:
                raise AsyncResolutionRequired("service requires resolve_async")
            return cast(T, self._resolve_binding(key, binding))
        finally:
            self._end_resolution()

    def resolve_token(self, token: object, *, qualifier: Qualifier | str | None = None) -> Any:
        """Resolve a Protocol or other non-class token at composition boundaries."""
        self._begin_resolution()
        try:
            key = self._canonical_key(self._key(token, qualifier))
            binding = self._binding_for(key, token)
            if binding.is_async:
                raise AsyncResolutionRequired("service requires resolve_async")
            return self._resolve_binding(key, binding)
        finally:
            self._end_resolution()

    async def resolve_async(self, token: type[T], *, qualifier: Qualifier | str | None = None) -> T:
        task = asyncio.current_task()
        if task is None:
            raise InvalidBinding("async resolution requires an asyncio task")
        self._begin_resolution(task)
        try:
            key = self._canonical_key(self._key(token, qualifier))
            binding = self._binding_for(key, token)
            if not binding.is_async:
                return cast(T, await self._resolve_sync_binding_async(key, binding))
            return cast(T, await self._resolve_async_binding(key, binding))
        finally:
            self._end_resolution(task)

    async def resolve_token_async(
        self, token: object, *, qualifier: Qualifier | str | None = None
    ) -> Any:
        task = asyncio.current_task()
        if task is None:
            raise InvalidBinding("async resolution requires an asyncio task")
        self._begin_resolution(task)
        try:
            key = self._canonical_key(self._key(token, qualifier))
            binding = self._binding_for(key, token)
            if not binding.is_async:
                return await self._resolve_sync_binding_async(key, binding)
            return await self._resolve_async_binding(key, binding)
        finally:
            self._end_resolution(task)

    def _resolve_binding(self, key: ServiceKey, binding: _Binding) -> object:
        self._raise_if_cycle(key)
        cache_key = (key, id(binding))
        if binding.scope is Scope.TRANSIENT:
            return self._invoke_factory(key, binding, self, self)
        if binding.scope is Scope.SINGLETON:
            root = self._root()
            value = self._resolve_cached(
                cache_key,
                key,
                root,
                lambda: binding.instance,
                lambda value: setattr(binding, "instance", value),
                lambda: self._invoke_factory(key, binding, root, root, flight_key=cache_key),
            )
            self._assert_active()
            return value
        owner = self._nearest_scope(binding.scope)
        if owner is None:
            raise ScopeUnavailable(f"{binding.scope.value} binding requires an active scope")
        value = self._resolve_cached(
            cache_key,
            key,
            owner,
            lambda: owner._instances.get(cache_key, _UNSET),
            lambda value: owner._instances.__setitem__(cache_key, value),
            lambda: self._invoke_factory(key, binding, owner, owner, flight_key=cache_key),
        )
        self._assert_active()
        return value

    async def _resolve_sync_binding_async(self, key: ServiceKey, binding: _Binding) -> object:
        origin_token = _SYNC_FACTORY_ORIGIN_LOOP.set(asyncio.get_running_loop())
        try:
            self._raise_if_cycle(key)
            cache_key = (key, id(binding))
            if binding.scope is Scope.TRANSIENT:
                return self._invoke_factory(key, binding, self, self)
            if binding.scope is Scope.SINGLETON:
                owner = self._root()
                get_value = partial(getattr, binding, "instance")
                set_value = partial(setattr, binding, "instance")
            else:
                scoped_owner = self._nearest_scope(binding.scope)
                if scoped_owner is None:
                    raise ScopeUnavailable(
                        f"{binding.scope.value} binding requires an active scope"
                    )
                owner = scoped_owner
                get_value = partial(owner._instances.get, cache_key, _UNSET)
                set_value = partial(owner._instances.__setitem__, cache_key)
            value = await self._resolve_sync_cached_async(
                cache_key,
                key,
                owner,
                get_value,
                set_value,
                lambda: self._invoke_factory(key, binding, owner, owner, flight_key=cache_key),
            )
            self._assert_active()
            return value
        finally:
            _SYNC_FACTORY_ORIGIN_LOOP.reset(origin_token)

    async def _resolve_sync_cached_async(
        self,
        cache_key: object,
        service_key: ServiceKey,
        owner: Container,
        get_value: Callable[[], object],
        set_value: Callable[[object], None],
        construct: Callable[[], object],
    ) -> object:
        loop = asyncio.get_running_loop()
        with owner._condition:
            if not owner._hierarchy_is_active():
                raise ScopeDisposed("container scope is disposed")
            owner._assert_owner_loop_locked(loop)
            value = get_value()
            if value is not _UNSET:
                return value
            flight = owner._sync_flights.get(cache_key)
            if flight is None:
                flight = Future()
                owner._sync_flights[cache_key] = flight
                owner._inflight.add(cache_key)
                flight_node = object()
                owner._flight_nodes[cache_key] = flight_node
                owner._register_flight_node(flight_node, service_key)
                construct_here = True
                waiting_from = None
                waiting_for = None
            else:
                construct_here = False
                current_flight = _CURRENT_FLIGHT.get()
                if (
                    current_flight is not None
                    and current_flight.active
                    and current_flight.node is not None
                ):
                    waiting_for = owner._flight_nodes[cache_key]
                    cycle = owner._add_wait_edge(current_flight.node, waiting_for)
                    if cycle is not None:
                        raise CircularDependency(cycle)
                    waiting_from = current_flight.node
                else:
                    waiting_from = None
                    waiting_for = None
        if not construct_here:
            try:
                value = await asyncio.shield(asyncio.wrap_future(flight))
                with owner._condition:
                    owner._assert_owner_loop_locked(loop)
                return value
            finally:
                if waiting_from is not None and waiting_for is not None:
                    owner._remove_wait_edge(waiting_from, waiting_for)
        try:
            value = construct()
        except BaseException as error:
            with owner._condition:
                owner._inflight.discard(cache_key)
                owner._sync_flights.pop(cache_key, None)
                owner._retire_flight_key_locked(cache_key)
                flight.set_exception(error)
                owner._condition.notify_all()
            raise
        publication_error: BaseException | None = None
        with owner._condition:
            try:
                if not owner._hierarchy_is_active():
                    raise ScopeDisposed("container scope was disposed during resolution")
                set_value(value)
            except BaseException as error:
                publication_error = error
            owner._inflight.discard(cache_key)
            owner._sync_flights.pop(cache_key, None)
            owner._retire_flight_key_locked(cache_key)
            owner._release_construction_locked()
            if publication_error is None:
                flight.set_result(value)
            else:
                flight.set_exception(publication_error)
            owner._condition.notify_all()
        if publication_error is not None:
            raise publication_error
        return value

    async def _resolve_async_binding(self, key: ServiceKey, binding: _Binding) -> object:
        self._raise_if_cycle(key)
        cache_key = (key, id(binding))
        if binding.scope is Scope.TRANSIENT:
            return await self._invoke_async_factory(key, binding, self, self)
        owner = (
            self._root() if binding.scope is Scope.SINGLETON else self._nearest_scope(binding.scope)
        )
        if owner is None:
            raise ScopeUnavailable(f"{binding.scope.value} binding requires an active scope")
        get_value: Callable[[], object]
        if binding.scope is Scope.SINGLETON:
            get_value = partial(getattr, binding, "instance")
        else:
            get_value = partial(owner._instances.get, cache_key, _UNSET)
        with owner._condition:
            if not owner._hierarchy_is_active():
                raise ScopeDisposed("container scope is disposed")
            loop = asyncio.get_running_loop()
            if owner._async_loop is not None and owner._async_loop is not loop:
                raise InvalidBinding("asynchronous scope is bound to another event loop")
            value = get_value()
            if value is not _UNSET:
                return value
            owner._async_loop = loop
            task = owner._async_inflight.get(cache_key)
            if task is None:
                flight_node = object()
                owner._flight_nodes[cache_key] = flight_node
                owner._register_flight_node(flight_node, key)
                start = asyncio.Event()
                task = loop.create_task(
                    self._await_async_flight_start(start, key, binding, owner, cache_key, get_value)
                )
                self._grant_internal_authority(task)
                owner._async_inflight[cache_key] = task
                task.add_done_callback(partial(owner._retire_async_flight, cache_key))
                task.add_done_callback(self._revoke_internal_authority)
                start.set()
            current_flight = _CURRENT_FLIGHT.get()
            if (
                current_flight is not None
                and current_flight.active
                and current_flight.node is not None
            ):
                waiting_for = owner._flight_nodes[cache_key]
                cycle = owner._add_wait_edge(current_flight.node, waiting_for)
                if cycle is not None:
                    raise CircularDependency(cycle)
                waiting_from = current_flight.node
            else:
                waiting_from = None
                waiting_for = None
        try:
            value = await asyncio.shield(task)
        finally:
            if waiting_from is not None:
                owner._remove_wait_edge(waiting_from, waiting_for)
        self._assert_active()
        return value

    async def _await_async_flight_start(
        self,
        start: asyncio.Event,
        key: ServiceKey,
        binding: _Binding,
        owner: Container,
        cache_key: object,
        get_value: Callable[[], object],
    ) -> object:
        await start.wait()
        return await self._construct_and_cache_async(key, binding, owner, cache_key, get_value)

    async def _construct_and_cache_async(
        self,
        key: ServiceKey,
        binding: _Binding,
        owner: Container,
        cache_key: object,
        get_value: Callable[[], object],
    ) -> object:
        try:
            value = await self._invoke_async_factory(
                key, binding, owner, owner, flight_key=cache_key
            )
        except BaseException:
            with owner._condition:
                owner._async_inflight.pop(cache_key, None)
                owner._retire_flight_key_locked(cache_key)
                owner._release_async_loop_if_idle_locked()
                owner._condition.notify_all()
            owner._release_handoff_parent_if_idle()
            raise
        publication_error: BaseException | None = None
        with owner._condition:
            try:
                if not owner._hierarchy_is_active():
                    raise ScopeDisposed("container scope was disposed during resolution")
                if get_value() is _UNSET:
                    if binding.scope is Scope.SINGLETON:
                        binding.instance = value
                    else:
                        owner._instances[cache_key] = value
            except BaseException as error:
                publication_error = error
            owner._async_inflight.pop(cache_key, None)
            owner._retire_flight_key_locked(cache_key)
            owner._release_construction_locked()
            owner._release_async_loop_if_idle_locked()
            owner._condition.notify_all()
        owner._release_handoff_parent_if_idle()
        if publication_error is not None:
            raise publication_error
        return value

    def _resolve_cached(
        self,
        cache_key: object,
        service_key: ServiceKey,
        owner: Container,
        get_value: Callable[[], object],
        set_value: Callable[[object], None],
        construct: Callable[[], object],
    ) -> object:
        caller_loop = self._current_running_loop()
        with owner._condition:
            if not owner._hierarchy_is_active():
                raise ScopeDisposed("container scope is disposed")
            owner._assert_owner_loop_locked(caller_loop)
            value = get_value()
            if value is not _UNSET:
                return value
            flight = owner._sync_flights.get(cache_key)
            if flight is None:
                flight = Future()
                owner._sync_flights[cache_key] = flight
                owner._inflight.add(cache_key)
                flight_node = object()
                owner._flight_nodes[cache_key] = flight_node
                owner._register_flight_node(flight_node, service_key)
                construct_here = True
                waiting_from = None
            else:
                construct_here = False
                current_flight = _CURRENT_FLIGHT.get()
                if (
                    current_flight is not None
                    and current_flight.active
                    and current_flight.node is not None
                ):
                    waiting_for = owner._flight_nodes[cache_key]
                    cycle = owner._add_wait_edge(current_flight.node, waiting_for)
                    if cycle is not None:
                        raise CircularDependency(cycle)
                    waiting_from = current_flight.node
                else:
                    waiting_from = None
                    waiting_for = None
        if not construct_here:
            try:
                value = flight.result()
                with owner._condition:
                    owner._assert_owner_loop_locked(caller_loop)
                return value
            finally:
                if waiting_from is not None:
                    owner._remove_wait_edge(waiting_from, waiting_for)
        try:
            value = construct()
        except BaseException as error:
            with owner._condition:
                owner._inflight.discard(cache_key)
                owner._sync_flights.pop(cache_key, None)
                owner._retire_flight_key_locked(cache_key)
                flight.set_exception(error)
                owner._condition.notify_all()
            raise
        publication_error: BaseException | None = None
        with owner._condition:
            try:
                if not owner._hierarchy_is_active():
                    raise ScopeDisposed("container scope was disposed during resolution")
                set_value(value)
            except BaseException as error:
                publication_error = error
            owner._inflight.discard(cache_key)
            owner._sync_flights.pop(cache_key, None)
            owner._retire_flight_key_locked(cache_key)
            owner._release_construction_locked()
            if publication_error is None:
                flight.set_result(value)
            else:
                flight.set_exception(publication_error)
            owner._condition.notify_all()
        if publication_error is not None:
            raise publication_error
        return value

    def _raise_if_cycle(self, key: ServiceKey) -> None:
        active_keys = [
            frame.key
            for frame in _RESOLUTION_STACK.get()
            if frame.active and frame.registry is self._registry_identity
        ]
        if key in active_keys:
            cycle_start = active_keys.index(key)
            raise CircularDependency((*active_keys[cycle_start:], key))

    def _binding_for(self, key: ServiceKey, requested_token: object) -> _Binding:
        with self._registry_lock:
            frames = _RESOLUTION_STACK.get()
            frame = next((current for current in reversed(frames) if current.active), None)
            if frame is not None and frame.registry is self._registry_identity:
                contextual = self._contextual.get((frame.key, key))
                if contextual is not None:
                    return contextual
            binding = self._bindings.get(key)
        if binding is None:
            raise ServiceNotBound(requested_token)
        return binding

    def _invoke_factory(
        self,
        key: ServiceKey,
        binding: _Binding,
        resolver: Container,
        owner: Container,
        *,
        flight_key: object | None = None,
    ) -> object:
        stack = _RESOLUTION_STACK.get()
        active_keys = [
            frame.key
            for frame in stack
            if frame.active and frame.registry is self._registry_identity
        ]
        if key in active_keys:
            cycle_start = active_keys.index(key)
            raise CircularDependency((*active_keys[cycle_start:], key))
        owner._begin_construction()
        frame = _ResolutionFrame(key, self._registry_identity)
        token = _RESOLUTION_STACK.set((*stack, frame))
        flight = (
            _ConstructionFlight(owner, flight_key, key, owner._flight_nodes[flight_key])
            if flight_key is not None
            else None
        )
        flight_token = _CURRENT_FLIGHT.set(flight) if flight is not None else None
        factory_resolver = _ResolverLease(resolver, frame)
        registry_owner = self._enter_registry_factory()
        try:
            mask = (
                nullcontext()
                if self._can_use_ambient_context(binding, resolver)
                else without_context()
            )
            with mask:
                try:
                    value = binding.factory(factory_resolver)
                    if isawaitable(value):
                        self._reject_awaitable(value)
                        raise AsyncResolutionRequired("sync factory returned an awaitable")
                except BaseException:
                    owner._abandon_construction()
                    raise
                owner._complete_construction(
                    value,
                    binding,
                    release=flight_key is None,
                    origin_loop=(_SYNC_FACTORY_ORIGIN_LOOP.get() or self._current_running_loop()),
                )
                return value
        finally:
            self._leave_registry_factory(registry_owner)
            factory_resolver.invalidate()
            frame.active = False
            if flight is not None:
                flight.invalidate()
            if flight_token is not None:
                _CURRENT_FLIGHT.reset(flight_token)
            _RESOLUTION_STACK.reset(token)

    async def _invoke_async_factory(
        self,
        key: ServiceKey,
        binding: _Binding,
        resolver: Container,
        owner: Container,
        *,
        flight_key: object | None = None,
    ) -> object:
        stack = _RESOLUTION_STACK.get()
        active_keys = [
            frame.key
            for frame in stack
            if frame.active and frame.registry is self._registry_identity
        ]
        if key in active_keys:
            cycle_start = active_keys.index(key)
            raise CircularDependency((*active_keys[cycle_start:], key))
        current_task = asyncio.current_task()
        if current_task is None:
            raise InvalidBinding("async factory resolution requires an asyncio task")
        owner._begin_async_construction(current_task)
        frame = _ResolutionFrame(key, self._registry_identity)
        token = _RESOLUTION_STACK.set((*stack, frame))
        flight = (
            _ConstructionFlight(owner, flight_key, key, owner._flight_nodes[flight_key])
            if flight_key is not None
            else None
        )
        flight_token = _CURRENT_FLIGHT.set(flight) if flight is not None else None
        factory_resolver = _ResolverLease(resolver, frame)
        registry_owner = self._enter_registry_factory()
        try:
            factory = cast(AsyncFactory, binding.factory)
            mask = (
                nullcontext()
                if self._can_use_ambient_context(binding, resolver)
                else without_context()
            )
            with mask:
                try:
                    result = factory(factory_resolver)
                    if not isawaitable(result):
                        raise InvalidBinding("async factory must return an awaitable")
                    value = await result
                except BaseException:
                    owner._abandon_construction()
                    raise
                owner._complete_construction(
                    value,
                    binding,
                    release=flight_key is None,
                    origin_loop=current_task.get_loop(),
                )
                return value
        finally:
            self._leave_registry_factory(registry_owner)
            with owner._condition:
                owner._async_constructions.discard(current_task)
                owner._release_async_loop_if_idle_locked()
                owner._condition.notify_all()
            factory_resolver.invalidate()
            frame.active = False
            if flight is not None:
                flight.invalidate()
            if flight_token is not None:
                _CURRENT_FLIGHT.reset(flight_token)
            _RESOLUTION_STACK.reset(token)

    def _add_wait_edge(
        self, waiting_from: object, waiting_for: object
    ) -> tuple[ServiceKey, ...] | None:
        graph = self._resolution_graph
        with graph.lock:
            targets = graph.edges.setdefault(waiting_from, {})
            targets[waiting_for] = targets.get(waiting_for, 0) + 1

            def path_to_origin(current: object, path: list[object]) -> list[object] | None:
                if current == waiting_from:
                    return [*path, current]
                if current in path:
                    return None
                for next_key in graph.edges.get(current, {}):
                    found = path_to_origin(next_key, [*path, current])
                    if found is not None:
                        return found
                return None

            cycle_path = path_to_origin(waiting_for, [waiting_from])
            if cycle_path is None:
                return None
            self._remove_wait_edge_locked(graph, waiting_from, waiting_for)
            return tuple(graph.services[item] for item in cycle_path)

    def _remove_wait_edge(self, waiting_from: object, waiting_for: object) -> None:
        graph = self._resolution_graph
        with graph.lock:
            self._remove_wait_edge_locked(graph, waiting_from, waiting_for)

    @staticmethod
    def _remove_wait_edge_locked(
        graph: _ResolutionGraph, waiting_from: object, waiting_for: object
    ) -> None:
        targets = graph.edges.get(waiting_from)
        if targets is None:
            return
        count = targets.get(waiting_for)
        if count is None:
            return
        if count > 1:
            targets[waiting_for] = count - 1
            return
        targets.pop(waiting_for)
        if not targets:
            graph.edges.pop(waiting_from, None)

    def _register_flight_node(self, node: object, service_key: ServiceKey) -> None:
        graph = self._resolution_graph
        with graph.lock:
            graph.services[node] = service_key

    def _retire_flight_key_locked(self, cache_key: object) -> None:
        node = self._flight_nodes.pop(cache_key, None)
        if node is None:
            return
        graph = self._resolution_graph
        with graph.lock:
            graph.services.pop(node, None)
            graph.edges.pop(node, None)
            for source, targets in tuple(graph.edges.items()):
                targets.pop(node, None)
                if not targets:
                    graph.edges.pop(source, None)

    def _retire_async_flight(self, cache_key: object, task: asyncio.Task[object]) -> None:
        try:
            if not task.cancelled():
                task.exception()
        except BaseException:
            pass
        with self._condition:
            if self._async_inflight.get(cache_key) is not task:
                return
            self._async_inflight.pop(cache_key, None)
            self._retire_flight_key_locked(cache_key)
            self._release_async_loop_if_idle_locked()
            self._condition.notify_all()

    def _release_async_loop_if_idle_locked(self) -> None:
        if (
            not self._async_inflight
            and not self._async_constructions
            and not self._tasks
            and not self._resolution_tasks
            and not any(resource.loop is not None for resource in self._resources)
        ):
            self._async_loop = None

    def _begin_construction(self) -> None:
        with self._condition:
            if not self._hierarchy_is_active():
                raise ScopeDisposed("container scope is disposed")
            self._active_constructions += 1

    def _begin_resolution(self, task: asyncio.Task[Any] | None = None) -> None:
        with self._condition:
            if not self._hierarchy_is_active():
                raise ScopeDisposed("container scope is disposed")
            if self._scope is Scope.INVOCATION and not self._has_invocation_authority():
                raise ScopeUnavailable("invocation scope must be entered as the active invocation")
            self._active_resolutions += 1
            if task is not None:
                loop = task.get_loop()
                if self._async_loop is not None and self._async_loop is not loop:
                    self._active_resolutions -= 1
                    raise InvalidBinding("asynchronous scope is bound to another event loop")
                self._async_loop = loop
                self._resolution_tasks[task] = self._resolution_tasks.get(task, 0) + 1

    def _end_resolution(self, task: asyncio.Task[Any] | None = None) -> None:
        with self._condition:
            self._active_resolutions -= 1
            if task is not None:
                count = self._resolution_tasks.get(task, 0)
                if count <= 1:
                    self._resolution_tasks.pop(task, None)
                else:
                    self._resolution_tasks[task] = count - 1
            self._release_async_loop_if_idle_locked()
            self._condition.notify_all()
        self._release_handoff_parent_if_idle()

    @staticmethod
    def _can_use_ambient_context(binding: _Binding, resolver: Container) -> bool:
        return (
            binding.scope in {Scope.INVOCATION, Scope.TRANSIENT}
            and resolver._scope is Scope.INVOCATION
            and resolver._entered
            and resolver._execution_context is not None
            and maybe_current_context() is resolver._execution_context
        )

    def _begin_async_construction(self, task: asyncio.Task[Any]) -> None:
        with self._condition:
            if not self._hierarchy_is_active():
                raise ScopeDisposed("container scope is disposed")
            loop = task.get_loop()
            if self._async_loop is not None and self._async_loop is not loop:
                raise InvalidBinding("asynchronous scope is bound to another event loop")
            self._async_loop = loop
            self._active_constructions += 1
            self._async_constructions.add(task)

    def _abandon_construction(self) -> None:
        with self._condition:
            self._active_constructions -= 1
            self._condition.notify_all()
        self._release_handoff_parent_if_idle()

    def _complete_construction(
        self,
        value: object,
        binding: _Binding,
        *,
        release: bool,
        origin_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        try:
            resource = self._discover_resource(value, binding, origin_loop=origin_loop)
        except BaseException:
            self._abandon_construction()
            raise
        with self._condition:
            try:
                publishable = self._hierarchy_is_active()
                retained_for_cleanup = publishable or self._hierarchy_accepts_cleanup_resource()
                if retained_for_cleanup:
                    self._record_resource_locked(resource)
                elif not publishable:
                    self._dispose_unpublished_resource(resource)
            except BaseException:
                self._active_constructions -= 1
                self._condition.notify_all()
                raise
            if release or not publishable:
                self._release_construction_locked()
        if not publishable:
            self._release_handoff_parent_if_idle()
            raise ScopeDisposed("container scope was disposed during resolution")

    def _dispose_unpublished_resource(self, resource: _ResourceRecord | None) -> None:
        if resource is None:
            return
        if resource.close is None and resource.aclose is None:
            return
        self._record_late_async_resource(resource)

    def _record_late_async_resource(self, resource: _ResourceRecord) -> None:
        with self._condition:
            resource_id = id(resource.value)
            if self._late_owned_resources.get(resource_id) is resource.value:
                return
            self._late_owned_resources[resource_id] = resource.value
            self._late_async_resources.append(resource)

    @staticmethod
    def _discover_resource(
        value: object,
        binding: _Binding,
        *,
        origin_loop: asyncio.AbstractEventLoop | None = None,
    ) -> _ResourceRecord | None:
        if not binding.owned:
            return None
        invalid_contract = False
        try:
            close_candidate = getattr(value, "close", None)
            aclose_candidate = getattr(value, "aclose", None)
        except BaseException:
            invalid_contract = True
            close_candidate = None
            aclose_candidate = None
        if invalid_contract:
            raise InvalidBinding("resource disposal contract is invalid")
        if callable(close_candidate) and iscoroutinefunction(close_candidate):
            raise InvalidBinding("asynchronous disposal must use aclose")
        close = (
            cast(Callable[[], object] | None, close_candidate)
            if callable(close_candidate)
            else None
        )
        aclose = (
            cast(Callable[[], Awaitable[object]] | None, aclose_candidate)
            if callable(aclose_candidate)
            else None
        )
        if close is None and aclose is None:
            return None
        return _ResourceRecord(
            value,
            close,
            aclose,
            origin_loop if aclose is not None else None,
        )

    def _record_resource_locked(self, resource: _ResourceRecord | None) -> None:
        if resource is None or id(resource.value) in self._owned_resource_ids:
            return
        if (
            resource.loop is not None
            and self._async_loop is not None
            and self._async_loop is not resource.loop
        ):
            raise InvalidBinding("asynchronous resource belongs to another event loop")
        self._owned_resource_ids.add(id(resource.value))
        self._resources.append(resource)
        if resource.loop is not None:
            self._async_loop = resource.loop

    @staticmethod
    def _call_sync_disposer(disposer: Callable[[], object]) -> None:
        with without_context():
            result = disposer()
            if not isawaitable(result):
                return
            Container._reject_awaitable(result)
            raise InvalidBinding("synchronous disposer returned an awaitable; use aclose")

    @staticmethod
    def _reject_awaitable(value: object) -> None:
        if isinstance(value, asyncio.Future):
            loop = value.get_loop()

            if value.done():
                Container._consume_async_future(value)
                return

            if loop.is_closed():
                with suppress(RuntimeError):
                    value.cancel()
                return

            def cancel_and_consume() -> None:
                value.add_done_callback(Container._consume_async_future)
                if not value.done():
                    value.cancel()

            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if loop is current_loop:
                cancel_and_consume()
            elif loop.is_running():
                loop.call_soon_threadsafe(cancel_and_consume)
            else:
                with suppress(RuntimeError):
                    value.cancel()
            return
        close = getattr(value, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _consume_async_future(future: asyncio.Future[Any]) -> None:
        try:
            if not future.cancelled():
                future.exception()
        except BaseException:
            pass

    @staticmethod
    async def _call_async_disposer(
        disposer: Callable[[], Awaitable[object]],
    ) -> None:
        with without_context():
            result = disposer()
            if not isawaitable(result):
                raise InvalidBinding("asynchronous disposer must return an awaitable")
            await result

    def _release_construction_locked(self) -> None:
        self._active_constructions -= 1
        self._condition.notify_all()

    def _assert_active(self) -> None:
        if not self._hierarchy_is_active():
            raise ScopeDisposed("container scope is disposed")

    def _hierarchy_is_active(self) -> bool:
        current: Container | None = self
        while current is not None:
            if current._state != "active":
                return False
            current = current._parent
        return True

    def _hierarchy_accepts_cleanup_resource(self) -> bool:
        current: Container | None = self
        while current is not None:
            if (
                current._state == "disposed"
                or current._close_future.done()
                or current._cleanup_snapshot_taken
            ):
                return False
            current = current._parent
        return True

    def _assert_registry_owner(self) -> None:
        self._assert_registry_mutation_outside_factory()
        self._assert_active()
        if self._parent is not None:
            raise InvalidBinding("bindings can only be declared on the root container")
        if self._frozen:
            raise InvalidBinding("container declarations are frozen")

    def _assert_registry_mutable_locked(self) -> None:
        if self._state != "active":
            raise ScopeDisposed("container scope is disposed")
        if self._frozen:
            raise InvalidBinding("container declarations are frozen")
        if self._root()._active_factories > 0 or any(
            frame.active and frame.registry is self._registry_identity
            for frame in _RESOLUTION_STACK.get()
        ):
            raise InvalidBinding("factory cannot mutate container declarations")

    def _assert_registry_mutation_outside_factory(self) -> None:
        root = self._root()
        with self._registry_lock:
            factory_active = root._active_factories > 0
        if factory_active or any(
            frame.active and frame.registry is self._registry_identity
            for frame in _RESOLUTION_STACK.get()
        ):
            raise InvalidBinding("factory cannot mutate container declarations")

    def _enter_registry_factory(self) -> Container:
        root = self._root()
        with root._registry_lock:
            root._active_factories += 1
        return root

    def _leave_registry_factory(self, root: Container) -> None:
        with root._registry_lock:
            root._active_factories -= 1

    def _assert_scope_creation_allowed(self) -> None:
        if any(
            frame.active and frame.registry is self._registry_identity
            for frame in _RESOLUTION_STACK.get()
        ):
            raise InvalidBinding("factory cannot create lifecycle scopes")

    def close(self) -> None:
        deadline, new_operation = self._shutdown_coordinator.enter(self._shutdown_timeout)
        self._adopt_shutdown_deadline_hierarchy(deadline, reset=new_operation)
        acquired = False
        try:
            self._assert_shutdown_outside_factory()
            if any(scope._shutdown_lock is self._shutdown_lock for scope in _DISPOSAL_STACK.get()):
                raise InvalidBinding("scope cannot close from its disposal hierarchy")
            acquired = self._shutdown_lock.acquire(timeout=self._remaining_shutdown_time())
            if not acquired:
                raise ScopeDisposalError((ShutdownTimeout(),))
            self._close_with_shutdown_lock()
        finally:
            if acquired:
                self._shutdown_lock.release()
            self._shutdown_coordinator.leave()

    def _close_with_shutdown_lock(self) -> None:
        self._assert_shutdown_outside_factory()
        if any(scope._shutdown_lock is self._shutdown_lock for scope in _DISPOSAL_STACK.get()):
            raise InvalidBinding("scope cannot close from its disposal hierarchy")
        current_task = self._current_asyncio_task()
        if current_task is not None and self._owns_task_in_tree(current_task):
            raise InvalidBinding("scope-owned task cannot close its owning scope")
        self._assert_no_external_entry_owner_in_tree(current_task, get_ident())
        with self._condition:
            if self._state == "disposed" or self._state == "disposing":
                close_future = self._close_future
                cleanup_task = self._cleanup_task
                initiator = False
            else:
                if self._requires_async_disposal() or self._async_inflight:
                    raise AsyncDisposalRequired("scope owns asynchronous resources; use aclose")
                self._state = "disposing"
                if self._shutdown_deadline is None:
                    self._shutdown_deadline = monotonic() + self._shutdown_timeout
                self._revoke_context()
                self._sync_cleanup_active = True
                close_future = self._close_future
                cleanup_task = None
                initiator = True
        if not initiator:
            if cleanup_task is None and not close_future.done() and self._requires_async_disposal():
                raise AsyncDisposalRequired("scope owns asynchronous resources; use aclose")
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if (
                cleanup_task is not None
                and not close_future.done()
                and cleanup_task.get_loop() is running_loop
            ):
                raise AsyncDisposalRequired("asynchronous cleanup is running on this event loop")
            close_future.result()
            self._raise_disposal_outcome()
            return
        if self._prepare_sync_disposal():
            self._release_sync_cleanup_preparation()
            raise AsyncDisposalRequired("scope owns asynchronous resources; use aclose")
        self._dispose_sync_prepared()

    def _prepare_sync_disposal(self) -> bool:
        with self._condition:
            if self._state == "disposed":
                return False
            if self._state == "active":
                self._state = "disposing"
                if self._shutdown_deadline is None:
                    self._shutdown_deadline = monotonic() + self._shutdown_timeout
                self._revoke_context()
                self._sync_cleanup_active = True
            children = tuple(self._children)
        async_required = False
        for child in children:
            child._adopt_shutdown_deadline(self._shutdown_deadline)
            async_required = child._prepare_sync_disposal() or async_required
        with self._condition:
            if self._requires_async_disposal_local():
                return True
            while self._active_constructions or self._active_resolutions or self._inflight:
                remaining = self._remaining_shutdown_time()
                if remaining <= 0:
                    self._disposal_failures = (
                        *self._disposal_failures,
                        ShutdownTimeout(),
                    )
                    break
                self._condition.wait(remaining)
            return async_required or self._requires_async_disposal_local()

    def _release_sync_cleanup_preparation(self) -> None:
        with self._condition:
            children = tuple(self._children)
            if self._cleanup_task is None:
                self._sync_cleanup_active = False
                self._condition.notify_all()
        for child in children:
            child._release_sync_cleanup_preparation()

    def _dispose_sync_prepared(self) -> None:
        failures = list(self._disposal_failures)
        cleanup_cancellation: asyncio.CancelledError | None = None
        with self._condition:
            children = tuple(reversed(self._children))
            self._cleanup_snapshot_taken = True
            resources = tuple(reversed(self._resources))
        for child in children:
            try:
                child._dispose_sync_prepared()
            except asyncio.CancelledError as error:
                with child._condition:
                    failures.extend(child._disposal_failures)
                cleanup_cancellation = cleanup_cancellation or error
            except ScopeDisposalError as error:
                failures.extend(error.failures)
            except BaseException as error:
                failures.append(error)
        disposal_token = _DISPOSAL_STACK.set((*_DISPOSAL_STACK.get(), self))
        try:
            for resource in resources:
                if self._remaining_shutdown_time() <= 0:
                    if not any(isinstance(error, ShutdownTimeout) for error in failures):
                        failures.append(ShutdownTimeout())
                    break
                if resource.close is not None:
                    try:
                        self._call_sync_disposer_bounded(resource.close)
                    except asyncio.CancelledError as error:
                        cleanup_cancellation = cleanup_cancellation or error
                    except BaseException as error:
                        failures.append(error)
        finally:
            _DISPOSAL_STACK.reset(disposal_token)
        self._disposal_failures = tuple(failures)
        failure: BaseException | None = cleanup_cancellation
        if failure is None and failures:
            failure = ScopeDisposalError(tuple(failures))
        self._finish_disposal(failure)
        self._raise_disposal_outcome()

    async def aclose(self) -> None:
        deadline, new_operation = self._shutdown_coordinator.enter(self._shutdown_timeout)
        self._adopt_shutdown_deadline_hierarchy(deadline, reset=new_operation)
        try:
            await self._aclose_coordinated()
        finally:
            self._shutdown_coordinator.leave()

    async def _aclose_from_parent(self) -> None:
        await self._aclose_coordinated(entry_authorized=True)

    async def _aclose_coordinated(self, *, entry_authorized: bool = False) -> None:
        self._assert_shutdown_outside_factory()
        if any(scope._shutdown_lock is self._shutdown_lock for scope in _DISPOSAL_STACK.get()):
            raise InvalidBinding("scope cannot close from its disposal hierarchy")
        current_task = asyncio.current_task()
        if current_task is not None and self._owns_task_in_tree(current_task):
            raise InvalidBinding("scope-owned task cannot close its owning scope")
        if not entry_authorized:
            self._assert_no_external_entry_owner_in_tree(current_task, get_ident())
        loop = asyncio.get_running_loop()
        await self._drain_late_async_resources(loop)
        lock_cancellation: asyncio.CancelledError | None = None
        lock_acquired = False
        if not entry_authorized:
            lock_cancellation = await self._acquire_shutdown_lock_async()
            lock_acquired = True
        try:
            if not entry_authorized:
                self._assert_no_external_entry_owner_in_tree(current_task, get_ident())
            self._assert_async_cleanup_loop(loop)
            with self._condition:
                wait_for_sync_cleanup = entry_authorized and self._sync_cleanup_active
                if (
                    self._state != "disposed"
                    and self._cleanup_task is None
                    and not wait_for_sync_cleanup
                ):
                    self._state = "disposing"
                    if self._shutdown_deadline is None:
                        self._shutdown_deadline = monotonic() + self._shutdown_timeout
                    self._revoke_context()
                    cleanup_start = asyncio.Event()
                    cleanup_task = loop.create_task(
                        self._await_cleanup_start(cleanup_start),
                        context=ContextVarsContext(),
                    )
                    self._cleanup_task = cleanup_task
                    cleanup_task.add_done_callback(self._settle_async_cleanup)
                    cleanup_start.set()
                close_future = self._close_future
        except BaseException:
            if lock_cancellation is not None:
                raise lock_cancellation from None
            raise
        finally:
            if lock_acquired:
                self._shutdown_lock.release()
        if wait_for_sync_cleanup:
            handoff_cancellation: asyncio.CancelledError | None = None
            while not close_future.done():
                with self._condition:
                    sync_cleanup_active = self._sync_cleanup_active
                if not sync_cleanup_active:
                    break
                if self._remaining_shutdown_time() <= 0:
                    if handoff_cancellation is not None:
                        raise handoff_cancellation from None
                    raise ScopeDisposalError((ShutdownTimeout(),))
                try:
                    await asyncio.sleep(0.001)
                except asyncio.CancelledError as error:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        handoff_cancellation = handoff_cancellation or error
                        continue
                    break
            if close_future.done():
                close_future.result()
                if handoff_cancellation is not None:
                    raise handoff_cancellation from None
                self._raise_disposal_outcome()
                return
            handoff_error: BaseException | None = None
            try:
                await self._aclose_coordinated(entry_authorized=True)
            except asyncio.CancelledError as error:
                handoff_cancellation = handoff_cancellation or error
            except BaseException as error:
                handoff_error = error
            if handoff_cancellation is not None:
                raise handoff_cancellation from None
            if handoff_error is not None:
                raise handoff_error
            return
        caller_cancellation = lock_cancellation
        if not close_future.done():
            while not close_future.done():
                try:
                    await asyncio.sleep(0.001)
                except asyncio.CancelledError as error:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        caller_cancellation = error
                        continue
                    break
                except BaseException:
                    break
        close_future.result()
        if caller_cancellation is not None:
            raise caller_cancellation from None
        self._raise_disposal_outcome()

    async def _acquire_shutdown_lock_async(self) -> asyncio.CancelledError | None:
        caller_cancellation: asyncio.CancelledError | None = None
        while not self._shutdown_lock.acquire(blocking=False):
            remaining = self._remaining_shutdown_time()
            if remaining <= 0:
                if caller_cancellation is not None:
                    raise caller_cancellation from None
                raise ScopeDisposalError((ShutdownTimeout(),))
            try:
                await asyncio.sleep(min(0.001, remaining))
            except asyncio.CancelledError as error:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    caller_cancellation = caller_cancellation or error
                    continue
                raise
        return caller_cancellation

    async def _acquire_entry_lock_async(self) -> None:
        while not self._shutdown_lock.acquire(blocking=False):
            await asyncio.sleep(0.001)

    async def _await_cleanup_start(self, start: asyncio.Event) -> None:
        await start.wait()
        await self._aclose_impl()

    async def _aclose_impl(self) -> None:
        failures = list(self._disposal_failures)
        cleanup_cancellation: asyncio.CancelledError | None = None
        tasks = tuple(
            self._tasks
            | self._async_constructions
            | set(self._async_inflight.values())
            | set(self._resolution_tasks)
        )
        pending_tasks: list[asyncio.Task[Any]] = []
        for task in tasks:
            if task.done():
                if task.cancelled():
                    continue
                try:
                    task_error = task.exception()
                except BaseException as error:
                    task_error = error
                if task_error is not None:
                    failures.append(task_error)
                continue
            task.cancel()
            pending_tasks.append(task)
        if pending_tasks:
            done_tasks, unfinished_tasks = await asyncio.wait(
                pending_tasks, timeout=self._remaining_shutdown_time()
            )
            task_results: list[object] = []
            for task in done_tasks:
                if task.cancelled():
                    continue
                try:
                    task_error = task.exception()
                except BaseException as error:
                    task_error = error
                if task_error is not None:
                    task_results.append(task_error)
            failures.extend(
                result
                for result in task_results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            )
            if unfinished_tasks:
                for task in unfinished_tasks:
                    task.add_done_callback(self._consume_async_future)
                failures.append(ShutdownTimeout())
        constructions_finished = await self._wait_for_constructions_async()
        if not constructions_finished:
            failures.append(ShutdownTimeout())
        with self._condition:
            children = tuple(reversed(self._children))
            self._cleanup_snapshot_taken = True
            resources = tuple(reversed(self._resources))
        for child in children:
            child._adopt_shutdown_deadline(self._shutdown_deadline)
            child_cleanup = asyncio.create_task(child._aclose_from_parent())
            done_children, _ = await asyncio.wait(
                {child_cleanup}, timeout=self._remaining_shutdown_time()
            )
            if not done_children:
                child_cleanup.add_done_callback(self._consume_async_future)
                failures.append(ShutdownTimeout())
                continue
            try:
                await child_cleanup
            except asyncio.CancelledError as error:
                with child._condition:
                    failures.extend(child._disposal_failures)
                cleanup_cancellation = cleanup_cancellation or error
            except ScopeDisposalError as error:
                failures.extend(error.failures)
            except BaseException as error:
                failures.append(error)
        disposal_token = _DISPOSAL_STACK.set((*_DISPOSAL_STACK.get(), self))
        try:
            for resource in resources:
                if self._remaining_shutdown_time() <= 0:
                    if not any(isinstance(error, ShutdownTimeout) for error in failures):
                        failures.append(ShutdownTimeout())
                    break
                if resource.aclose is not None:
                    disposer_task = asyncio.create_task(self._call_async_disposer(resource.aclose))
                    done_disposers, _ = await asyncio.wait(
                        {disposer_task}, timeout=self._remaining_shutdown_time()
                    )
                    if not done_disposers:
                        disposer_task.add_done_callback(self._consume_async_future)
                        disposer_task.cancel()
                        failures.append(ShutdownTimeout())
                        continue
                    try:
                        await disposer_task
                    except asyncio.CancelledError as error:
                        cleanup_cancellation = cleanup_cancellation or error
                    except BaseException as error:
                        failures.append(error)
                    continue
                if resource.close is not None:
                    try:
                        disposer_task = asyncio.create_task(
                            asyncio.to_thread(self._call_sync_disposer, resource.close)
                        )
                        done_disposers, _ = await asyncio.wait(
                            {disposer_task}, timeout=self._remaining_shutdown_time()
                        )
                        if not done_disposers:
                            disposer_task.add_done_callback(self._consume_async_future)
                            disposer_task.cancel()
                            failures.append(ShutdownTimeout())
                            continue
                        await disposer_task
                    except asyncio.CancelledError as error:
                        cleanup_cancellation = cleanup_cancellation or error
                    except BaseException as error:
                        failures.append(error)
            with self._condition:
                late_failure_start = len(self._disposal_failures)
            await self._drain_late_async_resources(asyncio.get_running_loop())
            with self._condition:
                failures.extend(self._disposal_failures[late_failure_start:])
        finally:
            _DISPOSAL_STACK.reset(disposal_token)
        self._disposal_failures = tuple(failures)
        failure: BaseException | None = cleanup_cancellation
        if failure is None and failures:
            failure = ScopeDisposalError(tuple(failures))
        self._finish_disposal(failure)

    def _settle_async_cleanup(self, task: asyncio.Task[None]) -> None:
        try:
            error = task.exception()
        except BaseException as caught:
            error = caught
        with self._condition:
            unsettled = self._state != "disposed" and not self._close_future.done()
        if not unsettled:
            return
        if error is None:
            error = InvalidBinding("asynchronous cleanup ended without finalizing scope")
        failure = error if isinstance(error, ScopeDisposalError) else ScopeDisposalError((error,))
        self._finish_disposal(failure)

    async def _wait_for_constructions_async(self) -> bool:
        while True:
            with self._condition:
                if not (
                    self._active_constructions
                    or self._active_resolutions
                    or self._inflight
                    or self._async_inflight
                ):
                    return True
                remaining = self._remaining_shutdown_time()
                if remaining <= 0:
                    return False
            await asyncio.sleep(min(0.001, remaining))

    def _remaining_shutdown_time(self) -> float:
        deadline = self._shutdown_deadline
        if deadline is None:
            return self._shutdown_timeout
        return max(0.0, deadline - monotonic())

    def _adopt_shutdown_deadline(self, deadline: float | None, *, reset: bool = False) -> None:
        if deadline is None:
            return
        with self._condition:
            if reset or self._shutdown_deadline is None or deadline < self._shutdown_deadline:
                self._shutdown_deadline = deadline

    def _adopt_shutdown_deadline_hierarchy(
        self, deadline: float | None, *, reset: bool = False
    ) -> None:
        self._adopt_shutdown_deadline(deadline, reset=reset)
        with self._condition:
            children = tuple(self._children)
        for child in children:
            child._adopt_shutdown_deadline_hierarchy(deadline, reset=reset)

    async def _drain_late_async_resources(self, loop: asyncio.AbstractEventLoop) -> None:
        caller_cancellation: asyncio.CancelledError | None = None
        with self._condition:
            children = tuple(reversed(self._children))
        for child in children:
            with child._condition:
                failure_start = len(child._disposal_failures)
            try:
                await child._drain_late_async_resources(loop)
            except asyncio.CancelledError as cancellation:
                caller_cancellation = caller_cancellation or cancellation
            except BaseException as child_error:
                self._record_late_cleanup_failure(child_error)
            with child._condition:
                child_failures = child._disposal_failures[failure_start:]
                child_cancelled = child._disposal_cancelled
            for child_failure in child_failures:
                self._record_late_cleanup_failure(child_failure)
            if child_cancelled:
                with self._condition:
                    self._disposal_cancelled = True
                if caller_cancellation is None:
                    caller_cancellation = asyncio.CancelledError()
        while True:
            with self._condition:
                wait_for_constructions = bool(
                    (self._late_async_resources or self._state == "disposed")
                    and (
                        self._active_constructions
                        or self._active_resolutions
                        or self._inflight
                        or self._async_inflight
                    )
                )
            if wait_for_constructions:
                waiter = loop.create_task(
                    self._wait_for_constructions_async(),
                    context=ContextVarsContext(),
                )
                while not waiter.done():
                    try:
                        await asyncio.shield(waiter)
                    except asyncio.CancelledError as error:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling():
                            caller_cancellation = caller_cancellation or error
                            continue
                        break
                if not waiter.result():
                    self._record_late_cleanup_failure(ShutdownTimeout())
                    if caller_cancellation is not None:
                        raise caller_cancellation from None
                    return
            with self._condition:
                task = self._late_cleanup_task
                if task is not None and task.done():
                    self._late_cleanup_task = None
                    task = None
                if task is not None and task.get_loop() is not loop:
                    raise InvalidBinding("asynchronous scope is bound to another event loop")
                if task is None:
                    resources = tuple(reversed(self._late_async_resources))
                    for resource in resources:
                        if resource.loop is not None and resource.loop is not loop:
                            raise InvalidBinding(
                                "asynchronous scope is bound to another event loop"
                            )
                    if not resources:
                        self._release_handoff_parent_if_idle()
                        if caller_cancellation is not None:
                            raise caller_cancellation from None
                        return
                    self._late_async_resources.clear()
                    start = asyncio.Event()
                    task = loop.create_task(
                        self._await_late_cleanup_start(start, resources),
                        context=ContextVarsContext(),
                    )
                    self._late_cleanup_task = task
                    task.add_done_callback(self._settle_late_cleanup)
                    start.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as error:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        caller_cancellation = caller_cancellation or error
                        continue
                    break
            if task.done():
                if task.cancelled():
                    caller_cancellation = caller_cancellation or asyncio.CancelledError()
                    continue
                task.result()

    async def _await_late_cleanup_start(
        self,
        start: asyncio.Event,
        resources: tuple[_ResourceRecord, ...],
    ) -> None:
        await start.wait()
        await self._dispose_late_async_resources(resources)

    async def _dispose_late_async_resources(self, resources: tuple[_ResourceRecord, ...]) -> None:
        disposal_token = _DISPOSAL_STACK.set((*_DISPOSAL_STACK.get(), self))
        cleanup_cancellation: asyncio.CancelledError | None = None
        try:
            for resource in resources:
                remaining = self._remaining_shutdown_time()
                if remaining <= 0:
                    self._record_late_cleanup_failure(ShutdownTimeout())
                    continue
                if resource.aclose is not None:
                    disposer_task = asyncio.create_task(self._call_async_disposer(resource.aclose))
                elif resource.close is not None:
                    disposer_task = asyncio.create_task(
                        asyncio.to_thread(self._call_sync_disposer, resource.close)
                    )
                else:
                    continue
                done, _ = await asyncio.wait({disposer_task}, timeout=remaining)
                if not done:
                    disposer_task.add_done_callback(self._consume_async_future)
                    disposer_task.cancel()
                    self._record_late_cleanup_failure(ShutdownTimeout())
                    continue
                try:
                    await disposer_task
                except asyncio.CancelledError as error:
                    cleanup_cancellation = cleanup_cancellation or error
                except BaseException as error:
                    self._record_late_cleanup_failure(error)
        finally:
            _DISPOSAL_STACK.reset(disposal_token)
            with self._condition:
                for resource in resources:
                    resource_id = id(resource.value)
                    if self._late_owned_resources.get(resource_id) is resource.value:
                        self._late_owned_resources.pop(resource_id, None)
        if cleanup_cancellation is not None:
            with self._condition:
                self._disposal_cancelled = True
            raise cleanup_cancellation from None

    def _record_late_cleanup_failure(self, error: BaseException) -> None:
        with self._condition:
            self._disposal_failures = (
                *self._disposal_failures,
                self._sanitized_failure(error),
            )

    def _settle_late_cleanup(self, task: asyncio.Task[None]) -> None:
        self._consume_async_future(task)
        with self._condition:
            if self._late_cleanup_task is task:
                self._late_cleanup_task = None
        self._release_handoff_parent_if_idle()

    def _call_sync_disposer_bounded(self, disposer: Callable[[], object]) -> None:
        if self._remaining_shutdown_time() <= 0:
            raise ShutdownTimeout
        outcome: Future[None] = Future()
        context = copy_context()

        def dispose() -> None:
            try:
                self._call_sync_disposer(disposer)
            except BaseException as error:
                outcome.set_exception(error)
            else:
                outcome.set_result(None)

        worker = Thread(target=context.run, args=(dispose,), daemon=True)
        worker.start()
        try:
            outcome.result(timeout=self._remaining_shutdown_time())
        except FutureTimeoutError:
            if outcome.done():
                outcome.result()
                return
            raise ShutdownTimeout from None

    def _requires_async_disposal(self) -> bool:
        with self._condition:
            local_required = self._requires_async_disposal_local()
            children = tuple(self._children)
        return local_required or any(
            child._state != "disposed" and child._requires_async_disposal() for child in children
        )

    def _requires_async_disposal_local(self) -> bool:
        return (
            self._cleanup_task is not None
            or bool(self._async_inflight)
            or bool(self._async_constructions)
            or bool(self._resolution_tasks)
            or bool(self._tasks)
            or any(
                (resource.aclose is not None and resource.close is None)
                for resource in self._resources
            )
        )

    def _owns_task_in_tree(self, task: asyncio.Task[Any]) -> bool:
        with self._condition:
            if task in self._tasks or task in self._resolution_tasks:
                return True
            children = tuple(self._children)
        return any(child._owns_task_in_tree(task) for child in children)

    def _assert_no_external_entry_owner_in_tree(
        self,
        task: asyncio.Task[Any] | None,
        thread_id: int,
    ) -> None:
        with self._condition:
            entry_task = self._entry_task
            external_entry = self._entered and (
                (entry_task is not None and not entry_task.done() and entry_task is not task)
                or (entry_task is None and self._entry_thread_id != thread_id)
            )
            children = tuple(self._children)
        if external_entry:
            raise InvalidBinding("entered invocation must exit before external shutdown")
        for child in children:
            child._assert_no_external_entry_owner_in_tree(task, thread_id)

    def _assert_async_cleanup_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._condition:
            if (
                self._async_loop is not None
                and self._async_loop is not loop
                and (
                    any(not task.done() for task in self._tasks)
                    or any(not task.done() for task in self._resolution_tasks)
                    or self._async_constructions
                    or self._async_inflight
                    or any(resource.loop is not None for resource in self._resources)
                )
            ):
                raise InvalidBinding("asynchronous scope is bound to another event loop")
            children = tuple(self._children)
        for child in children:
            child._assert_async_cleanup_loop(loop)

    def _assert_shutdown_outside_factory(self) -> None:
        if any(
            frame.active and frame.registry is self._registry_identity
            for frame in _RESOLUTION_STACK.get()
        ):
            raise InvalidBinding("factory cannot close its container hierarchy")

    @staticmethod
    def _current_asyncio_task() -> asyncio.Task[Any] | None:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    @staticmethod
    def _current_running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _assert_owner_loop_locked(self, loop: asyncio.AbstractEventLoop | None) -> None:
        if self._async_loop is not None and self._async_loop is not loop:
            raise InvalidBinding("asynchronous scope is bound to another event loop")

    def _has_invocation_authority(self) -> bool:
        if (
            self._scope is not Scope.INVOCATION
            or not self._entered
            or _CURRENT_INVOCATION_SCOPE.get() is not self
            or not self._hierarchy_is_active()
        ):
            return False
        task = self._current_asyncio_task()
        if task is not None:
            return (
                task is self._entry_task
                or task in self._tasks
                or self._internal_authority_tasks.get(task, 0) > 0
            )
        return self._entry_task is None and self._entry_thread_id == get_ident()

    def _grant_internal_authority(self, task: asyncio.Task[Any]) -> None:
        if self._scope is Scope.INVOCATION and self._entered:
            with self._condition:
                self._internal_authority_tasks[task] = (
                    self._internal_authority_tasks.get(task, 0) + 1
                )

    def _revoke_internal_authority(self, task: asyncio.Task[Any]) -> None:
        with self._condition:
            count = self._internal_authority_tasks.get(task, 0)
            if count <= 1:
                self._internal_authority_tasks.pop(task, None)
            else:
                self._internal_authority_tasks[task] = count - 1

    def _revoke_context(self) -> None:
        if self._context_lease is not None:
            self._context_lease.revoke()

    def _finish_disposal(self, failure: BaseException | None) -> None:
        parent = self._parent
        is_root = parent is None
        with self._condition:
            if isinstance(failure, asyncio.CancelledError):
                self._disposal_cancelled = True
            elif isinstance(failure, ScopeDisposalError):
                self._disposal_failures = failure.failures
            elif failure is not None:
                self._disposal_failures = (*self._disposal_failures, failure)
            self._disposal_failures = tuple(
                self._sanitized_failure(error) for error in self._disposal_failures
            )
            retain_for_handoff = bool(
                self._children
                or self._active_constructions
                or self._active_resolutions
                or self._inflight
                or self._async_inflight
                or self._late_async_resources
                or self._late_cleanup_task is not None
            )
            self._state = "disposed"
            self._instances.clear()
            self._resources.clear()
            self._owned_resource_ids.clear()
            self._execution_context = None
            self._revoke_context()
            self._async_inflight.clear()
            self._sync_flights.clear()
            self._async_loop = None
            self._cleanup_task = None
            self._sync_cleanup_active = False
            self._tasks.clear()
            self._async_constructions.clear()
            self._resolution_tasks.clear()
            self._internal_authority_tasks.clear()
            if is_root:
                for binding in (*self._bindings.values(), *self._contextual.values()):
                    binding.instance = _UNSET
                self._bindings = {}
                self._aliases = {}
                self._contextual = {}
                self._tags = {}
            elif not retain_for_handoff:
                self._parent = None
            if not is_root:
                self._bindings = {}
                self._aliases = {}
                self._contextual = {}
                self._tags = {}
                self._registry_identity = object()
                self._registry_lock = RLock()
            self._close_future.set_result(None)
            self._condition.notify_all()
        if parent is not None and not retain_for_handoff:
            with parent._condition:
                if self in parent._children:
                    parent._children.remove(self)
            parent._release_handoff_parent_if_idle()

    def _release_handoff_parent_if_idle(self) -> None:
        with self._condition:
            parent = self._parent
            idle = self._state == "disposed" and not (
                self._children
                or self._active_constructions
                or self._active_resolutions
                or self._inflight
                or self._async_inflight
                or self._late_async_resources
                or self._late_cleanup_task is not None
            )
        if parent is None or not idle:
            return
        with parent._condition:
            if self in parent._children:
                parent._children.remove(self)
        with self._condition:
            if self._parent is parent:
                self._parent = None
        parent._release_handoff_parent_if_idle()

    @staticmethod
    def _sanitized_failure(error: BaseException) -> BaseException:
        error_type = type(error)
        try:
            if any(error_type is builtin for builtin in _BUILTIN_EXCEPTION_TYPES):
                sanitized = error_type()
            else:
                sanitized = BaseException.__new__(error_type)
                BaseException.__init__(sanitized)
        except BaseException:
            sanitized = RuntimeError()
        sanitized.__traceback__ = None
        sanitized.__cause__ = None
        sanitized.__context__ = None
        sanitized.__suppress_context__ = True
        return sanitized

    def _raise_disposal_outcome(self) -> None:
        if self._disposal_cancelled:
            raise asyncio.CancelledError from None
        if self._disposal_failures:
            raise ScopeDisposalError(self._disposal_failures) from None

    def __enter__(self) -> Container:
        with self._shutdown_lock:
            self._begin_entry_locked()
        return self._complete_entry()

    def _begin_entry_locked(self) -> None:
        self._assert_active()
        with self._condition:
            if not self._hierarchy_is_active():
                raise ScopeDisposed("container scope is disposed")
            if self._entered:
                raise InvalidBinding("container scope is already entered")
            self._entered = True
            self._entry_task = self._current_asyncio_task()
            self._entry_thread_id = get_ident()
            self._invocation_token = _CURRENT_INVOCATION_SCOPE.set(self)

    def _complete_entry(self) -> Container:
        try:
            if self._execution_context is not None:
                self._context_lease = ContextLease(
                    self._execution_context,
                    validator=self._has_invocation_authority,
                )
                self._context_binding = use_context(
                    self._execution_context, lease=self._context_lease
                )
                self._context_binding.__enter__()
            elif self._scope is Scope.INVOCATION and maybe_current_context() is not None:
                self._context_binding = without_context()
                self._context_binding.__enter__()
        except BaseException:
            with self._condition:
                self._entered = False
                self._entry_task = None
                self._entry_thread_id = None
                if self._invocation_token is not None:
                    _CURRENT_INVOCATION_SCOPE.reset(self._invocation_token)
                    self._invocation_token = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        finally:
            if self._context_binding is not None:
                with suppress(ValueError):
                    self._context_binding.__exit__(exc_type, exc, traceback)
                self._context_binding = None
            with self._condition:
                self._entered = False
                self._entry_task = None
                self._entry_thread_id = None
                if self._invocation_token is not None:
                    with suppress(ValueError):
                        _CURRENT_INVOCATION_SCOPE.reset(self._invocation_token)
                    self._invocation_token = None

    async def __aenter__(self) -> Container:
        await self._acquire_entry_lock_async()
        try:
            self._begin_entry_locked()
        finally:
            self._shutdown_lock.release()
        return self._complete_entry()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        cleanup_error: BaseException | None = None
        try:
            await self.aclose()
        except BaseException as error:
            cleanup_error = error
        finally:
            if self._context_binding is not None:
                with suppress(ValueError):
                    self._context_binding.__exit__(exc_type, exc, traceback)
                self._context_binding = None
            with self._condition:
                self._entered = False
                self._entry_task = None
                self._entry_thread_id = None
                if self._invocation_token is not None:
                    with suppress(ValueError):
                        _CURRENT_INVOCATION_SCOPE.reset(self._invocation_token)
                    self._invocation_token = None
        if isinstance(exc, asyncio.CancelledError):
            raise exc from None
        if cleanup_error is not None:
            raise cleanup_error

    @property
    def context(self) -> ExecutionContext:
        self._assert_active()
        if self._scope is not Scope.INVOCATION or self._execution_context is None:
            raise ScopeUnavailable("invocation scope has no execution context")
        if not self._has_invocation_authority():
            raise ScopeUnavailable("context access requires the active invocation scope")
        return self._execution_context

    @property
    def invocation_kind(self) -> InvocationKind:
        self._assert_active()
        if self._scope is not Scope.INVOCATION or self._invocation_kind is None:
            raise ScopeUnavailable("container is not an invocation scope")
        return self._invocation_kind

    def _canonical_key(self, key: ServiceKey) -> ServiceKey:
        with self._registry_lock:
            path: list[ServiceKey] = []
            while key in self._aliases:
                if key in path:
                    cycle_start = path.index(key)
                    raise CircularDependency((*path[cycle_start:], key))
                path.append(key)
                key = self._aliases[key]
            return key

    @staticmethod
    def _key(token: object, qualifier: Qualifier | str | None) -> ServiceKey:
        if isinstance(token, ServiceKey):
            if type(token) is not ServiceKey:
                raise InvalidBinding("service key must not be subclassed")
            if qualifier is not None:
                raise InvalidBinding("qualifier must not be repeated for a service key")
            return token
        resolved_qualifier = Qualifier(qualifier) if isinstance(qualifier, str) else qualifier
        if resolved_qualifier is not None and type(resolved_qualifier) is not Qualifier:
            raise InvalidBinding("qualifier must be a Qualifier or string")
        return ServiceKey(token, resolved_qualifier)

    @staticmethod
    def _validated_tag_name(name: object) -> str:
        if type(name) is not str:
            raise InvalidBinding("tag name must be a bounded string")
        try:
            Qualifier(name)
        except ValueError:
            raise InvalidBinding("tag name must be a bounded string") from None
        return name

    @staticmethod
    def _validate_factory_scope(
        factory: Factory | AsyncFactory, scope: Scope, *, expect_async: bool
    ) -> None:
        if not callable(factory):
            raise InvalidBinding("binding factory must be callable")
        if not isinstance(scope, Scope):
            raise InvalidBinding("binding scope must be a Scope")
        if not expect_async:
            try:
                call = type.__getattribute__(type(factory), "__call__")
            except BaseException:
                call = None
            if iscoroutinefunction(factory) or iscoroutinefunction(call):
                raise InvalidBinding("factory async mode does not match binding API")

    def _nearest_scope(self, scope: Scope) -> Container | None:
        current: Container | None = self
        while current is not None:
            if current._scope is scope:
                return current
            current = current._parent
        return None

    def _root(self) -> Container:
        current = self
        while current._parent is not None:
            current = current._parent
        return current


class ServiceProvider(ABC):
    """Register related bindings; `boot` is available for post-registration hooks."""

    @abstractmethod
    def register(self, container: Container) -> None:
        raise NotImplementedError

    def boot(self, container: Container) -> None:
        del container
