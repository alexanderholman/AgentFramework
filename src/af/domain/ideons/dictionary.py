"""Versioned logical dictionary lookup without forced sense closure."""

from __future__ import annotations

from collections import defaultdict

from af.domain.ideons.records import (
    CandidateSet,
    CandidateStatus,
    ContextGraph,
    FormSequence,
    SenseCandidate,
    SenseEntry,
)


class LogicDictionary:
    def __init__(self, entries: tuple[SenseEntry, ...]) -> None:
        identifiers = [entry.entry_id for entry in entries]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("dictionary entry ids must be unique")
        by_form: defaultdict[tuple[str, str], list[SenseEntry]] = defaultdict(list)
        for entry in entries:
            for form in entry.forms:
                by_form[(entry.profile_id, form.casefold())].append(entry)
        self._entries = entries
        self._by_form = {
            key: tuple(sorted(values, key=lambda entry: entry.entry_id))
            for key, values in by_form.items()
        }

    @property
    def entries(self) -> tuple[SenseEntry, ...]:
        return self._entries

    def candidates(
        self,
        forms: FormSequence,
        context: ContextGraph,
    ) -> tuple[CandidateSet, ...]:
        context_values = dict(context.values)
        return tuple(
            CandidateSet(
                form_index=index,
                surface=form.surface,
                candidates=tuple(
                    self._evaluate(index, entry, context_values)
                    for entry in self._by_form.get((forms.profile.identifier, form.normalized), ())
                ),
            )
            for index, form in enumerate(forms.forms)
        )

    @staticmethod
    def _evaluate(
        form_index: int,
        entry: SenseEntry,
        context: dict[str, str],
    ) -> SenseCandidate:
        reasons: list[str] = []
        excluded = False
        for field, forbidden_value in entry.excluded_contexts:
            if context.get(field) == forbidden_value:
                excluded = True
                reasons.append(f"excluded-context:{field}={forbidden_value}")
        for field, required_value in entry.valid_contexts:
            actual = context.get(field)
            if actual is None:
                reasons.append(f"context-unresolved:{field}")
            elif actual == required_value:
                reasons.append(f"context-match:{field}={required_value}")
            else:
                excluded = True
                reasons.append(f"context-conflict:{field}={actual}")
        return SenseCandidate(
            form_index=form_index,
            entry=entry,
            status=CandidateStatus.EXCLUDED if excluded else CandidateStatus.VIABLE,
            reasons=tuple(reasons),
        )
