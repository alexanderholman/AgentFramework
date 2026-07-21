"""A small explicit service container: Laravel-inspired, Python-oriented."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar, cast

from af.framework.errors import InvalidBinding, ServiceNotBound

T = TypeVar("T")
Factory = Callable[["Container"], object]


class Scope(StrEnum):
    SINGLETON = "singleton"
    TRANSIENT = "transient"


@dataclass(slots=True)
class _Binding:
    factory: Factory
    scope: Scope
    instance: object | None = None


class Container:
    """Resolve explicit bindings; deliberately does not inspect constructors."""

    def __init__(self) -> None:
        self._bindings: dict[object, _Binding] = {}

    def bind(
        self,
        token: object,
        factory: Factory,
        *,
        scope: Scope = Scope.TRANSIENT,
        replace: bool = False,
    ) -> None:
        if token in self._bindings and not replace:
            raise InvalidBinding(f"binding already exists: {token!r}")
        self._bindings[token] = _Binding(factory=factory, scope=scope)

    def instance(self, token: object, value: object, *, replace: bool = False) -> None:
        self.bind(token, lambda _: value, scope=Scope.SINGLETON, replace=replace)
        self._bindings[token].instance = value

    def resolve(self, token: type[T]) -> T:
        try:
            binding = self._bindings[token]
        except KeyError as error:
            raise ServiceNotBound(token) from error

        if binding.scope is Scope.SINGLETON:
            if binding.instance is None:
                binding.instance = binding.factory(self)
            return cast(T, binding.instance)
        return cast(T, binding.factory(self))

    def resolve_token(self, token: object) -> Any:
        """Resolve a Protocol or other non-class token at composition boundaries."""
        try:
            binding = self._bindings[token]
        except KeyError as error:
            raise ServiceNotBound(token) from error
        if binding.scope is Scope.SINGLETON:
            if binding.instance is None:
                binding.instance = binding.factory(self)
            return binding.instance
        return binding.factory(self)


class ServiceProvider(ABC):
    """Register related bindings; `boot` is available for post-registration hooks."""

    @abstractmethod
    def register(self, container: Container) -> None:
        raise NotImplementedError

    def boot(self, container: Container) -> None:
        del container
