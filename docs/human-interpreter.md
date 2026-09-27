# Human Interpreter boundary

The v0.1 reference façade builds on the existing `IdeonController` and controlled
Prime Language dictionary. It preserves the original UTF-8 observation, source
metadata, dictionary hypotheses, recursive inquiry and unknown openings. It does
not replace that ontology or execute an interpreted request.

```sh
af human interpret 'sort integers [3,1,3]'
af human interpret 'does [3,1] contain 1?' --source-id request:1
af human interpret 'bank'
af human interpret 'sort integers [3,1]' --source-kind tool
af human templates
```

`human interpret-json` accepts the same fields as
`POST /api/v1/human/interpret`: `text`, `source_id`, `source_kind`, `rights`,
optional timezone-aware ISO `received_at`, and an optional small semantic
`context` mapping. Omitted observation time is the actual receipt time. Supply the
original timestamp and source metadata for exact deterministic replay.
There are no execution scope, credential or permission fields.

The four installed English templates recognize ordering (`sort integers …` or
`order integers …`), integer sum, membership, and order-and-locate. Slots use
strict integer JSON and a maximum of 64 elements. A matched WHAT contract names
the canonical problem and typed inputs and carries the template/version digest,
observation digest, codepoint spans and byte spans for every extracted field.
Equivalent declared wordings share a meaning digest while preserving separate
source evidence. A successful match is interpretation acceptance, not authority.

The 4 KiB observation bound is measured in bytes, not characters. Decoding is
strict UTF-8. The source remains unchanged; Ideon normalization affects dictionary
lookup only. Tool/document source kinds and top-level quotations/code fences are
quarantined as data. Unsupported wording and ambiguous senses request
clarification. Malformed template slots are rejected. Negation, appended commands
and near matches do not enter a known template by substring resemblance.

The Python application API separates the three masks:

1. `interpret(observation, context)` produces an immutable WHAT contract or
   explicit non-closure and preserves the Ideon record.
2. `assess(interpretation, context, should, how)` passes the stable WHAT to an
   injected SHOULD policy. HOW is called only after permission and must return
   feasibility for the same WHAT digest. Unknown feasibility stays unknown.
3. `render(interpretation, value, audience, release)` independently checks the
   original problem acceptance, builds an evidence-bound claim, then requires an
   explicit release decision for that exact claim and audience. A deterministic
   template renders only the verified problem value.

There is no execution or delivery port. A caller that wants to execute a permitted
problem must still enter AF's normal authorized task pipeline. The HTTP/CLI
interpretation endpoints only inspect meaning; they do not install policy or
invoke HOW. Injected policy ports are host services, not functions supplied in
untrusted text. `RenderedSignal` is an output artifact, not a sent message.

`test_human_interpreter.py` covers equivalent wording with distinct provenance,
retained ambiguity, Unicode bytes, quote/tool isolation, invalid shapes, no HOW
after denial or stale policy evidence, and no release before verification or to a
different audience. `test_human_interface.py` checks exact CLI/JSON/HTTP parity
with pinned metadata and rejects oversized input and invented authority fields.

This is a finite foundation for issue #132. It does not claim unrestricted human
language comprehension, 100 semantic families, multilingual coverage, learned
mask promotion, normative-policy completeness, arbitrary quoted-document parsing,
fully general outcome/redaction templates, or symmetric natural-language output
for every AF capability. Unknown forms remain inspectable instead of being
silently converted into executable meaning.
