"""Assign the newest unassigned reading from the scale's device page."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .manager import DISCARD_OPTION, ScaleManager, signal_updated
from .sensor import scale_device_info

if TYPE_CHECKING:
    from . import FG260ConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FG260ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the assign select."""
    async_add_entities([AssignLatestSelect(entry.runtime_data)])


class AssignLatestSelect(SelectEntity):
    """Pick who the newest unassigned reading belongs to.

    It has no lasting choice: picking acts once, so it shows nothing
    selected. It is unavailable while nothing is waiting.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "assign_latest"
    _attr_current_option = None

    def __init__(self, manager: ScaleManager) -> None:
        self._manager = manager
        entry = manager.entry
        self._attr_unique_id = f"{entry.entry_id}_assign_latest"
        self._attr_device_info = scale_device_info(entry)
        self._attr_options = [p.name for p in manager.people.values()] + [
            DISCARD_OPTION
        ]

    @property
    def available(self) -> bool:
        return bool(self._manager.queue.items())

    async def async_select_option(self, option: str) -> None:
        await self._manager.async_assign_latest(option)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_updated(self._manager.entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
