"""Application boundary for deterministic Prime Language / Ideon interpretation."""

from __future__ import annotations

from dataclasses import dataclass, field

from af.domain.ideons import (
    IdentityRole,
    IdeonEdge,
    IdeonGraph,
    IdeonIdentity,
    IdeonKind,
    IdeonNode,
    IdeonRef,
    IdeonStore,
    NodeKind,
    Property,
)
from af.domain.ideons.dictionary import LogicDictionary
from af.domain.ideons.forms import LosslessFormEncoder
from af.domain.ideons.inquiry import DecisionEngine, InquiryEngine
from af.domain.ideons.principia import SetStack
from af.domain.ideons.records import (
    CandidateSet,
    ContextGraph,
    DecisionTrace,
    FormSequence,
    InquiryState,
    InquiryStatus,
    InterpretationRecord,
    LanguageProfileRef,
    Question,
    ShouldAssessment,
)
from af.domain.ideons.temporal import (
    TemporalPerspective,
    TemporalPerspectiveStatus,
    WhenCondition,
    WhenKind,
    parse_when_expression,
    render_tense,
)


def _parse_context_time(context: ContextGraph) -> WhenCondition | None:
    """Parse ``context.time`` as a timezone-aware TIMESTAMP ``WhenCondition``, fail-closed.

    Returns ``None`` when the field is absent, unparseable, or naive (no tzinfo).
    """
    from datetime import datetime

    time_val = context.get("time")
    if time_val is None:
        return None
    try:
        dt = datetime.fromisoformat(time_val)
        if dt.tzinfo is not None:
            return WhenCondition(WhenKind.TIMESTAMP, instant=dt)
    except (ValueError, OverflowError):
        pass
    return None


def _parse_context_when(context: ContextGraph) -> WhenCondition | None:
    """Parse ``context.when`` via ``parse_when_expression``, fail-closed.

    Returns ``None`` when the field is absent or does not match the bounded
    ``kind:expression`` format.
    """
    when_val = context.get("when")
    if when_val is None:
        return None
    return parse_when_expression(when_val)


def _make_when_condition_from_context(context: ContextGraph) -> WhenCondition | None:
    """Extract a validated ``WhenCondition`` from a single context witness, fail-closed.

    When **both** ``context.time`` (valid timestamp) and ``context.when`` (valid
    expression) are present, returns ``None`` — this signals a conflict that the caller
    must handle explicitly rather than silently picking one.  Use
    ``_parse_context_time`` and ``_parse_context_when`` directly when the conflict case
    requires a contested temporal-qualification node.

    When exactly one field is valid, returns that ``WhenCondition``.
    Returns ``None`` when neither field yields a valid condition.
    """
    time_cond = _parse_context_time(context)
    when_cond = _parse_context_when(context)
    if time_cond is not None and when_cond is not None:
        return None  # Conflict: both witnesses present; caller must handle explicitly.
    return time_cond or when_cond


@dataclass(frozen=True, slots=True)
class InterpretRequest:
    surface: str
    source_id: str
    rights: str
    context: ContextGraph = field(default_factory=ContextGraph)
    should: ShouldAssessment | None = None
    action_requested: bool = False
    media_complete: bool = True
    reference_when: WhenCondition | None = None
    """Optional reference temporal perspective for tense computation.

    When provided alongside a timestamp or condition in ``context``, the engine
    computes ``WAS / IS / PROJECTED`` by calling ``render_tense(event, reference_when)``.
    When absent, the tense is recorded as ``"unanchored"`` — the WHEN condition is
    preserved in the graph but no tense standpoint can be asserted without a declared
    reference.  ``IS`` is only emitted when ``render_tense`` determines a present/equal
    standpoint from a supplied reference.
    """

    def __post_init__(self) -> None:
        if not isinstance(self.surface, str) or not self.surface:
            raise ValueError("interpretation surface must be non-empty text")
        if not self.source_id:
            raise ValueError("source id must not be empty")
        if not self.rights:
            raise ValueError("rights declaration must not be empty")
        if self.reference_when is not None and not isinstance(self.reference_when, WhenCondition):
            raise TypeError("reference_when must be a WhenCondition or None")


class IdeonService:
    """One shared use case for deterministic records and future route adapters."""

    def __init__(
        self,
        store: IdeonStore,
        dictionary: LogicDictionary,
        profile: LanguageProfileRef,
        *,
        set_stack: SetStack | None = None,
        encoder: LosslessFormEncoder | None = None,
        inquiry: InquiryEngine | None = None,
        decisions: DecisionEngine | None = None,
    ) -> None:
        self._store = store
        self._dictionary = dictionary
        self._profile = profile
        self._sets = set_stack or SetStack()
        self._encoder = encoder or LosslessFormEncoder()
        self._inquiry = inquiry or InquiryEngine()
        self._decisions = decisions or DecisionEngine()

    def encode_forms(self, media: str) -> FormSequence:
        return self._encoder.encode(media, self._profile)

    def lookup_senses(
        self,
        forms: FormSequence,
        context: ContextGraph,
    ) -> tuple[CandidateSet, ...]:
        return self._dictionary.candidates(forms, context)

    def query_openings(
        self,
        forms: FormSequence,
        candidates: tuple[CandidateSet, ...],
        context: ContextGraph,
        *,
        should: ShouldAssessment | None = None,
    ) -> InquiryState:
        return self._inquiry.query(forms, candidates, context, should=should)

    def decide(
        self,
        inquiry: InquiryState,
        *,
        should: ShouldAssessment | None = None,
        action_requested: bool = False,
        media_complete: bool = True,
    ) -> DecisionTrace:
        return self._decisions.decide(
            inquiry,
            should=should,
            action_requested=action_requested,
            media_complete=media_complete,
        )

    def construct_graph(
        self,
        forms: FormSequence,
        candidates: tuple[CandidateSet, ...],
        context: ContextGraph,
        inquiry: InquiryState,
        *,
        source_id: str | None = None,
        rights: str | None = None,
        reference_when: WhenCondition | None = None,
    ) -> IdeonGraph:
        """Build an interpretation graph for the supplied utterance.

        **Interpretation graph contract**: this graph is surface-bound.  FORM nodes and
        the ``surface`` annotation include the original surface text so that the same
        surface from different sources yields the same graph revision.  Paraphrases of
        semantically equivalent content produce different revisions unless equivalence is
        explicitly declared via ``IdeonIdentity``; use ``InterpretationRecord`` for the
        occurrence-level provenance (source, rights, span).

        When WHEN is satisfied, a typed ``temporal-qualification`` node is added with
        ``temporal-perspective-status``, ``when-kind``, and ``when-value`` properties.
        When the standpoint is resolved (reference supplied + compatible kinds), a
        ``resolved-tense`` property is also added with one of ``was | is | projected``.
        For unresolved states (``unanchored`` or ``contested``), ``resolved-tense`` is
        absent; only ``temporal-perspective-status`` carries the epistemic state.

        **Two-witness contract (fail-closed)**: when both ``context.time`` (a valid
        timezone-aware ISO 8601 timestamp) and ``context.when`` (a valid
        ``kind:expression`` condition) are present simultaneously, neither is silently
        prioritised.  The ``temporal-qualification`` node is emitted with
        ``temporal-perspective-status="contested"`` and a ``when-additional`` property
        carrying the raw ``context.when`` expression, so that both witnesses are
        preserved in the graph.  ``resolved-tense`` is absent in this case; the
        ``reference_when`` parameter is not consulted.  When only one witness is valid,
        the single-witness path applies normally: ``render_tense(event_when,
        reference_when)`` is called when a reference is supplied.  A directed
        ``IdeonEdge`` with relation ``qualifies-predicate`` connects the
        ``temporal-qualification`` node to each predicate (action/state/relation) SENSE
        node in the graph, making the temporal qualification structurally attached to the
        proposition it qualifies.
        """
        nodes: list[IdeonNode] = []
        edges: list[IdeonEdge] = []
        roles: list[IdentityRole] = []
        order: list[int] = []
        roots: list[str] = []
        selected_keys: list[int] = []
        unresolved_identity: list[Property] = []
        predicate_node_ids: list[str] = []  # SENSE nodes for predicates (action/state/relation)
        for form_index, candidate_set in enumerate(candidates):
            form = forms.forms[form_index]
            form_node_id = f"form-{form_index}"
            nodes.append(
                IdeonNode(
                    form_node_id,
                    NodeKind.FORM,
                    properties=(
                        Property("form-type", form.form_type),
                        Property("normalized", form.normalized),
                        Property("surface", form.surface),
                    ),
                )
            )
            roots.append(form_node_id)
            if not candidate_set.viable:
                unresolved_identity.append(
                    Property(
                        f"unresolved-form-{form_index}",
                        f"{form.form_type}:{form.normalized}",
                    )
                )
            for candidate_index, candidate in enumerate(candidate_set.viable):
                ref = IdeonRef(candidate.entry.ideon_key, candidate.entry.ideon_revision)
                node_id = f"sense-{form_index}-{candidate_index}"
                nodes.append(
                    IdeonNode(
                        node_id,
                        NodeKind.SENSE,
                        ideon=ref,
                        properties=(
                            Property("entry-id", candidate.entry.entry_id),
                            Property("lexeme", candidate.entry.lexeme),
                            Property("sense", candidate.entry.sense),
                        ),
                        provenance=candidate.entry.provenance,
                    )
                )
                roots.append(node_id)
                roles.append(
                    IdentityRole(f"token-{form_index}-candidate-{candidate_index}", ref.key)
                )
                order.append(ref.key)
                selected_keys.append(ref.key)
                if candidate.entry.semantic_type in {"action", "state", "relation"}:
                    predicate_node_ids.append(node_id)

        nodes.append(
            IdeonNode(
                "context",
                NodeKind.CONTEXT,
                properties=tuple(Property(name, value) for name, value in context.values),
            )
        )

        # Add a typed temporal-qualification node when WHEN is satisfied.
        # The node carries ``temporal-perspective-status``, ``when-kind``, ``when-value``,
        # and — when resolved — ``resolved-tense`` properties.  It is linked to every
        # predicate SENSE node via a ``qualifies-predicate`` edge.
        #
        # Tense resolution rules:
        # - When **both** ``context.time`` and ``context.when`` are valid, neither can be
        #   silently prioritised (option 2: fail-closed).  The node is emitted with
        #   ``temporal-perspective-status="contested"`` and a ``when-additional`` property
        #   carrying the raw ``context.when`` expression so that neither witness is
        #   discarded from the graph.  ``resolved-tense`` is absent.
        # - When only one is valid, the single-witness path applies:
        #   - ``reference_when`` absent → status ``"unanchored"``; ``resolved-tense`` absent.
        #   - ``reference_when`` supplied + compatible kinds → status ``"resolved"``; the
        #     ``resolved-tense`` property carries ``was | is | projected``.
        #   - ``reference_when`` supplied + incompatible kinds → status ``"contested"``;
        #     ``resolved-tense`` absent.
        # IS is reserved for a justified present/equal standpoint from ``render_tense``.
        when_result = inquiry.get(Question.WHEN)
        if when_result.status is InquiryStatus.SATISFIED:
            time_cond = _parse_context_time(context)
            when_cond = _parse_context_when(context)
            both_present = time_cond is not None and when_cond is not None

            if both_present:
                # Two valid WHEN witnesses — fail closed: emit contested node with both.
                assert time_cond is not None  # both_present guarantees this
                event_when: WhenCondition | None = time_cond
                perspective = TemporalPerspective(
                    status=TemporalPerspectiveStatus.CONTESTED, when=time_cond
                )
            else:
                event_when = time_cond or when_cond

            if event_when is not None:
                if not both_present:
                    # Single-witness path: compute tense perspective.
                    if reference_when is None:
                        perspective = TemporalPerspective(
                            status=TemporalPerspectiveStatus.UNANCHORED, when=event_when
                        )
                    else:
                        try:
                            resolved_tense = render_tense(event_when, reference_when)
                            perspective = TemporalPerspective(
                                status=TemporalPerspectiveStatus.RESOLVED,
                                when=event_when,
                                tense=resolved_tense,
                            )
                        except ValueError:
                            # Incompatible or uncomparable kinds — do not silently default to IS.
                            perspective = TemporalPerspective(
                                status=TemporalPerspectiveStatus.CONTESTED, when=event_when
                            )

                # Build the WhenCondition representation for graph properties.
                when_value: str
                if event_when.kind is WhenKind.TIMESTAMP:
                    when_value = event_when.instant.isoformat() if event_when.instant else ""
                elif event_when.kind is WhenKind.INTERVAL:
                    start = (
                        event_when.interval_start.isoformat() if event_when.interval_start else ""
                    )
                    end = event_when.interval_end.isoformat() if event_when.interval_end else ""
                    when_value = f"{start},{end}"
                else:
                    when_value = event_when.expression or ""

                tq_node_id = "temporal-qualification"
                tq_properties: list[Property] = [
                    # temporal-perspective-status: resolution status, always present
                    # (resolved | unanchored | contested).
                    Property("temporal-perspective-status", perspective.status_label),
                    Property("when-kind", event_when.kind.value),
                    Property("when-value", when_value),
                ]
                if both_present:
                    # Both witnesses present: record the condition-bearing witness as a
                    # separate property so neither is silently discarded from the graph.
                    raw_when = context.get("when")
                    assert raw_when is not None  # both_present guarantees context.when valid
                    tq_properties.append(Property("when-additional", raw_when))
                if perspective.tense is not None:
                    # resolved-tense: only present when the standpoint is resolved.
                    # Values are exactly was | is | projected; unresolved states
                    # (unanchored, contested) must never appear in this field.
                    tq_properties.insert(0, Property("resolved-tense", perspective.tense.value))
                nodes.append(
                    IdeonNode(
                        tq_node_id,
                        NodeKind.VALUE,
                        properties=tuple(tq_properties),
                    )
                )
                # Attach temporal qualification to each predicate SENSE node.
                for pred_id in predicate_node_ids:
                    edges.append(
                        IdeonEdge(
                            source=tq_node_id,
                            relation="qualifies-predicate",
                            target=pred_id,
                            source_role="temporal-qualification",
                            target_role="predicate",
                        )
                    )

        opening_ids = tuple(
            f"{result.question.value}:{result.role or 'unspecified'}"
            for result in inquiry.material_openings
        )
        identity_context = tuple(
            Property(name, value)
            for name, value in (
                *self._sets.as_identity_context(),
                *context.identity_values,
            )
        ) + tuple(unresolved_identity)
        identity = IdeonIdentity(
            namespace=f"prime-language/{self._profile.identifier}",
            ruleset="principia@0.1",
            name="utterance",
            kind=IdeonKind.OPEN if opening_ids else IdeonKind.COMPOSITE,
            roles=tuple(roles),
            order=tuple(order),
            context=identity_context,
            roots=tuple(roots),
        )
        # Source ID and rights belong in InterpretationRecord (occurrence provenance), not in
        # the semantic graph.  Including them here would create a new graph revision for every
        # new source even when the semantic topology is identical.  The ``construct_graph``
        # signature retains those parameters so callers can audit the split boundary, but they
        # are not written into the graph.
        annotations = [
            Property("profile", self._profile.identifier),
            Property("selected-key-count", len(selected_keys)),
            Property("surface", forms.surface),
        ]
        provenance = ["language-profile:" + self._profile.identifier]
        return IdeonGraph(
            identity=identity,
            nodes=tuple(nodes),
            edges=tuple(edges),
            openings=opening_ids,
            annotations=tuple(annotations),
            provenance=tuple(provenance),
        )

    def intern(self, graph: IdeonGraph) -> IdeonRef:
        return self._store.intern(graph)

    def resolve(self, ref: IdeonRef) -> IdeonGraph:
        return self._store.resolve(ref)

    def realise(self, ref: IdeonRef) -> str:
        graph = self.resolve(ref)
        for annotation in graph.annotations:
            if annotation.name == "surface" and isinstance(annotation.value, str):
                return annotation.value
        raise ValueError("Ideon revision has no surface realisation")

    def interpret(self, request: InterpretRequest) -> InterpretationRecord:
        forms = self.encode_forms(request.surface)
        candidates = self.lookup_senses(forms, request.context)
        inquiry = self.query_openings(
            forms,
            candidates,
            request.context,
            should=request.should,
        )
        decision = self.decide(
            inquiry,
            should=request.should,
            action_requested=request.action_requested,
            media_complete=request.media_complete,
        )
        graph = self.construct_graph(
            forms,
            candidates,
            request.context,
            inquiry,
            source_id=request.source_id,
            rights=request.rights,
            reference_when=request.reference_when,
        )
        ref = self.intern(graph)
        selected = tuple(
            candidate_set.viable[0].entry.entry_id
            for candidate_set in candidates
            if len(candidate_set.viable) == 1
        )
        openings = tuple(
            f"{opening.question.value}:{opening.role or 'unspecified'}"
            for opening in inquiry.material_openings
        )
        actual_output = (
            ("decision", decision.decision.value),
            ("plain_text", self._plain_text(decision, inquiry)),
        )
        return InterpretationRecord(
            source_id=request.source_id,
            source_span=(0, len(request.surface)),
            rights=request.rights,
            forms=forms,
            candidate_sets=candidates,
            selected_entry_ids=selected,
            ideon_key=ref.key,
            ideon_revision=ref.revision,
            context=request.context,
            inquiries=inquiry,
            openings=openings,
            decision=decision,
            actual_output=actual_output,
            should_assessment=request.should,
        )

    def stream(self, request: InterpretRequest) -> tuple[bytes, str]:
        record = self.interpret(request)
        return record.canonical_bytes(), dict(record.actual_output)["plain_text"]

    @staticmethod
    def _plain_text(decision: DecisionTrace, inquiry: InquiryState) -> str:
        openings = ", ".join(
            f"{item.question.value}:{item.role or 'unspecified'}"
            for item in inquiry.material_openings
        )
        if openings:
            return f"decision={decision.decision.value}; openings={openings}"
        return f"decision={decision.decision.value}; closure=satisfied"


class IdeonController:
    """Thin application-facing controller shared by presentation adapters."""

    def __init__(self, service: IdeonService) -> None:
        self._service = service

    def interpret(self, request: InterpretRequest) -> InterpretationRecord:
        return self._service.interpret(request)

    def resolve(self, ref: IdeonRef) -> IdeonGraph:
        return self._service.resolve(ref)

    def realise(self, ref: IdeonRef) -> str:
        return self._service.realise(ref)

    def stream(self, request: InterpretRequest) -> tuple[bytes, str]:
        return self._service.stream(request)
