"""Bounded exact maths and scalar physics; no inference or physical actuation."""

from af.domain.science.calculation import CalculationResult, calculate, verify_result
from af.domain.science.catalogue import CATALOGUE, UNITS
from af.domain.science.quantities import (
    CalculationRequest,
    Quantity,
    ScienceInputError,
    ScienceReason,
    parse_request,
)

__all__ = [
    "CATALOGUE",
    "UNITS",
    "CalculationRequest",
    "CalculationResult",
    "Quantity",
    "ScienceInputError",
    "ScienceReason",
    "calculate",
    "parse_request",
    "verify_result",
]
