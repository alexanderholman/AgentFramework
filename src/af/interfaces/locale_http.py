"""Explicit bounded HTTP locale selection and escaped-template view metadata."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import HTTPException, Request
from pydantic import BaseModel

from af.application.locales import (
    MESSAGE_SPECS,
    LocaleInputError,
    LocaleSelection,
    Localizer,
    Message,
)


class LocalePresentation(BaseModel):
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


def request_locale(request: Request) -> LocaleSelection | None:
    query = request.query_params
    if any(len(query.getlist(key)) > 1 for key in ("language", "territory")):
        raise HTTPException(400, detail="locale_invalid_selection")
    if "language" not in query and "territory" not in query:
        return None
    try:
        return LocaleSelection.parse(
            language=query.get("language", "en"), territory=query.get("territory") or None
        )
    except LocaleInputError:
        raise HTTPException(400, detail="locale_invalid_selection") from None


def locale_query(selection: LocaleSelection | None) -> str:
    if selection is None:
        return ""
    query = {"language": selection.language}
    if selection.territory is not None:
        query["territory"] = selection.territory
    return "?" + urlencode(query)


def content_language(localizer: Localizer, selection: LocaleSelection) -> str:
    return localizer.render(Message("system.hello", {}), selection).resolved_language


def web_locale_context(request: Request, localizer: Localizer | None) -> dict[str, object]:
    requested = request_locale(request)
    selection = requested or LocaleSelection.parse()
    labels: dict[str, dict[str, object]] = {}
    for message_id, spec in MESSAGE_SPECS.items():
        if not message_id.startswith("web."):
            continue
        labels[message_id] = (
            localizer.render(Message(message_id, {}), selection).to_dict()
            if localizer is not None
            else {
                "text": dict(spec.source)["other"],
                "resolved_language": "en",
                "direction": "ltr",
                "machine_assisted": False,
            }
        )
    primary = labels["web.tasks"]
    return {
        "messages": labels,
        "locale_language": primary["resolved_language"],
        "locale_direction": primary["direction"],
        "locale_machine_assisted": primary["machine_assisted"],
        "locale_query": locale_query(requested),
        "locale_selected": requested is not None,
        "selected_language": selection.language,
        "selected_territory": selection.territory or "",
        "locale_path": request.url.path,
    }
