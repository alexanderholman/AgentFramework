"""Bounded, tenant-scoped cache value contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

__all__ = ("CacheKey", "CacheNamespace")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,510}[A-Za-z0-9])?$")
_MAX_VALUE_BYTES = 1_048_576


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"cache {field_name} must be canonical")
    return value


@dataclass(frozen=True, slots=True, order=True, repr=False)
class CacheNamespace:
    """Exact application and tenant boundary for cache operations."""

    application_id: str
    tenant_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "application_id",
            _identifier(self.application_id, "application id"),
        )
        object.__setattr__(self, "tenant_id", _identifier(self.tenant_id, "tenant id"))
        object.__setattr__(self, "name", _identifier(self.name, "namespace"))

    def __repr__(self) -> str:
        return "CacheNamespace(identity='[REDACTED]')"


@dataclass(frozen=True, slots=True, order=True, repr=False)
class CacheKey:
    """One bounded key inside an exact cache namespace."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _identifier(self.value, "key"))

    def __repr__(self) -> str:
        return "CacheKey(value='[REDACTED]')"


def _validated_operation(namespace: object, key: object) -> tuple[CacheNamespace, CacheKey]:
    if not isinstance(namespace, CacheNamespace) or not isinstance(key, CacheKey):
        raise TypeError("cache operation requires validated namespace and key")
    return namespace, key


def _validated_value(value: object) -> bytes:
    if type(value) is not bytes or not value or len(value) > _MAX_VALUE_BYTES:
        raise ValueError("cache value must be bounded non-empty bytes")
    return value


def _validated_ttl(value: object) -> timedelta | None:
    if value is None:
        return None
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError("cache TTL must be a positive duration")
    return value
