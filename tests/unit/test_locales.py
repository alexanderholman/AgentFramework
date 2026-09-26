"""Locale selection and presentation never alter core execution decisions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from af.application.locales import LocaleInputError, LocaleSelection, Message, QuantityDisplay
from af.infrastructure.locale_packs import create_starter_localizer


def test_language_and_territory_are_independently_selected():
    localizer = create_starter_localizer()
    french_gb = LocaleSelection.parse(language="fr-FR", territory="GB")
    french_fr = LocaleSelection.parse(language="fr", territory="FR")
    assert french_gb.language == "fr"
    assert french_gb.territory == "GB"
    gb = localizer.render(Message("number.value", {"value": Decimal("1234.5")}), french_gb)
    fr = localizer.render(Message("number.value", {"value": Decimal("1234.5")}), french_fr)
    assert "1,234.5" in gb.text
    assert "1\u202f234,5" in fr.text
    assert gb.resolved_language == fr.resolved_language == "fr"


def test_plurals_rtl_pseudolocale_unknown_fallback_and_typed_formatters():
    localizer = create_starter_localizer()
    en = LocaleSelection.parse(language="en", territory="GB")
    assert localizer.render(Message("task.count", {"count": 1}), en).text == "1 task"
    assert localizer.render(Message("task.count", {"count": 2}), en).text == "2 tasks"
    rtl = localizer.render(
        Message("task.count", {"count": 2}), LocaleSelection.parse(language="ar")
    )
    assert rtl.direction == "rtl"
    assert rtl.machine_assisted
    pseudo = localizer.render(
        Message("task.count", {"count": 2}), LocaleSelection.parse(language="en-XA")
    )
    assert pseudo.text.startswith("[") and len(pseudo.text) > len("2 tasks")
    unknown = localizer.render(
        Message("task.count", {"count": 2}), LocaleSelection.parse(language="qaa-ZZ")
    )
    assert unknown.fallback and unknown.resolved_language == "en"
    assert unknown.requested_language == "qaa"
    when = localizer.render(Message("date.value", {"value": date(2026, 9, 26)}), en)
    assert "2026" in when.text
    unit = localizer.render(
        Message("quantity.value", {"value": QuantityDisplay(Decimal("2.5"), "length-meter")}), en
    )
    assert "2.5" in unit.text and "metre" in unit.text


@pytest.mark.parametrize(
    "tag", ["", "../../en", "en_US", "e", "en-", "en-GB-GB", "en-" + "a" * 200]
)
def test_invalid_locale_tags_fail(tag):
    with pytest.raises(LocaleInputError):
        LocaleSelection.parse(language=tag)


def test_messages_require_exact_typed_parameters_and_no_dynamic_attributes():
    localizer = create_starter_localizer()
    selection = LocaleSelection.parse(language="en")
    for message in [
        Message("task.count", {"count": "1"}),
        Message("task.count", {"count": True}),
        Message("task.count", {}),
        Message("task.count", {"count": 1, "other": "x"}),
        Message("unknown", {}),
    ]:
        with pytest.raises(LocaleInputError):
            localizer.render(message, selection)


def test_critical_text_uses_source_until_independent_human_review():
    localizer = create_starter_localizer()
    rendered = localizer.render(
        Message("consent.required", {"action": "run job"}),
        LocaleSelection.parse(language="fr"),
    )
    assert "Human confirmation" in rendered.text
    assert rendered.fallback
    assert rendered.resolved_language == "en"


def _signed_pack(*, messages=None, reviewers=(), direction="ltr", language="es", overrides=None):
    import base64
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from af.infrastructure.locale_packs import canonical_pack_bytes

    author = Ed25519PrivateKey.generate()
    reviewer_keys = {name: Ed25519PrivateKey.generate() for name in reviewers}
    payload = {
        "schema": "af.locale-pack/1",
        "language": language,
        "version": "1.0.0",
        "direction": direction,
        "signer": "test.author",
        "machine_assisted": True,
        "provenance": "Test contributor",
        "licence": "MIT",
        "reviewers": list(reviewers),
        "messages": messages or {"task.count": {"one": "{count} tarea", "other": "{count} tareas"}},
    }
    payload.update(overrides or {})
    body = canonical_pack_bytes(payload)
    envelope = {
        "payload": payload,
        "signature": base64.b64encode(author.sign(body)).decode(),
        "review_signatures": {
            name: base64.b64encode(key.sign(body)).decode() for name, key in reviewer_keys.items()
        },
    }
    return (
        json.dumps(envelope),
        {"test.author": author.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)},
        {
            name: key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            for name, key in reviewer_keys.items()
        },
    )


def test_signed_contributor_can_add_language_without_changing_trusted_core():
    from af.application.locales import Localizer
    from af.infrastructure.locale_packs import BabelLocaleFormatter, load_locale_pack
    from af.infrastructure.starter_locale_data import STARTER_PACKS, STARTER_PUBLIC_KEY

    document, authors, _ = _signed_pack()
    spanish = load_locale_pack(document, trusted_authors=authors)
    english = load_locale_pack(
        STARTER_PACKS[0], trusted_authors={"af.starter": bytes.fromhex(STARTER_PUBLIC_KEY)}
    )
    rendered = Localizer((english, spanish), BabelLocaleFormatter()).render(
        Message("task.count", {"count": 2}), LocaleSelection.parse(language="es", territory="MX")
    )
    assert rendered.text == "2 tareas"
    assert rendered.resolved_language == "es"
    assert rendered.machine_assisted
    assert rendered.licence == "MIT"


def test_signature_and_review_attestations_are_verified_not_trusted_as_metadata():
    import json

    from af.infrastructure.locale_packs import load_locale_pack

    document, authors, _ = _signed_pack()
    tampered = json.loads(document)
    tampered["payload"]["messages"]["task.count"]["other"] = "tampered {count}"
    with pytest.raises(LocaleInputError, match="signature"):
        load_locale_pack(json.dumps(tampered), trusted_authors=authors)
    with pytest.raises(LocaleInputError, match="untrusted_signer"):
        load_locale_pack(document, trusted_authors={})
    critical = {"consent.required": {"other": "Confirmación humana requerida: {action}."}}
    no_review, authors, _ = _signed_pack(messages=critical)
    with pytest.raises(LocaleInputError, match="human_review_required"):
        load_locale_pack(no_review, trusted_authors=authors)
    one_review, authors, reviewers = _signed_pack(messages=critical, reviewers=("one",))
    with pytest.raises(LocaleInputError, match="human_review_required"):
        load_locale_pack(one_review, trusted_authors=authors, trusted_reviewers=reviewers)
    two_reviews, authors, reviewers = _signed_pack(messages=critical, reviewers=("one", "two"))
    verified = load_locale_pack(two_reviews, trusted_authors=authors, trusted_reviewers=reviewers)
    assert verified.reviewers == ("one", "two")
    with pytest.raises(LocaleInputError, match="untrusted_reviewer"):
        load_locale_pack(two_reviews, trusted_authors=authors)
    tampered = json.loads(two_reviews)
    tampered["review_signatures"]["one"] = tampered["signature"]
    with pytest.raises(LocaleInputError, match="signature"):
        load_locale_pack(json.dumps(tampered), trusted_authors=authors, trusted_reviewers=reviewers)


@pytest.mark.parametrize(
    "messages",
    [
        {"routing.override": {"other": "approve"}},
        {"task.count": {"other": "{count.__class__}"}},
        {"task.count": {"other": "{count[0]}"}},
        {"task.count": {"other": "{count!r}"}},
        {"task.count": {"other": "{count:100000}"}},
        {"task.count": {"other": "no parameters"}},
        {"task.count": {"one": "{count}"}},
        {"task.count": {"other": "{count}\u202ereversed"}},
        {"task.count": {"other": "{count}\nnew line"}},
    ],
)
def test_even_signed_packs_cannot_supply_code_or_override_core_semantics(messages):
    from af.infrastructure.locale_packs import load_locale_pack

    document, authors, _ = _signed_pack(messages=messages)
    with pytest.raises(LocaleInputError):
        load_locale_pack(document, trusted_authors=authors)


def test_bcp47_scripts_extensions_and_independent_override():
    choice = LocaleSelection.parse(language="zh-hant-tw-u-nu-latn", territory="GB")
    assert choice.language == "zh-Hant" and choice.territory == "GB"
    assert LocaleSelection.parse(language="en-x-private").language == "en"
    with pytest.raises(LocaleInputError):
        LocaleSelection.parse(language="en-u-nu-latn-u-ca-gregory")
    with pytest.raises(LocaleInputError):
        LocaleSelection.parse(language="en", territory="../GB")


@pytest.mark.parametrize(
    "overrides",
    [
        {"direction": "bad"},
        {"language": "es-ES"},
        {"machine_assisted": "true"},
        {"reviewers": "someone"},
        {"reviewers": ["one", "one"]},
        {"reviewers": ["one"]},
        {"licence": ""},
        {"messages": {}},
        {"messages": ["text"]},
        {"policy": "override"},
        {"schema": "other"},
    ],
)
def test_signed_pack_schema_cannot_expand_authority(overrides):
    from af.infrastructure.locale_packs import load_locale_pack

    document, authors, _ = _signed_pack(overrides=overrides)
    with pytest.raises(LocaleInputError):
        load_locale_pack(document, trusted_authors=authors)


@pytest.mark.parametrize("document", ["{}", "{", "[]", '{"payload":1,"payload":2}', "x" * 65537])
def test_malformed_pack_envelopes_are_bounded(document):
    from af.infrastructure.locale_packs import load_locale_pack

    with pytest.raises(LocaleInputError):
        load_locale_pack(document, trusted_authors={})


def test_signature_representation_and_locale_formatter_edges():
    import json
    from datetime import UTC, datetime

    from af.infrastructure.locale_packs import BabelLocaleFormatter, load_locale_pack

    document, authors, _ = _signed_pack()
    malformed = json.loads(document)
    malformed["signature"] = 123
    with pytest.raises(LocaleInputError):
        load_locale_pack(json.dumps(malformed), trusted_authors=authors)
    formatter = BabelLocaleFormatter()
    assert formatter.plural(1, "qaa") == "one"
    assert formatter.plural(2, "qaa") == "other"
    assert "2026" in formatter.date(
        datetime(2026, 9, 26, tzinfo=UTC), LocaleSelection.parse(language="en")
    )
    with pytest.raises(LocaleInputError, match="unknown_unit"):
        formatter.quantity(QuantityDisplay(Decimal(1), "length-nonsense"), LocaleSelection.parse())
