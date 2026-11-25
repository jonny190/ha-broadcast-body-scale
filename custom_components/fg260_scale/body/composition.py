"""Body composition estimated from weight, height and impedance.

Built only from published equations, so any scale that reports an impedance
gets an estimate. These are population equations from whole-body
(hand-to-foot) studies, while bathroom scales measure foot to foot, so treat
the results as consistent trends rather than clinical values. They will not
match the scale vendor's own app.

Sources:

* Fat-free mass (FFM): Sun SS, Chumlea WC, Heymsfield SB, et al.
  "Development of bioelectrical impedance analysis prediction equations for
  body composition with the use of a multicomponent model for use in
  epidemiologic surveys." Am J Clin Nutr 2003;77:331-340.
* Skeletal muscle mass: Janssen I, Heymsfield SB, Baumgartner RN, Ross R.
  "Estimation of skeletal muscle mass by bioelectrical impedance analysis."
  J Appl Physiol 2000;89:465-471.
* Water, protein and bone mineral as fixed shares of FFM: the reference body
  of Brozek J, Grande F, Anderson JT, Keys A. "Densitometric analysis of body
  composition: revision of some quantitative assumptions." Ann N Y Acad Sci
  1963;110:113-140.
* Basal metabolic rate: the Katch-McArdle equation, 370 + 21.6 x FFM
  (Katch FI, McArdle WD, Nutrition, Weight Control and Exercise, 1977).

Visceral fat, subcutaneous fat and body age have no comparable published
equation for these inputs, so they are left out (None).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Identifies this method in stored results, so results are recalculated when
# it changes.
METHOD = "published-v1"

# Sun et al. 2003: FFM = a + b * H^2/R + c * W + d * R  (H cm, R ohm, W kg).
_SUN_2003 = {
    "male": (-10.68, 0.65, 0.26, 0.02),
    "female": (-9.53, 0.69, 0.17, 0.02),
}

# Janssen et al. 2000: SM = 0.401 * H^2/R + 3.825 * male - 0.071 * age + 5.102.
_JANSSEN_HT2R = 0.401
_JANSSEN_MALE = 3.825
_JANSSEN_AGE = -0.071
_JANSSEN_CONST = 5.102

# Brozek et al. 1963 reference body: shares of fat-free mass.
_FFM_WATER = 0.738
_FFM_PROTEIN = 0.194
_FFM_BONE_MINERAL = 0.056

# Katch-McArdle.
_BMR_CONST = 370.0
_BMR_PER_KG_FFM = 21.6

# Outside these, the inputs are not a plausible barefoot measurement.
MIN_IMPEDANCE = 150.0
MAX_IMPEDANCE = 1500.0
_BODY_FAT_RANGE = (2.0, 75.0)


@dataclass(frozen=True)
class BodyComposition:
    """Body composition values. Percentages are of body weight, 0-100.

    Values a method cannot provide are None. ``muscle`` and ``bone`` are kg.
    """

    bmi: float
    body_fat: float | None = None
    subcutaneous_fat: float | None = None
    visceral_fat: float | None = None
    muscle: float | None = None
    bmr: float | None = None
    bone: float | None = None
    water: float | None = None
    body_age: float | None = None
    protein: float | None = None
    skeletal_muscle: float | None = None


def bmi(weight_kg: float, height_cm: float) -> float:
    """Body mass index, one decimal."""
    metres = height_cm / 100
    return round(weight_kg / (metres * metres), 1)


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1)


def calculate(
    weight_kg: float,
    height_cm: float,
    age: int,
    sex: Literal["male", "female"],
    impedance: float | None,
) -> tuple[float, BodyComposition | None]:
    """Return ``(bmi, composition)``.

    ``composition`` is None without a plausible impedance (for example when
    the person wore socks), or when the estimate falls outside a plausible
    range.
    """
    body_mass_index = bmi(weight_kg, height_cm)
    if impedance is None or not MIN_IMPEDANCE <= impedance <= MAX_IMPEDANCE:
        return body_mass_index, None

    ht2r = height_cm * height_cm / impedance
    a, b, c, d = _SUN_2003[sex]
    ffm = a + b * ht2r + c * weight_kg + d * impedance
    body_fat = (weight_kg - ffm) / weight_kg * 100
    if not _BODY_FAT_RANGE[0] <= body_fat <= _BODY_FAT_RANGE[1]:
        return body_mass_index, None

    skeletal_muscle = (
        _JANSSEN_HT2R * ht2r
        + (_JANSSEN_MALE if sex == "male" else 0.0)
        + _JANSSEN_AGE * age
        + _JANSSEN_CONST
    )
    bone = ffm * _FFM_BONE_MINERAL
    return body_mass_index, BodyComposition(
        bmi=body_mass_index,
        body_fat=round(body_fat, 1),
        muscle=round(ffm - bone, 1),
        bmr=round(_BMR_CONST + _BMR_PER_KG_FFM * ffm),
        bone=round(bone, 1),
        water=_pct(ffm * _FFM_WATER, weight_kg),
        protein=_pct(ffm * _FFM_PROTEIN, weight_kg),
        skeletal_muscle=_pct(max(skeletal_muscle, 0.0), weight_kg),
    )
