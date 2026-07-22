# Prime Language and Ideon System

## Technical specification for deterministic language understanding, training, fine-tuning, delta modelling, and Prolog/Python implementation

**Version:** 0.1  
**Date:** 2026-07-22  
**Status:** Current integrated design specification. No implementation or experimental result is claimed by this document.

---

## 1. Purpose

This specification defines a deterministic language layer in which natural-language forms are resolved through a logical dictionary into **Ideons**: contained, typed idea graphs. Each Ideon is paired with one numeric key. The graph is the Ideon; the number is its key.

The system retains tokens as typed intermediate and reusable units. Its complete language representation consists of forms, lexical senses, logical relations, context graphs, and Ideon graphs.

The same formal contract shall support four implementation routes:

1. training a model from scratch;
2. fine-tuning an existing language model;
3. attaching a separately trained delta model to a frozen or mostly frozen model;
4. executing the deterministic layer directly with Prolog and Python, with neural models used only where interpretation remains unresolved.

All four routes must consume and produce the same canonical records so that they can be compared fairly.

---

## 2. Central proposition

The final unit of understanding is the Ideon graph.

The intended pipeline is:

```text
media
  -> observable forms
  -> typed tokens
  -> lexical-sense candidates
  -> logical dictionary graphs
  -> context-parameterised Ideon graph
  -> one numeric Ideon key
```

The inverse pipeline is:

```text
Ideon key
  -> Ideon graph
  -> selected language/context profile
  -> grammar and surface realisation
  -> text, speech, sign, code, image annotation, or another medium
```

The major operational gain over a conventional token stream is that a resolved sentence can thereafter be addressed, cached, compared, transmitted, reused, and reasoned over through one number while retaining access to its graph.

This is not necessarily information-theoretic compression: an exact number or its backing graph still requires storage. Compression arises operationally through canonical identity, graph reuse, shared sub-Ideons, cached procedures, and avoidance of repeated interpretation.

---

## 3. Normative terminology

### 3.1 Form

A directly observable linguistic or symbolic realisation, including bytes, Unicode code points, graphemes, glyphs, letters, numerals, punctuation, phonetic segments, speech, signs, Morse code, flags, mathematical notation, or code.

### 3.2 Token

A typed, addressable unit used during encoding or model computation. Tokens may represent forms, lexical senses, graph operators, relations, contexts, questions, controls, or cached composite Ideons.

A token is therefore not restricted to a subword fragment.

### 3.3 Lexeme and sense

A lexeme groups related language forms. A sense identifies one contextual meaning of that lexeme. Homographs and polysemous words must not share a single undifferentiated sense.

### 3.4 Ideon

An Ideon is a contained idea with its own internal topology. It may contain multiple connected components or semantic islands while remaining one bounded object.

Formally:

\[
I=\langle V,E,H,R,W,C,K,F,P\rangle
\]

where:

- \(V\) is a typed vertex set;
- \(E\) is a set of typed, directed binary edges;
- \(H\) is a set of typed \(n\)-ary hyperedges or relation nodes;
- \(R\) records roots, roles, order, origin, destination, and perspective;
- \(W\) records exact weights, intervals, or qualified scores;
- \(C\) is the context graph;
- \(K\) contains templates, interfaces, traits, and contracts;
- \(F\) records finalisation state and unresolved openings;
- \(P\) records provenance, privacy, security, access, and licensing constraints.

### 3.5 Ideon key

An Ideon key is one natural number paired with an Ideon:

\[
\operatorname{paired}(k_I,I),\qquad k_I\in\mathbb N,\qquad I\ne k_I.
\]

Within a declared namespace and registry version, keys must be unique:

\[
k_I=k_J\Rightarrow I=J.
\]

The graph is authoritative. The key addresses it.

### 3.6 Prime and composite Ideons

A prime Ideon is treated as irreducible under a declared set and ruleset:

\[
\operatorname{Prime}(I\mid\mathbb S,\Gamma)
\iff
\nexists A,B,\circ:\ I=A\circ B
\text{ under }(\mathbb S,\Gamma).
\]

A composite Ideon contains other Ideons joined by typed relations:

\[
I_C=\operatorname{Compose}(I_1,\ldots,I_n;E,H,R,W,C,K).
\]

Primeness is set-relative. A concept may be atomic in an everyday-language set and decomposable in an expert set. The registry preserves both views with their declared sets.

### 3.7 Logic dictionary

A versioned mapping from observable forms to lexical candidates, logical formulae, and Ideon graphs:

\[
D_\lambda(f,C)
\rightarrow
\{\langle lexeme,sense,k_I,I,\varphi,provenance\rangle\}.
\]

It returns a set because a form may have several valid senses.

### 3.8 Context graph

A graph that parameterises interpretation without altering the underlying reusable Ideons. It includes speaker, source, audience, place, temporal reference, domain, discourse, register, literal/hypothetical/metaphorical mode, and relevant assumptions.

### 3.9 Active set stack

Interpretation occurs against an ordered set stack:

\[
\mathbb S=[S_{core},S_{script},S_{language},S_{dialect},S_{domain},S_{source},S_{speaker},S_{document},S_{local}].
\]

More specific sets constrain interpretation but do not silently delete valid broader senses.

---

## 4. Graphs are authoritative; tensors are views

An Ideon may be projected into a vector, matrix, tensor, typed tensor, or hypergraph according to the operation being performed:

\[
\begin{aligned}
\phi_1(I)&\in\mathbb R^d,\\
\phi_2(I)&=A_{ij},\\
\phi_3(I)&=T_{ijr},\\
\phi_n(I)&=T^{(r)}_{i_1\ldots i_n},\\
\phi_G(I)&=G_I.
\end{aligned}
\]

For a typed \(n\)-ary relation \(r\):

\[
T^{(r)}\in V_{\tau_1}\otimes\cdots\otimes V_{\tau_n}.
\]

Vectors and tensors are computational representations, not the identity or definition of the Ideon. Different tasks may use different projections of the same graph.

Relation edges may themselves pass through relation Ideons:

```text
subject -> relation-Ideon -> object
```

This permits relations to possess provenance, context, confidence, exceptions, privacy, and their own ancestor/dependant relationships. Binary edges remain available as a compact special case. General relations are represented as relation nodes or hyperedges.

Composition is order- and role-sensitive:

\[
A\oplus B=C[A,B]\ne C[B,A]=B\oplus A.
\]

---

## 5. Numeric-key requirements

### 5.1 Separation of object and key

No implementation may treat the number itself as the Ideon. The minimum storage relation is:

```text
ideon_store[key] = ideon_graph
```

### 5.2 Stable and revision identity

Identity is dual:

- a stable numeric key answers **which Ideon**;
- a deterministic revision identifier answers **which recorded topology or definition**.

The stable key remains paired with the Ideon across compatible refinements. Each graph revision is immutable and receives a new revision identifier. A change that alters the identity conditions of the contained idea creates a new Ideon, a new stable key, and an explicit relation to the prior Ideon.

### 5.3 Prime and composite numeric keys

The registry assigns prime numeric keys to registered prime Ideons. A composite Ideon key is constructed from its canonical, order-sensitive composition signature and confirmed by the registry after canonical deduplication.

A plain product of child primes is forbidden as a complete compound-key algorithm because multiplication loses order and relation roles.

The composite-key constructor must satisfy:

\[
\kappa(C[A,B])\ne\kappa(C[B,A])
\]

and includes the ruleset, active namespace, relation roles, and identity-bearing context distinctions. The registry stores the pairing between this key and the authoritative Ideon graph.

### 5.4 Zero convention

Zero is reserved as a structural delimiter or control value. A lexical numeral zero must have its own nonzero token and Ideon key. If decimal key streams use zero delimiters, all component encodings must be unambiguously zero-free or length-prefixed.

### 5.5 Dense model identifiers

Models may use compact local row numbers internally, but a versioned manifest must map each local row back to the stable Ideon or dictionary key. A model-local integer is never the global semantic identity.

---

## 6. Linguistic type system

The types form a graph rather than a single ladder.

### 6.1 Representation types

```text
byte
code-point
grapheme
glyph
letter
numeral-form
punctuation
special/control
```

### 6.2 Speech and non-written types

```text
phone
phoneme
syllable
prosodic-unit
sign
gesture
morse-unit
flag-signal
```

### 6.3 Lexical and grammatical types

```text
morpheme
word-form
lexeme
sense
phrase
clause
sentence
utterance
paragraph
section
```

### 6.4 Publication and collection types

```text
chapter
article
issue
journal
book
corpus
library
```

### 6.5 Logical and semantic types

```text
entity
collection
action
state
property
relation
criterion
context
proposition
question
norm
procedure
rule
proof
source
perspective
```

The dictionary must distinguish mappings such as `realises`, `pronounces-as`, `denotes`, `means-in-context`, `contains`, `published-in`, and `translates-to`. Cross-language translation is a contextual relation, not automatic identity.

---

## 7. Language profiles

A language-aware profile \(UTF_\lambda\) is layered over Unicode:

\[
UTF_\lambda^v=
\langle U_v,N_\lambda,G_\lambda,O_\lambda,P_\lambda,M_\lambda,L_\lambda,\Gamma_\lambda,\Sigma_\lambda,\Lambda_\lambda\rangle.
\]

It specifies:

- Unicode version and normalisation policy;
- script and grapheme rules;
- orthography;
- phonology and morphology;
- lexicon and sense inventory;
- grammar and syntax;
- semantic constraints;
- logical rules;
- language, dialect, register, place, period, and community metadata.

Additional dictionaries and languages should sharpen the graph by contributing more forms, contrasts, senses, and relation evidence. They must not be forced into false one-to-one equivalence.

---

## 8. Logical dictionary specification

### 8.1 Entry contract

Every dictionary sense record must support:

```text
entry_id
stable_numeric_key
revision_id
language_profile
forms[]
pronunciations[]
lexeme
sense
type
supertypes[]
arguments[]
logical_formula
ideon_graph
relations[]
question_slots[]
valid_contexts[]
excluded_contexts[]
literal_hypothetical_metaphorical_modes[]
definitions[]
examples[]
provenance[]
evidence_status
confidence_or_margin
licence_and_usage_rights
privacy_and_security
version
```

Definitions are source- and context-bearing assertions, not timeless replacements:

\[
\operatorname{Defines}(source,form,sense,domain,whern,definition).
\]

Minority, historical, specialist, personal, or partially erroneous senses may be retained with qualifiers and margins rather than declared nonexistent merely because they are not the majority use.

### 8.2 Initial logical primitives

The bootstrapping logic set is:

\[
\mathcal L_0=\{NOT,AND,OR,IF,IFF,FORALL,EXISTS,EQUALS\}.
\]

The initial type set is:

\[
\mathcal T_0=\{Entity,Collection,Relation,Action,State,Property,Context,Criterion,Proposition\}.
\]

The initial relation set is:

\[
\mathcal R_0=\{INSTANCE\_OF,SUBTYPE\_OF,PART\_OF,HAS,DOES,CAUSES,ENABLES,PREVENTS,
BEFORE,AFTER,LOCATED\_IN,ORIGIN,DESTINATION,REFERS\_TO,MEANS\}.
\]

These sets are versioned and extensible. Their first implementation is the Principia core; later the dictionary must be capable of describing its own schema and rules.

### 8.3 Dictionary expansion

An entry begins as a defined sense graph:

\[
I^{(0)}=I_{sense}.
\]

Requested depth expands through declared relations:

\[
I^{(d+1)}=I^{(d)}\cup\operatorname{Neighbours}(I^{(d)},\mathcal R,\mathbb S,C).
\]

Model depth may limit simultaneous computation, but it must not be confused with whether the dictionary graph exists.

### 8.4 What must be predefined

The project does not need to predefine every possible sentence. It must predefine:

1. primitive Ideons;
2. dictionary-sense Ideons;
3. relation and argument contracts;
4. graph templates;
5. deterministic composition and validation rules;
6. language-specific realisation rules.

Novel sentence Ideons are constructed from these parts.

---

## 9. Inquiry, temporal perspective, and modality

### 9.1 The eleven specialist attentions

The core set is:

\[
\mathcal Q_0=
\{WHO,WHAT,WHERE,WHEN,WHERN,WHY,HOW,IS,COULD,WOULD,SHOULD\}.
\]

Each is both:

- a logical operator or graph-query template;
- a separately trainable specialist attention/objective;
- a missing-information detector;
- a field in the model's actual reading stream.

`DID` is treated as a specialised `WHAT`: it asks which action or state transition occurred, with a past Whern relative to the reference perspective.

### 9.2 Whern

Whern is a temporal and criterion-bearing condition:

```text
Whern {
    temporal_anchor,
    criterion,
    reference_perspective,
    status
}
```

Its default criterion is:

```text
now = datetime_provided
```

Whern may instead denote completion, threshold crossing, observation, permission, availability, or another condition becoming true.

### 9.3 IS, WAS, and WILL

`IS` is the primitive state/identity/existence relation. `WAS` and temporal `WILL` are surface views determined by Whern and reference perspective:

\[
WAS\leftarrow IS\rightarrow WILL.
\]

\[
\operatorname{RenderTense}(IS,t_e,t_r)=
\begin{cases}
WAS,&t_e<t_r,\\
IS,&t_e=t_r\text{ or the current criterion holds},\\
WILL,&t_e>t_r.
\end{cases}
\]

Temporal `will` must not be conflated with volitional senses such as `INTENDS`, `WANTS`, or `COMMITS`.

### 9.4 Modal transforms of IS

\[
\begin{aligned}
IS(\varphi)&:\ \varphi\text{ holds in the selected context},\\
COULD(\varphi)&:\ \text{a feasible path reaches }IS(\varphi),\\
WOULD(\varphi\mid\psi)&:\ \psi\text{ projects }IS(\varphi),\\
SHOULD(\varphi)&:\ IS(\varphi)\text{ has normative preference or requirement}.
\end{aligned}
\]

`SHOULD` is not Boolean. Its record must contain at least:

```text
action
affected_entities
norm_or_goal
evidence
relevance
severity
confidence_or_margin
reversibility
context
recommended_alternative
```

Its influence grows with relevance and severity. It may cause the system to ask, select a safer route, decline an action, or stop.

---

## 10. Non-closure, asking, answering, and stopping

The system must not force unresolved language into one sense merely to continue generation.

For candidate set \(X\):

\[
\operatorname{Resolve}(X)=
\begin{cases}
UNKNOWN,&|X|=0,\\
x,&|X|=1,\\
ASK\_SET(X),&|X|>1\text{ and the distinction matters}.
\end{cases}
\]

An unresolved sentence may form one open Ideon whose graph contains alternative candidate islands and the question required to resolve them.

### 10.1 Ask

Ask when:

- a required role, set, referent, criterion, or perspective is absent;
- multiple interpretations remain materially distinct;
- expected information gain exceeds the cost of asking;
- a `SHOULD` evaluation requires missing affected-party, norm, severity, or consent information;
- closure is insufficient for the requested action.

### 10.2 Answer or act

Answer or act when:

- required graph contracts are satisfied;
- remaining ambiguity is immaterial to the answer;
- provenance and confidence are adequate for the domain;
- `SHOULD` does not require a safer route, question, or stop.

### 10.3 Stop

Stop when:

- an inherited or local contract forbids continuation;
- a high-relevance `SHOULD` signal indicates unacceptable expected harm;
- required authorisation or consent is missing and cannot merely be asked for within the current interaction;
- continuing would falsely present unresolved material as closed;
- privacy or security rules prevent the needed disclosure or operation.

Every ask, answer, and stop decision must be logged as actual model/system output with its supporting fields.

---

## 11. Procedural media ingestion

Media ingestion is a documented procedure even when statistical models initially perform some steps.

### 11.1 Required pipeline

1. Record source, medium, rights, date, language candidates, and provenance.
2. Extract observable forms without discarding the original representation.
3. Map forms to lexeme and sense candidate sets.
4. Construct grammatical relations and a partial logical graph.
5. Run all eleven specialist inquiries against missing or contested graph slots.
6. Parameterise candidate Ideons using the context graph.
7. Preserve literal, hypothetical, counterfactual, quoted, metaphorical, fictional, and normative status.
8. Select, retain as open, or ask for the needed set.
9. Intern reusable Ideons, relations, and subgraphs rather than duplicating them.
10. Pair the final or open Ideon with its numeric key and revision record.
11. Emit machine-readable and plain-text reading streams derived from the same record.

### 11.2 Perspective completion example

For:

```text
sat on the bank
```

the partial interpretation may be:

```text
(WHO)   {someone or contextually inherited actor}
(DID)   sat
(WHERE) on
(WHAT)  bank
(WHERE) {river edge | financial location | other candidate}
```

The system is not merely predicting the next word. It attempts to fill obfuscated graph roles, tests dictionary senses against context, and records what remains absent. A river context may select the river-bank sense. Insufficient context produces an open Ideon or an `ASK_SET` operation.

---

## 12. Shared machine-readable training record

Every implementation route must support an equivalent record such as:

```json
{
  "record_id": "...",
  "source": {"id": "...", "span": [0, 15], "rights": "..."},
  "language_profile": "en-GB@version",
  "surface": "sat on the bank",
  "forms": [],
  "lexeme_candidates": [],
  "sense_candidates": [],
  "selected_senses": [],
  "ideon_key": null,
  "ideon_revision": null,
  "nodes": [],
  "edges": [],
  "hyperedges": [],
  "context_graph": {},
  "perspective": {},
  "mode": "literal|hypothetical|metaphorical|quoted|unknown",
  "questions": {
    "who": {}, "what": {}, "where": {}, "when": {}, "whern": {},
    "why": {}, "how": {}, "is": {}, "could": {}, "would": {}, "should": {}
  },
  "openings": [],
  "closure": {},
  "decision": "ask|answer|act|stop|continue-reading",
  "provenance": [],
  "actual_output": {}
}
```

The human-readable stream must be generated from this record. It must never replace or contradict the actual structured output.

---

## 13. Common model interface

All neural routes shall implement:

```text
encode_forms(media, profile) -> FormSequence
lookup_senses(forms, context) -> CandidateSet
construct_graph(forms, candidates, context) -> PartialIdeon
query_openings(partial_ideon) -> InquiryState
decide(inquiry_state, contracts) -> ask|answer|act|stop|continue
intern(ideon) -> numeric_key
resolve(numeric_key) -> ideon
realise(ideon_or_key, language_profile, perspective) -> media
stream(media) -> structured_records + derived_plaintext
```

The deterministic Prolog/Python implementation is the reference semantics. Neural implementations may approximate selection and graph construction, but their outputs must validate against the same contracts.

---

## 14. Route A: training from scratch

### 14.1 Architecture

A small or medium causal or encoder-decoder model with:

- lossless byte/Unicode fallback input;
- typed form, lexeme, sense, relation, context, and Ideon-key embeddings;
- graph-relative position and relation features;
- eleven specialist inquiry heads;
- graph node, edge, hyperedge, and opening heads;
- closure and ask/answer/stop heads;
- IS/Whern temporal head;
- COULD, WOULD, and SHOULD heads;
- optional graph encoder or graph cross-attention module;
- deterministic dictionary and renderer outside the neural network.

Attention may be conditioned on token type and graph role:

\[
e(t)=e_{id}+e_{type}+e_{language}+e_{sense}+e_{role}+e_{context}+e_{composition}.
\]

\[
Focus(i,j\mid C)=Attention(i,j)+B_{syntax}+B_{relation}+B_{context}+B_{inquiry}.
\]

### 14.2 Training curriculum

1. Form preservation and exact reconstruction.
2. Lexeme and sense discrimination.
3. Primitive logic and typed relation prediction.
4. Single-sentence graph construction.
5. Individual training for each of the eleven specialist inquiries.
6. Balanced joint training across all eleven inquiries.
7. Context alignment and perspective shifts.
8. IS/WAS/WILL rendering from Whern.
9. Could/would/should reasoning and ask/stop behaviour.
10. Multi-sentence, paragraph, article, and book ingestion.
11. Ideon reuse, graph diffing, and knowledge-forest construction.

The initial experimental target is at least 50,000 qualified examples per specialist inquiry, not 50,000 shared across all inquiries. Dataset size remains configurable and scaling results must be reported rather than assumed.

### 14.3 Objective

\[
\mathcal L=
\lambda_f\mathcal L_{form}
+\lambda_s\mathcal L_{sense}
+\lambda_g\mathcal L_{graph}
+\sum_{q\in\mathcal Q_0}\lambda_q\mathcal L_q
+\lambda_o\mathcal L_{opening}
+\lambda_c\mathcal L_{closure}
+\lambda_d\mathcal L_{decision}
+\lambda_k\mathcal L_{key}
+\lambda_r\mathcal L_{realisation}
+\lambda_p\mathcal L_{provenance}.
\]

Loss weights, schedules, and gradients must be logged. Joint training must be compared with specialist-only and sequentially composed variants.

### 14.4 Generation

The model should construct or select valid dictionary/Ideon objects rather than freely emitting malformed word fragments. Surface language is produced by a deterministic or constrained renderer whenever possible.

---

## 15. Route B: fine-tuning an existing model

### 15.1 Scope

Use supervised multi-task fine-tuning, parameter-efficient adapters, or selected full-parameter updates to teach an existing model the shared record and graph contract.

### 15.2 Required additions

- serialisation tokens for graph boundaries, types, relations, questions, openings, and decisions;
- dictionary lookup and Ideon-store tools;
- explicit structured outputs validated against the shared schema;
- balanced examples for each specialist inquiry;
- contrastive examples separating word-form identity from sense identity;
- tense/perspective examples in which the same event renders as was/is/will;
- temporal-will versus volitional-will discrimination;
- should-relevance examples that distinguish continue, ask, safer alternative, decline, and stop.

### 15.3 Training targets

Fine-tuning must teach the model to:

1. preserve candidate sets rather than invent a single answer;
2. call the dictionary and graph store before generating definitions already known;
3. emit complete Ideon deltas or graphs;
4. produce one numeric key only after successful interning or lookup;
5. generate language from the selected graph rather than treating the key as meaning by itself.

### 15.4 Base-model preservation

Evaluate catastrophic forgetting, language fluency, general reasoning, and tool use. Adapter and full-fine-tune results must be reported separately.

---

## 16. Route C: delta model

### 16.1 Purpose

The delta model supplies structured inquiry, logical-dictionary alignment, and Ideon-graph residuals while leaving the base model frozen or mostly frozen.

For base hidden state \(h_l\):

\[
\Delta h_l=f_\Delta(h_l,I_{partial},C,\mathcal Q_0),
\]

\[
h'_l=h_l+g_l\odot\Delta h_l,
\]

where \(g_l\) is a learned or rule-constrained gate.

### 16.2 Inputs

- base hidden states or logits;
- dictionary candidates;
- partial Ideon graph;
- context graph;
- missing-role/opening state;
- eleven specialist signals;
- contracts and SHOULD state.

### 16.3 Outputs

- sense-selection delta;
- graph node/edge additions and removals;
- attention or focus bias;
- ask/answer/stop delta;
- constrained next-object distribution;
- corrected Ideon candidate or numeric-key request.

### 16.4 Training

Train against the residual between the base model's interpretation and the validated reference graph:

\[
\Delta I=I_{reference}\ominus I_{base}.
\]

Report additions, deletions, relation corrections, unresolved openings, and false closures independently. The delta model must be compared with prompting alone and with direct fine-tuning.

### 16.5 Deployment modes

- sidecar service receiving text and base outputs;
- adapter inserted at selected layers;
- prefix or cross-attention graph conditioner;
- logit/decision router;
- AF capability rung selected when deterministic procedures alone are insufficient.

---

## 17. Route D: Prolog/Python reference implementation

### 17.1 Responsibility split

**Prolog** provides:

- facts and typed relations;
- dictionary-sense rules;
- unification and candidate enumeration;
- temporal and Whern rules;
- logical inference;
- question generation from missing variables;
- contract validation;
- explainable proof traces.

**Python** provides:

- media decoding and Unicode handling;
- data models and schema validation;
- graph storage and algorithms;
- numeric-key registry and arbitrary-precision handling;
- Prolog bridge;
- neural-model adapters;
- training-data generation;
- streaming logs, metrics, APIs, and user interfaces.

### 17.2 Prolog predicate sketch

```prolog
ideon(Key, Kind, Type, Revision).
form(Key, LanguageProfile, Surface, FormType).
sense(FormKey, SenseKey, ContextConstraint).
relation(RelationKey, SubjectKey, ObjectKey, ContextKey).
argument(RelationKey, Role, Position, ValueKey).
hyper_argument(RelationKey, Role, Position, ValueKey).
definition(SourceKey, SenseKey, DomainKey, WhernKey, FormulaKey).
candidate(FormKey, ContextKey, SenseKey).
missing_role(IdeonKey, QuestionType, Role).

is_at(Subject, Property, Whern, Context) :-
    relation(_, Subject, Property, Context),
    valid_whern(Whern, Context).

render_tense(EventWhern, ReferenceWhern, was) :-
    before(EventWhern, ReferenceWhern).
render_tense(EventWhern, ReferenceWhern, is) :-
    current_under(EventWhern, ReferenceWhern).
render_tense(EventWhern, ReferenceWhern, will) :-
    after(EventWhern, ReferenceWhern).

needs_ask(Ideon, Question, Role) :-
    missing_role(Ideon, Question, Role),
    materially_relevant(Ideon, Role).

must_stop(Ideon, Reason) :-
    should_signal(Ideon, Relevance, Severity, Reason),
    stop_threshold(Relevance, Severity).
```

### 17.3 Python type sketch

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class Decision(str, Enum):
    ASK = "ask"
    ANSWER = "answer"
    ACT = "act"
    STOP = "stop"
    CONTINUE_READING = "continue-reading"


@dataclass(frozen=True)
class IdeonRef:
    key: int
    revision: str


@dataclass
class Relation:
    key: int
    roles: tuple[tuple[str, IdeonRef], ...]
    context: IdeonRef | None = None
    weight: Any = None


@dataclass
class Ideon:
    stable_key: int | None
    revision: str | None
    kind: str
    type_key: int
    nodes: set[IdeonRef] = field(default_factory=set)
    relations: list[Relation] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    contracts: list[Any] = field(default_factory=list)
    openings: list[Any] = field(default_factory=list)


class LogicDictionary:
    def candidates(self, form: str, language: str, context: dict) -> list[IdeonRef]: ...


class IdeonStore:
    def intern(self, ideon: Ideon) -> IdeonRef: ...
    def resolve(self, ref: IdeonRef) -> Ideon: ...
    def expand(self, ref: IdeonRef, depth: int, relation_sets: Iterable[int]) -> Ideon: ...
```

### 17.4 Suggested project layout

```text
ideon/
  schema/
  principia/
  dictionary/
  language_profiles/
  graph/
  keys/
  context/
  inquiry/
  prolog/
  neural/
    from_scratch/
    finetune/
    delta/
  ingestion/
  realisation/
  evaluation/
  api/
  cli/
  web/
  tests/
```

The Prolog/Python version is not merely a demonstration. It defines expected deterministic behaviour and produces labels, proofs, and regression tests for the neural variants.

---

## 18. Data and corpus requirements

### 18.1 Source classes

- public-domain dictionaries, grammars, literature, and reference works;
- appropriately licensed Creative Commons material;
- qualified and peer-reviewed academic sources where rights permit computational use;
- institutionally licensed sources only under their actual permitted conditions;
- synthetic logical micro-worlds generated from validated templates;
- human-reviewed examples for ambiguity, metaphor, perspective, and normative judgement.

Access to read a work does not automatically imply permission to redistribute it or train on it. Every source record must carry rights and permitted-use metadata.

### 18.2 Dictionary-first annotation

Definitions should be translated into logical entry graphs, checked for type and argument validity, and linked across sources and languages. Statistical extraction may propose entries, but acceptance into the validated dictionary requires procedural checks and provenance.

### 18.3 Split policy

Split by documents, sources, Ideon families, and graph templates as appropriate. Avoid placing paraphrases or trivial graph variants across train and test splits.

### 18.4 Natural reading tests

After controlled tasks, evaluate on complete works such as *Alice's Adventures in Wonderland*. The system must stream its actual interpretation, questions, openings, closure state, and decisions as it reads. Illustrative reconstructions must be clearly labelled and must never substitute for model output.

---

## 19. Evaluation

### 19.1 Deterministic layer

- identical input, profiles, context, rules, and versions produce identical records;
- stable-key uniqueness and lookup correctness;
- revision tracking;
- lossless preservation of original forms;
- dictionary and Prolog proof reproducibility;
- graph-schema and contract validation;
- no collision or silent key reuse.

### 19.2 Linguistic layer

- lexeme and sense accuracy;
- ambiguity preservation;
- malformed-word rate;
- grammar and realisation validity;
- literal/hypothetical/metaphorical classification;
- multilingual and historical-variant alignment;
- was/is/will rendering accuracy;
- temporal-will/volitional-will separation.

### 19.3 Graph layer

- node, edge, role, direction, and hyperedge precision/recall/F1;
- graph edit distance to reference;
- connected-component/island correctness;
- order sensitivity;
- context alignment;
- graph expansion and contraction fidelity;
- Ideon reuse and duplicate detection.

### 19.4 Inquiry layer

Report separately for each of WHO, WHAT, WHERE, WHEN, WHERN, WHY, HOW, IS, COULD, WOULD, and SHOULD:

- missing-slot detection;
- question relevance;
- information gain;
- false question rate;
- premature closure rate;
- failure-to-ask rate;
- specialist-only and summed/joint performance.

### 19.5 Ask/answer/stop layer

- ask precision and recall;
- answer correctness at chosen closure threshold;
- calibration of closure and confidence;
- inappropriate continuation rate;
- unnecessary stop rate;
- SHOULD relevance/severity calibration;
- correct safer-alternative selection;
- proof or trace fidelity.

### 19.6 Efficiency

- wall-clock time;
- CPU/GPU memory;
- energy where measurable;
- token/form count versus Ideon-key reuse;
- dictionary lookup hit rate;
- cached/static, procedural, and neural execution proportions;
- cost per ingested unit and per resolved Ideon.

### 19.7 Required comparisons

1. conventional token/LM baseline;
2. deterministic Prolog/Python only;
3. from-scratch structured model;
4. fine-tuned model;
5. frozen base plus delta model;
6. ablations without context graph;
7. ablations without individual inquiry training;
8. ablations without Whern-derived was/is/will perspective rendering;
9. ablations without SHOULD-driven ask/stop behaviour;
10. graph-key reuse enabled versus disabled.

---

## 20. Acceptance tests

At minimum, the implementation must correctly handle:

1. `sat on the bank` with absent actor and ambiguous bank sense;
2. `the dictionary 2nd edition` versus `dictionary the 2nd edition`, preserving order and different structures;
3. one event rendered as was, is, or will under three reference Wherns;
4. temporal `will be` versus volitional `will/intends`;
5. a prime dictionary Ideon reused inside several composite Ideons;
6. \(C[A,B]\ne C[B,A]\);
7. an Ideon with multiple semantic islands;
8. a relation represented both as a compact edge and as a relation node with provenance;
9. an unresolved open Ideon that asks for a set rather than guessing;
10. a high-relevance SHOULD case that selects a safer route or stops;
11. two paraphrases resolving to one Ideon key where the declared set establishes equivalence;
12. two superficially similar sentences retaining different Ideon keys because context or perspective differs;
13. deterministic repeat runs producing byte-identical structured records;
14. actual reading-stream capture over a held-out natural text.

---

## 21. Implementation stages

### Stage 0: freeze the contracts

- formalise schema, namespaces, set stack, revision policy, and key policy;
- define the Principia primitive sets;
- write conformance tests before model training.

### Stage 1: logical dictionary v0

- implement forms, lexemes, senses, definitions, provenance, and graph entries;
- support at least one English profile and an extensible multilingual contract;
- encode the eleven inquiry slots in every applicable entry.

### Stage 2: Prolog/Python reference

- implement lookup, graph construction, inference, missing-role detection, ask/stop rules, interning, resolution, and realisation;
- provide CLI, API, test suite, and machine/plain-text streams.

### Stage 3: dataset compiler

- compile dictionary entries and micro-worlds into shared training records;
- validate rights, splits, balance, and provenance;
- reach the declared per-specialist scale before joint training claims.

### Stage 4: neural routes

- train from-scratch baseline;
- fine-tune at least one existing model;
- train a delta model against the same reference records;
- preserve identical evaluation interfaces.

### Stage 5: natural reading

- stream actual outputs while reading held-out works;
- retain complete structured output even when too verbose for one human;
- derive controlled human-readable verbosity from the full record.

### Stage 6: multilingual sharpening

- add independently sourced dictionaries and grammars;
- test whether added languages improve sense separation and graph structure without forced equivalence;
- measure changes rather than assuming improvement.

---

## 22. Required explicit sets

The following must not be silently assumed:

1. **Prime set:** the Ideons irreducible under each domain and ruleset.
2. **Logic set:** the operators primitive within the active Principia version.
3. **Relation set:** the relations available to graph construction and inference.
4. **Language set:** the active language, dialect, period, register, and script profiles.
5. **Definition set:** the sources and contextual definitions permitted for the task.
6. **Equivalence set:** the conditions under which forms, senses, graphs, or translations share an Ideon.
7. **Closure set:** the roles that must be filled before answering or acting.
8. **Norm set:** the affected parties, goals, rules, permissions, and evidence governing SHOULD.
9. **Privacy/security set:** the inherited and local constraints.

When a required set is missing and affects the result, the system must ask for it.

---

## 23. Completion criterion for this specification

An implementation conforms to this specification when it can:

1. ingest a sentence losslessly;
2. map forms to a contextual candidate set of logical dictionary senses;
3. build a partial or complete Ideon graph;
4. expose actual outputs for all eleven specialist inquiries;
5. represent non-closure without guessing;
6. ask, answer, continue, or stop according to explicit contracts;
7. pair the Ideon with one unique numeric key without equating object and key;
8. resolve the key back to the graph;
9. realise the graph through a selected language profile;
10. reproduce the same result under identical versioned inputs;
11. run the same records through Prolog/Python, from-scratch, fine-tuned, and delta-model routes;
12. report measured results and failures without substituting illustrative output.

The first implementation target is the Principia logical dictionary and Prolog/Python reference semantics. Neural training follows from that contract rather than inventing the contract indirectly from scale.
