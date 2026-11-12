"""Body composition estimate for the FG260 scale (WLA03 algorithm family).

Pure Python, no third-party dependencies. The arithmetic mirrors the reference
implementation's mix of float32 and float64 values, and its fused
multiply-adds (``math.fma``, Python 3.13+), so results match bit for bit.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Literal

# Impedance below this is rejected by the algorithm (BMI only).
MIN_IMPEDANCE = 10.0

# Impedance correction applied to raw 0xA6 frame values.
ADC_CORRECTION_THRESHOLD = 1500
ADC_OFFSET = 1000
ADC_WEIGHT_FACTOR = -0.4
ADC_DIVISOR = 0.6

# Body fat clamps.
BODY_FAT_MIN = 5.0
BODY_FAT_MIN_REPLACEMENT = 5.1
BODY_FAT_MAX = 70.0

# Body age score thresholds: each one met takes one more year off.
_BODY_AGE_STEPS = (
    50.0, 60.0, 65.0, 67.5, 70.0, 72.5, 75.0, 80.0, 85.0,
    87.5, 90.0, 92.5, 95.0, 96.5, 98.0, 99.0, 101.0,
)
_BODY_AGE_FLOOR = 18
_BODY_AGE_OFFSET = 8

BMR_MAX = 2500.0
VISCERAL_FAT_MAX = 30.0
SUBCUTANEOUS_FAT_MIN = 4.0
WATER_MAX = 80.0
PROTEIN_MIN = 5.0


@dataclass(frozen=True)
class BodyComposition:
    """Derived body composition values. Percentages are 0-100."""

    bmi: float
    body_fat: float
    subcutaneous_fat: float
    visceral_fat: float
    muscle: float
    bmr: float
    bone: float
    water: float
    body_age: float
    protein: float
    skeletal_muscle: float


def _f32(value: float) -> float:
    """Round a double to the nearest float32 and return it as a double."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def correct_impedance(adc: int, weight_kg: float) -> float:
    """Apply the vendor app's raw impedance correction."""
    value: float = adc
    if value >= ADC_CORRECTION_THRESHOLD:
        value = (
            ((value - ADC_OFFSET) + weight_kg * 10 * ADC_WEIGHT_FACTOR)
            / ADC_DIVISOR
            / 10
        )
    if value > ADC_CORRECTION_THRESHOLD:
        value /= 10
    return value


def _round1(x: float) -> float:
    """The reference's one-decimal rounding helper; returns a float32 value."""
    frac = _f32(math.fmod(x, 1.0))
    tenths = _f32(frac * 10.0)
    if math.fmod(tenths, 1.0) > 0.5:
        tenths = _f32(tenths + 1.0)
    scaled = _f32(float(int(tenths)) / 10.0)
    whole = int(x)
    if scaled == 0.0 and _f32(x - float(whole)) > 0.99:
        scaled = 1.0
    return _f32(scaled + _f32(float(whole)))


def _bmi(weight: float, height_cm: int) -> float:
    meters = height_cm / 100.0
    return _round1(weight / (meters * meters))


def _clamp_body_fat(value: float) -> float:
    if value < BODY_FAT_MIN:
        return _f32(BODY_FAT_MIN_REPLACEMENT)
    if value > BODY_FAT_MAX:
        return BODY_FAT_MAX
    return value


def _body_fat(bmi: float, age: int, impedance: float, male: bool, athlete: bool) -> float:
    if male:
        total = math.fma(float(age), 0.103, bmi * 1.524)
        offset = -21.992
    else:
        total = math.fma(float(age), 0.097, bmi * 1.545)
        offset = -12.689
    fat = _clamp_body_fat(_f32((total + offset) - 500.0 / impedance))
    if athlete:
        if male:
            fat = _f32(bmi / 3.0 + fat / 3.5)
        else:
            fat = _f32(bmi / 2.4 + fat / 3.0)
        fat = _clamp_body_fat(fat)
    return fat


def _visceral_fat(weight: float, bmi: float, body_fat: float, male: bool) -> float:
    lean = (body_fat / -100.0 + 1.0) * weight if body_fat > 0.01 else 0.0
    if male:
        value = math.fma(weight - lean, 0.026, math.fma(bmi, 0.8666, body_fat * 0.0082))
        value += -14.2692
    else:
        value = math.fma(weight - lean, -0.0534, math.fma(bmi, 0.8895, body_fat * 0.0943))
        value += -16.215
    if value >= VISCERAL_FAT_MAX:
        value = VISCERAL_FAT_MAX
    return 1.0 if value < 1.0 else value


def _weight_score(weight: float, height_cm: int, male: bool) -> float:
    std = 0.7 * float(height_cm - 80) if male else 0.45 * math.fma(float(height_cm), 1.37, -110.0)
    if std == weight:
        return 100.0
    if weight <= std:
        low = std * 0.7
        if low < weight:
            return math.fma(abs(std - weight), -50.0 / abs(std - low), 100.0)
        if std * 0.6 <= weight:
            return 50.0
        if std * 0.5 <= weight:
            return 40.0
        if weight < std * 0.4:
            if weight < std * 0.3:
                return 0.0 if weight < std * 0.0 else 10.0
            return 20.0
        return 30.0
    high = std * 1.3
    if high < weight:
        return 50.0
    return math.fma(abs(std - weight), -50.0 / abs(std - high), 100.0)


def _fat_score(body_fat: float, male: bool) -> float:
    ideal = 16.0 if male else 26.0
    if ideal == body_fat:
        return 100.0
    if body_fat <= ideal:
        if body_fat > 5.0:
            return math.fma(abs(ideal - body_fat), -50.0 / abs(ideal - 5.0), 100.0)
        return 10.0 if body_fat > 0.0 else 0.0
    if body_fat <= 45.0:
        return math.fma(abs(ideal - body_fat), -50.0 / abs(ideal - 45.0), 100.0)
    return 50.0


def _bmi_score(bmi: float) -> float:
    if bmi == 22.0:
        return 100.0
    if bmi <= 22.0 and (bmi <= 15.0 or 22.0 <= bmi):
        if bmi >= 10.0:
            return 40.0
        if bmi >= 5.0:
            return 30.0
        return 20.0 if bmi >= 0.0 else 0.0
    if bmi > 22.0 and bmi >= 35.0:
        return 50.0
    return math.fma(abs(22.0 - bmi), -3.84615385, 100.0)


def _body_age(
    weight: float, height_cm: int, age: int, bmi: float, body_fat: float, male: bool
) -> int:
    average = (
        _weight_score(weight, height_cm, male)
        + _fat_score(body_fat, male)
        + _bmi_score(bmi)
    ) / 3.0
    offset = 0
    for index, step in enumerate(_BODY_AGE_STEPS):
        if average >= step:
            offset = -(index + 1)
    if age + offset > 10:
        return age + offset + _BODY_AGE_OFFSET
    return _BODY_AGE_FLOOR


def calculate(
    weight_kg: float,
    height_cm: int,
    age: int,
    sex: Literal["male", "female"],
    athlete: bool,
    impedance: float | None,
) -> tuple[float, BodyComposition | None]:
    """Return ``(bmi, composition)``.

    ``composition`` is ``None`` when the impedance is missing, not positive, or
    below the algorithm's minimum (BMI only, as the vendor app reports).
    """
    weight = weight_kg
    male = sex == "male"
    bmi = _bmi(weight, height_cm)
    if impedance is None or impedance <= 0 or impedance < MIN_IMPEDANCE:
        return bmi, None

    fat = _body_fat(bmi, age, impedance, male, athlete)
    visceral = _visceral_fat(weight, bmi, fat, male)
    if male:
        subcutaneous = math.fma(fat, 0.965, visceral * -0.22)
    else:
        subcutaneous = math.fma(fat, 0.983, visceral * -0.303)
    subcutaneous = max(subcutaneous, SUBCUTANEOUS_FAT_MIN)

    factor = fat / -100.0 + 1.0
    lean = weight * factor
    # Always true once impedance >= MIN_IMPEDANCE (fat >= 5.1), kept for fidelity.
    normal = fat > 0.01 and lean >= 0.01
    bone = max(lean * (0.05 if male else 0.06), 1.0) if normal else 0.0
    muscle = (math.fma(weight, factor, -bone) / weight) * 100.0

    energy = math.fma(weight, ((100.0 - fat) * 21.6) / 100.0, 370.0)
    if energy >= BMR_MAX:
        energy = BMR_MAX
    bmr = int(_round1(energy))

    body_age = _body_age(weight, height_cm, age, bmi, fat, male)

    water = 0.0
    skeletal = 0.0
    if normal:
        water = min(((0.76 if male else 0.73) * (lean - bone) / weight) * 100.0, WATER_MAX)
        skeletal = (((0.68 if male else 0.62) * (lean - bone)) / weight) * 100.0
    base = (100.0 - fat) if male else math.fma(fat, -1.05, 100.0)
    protein = max(math.fma(-bone / weight, 100.0, base) - water, PROTEIN_MIN)

    return bmi, BodyComposition(
        bmi=bmi,
        body_fat=fat,
        subcutaneous_fat=subcutaneous,
        visceral_fat=visceral,
        muscle=muscle,
        bmr=float(bmr),
        bone=bone,
        water=water,
        body_age=float(body_age),
        protein=protein,
        skeletal_muscle=skeletal,
    )
