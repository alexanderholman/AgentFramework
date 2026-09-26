"""Bounded numerical literals and fixed unit symbols; no expression evaluator."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from af.domain.science.catalogue import (
    CATALOGUE,
    MAX_DIGITS,
    MAX_EXPONENT,
    MAX_POWER,
    MAX_PROMPT,
    MAX_SAMPLES,
    MAX_TOKEN,
    STATISTICS,
    UNITS,
    Formula,
)
from af.framework.errors import InvalidSelectionProposal


class ScienceReason(StrEnum):
    INVALID_SYNTAX = "science_invalid_syntax"
    INVALID_ARGUMENTS = "science_invalid_arguments"
    UNKNOWN_FORMULA = "science_unknown_formula"
    UNKNOWN_UNIT = "science_unknown_unit"
    DIMENSION_MISMATCH = "science_dimension_mismatch"
    OUT_OF_DOMAIN = "science_out_of_domain"
    COMPLEXITY_LIMIT = "science_complexity_limit"


class ScienceInputError(InvalidSelectionProposal):
    def __init__(self, reason: ScienceReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


# Fraction input is an integer ratio; decimals optionally use scientific notation.
_LITERAL = r"[+-]?(?:[0-9]+/[0-9]+|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
_QUANTITY = re.compile(rf"(?P<value>{_LITERAL})(?:\s+(?P<unit>[A-Za-z0-9*/^]+))?", re.ASCII)
_REQUEST = re.compile(r"(math|physics|calculate)\s+([a-z_]+)\s+(.+)", re.ASCII)
_FIELD = re.compile(r"([a-z_]+)=(.+)", re.ASCII)


@dataclass(frozen=True, slots=True)
class Quantity:
    value: Fraction
    unit: str

    @property
    def si_value(self) -> Fraction:
        return self.value * UNITS[self.unit].scale

    def to_dict(self) -> dict[str, str]:
        return {"exact": str(self.value), "unit": self.unit, "si_exact": str(self.si_value)}


@dataclass(frozen=True, slots=True)
class CalculationRequest:
    formula_id: str
    arguments: tuple[tuple[str, Quantity], ...]
    output_unit: str

    @property
    def formula(self) -> Formula:
        try:
            return CATALOGUE[self.formula_id]
        except KeyError as error:
            raise ScienceInputError(ScienceReason.UNKNOWN_FORMULA) from error


def parse_quantity(text: str) -> Quantity:
    if len(text) > MAX_TOKEN:
        raise ScienceInputError(ScienceReason.COMPLEXITY_LIMIT)
    match = _QUANTITY.fullmatch(text)
    if match is None:
        raise ScienceInputError(ScienceReason.INVALID_SYNTAX)
    literal = match.group("value")
    significand, _, exponent = literal.lower().partition("e")
    if sum(character.isdigit() for character in significand) > MAX_DIGITS:
        raise ScienceInputError(ScienceReason.COMPLEXITY_LIMIT)
    if exponent and (len(exponent.lstrip("+-")) > 3 or abs(int(exponent)) > MAX_EXPONENT):
        raise ScienceInputError(ScienceReason.COMPLEXITY_LIMIT)
    unit = match.group("unit") or "1"
    if unit not in UNITS:
        raise ScienceInputError(ScienceReason.UNKNOWN_UNIT)
    try:
        value = Fraction(literal)
    except ZeroDivisionError as error:
        raise ScienceInputError(ScienceReason.OUT_OF_DOMAIN) from error
    return Quantity(value, unit)


def parse_request(prompt: str) -> CalculationRequest:
    if len(prompt) > MAX_PROMPT:
        raise ScienceInputError(ScienceReason.COMPLEXITY_LIMIT)
    match = _REQUEST.fullmatch(prompt.strip())
    if match is None:
        raise ScienceInputError(ScienceReason.INVALID_SYNTAX)
    category, formula_id, body = match.groups()
    if category == "physics" and formula_id == "power":
        formula_id = "mechanical_power"
    formula = CATALOGUE.get(formula_id)
    if formula is None or (category != "calculate" and category != formula.category):
        raise ScienceInputError(ScienceReason.UNKNOWN_FORMULA)
    fields = re.split(r"\s+(?=[a-z_]+=)", body)
    if len(fields) > 4:
        raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
    arguments: dict[str, Quantity] = {}
    target: str | None = None
    samples: tuple[Quantity, ...] | None = None
    for field in fields:
        pair = _FIELD.fullmatch(field)
        if pair is None:
            raise ScienceInputError(ScienceReason.INVALID_SYNTAX)
        name, value = pair.groups()
        if name == "to":
            if target is not None:
                raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
            if value not in UNITS:
                raise ScienceInputError(ScienceReason.UNKNOWN_UNIT)
            target = value
        elif name == "values" and formula_id in STATISTICS:
            parts = value.split(",")
            if samples is not None:
                raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
            if not 1 <= len(parts) <= MAX_SAMPLES:
                raise ScienceInputError(ScienceReason.COMPLEXITY_LIMIT)
            samples = tuple(parse_quantity(part.strip()) for part in parts)
        else:
            if name in arguments:
                raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
            arguments[name] = parse_quantity(value)
    if formula_id in STATISTICS:
        if samples is None or arguments:
            raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
        request = CalculationRequest(
            formula_id,
            tuple((f"x{index}", quantity) for index, quantity in enumerate(samples)),
            target or formula.output_unit or samples[0].unit,
        )
        validate_request(request)
        return request
    expected = {parameter.name for parameter in formula.parameters}
    if set(arguments) != expected:
        raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
    output_unit = target or formula.output_unit or arguments[formula.parameters[0].name].unit
    request = CalculationRequest(
        formula_id,
        tuple((parameter.name, arguments[parameter.name]) for parameter in formula.parameters),
        output_unit,
    )
    validate_request(request)
    return request


def validate_request(request: CalculationRequest) -> None:
    """Also guard direct typed callers, independently of the text parser."""
    formula = request.formula
    arguments = dict(request.arguments)
    expected = (
        {f"x{index}" for index in range(len(arguments))}
        if formula.id in STATISTICS
        else {parameter.name for parameter in formula.parameters}
    )
    if len(arguments) != len(request.arguments) or set(arguments) != expected:
        raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
    if formula.id in STATISTICS and not 1 <= len(arguments) <= MAX_SAMPLES:
        raise ScienceInputError(ScienceReason.COMPLEXITY_LIMIT)
    if request.output_unit not in UNITS:
        raise ScienceInputError(ScienceReason.UNKNOWN_UNIT)
    total_bits = 0
    for name, quantity in request.arguments:
        parameter = (
            formula.parameters[0]
            if formula.id in STATISTICS
            else next(parameter for parameter in formula.parameters if parameter.name == name)
        )
        if not isinstance(quantity.value, Fraction):
            raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
        if quantity.unit not in UNITS:
            raise ScienceInputError(ScienceReason.UNKNOWN_UNIT)
        # Parsing can produce at most 133 decimal digits including a bounded exponent.
        if (
            max(abs(quantity.value.numerator).bit_length(), quantity.value.denominator.bit_length())
            > 448
        ):
            raise ScienceInputError(ScienceReason.COMPLEXITY_LIMIT)
        total_bits += max(
            abs(quantity.value.numerator).bit_length(), quantity.value.denominator.bit_length()
        )
        if (
            parameter.dimension is not None
            and UNITS[quantity.unit].dimension != parameter.dimension
        ):
            raise ScienceInputError(ScienceReason.DIMENSION_MISMATCH)
        value = quantity.si_value
        if (
            (parameter.domain == "nonnegative" and value < 0)
            or (parameter.domain == "positive" and value <= 0)
            or (parameter.domain == "nonzero" and value == 0)
            or (
                parameter.domain == "bounded_integer"
                and (value.denominator != 1 or abs(value) > MAX_POWER)
            )
            or (
                parameter.domain == "root_degree"
                and (value.denominator != 1 or not 1 <= value <= MAX_POWER)
            )
        ):
            raise ScienceInputError(ScienceReason.OUT_OF_DOMAIN)
    if total_bits > 4096:
        raise ScienceInputError(ScienceReason.COMPLEXITY_LIMIT)
    if formula.id in STATISTICS or formula.id in {"add", "subtract", "ratio", "percent"}:
        dimensions = {UNITS[quantity.unit].dimension for quantity in arguments.values()}
        if len(dimensions) != 1:
            raise ScienceInputError(ScienceReason.DIMENSION_MISMATCH)
    canonical_unit = formula.output_unit or request.arguments[0][1].unit
    if UNITS[request.output_unit].dimension != UNITS[canonical_unit].dimension:
        raise ScienceInputError(ScienceReason.DIMENSION_MISMATCH)
    if formula.id == "power" and arguments["base"].value == 0 and arguments["exponent"].value <= 0:
        raise ScienceInputError(ScienceReason.OUT_OF_DOMAIN)
    if (
        formula.id == "root"
        and arguments["value"].value < 0
        and int(arguments["degree"].value) % 2 == 0
    ):
        raise ScienceInputError(ScienceReason.OUT_OF_DOMAIN)
