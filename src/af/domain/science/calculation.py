"""Exact calculation and separately implemented algebraic identity verification."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction
from statistics import median

from af.domain.science.catalogue import (
    CATALOGUE,
    CATALOGUE_DIGEST,
    CATALOGUE_VERSION,
    CONSTANT_DIGEST,
    CONSTANT_VERSION,
    CONSTANTS,
    DISPLAY_PRECISION,
    UNIT_DIGEST,
    UNIT_VERSION,
    UNITS,
)
from af.domain.science.quantities import (
    CalculationRequest,
    Quantity,
    ScienceInputError,
    validate_request,
)

VERIFIER_ID = "af.science.algebraic-identities/1.0.0"


def decimal_display(value: Fraction) -> str:
    # A fresh context prevents callers' rounding, precision, and traps changing results.
    with localcontext(Context(prec=DISPLAY_PRECISION, rounding=ROUND_HALF_EVEN)):
        return str(Decimal(value.numerator) / Decimal(value.denominator))


@dataclass(frozen=True, slots=True)
class CalculationResult:
    formula_id: str
    formula_digest: str
    exact_value: Fraction
    unit: str
    substitutions: tuple[tuple[str, Quantity], ...]

    def to_dict(self) -> dict[str, object]:
        formula = CATALOGUE[self.formula_id]
        return {
            "reason_code": "science_verified",
            "formula": {
                "id": formula.id,
                "version": formula.version,
                "digest": self.formula_digest,
                "equation": formula.equation,
            },
            "catalogue": {"version": CATALOGUE_VERSION, "digest": CATALOGUE_DIGEST},
            "quantity": {
                "exact": None if formula.id == "root" else str(self.exact_value),
                "rational": str(self.exact_value),
                "decimal": decimal_display(self.exact_value),
                "unit": self.unit,
                "dimension": UNITS[self.unit].dimension,
            },
            "substitutions": {name: quantity.to_dict() for name, quantity in self.substitutions},
            "assumptions": formula.assumptions,
            "domains": [parameter.to_dict() for parameter in formula.parameters],
            "precision": formula.to_dict()["precision"],
            "units": {"version": UNIT_VERSION, "digest": UNIT_DIGEST},
            "constants": {"version": CONSTANT_VERSION, "digest": CONSTANT_DIGEST},
            "verifier": VERIFIER_ID,
        }


def calculate(request: CalculationRequest) -> CalculationResult:
    validate_request(request)
    values = {name: quantity.si_value for name, quantity in request.arguments}
    # This finite dispatch deliberately accepts no Python or symbolic source expression.
    match request.formula_id:
        case "add":
            value = values["a"] + values["b"]
        case "subtract":
            value = values["a"] - values["b"]
        case "multiply":
            value = values["a"] * values["b"]
        case "divide":
            value = values["a"] / values["b"]
        case "ratio":
            value = values["a"] / values["b"]
        case "percent":
            value = 100 * values["a"] / values["b"]
        case "percentage":
            value = values["value"] * values["percent"] / 100
        case "root":
            radicand = values["value"]
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)) as context:
                magnitude = Decimal(abs(radicand.numerator)) / Decimal(radicand.denominator)
                root = context.power(magnitude, Decimal(1) / Decimal(int(values["degree"])))
            with localcontext(Context(prec=DISPLAY_PRECISION, rounding=ROUND_HALF_EVEN)):
                value = Fraction(+root) * (-1 if radicand < 0 else 1)
        case "mean":
            value = sum(values.values(), Fraction()) / len(values)
        case "median":
            value = median(values.values())
        case "minimum":
            value = min(values.values())
        case "maximum":
            value = max(values.values())
        case "variance":
            average = sum(values.values(), Fraction()) / len(values)
            value = sum(((item - average) ** 2 for item in values.values()), Fraction()) / len(
                values
            )
        case "power":
            exponent = int(values["exponent"])
            value = values["base"] ** exponent
        case "convert":
            value = values["value"]
        case "speed":
            value = values["distance"] / values["time"]
        case "time":
            value = values["distance"] / values["speed"]
        case "distance":
            value = values["speed"] * values["time"]
        case "acceleration":
            value = (values["final"] - values["initial"]) / values["time"]
        case "force":
            value = values["mass"] * values["acceleration"]
        case "kinetic_energy":
            value = CONSTANTS["half"] * values["mass"] * values["speed"] ** 2
        case "momentum":
            value = values["mass"] * values["velocity"]
        case "density":
            value = values["mass"] / values["volume"]
        case "pressure":
            value = values["force"] / values["area"]
        case "voltage":
            value = values["current"] * values["resistance"]
        case "electrical_power":
            value = values["voltage"] * values["current"]
        case "mechanical_power":
            value = values["work"] / values["time"]
        case "energy":
            value = values["power"] * values["time"]
        case "current":
            value = values["voltage"] / values["resistance"]
        case "resistance":
            value = values["voltage"] / values["current"]
        case "work":
            value = values["force"] * values["distance"]
        case _:  # validate_request excludes this; guard future catalogue extension.
            raise ValueError("formula has no calculation implementation")
    result = CalculationResult(
        formula_id=request.formula_id,
        formula_digest=request.formula.digest,
        exact_value=value / UNITS[request.output_unit].scale,
        unit=request.output_unit,
        substitutions=request.arguments,
    )
    if not verify_result(request, result):
        raise ValueError("independent scientific verification failed")
    return result


def _product_identity(result: Fraction, first: Fraction, second: Fraction) -> bool:
    if first == 0:
        return result == 0
    return result / first == second


def verify_result(request: CalculationRequest, result: CalculationResult) -> bool:
    """Check identities using inverses rather than replaying the calculation dispatch.

    This independently verifies formula arithmetic, not the suitability of a physical
    model or experimentally supplied inputs. Unit definitions are pinned shared data.
    """
    try:
        validate_request(request)
    except ScienceInputError:
        return False
    if (
        result.formula_id != request.formula_id
        or result.formula_digest != request.formula.digest
        or result.unit != request.output_unit
        or result.substitutions != request.arguments
        or not isinstance(result.exact_value, Fraction)
    ):
        return False
    values = {name: quantity.si_value for name, quantity in request.arguments}
    actual = result.exact_value * UNITS[result.unit].scale
    match request.formula_id:
        case "add":
            return actual - values["a"] == values["b"]
        case "subtract":
            return actual + values["b"] == values["a"]
        case "multiply":
            return _product_identity(actual, values["b"], values["a"])
        case "divide":
            return actual * values["b"] == values["a"]
        case "ratio":
            return actual * values["b"] == values["a"]
        case "percent":
            return actual * values["b"] / 100 == values["a"]
        case "percentage":
            return _product_identity(actual * 100, values["percent"], values["value"])
        case "root":
            radicand = values["value"]
            if radicand == 0:
                return actual == 0
            if (actual < 0) != (radicand < 0):
                return False
            residual = actual ** int(values["degree"]) - radicand
            return abs(residual) <= abs(radicand) * Fraction(1, 10**30)
        case "mean":
            return sum((item - actual for item in values.values()), Fraction()) == 0
        case "median":
            samples = tuple(values.values())

            # Independent order statistics by rank counting; the calculator sorts.
            def ranked(index: int) -> Fraction:
                return min(
                    candidate
                    for candidate in samples
                    if sum(item <= candidate for item in samples) > index
                )

            middle = len(samples) // 2
            if len(samples) % 2:
                return actual == ranked(middle)
            return 2 * actual == ranked(middle - 1) + ranked(middle)
        case "minimum":
            return actual in values.values() and all(actual <= item for item in values.values())
        case "maximum":
            return actual in values.values() and all(actual >= item for item in values.values())
        case "variance":
            n = len(values)
            squares = sum((item * item for item in values.values()), Fraction())
            total = sum(values.values(), Fraction())
            return actual * n * n == squares * n - total * total
        case "power":
            product = Fraction(1)
            exponent = int(values["exponent"])
            for _ in range(abs(exponent)):
                product *= values["base"]
            return actual == product if exponent >= 0 else actual * product == 1
        case "convert":
            return actual == values["value"]
        case "speed":
            return actual * values["time"] == values["distance"]
        case "time":
            return actual * values["speed"] == values["distance"]
        case "distance":
            return _product_identity(actual, values["time"], values["speed"])
        case "acceleration":
            return actual * values["time"] + values["initial"] == values["final"]
        case "force":
            return _product_identity(actual, values["acceleration"], values["mass"])
        case "kinetic_energy":
            speed = values["speed"]
            if speed == 0:
                return actual == 0
            return 2 * actual / speed / speed == values["mass"]
        case "momentum":
            return _product_identity(actual, values["velocity"], values["mass"])
        case "density":
            return actual * values["volume"] == values["mass"]
        case "pressure":
            return actual * values["area"] == values["force"]
        case "voltage":
            return _product_identity(actual, values["resistance"], values["current"])
        case "electrical_power":
            return _product_identity(actual, values["current"], values["voltage"])
        case "mechanical_power":
            return actual * values["time"] == values["work"]
        case "energy":
            return _product_identity(actual, values["time"], values["power"])
        case "current":
            return actual * values["resistance"] == values["voltage"]
        case "resistance":
            return actual * values["current"] == values["voltage"]
        case "work":
            return _product_identity(actual, values["distance"], values["force"])
        case _:
            return False
