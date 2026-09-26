"""Acceptance properties of the bounded mathematics and physics catalogue."""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction

import pytest

from af.domain.science import (
    CATALOGUE,
    ScienceInputError,
    calculate,
    parse_request,
    verify_result,
)
from af.domain.science_procedures import ScienceClassifier, ScienceProcedure, ScienceVerifier
from af.framework.types import Classification, ProcedureResult, WorkRequest


@pytest.mark.parametrize(
    ("prompt", "exact", "unit"),
    [
        ("math add a=1/3 b=1/6", "1/2", "1"),
        ("math subtract a=-2 b=3", "-5", "1"),
        ("math multiply a=0.1 b=0.2", "1/50", "1"),
        ("math divide a=1 b=3", "1/3", "1"),
        ("math power base=2 exponent=-3", "1/8", "1"),
        ("calculate convert value=36 km/h to=m/s", "10", "m/s"),
        ("physics speed distance=2 km time=4 min", "25/3", "m/s"),
        ("physics distance speed=3 m/s time=2 s", "6", "m"),
        ("physics acceleration initial=3 m/s final=1 m/s time=2 s", "-1", "m/s^2"),
        ("physics force mass=2000 g acceleration=-3 m/s^2", "-6", "N"),
        ("physics kinetic_energy mass=2 kg speed=3 m/s", "9", "J"),
        ("physics momentum mass=2 kg velocity=-3 m/s", "-6", "kg*m/s"),
        ("physics density mass=2 kg volume=1000 cm^3", "2000", "kg/m^3"),
        ("physics pressure force=6 N area=2 m^2", "3", "Pa"),
        ("physics voltage current=-2 A resistance=3 ohm", "-6", "V"),
        ("physics electrical_power voltage=3 V current=2 A", "6", "W"),
        ("physics work force=3 N distance=2 m", "6", "J"),
    ],
)
def test_every_formula_computes_exact_quantity_with_verified_provenance(prompt, exact, unit):
    request = parse_request(prompt)
    result = calculate(request)
    assert result.exact_value == Fraction(exact)
    assert result.unit == unit
    assert verify_result(request, result)
    payload = result.to_dict()
    assert payload["reason_code"] == "science_verified"
    assert payload["formula"]["digest"].startswith("sha256:")
    assert payload["precision"]["arithmetic"] == "exact-rational"
    assert payload["substitutions"]
    assert payload["assumptions"]


def test_addition_commutes_and_subtraction_inverts_across_bounded_rationals():
    for a in range(-8, 9):
        for b in range(-5, 6):
            first = calculate(parse_request(f"math add a={a}/7 b={b}/3"))
            second = calculate(parse_request(f"math add a={b}/3 b={a}/7"))
            assert first.exact_value == second.exact_value
            inverse = calculate(parse_request(f"math subtract a={first.exact_value} b={b}/3"))
            assert inverse.exact_value == Fraction(a, 7)


def test_kinetic_energy_is_nonnegative_and_quadratic_in_speed():
    for mass in (0, 1, 2, 100):
        for speed in range(6):
            base = calculate(
                parse_request(f"physics kinetic_energy mass={mass} kg speed={speed} m/s")
            )
            double = calculate(
                parse_request(f"physics kinetic_energy mass={mass} kg speed={speed * 2} m/s")
            )
            assert base.exact_value >= 0
            assert double.exact_value == 4 * base.exact_value


def test_conversion_round_trip_and_equivalent_input_units():
    energy = calculate(parse_request("physics kinetic_energy mass=2000 g speed=36 km/h to=kJ"))
    assert energy.exact_value == Fraction(1, 10)
    assert energy.unit == "kJ"
    converted = calculate(parse_request("calculate convert value=1/10 kJ to=J"))
    assert converted.exact_value == 100
    assert (
        converted.exact_value
        == calculate(parse_request("physics kinetic_energy mass=2 kg speed=10 m/s")).exact_value
    )


@pytest.mark.parametrize(
    ("prompt", "reason"),
    [
        ("math divide a=1 b=0", "science_out_of_domain"),
        ("math power base=0 exponent=-1", "science_out_of_domain"),
        ("math power base=0 exponent=0", "science_out_of_domain"),
        ("math power base=2 exponent=17", "science_out_of_domain"),
        ("math power base=2 exponent=1/2", "science_out_of_domain"),
        ("physics kinetic_energy mass=-1 kg speed=2 m/s", "science_out_of_domain"),
        ("physics speed distance=1 m time=0 s", "science_out_of_domain"),
        ("physics force mass=1 s acceleration=1 m/s^2", "science_dimension_mismatch"),
        ("physics force mass=1 kg acceleration=1 m/s^2 to=J", "science_dimension_mismatch"),
        ("calculate convert value=1 kg to=s", "science_dimension_mismatch"),
        ("math add a=1 m b=2 s", "science_dimension_mismatch"),
        ("math add a=NaN b=1", "science_invalid_syntax"),
        ("math add a=1e101 b=1", "science_complexity_limit"),
        ("math add a=1e-101 b=1", "science_complexity_limit"),
        ("math add a=1/0 b=1", "science_out_of_domain"),
        ("math add a=1 b=__import__('os')", "science_invalid_syntax"),
        ("math add a=1 b=2; print(2)", "science_invalid_syntax"),
        ("math add a=1 a=2 b=3", "science_invalid_arguments"),
        ("math add a=1", "science_invalid_arguments"),
        ("math add a=1 b=2 c=3", "science_invalid_arguments"),
        ("math add a=1 zz b=2", "science_unknown_unit"),
        ("math solve a=1", "science_unknown_formula"),
        ("physics add a=1 b=2", "science_unknown_formula"),
        ("math add " + "a" * 1100, "science_complexity_limit"),
    ],
)
def test_invalid_requests_fail_before_calculation(prompt, reason):
    with pytest.raises(ScienceInputError) as failure:
        parse_request(prompt)
    assert failure.value.reason.value == reason


def test_formula_and_unit_catalogues_are_immutable_and_digest_is_stable():
    original = CATALOGUE["force"]
    assert original.digest == CATALOGUE["force"].digest
    assert replace(original, equation="F = mass + acceleration").digest != original.digest
    with pytest.raises(TypeError):
        CATALOGUE["force"] = original


def test_independent_verifier_rejects_result_and_provenance_tampering():
    request = parse_request("physics force mass=2 kg acceleration=3 m/s^2")
    result = calculate(request)
    assert not verify_result(request, replace(result, exact_value=Fraction(7)))
    assert not verify_result(request, replace(result, unit="J"))
    assert not verify_result(request, replace(result, formula_digest="sha256:wrong"))


def test_pipeline_contribution_handles_supported_requests_and_rejects_tampering():
    request = WorkRequest(prompt="math divide a=1 b=3")
    classifier = ScienceClassifier()
    classification = classifier.classify(request)
    assert classification is not None
    procedure = ScienceProcedure()
    procedure.validate(classification)
    result = procedure.execute(classification)
    verifier = ScienceVerifier()
    verdict = verifier.verify(request, classification, procedure, result)
    assert verdict is not None and verdict.passed
    payload = json.loads(result.output)
    assert payload["quantity"]["exact"] == "1/3"
    assert len(payload["quantity"]["decimal"].replace("0.", "")) == 34
    bad = verifier.verify(request, classification, procedure, ProcedureResult(output="{}"))
    assert bad is not None and not bad.passed
    assert classifier.classify(WorkRequest(prompt="tell me a joke")) is None
    other = Classification(intent="unknown", arguments={}, confidence=0, classifier="test")
    assert verifier.verify(request, other, procedure, result) is None


@pytest.mark.parametrize(
    ("prompt", "expected", "unit"),
    [
        ("math ratio a=2 m b=4 cm", Fraction(50), "1"),
        ("math percent a=2 kg b=4 kg", Fraction(50), "1"),
        ("math percentage value=20 m percent=25", Fraction(5), "m"),
        ("math mean values=1 m,200 cm,3 m", Fraction(2), "m"),
        ("math median values=4,1,2,3", Fraction(5, 2), "1"),
        ("math minimum values=4,1,2,3", Fraction(1), "1"),
        ("math maximum values=4,1,2,3", Fraction(4), "1"),
        ("math variance values=1,2,3", Fraction(2, 3), "1"),
        ("math add a=1 m b=50 cm", Fraction(3, 2), "m"),
        ("physics time distance=10 m speed=2 m/s", Fraction(5), "s"),
        ("physics power work=10 J time=2 s", Fraction(5), "W"),
        ("physics energy power=10 W time=2 s", Fraction(20), "J"),
        ("physics current voltage=10 V resistance=2 ohm", Fraction(5), "A"),
        ("physics resistance voltage=10 V current=2 A", Fraction(5), "ohm"),
    ],
)
def test_statistics_proportions_and_inverse_physics(prompt, expected, unit):
    request = parse_request(prompt)
    result = calculate(request)
    assert result.exact_value == expected
    assert result.unit == unit
    assert verify_result(request, result)


def test_roots_use_explicit_decimal_precision_and_independent_residual():
    for prompt, radicand, degree in [
        ("math root value=2 degree=2", Fraction(2), 2),
        ("math root value=-8 degree=3", Fraction(-8), 3),
        ("math root value=1e-100 degree=16", Fraction(1, 10**100), 16),
        ("math root value=1e100 degree=16", Fraction(10**100), 16),
    ]:
        request = parse_request(prompt)
        result = calculate(request)
        assert abs(result.exact_value**degree - radicand) <= abs(radicand) * Fraction(1, 10**30)
        assert verify_result(request, result)
        assert result.to_dict()["quantity"]["exact"] is None
        assert result.to_dict()["precision"]["arithmetic"] == "decimal"
        assert not verify_result(request, replace(result, exact_value=result.exact_value * 2))
    assert calculate(parse_request("math root value=0 degree=2")).exact_value == 0


@pytest.mark.parametrize(
    "prompt",
    [
        "math root value=-2 degree=2",
        "math root value=2 degree=0",
        "math root value=2 degree=17",
        "math root value=2 degree=1/2",
        "math mean values=1 m,2 s",
        "math variance values=1 m,2 m",
        "math mean values=1,2 values=3,4",
        "math mean values=1,2 unexpected=3",
        "math mean values=1,2 to=s",
        "math mean values=" + ",".join(["1"] * 33),
        "math percent a=2 s b=3 m",
    ],
)
def test_extended_catalogue_domains_are_bounded(prompt):
    with pytest.raises(ScienceInputError):
        parse_request(prompt)


def test_median_duplicate_boundaries_and_statistics_permutation_invariance():
    for values in ("1,1,4,4", "1,1,1,4", "1,2,3", "2", "3,3"):
        for name in ("mean", "median", "variance", "minimum", "maximum"):
            request = parse_request(f"math {name} values={values}")
            result = calculate(request)
            reordered = parse_request(f"math {name} values={','.join(reversed(values.split(',')))}")
            assert calculate(reordered).exact_value == result.exact_value
            assert verify_result(request, result)
            assert not verify_result(request, replace(result, exact_value=result.exact_value + 1))
    request = parse_request("math median values=1,1,4,4")
    assert not verify_result(request, replace(calculate(request), exact_value=Fraction(1)))


def test_decimal_rendering_and_roots_ignore_process_decimal_context():
    from decimal import ROUND_DOWN, localcontext

    prompt = parse_request("math root value=2 degree=2")
    expected = calculate(prompt).to_dict()
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        assert calculate(prompt).to_dict() == expected
