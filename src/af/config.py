"""Runtime configuration with environment-variable defaults."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from itertools import islice
from math import isfinite
from typing import cast

from af.framework.errors import InvalidProviderConfiguration, PlatformError, PlatformReason
from af.framework.types import parse_probability

_SCALAR_MAX_LENGTH = 2_048
_COLLECTION_MAX_ITEMS = 256
_MAX_MAINTENANCE_INTERVAL_SECONDS = 86_400.0
_UNSAFE_SINGLE_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class ConfigurationProfile(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class ResourceLeaseBackend(StrEnum):
    MEMORY = "memory"
    SQLITE = "sqlite"


@dataclass(frozen=True, slots=True, repr=False)
class SecretRef:
    provider: str
    key: str

    def __post_init__(self) -> None:
        try:
            _validate_single_line(self.provider, "secret provider")
            _validate_single_line(self.key, "secret key")
        except ValueError:
            raise ValueError("secret reference must use bounded non-empty values") from None

    def __str__(self) -> str:
        return "[REDACTED]"

    def __repr__(self) -> str:
        return "SecretRef(provider='[REDACTED]', key='[REDACTED]')"


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    app_name: str = "AF Agent Framework"
    database_url: str = "sqlite+pysqlite:///storage/af.sqlite3"
    host: str = "127.0.0.1"
    port: int = 8000
    mcp_transport: str = "stdio"
    http_auth_required: bool = False
    auth_tenant_id: str = "local"
    passkey_rp_id: str | None = None
    passkey_origin: str | None = None
    session_cookie_signing_key: str = "af-local-session-signing-key"
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    allow_remote_inference: bool = False
    selector_confidence_threshold: float = 0.8
    decision_enabled: bool = False
    decision_model: str = "jev-1.13.0"
    decision_timeout_seconds: float = 30.0
    decision_token_price_usd: str = "0.000000042"
    selector_provider: str = "disabled"
    selector_model: str | None = None
    selector_endpoint: str = "http://127.0.0.1:11434"
    selector_timeout_seconds: float = 600.0
    resource_lease_backend: ResourceLeaseBackend = ResourceLeaseBackend.MEMORY
    resource_lease_sqlite_path: str = "storage/af-resource-leases.sqlite3"
    resource_lease_maintenance_host_id: str | None = None
    resource_lease_maintenance_interval_seconds: float = 30.0
    schedules_path: str | None = None
    engineering_repository: str | None = None
    inspector_enabled: bool = False
    metrics_enabled: bool = False
    worker_queue_sqlite_path: str | None = None
    worker_optimization_profile_path: str | None = None

    def __post_init__(self) -> None:
        try:
            backend = ResourceLeaseBackend(self.resource_lease_backend)
        except (TypeError, ValueError):
            raise ValueError("resource lease backend is invalid") from None
        object.__setattr__(self, "resource_lease_backend", backend)
        object.__setattr__(
            self, "allowed_hosts", _bounded_string_tuple(self.allowed_hosts, "allowed_hosts")
        )
        object.__setattr__(
            self,
            "allowed_origins",
            _bounded_string_tuple(self.allowed_origins, "allowed_origins"),
        )
        _validate_settings(self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(values='[REDACTED]')"

    @classmethod
    def from_environment(cls) -> Settings:
        """Read AF-prefixed settings without coupling the core to a settings library."""
        defaults = cls()
        inherited_values = {name: getattr(defaults, name) for name in Settings.__dataclass_fields__}
        settings = load_settings(
            MappingConfigSource("class-defaults", inherited_values),
            EnvironmentConfigSource(os.environ),
        ).settings
        return cls(**asdict(settings))


@dataclass(frozen=True, slots=True, repr=False)
class MappingConfigSource:
    name: str
    values: Mapping[str, object]
    profiles: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_source_name(self.name)

    def __repr__(self) -> str:
        return f"MappingConfigSource(name={self.name!r}, values='[REDACTED]')"


@dataclass(frozen=True, slots=True, repr=False)
class EnvironmentConfigSource:
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    name: str = "environment"

    def __post_init__(self) -> None:
        _validate_source_name(self.name)

    def __repr__(self) -> str:
        return f"EnvironmentConfigSource(name={self.name!r}, environ='[REDACTED]')"


@dataclass(frozen=True, slots=True, repr=False)
class ConfigSnapshot:
    settings: Settings
    profile: ConfigurationProfile | None
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.settings, Settings):
            raise TypeError("snapshot settings must be Settings")
        if self.profile is not None and not isinstance(self.profile, ConfigurationProfile):
            raise TypeError("snapshot profile must be a ConfigurationProfile")
        object.__setattr__(self, "sources", _bounded_string_tuple(self.sources, "sources"))
        _validate_settings(self.settings)
        for source in self.sources:
            _validate_source_name(source)

    def __repr__(self) -> str:
        return f"ConfigSnapshot(profile={self.profile!r}, sources={self.sources!r})"

    @classmethod
    def explicit(cls, settings: Settings) -> ConfigSnapshot:
        return cls(settings=settings, profile=None, sources=("explicit",))


_SETTING_NAMES = frozenset(Settings.__dataclass_fields__)
_ENV_NAMES = {
    "AF_APP_NAME": "app_name",
    "AF_DATABASE_URL": "database_url",
    "AF_HOST": "host",
    "AF_PORT": "port",
    "AF_MCP_TRANSPORT": "mcp_transport",
    "AF_HTTP_AUTH_REQUIRED": "http_auth_required",
    "AF_AUTH_TENANT_ID": "auth_tenant_id",
    "AF_PASSKEY_RP_ID": "passkey_rp_id",
    "AF_PASSKEY_ORIGIN": "passkey_origin",
    "AF_SESSION_COOKIE_SIGNING_KEY": "session_cookie_signing_key",
    "AF_ALLOWED_HOSTS": "allowed_hosts",
    "AF_ALLOWED_ORIGINS": "allowed_origins",
    "AF_ALLOW_REMOTE_INFERENCE": "allow_remote_inference",
    "AF_SELECTOR_CONFIDENCE_THRESHOLD": "selector_confidence_threshold",
    "AF_DECISION_ENABLED": "decision_enabled",
    "AF_DECISION_MODEL": "decision_model",
    "AF_DECISION_TIMEOUT_SECONDS": "decision_timeout_seconds",
    "AF_DECISION_TOKEN_PRICE_USD": "decision_token_price_usd",
    "AF_SELECTOR_PROVIDER": "selector_provider",
    "AF_SELECTOR_MODEL": "selector_model",
    "AF_SELECTOR_ENDPOINT": "selector_endpoint",
    "AF_SELECTOR_TIMEOUT_SECONDS": "selector_timeout_seconds",
    "AF_RESOURCE_LEASE_BACKEND": "resource_lease_backend",
    "AF_RESOURCE_LEASE_SQLITE_PATH": "resource_lease_sqlite_path",
    "AF_RESOURCE_LEASE_MAINTENANCE_HOST_ID": "resource_lease_maintenance_host_id",
    "AF_RESOURCE_LEASE_MAINTENANCE_INTERVAL_SECONDS": (
        "resource_lease_maintenance_interval_seconds"
    ),
    "AF_SCHEDULES_PATH": "schedules_path",
    "AF_ENGINEERING_REPOSITORY": "engineering_repository",
    "AF_INSPECTOR_ENABLED": "inspector_enabled",
    "AF_METRICS_ENABLED": "metrics_enabled",
    "AF_WORKER_QUEUE_SQLITE_PATH": "worker_queue_sqlite_path",
    "AF_WORKER_OPTIMIZATION_PROFILE_PATH": "worker_optimization_profile_path",
}
_NON_CONFIG_ENV_NAMES = frozenset(
    {
        "AF_APPEND_ONLY_BASE_REF",
        "AF_APPEND_ONLY_HEAD_REF",
        "AF_APPEND_ONLY_BASE_STRATEGY",
        "AF_ROUTER_CLI",
        "AF_EPIC_MODEL",
        "AF_MACHINE_ROOT",
    }
)


def load_settings(
    *sources: MappingConfigSource | EnvironmentConfigSource,
    profile: ConfigurationProfile | str | None = None,
    overrides: Mapping[str, object] | None = None,
) -> ConfigSnapshot:
    resolved_profile = _resolve_profile(profile)
    values: dict[str, object] = asdict(Settings())
    provenance: list[str] = []
    for source in sources:
        try:
            if isinstance(source, EnvironmentConfigSource):
                unknown_environment = {
                    name
                    for name in source.environ
                    if name.startswith("AF_")
                    and name not in _ENV_NAMES
                    and name not in _NON_CONFIG_ENV_NAMES
                }
                if unknown_environment:
                    raise PlatformError(
                        PlatformReason.CONFIGURATION_INVALID,
                        stage="configuration.environment.keys",
                    )
                patch = {
                    setting: source.environ[name]
                    for name, setting in _ENV_NAMES.items()
                    if name in source.environ
                }
                values.update(_validate_patch(patch, textual=True))
            else:
                values.update(_validate_patch(dict(source.values), textual=False))
                validated_profiles: dict[str, dict[str, object]] = {}
                for declared_profile, profile_values in source.profiles.items():
                    try:
                        known_profile = ConfigurationProfile(declared_profile)
                    except (TypeError, ValueError) as exc:
                        raise PlatformError(
                            PlatformReason.CONFIGURATION_PROFILE_UNKNOWN,
                            stage="configuration.profile.declaration",
                            protected_cause=exc,
                        ) from None
                    validated_profiles[known_profile.value] = _validate_patch(
                        dict(profile_values), textual=False
                    )
                if resolved_profile is not None:
                    values.update(validated_profiles.get(resolved_profile.value, {}))
            provenance.append(source.name)
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(
                PlatformReason.CONFIGURATION_INVALID,
                stage="configuration.source",
                protected_cause=exc,
            ) from None
    if overrides is not None:
        try:
            values.update(_validate_patch(dict(overrides), textual=False))
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(
                PlatformReason.CONFIGURATION_INVALID,
                stage="configuration.overrides",
                protected_cause=exc,
            ) from None
        provenance.append("overrides")
    try:
        settings = Settings(**values)  # type: ignore[arg-type]
        _validate_settings(settings)
    except PlatformError:
        raise
    except Exception as exc:
        raise PlatformError(
            PlatformReason.CONFIGURATION_INVALID,
            stage="configuration.snapshot",
            protected_cause=exc,
        ) from None
    return ConfigSnapshot(settings, resolved_profile, tuple(provenance))


def _resolve_profile(profile: ConfigurationProfile | str | None) -> ConfigurationProfile | None:
    if profile is None or isinstance(profile, ConfigurationProfile):
        return profile
    if type(profile) is not str:
        raise PlatformError(
            PlatformReason.CONFIGURATION_PROFILE_UNKNOWN,
            stage="configuration.profile",
        )
    try:
        return ConfigurationProfile(profile)
    except Exception as exc:
        raise PlatformError(
            PlatformReason.CONFIGURATION_PROFILE_UNKNOWN,
            stage="configuration.profile",
            protected_cause=exc,
        ) from None


def _validate_patch(values: Mapping[str, object], *, textual: bool) -> dict[str, object]:
    unknown = set(values) - _SETTING_NAMES
    if unknown:
        raise PlatformError(
            PlatformReason.CONFIGURATION_INVALID,
            stage="configuration.keys",
        )
    parsed: dict[str, object] = {}
    for name, value in values.items():
        if isinstance(value, SecretRef):
            raise PlatformError(
                PlatformReason.CONFIGURATION_SECRET_UNRESOLVED,
                stage="configuration.secret",
            )
        parsed[name] = _parse_value(name, value, textual=textual)
    return parsed


def _parse_value(name: str, value: object, *, textual: bool) -> object:
    if name == "port":
        if textual and isinstance(value, str):
            if re.fullmatch(r"[+-]?\d+", value) is None:
                raise ValueError("port is invalid")
            return int(value)
        if type(value) is not int:
            raise ValueError("port must be an integer")
        return value
    if name in {
        "selector_confidence_threshold",
        "selector_timeout_seconds",
        "decision_timeout_seconds",
        "resource_lease_maintenance_interval_seconds",
    }:
        if textual and isinstance(value, str):
            if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value) is None:
                raise ValueError("threshold is invalid")
            return float(value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("threshold must be numeric")
        return float(value)
    if (
        name
        in {
            "resource_lease_maintenance_host_id",
            "selector_model",
            "passkey_rp_id",
            "passkey_origin",
            "schedules_path",
            "engineering_repository",
            "worker_queue_sqlite_path",
            "worker_optimization_profile_path",
        }
        and value is None
    ):
        return None
    if name in {
        "allow_remote_inference",
        "decision_enabled",
        "http_auth_required",
        "inspector_enabled",
        "metrics_enabled",
    }:
        if textual and isinstance(value, str):
            normalized = value.lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError("boolean is invalid")
        if type(value) is not bool:
            raise ValueError("remote inference setting must be boolean")
        return value
    if name in {"allowed_hosts", "allowed_origins"}:
        if textual and isinstance(value, str):
            return _split_csv(value)
        if not isinstance(value, tuple):
            raise ValueError(f"{name} must be a tuple of strings")
        if len(value) > _COLLECTION_MAX_ITEMS:
            raise ValueError(f"{name} contains too many values")
        for item in value:
            _validate_single_line(item, name)
        return value
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _validate_settings(settings: Settings) -> None:
    if settings.selector_provider not in {"disabled", "ollama", "openai"}:
        raise ValueError("selector provider is unsupported")
    if settings.selector_provider != "disabled" and settings.selector_model is None:
        raise ValueError("enabled selector requires an explicit model")
    if settings.selector_model is not None:
        _validate_single_line(settings.selector_model, "selector_model")
    _validate_single_line(settings.selector_endpoint, "selector_endpoint")
    if (
        type(settings.selector_timeout_seconds) not in {int, float}
        or not 0 < settings.selector_timeout_seconds <= 600
    ):
        raise ValueError("selector timeout must be positive and at most ten minutes")
    if type(settings.http_auth_required) is not bool:
        raise ValueError("HTTP authentication required must be boolean")
    if type(settings.inspector_enabled) is not bool or type(settings.metrics_enabled) is not bool:
        raise ValueError("observability settings must be boolean")
    _validate_single_line(settings.auth_tenant_id, "auth_tenant_id")
    if (settings.passkey_rp_id is None) != (settings.passkey_origin is None):
        raise ValueError("passkeys require both a relying-party ID and an exact origin")
    if settings.passkey_rp_id is not None:
        _validate_single_line(settings.passkey_rp_id, "passkey_rp_id")
    if settings.passkey_origin is not None:
        _validate_single_line(settings.passkey_origin, "passkey_origin")
    if settings.engineering_repository is not None:
        _validate_single_line(settings.engineering_repository, "engineering_repository")
    if type(settings.decision_enabled) is not bool:
        raise ValueError("decision enabled must be boolean")
    _validate_single_line(settings.decision_model, "decision_model")
    _validate_single_line(settings.decision_token_price_usd, "decision_token_price_usd")
    if (
        type(settings.decision_timeout_seconds) not in {int, float}
        or not 0 < settings.decision_timeout_seconds <= 600
    ):
        raise ValueError("decision timeout must be positive and at most ten minutes")
    if settings.port < 1 or settings.port > 65535:
        raise ValueError("port is outside its supported range")
    threshold = settings.selector_confidence_threshold
    if parse_probability(threshold) is None:
        raise InvalidProviderConfiguration(
            "selector confidence threshold must be a finite probability in [0, 1]"
        )
    maintenance_interval = settings.resource_lease_maintenance_interval_seconds
    if (
        not isfinite(maintenance_interval)
        or maintenance_interval <= 0
        or maintenance_interval > _MAX_MAINTENANCE_INTERVAL_SECONDS
    ):
        raise ValueError("resource lease maintenance interval is outside its supported range")
    for name in (
        "app_name",
        "database_url",
        "host",
        "mcp_transport",
        "session_cookie_signing_key",
        "resource_lease_sqlite_path",
    ):
        value = getattr(settings, name)
        _validate_single_line(value, name)
    if (
        settings.resource_lease_backend is ResourceLeaseBackend.SQLITE
        and settings.resource_lease_sqlite_path == ":memory:"
    ):
        raise ValueError("durable SQLite resource leases require a filesystem path")
    if settings.resource_lease_maintenance_host_id is not None:
        _validate_single_line(
            settings.resource_lease_maintenance_host_id,
            "resource_lease_maintenance_host_id",
        )
    worker_paths = (
        settings.worker_queue_sqlite_path,
        settings.worker_optimization_profile_path,
    )
    if (worker_paths[0] is None) != (worker_paths[1] is None):
        raise ValueError("worker queue and optimization profile must be configured together")
    for name, value in zip(
        ("worker_queue_sqlite_path", "worker_optimization_profile_path"),
        worker_paths,
        strict=True,
    ):
        if value is not None:
            _validate_single_line(value, name)
    if settings.worker_queue_sqlite_path == ":memory:":
        raise ValueError("worker SQLite queue requires a filesystem path")
    for name in ("allowed_hosts", "allowed_origins"):
        values = getattr(settings, name)
        if len(values) > _COLLECTION_MAX_ITEMS:
            raise ValueError(f"{name} contains too many values")
        for value in values:
            _validate_single_line(value, name)


def _split_csv(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(items) > _COLLECTION_MAX_ITEMS:
        raise ValueError("configuration collection contains too many values")
    for item in items:
        _validate_single_line(item, "configuration collection value")
    return items


def _validate_single_line(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _SCALAR_MAX_LENGTH
        or _UNSAFE_SINGLE_LINE.search(value)
    ):
        raise ValueError(f"{label} must be a bounded non-empty single-line value")


def _bounded_string_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a collection of strings")
    try:
        items = tuple(islice(iter(cast(Iterable[object], value)), _COLLECTION_MAX_ITEMS + 1))
    except TypeError as exc:
        raise ValueError(f"{label} must be a collection of strings") from exc
    if len(items) > _COLLECTION_MAX_ITEMS:
        raise ValueError(f"{label} contains too many values")
    for item in items:
        _validate_single_line(item, label)
    return cast(tuple[str, ...], items)


def _validate_source_name(name: str) -> None:
    if not name or len(name) > 200 or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", name):
        raise ValueError("source name must be a bounded non-empty single-line value")
