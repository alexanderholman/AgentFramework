from __future__ import annotations

import json
import unicodedata
from decimal import Decimal

from af.application.ideons import IdeonService, InterpretRequest
from af.domain.ideons import (
    IdentityRole,
    IdeonGraph,
    IdeonIdentity,
    IdeonKind,
    Property,
)
from af.domain.ideons.dictionary import LogicDictionary
from af.domain.ideons.records import (
    ContextGraph,
    Decision,
    LanguageProfileRef,
    Question,
    SenseEntry,
    ShouldAssessment,
)
from af.infrastructure.ideon_reference import create_reference_ideon_runtime
from af.infrastructure.ideons import InMemoryIdeonRegistry

PROFILE = LanguageProfileRef("en", "0.1", unicodedata.unidata_version, dialect="GB")


def reference_service() -> tuple[IdeonService, InMemoryIdeonRegistry]:
    store = InMemoryIdeonRegistry("reference-test@0.1")
    declarations = (
        ("sit.past", 3, ("sat",), "action", Question.WHAT, ()),
        ("on.spatial", 5, ("on",), "relation", Question.WHERE, ()),
        ("the.definite", 7, ("the",), "property", Question.WHAT, ()),
        (
            "bank.river-edge",
            11,
            ("bank",),
            "entity",
            Question.WHERE,
            (("domain", "river"),),
        ),
        (
            "bank.financial-institution",
            13,
            ("bank",),
            "entity",
            Question.WHERE,
            (("domain", "finance"),),
        ),
    )
    entries: list[SenseEntry] = []
    for entry_id, key, forms, semantic_type, question, contexts in declarations:
        ref = store.intern(
            IdeonGraph(
                IdeonIdentity(
                    namespace="prime-language/en-GB@0.1",
                    ruleset="principia@0.1",
                    name=entry_id,
                    kind=IdeonKind.PRIME,
                )
            ),
            stable_key=key,
        )
        entries.append(
            SenseEntry(
                entry_id=entry_id,
                ideon_key=ref.key,
                ideon_revision=ref.revision,
                profile_id=PROFILE.identifier,
                forms=forms,
                lexeme=entry_id.split(".")[0],
                sense=entry_id,
                semantic_type=semantic_type,
                question_slot=question,
                valid_contexts=contexts,
                provenance=("fixture:principia-v0",),
            )
        )
    return IdeonService(store, LogicDictionary(tuple(entries)), PROFILE), store


def test_sat_on_bank_builds_open_graph_and_round_trips_exact_reference() -> None:
    service, store = reference_service()
    record = service.interpret(
        InterpretRequest("sat on the bank", "fixture:bank-open", "internal-test-fixture")
    )

    assert record.decision.decision is Decision.ASK
    assert record.openings == ("who:actor", "where:location-sense")
    graph = service.resolve(
        next(
            ref
            for ref in store.revisions(record.ideon_key)
            if ref.revision == record.ideon_revision
        )
    )
    assert graph.identity.kind is IdeonKind.OPEN
    assert graph.openings == tuple(sorted(record.openings))
    assert Property("source-id", "fixture:bank-open") in graph.annotations
    assert Property("rights", "internal-test-fixture") in graph.annotations
    assert service.realise(store.revisions(record.ideon_key)[0]) == "sat on the bank"
    assert len(graph.identity.roots) > 1


def test_context_resolves_bank_and_actor_without_erasing_excluded_candidate() -> None:
    service, _ = reference_service()
    context = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river"},
        identity_fields=("actor", "domain"),
    )
    record = service.interpret(
        InterpretRequest(
            "sat on the bank",
            "fixture:bank-river",
            "internal-test-fixture",
            context,
        )
    )

    assert record.decision.decision is Decision.ANSWER
    assert record.openings == ()
    bank = record.candidate_sets[-1]
    assert [candidate.entry.sense for candidate in bank.viable] == ["bank.river-edge"]
    assert len(bank.candidates) == 2
    assert record.selected_entry_ids[-1] == "bank.river-edge"


def test_repeat_run_has_same_reference_and_byte_identical_structured_record() -> None:
    service, _ = reference_service()
    request = InterpretRequest(
        "sat on the bank",
        "fixture:repeat",
        "internal-test-fixture",
    )

    first = service.interpret(request)
    second = service.interpret(request)

    assert (first.ideon_key, first.ideon_revision) == (
        second.ideon_key,
        second.ideon_revision,
    )
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.record_id == second.record_id
    structured, plain = service.stream(request)
    assert structured == first.canonical_bytes()
    assert plain == "decision=ask; openings=who:actor, where:location-sense"


def test_source_order_and_identity_context_change_stable_identity() -> None:
    service, _ = reference_service()
    unresolved = ContextGraph.from_dict({"actor": "alice"}, identity_fields=("actor",))
    first = service.interpret(
        InterpretRequest("sat on the bank", "fixture:ordered-a", "fixture", unresolved)
    )
    reordered = service.interpret(
        InterpretRequest("the bank sat on", "fixture:ordered-b", "fixture", unresolved)
    )
    river = service.interpret(
        InterpretRequest(
            "sat on the bank",
            "fixture:river",
            "fixture",
            ContextGraph.from_dict(
                {"actor": "alice", "domain": "river"},
                identity_fields=("actor", "domain"),
            ),
        )
    )
    finance = service.interpret(
        InterpretRequest(
            "sat on the bank",
            "fixture:finance",
            "fixture",
            ContextGraph.from_dict(
                {"actor": "alice", "domain": "finance"},
                identity_fields=("actor", "domain"),
            ),
        )
    )

    assert first.ideon_key != reordered.ideon_key
    assert river.ideon_key != finance.ideon_key


def test_json_uses_decimal_strings_for_arbitrary_precision_keys() -> None:
    service, _ = reference_service()
    record = service.interpret(InterpretRequest("sat", "fixture:key", "fixture"))
    payload = json.loads(record.canonical_bytes())

    assert payload["ideon_key"] == str(record.ideon_key)
    assert isinstance(payload["ideon_key"], str)


def test_large_composite_key_serializes_without_changing_interpreter_limits() -> None:
    service, _ = reference_service()
    surface = " ".join(f"unknown{index}" for index in range(20))

    record = service.interpret(InterpretRequest(surface, "fixture:large-key", "fixture"))
    payload = json.loads(record.canonical_bytes())

    assert len(payload["ideon_key"]) > 4_300
    assert payload["ideon_key"].isdigit()
    assert record.record_id == record.record_id


def test_unresolved_forms_and_context_are_preserved_at_the_right_identity_layer() -> None:
    service, store = reference_service()

    quux = service.interpret(InterpretRequest("quux", "fixture:quux", "fixture"))
    xyzzy = service.interpret(InterpretRequest("xyzzy", "fixture:xyzzy", "fixture"))
    alice = service.interpret(
        InterpretRequest(
            "sat",
            "fixture:alice",
            "fixture",
            ContextGraph.from_dict({"actor": "alice"}),
        )
    )
    bob = service.interpret(
        InterpretRequest(
            "sat",
            "fixture:bob",
            "fixture",
            ContextGraph.from_dict({"actor": "bob"}),
        )
    )

    assert quux.ideon_key != xyzzy.ideon_key
    assert alice.ideon_key == bob.ideon_key
    assert alice.ideon_revision != bob.ideon_revision
    alice_graph = store.resolve(
        next(
            ref for ref in store.revisions(alice.ideon_key) if ref.revision == alice.ideon_revision
        )
    )
    assert any(
        node.kind.value == "context" and Property("actor", "alice") in node.properties
        for node in alice_graph.nodes
    )


def test_reference_runtime_manifests_prime_dictionary_keys_and_controller_views() -> None:
    runtime = create_reference_ideon_runtime()
    request = InterpretRequest("sat on the bank", "fixture:runtime", "fixture")

    record = runtime.controller.interpret(request)
    ref = runtime.store.latest(record.ideon_key)

    assert all(kind is IdeonKind.PRIME for _, _, kind in runtime.store.manifest()[:10])
    assert [key for key, _, _ in runtime.store.manifest()[:10]] == [
        3,
        5,
        7,
        11,
        13,
        17,
        19,
        23,
        29,
        31,
    ]
    assert runtime.controller.resolve(ref).identity.kind is IdeonKind.OPEN
    assert runtime.controller.realise(ref) == "sat on the bank"
    structured, plain = runtime.controller.stream(request)
    assert structured == record.canonical_bytes()
    assert plain.startswith("decision=ask")


def test_should_assessment_is_preserved_in_the_canonical_runtime_record() -> None:
    service, _ = reference_service()
    context = ContextGraph.from_dict({"actor": "operator"})
    assessment = ShouldAssessment(
        action="publish credentials",
        affected_entities=("account-holder",),
        norm_or_goal="protect access",
        evidence=("fixture:security-policy",),
        relevance=Decimal("1"),
        severity=Decimal("0.9"),
        confidence_or_margin=Decimal("0.8"),
        reversibility=Decimal("0.1"),
        context=context,
        recommended_alternative="rotate credentials",
        forbidden=True,
    )

    record = service.interpret(
        InterpretRequest(
            "sat",
            "fixture:should-record",
            "fixture",
            context,
            should=assessment,
            action_requested=True,
        )
    )
    payload = json.loads(record.canonical_bytes())

    assert payload["should_assessment"]["severity"] == "0.9"
    assert payload["should_assessment"]["forbidden"] is True
    assert payload["should_assessment"]["recommended_alternative"] == "rotate credentials"
    assert payload["decision"]["policy"] == {"should-stop-threshold": "0.64"}


def test_dictionary_edition_order_is_identity_bearing() -> None:
    runtime = create_reference_ideon_runtime()
    context = ContextGraph.from_dict({"actor": "speaker"}, identity_fields=("actor",))

    conventional = runtime.service.interpret(
        InterpretRequest("the dictionary 2nd edition", "fixture:edition-a", "fixture", context)
    )
    reordered = runtime.service.interpret(
        InterpretRequest("dictionary the 2nd edition", "fixture:edition-b", "fixture", context)
    )

    assert conventional.ideon_key != reordered.ideon_key


def test_declared_equivalent_paraphrases_share_key_but_retain_exact_revisions() -> None:
    store = InMemoryIdeonRegistry("equivalence-test@0.1")
    declared_identity = IdeonIdentity(
        namespace="prime-language/en-GB@0.1",
        ruleset="principia@0.1+declared-equivalence@0.1",
        name="alice-sits-at-river-bank",
        kind=IdeonKind.COMPOSITE,
        roles=(IdentityRole("actor", 3), IdentityRole("location", 11)),
        order=(3, 11),
    )
    first = store.intern(
        IdeonGraph(
            declared_identity,
            annotations=(Property("surface", "Alice sat on the river bank"),),
        )
    )
    paraphrase = store.intern(
        IdeonGraph(
            declared_identity,
            annotations=(Property("surface", "Alice was seated beside the river"),),
        )
    )

    assert first.key == paraphrase.key
    assert first.revision != paraphrase.revision
    assert store.resolve(first).annotations != store.resolve(paraphrase).annotations
