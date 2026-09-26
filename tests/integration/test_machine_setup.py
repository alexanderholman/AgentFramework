import json

import pytest

from af.application.machine_setup import MachineSetupController, MachineSetupRequest
from af.infrastructure.machine_setup import LocalMachineSetup


def controller():
    return MachineSetupController(LocalMachineSetup())


def test_plan_is_readonly_explicit_and_offline(tmp_path):
    root = tmp_path / "machine"
    setup = controller()
    plan = setup.plan(MachineSetupRequest(root, profiles=("scheduler",)))
    assert not root.exists()
    payload = plan.to_dict()
    assert payload["capabilities"] == ["core", "database", "queue", "cache", "scheduler"]
    assert payload["downloads_bytes"] == 0 and payload["ports"] == []
    assert payload["locality"] == "local-only"
    assert payload["blocked"] == []
    with pytest.raises(ValueError):
        setup.apply(plan, confirmed=False)
    assert not root.exists()


def test_apply_real_sqlite_verify_restart_reconcile_and_retained_disable(tmp_path):
    root = tmp_path / "machine"
    setup = controller()
    request = MachineSetupRequest(root, profiles=("scheduler",))
    receipt = setup.apply(setup.plan(request), confirmed=True)
    assert receipt["state"] == "completed"
    assert receipt["verification"]["verified"] and receipt["verification"]["selector_calls"] == 0
    assert receipt["verification"]["provider_calls"] == 0
    assert (root / "af.sqlite3").is_file()
    saved = json.loads((root / "machine.json").read_text())
    assert saved["locality"] == "local-only"
    restarted = controller()
    assert restarted.status(root)["state"] == "healthy"
    repeated = restarted.apply(restarted.plan(request), confirmed=True)
    assert repeated["state"] == "completed"
    with pytest.raises(ValueError):
        restarted.disable(root, "database", confirmed=True)
    restarted.disable(root, "scheduler", confirmed=True)
    restarted.disable(root, "queue", confirmed=True)
    assert (root / "af.sqlite3").is_file()
    assert restarted.status(root)["capabilities"]["queue"]["enabled"] is False


def test_changed_config_prevents_stale_plan_apply(tmp_path):
    root = tmp_path / "machine"
    setup = controller()
    first = setup.plan(MachineSetupRequest(root))
    setup.apply(first, confirmed=True)
    with pytest.raises(ValueError):
        setup.apply(first, confirmed=True)


def test_unsupported_profile_missing_model_and_unrecognized_database_never_mutate(tmp_path):
    with pytest.raises(ValueError):
        MachineSetupRequest(tmp_path, profiles=("remote-model",))
    root = tmp_path / "machine"
    setup = controller()
    plan = setup.plan(MachineSetupRequest(root, model_paths=(tmp_path / "missing.gguf",)))
    assert plan.to_dict()["blocked"]
    with pytest.raises(ValueError):
        setup.apply(plan, confirmed=True)
    assert not root.exists()
    root.mkdir()
    (root / "af.sqlite3").write_bytes(b"not SQLite")
    blocked = setup.plan(MachineSetupRequest(root))
    assert blocked.to_dict()["blocked"]


def test_interrupted_setup_is_reconciled_with_durable_step_receipts(tmp_path, monkeypatch):
    backend = LocalMachineSetup()
    setup = MachineSetupController(backend)
    root = tmp_path / "machine"
    request = MachineSetupRequest(root, profiles=("queue",))
    original = backend._provision

    def interrupt(capability, path):
        if capability == "queue":
            raise KeyboardInterrupt()
        original(capability, path)

    monkeypatch.setattr(backend, "_provision", interrupt)
    with pytest.raises(KeyboardInterrupt):
        setup.apply(setup.plan(request), confirmed=True)
    assert setup.status(root)["receipts"][0]["state"] == "running"
    restarted = controller()
    receipt = restarted.apply(restarted.plan(request), confirmed=True)
    assert receipt["state"] == "completed" and receipt["reconciled_interruptions"]
    assert {item["state"] for item in restarted.status(root)["receipts"]} == {
        "completed",
        "interrupted",
    }


def test_failed_readiness_records_pending_steps_and_rerun_preserves_user_data(
    tmp_path, monkeypatch
):
    import sqlite3

    backend = LocalMachineSetup()
    setup = MachineSetupController(backend)
    root = tmp_path / "machine"
    original = backend._provision

    def fail(capability, path):
        if capability == "cache":
            raise RuntimeError("secret-address")
        original(capability, path)

    monkeypatch.setattr(backend, "_provision", fail)
    request = MachineSetupRequest(root, profiles=("cache",))
    receipt = setup.apply(setup.plan(request), confirmed=True)
    assert receipt["state"] == "failed" and receipt["pending"] == ["cache"]
    assert "secret" not in json.dumps(receipt)
    with sqlite3.connect(root / "af.sqlite3") as connection:
        connection.execute("CREATE TABLE user_owned (value TEXT)")
        connection.execute("INSERT INTO user_owned VALUES ('retained')")
    fresh = controller()
    assert fresh.apply(fresh.plan(request), confirmed=True)["state"] == "completed"
    with sqlite3.connect(root / "af.sqlite3") as connection:
        connection.execute("DROP TABLE af_cache_entries")
    assert fresh.status(root)["state"] == "degraded"
    assert fresh.apply(fresh.plan(request), confirmed=True)["state"] == "completed"
    with sqlite3.connect(root / "af.sqlite3") as connection:
        assert connection.execute("SELECT value FROM user_owned").fetchall() == [("retained",)]


def test_insufficient_disk_missing_engine_and_unsafe_state_fail_before_mutation(
    tmp_path, monkeypatch
):
    from collections import namedtuple

    import af.infrastructure.machine_setup as module

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: usage(10, 5, 5))
    root = tmp_path / "machine"
    setup = controller()
    plan = setup.plan(MachineSetupRequest(root, engine_paths=(tmp_path / "missing-engine",)))
    assert {"insufficient-free-disk", "engine-path-unsupported-or-missing"} <= set(
        plan.to_dict()["blocked"]
    )
    with pytest.raises(ValueError):
        setup.apply(plan, confirmed=True)
    assert not root.exists()
    real = tmp_path / "real"
    real.mkdir()
    root.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        setup.plan(MachineSetupRequest(root))
    assert list(real.iterdir()) == []


def test_disable_unknown_machine_does_not_create_files_and_export_reproduces(tmp_path):
    root = tmp_path / "machine"
    setup = controller()
    with pytest.raises(ValueError):
        setup.disable(root, "cache", confirmed=True)
    assert not root.exists()
    request = MachineSetupRequest(root, profiles=("cache",))
    setup.apply(setup.plan(request), confirmed=True)
    exported = setup.export(root)
    other = tmp_path / "copy"
    reproduced = MachineSetupRequest.from_declaration(other, exported)
    receipt = setup.apply(setup.plan(reproduced), confirmed=True)
    assert receipt["state"] == "completed"
    assert setup.status(other)["capabilities"]["cache"]["enabled"]


def test_apply_ownership_is_exclusive(tmp_path):
    root = tmp_path / "machine"
    backend = LocalMachineSetup()
    setup = MachineSetupController(backend)
    plan = setup.plan(MachineSetupRequest(root))
    with backend._exclusive(root), pytest.raises(ValueError, match="another machine"):
        setup.apply(plan, confirmed=True)


@pytest.mark.parametrize(
    "options",
    [
        {"state_dir": "not-path"},
        {"profiles": ()},
        {"profiles": ("core", "core")},
        {"model_paths": []},
        {"max_memory_mib": 0},
        {"minimum_free_disk_mib": 0},
    ],
)
def test_request_rejects_ambiguous_or_unbounded_values(tmp_path, options):
    with pytest.raises(ValueError):
        MachineSetupRequest(**{"state_dir": tmp_path, **options})


@pytest.mark.parametrize(
    "declaration",
    [
        {},
        {"schema_version": "af.machine/1", "locality": "remote"},
        {"schema_version": "af.machine/1", "locality": "local-only", "capabilities": []},
        {
            "schema_version": "af.machine/1",
            "locality": "local-only",
            "capabilities": ["core"],
            "resource_budget": {},
            "remote_routes": ["remote"],
        },
        {
            "schema_version": "af.machine/1",
            "locality": "local-only",
            "capabilities": ["core"],
            "resource_budget": {},
            "models": [{"path": 12}],
        },
    ],
)
def test_declaration_import_requires_typed_local_only_contract(tmp_path, declaration):
    with pytest.raises(ValueError):
        MachineSetupRequest.from_declaration(tmp_path, declaration)


def test_unknown_setup_unapproved_disable_and_invalid_input_are_inert(tmp_path):
    setup = controller()
    with pytest.raises(TypeError):
        setup.plan(object())
    with pytest.raises(ValueError):
        setup.disable(tmp_path / "machine", "core", confirmed=True)
    with pytest.raises(ValueError):
        setup.export(tmp_path / "machine")
    assert list(tmp_path.iterdir()) == []


def test_discovery_inventory_unknown_memory_and_gpu_are_truthful(tmp_path, monkeypatch):
    import af.infrastructure.machine_setup as module

    model, engine = tmp_path / "model.gguf", tmp_path / "engine"
    model.write_bytes(b"GGUF")
    engine.write_text("#!/bin/sh\nexit 0\n")
    engine.chmod(0o700)
    gpu = tmp_path / "gpu"
    gpu.mkdir()
    (gpu / "vendor").write_text("0x1234")
    real_path = module.Path

    def path(value):
        return gpu if str(value) == "/sys/class/drm/card0/device" else real_path(value)

    monkeypatch.setattr(module, "Path", path)
    monkeypatch.setattr(module.os, "sysconf", lambda key: (_ for _ in ()).throw(OSError()))
    discovery = LocalMachineSetup().discover(
        MachineSetupRequest(tmp_path / "machine", model_paths=(model,), engine_paths=(engine,))
    )
    assert discovery["ram_mib"] is None and discovery["gpu_devices"][0]["vendor"] == "0x1234"
    assert discovery["models"][0]["state"] == "discovered-unverified"
    assert discovery["engines"][0]["state"] == "discovered-unverified"
    assert discovery["gpu_qualification"] == "not-performed"


def test_unsupported_os_and_low_ram_require_manual_resolution(tmp_path, monkeypatch):
    import af.infrastructure.machine_setup as module

    monkeypatch.setattr(module.platform, "system", lambda: "UnsupportedOS")
    monkeypatch.setattr(module.os, "sysconf", lambda key: 1)
    discovered = LocalMachineSetup().discover(MachineSetupRequest(tmp_path / "machine"))
    assert {"unsupported-host-locking", "insufficient-host-memory"} <= set(discovered["blocked"])


@pytest.mark.parametrize(
    "content",
    [
        b"x" * 1_048_577,
        b"{}",
        b'{"schema_version":"af.machine/1","locality":"local-only","capabilities":[]}',
    ],
)
def test_corrupt_configuration_is_not_overwritten(tmp_path, content):
    root = tmp_path / "machine"
    root.mkdir()
    path = root / "machine.json"
    path.write_bytes(content)
    assert controller().status(root)["state"] == "misconfigured"
    with pytest.raises(ValueError):
        controller().plan(MachineSetupRequest(root))
    assert path.read_bytes() == content


def test_existing_managed_symlink_is_never_followed(tmp_path):
    root = tmp_path / "machine"
    root.mkdir()
    target = tmp_path / "unrelated"
    target.write_text("retained")
    (root / "af.sqlite3").symlink_to(target)
    with pytest.raises(ValueError):
        controller().plan(MachineSetupRequest(root))
    assert target.read_text() == "retained"


def test_offline_probe_cannot_accept_unverified_result(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import af.bootstrap as bootstrap
    from af.infrastructure.machine_setup import _NoModelSelector

    selector = _NoModelSelector()
    with pytest.raises(RuntimeError):
        selector.select("unmatched", ())
    assert selector.calls == 1
    closed = []
    monkeypatch.setattr(
        bootstrap,
        "create_application",
        lambda **kwargs: SimpleNamespace(
            tasks=SimpleNamespace(
                execute=lambda prompt: SimpleNamespace(verified=False, output="bad")
            ),
            shutdown=lambda: closed.append(True),
        ),
    )
    with pytest.raises(ValueError):
        LocalMachineSetup._verify(tmp_path)
    assert closed == [True]


def test_machine_factory_uses_declaration_not_remote_environment(tmp_path, monkeypatch):
    import af.bootstrap_setup as setup
    from af.infrastructure.machine_setup import machine_settings

    with pytest.raises(ValueError):
        machine_settings(tmp_path / "absent")
    root = tmp_path / "machine"
    service = controller()
    service.apply(service.plan(MachineSetupRequest(root, profiles=("scheduler",))), confirmed=True)
    monkeypatch.setenv("AF_MACHINE_ROOT", str(root))
    monkeypatch.setenv("AF_ALLOW_REMOTE_INFERENCE", "true")
    captured = []
    monkeypatch.setattr(
        setup, "create_application", lambda **kwargs: captured.append(kwargs["settings"])
    )
    setup.create_machine_application()
    assert not captured[0].allow_remote_inference
    assert captured[0].worker_queue_sqlite_path is None  # no background worker is installed
    assert captured[0].schedules_path == str(root / "schedules.json")


def test_configured_application_exposes_real_owned_queue_and_cache(tmp_path, monkeypatch):
    from af.bootstrap_setup import create_machine_application
    from af.cache.contracts import CacheKey, CacheNamespace
    from af.cache.manager import CacheManager
    from af.queue import QueueManager

    root = tmp_path / "machine"
    service = controller()
    service.apply(service.plan(MachineSetupRequest(root, profiles=("scheduler",))), confirmed=True)
    monkeypatch.setenv("AF_MACHINE_ROOT", str(root))
    application = create_machine_application()
    try:
        cache = application.container.resolve(CacheManager)
        namespace, key = CacheNamespace("setup", "test", "runtime"), CacheKey("ready")
        cache.set(namespace, key, b"ready")
        assert cache.get(namespace, key) == b"ready"
        queue = application.container.resolve(QueueManager)
        assert queue.reserve(lane="default") is None
        assert application.tasks.execute("how many rs in strawberry").verified
    finally:
        application.shutdown()
