# Execution trace privacy

AF execution traces are observational metadata. New traces retain the trace ID,
registered intent/procedure/provider identifiers, routing tier, finite decision
reason codes, verification/reuse outcomes, stage sequence and timestamp. Prompt
text is `[redacted]`, arguments are empty, and a present output is `[redacted]`.
Free-form verifier explanations become `passed` or `failed`. Prompt hashes are not
retained because short inputs can be recovered by dictionary attacks.

The pipeline minimizes content before calling even a host-supplied repository.
The built-in memory and SQL repositories also minimize direct writes. SQL reads
minimize historical rows before returning them. `UnsupportedWork` and
`VerificationFailed` error strings contain no request or provider-output content.
Typed execution inputs and returned results retain their original semantics.

Tasks are different: explicit Task creation deliberately stores the request and
result so the owner can retrieve and edit them. Task search therefore still finds
Task content; new trace search indexes contain no duplicate request content.
Workflow checkpoints, approved engineering inputs, notification outboxes and
backups have their own documented storage and retention policies. This change does
not promise deletion of every application record.

Existing database bytes, old search indexes and retained backups can contain
historical content. Redaction on read does not physically erase them. Operators
must apply the documented retention, index rebuild and backup lifecycle when
removing historical information. Backups are independent sensitive copies; there
is no automatic purge of external backup media or secure-media-erasure claim.

The host controls installed Python extensions. This metadata policy is not a
sandbox for malicious local code; provider identifiers and diagnostic reason codes
must follow the extension contracts. Test evidence includes
`tests/integration/test_trace_privacy.py` and cross-interface pipeline/search tests.
