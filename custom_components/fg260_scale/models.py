"""People model for the Broadcast Body Scale integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal


@dataclass
class Person:
    """A person who can be matched to scale readings."""

    id: str
    name: str
    height_cm: int
    sex: Literal["male", "female"]
    birth_date: date
    notify_services: list[str] = field(default_factory=list)


def age_on(birth_date: date, on: date) -> int:
    """Return whole years between birth_date and the given date."""
    years = on.year - birth_date.year
    if (on.month, on.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def people_from_options(options: Mapping[str, Any]) -> dict[str, Person]:
    """Build Person objects, keyed by id, from a config entry's options."""
    people: dict[str, Person] = {}
    for raw in options.get("people", []):
        person = Person(
            id=raw["id"],
            name=raw["name"],
            height_cm=int(raw["height_cm"]),
            sex=raw["sex"],
            birth_date=date.fromisoformat(raw["birth_date"]),
            notify_services=list(raw.get("notify_services", [])),
        )
        people[person.id] = person
    return people
