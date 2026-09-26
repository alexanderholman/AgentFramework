# Deterministic mathematics and physics

The installed `science-calculate` procedure accepts a finite grammar through the
shared CLI, REST, GraphQL and MCP task/ask interfaces. It does not evaluate Python,
accept arbitrary symbolic expressions, call a model, or control physical devices.

```sh
af ask 'math add a=1/3 b=1/6'
af ask 'math root value=2 degree=2'
af ask 'math mean values=1 m,200 cm,3 m'
af ask 'physics kinetic_energy mass=2 kg speed=36 km/h to=kJ'
```

A request is `math FORMULA name=quantity ...`, `physics FORMULA name=quantity ...`,
or `calculate FORMULA name=quantity ...`. A quantity is an integer, decimal,
scientific literal, or integer fraction followed by an optional exact unit symbol.
Dimensionless inputs may omit their unit. `to=UNIT` chooses a compatible output
unit. Units are case sensitive. List statistics use `values=1,2,3` or fully
specified quantities such as `values=1 m,200 cm,3 m`.

| Formula | Inputs | Output |
| --- | --- | --- |
| add, subtract | a, b (matching dimensions) | first input unit |
| multiply, divide | a, b (dimensionless) | dimensionless |
| power | base, exponent (integer -16 through 16) | dimensionless |
| root | value, degree (integer 1 through 16) | dimensionless |
| ratio, percent | a, b (matching dimensions) | ratio or numeric percentage |
| percentage | value, percent | value unit |
| mean, median, minimum, maximum | values (1–32 matching quantities) | first input unit |
| variance | values (1–32 dimensionless values) | population variance |
| convert | value | requested compatible unit |
| speed | distance, time | m/s |
| distance | speed, time | m |
| time | distance, speed | s |
| acceleration | initial, final (velocities), time | m/s^2 |
| force | mass, acceleration | N |
| kinetic_energy | mass, speed | J |
| momentum | mass, velocity | kg*m/s |
| density | mass, volume | kg/m^3 |
| pressure | force, area | Pa |
| voltage | current, resistance | V |
| current | voltage, resistance | A |
| resistance | voltage, current | ohm |
| electrical_power | voltage, current | W |
| mechanical_power | work, time | W |
| energy | power, time | J |
| work | force, distance | J |

`physics power` aliases `mechanical_power`; `math power` is exponentiation.
`convert` uses the `calculate` prefix. A fraction such as `1/3` remains exact.

The canonical catalogue is the immutable `af.domain.science.CATALOGUE` mapping.
Every formula has a version, SHA-256 digest, equation, parameter dimensions and
domains, assumptions, precision, verification tolerance, and resource limits.
The unit and rational-constant packs are also versioned and digested. Conversion
scales are exact SI definitions; no empirical physical constants are needed by
these formulas.

Rational calculations retain exact `Fraction` values and present a 34-significant-
digit `Decimal` string rounded half-even. Roots use a 50-digit intermediate and
34-digit final decimal; their `quantity.exact` is null. `quantity.rational` records
the exact rational representation of that rounded approximation. Root verification
independently checks the rational residual against `abs(input) * 1e-30`; zero roots
must be exact and principal-root sign is checked. Other formulas use zero-tolerance
independent inverse or algebraic identities. Statistics additionally use independent
rank or moment identities. Verification establishes arithmetic correctness within
the stated model, not physical applicability or measurement accuracy.

Inputs are bounded to 1,024 request characters, 96 characters per quantity,
32 significand digits, decimal exponent magnitude 100, power magnitude 16,
32 samples, 448 bits per typed rational, and 4,096 total input-rational bits.
Negative mass/speed, nonpositive required denominators, incompatible dimensions,
even roots of negatives, and undefined zero powers fail before calculation.
Malformed code-like text is rejected by the parser. Structured callers receive
`ScienceInputError.reason`; existing transport validation maps rejected requests
to its standard unsupported-work outcome.

The JSON result includes substitutions with original and SI values, quantity,
formula/catalogue/unit/constant identity and digests, assumptions, precision,
independent verifier identity and stable `science_verified` reason code. These
fields remain present when a verified result is reused.

Validation: `tests/unit/test_science.py` and
`tests/feature/test_science_journey.py` cover catalogue examples, properties,
inverse and dimensional checks, bounds, malformed inputs, tampering, independent
rounding contexts, CLI/REST parity and verified reuse.
