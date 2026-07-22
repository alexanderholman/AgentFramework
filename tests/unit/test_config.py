from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError, dataclass

import pytest

from af.config import (
    ConfigSnapshot,
    ConfigurationProfile,
    EnvironmentConfigSource,
    MappingConfigSource,
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
                "AF_SELECTOR_CONFIDENCE_THRESHOLD": "0.65",
            }
        )
    )

    assert snapshot.settings.app_name == "Configured AF"
    assert snapshot.settings.port == 9000
    assert snapshot.settings.allowed_hosts == ("one.example", "two.example")
    assert snapshot.settings.allow_remote_inference is True
    assert snapshot.settings.selector_confidence_threshold == 0.65

    disabled = load_settings(EnvironmentConfigSource({"AF_ALLOW_REMOTE_INFERENCE": "off"}))
    assert disabled.settings.allow_remote_inference is False


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
    assert Settings.from_environment().port == 8123

    @dataclass(frozen=True, slots=True, repr=False)
    class CustomSettings(Settings):
        host: str = "subclass-default.example"

    custom = CustomSettings.from_environment()
    assert isinstance(custom, CustomSettings)
    assert custom.host == "subclass-default.example"


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
