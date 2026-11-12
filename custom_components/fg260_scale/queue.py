"""Persistent queue of unassigned readings, plus each person's recent results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
QUEUE_CAP = 50
HISTORY_CAP = 50


@dataclass(frozen=True)
class PendingReading:
    """A weigh-in waiting to be assigned.

    ``impedance`` is as the scale sent it: a raw ADC needing the vendor
    correction when ``adc_mode`` is "fitdays", else ohms. ``formula`` is the
    body-fat formula number the scale asked for (WLA03 = 3), or None.
    Stored readings from before these fields came from the FG260, hence the
    defaults.
    """

    id: str
    timestamp: datetime
    weight_kg: float
    impedance: float | None
    formula: int | None = 3
    adc_mode: str = "fitdays"
    scale_metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "weight_kg": self.weight_kg,
            "impedance": self.impedance,
            "formula": self.formula,
            "adc_mode": self.adc_mode,
            "scale_metrics": self.scale_metrics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingReading:
        return cls(
            id=data["id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            weight_kg=float(data["weight_kg"]),
            impedance=data.get("impedance"),
            formula=data.get("formula", 3),
            adc_mode=data.get("adc_mode", "fitdays"),
            scale_metrics=dict(data.get("scale_metrics") or {}),
        )


class ReadingQueue:
    """Unassigned readings (capped, oldest dropped first) and results history.

    Everything lives in one ``Store`` keyed ``fg260_scale.<entry_id>``. Each
    person's results are plain JSON dicts kept oldest first, capped at
    ``HISTORY_CAP``; the manager owns their format (``timestamp`` and
    ``reading_id`` keys are relied on here).
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self._items: list[PendingReading] = []
        self._history: dict[str, list[dict[str, Any]]] = {}

    async def load(self) -> None:
        """Load the queue and results; older stores kept only a last result."""
        data = await self._store.async_load() or {}
        self._items = [PendingReading.from_dict(r) for r in data.get("queue", [])]
        if "history" in data:
            self._history = {k: list(v) for k, v in data["history"].items()}
        else:
            self._history = {k: [v] for k, v in data.get("last", {}).items()}

    async def _save(self) -> None:
        await self._store.async_save(
            {"queue": [r.as_dict() for r in self._items], "history": self._history}
        )

    async def add(self, reading: PendingReading) -> list[PendingReading]:
        """Queue a reading; return any readings dropped to stay within the cap."""
        self._items.append(reading)
        dropped: list[PendingReading] = []
        while len(self._items) > QUEUE_CAP:
            dropped.append(self._items.pop(0))
        await self._save()
        return dropped

    async def pop(self, reading_id: str) -> PendingReading | None:
        """Remove and return a queued reading, or None if it isn't queued."""
        for index, reading in enumerate(self._items):
            if reading.id == reading_id:
                del self._items[index]
                await self._save()
                return reading
        return None

    def get(self, reading_id: str) -> PendingReading | None:
        """Return a queued reading without removing it."""
        return next((r for r in self._items if r.id == reading_id), None)

    def items(self) -> list[PendingReading]:
        """Return the queued readings, oldest first."""
        return list(self._items)

    def history(self, person_id: str) -> list[dict[str, Any]]:
        """A person's stored results, oldest first."""
        return list(self._history.get(person_id, []))

    async def add_result(self, person_id: str, data: dict[str, Any]) -> None:
        """Store a result in time order, dropping the oldest beyond the cap."""
        results = self._history.setdefault(person_id, [])
        results.append(data)
        results.sort(key=lambda r: datetime.fromisoformat(r["timestamp"]))
        del results[:-HISTORY_CAP]
        await self._save()

    async def set_history(self, person_id: str, results: list[dict[str, Any]]) -> None:
        """Replace a person's results (e.g. recalculated)."""
        self._history[person_id] = list(results)
        await self._save()

    def find_result(self, reading_id: str) -> tuple[str, dict[str, Any]] | None:
        """Find which person holds a reading, and its result."""
        for person_id, results in self._history.items():
            for data in results:
                if data.get("reading_id") == reading_id:
                    return person_id, data
        return None

    async def remove_result(self, reading_id: str) -> tuple[str, dict[str, Any]] | None:
        """Remove a reading from whoever holds it; return (person, result)."""
        if (found := self.find_result(reading_id)) is None:
            return None
        person_id, data = found
        self._history[person_id].remove(data)
        await self._save()
        return found

    async def async_remove(self) -> None:
        """Delete the stored data (entry removed)."""
        await self._store.async_remove()
