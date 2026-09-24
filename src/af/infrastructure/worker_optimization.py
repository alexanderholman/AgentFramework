"""Bounded JSON loading for qualified worker optimization profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from af.application.worker_generation import (
    PinnedWorkerOptimizationProvider,
    WorkerRuntimeOptimization,
)
from af.application.worker_qualification import WorkerOptimizationQualification
from af.framework.errors import InvalidProviderConfiguration
from af.framework.managers import CapabilityBinding

__all__ = (
    "load_worker_optimization_profile",
    "record_worker_optimization_profile",
    "worker_queue_storage_identity",
)

_MAX_PROFILE_BYTES = 65_536
_UTC_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")
_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "driver_name", "binding", "qualification", "optimization"}
)
_BINDING_FIELDS = frozenset(
    {
        "driver_version",
        "artifact_digest",
        "dependency_graph_digest",
        "configuration_digest",
        "deployment_profile",
        "policy_version",
    }
)
_OPTIMIZATION_FIELDS = frozenset(
    {
        "worker_count",
        "max_concurrency",
        "poll_interval_ms",
        "heartbeat_interval_ms",
        "max_idle_time_ms",
        "queue_wait_alert_after_ms",
        "renewal_min_interval_ms",
        "renewal_max_interval_ms",
    }
)
_QUALIFICATION_FIELDS = frozenset(
    {
        "evidence_digest",
        "environment_digest",
        "observed_at",
        "expires_at",
        "sample_count",
        "real_service",
        "correctness_failures",
        "settlement_failures",
        "reservation_losses",
    }
)


def _mapping(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"worker optimization {label} fields are invalid")
    return cast(Mapping[str, object], value)


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"worker optimization {field} must be a positive integer")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"worker optimization {field} must be a non-negative integer")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"worker optimization {field} must be a string")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"worker optimization {field} must be a boolean")
    return value


def _timestamp(value: object, field: str) -> datetime:
    raw = _string(value, field)
    if _UTC_TIMESTAMP.fullmatch(raw) is None:
        raise ValueError(f"worker optimization {field} must use canonical UTC")
    parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError(f"worker optimization {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("worker optimization current time must be timezone-aware")
    return current.astimezone(UTC)


def _iso8601(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_duration(value: object, field: str) -> timedelta | None:
    if value is None:
        return None
    return timedelta(milliseconds=_positive_int(value, field))


def _duration_ms(value: timedelta | None, field: str) -> int | None:
    if value is None:
        return None
    microseconds = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    if microseconds <= 0 or microseconds % 1_000 != 0:
        raise ValueError(f"worker optimization {field} must use positive whole milliseconds")
    return microseconds // 1_000


def _require_recordable(
    qualification: WorkerOptimizationQualification,
    *,
    now: datetime,
) -> None:
    if qualification.observed_at > now:
        raise InvalidProviderConfiguration(
            "worker optimization qualification observation is in the future"
        )
    if not qualification.real_service:
        raise InvalidProviderConfiguration(
            "worker optimization requires real-service qualification evidence"
        )
    if qualification.correctness_failures:
        raise InvalidProviderConfiguration(
            "worker optimization qualification has correctness failures"
        )
    if qualification.settlement_failures:
        raise InvalidProviderConfiguration(
            "worker optimization qualification has settlement failures"
        )
    if qualification.reservation_losses:
        raise InvalidProviderConfiguration(
            "worker optimization qualification has reservation losses"
        )
    if qualification.expires_at <= now:
        raise InvalidProviderConfiguration("worker optimization qualification is expired")


def _profile_payload(qualification: WorkerOptimizationQualification) -> dict[str, object]:
    values = qualification.optimization
    return {
        "schema_version": 2,
        "driver_name": qualification.driver_name,
        "binding": asdict(qualification.binding),
        "qualification": {
            "evidence_digest": qualification.evidence_digest,
            "environment_digest": qualification.environment_digest,
            "observed_at": _iso8601(qualification.observed_at),
            "expires_at": _iso8601(qualification.expires_at),
            "sample_count": qualification.sample_count,
            "real_service": qualification.real_service,
            "correctness_failures": qualification.correctness_failures,
            "settlement_failures": qualification.settlement_failures,
            "reservation_losses": qualification.reservation_losses,
        },
        "optimization": {
            "worker_count": values.worker_count,
            "max_concurrency": values.max_concurrency,
            "poll_interval_ms": _duration_ms(values.poll_interval, "poll_interval"),
            "heartbeat_interval_ms": _duration_ms(values.heartbeat_interval, "heartbeat_interval"),
            "max_idle_time_ms": _duration_ms(values.max_idle_time, "max_idle_time"),
            "queue_wait_alert_after_ms": _duration_ms(
                values.queue_wait_alert_after, "queue_wait_alert_after"
            ),
            "renewal_min_interval_ms": _duration_ms(
                values.renewal_min_interval, "renewal_min_interval"
            ),
            "renewal_max_interval_ms": _duration_ms(
                values.renewal_max_interval, "renewal_max_interval"
            ),
        },
    }


def record_worker_optimization_profile(
    path: str | Path,
    qualification: WorkerOptimizationQualification,
    *,
    now: datetime | None = None,
) -> str:
    """Atomically record one exact-binding profile from external measured evidence."""

    try:
        if not isinstance(qualification, WorkerOptimizationQualification):
            raise TypeError("worker optimization qualification is invalid")
        current = _utc(now)
        _require_recordable(qualification, now=current)
        payload = _profile_payload(qualification)
        raw = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(raw) > _MAX_PROFILE_BYTES:
            raise ValueError("worker optimization profile size is invalid")
        profile_path = Path(path)
        parent = profile_path.parent
        if (
            not parent.is_dir()
            or profile_path.is_symlink()
            or (profile_path.exists() and not profile_path.is_file())
        ):
            raise OSError("worker optimization profile target is invalid")
    except InvalidProviderConfiguration:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise InvalidProviderConfiguration(
            "worker optimization profile could not be recorded"
        ) from error

    temporary_path: Path | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{profile_path.name}.", suffix=".tmp", dir=parent
        )
        temporary_path = Path(temporary)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, profile_path)
        temporary_path = None
    except OSError as error:
        raise InvalidProviderConfiguration(
            "worker optimization profile could not be recorded"
        ) from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def _load_profile(path: Path) -> tuple[bytes, Mapping[str, object]]:
    with path.open("rb") as source:
        raw = source.read(_MAX_PROFILE_BYTES + 1)
    if not raw or len(raw) > _MAX_PROFILE_BYTES:
        raise ValueError("worker optimization profile size is invalid")
    document = json.loads(raw.decode("utf-8"))
    return raw, _mapping(document, _TOP_LEVEL_FIELDS, "document")


def load_worker_optimization_profile(
    path: str | Path,
    *,
    now: datetime | None = None,
) -> PinnedWorkerOptimizationProvider:
    """Load one exact-binding profile without exposing file or document details on failure."""

    try:
        profile_path = Path(path)
        raw, document = _load_profile(profile_path)
        if type(document["schema_version"]) is not int or document["schema_version"] != 2:
            raise ValueError("worker optimization schema version is unsupported")
        driver_name = document["driver_name"]
        if not isinstance(driver_name, str):
            raise ValueError("worker optimization driver is invalid")
        binding_values = _mapping(document["binding"], _BINDING_FIELDS, "binding")
        binding = CapabilityBinding(
            driver_version=_string(binding_values["driver_version"], "driver_version"),
            artifact_digest=_string(binding_values["artifact_digest"], "artifact_digest"),
            dependency_graph_digest=_string(
                binding_values["dependency_graph_digest"],
                "dependency_graph_digest",
            ),
            configuration_digest=_string(
                binding_values["configuration_digest"],
                "configuration_digest",
            ),
            deployment_profile=_string(
                binding_values["deployment_profile"],
                "deployment_profile",
            ),
            policy_version=_string(binding_values["policy_version"], "policy_version"),
        )
        values = _mapping(
            document["optimization"],
            _OPTIMIZATION_FIELDS,
            "runtime",
        )
        optimization = WorkerRuntimeOptimization(
            worker_count=_positive_int(values["worker_count"], "worker_count"),
            max_concurrency=_positive_int(values["max_concurrency"], "max_concurrency"),
            poll_interval=timedelta(
                milliseconds=_positive_int(values["poll_interval_ms"], "poll_interval_ms")
            ),
            heartbeat_interval=timedelta(
                milliseconds=_positive_int(values["heartbeat_interval_ms"], "heartbeat_interval_ms")
            ),
            max_idle_time=_optional_duration(values["max_idle_time_ms"], "max_idle_time_ms"),
            queue_wait_alert_after=_optional_duration(
                values["queue_wait_alert_after_ms"],
                "queue_wait_alert_after_ms",
            ),
            renewal_min_interval=timedelta(
                milliseconds=_positive_int(
                    values["renewal_min_interval_ms"], "renewal_min_interval_ms"
                )
            ),
            renewal_max_interval=timedelta(
                milliseconds=_positive_int(
                    values["renewal_max_interval_ms"], "renewal_max_interval_ms"
                )
            ),
        )
        qualification_values = _mapping(
            document["qualification"], _QUALIFICATION_FIELDS, "qualification"
        )
        qualification = WorkerOptimizationQualification(
            driver_name=driver_name,
            binding=binding,
            optimization=optimization,
            evidence_digest=_string(qualification_values["evidence_digest"], "evidence_digest"),
            environment_digest=_string(
                qualification_values["environment_digest"], "environment_digest"
            ),
            observed_at=_timestamp(qualification_values["observed_at"], "observed_at"),
            expires_at=_timestamp(qualification_values["expires_at"], "expires_at"),
            sample_count=_positive_int(qualification_values["sample_count"], "sample_count"),
            real_service=_boolean(qualification_values["real_service"], "real_service"),
            correctness_failures=_non_negative_int(
                qualification_values["correctness_failures"], "correctness_failures"
            ),
            settlement_failures=_non_negative_int(
                qualification_values["settlement_failures"], "settlement_failures"
            ),
            reservation_losses=_non_negative_int(
                qualification_values["reservation_losses"], "reservation_losses"
            ),
        )
        _require_recordable(qualification, now=_utc(now))
        return PinnedWorkerOptimizationProvider(
            driver_name=driver_name,
            binding=binding,
            profile_digest=hashlib.sha256(raw).hexdigest(),
            qualification_digest=qualification.evidence_digest,
            optimization=optimization,
            expires_at=qualification.expires_at,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise InvalidProviderConfiguration("worker optimization profile is invalid") from error


def worker_queue_storage_identity(path: str | Path) -> str:
    """Return a non-reversible identity for the concrete queue storage target."""

    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise InvalidProviderConfiguration("worker queue storage identity is invalid") from error
    return hashlib.sha256(resolved.as_posix().encode("utf-8")).hexdigest()
