"""Reusable provider and driver contract-suite fixtures."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from inspect import isawaitable
from typing import Protocol, cast

from af.framework.application import ApplicationState
from af.framework.container import ServiceProvider
from af.testing.application import TestApplication, TestApplicationBuilder

__all__ = (
    "ConformanceCase",
    "ConformanceEvidence",
    "ConformanceReport",
    "DriverConformanceSuite",
    "IntegrationUnavailable",
    "ProviderConformanceSuite",
    "RealServiceHook",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_MAX_CASES = 256


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical identifier")
    return value


class _Exercise[T](Protocol):
    def __call__(self, fixture: T, /) -> object: ...


@dataclass(frozen=True, slots=True, repr=False)
class ConformanceCase[T]:
    """One reusable synchronous contract check."""

    name: str
    exercise: _Exercise[T]

    def __post_init__(self) -> None:
        _identifier(self.name, "conformance case name")
        if not callable(self.exercise):
            raise ValueError("conformance case exercise must be callable")

    def __repr__(self) -> str:
        return f"ConformanceCase(name={self.name!r}, exercise='[REDACTED]')"


class ConformanceEvidence(StrEnum):
    ISOLATED = "isolated"
    FAKE = "fake"
    REAL_SERVICE = "real-service"


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """Executed checks; never sufficient alone for production certification."""

    suite: str
    cases: tuple[str, ...]
    evidence: ConformanceEvidence
    integration: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.suite, "conformance suite name")
        if (
            not self.cases
            or len(self.cases) > _MAX_CASES
            or len(set(self.cases)) != len(self.cases)
        ):
            raise ValueError("conformance report cases are invalid")
        for name in self.cases:
            _identifier(name, "conformance report case name")
        if not isinstance(self.evidence, ConformanceEvidence):
            raise ValueError("conformance report evidence is invalid")
        if self.integration is not None:
            _identifier(self.integration, "conformance integration name")
        if self.evidence is ConformanceEvidence.REAL_SERVICE:
            if self.integration is None:
                raise ValueError("real-service evidence requires an integration name")
        elif self.integration is not None:
            raise ValueError("only real-service evidence may name an integration")

    @property
    def certifies_production(self) -> bool:
        return False


class IntegrationUnavailable(RuntimeError):
    """An explicitly configured real-service fixture is not available."""


type DriverFixture[T] = Callable[[], AbstractContextManager[T]]


@dataclass(frozen=True, slots=True, repr=False)
class RealServiceHook[T]:
    """Explicit opt-in hook for a caller-owned real-service fixture."""

    name: str
    fixture: DriverFixture[T]
    enabled: bool = False
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.name, "real-service integration name")
        if not callable(self.fixture):
            raise ValueError("real-service fixture must be callable")
        if type(self.enabled) is not bool:
            raise ValueError("real-service enablement must be boolean")
        if not self.enabled:
            if (
                type(self.disabled_reason) is not str
                or not self.disabled_reason
                or len(self.disabled_reason) > 512
                or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", self.disabled_reason)
            ):
                raise ValueError("disabled real-service hooks require a bounded single-line reason")
        elif self.disabled_reason is not None:
            raise ValueError("enabled real-service hooks cannot have a disabled reason")

    def open(self) -> AbstractContextManager[T]:
        if not self.enabled:
            raise IntegrationUnavailable(cast(str, self.disabled_reason))
        fixture = self.fixture()
        if not isinstance(fixture, AbstractContextManager):
            raise TypeError("real-service fixture did not return a context manager")
        return fixture

    def __repr__(self) -> str:
        return (
            f"RealServiceHook(name={self.name!r}, fixture='[REDACTED]', "
            f"enabled={self.enabled!r}, disabled_reason='[REDACTED]')"
        )


def _normalize_cases[T](
    cases: tuple[ConformanceCase[T], ...],
) -> tuple[ConformanceCase[T], ...]:
    normalized = tuple(cases)
    if (
        not normalized
        or len(normalized) > _MAX_CASES
        or any(not isinstance(case, ConformanceCase) for case in normalized)
        or len({case.name for case in normalized}) != len(normalized)
    ):
        raise ValueError("conformance cases must be bounded, non-empty, and unique")
    return normalized


def _run_case[T](case: ConformanceCase[T], fixture: T) -> None:
    result = case.exercise(fixture)
    if isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise TypeError("conformance cases must be synchronous")


class ProviderConformanceSuite:
    """Run every provider case in a fresh isolated TestApplication."""

    def __init__(
        self,
        name: str,
        cases: tuple[ConformanceCase[TestApplication], ...],
    ) -> None:
        self._name = _identifier(name, "provider conformance suite name")
        self._cases = _normalize_cases(cases)

    def run(
        self,
        provider_factory: Callable[[], ServiceProvider],
        *,
        builder_factory: Callable[[], TestApplicationBuilder],
    ) -> ConformanceReport:
        if not callable(provider_factory) or not callable(builder_factory):
            raise ValueError("provider conformance factories must be callable")
        for case in self._cases:
            provider = provider_factory()
            builder = builder_factory()
            if not isinstance(provider, ServiceProvider):
                raise TypeError("provider fixture did not return a ServiceProvider")
            if not isinstance(builder, TestApplicationBuilder):
                raise TypeError("builder fixture did not return a TestApplicationBuilder")
            application = builder.provider(provider).build()
            try:
                _run_case(case, application)
            finally:
                try:
                    if application.state is not ApplicationState.SHUTDOWN:
                        application.shutdown()
                finally:
                    application.assert_clean()
        return ConformanceReport(
            self._name,
            tuple(case.name for case in self._cases),
            ConformanceEvidence.ISOLATED,
        )


class DriverConformanceSuite[T]:
    """Run reusable driver contracts against fake or explicit real-service fixtures."""

    def __init__(
        self,
        name: str,
        cases: tuple[ConformanceCase[T], ...],
    ) -> None:
        self._name = _identifier(name, "driver conformance suite name")
        self._cases = _normalize_cases(cases)

    def run(
        self,
        fixture_factory: DriverFixture[T],
    ) -> ConformanceReport:
        return self._run(fixture_factory, ConformanceEvidence.FAKE)

    def _run(
        self,
        fixture_factory: DriverFixture[T],
        evidence: ConformanceEvidence,
        integration: str | None = None,
    ) -> ConformanceReport:
        if not callable(fixture_factory):
            raise ValueError("driver fixture factory must be callable")
        for case in self._cases:
            fixture = fixture_factory()
            if not isinstance(fixture, AbstractContextManager):
                raise TypeError("driver fixture did not return a context manager")
            with fixture as driver:
                _run_case(case, driver)
        return ConformanceReport(
            self._name,
            tuple(case.name for case in self._cases),
            evidence,
            integration,
        )

    def run_real(self, hook: RealServiceHook[T]) -> ConformanceReport:
        if not isinstance(hook, RealServiceHook):
            raise TypeError("real-service conformance requires a RealServiceHook")
        if not hook.enabled:
            hook.open()
        return self._run(
            hook.open,
            ConformanceEvidence.REAL_SERVICE,
            hook.name,
        )
