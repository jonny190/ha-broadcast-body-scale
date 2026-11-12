"""Tests for the WLA03 body composition port, against native-generated vectors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.fg260_scale.body.wla03 import (
    BodyComposition,
    calculate,
    correct_impedance,
)

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "wla03_vectors.json").read_text()
)
FIELDS = (
    "body_fat", "subcutaneous_fat", "visceral_fat", "muscle", "bmr",
    "bone", "water", "body_age", "protein", "skeletal_muscle",
)


def _args(vec: dict, impedance="vec") -> tuple:
    i = vec["in"]
    imp = i["impedance"] if impedance == "vec" else impedance
    return (i["weight_kg"], i["height_cm"], i["age"], i["sex"], i["athlete"], imp)


def test_vectors_present() -> None:
    assert len(VECTORS) >= 6000


def test_vectors_match() -> None:
    for vec in VECTORS:
        out = vec["out"]
        bmi, comp = calculate(*_args(vec))
        assert bmi == pytest.approx(out["bmi"], abs=0.05), vec["in"]
        assert round(bmi, 1) == round(out["bmi"], 1), vec["in"]
        if vec["in"]["impedance"] < 10:
            assert comp is None, vec["in"]
            continue
        assert isinstance(comp, BodyComposition)
        assert comp.bmi == pytest.approx(out["bmi"], abs=0.05)
        for field in FIELDS:
            got, exp = getattr(comp, field), out[field]
            assert round(got, 1) == round(exp, 1), (field, vec["in"], got, exp)
            assert got == pytest.approx(exp, abs=0.05), (field, vec["in"])


def test_vectors_exact() -> None:
    """The port is bit-exact with native, so hold it to that."""
    for vec in VECTORS:
        if vec["in"]["impedance"] < 10:
            continue
        _, comp = calculate(*_args(vec))
        for field in FIELDS:
            assert getattr(comp, field) == vec["out"][field], (field, vec["in"])


@pytest.mark.parametrize("impedance", [None, 0, -5.0])
def test_no_impedance_gives_bmi_only(impedance) -> None:
    for vec in VECTORS[:50]:
        bmi, comp = calculate(*_args(vec, impedance))
        assert bmi == vec["out"]["bmi"]
        assert comp is None


def test_low_impedance_is_error_path() -> None:
    bmi, comp = calculate(70.0, 175, 30, "male", False, 9.99)
    assert comp is None
    assert bmi == pytest.approx(22.9, abs=0.05)


def test_correct_impedance() -> None:
    assert correct_impedance(525, 115.05) == 525
    assert correct_impedance(5250, 115.05) == pytest.approx(
        ((5250 - 1000) + 1150.5 * (-0.4)) / 0.6 / 10
    )


def test_correct_impedance_against_raw_adc_vectors() -> None:
    raw = [v for v in VECTORS if "raw_adc" in v]
    assert len(raw) >= 300
    for vec in raw:
        got = correct_impedance(vec["raw_adc"], vec["in"]["weight_kg"])
        assert got == pytest.approx(vec["in"]["impedance"], rel=1e-12, abs=1e-12)
