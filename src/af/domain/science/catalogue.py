"""Immutable, finite formula and unit definitions for exact rational calculations.

Dimensions use SI exponents in the order mass, length, time, electric current.
All numerical scales and constants in this catalogue are exact definitions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType

type Dimension = tuple[int, int, int, int]
SCALAR: Dimension = (0, 0, 0, 0)
MASS: Dimension = (1, 0, 0, 0)
LENGTH: Dimension = (0, 1, 0, 0)
TIME: Dimension = (0, 0, 1, 0)
CURRENT: Dimension = (0, 0, 0, 1)
SPEED: Dimension = (0, 1, -1, 0)
ACCELERATION: Dimension = (0, 1, -2, 0)
FORCE: Dimension = (1, 1, -2, 0)
ENERGY: Dimension = (1, 2, -2, 0)
MOMENTUM: Dimension = (1, 1, -1, 0)
DENSITY: Dimension = (1, -3, 0, 0)
PRESSURE: Dimension = (1, -1, -2, 0)
VOLTAGE: Dimension = (1, 2, -3, -1)
RESISTANCE: Dimension = (1, 2, -3, -2)
POWER: Dimension = (1, 2, -3, 0)
AREA: Dimension = (0, 2, 0, 0)
VOLUME: Dimension = (0, 3, 0, 0)
CATALOGUE_VERSION = "1.0.0"
UNIT_VERSION = "af-si-exact-1.0.0"
CONSTANT_VERSION = "af-rational-definitions-1.0.0"
DISPLAY_PRECISION = 34
MAX_PROMPT = 1024
MAX_TOKEN = 96
MAX_DIGITS = 32
MAX_EXPONENT = 100
MAX_POWER = 16
MAX_SAMPLES = 32
STATISTICS = frozenset({"mean", "median", "minimum", "maximum", "variance"})


def digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Unit:
    symbol: str
    dimension: Dimension
    scale: Fraction = Fraction(1)


_UNITS = (
    Unit("1", SCALAR),
    Unit("kg", MASS),
    Unit("g", MASS, Fraction(1, 1000)),
    Unit("m", LENGTH),
    Unit("cm", LENGTH, Fraction(1, 100)),
    Unit("mm", LENGTH, Fraction(1, 1000)),
    Unit("km", LENGTH, Fraction(1000)),
    Unit("s", TIME),
    Unit("ms", TIME, Fraction(1, 1000)),
    Unit("min", TIME, Fraction(60)),
    Unit("h", TIME, Fraction(3600)),
    Unit("A", CURRENT),
    Unit("mA", CURRENT, Fraction(1, 1000)),
    Unit("m/s", SPEED),
    Unit("km/h", SPEED, Fraction(5, 18)),
    Unit("m/s^2", ACCELERATION),
    Unit("N", FORCE),
    Unit("kN", FORCE, Fraction(1000)),
    Unit("J", ENERGY),
    Unit("kJ", ENERGY, Fraction(1000)),
    Unit("kg*m/s", MOMENTUM),
    Unit("kg/m^3", DENSITY),
    Unit("g/cm^3", DENSITY, Fraction(1000)),
    Unit("Pa", PRESSURE),
    Unit("kPa", PRESSURE, Fraction(1000)),
    Unit("V", VOLTAGE),
    Unit("ohm", RESISTANCE),
    Unit("W", POWER),
    Unit("kW", POWER, Fraction(1000)),
    Unit("m^2", AREA),
    Unit("cm^2", AREA, Fraction(1, 10000)),
    Unit("m^3", VOLUME),
    Unit("cm^3", VOLUME, Fraction(1, 1000000)),
    Unit("L", VOLUME, Fraction(1, 1000)),
)
UNITS = MappingProxyType({unit.symbol: unit for unit in _UNITS})
UNIT_DIGEST = digest([(unit.symbol, unit.dimension, str(unit.scale)) for unit in _UNITS])
CONSTANTS = MappingProxyType({"half": Fraction(1, 2)})
CONSTANT_DIGEST = digest({name: str(value) for name, value in CONSTANTS.items()})


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    dimension: Dimension | None = SCALAR
    domain: str = "finite"

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "dimension": self.dimension, "domain": self.domain}


@dataclass(frozen=True, slots=True)
class Formula:
    id: str
    category: str
    equation: str
    parameters: tuple[Parameter, ...]
    output_unit: str | None
    assumptions: tuple[str, ...]
    version: str = CATALOGUE_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "category": self.category,
            "equation": self.equation,
            "parameters": [parameter.to_dict() for parameter in self.parameters],
            "output_unit": self.output_unit,
            "assumptions": self.assumptions,
            "version": self.version,
            "unit_version": UNIT_VERSION,
            "unit_digest": UNIT_DIGEST,
            "constant_version": CONSTANT_VERSION,
            "constant_digest": CONSTANT_DIGEST,
            "precision": {
                "arithmetic": "decimal" if self.id == "root" else "exact-rational",
                "decimal_significant_digits": DISPLAY_PRECISION,
                "rounding": "ROUND_HALF_EVEN",
                "verification_tolerance": (
                    "abs(result**degree - value) <= abs(value) * 1e-30; zero is exact"
                    if self.id == "root"
                    else "0 (exact rational identity)"
                ),
            },
            "complexity": {
                "max_prompt_characters": MAX_PROMPT,
                "max_quantity_characters": MAX_TOKEN,
                "max_numeric_digits": MAX_DIGITS,
                "max_decimal_exponent_magnitude": MAX_EXPONENT,
                "max_integer_power_magnitude": MAX_POWER,
                "max_inputs": MAX_SAMPLES if self.id in STATISTICS else 3,
                "max_arithmetic_operations": 4096,
                "max_typed_rational_bits": 448,
                "max_total_rational_bits": 4096,
            },
        }

    @property
    def digest(self) -> str:
        return digest(self.to_dict())


_EXACT = ("Input literals are exact rational values; measurement uncertainty is not inferred.",)
_CLASSICAL = (
    *_EXACT,
    "Scalar classical relation; the caller must establish applicability of the physical model.",
    "This calculator has no physical-control or actuation capability.",
)
_FORMULAS = (
    Formula("add", "math", "r = a + b", (Parameter("a", None), Parameter("b", None)), None, _EXACT),
    Formula(
        "subtract", "math", "r = a - b", (Parameter("a", None), Parameter("b", None)), None, _EXACT
    ),
    Formula("multiply", "math", "r = a * b", (Parameter("a"), Parameter("b")), "1", _EXACT),
    Formula(
        "divide",
        "math",
        "r = a / b",
        (Parameter("a"), Parameter("b", domain="nonzero")),
        "1",
        _EXACT,
    ),
    Formula(
        "power",
        "math",
        "r = base ** exponent",
        (Parameter("base"), Parameter("exponent", domain="bounded_integer")),
        "1",
        (*_EXACT, "The exponent is an integer from -16 to 16; 0**0 is excluded."),
    ),
    Formula(
        "convert",
        "conversion",
        "r * output_scale = value * input_scale",
        (Parameter("value", None),),
        None,
        _EXACT,
    ),
    Formula(
        "speed",
        "physics",
        "speed = distance / time",
        (Parameter("distance", LENGTH, "nonnegative"), Parameter("time", TIME, "positive")),
        "m/s",
        (*_CLASSICAL, "Average speed over the supplied elapsed time."),
    ),
    Formula(
        "distance",
        "physics",
        "distance = speed * time",
        (Parameter("speed", SPEED, "nonnegative"), Parameter("time", TIME, "nonnegative")),
        "m",
        (*_CLASSICAL, "Speed is constant over the supplied interval."),
    ),
    Formula(
        "acceleration",
        "physics",
        "acceleration = (final - initial) / time",
        (
            Parameter("initial", SPEED),
            Parameter("final", SPEED),
            Parameter("time", TIME, "positive"),
        ),
        "m/s^2",
        (*_CLASSICAL, "Average signed one-dimensional acceleration."),
    ),
    Formula(
        "force",
        "physics",
        "force = mass * acceleration",
        (Parameter("mass", MASS, "nonnegative"), Parameter("acceleration", ACCELERATION)),
        "N",
        (*_CLASSICAL, "Constant mass; signed one-dimensional net force."),
    ),
    Formula(
        "kinetic_energy",
        "physics",
        "energy = mass * speed ** 2 / 2",
        (Parameter("mass", MASS, "nonnegative"), Parameter("speed", SPEED, "nonnegative")),
        "J",
        (*_CLASSICAL, "Translational nonrelativistic kinetic energy."),
    ),
    Formula(
        "momentum",
        "physics",
        "momentum = mass * velocity",
        (Parameter("mass", MASS, "nonnegative"), Parameter("velocity", SPEED)),
        "kg*m/s",
        (*_CLASSICAL, "Signed one-dimensional nonrelativistic momentum."),
    ),
    Formula(
        "density",
        "physics",
        "density = mass / volume",
        (Parameter("mass", MASS, "nonnegative"), Parameter("volume", VOLUME, "positive")),
        "kg/m^3",
        (*_CLASSICAL, "Average mass density of the supplied volume."),
    ),
    Formula(
        "pressure",
        "physics",
        "pressure = force / area",
        (Parameter("force", FORCE, "nonnegative"), Parameter("area", AREA, "positive")),
        "Pa",
        (*_CLASSICAL, "Normal compressive force distributed over the supplied area."),
    ),
    Formula(
        "voltage",
        "physics",
        "voltage = current * resistance",
        (Parameter("current", CURRENT), Parameter("resistance", RESISTANCE, "nonnegative")),
        "V",
        (*_CLASSICAL, "Ohmic element at fixed conditions; passive sign convention."),
    ),
    Formula(
        "electrical_power",
        "physics",
        "power = voltage * current",
        (Parameter("voltage", VOLTAGE), Parameter("current", CURRENT)),
        "W",
        (*_CLASSICAL, "Instantaneous or DC power with passive sign convention."),
    ),
    Formula(
        "work",
        "physics",
        "work = force * distance",
        (Parameter("force", FORCE), Parameter("distance", LENGTH, "nonnegative")),
        "J",
        (*_CLASSICAL, "Constant signed force component parallel to displacement."),
    ),
)
_EXTENDED_FORMULAS: tuple[Formula, ...] = (
    Formula(
        "root",
        "math",
        "r ** degree = value",
        (Parameter("value"), Parameter("degree", domain="root_degree")),
        "1",
        (*_EXACT, "Real principal root; negative inputs require odd degree from 1 to 16."),
    ),
    Formula(
        "ratio",
        "math",
        "r = a / b",
        (Parameter("a", None), Parameter("b", None, "nonzero")),
        "1",
        _EXACT,
    ),
    Formula(
        "percent",
        "math",
        "r = 100 * a / b",
        (Parameter("a", None), Parameter("b", None, "nonzero")),
        "1",
        _EXACT,
    ),
    Formula(
        "percentage",
        "math",
        "r = value * percent / 100",
        (Parameter("value", None), Parameter("percent")),
        None,
        _EXACT,
    ),
    Formula(
        "mean",
        "math",
        "r = sum(values) / count(values)",
        (Parameter("values", None, "sample_list"),),
        None,
        _EXACT,
    ),
    Formula(
        "median",
        "math",
        "r = middle value or mean of two middle sorted values",
        (Parameter("values", None, "sample_list"),),
        None,
        _EXACT,
    ),
    Formula(
        "minimum",
        "math",
        "r = minimum(values)",
        (Parameter("values", None, "sample_list"),),
        None,
        _EXACT,
    ),
    Formula(
        "maximum",
        "math",
        "r = maximum(values)",
        (Parameter("values", None, "sample_list"),),
        None,
        _EXACT,
    ),
    Formula(
        "variance",
        "math",
        "r = sum((x - mean(values)) ** 2) / count(values)",
        (Parameter("values", SCALAR, "sample_list"),),
        "1",
        (*_EXACT, "Population variance of dimensionless values; no sample correction."),
    ),
    Formula(
        "time",
        "physics",
        "time = distance / speed",
        (Parameter("distance", LENGTH, "nonnegative"), Parameter("speed", SPEED, "positive")),
        "s",
        (*_CLASSICAL, "Constant nonzero speed over the supplied distance."),
    ),
    Formula(
        "power",
        "physics",
        "power = work / time",
        (Parameter("work", ENERGY), Parameter("time", TIME, "positive")),
        "W",
        (*_CLASSICAL, "Average signed power over the supplied interval."),
    ),
    Formula(
        "energy",
        "physics",
        "energy = power * time",
        (Parameter("power", POWER), Parameter("time", TIME, "nonnegative")),
        "J",
        (*_CLASSICAL, "Constant signed power over the supplied interval."),
    ),
    Formula(
        "current",
        "physics",
        "current = voltage / resistance",
        (Parameter("voltage", VOLTAGE), Parameter("resistance", RESISTANCE, "positive")),
        "A",
        (*_CLASSICAL, "Ohmic element at fixed conditions; passive sign convention."),
    ),
    Formula(
        "resistance",
        "physics",
        "resistance = voltage / current",
        (Parameter("voltage", VOLTAGE), Parameter("current", CURRENT, "nonzero")),
        "ohm",
        (*_CLASSICAL, "Effective signed voltage-to-current ratio at fixed conditions."),
    ),
)
# Mathematical power and physical power have distinct catalogue identities.
_EXTENDED_FORMULAS = tuple(
    Formula(
        "mechanical_power",
        item.category,
        item.equation,
        item.parameters,
        item.output_unit,
        item.assumptions,
    )
    if item.id == "power"
    else item
    for item in _EXTENDED_FORMULAS
)
CATALOGUE = MappingProxyType({formula.id: formula for formula in (*_FORMULAS, *_EXTENDED_FORMULAS)})
CATALOGUE_DIGEST = digest([formula.to_dict() for formula in CATALOGUE.values()])
