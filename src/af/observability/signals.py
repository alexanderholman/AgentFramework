"""Bounded structured telemetry signals and best-effort delivery."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from inspect import isasyncgen, isawaitable, iscoroutinefunction
from math import isfinite
from threading import Lock
from typing import Protocol

from af.observability.context import TelemetryContext

__all__ = (
    "HealthState",
    "PublisherEvidence",
    "TelemetryKind",
    "TelemetryPublisher",
    "TelemetrySignal",
    "TelemetrySink",
)

type AttributeValue = bool | int | float | str

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_ATTRIBUTE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,62}[a-z0-9]$|^[a-z]$")
_ATTRIBUTE_TOKEN = re.compile(r"^[a-z0-9](?:[a-z0-9_.:-]{0,126}[a-z0-9])?$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_.-])(?:authorization|cookie|credential|document|password|payload|"
    r"prompt|response|secret|token|vector|content)(?:$|[_.-])"
)
_MAX_ATTRIBUTES = 32
_MAX_LABEL_VALUES = 1024
_MAX_LABEL_SERIES = 16_384


class TelemetryKind(StrEnum):
    """Stable signal families shared by adapters."""

    LOG = "log"
    SPAN = "span"
    METRIC = "metric"
    HEALTH = "health"
    LIFECYCLE = "lifecycle"


class HealthState(StrEnum):
    """Health dimensions that must not be collapsed into a single boolean."""

    LIVE = "live"
    READY = "ready"
    DEGRADED = "degraded"
    CAPABILITY_MISMATCH = "capability-mismatch"
    STOPPED = "stopped"


def _attribute_value(value: object) -> AttributeValue:
    if type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise ValueError("telemetry integer attributes must be signed 64-bit values")
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("telemetry float attributes must be finite")
        return value
    if type(value) is str and _ATTRIBUTE_TOKEN.fullmatch(value) is not None:
        return value
    raise ValueError("telemetry string attributes must be bounded low-cardinality tokens")


def _snapshot_attributes(
    attributes: Mapping[str, object],
    *,
    limit: int,
) -> tuple[tuple[str, AttributeValue], ...]:
    if type(limit) is not int or not 0 <= limit <= _MAX_ATTRIBUTES:
        raise ValueError(f"telemetry signals allow at most {_MAX_ATTRIBUTES} attributes")
    if not isinstance(attributes, Mapping) or len(attributes) > limit:
        raise ValueError(f"telemetry signal accepts at most {limit} attributes")
    snapshot: list[tuple[str, AttributeValue]] = []
    for key, value in attributes.items():
        if type(key) is not str or _ATTRIBUTE_KEY.fullmatch(key) is None:
            raise ValueError("telemetry attribute keys must be canonical identifiers")
        if _SENSITIVE_KEY.search(key):
            raise ValueError("telemetry attributes cannot use sensitive keys")
        snapshot.append((key, _attribute_value(value)))
    snapshot.sort(key=lambda item: item[0])
    return tuple(snapshot)


@dataclass(frozen=True, slots=True, repr=False)
class TelemetrySignal:
    """One immutable, adapter-neutral structured telemetry signal."""

    kind: TelemetryKind
    name: str
    context: TelemetryContext | None
    attributes: tuple[tuple[str, AttributeValue], ...]
    health_state: HealthState | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TelemetryKind):
            raise TypeError("telemetry signal kind is invalid")
        if type(self.name) is not str or _IDENTIFIER.fullmatch(self.name) is None:
            raise ValueError("telemetry signal name must be a canonical identifier")
        if self.context is not None and not isinstance(self.context, TelemetryContext):
            raise TypeError("telemetry signal context is invalid")
        if type(self.attributes) is not tuple or any(
            type(item) is not tuple or len(item) != 2 for item in self.attributes
        ):
            raise ValueError("telemetry signal attributes must be a canonical snapshot")
        try:
            mapped = dict(self.attributes)
            canonical = _snapshot_attributes(mapped, limit=_MAX_ATTRIBUTES)
        except (TypeError, ValueError) as error:
            raise ValueError("telemetry signal attributes are unsafe") from error
        if len(mapped) != len(self.attributes) or canonical != self.attributes:
            raise ValueError("telemetry signal attributes must be a canonical snapshot")
        if self.kind is TelemetryKind.HEALTH:
            if not isinstance(self.health_state, HealthState):
                raise ValueError("health telemetry requires a health state")
        elif self.health_state is not None:
            raise ValueError("health state is only valid for health telemetry")

    @classmethod
    def create(
        cls,
        *,
        kind: TelemetryKind,
        name: str,
        context: TelemetryContext | None = None,
        attributes: Mapping[str, object] | None = None,
        attribute_limit: int = 16,
    ) -> TelemetrySignal:
        """Create a signal after snapshotting bounded safe attributes."""

        return cls(
            kind=kind,
            name=name,
            context=context,
            attributes=_snapshot_attributes(
                {} if attributes is None else attributes,
                limit=attribute_limit,
            ),
        )

    @classmethod
    def health(
        cls,
        *,
        name: str,
        state: HealthState,
        context: TelemetryContext | None = None,
        attributes: Mapping[str, object] | None = None,
        attribute_limit: int = 16,
    ) -> TelemetrySignal:
        """Create a health signal with an explicit health dimension."""

        return cls(
            kind=TelemetryKind.HEALTH,
            name=name,
            context=context,
            attributes=_snapshot_attributes(
                {} if attributes is None else attributes,
                limit=attribute_limit,
            ),
            health_state=state,
        )

    def __repr__(self) -> str:
        return (
            f"TelemetrySignal(kind={self.kind.value!r}, name={self.name!r}, "
            "context='[REDACTED]', attributes='[REDACTED]', "
            f"health_state={None if self.health_state is None else self.health_state.value!r})"
        )


class TelemetrySink(Protocol):
    """Synchronous adapter boundary for one telemetry destination."""

    def emit(self, signal: TelemetrySignal, /) -> object: ...


@dataclass(frozen=True, slots=True)
class PublisherEvidence:
    """Count-only publisher evidence that cannot disclose signal data."""

    accepted_signals: int
    successful_deliveries: int
    failed_deliveries: int
    cardinality_drops: int


class TelemetryPublisher:
    """Best-effort fan-out with bounded cross-signal label cardinality."""

    def __init__(
        self,
        sinks: Iterable[TelemetrySink] = (),
        *,
        label_value_limit: int = 32,
        label_series_limit: int = 1024,
    ) -> None:
        if type(label_value_limit) is not int or not 1 <= label_value_limit <= _MAX_LABEL_VALUES:
            raise ValueError("telemetry label value limit is invalid")
        if type(label_series_limit) is not int or not 1 <= label_series_limit <= _MAX_LABEL_SERIES:
            raise ValueError("telemetry label series limit is invalid")
        snapshot = tuple(sinks)
        for sink in snapshot:
            try:
                emit = sink.emit
            except Exception as error:
                raise ValueError("telemetry sink is invalid") from error
            if not callable(emit) or iscoroutinefunction(emit):
                raise ValueError("telemetry sinks must expose synchronous emit")
        self._sinks = snapshot
        self._label_value_limit = label_value_limit
        self._label_series_limit = label_series_limit
        self._label_values: dict[tuple[TelemetryKind, str, str], set[AttributeValue]] = {}
        self._accepted_signals = 0
        self._successful_deliveries = 0
        self._failed_deliveries = 0
        self._cardinality_drops = 0
        self._lock = Lock()

    @property
    def evidence(self) -> PublisherEvidence:
        with self._lock:
            return PublisherEvidence(
                accepted_signals=self._accepted_signals,
                successful_deliveries=self._successful_deliveries,
                failed_deliveries=self._failed_deliveries,
                cardinality_drops=self._cardinality_drops,
            )

    def publish(self, signal: TelemetrySignal, /) -> None:
        """Deliver a safe signal without surfacing optional sink failures."""

        if type(signal) is not TelemetrySignal:
            raise TypeError("telemetry publisher requires a TelemetrySignal")
        with self._lock:
            additions: list[tuple[tuple[TelemetryKind, str, str], AttributeValue]] = []
            new_labels: set[tuple[TelemetryKind, str, str]] = set()
            for key, value in signal.attributes:
                label = (signal.kind, signal.name, key)
                known = self._label_values.get(label)
                if (
                    known is not None
                    and value not in known
                    and len(known) >= self._label_value_limit
                ):
                    self._cardinality_drops += 1
                    return
                if known is None:
                    new_labels.add(label)
                additions.append((label, value))
            if len(self._label_values) + len(new_labels) > self._label_series_limit:
                self._cardinality_drops += 1
                return
            for label, value in additions:
                self._label_values.setdefault(label, set()).add(value)
            self._accepted_signals += 1

        for sink in self._sinks:
            try:
                result = sink.emit(signal)
                if isawaitable(result) or isasyncgen(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    else:
                        cancel = getattr(result, "cancel", None)
                        if callable(cancel):
                            cancel()
                    raise TypeError("telemetry sink returned an asynchronous result")
            except Exception:
                with self._lock:
                    self._failed_deliveries += 1
            else:
                with self._lock:
                    self._successful_deliveries += 1
