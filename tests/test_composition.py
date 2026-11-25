"""Body composition from published equations (body/composition.py).

Expected values are worked by hand from the papers' coefficients.
"""

from __future__ import annotations

import pytest

from custom_components.fg260_scale.body.composition import bmi, calculate
from custom_components.fg260_scale.parser import a6_impedance_ohms


def test_bmi() -> None:
    assert bmi(80.0, 180) == 24.7
    assert bmi(60.0, 165) == 22.0


def test_male_worked_example() -> None:
    # 80 kg, 180 cm, 40 years, 500 ohm. H^2/R = 32400 / 500 = 64.8.
    # Sun 2003 (men): FFM = -10.68 + 0.65*64.8 + 0.26*80 + 0.02*500 = 62.24 kg
    # Janssen 2000: SM = 0.401*64.8 + 3.825 - 0.071*40 + 5.102 = 32.0718 kg
    bmi_value, comp = calculate(80.0, 180, 40, "male", 500)
    ffm = 62.24
    assert bmi_value == comp.bmi == 24.7
    assert comp.body_fat == round((80 - ffm) / 80 * 100, 1) == 22.2
    assert comp.bmr == round(370 + 21.6 * ffm) == 1714
    assert comp.water == round(ffm * 0.738 / 80 * 100, 1) == 57.4
    assert comp.protein == round(ffm * 0.194 / 80 * 100, 1) == 15.1
    assert comp.bone == round(ffm * 0.056, 1) == 3.5
    assert comp.muscle == round(ffm * (1 - 0.056), 1) == 58.8
    assert comp.skeletal_muscle == round(32.0718 / 80 * 100, 1) == 40.1


def test_female_worked_example() -> None:
    # 62 kg, 165 cm, 35 years, 600 ohm. H^2/R = 27225 / 600 = 45.375.
    # Sun 2003 (women): FFM = -9.53 + 0.69*45.375 + 0.17*62 + 0.02*600
    #                       = 44.31875 kg
    # Janssen 2000: SM = 0.401*45.375 - 0.071*35 + 5.102 = 20.812375 kg
    _, comp = calculate(62.0, 165, 35, "female", 600)
    ffm = 44.31875
    assert comp.body_fat == round((62 - ffm) / 62 * 100, 1) == 28.5
    assert comp.bmr == round(370 + 21.6 * ffm) == 1327
    assert comp.skeletal_muscle == round(20.812375 / 62 * 100, 1) == 33.6


def test_values_without_a_published_equation_are_none() -> None:
    _, comp = calculate(80.0, 180, 40, "male", 500)
    assert (comp.visceral_fat, comp.subcutaneous_fat, comp.body_age) == (None,) * 3


@pytest.mark.parametrize("impedance", [None, 0, 149.9, 1500.1])
def test_no_plausible_impedance_gives_bmi_only(impedance) -> None:
    assert calculate(80.0, 180, 40, "male", impedance) == (24.7, None)


def test_implausible_estimate_gives_bmi_only() -> None:
    # A very low impedance for a light person puts FFM above body weight.
    assert calculate(40.0, 200, 40, "male", 150)[1] is None


def test_a6_impedance() -> None:
    assert a6_impedance_ohms(525, 115.05) == 525
    assert a6_impedance_ohms(5250, 115.05) == pytest.approx(
        ((5250 - 1000) - 115.05 * 4) / 0.6 / 10
    )
