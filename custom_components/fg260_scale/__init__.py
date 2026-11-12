"""Broadcast Body Scale integration (domain fg260_scale)."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .manager import (
    ACTION_ASSIGN,
    ACTION_DISCARD,
    ACTION_MOVE,
    ACTION_UNASSIGN,
    ScaleManager,
)
from .queue import ReadingQueue
from .sensor import person_device_identifier, scale_device_info

_LOGGER = logging.getLogger(__name__)

type FG260ConfigEntry = ConfigEntry[ScaleManager]

PLATFORMS = [Platform.NUMBER, Platform.SELECT, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

EVENT_NOTIFICATION_ACTION = "mobile_app_notification_action"
SERVICE_ASSIGN = "assign_reading"
SERVICE_DISCARD = "discard_reading"
ATTR_READING_ID = "reading_id"
ATTR_PERSON = "person"

ASSIGN_SCHEMA = vol.Schema(
    {vol.Required(ATTR_READING_ID): cv.string, vol.Required(ATTR_PERSON): cv.string}
)
DISCARD_SCHEMA = vol.Schema({vol.Required(ATTR_READING_ID): cv.string})


def _manager_for_reading(hass: HomeAssistant, reading_id: str) -> ScaleManager:
    """Find the loaded scale that holds this reading (queued or assigned)."""
    entry: FG260ConfigEntry
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.runtime_data.has_reading(reading_id):
            return entry.runtime_data
    raise ServiceValidationError(f"Unknown reading: {reading_id}")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services and the notification-action listener once."""

    async def _assign(call: ServiceCall) -> None:
        reading_id = call.data[ATTR_READING_ID]
        manager = _manager_for_reading(hass, reading_id)
        await manager.async_assign(reading_id, call.data[ATTR_PERSON])

    async def _discard(call: ServiceCall) -> None:
        reading_id = call.data[ATTR_READING_ID]
        manager = _manager_for_reading(hass, reading_id)
        await manager.async_discard(reading_id)

    hass.services.async_register(DOMAIN, SERVICE_ASSIGN, _assign, ASSIGN_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DISCARD, _discard, DISCARD_SCHEMA)

    async def _on_action(event: Event) -> None:
        action = event.data.get("action")
        if not isinstance(action, str) or not action.startswith("FG260_"):
            return
        parts = action.split("|")
        try:
            if parts[0] == ACTION_ASSIGN and len(parts) == 3:
                manager = _manager_for_reading(hass, parts[1])
                # First answer wins: a stale "who was it?" tap never moves
                # a reading someone already claimed.
                if manager.queue.get(parts[1]) is None:
                    raise ServiceValidationError("Reading already assigned")
                await manager.async_assign(parts[1], parts[2])
            elif parts[0] == ACTION_DISCARD and len(parts) == 2:
                manager = _manager_for_reading(hass, parts[1])
                await manager.async_discard(parts[1])
            elif parts[0] == ACTION_MOVE and len(parts) == 3:
                manager = _manager_for_reading(hass, parts[1])
                await manager.async_move(parts[1], parts[2])
            elif parts[0] == ACTION_UNASSIGN and len(parts) == 2:
                manager = _manager_for_reading(hass, parts[1])
                await manager.async_unassign(parts[1])
            else:
                _LOGGER.warning("Ignoring unknown notification action %s", action)
        except ServiceValidationError as err:
            _LOGGER.warning("Could not handle notification action %s: %s", action, err)

    hass.bus.async_listen(EVENT_NOTIFICATION_ACTION, _on_action)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FG260ConfigEntry) -> bool:
    """Set up a scale from a config entry."""
    manager = ScaleManager(hass, entry)
    await manager.async_start()
    entry.runtime_data = manager

    # The scale's own device must exist before person devices point at it.
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id, **scale_device_info(entry)
    )
    # Drop devices of people who were removed in the options.
    wanted = {person_device_identifier(entry.entry_id, pid) for pid in manager.people}
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        idents = {i for i in device.identifiers if i[0] == DOMAIN}
        if idents and not idents & wanted and (
            DOMAIN, entry.unique_id or entry.entry_id
        ) not in idents:
            devices.async_remove_device(device.id)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: FG260ConfigEntry) -> None:
    """Reload so added or removed people get (or lose) their devices."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: FG260ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored queue and results when the scale is removed."""
    await ReadingQueue(hass, entry.entry_id).async_remove()
