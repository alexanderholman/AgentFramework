"""Lossless Unicode form encoding for the reference Ideon implementation."""

from __future__ import annotations

import re
import unicodedata

from af.domain.ideons.records import Form, FormSequence, LanguageProfileRef

_FORM_PATTERN = re.compile(r"\w+(?:['\u2019]\w+)*|[^\w\s]", flags=re.UNICODE)


class LosslessFormEncoder:
    def encode(self, media: str, profile: LanguageProfileRef) -> FormSequence:
        if not isinstance(media, str):
            raise TypeError("text media must be a string")
        if profile.unicode_version != unicodedata.unidata_version:
            raise ValueError(
                "language profile Unicode version does not match the runtime: "
                f"{profile.unicode_version!r} != {unicodedata.unidata_version!r}"
            )
        offsets = [0]
        for character in media:
            offsets.append(offsets[-1] + len(character.encode("utf-8")))
        forms = tuple(
            Form(
                surface=match.group(0),
                normalized=unicodedata.normalize(profile.normalization, match.group(0)).casefold(),
                form_type=_form_type(match.group(0)),
                codepoint_span=(match.start(), match.end()),
                byte_span=(offsets[match.start()], offsets[match.end()]),
            )
            for match in _FORM_PATTERN.finditer(media)
        )
        return FormSequence(media, media.encode("utf-8"), profile, forms)


def _form_type(surface: str) -> str:
    if surface.isdecimal():
        return "numeral-form"
    if all(unicodedata.category(character).startswith("P") for character in surface):
        return "punctuation"
    return "word-form"
