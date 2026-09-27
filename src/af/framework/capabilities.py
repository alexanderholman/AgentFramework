"""Provider-neutral capability and request-requirement contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter

__all__ = (
    "CapabilityCandidate",
    "CapabilityClass",
    "CapabilityDescriptor",
    "CapabilityEffect",
    "CapabilityLifecycle",
    "CapabilityLocality",
    "CapabilityReasonCode",
    "CapabilityResourceEnvelope",
    "CapabilityRisk",
    "CapabilityRoute",
    "ExactCapabilityMatcher",
    "RequestRequirements",
)

_SCHEMA_VERSION = "1.0.0"
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_LICENCE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,126}[A-Za-z0-9])?$")
_MAX_BYTES = 1_073_741_824
_MAX_CONTEXT_ITEMS = 1_000_000
_MAX_RESOURCE_VALUE = 1_000_000_000
_RISK_ORDER = {
    "none": 0,
    "low": 1,
    "elevated": 2,
    "high": 3,
    "critical": 4,
}


class CapabilityClass(StrEnum):
    STATIC = "static"
    PROCEDURAL = "procedural"
    FUNCTION_SYSTEM = "function_system"
    MODEL = "model"
    WORKFLOW = "workflow"
    GENERATIVE = "generative"
    HUMAN = "human"


class CapabilityLifecycle(StrEnum):
    DECLARED = "declared"
    ACQUIRED = "acquired"
    RUNTIME_READY = "runtime_ready"
    BENCHMARKED = "benchmarked"
    DEPLOYED = "deployed"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    REVOKED = "revoked"


class CapabilityReasonCode(StrEnum):
    MATCH = "match"
    PARTIAL_MATCH = "partial_match"
    EXCESSIVE_COST = "excessive_cost"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"
    PROHIBITED = "prohibited"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class CapabilityEffect(StrEnum):
    READ_ONLY = "read_only"
    SIDE_EFFECT = "side_effect"
    EXTERNAL_EFFECT = "external_effect"


class CapabilityRisk(StrEnum):
    NONE = "none"
    LOW = "low"
    ELEVATED = "elevated"
    HIGH = "high"
    CRITICAL = "critical"


class CapabilityLocality(StrEnum):
    LOCAL_ONLY = "local_only"
    REMOTE_ALLOWED = "remote_allowed"
    EXTERNAL_ONLY = "external_only"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _check_fields(payload: Mapping[str, object], expected: frozenset[str], name: str) -> None:
    unknown = sorted(set(payload) - expected)
    if unknown:
        raise ValueError(f"{name} contains unknown fields")
    missing = sorted(expected - set(payload))
    if missing:
        raise ValueError(f"{name} is missing required fields")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    if not isinstance(value, tuple | list):
        raise ValueError(f"{field_name} must be a bounded sequence")
    return tuple(value)


def _identifier(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical identifier")
    return value


def _bounded_text(value: object, field_name: str, *, maximum: int = 127) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError(f"{field_name} must be bounded non-empty text")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


def _language(value: object, field_name: str) -> str:
    text = _bounded_text(value, field_name, maximum=63)
    if _LANGUAGE.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a language or locale tag")
    return text


def _licence(value: object, field_name: str) -> str:
    text = _bounded_text(value, field_name)
    if _LICENCE.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a bounded licence token")
    return text


def _positive_int(value: object, field_name: str, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{field_name} is invalid")
    return value


def _non_negative_int(value: object, field_name: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{field_name} is invalid")
    return value


def _bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be boolean")
    return value


def _string(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be text")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _string(value, field_name)


def _enum_tuple[T: StrEnum](
    value: object,
    enum_type: type[T],
    field_name: str,
) -> tuple[T, ...]:
    items = _sequence(value, field_name)
    if not items:
        raise ValueError(f"{field_name} must not be empty")
    normalized: list[T] = []
    for item in items:
        if isinstance(item, enum_type):
            normalized_item = item
        else:
            try:
                normalized_item = enum_type(_string(item, field_name))
            except ValueError as error:
                raise ValueError(f"{field_name} contains an invalid value") from error
        normalized.append(normalized_item)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must be unique")
    return tuple(sorted(normalized, key=lambda item: item.value))


def _text_tuple(
    value: object,
    field_name: str,
    *,
    validator: Callable[[object, str], str] = _identifier,
    maximum_items: int = 64,
) -> tuple[str, ...]:
    items = _sequence(value, field_name)
    if len(items) > maximum_items:
        raise ValueError(f"{field_name} must be bounded")
    normalized = [validator(item, field_name) for item in items]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must be unique")
    return tuple(sorted(normalized))


def _risk_rank(value: CapabilityRisk) -> int:
    return _RISK_ORDER[value.value]


@dataclass(frozen=True, slots=True)
class CapabilityResourceEnvelope:
    latency_ms: int
    memory_mib: int
    cpu_millis: int
    accelerator_millis: int
    estimated_cost_micros: int
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("capability resource schema version is invalid")
        object.__setattr__(
            self,
            "latency_ms",
            _non_negative_int(self.latency_ms, "capability latency", maximum=_MAX_RESOURCE_VALUE),
        )
        object.__setattr__(
            self,
            "memory_mib",
            _non_negative_int(self.memory_mib, "capability memory", maximum=_MAX_RESOURCE_VALUE),
        )
        object.__setattr__(
            self,
            "cpu_millis",
            _non_negative_int(self.cpu_millis, "capability cpu", maximum=_MAX_RESOURCE_VALUE),
        )
        object.__setattr__(
            self,
            "accelerator_millis",
            _non_negative_int(
                self.accelerator_millis,
                "capability accelerator",
                maximum=_MAX_RESOURCE_VALUE,
            ),
        )
        object.__setattr__(
            self,
            "estimated_cost_micros",
            _non_negative_int(
                self.estimated_cost_micros,
                "capability estimated cost",
                maximum=_MAX_RESOURCE_VALUE,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "accelerator_millis": self.accelerator_millis,
            "cpu_millis": self.cpu_millis,
            "estimated_cost_micros": self.estimated_cost_micros,
            "latency_ms": self.latency_ms,
            "memory_mib": self.memory_mib,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityResourceEnvelope:
        data = _mapping(payload, "capability resources")
        _check_fields(
            data,
            frozenset(
                {
                    "accelerator_millis",
                    "cpu_millis",
                    "estimated_cost_micros",
                    "latency_ms",
                    "memory_mib",
                    "schema_version",
                }
            ),
            "capability resources",
        )
        return cls(
            latency_ms=_non_negative_int(
                data["latency_ms"],
                "capability latency",
                maximum=_MAX_RESOURCE_VALUE,
            ),
            memory_mib=_non_negative_int(
                data["memory_mib"],
                "capability memory",
                maximum=_MAX_RESOURCE_VALUE,
            ),
            cpu_millis=_non_negative_int(
                data["cpu_millis"],
                "capability cpu",
                maximum=_MAX_RESOURCE_VALUE,
            ),
            accelerator_millis=_non_negative_int(
                data["accelerator_millis"],
                "capability accelerator",
                maximum=_MAX_RESOURCE_VALUE,
            ),
            estimated_cost_micros=_non_negative_int(
                data["estimated_cost_micros"],
                "capability estimated cost",
                maximum=_MAX_RESOURCE_VALUE,
            ),
            schema_version=_string(data["schema_version"], "capability resource schema version"),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    capability_id: str
    asset_id: str
    provider_id: str
    runtime_id: str
    deployment_id: str | None
    procedure_id: str | None
    workflow_id: str | None
    capability_class: CapabilityClass
    modality: str
    domain: str
    intent: str
    input_schema: str
    output_schema: str
    languages: tuple[str, ...]
    locales: tuple[str, ...]
    max_input_bytes: int
    max_output_bytes: int
    max_context_items: int
    declared_quality_evidence: tuple[str, ...]
    verifier_requirements: tuple[str, ...]
    effects: tuple[CapabilityEffect, ...]
    risk: CapabilityRisk
    locality: CapabilityLocality
    licence: str
    declared_state: CapabilityLifecycle
    resource_envelope: CapabilityResourceEnvelope
    dependency_ids: tuple[str, ...] = ()
    fallback_ids: tuple[str, ...] = ()
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("capability descriptor schema version is invalid")
        object.__setattr__(
            self,
            "capability_id",
            _identifier(self.capability_id, "capability identifier"),
        )
        object.__setattr__(self, "asset_id", _identifier(self.asset_id, "asset identifier"))
        object.__setattr__(
            self,
            "provider_id",
            _identifier(self.provider_id, "provider identifier"),
        )
        object.__setattr__(
            self,
            "runtime_id",
            _identifier(self.runtime_id, "runtime identifier"),
        )
        if self.deployment_id is not None:
            object.__setattr__(
                self,
                "deployment_id",
                _identifier(self.deployment_id, "deployment identifier"),
            )
        if self.procedure_id is not None:
            object.__setattr__(
                self,
                "procedure_id",
                _identifier(self.procedure_id, "procedure identifier"),
            )
        if self.workflow_id is not None:
            object.__setattr__(
                self,
                "workflow_id",
                _identifier(self.workflow_id, "workflow identifier"),
            )
        if not isinstance(self.capability_class, CapabilityClass):
            raise ValueError("capability class is invalid")
        object.__setattr__(self, "modality", _identifier(self.modality, "capability modality"))
        object.__setattr__(self, "domain", _identifier(self.domain, "capability domain"))
        object.__setattr__(self, "intent", _identifier(self.intent, "capability intent"))
        object.__setattr__(
            self,
            "input_schema",
            _identifier(self.input_schema, "capability input schema"),
        )
        object.__setattr__(
            self,
            "output_schema",
            _identifier(self.output_schema, "capability output schema"),
        )
        object.__setattr__(
            self,
            "languages",
            _text_tuple(self.languages, "capability languages", validator=_language),
        )
        object.__setattr__(
            self,
            "locales",
            _text_tuple(self.locales, "capability locales", validator=_language),
        )
        object.__setattr__(
            self,
            "max_input_bytes",
            _positive_int(self.max_input_bytes, "capability input bytes", maximum=_MAX_BYTES),
        )
        object.__setattr__(
            self,
            "max_output_bytes",
            _positive_int(self.max_output_bytes, "capability output bytes", maximum=_MAX_BYTES),
        )
        object.__setattr__(
            self,
            "max_context_items",
            _positive_int(
                self.max_context_items,
                "capability context items",
                maximum=_MAX_CONTEXT_ITEMS,
            ),
        )
        object.__setattr__(
            self,
            "declared_quality_evidence",
            _text_tuple(self.declared_quality_evidence, "declared quality evidence"),
        )
        object.__setattr__(
            self,
            "verifier_requirements",
            _text_tuple(self.verifier_requirements, "verifier requirements"),
        )
        object.__setattr__(
            self,
            "effects",
            _enum_tuple(self.effects, CapabilityEffect, "capability effects"),
        )
        if not isinstance(self.risk, CapabilityRisk):
            raise ValueError("capability risk is invalid")
        if not isinstance(self.locality, CapabilityLocality):
            raise ValueError("capability locality is invalid")
        object.__setattr__(self, "licence", _licence(self.licence, "capability licence"))
        if not isinstance(self.declared_state, CapabilityLifecycle):
            raise ValueError("capability lifecycle is invalid")
        if not isinstance(self.resource_envelope, CapabilityResourceEnvelope):
            raise ValueError("capability resource envelope is invalid")
        object.__setattr__(
            self,
            "dependency_ids",
            _text_tuple(self.dependency_ids, "capability dependencies"),
        )
        object.__setattr__(
            self,
            "fallback_ids",
            _text_tuple(self.fallback_ids, "capability fallbacks"),
        )
        if self.capability_id in self.dependency_ids or self.capability_id in self.fallback_ids:
            raise ValueError("capability relationships must not reference self")
        if set(self.dependency_ids) & set(self.fallback_ids):
            raise ValueError("capability relationships must not overlap")
        if self.capability_class is CapabilityClass.PROCEDURAL:
            if self.procedure_id is None or self.workflow_id is not None:
                raise ValueError("procedural capabilities require a procedure and no workflow")
        elif self.capability_class is CapabilityClass.WORKFLOW:
            if self.workflow_id is None or self.procedure_id is not None:
                raise ValueError("workflow capabilities require a workflow and no procedure")
        else:
            if self.procedure_id is not None and self.workflow_id is not None:
                raise ValueError("capability cannot bind both procedure and workflow")

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "capability_class": self.capability_class.value,
            "capability_id": self.capability_id,
            "declared_quality_evidence": list(self.declared_quality_evidence),
            "declared_state": self.declared_state.value,
            "dependency_ids": list(self.dependency_ids),
            "deployment_id": self.deployment_id,
            "domain": self.domain,
            "effects": [item.value for item in self.effects],
            "fallback_ids": list(self.fallback_ids),
            "input_schema": self.input_schema,
            "intent": self.intent,
            "languages": list(self.languages),
            "licence": self.licence,
            "locales": list(self.locales),
            "locality": self.locality.value,
            "max_context_items": self.max_context_items,
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "modality": self.modality,
            "output_schema": self.output_schema,
            "procedure_id": self.procedure_id,
            "provider_id": self.provider_id,
            "resource_envelope": self.resource_envelope.to_dict(),
            "risk": self.risk.value,
            "runtime_id": self.runtime_id,
            "schema_version": self.schema_version,
            "verifier_requirements": list(self.verifier_requirements),
            "workflow_id": self.workflow_id,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityDescriptor:
        data = _mapping(payload, "capability descriptor")
        _check_fields(
            data,
            frozenset(
                {
                    "asset_id",
                    "capability_class",
                    "capability_id",
                    "declared_quality_evidence",
                    "declared_state",
                    "dependency_ids",
                    "deployment_id",
                    "domain",
                    "effects",
                    "fallback_ids",
                    "input_schema",
                    "intent",
                    "languages",
                    "licence",
                    "locales",
                    "locality",
                    "max_context_items",
                    "max_input_bytes",
                    "max_output_bytes",
                    "modality",
                    "output_schema",
                    "procedure_id",
                    "provider_id",
                    "resource_envelope",
                    "risk",
                    "runtime_id",
                    "schema_version",
                    "verifier_requirements",
                    "workflow_id",
                }
            ),
            "capability descriptor",
        )
        return cls(
            capability_id=_string(data["capability_id"], "capability identifier"),
            asset_id=_string(data["asset_id"], "asset identifier"),
            provider_id=_string(data["provider_id"], "provider identifier"),
            runtime_id=_string(data["runtime_id"], "runtime identifier"),
            deployment_id=_optional_string(data["deployment_id"], "deployment identifier"),
            procedure_id=_optional_string(data["procedure_id"], "procedure identifier"),
            workflow_id=_optional_string(data["workflow_id"], "workflow identifier"),
            capability_class=CapabilityClass(_string(data["capability_class"], "capability class")),
            modality=_string(data["modality"], "capability modality"),
            domain=_string(data["domain"], "capability domain"),
            intent=_string(data["intent"], "capability intent"),
            input_schema=_string(data["input_schema"], "capability input schema"),
            output_schema=_string(data["output_schema"], "capability output schema"),
            languages=tuple(
                _string(item, "capability languages")
                for item in _sequence(data["languages"], "capability languages")
            ),
            locales=tuple(
                _string(item, "capability locales")
                for item in _sequence(data["locales"], "capability locales")
            ),
            max_input_bytes=_positive_int(
                data["max_input_bytes"],
                "capability input bytes",
                maximum=_MAX_BYTES,
            ),
            max_output_bytes=_positive_int(
                data["max_output_bytes"],
                "capability output bytes",
                maximum=_MAX_BYTES,
            ),
            max_context_items=_positive_int(
                data["max_context_items"],
                "capability context items",
                maximum=_MAX_CONTEXT_ITEMS,
            ),
            declared_quality_evidence=tuple(
                _string(item, "declared quality evidence")
                for item in _sequence(
                    data["declared_quality_evidence"],
                    "declared quality evidence",
                )
            ),
            verifier_requirements=tuple(
                _string(item, "verifier requirements")
                for item in _sequence(data["verifier_requirements"], "verifier requirements")
            ),
            effects=tuple(
                CapabilityEffect(_string(item, "capability effects"))
                for item in _sequence(data["effects"], "capability effects")
            ),
            risk=CapabilityRisk(_string(data["risk"], "capability risk")),
            locality=CapabilityLocality(_string(data["locality"], "capability locality")),
            licence=_string(data["licence"], "capability licence"),
            declared_state=CapabilityLifecycle(
                _string(data["declared_state"], "capability lifecycle")
            ),
            resource_envelope=CapabilityResourceEnvelope.from_dict(data["resource_envelope"]),
            dependency_ids=tuple(
                _string(item, "capability dependencies")
                for item in _sequence(data["dependency_ids"], "capability dependencies")
            ),
            fallback_ids=tuple(
                _string(item, "capability fallbacks")
                for item in _sequence(data["fallback_ids"], "capability fallbacks")
            ),
            schema_version=_string(data["schema_version"], "capability descriptor schema version"),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class RequestRequirements:
    request_id: str
    capability_classes: tuple[CapabilityClass, ...]
    modality: str
    domain: str
    intent: str
    input_schema: str
    output_schema: str
    language: str
    locale: str
    input_bytes: int
    expected_output_bytes: int
    context_items: int
    required_verifiers: tuple[str, ...]
    allowed_effects: tuple[CapabilityEffect, ...]
    max_risk: CapabilityRisk
    accepted_localities: tuple[CapabilityLocality, ...]
    accepted_licenses: tuple[str, ...]
    accepted_lifecycles: tuple[CapabilityLifecycle, ...]
    require_authorized: bool
    require_verified_quality: bool
    max_resources: CapabilityResourceEnvelope
    required_runtime_id: str | None = None
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("request requirements schema version is invalid")
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request identifier"))
        object.__setattr__(
            self,
            "capability_classes",
            _enum_tuple(
                self.capability_classes,
                CapabilityClass,
                "request capability classes",
            ),
        )
        object.__setattr__(self, "modality", _identifier(self.modality, "request modality"))
        object.__setattr__(self, "domain", _identifier(self.domain, "request domain"))
        object.__setattr__(self, "intent", _identifier(self.intent, "request intent"))
        object.__setattr__(
            self,
            "input_schema",
            _identifier(self.input_schema, "request input schema"),
        )
        object.__setattr__(
            self,
            "output_schema",
            _identifier(self.output_schema, "request output schema"),
        )
        object.__setattr__(self, "language", _language(self.language, "request language"))
        object.__setattr__(self, "locale", _language(self.locale, "request locale"))
        object.__setattr__(
            self,
            "input_bytes",
            _positive_int(self.input_bytes, "request input bytes", maximum=_MAX_BYTES),
        )
        object.__setattr__(
            self,
            "expected_output_bytes",
            _positive_int(
                self.expected_output_bytes,
                "request output bytes",
                maximum=_MAX_BYTES,
            ),
        )
        object.__setattr__(
            self,
            "context_items",
            _positive_int(self.context_items, "request context items", maximum=_MAX_CONTEXT_ITEMS),
        )
        object.__setattr__(
            self,
            "required_verifiers",
            _text_tuple(self.required_verifiers, "request verifiers"),
        )
        object.__setattr__(
            self,
            "allowed_effects",
            _enum_tuple(self.allowed_effects, CapabilityEffect, "request allowed effects"),
        )
        if not isinstance(self.max_risk, CapabilityRisk):
            raise ValueError("request max risk is invalid")
        object.__setattr__(
            self,
            "accepted_localities",
            _enum_tuple(
                self.accepted_localities,
                CapabilityLocality,
                "request accepted localities",
            ),
        )
        object.__setattr__(
            self,
            "accepted_licenses",
            _text_tuple(self.accepted_licenses, "request accepted licenses", validator=_licence),
        )
        object.__setattr__(
            self,
            "accepted_lifecycles",
            _enum_tuple(
                self.accepted_lifecycles,
                CapabilityLifecycle,
                "request accepted lifecycles",
            ),
        )
        object.__setattr__(
            self,
            "require_authorized",
            _bool(self.require_authorized, "request authorization requirement"),
        )
        object.__setattr__(
            self,
            "require_verified_quality",
            _bool(self.require_verified_quality, "request verified quality requirement"),
        )
        if not isinstance(self.max_resources, CapabilityResourceEnvelope):
            raise ValueError("request max resources are invalid")
        if self.required_runtime_id is not None:
            object.__setattr__(
                self,
                "required_runtime_id",
                _identifier(self.required_runtime_id, "request runtime identifier"),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted_lifecycles": [item.value for item in self.accepted_lifecycles],
            "accepted_licenses": list(self.accepted_licenses),
            "accepted_localities": [item.value for item in self.accepted_localities],
            "allowed_effects": [item.value for item in self.allowed_effects],
            "capability_classes": [item.value for item in self.capability_classes],
            "context_items": self.context_items,
            "domain": self.domain,
            "expected_output_bytes": self.expected_output_bytes,
            "input_bytes": self.input_bytes,
            "input_schema": self.input_schema,
            "intent": self.intent,
            "language": self.language,
            "locale": self.locale,
            "max_resources": self.max_resources.to_dict(),
            "max_risk": self.max_risk.value,
            "modality": self.modality,
            "output_schema": self.output_schema,
            "request_id": self.request_id,
            "require_authorized": self.require_authorized,
            "require_verified_quality": self.require_verified_quality,
            "required_runtime_id": self.required_runtime_id,
            "required_verifiers": list(self.required_verifiers),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RequestRequirements:
        data = _mapping(payload, "request requirements")
        _check_fields(
            data,
            frozenset(
                {
                    "accepted_lifecycles",
                    "accepted_licenses",
                    "accepted_localities",
                    "allowed_effects",
                    "capability_classes",
                    "context_items",
                    "domain",
                    "expected_output_bytes",
                    "input_bytes",
                    "input_schema",
                    "intent",
                    "language",
                    "locale",
                    "max_resources",
                    "max_risk",
                    "modality",
                    "output_schema",
                    "request_id",
                    "require_authorized",
                    "require_verified_quality",
                    "required_runtime_id",
                    "required_verifiers",
                    "schema_version",
                }
            ),
            "request requirements",
        )
        return cls(
            request_id=_string(data["request_id"], "request identifier"),
            capability_classes=tuple(
                CapabilityClass(_string(item, "request capability classes"))
                for item in _sequence(data["capability_classes"], "request capability classes")
            ),
            modality=_string(data["modality"], "request modality"),
            domain=_string(data["domain"], "request domain"),
            intent=_string(data["intent"], "request intent"),
            input_schema=_string(data["input_schema"], "request input schema"),
            output_schema=_string(data["output_schema"], "request output schema"),
            language=_string(data["language"], "request language"),
            locale=_string(data["locale"], "request locale"),
            input_bytes=_positive_int(
                data["input_bytes"],
                "request input bytes",
                maximum=_MAX_BYTES,
            ),
            expected_output_bytes=_positive_int(
                data["expected_output_bytes"],
                "request output bytes",
                maximum=_MAX_BYTES,
            ),
            context_items=_positive_int(
                data["context_items"],
                "request context items",
                maximum=_MAX_CONTEXT_ITEMS,
            ),
            required_verifiers=tuple(
                _string(item, "request verifiers")
                for item in _sequence(data["required_verifiers"], "request verifiers")
            ),
            allowed_effects=tuple(
                CapabilityEffect(_string(item, "request allowed effects"))
                for item in _sequence(data["allowed_effects"], "request allowed effects")
            ),
            max_risk=CapabilityRisk(_string(data["max_risk"], "request max risk")),
            accepted_localities=tuple(
                CapabilityLocality(_string(item, "request accepted localities"))
                for item in _sequence(data["accepted_localities"], "request accepted localities")
            ),
            accepted_licenses=tuple(
                _string(item, "request accepted licenses")
                for item in _sequence(data["accepted_licenses"], "request accepted licenses")
            ),
            accepted_lifecycles=tuple(
                CapabilityLifecycle(_string(item, "request accepted lifecycles"))
                for item in _sequence(data["accepted_lifecycles"], "request accepted lifecycles")
            ),
            require_authorized=_bool(
                data["require_authorized"],
                "request authorization requirement",
            ),
            require_verified_quality=_bool(
                data["require_verified_quality"],
                "request verified quality requirement",
            ),
            max_resources=CapabilityResourceEnvelope.from_dict(data["max_resources"]),
            required_runtime_id=_optional_string(
                data["required_runtime_id"],
                "request runtime identifier",
            ),
            schema_version=_string(data["schema_version"], "request requirements schema version"),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapabilityCandidate:
    descriptor: CapabilityDescriptor
    measured_state: CapabilityLifecycle
    quality_verified: bool
    authorized: bool
    measured_quality_evidence: tuple[str, ...]
    estimated_resources: CapabilityResourceEnvelope
    reason: CapabilityReasonCode
    exact_match: bool
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("capability candidate schema version is invalid")
        if not isinstance(self.descriptor, CapabilityDescriptor):
            raise ValueError("capability candidate descriptor is invalid")
        if not isinstance(self.measured_state, CapabilityLifecycle):
            raise ValueError("capability candidate measured state is invalid")
        object.__setattr__(
            self,
            "quality_verified",
            _bool(self.quality_verified, "capability verified quality"),
        )
        object.__setattr__(self, "authorized", _bool(self.authorized, "capability authorization"))
        object.__setattr__(
            self,
            "measured_quality_evidence",
            _text_tuple(
                self.measured_quality_evidence,
                "capability measured quality evidence",
                maximum_items=64,
            )
            if self.measured_quality_evidence
            else (),
        )
        if not isinstance(self.estimated_resources, CapabilityResourceEnvelope):
            raise ValueError("capability candidate resources are invalid")
        if not isinstance(self.reason, CapabilityReasonCode):
            raise ValueError("capability candidate reason is invalid")
        object.__setattr__(self, "exact_match", _bool(self.exact_match, "capability exact match"))
        if self.exact_match and self.reason is not CapabilityReasonCode.MATCH:
            raise ValueError("capability exact match requires the match reason")
        if self.reason is CapabilityReasonCode.MATCH and not self.exact_match:
            raise ValueError("capability match reason requires exact match")
        if self.quality_verified:
            if not self.measured_quality_evidence:
                raise ValueError("capability verified quality requires measured evidence")
            if self.measured_state not in {
                CapabilityLifecycle.BENCHMARKED,
                CapabilityLifecycle.DEPLOYED,
                CapabilityLifecycle.DEGRADED,
            }:
                raise ValueError("capability verified quality requires measured capability state")

    def to_dict(self) -> dict[str, object]:
        return {
            "authorized": self.authorized,
            "descriptor": self.descriptor.to_dict(),
            "estimated_resources": self.estimated_resources.to_dict(),
            "exact_match": self.exact_match,
            "measured_quality_evidence": list(self.measured_quality_evidence),
            "measured_state": self.measured_state.value,
            "quality_verified": self.quality_verified,
            "reason": self.reason.value,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityCandidate:
        data = _mapping(payload, "capability candidate")
        _check_fields(
            data,
            frozenset(
                {
                    "authorized",
                    "descriptor",
                    "estimated_resources",
                    "exact_match",
                    "measured_quality_evidence",
                    "measured_state",
                    "quality_verified",
                    "reason",
                    "schema_version",
                }
            ),
            "capability candidate",
        )
        return cls(
            descriptor=CapabilityDescriptor.from_dict(data["descriptor"]),
            measured_state=CapabilityLifecycle(
                _string(data["measured_state"], "capability measured state")
            ),
            quality_verified=_bool(data["quality_verified"], "capability verified quality"),
            authorized=_bool(data["authorized"], "capability authorization"),
            measured_quality_evidence=tuple(
                _string(item, "capability measured quality evidence")
                for item in _sequence(
                    data["measured_quality_evidence"],
                    "capability measured quality evidence",
                )
            ),
            estimated_resources=CapabilityResourceEnvelope.from_dict(data["estimated_resources"]),
            reason=CapabilityReasonCode(_string(data["reason"], "capability candidate reason")),
            exact_match=_bool(data["exact_match"], "capability exact match"),
            schema_version=_string(data["schema_version"], "capability candidate schema version"),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class CapabilityRoute:
    requirements: RequestRequirements
    candidates: tuple[CapabilityCandidate, ...]
    selected_capability_id: str | None
    reason: CapabilityReasonCode
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("capability route schema version is invalid")
        if not isinstance(self.requirements, RequestRequirements):
            raise ValueError("capability route requirements are invalid")
        if type(self.candidates) is not tuple or any(
            not isinstance(item, CapabilityCandidate) for item in self.candidates
        ):
            raise ValueError("capability route candidates are invalid")
        ordered = tuple(sorted(self.candidates, key=lambda item: item.descriptor.capability_id))
        if len({item.descriptor.capability_id for item in ordered}) != len(ordered):
            raise ValueError("capability route candidates must be unique")
        object.__setattr__(self, "candidates", ordered)
        if self.selected_capability_id is not None:
            object.__setattr__(
                self,
                "selected_capability_id",
                _identifier(self.selected_capability_id, "selected capability identifier"),
            )
            if self.selected_capability_id not in {
                item.descriptor.capability_id for item in self.candidates
            }:
                raise ValueError("capability route selection must reference a candidate")
        if not isinstance(self.reason, CapabilityReasonCode):
            raise ValueError("capability route reason is invalid")
        if self.reason is CapabilityReasonCode.MATCH and self.selected_capability_id is None:
            raise ValueError("capability route match requires a selected capability")
        if (
            self.reason is CapabilityReasonCode.AMBIGUOUS
            and self.selected_capability_id is not None
        ):
            raise ValueError("capability route ambiguity cannot select a capability")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates": [item.to_dict() for item in self.candidates],
            "reason": self.reason.value,
            "requirements": self.requirements.to_dict(),
            "schema_version": self.schema_version,
            "selected_capability_id": self.selected_capability_id,
        }

    @classmethod
    def from_dict(cls, payload: object) -> CapabilityRoute:
        data = _mapping(payload, "capability route")
        _check_fields(
            data,
            frozenset(
                {
                    "candidates",
                    "reason",
                    "requirements",
                    "schema_version",
                    "selected_capability_id",
                }
            ),
            "capability route",
        )
        return cls(
            requirements=RequestRequirements.from_dict(data["requirements"]),
            candidates=tuple(
                CapabilityCandidate.from_dict(item)
                for item in _sequence(data["candidates"], "capability route candidates")
            ),
            selected_capability_id=_optional_string(
                data["selected_capability_id"],
                "selected capability identifier",
            ),
            reason=CapabilityReasonCode(_string(data["reason"], "capability route reason")),
            schema_version=_string(data["schema_version"], "capability route schema version"),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())


class ExactCapabilityMatcher:
    """Deterministic exact matcher over typed requirements and candidates."""

    def match(
        self,
        requirements: RequestRequirements,
        candidates: Iterable[CapabilityCandidate],
    ) -> CapabilityRoute:
        if not isinstance(requirements, RequestRequirements):
            raise ValueError("capability matcher requires typed request requirements")
        observed = tuple(candidates)
        if any(not isinstance(item, CapabilityCandidate) for item in observed):
            raise ValueError("capability matcher requires typed candidates")
        self._validate_relationships(observed)
        evaluated = tuple(self._evaluate(requirements, item) for item in observed)
        exact_matches = [item for item in evaluated if item.reason is CapabilityReasonCode.MATCH]
        if not exact_matches:
            return CapabilityRoute(
                requirements=requirements,
                candidates=evaluated,
                selected_capability_id=None,
                reason=self._route_reason(evaluated),
            )
        best_score = min(self._score(item) for item in exact_matches)
        best = [item for item in exact_matches if self._score(item) == best_score]
        if len(best) != 1:
            return CapabilityRoute(
                requirements=requirements,
                candidates=evaluated,
                selected_capability_id=None,
                reason=CapabilityReasonCode.AMBIGUOUS,
            )
        return CapabilityRoute(
            requirements=requirements,
            candidates=evaluated,
            selected_capability_id=best[0].descriptor.capability_id,
            reason=CapabilityReasonCode.MATCH,
        )

    def _evaluate(
        self,
        requirements: RequestRequirements,
        candidate: CapabilityCandidate,
    ) -> CapabilityCandidate:
        descriptor = candidate.descriptor
        if descriptor.capability_class not in requirements.capability_classes:
            return replace(candidate, reason=CapabilityReasonCode.UNSUPPORTED, exact_match=False)
        if (
            descriptor.modality != requirements.modality
            or descriptor.domain != requirements.domain
            or descriptor.intent != requirements.intent
            or descriptor.input_schema != requirements.input_schema
            or descriptor.output_schema != requirements.output_schema
            or (
                requirements.required_runtime_id is not None
                and descriptor.runtime_id != requirements.required_runtime_id
            )
        ):
            return replace(candidate, reason=CapabilityReasonCode.UNSUPPORTED, exact_match=False)
        if (
            candidate.measured_state
            in {
                CapabilityLifecycle.UNAVAILABLE,
                CapabilityLifecycle.REVOKED,
            }
            or candidate.measured_state not in requirements.accepted_lifecycles
        ):
            return replace(candidate, reason=CapabilityReasonCode.UNAVAILABLE, exact_match=False)
        if requirements.require_authorized and not candidate.authorized:
            return replace(candidate, reason=CapabilityReasonCode.PROHIBITED, exact_match=False)
        if requirements.require_verified_quality and not candidate.quality_verified:
            return replace(candidate, reason=CapabilityReasonCode.UNVERIFIED, exact_match=False)
        if descriptor.locality not in requirements.accepted_localities:
            return replace(candidate, reason=CapabilityReasonCode.PROHIBITED, exact_match=False)
        if descriptor.licence not in requirements.accepted_licenses:
            return replace(candidate, reason=CapabilityReasonCode.PROHIBITED, exact_match=False)
        if _risk_rank(descriptor.risk) > _risk_rank(requirements.max_risk):
            return replace(candidate, reason=CapabilityReasonCode.PROHIBITED, exact_match=False)
        if not set(requirements.allowed_effects).issuperset(descriptor.effects):
            return replace(candidate, reason=CapabilityReasonCode.PROHIBITED, exact_match=False)
        if (
            requirements.language not in descriptor.languages
            or requirements.locale not in descriptor.locales
            or requirements.input_bytes > descriptor.max_input_bytes
            or requirements.expected_output_bytes > descriptor.max_output_bytes
            or requirements.context_items > descriptor.max_context_items
            or not set(requirements.required_verifiers).issubset(descriptor.verifier_requirements)
        ):
            return replace(candidate, reason=CapabilityReasonCode.PARTIAL_MATCH, exact_match=False)
        if any(
            used > allowed
            for used, allowed in zip(
                self._score(candidate), self._score_limit(requirements.max_resources), strict=True
            )
        ):
            return replace(candidate, reason=CapabilityReasonCode.EXCESSIVE_COST, exact_match=False)
        return replace(candidate, reason=CapabilityReasonCode.MATCH, exact_match=True)

    def _route_reason(self, candidates: tuple[CapabilityCandidate, ...]) -> CapabilityReasonCode:
        if not candidates:
            return CapabilityReasonCode.UNSUPPORTED
        precedence = (
            CapabilityReasonCode.PARTIAL_MATCH,
            CapabilityReasonCode.EXCESSIVE_COST,
            CapabilityReasonCode.PROHIBITED,
            CapabilityReasonCode.UNVERIFIED,
            CapabilityReasonCode.UNAVAILABLE,
            CapabilityReasonCode.UNSUPPORTED,
        )
        for reason in precedence:
            if any(item.reason is reason for item in candidates):
                return reason
        return CapabilityReasonCode.UNSUPPORTED

    def _score(self, candidate: CapabilityCandidate) -> tuple[int, int, int, int, int]:
        resources = candidate.estimated_resources
        return (
            resources.estimated_cost_micros,
            resources.latency_ms,
            resources.accelerator_millis,
            resources.cpu_millis,
            resources.memory_mib,
        )

    def _score_limit(self, resources: CapabilityResourceEnvelope) -> tuple[int, int, int, int, int]:
        return (
            resources.estimated_cost_micros,
            resources.latency_ms,
            resources.accelerator_millis,
            resources.cpu_millis,
            resources.memory_mib,
        )

    def _validate_relationships(self, candidates: tuple[CapabilityCandidate, ...]) -> None:
        known = {item.descriptor.capability_id for item in candidates}
        if len(known) != len(candidates):
            raise ValueError("capability candidates must be unique")
        for item in candidates:
            for relation in item.descriptor.dependency_ids + item.descriptor.fallback_ids:
                if relation not in known:
                    raise ValueError("unknown capability relationship")
        self._validate_acyclic(
            {
                item.descriptor.capability_id: item.descriptor.dependency_ids
                for item in candidates
                if item.descriptor.dependency_ids
            },
            "dependency relationships must be acyclic",
        )
        self._validate_acyclic(
            {
                item.descriptor.capability_id: item.descriptor.fallback_ids
                for item in candidates
                if item.descriptor.fallback_ids
            },
            "fallback relationships must be acyclic",
        )

    def _validate_acyclic(
        self,
        graph: Mapping[str, tuple[str, ...]],
        message: str,
    ) -> None:
        try:
            # The supported catalogue may exceed Python's recursion depth. The
            # standard sorter validates all edges with bounded iterative state.
            tuple(TopologicalSorter(graph).static_order())
        except CycleError:
            raise ValueError(message) from None
