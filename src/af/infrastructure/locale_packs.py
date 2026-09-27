"""Ed25519 authenticated declarative locale packs and pinned Babel/CLDR formatting."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Context, Decimal, localcontext
from string import Formatter

from babel import Locale, UnknownLocaleError
from babel.core import get_global
from babel.dates import format_date, format_datetime
from babel.numbers import format_decimal
from babel.units import UnknownUnitError, format_unit
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from af.application.locales import (
    MESSAGE_SPECS,
    LocaleInputError,
    LocaleSelection,
    Localizer,
    QuantityDisplay,
    VerifiedLocalePack,
)

PACK_SCHEMA = "af.locale-pack/1"
MAX_PACK_BYTES = 65536


def canonical_pack_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _text(value: object, *, limit: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise LocaleInputError("locale_invalid_pack")
    if any(
        ord(character) < 32 or character in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for character in value
    ):
        raise LocaleInputError("locale_invalid_pack")
    return value


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise LocaleInputError("locale_invalid_pack")
    return value


def _signature(key: bytes, signature: object, body: bytes) -> None:
    if not isinstance(signature, str) or len(signature) > 128:
        raise LocaleInputError("locale_invalid_signature")
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            base64.b64decode(signature, validate=True), body
        )
    except (InvalidSignature, ValueError, TypeError) as error:
        raise LocaleInputError("locale_invalid_signature") from error


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LocaleInputError("locale_duplicate_json_key")
        result[key] = value
    return result


def load_locale_pack(
    document: str,
    *,
    trusted_authors: Mapping[str, bytes],
    trusted_reviewers: Mapping[str, bytes] | None = None,
) -> VerifiedLocalePack:
    """Authenticate before installing; reviewers sign the same complete payload.

    Caller-managed trust keys establish who may author or attest human review.
    Critical translations require two distinct trusted human reviewer signatures;
    author or metadata claims alone never satisfy that gate.
    """
    if not isinstance(document, str) or len(document.encode("utf-8")) > MAX_PACK_BYTES:
        raise LocaleInputError("locale_pack_too_large")
    try:
        envelope = _mapping(json.loads(document, object_pairs_hook=_unique_json_pairs))
    except (ValueError, TypeError, RecursionError) as error:
        raise LocaleInputError("locale_invalid_pack") from error
    if set(envelope) != {"payload", "signature", "review_signatures"}:
        raise LocaleInputError("locale_invalid_pack")
    payload = _mapping(envelope["payload"])
    expected = {
        "schema",
        "language",
        "version",
        "direction",
        "messages",
        "signer",
        "machine_assisted",
        "provenance",
        "licence",
        "reviewers",
    }
    if set(payload) != expected or payload["schema"] != PACK_SCHEMA:
        raise LocaleInputError("locale_invalid_pack")
    signer = _text(payload["signer"], limit=100)
    key = trusted_authors.get(signer)
    if key is None:
        raise LocaleInputError("locale_untrusted_signer")
    body = canonical_pack_bytes(payload)
    _signature(key, envelope["signature"], body)
    language = _text(payload["language"], limit=128)
    selection = LocaleSelection.parse(language=language)
    if selection.language != language or selection.territory is not None:
        raise LocaleInputError("locale_pack_language_only")
    direction = _text(payload["direction"], limit=3)
    if direction not in {"ltr", "rtl"}:
        raise LocaleInputError("locale_invalid_direction")
    version = _text(payload["version"], limit=50)
    if type(payload["machine_assisted"]) is not bool:
        raise LocaleInputError("locale_invalid_pack")
    reviewers_raw = payload["reviewers"]
    if not isinstance(reviewers_raw, list) or len(reviewers_raw) > 8:
        raise LocaleInputError("locale_invalid_reviewers")
    reviewers = tuple(_text(item, limit=100) for item in reviewers_raw)
    if len(set(reviewers)) != len(reviewers):
        raise LocaleInputError("locale_invalid_reviewers")
    attestations = _mapping(envelope["review_signatures"])
    if set(attestations) != set(reviewers):
        raise LocaleInputError("locale_invalid_reviewers")
    verified_keys: set[bytes] = set()
    for reviewer in reviewers:
        reviewer_key = (trusted_reviewers or {}).get(reviewer)
        if reviewer_key is None:
            raise LocaleInputError("locale_untrusted_reviewer")
        _signature(reviewer_key, attestations[reviewer], body)
        verified_keys.add(reviewer_key)
    messages = _mapping(payload["messages"])
    if not messages or len(messages) > len(MESSAGE_SPECS):
        raise LocaleInputError("locale_invalid_messages")
    validated: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for message_id, raw_variants in messages.items():
        spec = MESSAGE_SPECS.get(message_id)
        if spec is None:
            raise LocaleInputError("locale_forbidden_message")
        if spec.critical and len(verified_keys) < 2:
            raise LocaleInputError("locale_human_review_required")
        variants = _mapping(raw_variants)
        if "other" not in variants or set(variants) - {
            "zero",
            "one",
            "two",
            "few",
            "many",
            "other",
        }:
            raise LocaleInputError("locale_invalid_plural")
        parsed: list[tuple[str, str]] = []
        for plural, raw_template in variants.items():
            template = _text(raw_template, limit=4096)
            placeholders: set[str] = set()
            try:
                for _, field, formatting, conversion in Formatter().parse(template):
                    if field is not None:
                        if formatting or conversion:
                            raise LocaleInputError("locale_invalid_template")
                        placeholders.add(field)
            except ValueError as error:
                raise LocaleInputError("locale_invalid_template") from error
            if placeholders != {name for name, _ in spec.parameters}:
                raise LocaleInputError("locale_invalid_template")
            parsed.append((plural, template))
        validated.append((message_id, tuple(sorted(parsed))))
    return VerifiedLocalePack(
        language,
        version,
        direction,
        tuple(sorted(validated)),
        "sha256:" + hashlib.sha256(body).hexdigest(),
        payload["machine_assisted"],
        _text(payload["provenance"]),
        _text(payload["licence"], limit=200),
        reviewers,
    )


class BabelLocaleFormatter:
    """Pinned Babel 2.17.0 uses CLDR data; territory controls display conventions."""

    def _locale(self, selection: LocaleSelection) -> Locale:
        language = selection.language.replace("-", "_")
        territory = selection.territory
        if territory is not None and territory not in {"XA", "XB"}:
            try:
                return Locale.parse(f"{language}_{territory}")
            except (UnknownLocaleError, ValueError):
                territory_languages = get_global("territory_languages").get(territory, {})
                if territory_languages:
                    regional = max(
                        territory_languages,
                        key=lambda item: territory_languages[item].get("population_percent", 0),
                    )
                    try:
                        return Locale.parse(f"{regional}_{territory}")
                    except (UnknownLocaleError, ValueError):
                        pass
        try:
            return Locale.parse(language)
        except (UnknownLocaleError, ValueError):
            return Locale.parse("en_GB")

    def number(self, value: Decimal | int, selection: LocaleSelection) -> str:
        with localcontext(Context(prec=256)):
            return format_decimal(
                value,
                locale=self._locale(selection),
                decimal_quantization=False,
                numbering_system="latn",
            )

    def date(self, value: date | datetime, selection: LocaleSelection) -> str:
        if isinstance(value, datetime):
            return format_datetime(
                value, locale=self._locale(selection), tzinfo=value.tzinfo or UTC
            )
        return format_date(value, locale=self._locale(selection))

    def quantity(self, value: QuantityDisplay, selection: LocaleSelection) -> str:
        try:
            with localcontext(Context(prec=256)):
                return format_unit(
                    value.value, value.unit, locale=self._locale(selection), length="long"
                )
        except UnknownUnitError as error:
            raise LocaleInputError("locale_unknown_unit") from error

    def plural(self, count: int, language: str) -> str:
        try:
            return str(Locale.parse(language.replace("-", "_")).plural_form(count))
        except (UnknownLocaleError, ValueError):
            return "one" if count == 1 else "other"


def create_starter_localizer() -> Localizer:
    from af.infrastructure.starter_locale_data import STARTER_PACKS, STARTER_PUBLIC_KEY

    packs = tuple(
        load_locale_pack(
            document, trusted_authors={"af.starter": bytes.fromhex(STARTER_PUBLIC_KEY)}
        )
        for document in STARTER_PACKS
    )
    return Localizer(packs, BabelLocaleFormatter())
