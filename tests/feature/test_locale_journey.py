"""All locale variants present one unchanged domain result and formula decision."""

from __future__ import annotations

import json

from af.application.locales import LocaleSelection
from af.framework.types import WorkRequest
from af.infrastructure.locale_packs import create_starter_localizer
from af.interfaces.locale_presenters import localize_outcome
from af.interfaces.presenters import outcome_to_dict


def test_locale_territory_rtl_and_pseudo_cannot_change_execution(application):
    outcome = application.agent.handle(
        WorkRequest(prompt="physics kinetic_energy mass=2 kg speed=3 m/s")
    )
    canonical = outcome_to_dict(outcome)
    science = json.loads(outcome.output)
    localizer = create_starter_localizer()
    for language, territory in [
        ("en", "GB"),
        ("fr", "CA"),
        ("ar", "EG"),
        ("en-XA", None),
        ("ar-XB", None),
        ("qaa-ZZ", None),
    ]:
        rendered = localize_outcome(
            outcome, localizer, LocaleSelection.parse(language=language, territory=territory)
        )
        presentation = rendered.pop("presentation")
        assert rendered == canonical
        assert json.loads(rendered["output"])["formula"] == science["formula"]
        assert json.loads(rendered["output"])["reason_code"] == "science_verified"
        assert rendered["route"] == "science-calculate" and rendered["verified"] is True
        assert presentation["direction"] == ("rtl" if language.startswith("ar") else "ltr")
        if language in {"en-XA", "ar-XB"}:
            assert presentation["text"].startswith("[")


def test_task_presentation_preserves_the_standard_task_contract(application):
    from af.interfaces.locale_presenters import localize_task
    from af.interfaces.presenters import task_to_dict

    task = application.tasks.create("math divide a=1 b=3")
    localized = localize_task(
        task, create_starter_localizer(), LocaleSelection.parse(language="fr")
    )
    assert localized.pop("presentation")["resolved_language"] == "fr"
    assert localized == task_to_dict(task)
