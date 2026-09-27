from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError, dataclass

import pytest

from af.config import (
    ConfigSnapshot,
    ConfigurationProfile,
    EnvironmentConfigSource,
    MappingConfigSource,
    ResourceLeaseBackend,
    SecretRef,
    Settings,
    load_settings,
)
from af.framework.errors import PlatformError, PlatformReason


def test_layered_configuration_has_typed_precedence_and_immutable_snapshot() -> None:
    values = {"app_name": "base", "port": 8100}
    source = MappingConfigSource(
        "file",
        values,
        profiles={"test": {"app_name": "profile", "port": 8200}},
    )

    snapshot = load_settings(
        source,
        profile=ConfigurationProfile.TEST,
        overrides={"port": 8300},
    )
    values["app_name"] = "mutated"

    assert snapshot.settings.app_name == "profile"
    assert snapshot.settings.port == 8300
    assert snapshot.profile is ConfigurationProfile.TEST
    assert snapshot.sources == ("file", "overrides")
    with pytest.raises(FrozenInstanceError):
        snapshot.profile = ConfigurationProfile.LOCAL  # type: ignore[misc]

    production = load_settings(
        MappingConfigSource("test-only", {"host": "base"}, profiles={"test": {"port": 9001}}),
        MappingConfigSource("production", {}, profiles={"production": {"port": 9002}}),
        profile=ConfigurationProfile.PRODUCTION,
    )
    assert production.settings.port == 9002


def test_environment_source_preserves_existing_settings_parsing() -> None:
    snapshot = load_settings(
        EnvironmentConfigSource(
            {
                "AF_APP_NAME": "Configured AF",
                "AF_PORT": "9000",
                "AF_ALLOWED_HOSTS": "one.example, two.example",
                "AF_ALLOW_REMOTE_INFERENCE": "yes",
                "AF_SESSION_COOKIE_SIGNING_KEY": "deployment-session-key",
                "AF_SELECTOR_CONFIDENCE_THRESHOLD": "0.65",
            }
        )
    )

    assert snapshot.settings.app_name == "Configured AF"
    assert snapshot.settings.port == 9000
    assert snapshot.settings.allowed_hosts == ("one.example", "two.example")
    assert snapshot.settings.allow_remote_inference is True
    assert snapshot.settings.session_cookie_signing_key == "deployment-session-key"
    assert snapshot.settings.selector_confidence_threshold == 0.65

    disabled = load_settings(EnvironmentConfigSource({"AF_ALLOW_REMOTE_INFERENCE": "off"}))
    assert disabled.settings.allow_remote_inference is False


def test_resource_lease_storage_requires_an_explicit_typed_opt_in() -> None:
    assert Settings().resource_lease_backend is ResourceLeaseBackend.MEMORY

    snapshot = load_settings(
        EnvironmentConfigSource(
            {
                "AF_RESOURCE_LEASE_BACKEND": "sqlite",
                "AF_RESOURCE_LEASE_SQLITE_PATH": "/var/lib/af/resource-leases.sqlite3",
            }
        )
    )

    assert snapshot.settings.resource_lease_backend is ResourceLeaseBackend.SQLITE
    assert snapshot.settings.resource_lease_sqlite_path == ("/var/lib/af/resource-leases.sqlite3")

    with pytest.raises(PlatformError) as invalid_backend:
        load_settings(EnvironmentConfigSource({"AF_RESOURCE_LEASE_BACKEND": "automatic"}))
    assert invalid_backend.value.reason is PlatformReason.CONFIGURATION_INVALID

    with pytest.raises(PlatformError) as ephemeral_durable_store:
        load_settings(
            EnvironmentConfigSource(
                {
                    "AF_RESOURCE_LEASE_BACKEND": "sqlite",
                    "AF_RESOURCE_LEASE_SQLITE_PATH": ":memory:",
                }
            )
        )
    assert ephemeral_durable_store.value.reason is PlatformReason.CONFIGURATION_INVALID


def test_resource_lease_maintenance_has_typed_host_scope_and_bounded_cadence() -> None:
    defaults = Settings()
    assert defaults.resource_lease_maintenance_host_id is None
    assert defaults.resource_lease_maintenance_interval_seconds == 30.0

    snapshot = load_settings(
        EnvironmentConfigSource(
            {
                "AF_RESOURCE_LEASE_MAINTENANCE_HOST_ID": "worker-a.example",
                "AF_RESOURCE_LEASE_MAINTENANCE_INTERVAL_SECONDS": "2.5",
            }
        )
    )

    assert snapshot.settings.resource_lease_maintenance_host_id == "worker-a.example"
    assert snapshot.settings.resource_lease_maintenance_interval_seconds == 2.5

    for invalid in ("0", "nan", "86400.1"):
        with pytest.raises(PlatformError) as invalid_cadence:
            load_settings(
                EnvironmentConfigSource({"AF_RESOURCE_LEASE_MAINTENANCE_INTERVAL_SECONDS": invalid})
            )
        assert invalid_cadence.value.reason is PlatformReason.CONFIGURATION_INVALID


def test_worker_queue_bootstrap_requires_explicit_backend_and_profile_artifacts() -> None:
    defaults = Settings()
    assert defaults.worker_queue_sqlite_path is None
    assert defaults.worker_optimization_profile_path is None

    snapshot = load_settings(
        EnvironmentConfigSource(
            {
                "AF_WORKER_QUEUE_SQLITE_PATH": "/var/lib/af/worker-queue.sqlite3",
                "AF_WORKER_OPTIMIZATION_PROFILE_PATH": "/etc/af/worker-profile.json",
            }
        )
    )

    assert snapshot.settings.worker_queue_sqlite_path == "/var/lib/af/worker-queue.sqlite3"
    assert snapshot.settings.worker_optimization_profile_path == "/etc/af/worker-profile.json"

    for incomplete in (
        {"AF_WORKER_QUEUE_SQLITE_PATH": "/var/lib/af/worker-queue.sqlite3"},
        {"AF_WORKER_OPTIMIZATION_PROFILE_PATH": "/etc/af/worker-profile.json"},
        {
            "AF_WORKER_QUEUE_SQLITE_PATH": ":memory:",
            "AF_WORKER_OPTIMIZATION_PROFILE_PATH": "/etc/af/worker-profile.json",
        },
    ):
        with pytest.raises(PlatformError) as invalid:
            load_settings(EnvironmentConfigSource(incomplete))
        assert invalid.value.reason is PlatformReason.CONFIGURATION_INVALID


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"unknown": "value"}, PlatformReason.CONFIGURATION_INVALID),
        ({"port": True}, PlatformReason.CONFIGURATION_INVALID),
        ({"port": "not-an-int"}, PlatformReason.CONFIGURATION_INVALID),
        (
            {"database_url": SecretRef("environment", "DATABASE_SECRET")},
            PlatformReason.CONFIGURATION_SECRET_UNRESOLVED,
        ),
    ],
)
def test_invalid_configuration_fails_with_stable_sanitized_reason(
    values: dict[str, object], reason: PlatformReason
) -> None:
    with pytest.raises(PlatformError) as captured:
        load_settings(MappingConfigSource("unsafe", values))

    assert captured.value.reason is reason
    assert "DATABASE_SECRET" not in str(captured.value)
    assert "DATABASE_SECRET" not in repr(captured.value)


def test_unknown_profile_and_malformed_environment_fail_closed() -> None:
    source = MappingConfigSource("file", {}, profiles={"test": {}})
    with pytest.raises(PlatformError) as unknown:
        load_settings(source, profile="missing")
    assert unknown.value.reason is PlatformReason.CONFIGURATION_PROFILE_UNKNOWN

    with pytest.raises(PlatformError) as malformed:
        load_settings(EnvironmentConfigSource({"AF_ALLOW_REMOTE_INFERENCE": "sometimes"}))
    assert malformed.value.reason is PlatformReason.CONFIGURATION_INVALID

    for environment in (
        {"AF_PORT": "not-a-port"},
        {"AF_SELECTOR_CONFIDENCE_THRESHOLD": "not-a-threshold"},
    ):
        with pytest.raises(PlatformError) as invalid_number:
            load_settings(EnvironmentConfigSource(environment))
        assert invalid_number.value.reason is PlatformReason.CONFIGURATION_INVALID

    with pytest.raises(PlatformError) as unknown_environment:
        load_settings(EnvironmentConfigSource({"AF_UNKNOWN_SETTING": "secret-value"}))
    assert unknown_environment.value.reason is PlatformReason.CONFIGURATION_INVALID
    assert "secret-value" not in repr(unknown_environment.value)

    operational = load_settings(
        EnvironmentConfigSource(
            {
                "AF_APPEND_ONLY_BASE_REF": "base",
                "AF_APPEND_ONLY_HEAD_REF": "head",
                "AF_APPEND_ONLY_BASE_STRATEGY": "merge-base",
                "AF_ROUTER_CLI": "/isolated/af",
            }
        )
    )
    assert operational.settings == Settings()


@pytest.mark.parametrize(
    "values",
    [
        {"port": 0},
        {"selector_confidence_threshold": float("nan")},
        {"app_name": ""},
        {"allowed_hosts": ["not-a-tuple"]},
    ],
)
def test_final_settings_validation_rejects_unsafe_values(values: dict[str, object]) -> None:
    with pytest.raises(PlatformError) as captured:
        load_settings(MappingConfigSource("invalid", values))
    assert captured.value.reason is PlatformReason.CONFIGURATION_INVALID


def test_secret_reference_redacts_key_in_all_public_representations() -> None:
    reference = SecretRef("environment", "VERY_SECRET_KEY")

    assert str(reference) == "[REDACTED]"
    assert "VERY_SECRET_KEY" not in repr(reference)
    assert reference == SecretRef("environment", "VERY_SECRET_KEY")
    assert hash(reference) == hash(SecretRef("environment", "VERY_SECRET_KEY"))
    with pytest.raises(ValueError, match="secret reference"):
        SecretRef("environment", "")


def test_settings_from_environment_uses_layered_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_PORT", "8123")
    monkeypatch.setenv("AF_ALLOW_REMOTE_INFERENCE", "false")
    monkeypatch.setenv("AF_RESOURCE_LEASE_MAINTENANCE_INTERVAL_SECONDS", "2.5")
    configured = Settings.from_environment()
    assert configured.port == 8123
    assert configured.allow_remote_inference is False
    assert configured.resource_lease_maintenance_interval_seconds == 2.5

    @dataclass(frozen=True, slots=True, repr=False)
    class CustomSettings(Settings):
        host: str = "subclass-default.example"

    custom = CustomSettings.from_environment()
    assert isinstance(custom, CustomSettings)
    assert custom.host == "subclass-default.example"


@pytest.mark.parametrize(
    "values",
    [
        {"port": float("nan")},
        {"port": float("inf")},
        {"port": 8123.5},
        {"port": True},
        {"allow_remote_inference": "false"},
        {"allow_remote_inference": 1},
        {"resource_lease_maintenance_interval_seconds": True},
        {"resource_lease_maintenance_interval_seconds": "30"},
        {"resource_lease_maintenance_interval_seconds": float("nan")},
        {"resource_lease_maintenance_interval_seconds": 10**1000},
        {"schedules_path": "\x00private-name"},
        {"schedules_path": 12},
    ],
)
def test_direct_settings_fail_closed_at_the_same_boundary_as_configured_values(values):
    with pytest.raises(ValueError):
        Settings(**values)


def test_snapshot_accepts_existing_settings_without_copying() -> None:
    settings = Settings(port=8765)
    snapshot = ConfigSnapshot.explicit(settings)
    assert snapshot.settings is settings
    assert snapshot.sources == ("explicit",)


def test_config_representations_redact_values_and_identity() -> None:
    settings = Settings(database_url="postgresql://user:password@example/database")
    mapping = MappingConfigSource("safe-source", {"database_url": settings.database_url})
    environment = EnvironmentConfigSource({"AF_DATABASE_URL": settings.database_url})
    snapshot = ConfigSnapshot.explicit(settings)

    for value in (settings, mapping, environment, snapshot):
        assert "password" not in repr(value)


def test_invalid_override_is_sanitized_and_snapshot_construction_validates() -> None:
    with pytest.raises(PlatformError) as override:
        load_settings(overrides={"port": "TOP_SECRET_PORT"})
    assert override.value.reason is PlatformReason.CONFIGURATION_INVALID
    assert "TOP_SECRET_PORT" not in repr(override.value)
    assert "TOP_SECRET_PORT" not in repr(override.value.__context__)

    with pytest.raises(ValueError, match="port"):
        Settings(port=0)


def test_source_name_rejects_log_injection() -> None:
    with pytest.raises(ValueError, match="source name"):
        load_settings(MappingConfigSource("bad\u2028source", {}))


def test_profile_declarations_are_typed_and_all_profiles_are_validated() -> None:
    with pytest.raises(PlatformError) as unknown:
        load_settings(MappingConfigSource("file", {}, profiles={"prodution": {}}))
    assert unknown.value.reason is PlatformReason.CONFIGURATION_PROFILE_UNKNOWN

    with pytest.raises(PlatformError) as malformed:
        load_settings(
            MappingConfigSource("file", {}, profiles={"test": {"port": "private-value"}}),
            profile=ConfigurationProfile.PRODUCTION,
        )
    assert malformed.value.reason is PlatformReason.CONFIGURATION_INVALID
    assert "private-value" not in repr(malformed.value)


def test_snapshot_and_allowlists_are_defensively_normalized_and_validated() -> None:
    sources = ["file"]
    snapshot = ConfigSnapshot(  # type: ignore[arg-type]
        Settings(allowed_hosts=["example.test"]), None, sources
    )
    sources.append("mutated")
    assert snapshot.sources == ("file",)
    assert snapshot.settings.allowed_hosts == ("example.test",)

    with pytest.raises(TypeError, match="profile"):
        ConfigSnapshot(Settings(), "production", ())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="allowed_hosts"):
        Settings(allowed_hosts=["bad\u2028host"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="collection of strings"):
        Settings(allowed_hosts="example.test")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="collection of strings"):
        ConfigSnapshot(Settings(), None, "file")  # type: ignore[arg-type]


def test_sensitive_identity_is_redacted_and_hostile_mappings_are_sanitized() -> None:
    assert "vault-account" not in repr(SecretRef("vault-account", "private-key"))
    assert "private-host" not in repr(Settings(host="private-host"))

    class HostileMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError("private mapping value")

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("private mapping value")

        def __len__(self) -> int:
            return 1

    with pytest.raises(PlatformError) as hostile:
        load_settings(MappingConfigSource("hostile", HostileMapping()))
    assert "private mapping value" not in repr(hostile.value)

    with pytest.raises(PlatformError) as hostile_profile:
        load_settings(profile=object())  # type: ignore[arg-type]
    assert hostile_profile.value.reason is PlatformReason.CONFIGURATION_PROFILE_UNKNOWN
