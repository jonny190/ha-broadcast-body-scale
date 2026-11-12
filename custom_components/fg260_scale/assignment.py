"""Decide who stepped on the scale. Pure logic with no Home Assistant imports."""

from __future__ import annotations

from collections.abc import Mapping


def choose_person(
    weight_kg: float,
    last_weights: Mapping[str, float | None],
    tolerance_kg: float,
) -> str | None:
    """Return the only person whose last weight is within tolerance, else None.

    A person with no history (``None``) is never chosen.
    """
    matches = [
        person_id
        for person_id, last in last_weights.items()
        if last is not None and abs(weight_kg - last) <= tolerance_kg
    ]
    return matches[0] if len(matches) == 1 else None
