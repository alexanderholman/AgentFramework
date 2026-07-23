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
    NodeKind,
    Property,
)
from af.domain.ideons.dictionary import LogicDictionary
from af.domain.ideons.records import (
    ContextGraph,
    Decision,
    InquiryStatus,
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
    # Each element: (entry_id, key, forms, semantic_type, question, valid_contexts, semantic_roles)
    declarations = (
        ("sit.past", 3, ("sat",), "action", Question.WHAT, (), ("agent",)),
        ("on.spatial", 5, ("on",), "relation", Question.WHERE, (), ("location",)),
        ("the.definite", 7, ("the",), "property", Question.WHAT, (), ()),
        (
            "bank.river-edge",
            11,
            ("bank",),
            "entity",
            Question.WHERE,
            (("domain", "river"),),
            (),
        ),
        (
            "bank.financial-institution",
            13,
            ("bank",),
            "entity",
            Question.WHERE,
            (("domain", "finance"),),
            (),
        ),
    )
    entries: list[SenseEntry] = []
    for entry_id, key, forms, semantic_type, question, contexts, roles in declarations:
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
                semantic_roles=roles,
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
    # Source ID and rights belong in InterpretationRecord, not in the semantic graph.
    assert all(prop.name != "source-id" for prop in graph.annotations)
    assert all(prop.name != "rights" for prop in graph.annotations)
    assert record.source_id == "fixture:bank-open"
    assert record.rights == "internal-test-fixture"
    assert service.realise(store.revisions(record.ideon_key)[0]) == "sat on the bank"
    assert len(graph.identity.roots) > 1


def test_same_semantics_from_different_sources_share_graph_revision() -> None:
    """Occurrence provenance (source_id, rights) belongs in InterpretationRecord only.
    Same semantic topology from different sources must share the same graph revision
    while retaining distinct interpretation records."""
    service, _ = reference_service()
    record_a = service.interpret(
        InterpretRequest("sat on the bank", "source-a", "internal-test-fixture")
    )
    record_b = service.interpret(
        InterpretRequest("sat on the bank", "source-b", "internal-test-fixture")
    )

    assert record_a.ideon_key == record_b.ideon_key
    assert record_a.ideon_revision == record_b.ideon_revision
    assert record_a.source_id != record_b.source_id
    assert record_a.record_id != record_b.record_id


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


def test_temporal_qualification_node_in_graph_when_when_is_satisfied() -> None:
    """End-to-end: a valid context.time produces a typed 'temporal-qualification' node
    in the interpretation graph, proving the WhenCondition model is applied globally.
    Different temporal contexts produce different graph revisions; an invalid timestamp
    does not produce a temporal-qualification node (fail-closed)."""
    service, store = reference_service()
    ctx_with_time = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-01-01T00:00:00Z"}
    )
    ctx_without_time = ContextGraph.from_dict({"actor": "alice", "domain": "river"})
    ctx_invalid_time = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "not-a-date"}
    )

    record_time = service.interpret(
        InterpretRequest(
            "sat on the bank", "fixture:e2e-time", "internal-test-fixture", ctx_with_time
        )
    )
    record_no_time = service.interpret(
        InterpretRequest(
            "sat on the bank", "fixture:e2e-no-time", "internal-test-fixture", ctx_without_time
        )
    )
    record_invalid = service.interpret(
        InterpretRequest(
            "sat on the bank", "fixture:e2e-invalid", "internal-test-fixture", ctx_invalid_time
        )
    )

    # Valid timestamp produces a different graph revision than no timestamp
    assert record_time.ideon_revision != record_no_time.ideon_revision

    # The graph with a valid time includes a temporal-qualification node
    graph_with = store.resolve(
        next(
            r
            for r in store.revisions(record_time.ideon_key)
            if r.revision == record_time.ideon_revision
        )
    )
    tq_node = next(
        (node for node in graph_with.nodes if node.node_id == "temporal-qualification"),
        None,
    )
    assert tq_node is not None
    prop_names = {p.name for p in tq_node.properties}
    assert "when-kind" in prop_names
    assert "when-value" in prop_names
    assert any(p.name == "when-kind" and p.value == "timestamp" for p in tq_node.properties)

    # Graph without timestamp has no temporal-qualification node
    graph_without = store.resolve(
        next(
            r
            for r in store.revisions(record_no_time.ideon_key)
            if r.revision == record_no_time.ideon_revision
        )
    )
    assert not any(n.node_id == "temporal-qualification" for n in graph_without.nodes)

    # Invalid timestamp: WHEN fails closed → no temporal-qualification node
    graph_invalid = store.resolve(
        next(
            r
            for r in store.revisions(record_invalid.ideon_key)
            if r.revision == record_invalid.ideon_revision
        )
    )
    assert not any(n.node_id == "temporal-qualification" for n in graph_invalid.nodes)
    # Invalid timestamp leaves WHEN NOT_APPLICABLE (fail-closed)
    forms_invalid = service.encode_forms("sat on the bank")
    candidates_invalid = service.lookup_senses(forms_invalid, ctx_invalid_time)
    inquiry_invalid = service.query_openings(forms_invalid, candidates_invalid, ctx_invalid_time)
    assert inquiry_invalid.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE


def test_interpretation_graph_is_surface_bound() -> None:
    """The interpretation graph includes surface FORM nodes (interpretation graph
    contract).  Same surface from different sources yields the same revision.
    Different surfaces (even if semantically related) produce different revisions
    unless declared equivalent via IdeonIdentity."""
    service, _ = reference_service()

    # Same surface, different sources: same graph revision (already tested separately)
    same_surface_a = service.interpret(
        InterpretRequest("sat on the bank", "source-a", "internal-test-fixture")
    )
    same_surface_b = service.interpret(
        InterpretRequest("sat on the bank", "source-b", "internal-test-fixture")
    )
    assert same_surface_a.ideon_revision == same_surface_b.ideon_revision

    # Different word order (different FORM sequence): different revision
    different_surface = service.interpret(
        InterpretRequest("the bank sat on", "source-a", "internal-test-fixture")
    )
    assert same_surface_a.ideon_revision != different_surface.ideon_revision


def test_temporal_qualification_linked_to_predicate_via_edge() -> None:
    """End-to-end: the temporal-qualification node is connected to each predicate
    SENSE node via a 'qualifies-predicate' IdeonEdge in the resolved graph.
    The node carries 'when-kind', 'when-value', and 'tense' properties.
    This proves the temporal qualification is structurally attached to the
    proposition it qualifies, not floating in isolation."""
    from af.domain.ideons import IdeonEdge  # noqa: F401

    service, store = reference_service()
    ctx = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-01-01T00:00:00Z"}
    )

    record = service.interpret(
        InterpretRequest("sat on the bank", "fixture:tq-edge", "internal-test-fixture", ctx)
    )
    graph = store.resolve(
        next(r for r in store.revisions(record.ideon_key) if r.revision == record.ideon_revision)
    )

    # Temporal-qualification node must be present with tense, when-kind, when-value
    tq_node = next((n for n in graph.nodes if n.node_id == "temporal-qualification"), None)
    assert tq_node is not None
    prop_by_name = {p.name: p.value for p in tq_node.properties}
    assert prop_by_name["when-kind"] == "timestamp"
    assert "tense" in prop_by_name

    # Must have at least one 'qualifies-predicate' edge from temporal-qualification
    tq_edges = [
        e
        for e in graph.edges
        if e.source == "temporal-qualification" and e.relation == "qualifies-predicate"
    ]
    assert tq_edges, "temporal-qualification must be linked to at least one predicate node"
    # Each target must be a SENSE node (predicate)
    node_by_id = {n.node_id: n for n in graph.nodes}
    for edge in tq_edges:
        target_node = node_by_id[edge.target]
        assert target_node.kind is NodeKind.SENSE


def test_temporal_qualification_tense_past_present_projected() -> None:
    """End-to-end: past/present/projected tense is correctly stored in the graph
    when a reference_when is supplied alongside an event timestamp.
    Different reference times produce different tense values and different revisions."""
    from datetime import UTC, datetime

    from af.domain.ideons.temporal import WhenCondition, WhenKind

    service, store = reference_service()
    ctx_river = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-06-01T00:00:00Z"}
    )

    ref_past = WhenCondition(WhenKind.TIMESTAMP, instant=datetime(2026, 7, 1, tzinfo=UTC))
    ref_same = WhenCondition(WhenKind.TIMESTAMP, instant=datetime(2026, 6, 1, tzinfo=UTC))
    ref_future = WhenCondition(WhenKind.TIMESTAMP, instant=datetime(2026, 5, 1, tzinfo=UTC))

    def get_tense(ref: WhenCondition) -> str:
        rec = service.interpret(
            InterpretRequest(
                "sat on the bank",
                f"fixture:tense-{ref.instant.date() if ref.instant else 'x'}",
                "internal-test-fixture",
                ctx_river,
                reference_when=ref,
            )
        )
        g = store.resolve(
            next(r for r in store.revisions(rec.ideon_key) if r.revision == rec.ideon_revision)
        )
        tq = next((n for n in g.nodes if n.node_id == "temporal-qualification"), None)
        assert tq is not None
        return next(p.value for p in tq.properties if p.name == "tense")

    assert get_tense(ref_past) == "was"
    assert get_tense(ref_same) == "is"
    assert get_tense(ref_future) == "projected"


def test_temporal_qualification_with_non_timestamp_condition() -> None:
    """End-to-end: a criterion condition in context.when produces a temporal-qualification
    node with when-kind=criterion (no datetime required).
    The WHEN satisfaction and graph edge are verified through interpret() and resolve()."""
    service, store = reference_service()
    ctx = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "when": "criterion:task-approved"}
    )

    record = service.interpret(
        InterpretRequest("sat on the bank", "fixture:criterion-tq", "internal-test-fixture", ctx)
    )
    assert record.inquiries.get(Question.WHEN).status is InquiryStatus.SATISFIED

    graph = store.resolve(
        next(r for r in store.revisions(record.ideon_key) if r.revision == record.ideon_revision)
    )
    tq_node = next((n for n in graph.nodes if n.node_id == "temporal-qualification"), None)
    assert tq_node is not None
    prop_by_name = {p.name: p.value for p in tq_node.properties}
    assert prop_by_name["when-kind"] == "criterion"
    assert prop_by_name["when-value"] == "task-approved"
    assert "tense" in prop_by_name

    # Edge from temporal-qualification to predicate SENSE node
    tq_edges = [e for e in graph.edges if e.source == "temporal-qualification"]
    assert tq_edges, "criterion temporal-qualification must be linked to predicate"


def test_temporal_qualification_absent_when_invalid_context_when() -> None:
    """A plain (non-prefixed) context.when string fails closed: WHEN stays NOT_APPLICABLE,
    no temporal-qualification node is added, no edges are created."""
    service, store = reference_service()
    ctx = ContextGraph.from_dict({"actor": "alice", "domain": "river", "when": "task-approved"})

    record = service.interpret(
        InterpretRequest("sat on the bank", "fixture:invalid-when", "internal-test-fixture", ctx)
    )
    assert record.inquiries.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE

    graph = store.resolve(
        next(r for r in store.revisions(record.ideon_key) if r.revision == record.ideon_revision)
    )
    assert not any(n.node_id == "temporal-qualification" for n in graph.nodes)
    assert not any(e.relation == "qualifies-predicate" for e in graph.edges)


def test_incompatible_reference_when_yields_contested_tense_not_is() -> None:
    """Regression: when reference_when is provided but kinds are incompatible with
    the event WhenCondition, construct_graph must NOT silently default to tense=IS.
    The temporal-qualification node must carry tense='contested', proving the
    unresolved comparison is explicit rather than hidden.

    Cases tested:
    - TIMESTAMP event with CRITERION reference (incompatible kinds)
    - CRITERION event with TIMESTAMP reference (incompatible kinds)
    """
    from datetime import UTC, datetime

    from af.domain.ideons.temporal import WhenCondition, WhenKind

    service, store = reference_service()
    ctx = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-06-01T00:00:00Z"}
    )
    # reference_when is CRITERION — incompatible with TIMESTAMP event
    criterion_reference = WhenCondition(
        WhenKind.CRITERION, expression="task-complete", status="satisfied"
    )

    record = service.interpret(
        InterpretRequest(
            "sat on the bank",
            "fixture:contested-tense",
            "internal-test-fixture",
            ctx,
            reference_when=criterion_reference,
        )
    )
    graph = store.resolve(
        next(r for r in store.revisions(record.ideon_key) if r.revision == record.ideon_revision)
    )
    tq_node = next((n for n in graph.nodes if n.node_id == "temporal-qualification"), None)
    assert tq_node is not None, "temporal-qualification node must be present when WHEN is satisfied"
    prop_by_name = {p.name: p.value for p in tq_node.properties}
    # Must NOT silently default to 'is'; incompatible kinds yield 'contested'
    assert prop_by_name["tense"] == "contested", (
        f"expected tense='contested' for incompatible kinds, got tense={prop_by_name['tense']!r}"
    )

    # Inverted case: event is CRITERION, reference is TIMESTAMP
    ctx_criterion = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "when": "criterion:task-complete"}
    )
    timestamp_reference = WhenCondition(
        WhenKind.TIMESTAMP, instant=datetime(2026, 6, 1, tzinfo=UTC)
    )
    record_inv = service.interpret(
        InterpretRequest(
            "sat on the bank",
            "fixture:contested-tense-inv",
            "internal-test-fixture",
            ctx_criterion,
            reference_when=timestamp_reference,
        )
    )
    graph_inv = store.resolve(
        next(
            r
            for r in store.revisions(record_inv.ideon_key)
            if r.revision == record_inv.ideon_revision
        )
    )
    tq_inv = next((n for n in graph_inv.nodes if n.node_id == "temporal-qualification"), None)
    assert tq_inv is not None
    prop_inv = {p.name: p.value for p in tq_inv.properties}
    assert prop_inv["tense"] == "contested", (
        f"expected tense='contested' for inverted incompatible kinds, got {prop_inv['tense']!r}"
    )


def test_uncomparable_event_interval_with_timestamp_reference_yields_contested() -> None:
    """Regression: an INTERVAL event condition compared against a TIMESTAMP reference
    must also yield tense='contested' (not 'is').  Validates that the fail-closed
    behaviour covers all uncomparable kind pairs, not only criterion↔timestamp."""
    from datetime import UTC, datetime

    from af.domain.ideons.temporal import WhenCondition, WhenKind

    service, store = reference_service()
    # Event is an interval; reference is a timestamp — uncomparable
    ctx = ContextGraph.from_dict(
        {
            "actor": "alice",
            "domain": "river",
            "when": "interval:2026-01-01T00:00:00Z,2026-12-31T00:00:00Z",
        }
    )
    timestamp_reference = WhenCondition(
        WhenKind.TIMESTAMP, instant=datetime(2026, 6, 1, tzinfo=UTC)
    )
    record = service.interpret(
        InterpretRequest(
            "sat on the bank",
            "fixture:interval-contested",
            "internal-test-fixture",
            ctx,
            reference_when=timestamp_reference,
        )
    )
    graph = store.resolve(
        next(r for r in store.revisions(record.ideon_key) if r.revision == record.ideon_revision)
    )
    tq_node = next((n for n in graph.nodes if n.node_id == "temporal-qualification"), None)
    assert tq_node is not None
    prop_by_name = {p.name: p.value for p in tq_node.properties}
    assert prop_by_name["tense"] == "contested", (
        "INTERVAL event vs TIMESTAMP reference must yield tense='contested', "
        f"got {prop_by_name['tense']!r}"
    )
    assert prop_by_name["when-kind"] == "interval"


def test_no_reference_when_yields_unanchored_tense_for_all_when_kinds() -> None:
    """Regression: absent reference_when must yield tense='unanchored' for every WHEN kind.

    IS is reserved for a justified present/equal standpoint computed by render_tense.
    event:, criterion:, interval:, and ordering: conditions do not imply present tense
    by themselves; a timestamp without a reference perspective also has no standpoint.
    This test covers all four non-trivially-satisfying kinds plus a raw timestamp context.
    """
    service, store = reference_service()

    def get_tense_no_ref(ctx: ContextGraph, source_id: str) -> str:
        rec = service.interpret(
            InterpretRequest("sat on the bank", source_id, "internal-test-fixture", ctx)
        )
        g = store.resolve(
            next(r for r in store.revisions(rec.ideon_key) if r.revision == rec.ideon_revision)
        )
        tq = next((n for n in g.nodes if n.node_id == "temporal-qualification"), None)
        assert tq is not None, f"temporal-qualification missing for source={source_id!r}"
        return next(p.value for p in tq.properties if p.name == "tense")

    # EVENT — no reference standpoint → must not infer IS
    ctx_event = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "when": "event:door-opens"}
    )
    tense_event = get_tense_no_ref(ctx_event, "fixture:unanchored-event")
    assert tense_event == "unanchored", (
        f"EVENT with no reference must yield tense='unanchored', got {tense_event!r}"
    )

    # CRITERION — condition clause; does not prove the proposition holds now
    ctx_criterion = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "when": "criterion:task-approved"}
    )
    tense_criterion = get_tense_no_ref(ctx_criterion, "fixture:unanchored-criterion")
    assert tense_criterion == "unanchored", (
        f"CRITERION with no reference must yield tense='unanchored', got {tense_criterion!r}"
    )

    # INTERVAL — time range; standpoint relative to interval is unknown without reference
    ctx_interval = ContextGraph.from_dict(
        {
            "actor": "alice",
            "domain": "river",
            "when": "interval:2026-01-01T00:00:00Z,2026-12-31T00:00:00Z",
        }
    )
    tense_interval = get_tense_no_ref(ctx_interval, "fixture:unanchored-interval")
    assert tense_interval == "unanchored", (
        f"INTERVAL with no reference must yield tense='unanchored', got {tense_interval!r}"
    )

    # ORDERING — relational anchor; before/after/during relative to an event is not a tense
    ctx_ordering = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "when": "ordering:before:release-v1"}
    )
    tense_ordering = get_tense_no_ref(ctx_ordering, "fixture:unanchored-ordering")
    assert tense_ordering == "unanchored", (
        f"ORDERING with no reference must yield tense='unanchored', got {tense_ordering!r}"
    )

    # TIMESTAMP — even an exact instant has no standpoint without a declared reference
    ctx_timestamp = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-06-01T00:00:00Z"}
    )
    tense_timestamp = get_tense_no_ref(ctx_timestamp, "fixture:unanchored-timestamp")
    assert tense_timestamp == "unanchored", (
        f"TIMESTAMP with no reference must yield tense='unanchored', got {tense_timestamp!r}"
    )


def test_temporal_perspective_tense_status_property_in_graph() -> None:
    """End-to-end: the temporal-qualification node carries a 'tense-status' property
    with values 'resolved', 'unanchored', or 'contested' depending on the resolution path.

    Resolves: RESOLVED (reference supplied + compatible kinds), UNANCHORED (no reference),
    CONTESTED (reference supplied + incompatible kinds).  Validates that 'tense-status'
    is always present alongside 'tense' in the resolved graph."""
    from datetime import UTC, datetime

    from af.domain.ideons.temporal import WhenCondition, WhenKind

    service, store = reference_service()

    def get_props(
        ctx: ContextGraph, source_id: str, ref: WhenCondition | None = None
    ) -> dict[str, str]:
        rec = service.interpret(
            InterpretRequest(
                "sat on the bank", source_id, "internal-test-fixture", ctx, reference_when=ref
            )
        )
        g = store.resolve(
            next(r for r in store.revisions(rec.ideon_key) if r.revision == rec.ideon_revision)
        )
        tq = next((n for n in g.nodes if n.node_id == "temporal-qualification"), None)
        assert tq is not None, f"temporal-qualification missing for source={source_id!r}"
        return {p.name: p.value for p in tq.properties}

    # RESOLVED: timestamp event + timestamp reference → tense-status='resolved', tense='was'
    ctx_ts = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-06-01T00:00:00Z"}
    )
    ref_ts = WhenCondition(WhenKind.TIMESTAMP, instant=datetime(2026, 7, 1, tzinfo=UTC))
    props_resolved = get_props(ctx_ts, "fixture:status-resolved", ref_ts)
    assert props_resolved["tense-status"] == "resolved"
    assert props_resolved["tense"] == "was"

    # UNANCHORED: timestamp event, no reference → tense-status='unanchored', tense='unanchored'
    props_unanchored = get_props(ctx_ts, "fixture:status-unanchored")
    assert props_unanchored["tense-status"] == "unanchored"
    assert props_unanchored["tense"] == "unanchored"

    # CONTESTED: timestamp event + criterion reference (incompatible kinds)
    criterion_ref = WhenCondition(WhenKind.CRITERION, expression="x", status="satisfied")
    props_contested = get_props(ctx_ts, "fixture:status-contested", criterion_ref)
    assert props_contested["tense-status"] == "contested"
    assert props_contested["tense"] == "contested"
