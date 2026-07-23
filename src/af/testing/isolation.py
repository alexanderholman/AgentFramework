"""Bounded process and application leak detection for extension tests."""

from __future__ import annotations

import os
import threading
import warnings
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, cast

from af.framework.application import ApplicationState
from af.framework.container import Container
from af.testing.application import TestApplication

__all__ = (
    "ApplicationLeakReport",
    "ProcessStateGuard",
    "ProcessStateLeak",
    "assert_test_application_clean",
    "inspect_test_application",
)

_PROCESS_STATE_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class _ProcessSnapshot:
    cwd: str | None
    environment: tuple[tuple[str, str], ...]
    warning_filters: tuple[tuple[object, ...], ...]
    warning_default_action: str
    warning_once_registry: dict[object, object]
    threads: frozenset[threading.Thread]


class ProcessStateLeak(AssertionError):
    """One bounded process-global surface changed during an isolated test."""

    def __init__(self, surfaces: tuple[str, ...]) -> None:
        self.surfaces = tuple(sorted(set(surfaces)))
        super().__init__(
            "test leaked process-global state: " + ", ".join(self.surfaces)
        )


class ProcessStateGuard:
    """Snapshot, restore, and reject mutations to bounded process-global surfaces.

    The guard covers the current working directory, environment, warning policy,
    and newly live threads. It deliberately reports only surface names; paths,
    environment values, warning contents, and thread names are never disclosed.
    """

    def __init__(self) -> None:
        self._snapshot: _ProcessSnapshot | None = None
        self._entered = False

    def __enter__(self) -> Self:
        if self._entered:
            raise RuntimeError("process-state guard is already active")
        _PROCESS_STATE_LOCK.acquire()
        try:
            self._snapshot = _capture_process_state()
            self._entered = True
        except BaseException:
            _PROCESS_STATE_LOCK.release()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, traceback
        snapshot = self._snapshot
        if not self._entered or snapshot is None:
            raise RuntimeError("process-state guard is not active")

        surfaces: list[str] = []
        try:
            current = _capture_process_state()
            if current.cwd != snapshot.cwd:
                surfaces.append("working-directory")
            if current.environment != snapshot.environment:
                surfaces.append("environment")
            if (
                current.warning_filters != snapshot.warning_filters
                or current.warning_default_action != snapshot.warning_default_action
                or current.warning_once_registry != snapshot.warning_once_registry
            ):
                surfaces.append("warnings")
            if any(
                thread not in snapshot.threads and thread.is_alive()
                for thread in current.threads
            ):
                surfaces.append("threads")

            _restore_process_state(snapshot, surfaces)
        finally:
            self._snapshot = None
            self._entered = False
            _PROCESS_STATE_LOCK.release()

        normalized = tuple(sorted(set(surfaces)))
        if not normalized:
            return False
        if exc is not None:
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(
                    "AF testing detected process-global state changes on: "
                    + ", ".join(normalized)
                )
            return False
        raise ProcessStateLeak(normalized)


def _capture_process_state() -> _ProcessSnapshot:
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    return _ProcessSnapshot(
        cwd=cwd,
        environment=tuple(sorted(os.environ.items())),
        warning_filters=cast(
            tuple[tuple[object, ...], ...],
            tuple(tuple(value) for value in warnings.filters),
        ),
        warning_default_action=warnings.defaultaction,
        warning_once_registry=cast(dict[object, object], dict(warnings.onceregistry)),
        threads=frozenset(threading.enumerate()),
    )


def _restore_process_state(snapshot: _ProcessSnapshot, surfaces: list[str]) -> None:
    try:
        os.environ.clear()
        os.environ.update(dict(snapshot.environment))
    except BaseException:
        surfaces.append("environment-restore")

    if snapshot.cwd is not None:
        try:
            os.chdir(snapshot.cwd)
        except BaseException:
            surfaces.append("working-directory-restore")

    try:
        warnings.filters[:] = cast(Any, list(snapshot.warning_filters))
        warnings.defaultaction = snapshot.warning_default_action
        warnings.onceregistry.clear()
        warnings.onceregistry.update(cast(Any, snapshot.warning_once_registry))
        filters_mutated = getattr(warnings, "_filters_mutated", None)
        if callable(filters_mutated):
            filters_mutated()
    except BaseException:
        surfaces.append("warnings-restore")


@dataclass(frozen=True, slots=True)
class ApplicationLeakReport:
    """Count-only test-host cleanup evidence with no service or resource values."""

    lifecycle_shutdown: bool
    active_scopes: int
    retained_scope_links: int
    entered_scopes: int
    retained_resources: int
    active_tasks: int
    active_operations: int
    cleanup_failures: int

    @property
    def clean(self) -> bool:
        return self.lifecycle_shutdown and not any(
            (
                self.active_scopes,
                self.retained_scope_links,
                self.entered_scopes,
                self.retained_resources,
                self.active_tasks,
                self.active_operations,
                self.cleanup_failures,
            )
        )

    @property
    def leaked_surfaces(self) -> tuple[str, ...]:
        surfaces: list[str] = []
        if not self.lifecycle_shutdown:
            surfaces.append("lifecycle")
        for name in (
            "active_scopes",
            "retained_scope_links",
            "entered_scopes",
            "retained_resources",
            "active_tasks",
            "active_operations",
            "cleanup_failures",
        ):
            if getattr(self, name):
                surfaces.append(name.replace("_", "-"))
        return tuple(surfaces)


def inspect_test_application(application: TestApplication) -> ApplicationLeakReport:
    """Inspect lifecycle-owned scopes and resources after test-host shutdown."""

    if not isinstance(application, TestApplication):
        raise TypeError("application leak inspection requires a TestApplication")

    lifecycle = application.application.lifecycle
    roots = (lifecycle._root, application.container)
    scopes = _collect_scopes(roots)

    active_scopes = 0
    retained_scope_links = 0
    entered_scopes = 0
    retained_resources = 0
    active_tasks = 0
    active_operations = 0
    cleanup_failures = len(lifecycle.cleanup_failures)

    for scope in scopes:
        with scope._condition:
            active_scopes += scope._state != "disposed"
            retained_scope_links += len(scope._children)
            entered_scopes += bool(scope._entered)
            retained_resources += (
                len(scope._resources)
                + len(scope._late_async_resources)
                + len(scope._late_owned_resources)
                + len(scope._owned_resource_ids)
            )
            late_cleanup_task = scope._late_cleanup_task
            active_tasks += (
                len(scope._tasks)
                + len(scope._async_constructions)
                + len(scope._async_inflight)
                + len(scope._resolution_tasks)
                + int(late_cleanup_task is not None and not late_cleanup_task.done())
            )
            active_operations += (
                scope._active_factories
                + scope._active_constructions
                + scope._active_resolutions
                + len(scope._inflight)
                + len(scope._sync_flights)
            )
            cleanup_failures += len(scope._disposal_failures)

    root = lifecycle._root
    with root._ownership_lock:
        retained_resources += (
            len(root._container_owned_resources)
            + len(root._externally_owned_resources)
        )

    retained_resources += len(lifecycle._provider_resources)
    active_operations += (
        len(lifecycle._pending_async_cleanup)
        + len(lifecycle._pending_async_shutdowns)
        + len(lifecycle._pending_async_contexts)
        + len(lifecycle._pending_shutdown_awaitables)
        + lifecycle._sync_operations
        + lifecycle._async_activation_count
    )

    return ApplicationLeakReport(
        lifecycle_shutdown=lifecycle.state is ApplicationState.SHUTDOWN,
        active_scopes=active_scopes,
        retained_scope_links=retained_scope_links,
        entered_scopes=entered_scopes,
        retained_resources=retained_resources,
        active_tasks=active_tasks,
        active_operations=active_operations,
        cleanup_failures=cleanup_failures,
    )


def assert_test_application_clean(application: TestApplication) -> None:
    """Reject incomplete lifecycle cleanup and retained scope/resource state."""

    application.assert_clean()
    report = inspect_test_application(application)
    if not report.clean:
        raise AssertionError(
            "test application leaked state: " + ", ".join(report.leaked_surfaces)
        )


def _collect_scopes(roots: tuple[Container, ...]) -> tuple[Container, ...]:
    pending = list(roots)
    visited: set[int] = set()
    scopes: list[Container] = []
    while pending:
        scope = pending.pop()
        identity = id(scope)
        if identity in visited:
            continue
        visited.add(identity)
        scopes.append(scope)
        with scope._condition:
            pending.extend(scope._children)
    return tuple(scopes)
