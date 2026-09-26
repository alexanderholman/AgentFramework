"""Presentation-only locales with typed messages and immutable domain values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol

from af.framework.errors import AFError


class LocaleInputError(AFError, ValueError):
    """A locale selection, message, or signed presentation pack is invalid."""


@dataclass(frozen=True, slots=True)
class LocaleSelection:
    language: str
    territory: str | None = None
    pseudolocale: bool = False

    @classmethod
    def parse(cls, *, language: str = "en", territory: str | None = None) -> LocaleSelection:
        """Parse ordinary BCP 47 language/script/region/variant/extension tags.

        Legacy grandfathered tags are deliberately rejected; use their preferred
        modern forms. Extensions are syntactically checked but never affect policy.
        """
        if not isinstance(language, str) or not 2 <= len(language) <= 128:
            raise LocaleInputError("locale_invalid_tag")
        parts = language.split("-")
        if re.fullmatch(r"[A-Za-z]{2,8}", parts[0]) is None:
            raise LocaleInputError("locale_invalid_tag")
        primary = parts.pop(0).lower()
        script = ""
        if parts and re.fullmatch(r"[A-Za-z]{4}", parts[0]):
            script = "-" + parts.pop(0).title()
        region: str | None = None
        if parts and re.fullmatch(r"(?:[A-Za-z]{2}|[0-9]{3})", parts[0]):
            region = parts.pop(0).upper()
        variants: set[str] = set()
        while parts and re.fullmatch(r"(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3})", parts[0]):
            variant = parts.pop(0).lower()
            if variant in variants:
                raise LocaleInputError("locale_invalid_tag")
            variants.add(variant)
        extensions: set[str] = set()
        while parts:
            singleton = parts.pop(0).lower()
            if re.fullmatch(r"[a-z0-9]", singleton) is None or singleton in extensions:
                raise LocaleInputError("locale_invalid_tag")
            extensions.add(singleton)
            count = 0
            pattern = r"[A-Za-z0-9]{1,8}" if singleton == "x" else r"[A-Za-z0-9]{2,8}"
            while parts and re.fullmatch(pattern, parts[0]):
                parts.pop(0)
                count += 1
            if count == 0:
                raise LocaleInputError("locale_invalid_tag")
        if territory is not None:
            if (
                not isinstance(territory, str)
                or re.fullmatch(r"(?:[A-Za-z]{2}|[0-9]{3})", territory) is None
            ):
                raise LocaleInputError("locale_invalid_territory")
            region = territory.upper()
        return cls(primary + script, region, region in {"XA", "XB"})


@dataclass(frozen=True, slots=True)
class QuantityDisplay:
    value: Decimal
    unit: str


type LocaleValue = str | int | Decimal | date | datetime | QuantityDisplay


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    parameters: Mapping[str, LocaleValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class MessageSpec:
    parameters: tuple[tuple[str, str], ...]
    source: tuple[tuple[str, str], ...]
    critical: bool = False


MESSAGE_SPECS = MappingProxyType(
    {
        "system.hello": MessageSpec((), (("other", "Hello, world!"),)),
        "result.completed": MessageSpec(
            (("procedure", "text"), ("output", "text")),
            (("other", "Result from {procedure}: {output}"),),
        ),
        "task.count": MessageSpec(
            (("count", "integer"),), (("one", "{count} task"), ("other", "{count} tasks"))
        ),
        "number.value": MessageSpec((("value", "decimal"),), (("other", "{value}"),)),
        "date.value": MessageSpec((("value", "date"),), (("other", "{value}"),)),
        "quantity.value": MessageSpec((("value", "quantity"),), (("other", "{value}"),)),
        "error.unsupported": MessageSpec(
            (("request", "text"),), (("other", "Unsupported request: {request}"),)
        ),
        "consent.required": MessageSpec(
            (("action", "text"),),
            (("other", "Human confirmation required for {action}."),),
            True,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class VerifiedLocalePack:
    language: str
    version: str
    direction: str
    messages: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    digest: str
    machine_assisted: bool
    provenance: str
    licence: str
    reviewers: tuple[str, ...]


class LocaleFormatter(Protocol):
    def number(self, value: Decimal | int, selection: LocaleSelection) -> str: ...
    def date(self, value: date | datetime, selection: LocaleSelection) -> str: ...
    def quantity(self, value: QuantityDisplay, selection: LocaleSelection) -> str: ...
    def plural(self, count: int, language: str) -> str: ...


@dataclass(frozen=True, slots=True)
class LocalizedMessage:
    message_id: str
    text: str
    direction: str
    requested_language: str
    territory: str | None
    resolved_language: str
    fallback: bool
    machine_assisted: bool
    pack_digest: str
    pack_version: str
    provenance: str
    licence: str

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def _validate_parameters(message: Message, spec: MessageSpec) -> None:
    if set(message.parameters) != {name for name, _ in spec.parameters}:
        raise LocaleInputError("locale_invalid_parameters")
    for name, kind in spec.parameters:
        value = message.parameters[name]
        valid = (
            (kind == "text" and type(value) is str and len(value) <= 32768)
            or (kind == "integer" and type(value) is int and abs(value) <= 10**15)
            or (
                kind == "decimal"
                and isinstance(value, Decimal)
                and value.is_finite()
                and abs(value.adjusted()) <= 100
            )
            or (kind == "date" and isinstance(value, (date, datetime)))
            or (
                kind == "quantity"
                and isinstance(value, QuantityDisplay)
                and value.value.is_finite()
                and abs(value.value.adjusted()) <= 100
                and re.fullmatch(r"[a-z]+-[a-z0-9-]+", value.unit) is not None
            )
        )
        if not valid:
            raise LocaleInputError("locale_invalid_parameters")


def _pseudo(text: str) -> str:
    mapping = str.maketrans("aeiouAEIOU", "áëïôüÁËÏÔÜ")
    return "[" + text.translate(mapping) + " ··· " + "~" * max(3, len(text) // 3) + "]"


class Localizer:
    def __init__(self, packs: tuple[VerifiedLocalePack, ...], formatter: LocaleFormatter) -> None:
        mapping = {pack.language: pack for pack in packs}
        if len(mapping) != len(packs) or "en" not in mapping:
            raise LocaleInputError("locale_invalid_pack_set")
        self._packs = MappingProxyType(mapping)
        self._formatter = formatter

    def render(self, message: Message, selection: LocaleSelection) -> LocalizedMessage:
        spec = MESSAGE_SPECS.get(message.id)
        if spec is None:
            raise LocaleInputError("locale_unknown_message")
        _validate_parameters(message, spec)
        requested = selection.language
        pack = (
            self._packs.get(requested)
            or self._packs.get(requested.split("-")[0])
            or self._packs["en"]
        )
        variants = dict(dict(pack.messages).get(message.id, ()))
        fallback = pack.language != requested
        if not variants:
            variants = dict(spec.source)
            pack = self._packs["en"]
            fallback = True
        count = message.parameters.get("count")
        plural = self._formatter.plural(count, pack.language) if type(count) is int else "other"
        template = variants.get(plural, variants["other"])
        direction = "rtl" if selection.territory == "XB" else pack.direction
        rendered: dict[str, str] = {}
        for name, _kind in spec.parameters:
            value = message.parameters[name]
            if isinstance(value, QuantityDisplay):
                text = self._formatter.quantity(value, selection)
            elif isinstance(value, (date, datetime)):
                text = self._formatter.date(value, selection)
            elif isinstance(value, (Decimal, int)):
                text = self._formatter.number(value, selection)
            else:
                text = value
            rendered[name] = "\u2068" + text + "\u2069" if direction == "rtl" else text
        # Validated placeholders have no attribute/index lookup, format spec, or conversion.
        text = template.format_map(rendered)
        if selection.pseudolocale and not spec.critical:
            text = _pseudo(text)
        return LocalizedMessage(
            message.id,
            text,
            direction,
            requested,
            selection.territory,
            pack.language,
            fallback,
            pack.machine_assisted,
            pack.digest,
            pack.version,
            pack.provenance,
            pack.licence,
        )
