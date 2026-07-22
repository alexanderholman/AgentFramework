# Prime Language / Ideon: first Agent Framework vertical slice

Status: implemented deterministic reference slice. The focused suite executed 86 tests with
91% branch coverage, while the full repository suite clears its 90% gate.
This is not a claim that the complete
Prime Language specification or a general natural-language parser has been implemented.

This note narrows [Prime Language and Ideon System v0.1](prime-language-ideon-system-spec-v0.1.md)
to the smallest useful Agent Framework (AF) path: typed Ideon graphs, deterministic identity
and revision handling, a small logical dictionary, explicit non-closure, and an application
service that can interpret controlled examples. It establishes reference semantics on which
later Prolog and neural routes can depend.

## Scope

The slice includes:

- immutable, typed graph values for nodes, compact edges, relation nodes/hyperedges, context, openings, decisions, provenance, stable references, and graph revisions;
- deterministic canonical serialisation, order-sensitive composition, identity-bearing context, and graph revision derivation;
- a registry that interns, deduplicates, and resolves Ideons without treating their numeric keys as their meanings;
- a minimal, concrete, versioned English logical dictionary sufficient for the controlled acceptance fixtures, including both senses of `bank`;
- construction of complete or open Ideons, all eleven inquiry fields (seven with deterministic behaviour; four represented but not yet evaluated), and deterministic ask/answer/stop decisions for declared fixtures;
- exact structured records shared by the implemented REST and CLI adapters; and
- an in-memory persistence adapter for unit and conformance testing.

The current AF generic procedure pipeline is string-oriented. It is not the authoritative representation for this subsystem and must not sit inside the domain layer. A narrow adapter may later let that pipeline submit an interpretation request and consume an Ideon reference or decision.

## Non-scope and present limitations

- This is a controlled reference slice, not a general English parser, complete dictionary, or claim of language understanding.
- Dictionary entries and equivalences are declared. Paraphrase identity is not inferred or learned.
- Seven inquiry attentions (WHO, WHAT, WHERE, WHEN, WHERN, IS, SHOULD) have conservative deterministic behaviour in this slice; WHY, HOW, COULD, and WOULD are structurally represented but their procedural rules are not yet implemented (they default to NOT_APPLICABLE).
- Temporal modality (`WAS < IS > PROJECTED`) attaches to propositions, relations, and context via the `Tense`/`TemporalQualification` values; it is not encoded as a privileged inquiry attention.  `WHERN` is reserved for criterion-conditioned temporality, not simple future anchoring.  The surface token `will` is classified into temporal (→ WHEN question slot) or volitional (→ WHAT) by contextual disambiguation after dictionary lookup.
- Source ID, rights declaration, and original surface belong in `InterpretationRecord`; they must not create a new semantic graph revision when the topology and identity are unchanged.
- Constructed non-prime keys are large injective integers that serve as canonical *identity codes*, not compact operational or database IDs.  Compact manifested registry addresses remain a follow-up concern.
- Support for edge and relation-node forms does not yet imply that arbitrary instances of those forms are automatically semantically equivalent.
- The in-memory registry uses a lock for atomic process-local operations, but it is not durable, distributed production storage. Its namespace is local to the configured registry snapshot.
- Identity canonicalisation is schema- and ruleset-relative. Its constructed-key codec is injective, but that does not imply semantic correctness or universal identity; SHA-256 revision collisions are detected as explicit conflicts.
- No general graph-isomorphism solver, multilingual profile, media decoder, surface realiser, natural-text reading stream, rights enforcer, Prolog runtime, model training, fine-tuning, or delta model is included.
- No storage or compression saving is claimed. A numeric key addresses an authoritative graph and revision; it does not contain the graph's meaning by itself.
- No existing AF route should silently coerce an open Ideon into a string answer.

## Module boundaries and dependency direction

The slice belongs in these layers:

| Layer | Path | Responsibility |
|---|---|---|
| Domain | `src/af/domain/ideons/models.py` | Immutable graph, node, relation, identity, property, and exact-reference values |
| Domain | `src/af/domain/ideons/canonical.py` | Canonical identity/revision payloads and deterministic byte serialisation |
| Domain | `src/af/domain/ideons/numeric.py` | Limit-independent arbitrary-precision numeric transport encodings |
| Domain | `src/af/domain/ideons/registry.py` | Store protocol, interning contract, key/revision rules, and conflict errors |
| Domain | `src/af/domain/ideons/records.py` | Context, form, candidate, inquiry, SHOULD, decision, and runtime-record values |
| Domain | `src/af/domain/ideons/dictionary.py` | Concrete controlled-fixture logical dictionary and contextual candidate evaluation |
| Domain | `src/af/domain/ideons/{forms,inquiry,principia,temporal}.py` | Controlled form encoding, inquiry/decision rules, set declarations, and temporal helpers |
| Domain | `src/af/domain/ideons/__init__.py` | Deliberate public domain surface |
| Application | `src/af/application/ideons.py` | Use-case orchestration: lookup, construction, inquiry, decision, intern, and resolve |
| Infrastructure | `src/af/infrastructure/ideons.py` | Atomic process-local implementation of the `IdeonStore` registry port |
| Infrastructure | `src/af/infrastructure/ideon_reference.py` | Composition of the controlled dictionary, explicit prime manifest, registry, service, and controller |
| Presentation | `src/af/interfaces/ideons.py`, `src/af/interfaces/cli.py` | REST/CLI transport mapping only; no semantic or identity rules |

Dependencies point inward:

1. presentation adapters call the application service;
2. the application service depends on domain values, the `IdeonStore` port, and the current concrete `LogicDictionary` fixture implementation;
3. infrastructure implements the store port and composes the controlled reference runtime; and
4. domain code imports neither application, infrastructure, the generic procedure pipeline, nor a neural framework.

The AF composition root supplies the store/service adapters. A later candidate-provider protocol is needed before Prolog or neural lookup can replace the concrete controlled dictionary without changing the application constructor.

## Stable key and immutable revision policy

An `IdeonRef` contains both a positive stable numeric key and a deterministic revision identifier. They answer different questions:

- the stable key identifies the contained idea within a declared namespace, registry/schema version, active set, and identity rules;
- the revision identifies one exact, immutable recorded graph topology and definition.

Zero remains reserved for structure/control and is never allocated as an Ideon key. A model-local row number is not a stable key.

Canonicalisation excludes the assigned stable key, revision identifier, insertion time, object identity, and unordered container iteration. It includes the implemented identity inputs: schema and ruleset versions, namespace/set declaration, graph kind, ordered roles and children, roots, unresolved forms, and declared identity-bearing context. Mapping keys and unordered collections receive a defined order; semantic sequences retain their order. Callers constructing arbitrary graphs must explicitly place identity-bearing relation roles/direction into `IdeonIdentity`; this slice does not infer those identity conditions from topology.

The first slice requires new prime Ideons to supply actual odd-prime keys from a versioned
manifest; the reference manifest uses a bounded 64-bit prime lane. It derives every non-prime
key as an injective even integer from a versioned, length-prefixed binary identity payload,
keeping the two key lanes disjoint without recursively decimal-encoding child keys. A SHA-256 revision is derived
separately from the complete canonical graph payload. The registry compares authoritative
canonical payloads before deduplicating and raises an explicit conflict rather than silently
reusing or overwriting a key when payloads disagree.

Compatible refinements retain the stable key and append a new immutable revision. A change to identity conditions creates a different stable key and, when known, an explicit relation to the prior Ideon. Exact replay and resolution use `(key, revision)`, not a mutable notion of “latest”.

Composition is role- and order-sensitive. Canonical object-field sorting must therefore never sort semantic child/role sequences: `C[A,B]` and `C[B,A]` have different identity payloads and keys. Reuse is by reference, so a prime Ideon embedded in several composites is stored once and referenced many times.

## Deterministic interpretation flow

Given an interpretation request, the implemented application service performs these steps in order:

1. Validate the request, injected language profile/set stack, context, and optional SHOULD inputs.
2. Preserve the original surface representation losslessly and add typed normalized forms.
3. Request every matching controlled-dictionary sense in deterministic order.
4. Run the eleven inquiry templates over those forms/candidates and supplied context.
5. Decide `ask`, `answer`, `act`, `stop`, or `continue-reading`, recording rules, evidence, and the active SHOULD threshold policy.
6. Construct the complete or open semantic graph from forms, viable candidates, openings, and identity-bearing context.  Source ID and rights are **not** written into the graph; they remain in the occurrence-provenance layer (`InterpretationRecord`).
7. Canonicalise, validate, and intern the graph, then emit its `(key, revision)` in the canonical runtime record.

The semantic graph is source-independent: two requests carrying identical surface/context but different source IDs or rights declarations resolve to the **same** graph revision while producing distinct `InterpretationRecord` values.  This is verified by a dedicated regression test.

Whern rendering, declared-equivalence construction, exact resolve/byte comparison, compact/reified relations, and semantic-island behavior are exercised by focused conformance tests, but `interpret()` does not yet invoke all of those helpers automatically.

Every source of apparent nondeterminism must be an input: dictionary/ruleset version, registry snapshot, clock/reference Whern, locale, ordering, and policy thresholds. Operational timestamps, source IDs, and rights declarations remain outside canonical semantic bytes. With those inputs fixed, repeated output records must be byte-identical.

For `sat on the bank`, absence of an actor creates a WHO opening **only because a predicate (action `sat`) is present**.  An utterance with no action/state/relation leaves WHO as NOT_APPLICABLE rather than spuriously material.  The declared river-edge and financial senses remain candidates unless context selects one; the result is an open Ideon with `decision = ask` and it must not guess.

## Application API contract

The Python application boundary should expose the shared specification without leaking storage details:

```python
class IdeonService:
    def encode_forms(self, media: str) -> FormSequence: ...
    def lookup_senses(
        self, forms: FormSequence, context: ContextGraph
    ) -> tuple[CandidateSet, ...]: ...
    def query_openings(
        self, forms: FormSequence, candidates: tuple[CandidateSet, ...],
        context: ContextGraph, *, should: ShouldAssessment | None = None,
    ) -> InquiryState: ...
    def decide(
        self, inquiry: InquiryState, *, should: ShouldAssessment | None = None,
        action_requested: bool = False, media_complete: bool = True,
    ) -> DecisionTrace: ...
    def construct_graph(
        self, forms: FormSequence, candidates: tuple[CandidateSet, ...],
        context: ContextGraph, inquiry: InquiryState, *,
        source_id: str | None = None, rights: str | None = None,
    ) -> IdeonGraph: ...
    def intern(self, graph: IdeonGraph) -> IdeonRef: ...
    def resolve(self, ref: IdeonRef) -> IdeonGraph: ...
    def realise(self, ref: IdeonRef) -> str: ...
    def interpret(self, request: InterpretRequest) -> InterpretationRecord: ...
    def stream(self, request: InterpretRequest) -> tuple[bytes, str]: ...
```

The service is constructed with the language profile, active set stack, dictionary, and store. `InterpretRequest` carries the source surface, source/rights declarations, context, optional SHOULD assessment, and closure inputs; no ambient locale, time, or global policy may alter the result. `InterpretationRecord` carries original forms, candidate and selected senses, context, eleven inquiry outputs, openings, decision trace, SHOULD fields, and exact key/revision. The immutable graph is resolved through that exact reference; source, rights, and non-identity context are retained in its revision-bearing payload.

Contract rules:

- candidate lookup returns a set/list, including zero or multiple candidates; it never fabricates a sense;
- `intern` validates first, deduplicates equal identity payloads, preserves prior revisions, and returns a nonzero key plus exact revision;
- `resolve` requires an exact reference for deterministic replay and returns a defensive immutable value;
- invalid graphs, unknown references, revision conflicts, and key/payload conflicts use explicit typed failures;
- transport JSON is derived from the domain record and uses the same canonical field semantics; and
- a plaintext explanation is a view of the structured record and cannot replace or contradict it.

`realise` currently replays the preserved source surface and `stream` emits canonical record bytes plus the deterministic plain-text decision view. General language-profile rendering and held-out natural-text ingestion remain deferred.

## Acceptance/conformance matrix

“Verified in focused suite” means the named controlled behaviour passed in the executed Ideon
unit/API suite. It does not establish broad linguistic accuracy beyond those fixtures.

| # | Specification acceptance case | Initial status | Required conformance evidence |
|---:|---|---|---|
| 1 | `sat on the bank`: absent actor and ambiguous bank | Verified in focused suite | WHO opening, both bank senses retained without context, and deterministic `ask` when material |
| 2 | `the dictionary 2nd edition` vs reordered surface | Verified in focused suite | Preserved source order, different ordered graph structures, and different identity payloads |
| 3 | One event rendered as was/is/projected under three Wherns | Verified in focused suite | The pure temporal rule produces deterministic tense views (WAS/IS/PROJECTED) for three explicit reference Wherns; full graph realisation is not claimed |
| 4 | Temporal `will be` vs volitional `will/intends` | Verified in focused suite | The controlled rule and dictionary fixtures classify the resolved relation into temporal (WHEN slot) or volitional (WHAT slot) after disambiguation; broad parsing is not claimed |
| 5 | Prime dictionary Ideon reused in composites | Verified in focused suite | Several composites contain the same exact prime `IdeonRef`, not copied graphs |
| 6 | `C[A,B] != C[B,A]` | Verified in focused suite | Different canonical identity bytes and stable keys |
| 7 | Multiple semantic islands | Verified in focused suite | One valid bounded Ideon retains multiple disconnected components |
| 8 | Compact edge and provenance-bearing relation node | Verified in focused suite | Both representations validate and round-trip; automatic semantic equivalence is not claimed |
| 9 | Open Ideon asks for a set rather than guessing | Verified in focused suite | `Decision.ASK` plus the opening retains ordered candidates and the missing material distinction |
| 10 | High-relevance SHOULD selects safer route or stops | Verified in focused suite | Complete SHOULD fields, serialized stop-threshold policy, decision, alternative/reason, and trace |
| 11 | Declared-equivalent paraphrases share a key | Verified in focused suite | Explicit equivalence/identity declaration produces one identity payload/key; learned paraphrase detection is not claimed |
| 12 | Similar surfaces differ by context/perspective | Verified in focused suite | Identity-bearing context yields different identity payloads and keys |
| 13 | Repeat runs are byte-identical | Verified in focused suite | Two runs against identical versioned inputs and registry snapshot compare equal byte-for-byte |
| 14 | Actual held-out natural-text reading stream | Planned | Captured structured outputs from a declared held-out text; illustrative output is not acceptable |

Tests for graph identity, revision history, exact-reference resolution, zero reservation, collision/conflict handling, and immutable return values are supporting conformance tests even where they do not correspond one-to-one with an item above.

Executed verification for this slice (correction pass):

```text
88 passed (focused Ideon suite, +2 regression tests for provenance-separation and temporal qualification)
strict mypy: no issues in 51 source files
ruff check + format: passed
full suite (excluding pre-existing yaml subprocess issue): 358 passed
```

## Persistence evolution

`InMemoryIdeonRegistry` is the initial adapter. The store protocol should permit a later transactional implementation with, at minimum:

- an identity table unique on `(namespace, stable_key)` and on the canonical identity digest;
- an append-only revision table unique on `(namespace, stable_key, revision_id)` containing canonical graph bytes and schema/ruleset versions;
- dictionary form/sense and explicit equivalence records with provenance and validity context;
- relation/reference indexes without duplicating authoritative child graphs; and
- a separate operational audit stream for clocks, request IDs, metrics, access control, and privacy events.

Interning must be atomic: compare full canonical payloads, insert identity/revision or return the existing exact reference, and fail closed on conflict. Backups and migrations must preserve canonical bytes and key/revision mappings. A durable adapter must run the same registry conformance suite as the in-memory adapter before it is accepted.

## Prolog and neural follow-up

The Prolog bridge is the next reference-semantics extension. It should project versioned dictionary facts, typed relations, Whern rules, missing-role rules, and SHOULD contracts from the same domain values, then return candidate bindings and proof traces into an `InterpretationRecord`. Golden tests must compare Python and Prolog candidates, decisions, and exact graph records; Prolog must not maintain a second independent key registry.

Only after the record and conformance suite are stable should AF add from-scratch, fine-tuned, or delta-model providers. Neural providers may propose senses, graph deltas, focus biases, or decisions, but they do not mint semantic keys. Their output is validated, canonicalised, and interned through the same application/domain path. The current AF pipeline can select such providers as higher capability rungs while retaining deterministic dictionary/registry work as the cheaper reference path.

Natural reading, including the held-out-text acceptance test, follows once ingestion and streaming emit the model/system's complete structured output. Human-readable verbosity is then derived from that record rather than substituted for it.
