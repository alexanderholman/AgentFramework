# Finite computational catalogues

AF ships a project-authored, offline executable seed for issues #110 and #111.
`CatalogueProvider` installs one shared `catalogue-execute` procedure, so these
requests use the normal classification, policy, verification, recording and reuse
pipeline through CLI, REST, GraphQL and MCP task execution:

```sh
af ask 'algorithm insertion-sort {"values":[3,1,2]}'
af ask 'problem order-and-locate {"values":[9,2,9,-1],"target":9}'
af catalogues algorithms
af catalogues problems
af catalogues benchmark merge-sort '{"values":[3,1,2]}' --repetitions 5
```

The result of `order-and-locate` is `{"ordered":[-1,2,9,9],"index":2}`.
Its trace includes the ordering and position nodes, the problem and graph digests,
the algorithm snapshot, and independent whole-problem acceptance. A verified
result from each node alone does not establish the composite result.

## Installed algorithms and structures

| Method ID | Exact contract | Independent acceptance |
| --- | --- | --- |
| `insertion-sort`, `merge-sort` | Ascending integer permutation | Monotonicity plus multiset equality |
| `binary-first` | First matching index in an already ascending sequence, or null | Linear first-occurrence oracle |
| `linear-membership` | Boolean membership | Independent built-in membership |
| `sorted-membership` | Merge sort followed by binary search | Membership against the original sequence |
| `sum-integers` | Exact integer sum | Independent reduction |
| `string-matches` | All overlapping literal Unicode codepoint positions | Repeated native substring search |
| `breadth-first` | Reachable nodes, each once, in nondecreasing distance | Independent edge-relaxation distance oracle |
| `shortest-path` | Minimum-edge directed path, or null when unreachable | Edge validity and independent minimum distance |

Descriptors distinguish algorithm and implementation identity, schemas,
preconditions, postconditions, effects, exactness, determinism, complexity models,
provenance and dependencies. Three structure descriptors cover the actual integer
vector, FIFO deque and directed adjacency-list representations used here.
Unknown thread-safety and measured-cost properties remain null.

Bounds are enforced before execution: 32 KiB JSON, 1,024 sequence elements,
signed 64-bit input integers, 64 graph nodes, 512 unique directed edges,
2,048 text codepoints and a nonempty pattern of at most 256 codepoints. Sums use
Python integers and do not silently overflow. Boolean and floating-point values
are not integer inputs. Binary search rejects unsorted inputs. Graphs are
unweighted; there are no negative-weight or probabilistic path claims.
Duplicate JSON keys, extra fields, invalid encodings and unsupported method IDs
fail closed. There is no expression evaluator or source-code loader.

Complexities describe the method body under the bounded integer/RAM model.
Validation, snapshot construction and independent verification add costs;
`binary-first` has an O(n) sorted-input validation and independent linear oracle.
Graph construction sorts edges for deterministic traversal. No catalogue entry
claims its complete verified task has only its inner method's asymptotic cost.

## Problems and decomposition

The eight problem IDs are `ordering`, `sorted-position`, `integer-membership`,
`integer-sum`, `substring-occurrences`, `reachable-nodes`,
`shortest-unweighted-path` and `order-and-locate`.
Problem identity and acceptance are separate from implementation identity.
The first seven are prime only within
`af.closed-computational-problems@1.0.0`, the declared finite rules and bounded
input/output abstraction. This is not universal irreducibility.

The admitted composite is ordering followed by sorted position, with typed edge
validation, no orphan nodes, bounded expansion and whole-result verification.
This is a finite rule, not a general user-defined graph executor. Exact IDs and
three explicitly declared aliases (`sort`, `contains`, `sum`) can propose problem
matches. Missing inputs, invalid inputs and no match remain separate states.
The task grammar requires canonical problem IDs; a recognition proposal does not
authorize execution. General natural-language recognition belongs to the Human
Interpreter boundary described in `human-interpreter.md`.

## Provenance, admission and snapshots

All seed contracts, fixtures and implementations are independently project-authored.
No textbook prose/code, competitive-programming corpus or editorials were ingested.
`LicenseRef-AF-Repository` refers to the repository's existing `LICENSE`; it is not
an open-source licence grant. Concept licences remain unknown where there is no
imported source licence record. Future imported concepts, code, fixtures and data
need separate provenance and permission decisions.

The installed source is an explicit execution allowlist. There is no runtime
self-admission route for an imported manifest, generated implementation, timing
claim or template. Changes to installed methods require source review and the
repository's conformance and release gates. This seed does not implement the
full epic's external admission/reviewer/evidence-revocation service.

`ReferenceAlgorithms.snapshot()` includes canonical descriptors, input JSON
schemas, structures and quarantine records. `from_snapshot()` accepts only the
installed compatible definitions, never imports a named module, and rejects
altered identities, schemas, licensing and duplicate records. Quarantine creates
a new immutable snapshot and blocks both the implementation and dependent
composites. Older snapshots remain available for explicit host-managed replay;
loading an old snapshot is not a governance approval to roll back a live policy.
`ProblemCatalogue.snapshot()` and `from_snapshot()` similarly pin the finite
ontology, aliases and decomposition rule. Snapshot digests identify these
metadata records; they are not signed code-artifact attestations.

To retain a snapshot or measurement, save the CLI JSON output as a managed
artifact. Snapshots operate without model/provider access. Benchmark records
include the input and catalogue digests, runtime/platform, sample count, receipt
time, separate preflight, warm method, verification and serialization timings.
A failed independent oracle produces rejected evidence. Peak memory, energy and
host-load fields remain unknown. These smoke observations establish neither
cross-machine comparability nor performance superiority and are not consumed as
admission evidence. Selection uses an explicit bounded reference preference,
with unknown input bounds or requested unmeasured memory constraints returned
as unresolved; it is not presented as benchmark-optimal selection.

## Evidence and remaining scope

`test_algorithm_catalogue.py` covers seeded randomized differential vectors,
edge cases, semantic acceptance, strict bounds, quarantine and snapshot tampering.
`test_problem_catalogue.py` covers identity, schema-invalid decompositions,
replaceable algorithm ports, forged node evidence and whole-result rejection.
`test_catalogue_journey.py` exercises default installation, CLI/REST parity and
reuse. `test_algorithm_benchmarks.py` exercises real measurement records and
negative oracle evidence.

This seed does not complete either broad epic. Remaining scope includes licensed
book/corpus inventories and adapters; extensive algorithm/structure families;
multiple runtime/language conformance; formal proof adapters; persistent admission
and benchmark history; richer resource-aware selection; general typed compositions;
large problem corpora; multiple realistic composite domains; learned recognition
and governed promotion. No such coverage or external-source rights are implied
by the working reference subset.
