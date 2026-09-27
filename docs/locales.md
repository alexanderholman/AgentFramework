# Locale presentation

`LocaleSelection.parse(language="fr", territory="GB")` selects message language
and display territory independently. The shared presenter helpers
`localize_outcome` and `localize_task` add a `presentation` field to the ordinary
serialized result. Procedure identity, typed values, result JSON, verification,
policy and authorization remain in the unchanged canonical fields.

The starter includes English source, machine-assisted French and Arabic, RTL
layout metadata, the `en-XA` and `ar-XB` expanded pseudolocales, and English fallback
for well-formed unknown languages. Translations retain stable core message IDs
with exact typed parameters. Arbitrary format expressions and attributes are not
accepted. BCP 47 language/script/region/variant/extensions are syntax checked;
legacy grandfathered tags require their modern preferred form. Script/region
casing is normalized. Selection does not infer user identity or authorization.

Babel 2.17.0 supplies pinned CLDR plural, number, date and unit formatting. When
CLDR lacks an exact language-territory pairing, territorial formatting uses the
territory's most prevalent language; message language remains selected independently.
Unknown territories fall back to the requested language's format, then `en-GB`.
Numbers use Latin digits explicitly. Naive datetimes format in UTC. RTL parameter
text uses Unicode isolation; adapters must honor `direction` and escape plain text
when rendering HTML. Pseudolocalization expands and accents presentation text only.

`create_starter_localizer()` verifies the embedded starter packs. The embedded
Ed25519 public key authenticates the shipped data; its signing private key is not
included. To install contributor packs, call `load_locale_pack` with explicitly
trusted author public keys, then supply the resulting immutable packs to `Localizer`.
There is no automatic trust of downloaded keys or executable locale content.

A pack envelope has exactly `payload`, `signature`, and `review_signatures`.
The payload contains exactly `schema` (`af.locale-pack/1`), `language`, `version`,
`direction`, `messages`, `signer`, `machine_assisted`, `provenance`, `licence`, and
`reviewers`. Messages map stable core IDs to plural-category/template pairs;
`other` is mandatory. Every template must contain exactly the registered parameter
names with no indexing, attributes, conversions, or format specifications. Packs
are bounded to 65,536 encoded bytes. Duplicate JSON keys, unsupported messages,
raw bidi controls, newlines and malformed signatures are rejected.

Author and reviewer Ed25519 signatures cover `canonical_pack_bytes(payload)`:
UTF-8 JSON with sorted keys, ASCII escaping, and compact separators. Signatures
are base64. A reviewer is trusted only through the caller's configured reviewer
public-key map. Consent/safety-critical translation requires signatures from two
distinct configured human reviewer keys over the complete payload. Author metadata
alone cannot satisfy review. Starter translations omit critical messages, so they
retain canonical English source text. No human translation certification is claimed.

Adding a language requires a signed declarative pack, not a trusted-core change.
The trust administrator is responsible for establishing the author/reviewer key
identities and revoking obsolete keys. The loader cannot import formulas, routes,
policy, consent decisions, code, or security configuration. Presenters return
`machine_assisted`, provenance, licence, resolved language, fallback, and pack
version/digest so interfaces can make translation status visible.

Verification lives in `tests/unit/test_locales.py` and
`tests/feature/test_locale_journey.py`: language-territory independence, CLDR
formatting, pluralization, fallback, RTL, pseudolocalization, immutable domain
output, pack and reviewer signatures, template attacks, and forbidden semantics.

The installed HTML and REST adapters accept explicit `language` and `territory`
query parameters, for example `/tasks?language=ar&territory=EG` and
`/api/v1/tasks?language=fr&territory=CA`. Duplicate or malformed selections are
rejected before a task mutation. An empty HTML territory field clears that choice.
Selection is per request; it is not inferred from identity or stored as authority.
REST adds its typed `presentation` field only when a locale is requested; ordinary
responses retain their previous shape. Responses identify the actual rendered
language with `Content-Language`. Unknown languages render English with fallback
metadata, and a territory never substitutes another message language.

HTML has independent labelled language and territory controls and preserves the
selection through task links, forms and redirects. Signed starter pack version
1.1.0 includes ordinary navigation and task labels. The Delete label uses the
critical source message until an appropriately reviewed translation is installed.
Each translated label carries its own language and direction, including English
fallback inside RTL pages. Canonical task prompts and outputs remain escaped and
use automatic text direction. Machine-assisted text is visibly labelled. A main
landmark, skip link, focus indicators, responsive layout and reduced-motion rules
support accessible operation. The stylesheet is a local external resource so it
works under the application's restrictive content-security policy.

`tests/feature/test_locale_http.py` proves actual REST/HTML CRUD, locale persistence,
invalid-selection denial, ordinary-response compatibility, RTL/pseudolocale output,
and semantic parity for a verified physics result. These structural tests are not
a claim of completed assistive-technology or WCAG certification.
