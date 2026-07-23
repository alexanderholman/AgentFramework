# Recursive inquiry expressions and situated WHEN marks

Status: bounded reference contract implemented on the Prime Language / Ideon branch.

## General recursion

Recursion belongs to the inquiry structure generally; it is not a special property of
`WHEN`. Every Prime Language question type may be represented as either a specialist
leaf or a bounded group:

```text
QExpression := QLeaf | QGroup
QGroup      := (question, operator, members, status, role, evidence)
operator    := ALL | ANY | SEQUENCE | TRIGGER
```

The group question and child questions need not be identical. This permits `WHERN` to
contain a recursive `WHERE` expression and a recursive `WHEN` expression without
flattening either constituent. Specialist modules remain responsible for validating leaf
semantics; the shared contract only owns recursion, canonical identity, transport, and
finite depth/size bounds.

Multiplicity does not imply conflict. Two valid `WHEN` leaves may be composed by `ALL`,
`ANY`, `SEQUENCE`, or `TRIGGER`. A group is contested only when its specialist analysis
finds a genuine conflict or cannot establish the intended relation. Unknown composition
must remain open rather than invalidating otherwise valid leaves.

## Situated physical marks

A UTC timestamp is a conventional coordinate projection, not time itself. The physical
reference leaf for a resolved instant is a `WhenMark`: an atomic/local clock reading on a
declared worldline, tied to the event at which the reading was recorded.

```text
WhenMark
  clock_ref
  worldline_ref
  proper_time_seconds
  event
    position_ref
    velocity_ref
    frame_ref
    metric_model_ref
    local_state_ref
  uncertainty_seconds
  provenance[]
  utc_projection?
```

The referenced position, velocity, frame, and metric records must declare units,
coordinate conventions, schemas, and revisions. `local_state_ref` identifies the
scope-bounded state of the relevant position at the event; it is not an attempted snapshot
of the entire universe.

Two marks with the same UTC projection remain distinct when their worldline, position,
velocity, metric context, local state, uncertainty, or provenance differs. Comparing marks
from different worldlines or frames requires an explicit comparison/transform rule and must
not silently fall back to UTC scalar ordering.

The current implementation is deliberately a reference contract, not a relativity engine.
Future adapters may provide coordinate transforms and comparison evidence without changing
the authoritative mark schema.

## Integration sequence

1. Use the shared recursive expression contract for inquiry records.
2. Preserve every typed WHEN member and its explicit operator through inquiry, decision,
   graph construction, and canonical records.
3. Represent `WHERN` as a group containing the authoritative WHERE and WHEN expressions.
4. Add `WhenMark` as a typed WHEN leaf while retaining timestamp/event/criterion/interval/
   ordering leaves.
5. Only derive `WAS`, `IS`, or `PROJECTED` when a declared comparison rule and sufficient
   evidence support the result; otherwise preserve an open or contested perspective.
