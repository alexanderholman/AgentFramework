from __future__ import annotations

import os
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.testing import (
    ProcessStateGuard,
    ProcessStateLeak,
    TestApplicationBuilder,
    assert_test_application_clean,
    inspect_test_application,
)


@dataclass(slots=True)
class _ScopedValue:
    identity: object


class _ScopedProvider(ServiceProvider):
    provider_id = "tests.isolation-scoped"

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            _ScopedValue,
            lambda _resolver: _ScopedValue(object()),
            scope=Scope.APPLICATION,
            owned=False,
        )


def test_process_state_guard_detects_and_restores_environment() -> None:
    key = "AF_TEST_PROCESS_STATE_GUARD_SECRET"
    previous = os.environ.pop(key, None)
    try:
        with pytest.raises(ProcessStateLeak) as captured:
            with ProcessStateGuard():
                os.environ[key] = "must-not-appear-in-error"

        assert captured.value.surfaces == ("environment",)
        assert key not in os.environ
        assert "must-not-appear-in-error" not in str(captured.value)
    finally:
        if previous is not None:
            os.environ[key] = previous


def test_process_state_guard_restores_working_directory_and_warning_policy(
    tmp_path: Path,
) -> None:
    original = os.getcwd()
    target = os.fspath(tmp_path)

    with pytest.raises(ProcessStateLeak) as captured:
        with ProcessStateGuard():
            os.chdir(target)
            warnings.filterwarnings("ignore", message="af-test-isolation")

    assert os.getcwd() == original
    assert captured.value.surfaces == ("warnings", "working-directory")


def test_process_state_guard_detects_new_live_threads_without_disclosing_names() -> None:
    started = threading.Event()
    release = threading.Event()

    def worker() -> None:
        started.set()
        release.wait()

    thread = threading.Thread(target=worker, name="sensitive-thread-name")
    try:
        with pytest.raises(ProcessStateLeak) as captured:
            with ProcessStateGuard():
                thread.start()
                assert started.wait(timeout=2)

        assert captured.value.surfaces == ("threads",)
        assert "sensitive-thread-name" not in str(captured.value)
    finally:
        release.set()
        thread.join(timeout=2)


def test_application_leak_report_rejects_retained_internal_activity() -> None:
    application = TestApplicationBuilder().build()
    application.shutdown()
    assert_test_application_clean(application)

    application.container._active_resolutions = 1
    try:
        report = inspect_test_application(application)
        assert report.clean is False
        assert report.active_operations == 1
        assert report.leaked_surfaces == ("active-operations",)
        with pytest.raises(AssertionError, match="active-operations"):
            assert_test_application_clean(application)
    finally:
        application.container._active_resolutions = 0

    assert_test_application_clean(application)


def test_two_test_applications_boot_concurrently_without_scope_or_resource_leakage() -> None:
    def run() -> object:
        application = TestApplicationBuilder().provider(_ScopedProvider()).build()
        try:
            return application.resolve(_ScopedValue).identity
        finally:
            application.shutdown()
            assert_test_application_clean(application)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(run), executor.submit(run))
        first, second = (future.result() for future in futures)

    assert first is not second
