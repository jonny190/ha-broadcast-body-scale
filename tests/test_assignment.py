"""Tests for choosing who weighed in."""

from __future__ import annotations

from custom_components.fg260_scale.assignment import choose_person


def test_choose_person() -> None:
    last = {"chris": 115.0, "sam": 70.0}
    assert choose_person(115.05, last, 3.0) == "chris"
    assert choose_person(71.5, last, 3.0) == "sam"
    # within tolerance of both -> ambiguous
    assert choose_person(72.0, {"a": 70.0, "b": 73.0}, 3.0) is None
    # nobody in range
    assert choose_person(90.0, last, 3.0) is None
    # someone with no history is never chosen
    assert choose_person(115.0, {"chris": None}, 3.0) is None
    assert choose_person(115.0, {"chris": 115.0, "new": None}, 3.0) == "chris"
    # boundary counts as in range
    assert choose_person(118.0, {"chris": 115.0}, 3.0) == "chris"
    assert choose_person(115.0, {}, 3.0) is None
