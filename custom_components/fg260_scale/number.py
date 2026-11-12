"""Per-person height, editable from the person's device page."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MAX_HEIGHT_CM, MIN_HEIGHT_CM
from .manager import ScaleManager
from .models import Person
from .sensor import async_scale_device_id, person_device_info

if TYPE_CHECKING:
    from . import FG260ConfigEntry

PARALLEL_UPDATES = 0



async def async_setup_entry(
    hass: HomeAssistant,
    entry: FG260ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a height entity for each person."""
    manager = entry.runtime_data
    scale_device_id = async_scale_device_id(hass, entry)
    async_add_entities(
        HeightNumber(manager, person, scale_device_id)
        for person in manager.people.values()
    )


class HeightNumber(NumberEntity):
    """A person's height. Changing it recalculates their latest results."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "height"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_HEIGHT_CM
    _attr_native_max_value = MAX_HEIGHT_CM
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfLength.CENTIMETERS

    def __init__(
        self, manager: ScaleManager, person: Person, scale_device_id: str
    ) -> None:
        self._manager = manager
        self._person_id = person.id
        entry_id = manager.entry.entry_id
        self._attr_unique_id = f"{entry_id}_{person.id}_height"
        self._attr_device_info = person_device_info(entry_id, person, scale_device_id)
        self._attr_native_value = person.height_cm

    async def async_set_native_value(self, value: float) -> None:
        """Save the new height; the integration reloads and recalculates."""
        await self._manager.async_set_height(self._person_id, round(value))
