"""Tests for the per-person sensors and the unassigned readings sensor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.fg260_scale.const import DOMAIN

from .test_manager import (  # noqa: F401
    ADDRESS,
    SAM,
    ENTRY_ID,
    CHRIS,
    SESSION_2,
    SESSION_3,
    _adverts,
    _expected_comp,
    _feed,
    _last,
    _queue_session_2,
    _store,
    bt_register,
    notify,
    setup_entry,
)

ENABLED = {"weight", "weight_lb", "weight_st", "body_fat", "bmi", "last_measured"}
ALL_KEYS = ENABLED | {
    "impedance",
    "water",
    "muscle",
    "bone",
    "visceral_fat",
    "bmr",
    "body_age",
    "protein",
    "subcutaneous_fat",
    "skeletal_muscle",
}
EARLIER = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)


def _uid(person: str, key: str) -> str:
    return f"{ENTRY_ID}_{person}_{key}"


def _entity_id(hass: HomeAssistant, person: str, key: str) -> str:
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("sensor", DOMAIN, _uid(person, key))
    assert entity_id is not None, key
    return entity_id


def _enable_all(hass: HomeAssistant, person: str) -> None:
    reg = er.async_get(hass)
    for key in ALL_KEYS - ENABLED:
        reg.async_update_entity(_entity_id(hass, person, key), disabled_by=None)


async def test_sensors_created_with_defaults(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    entry = await setup_entry()
    reg = er.async_get(hass)
    for person in ("chris", "sam"):
        for key in ALL_KEYS:
            entity = reg.async_get(_entity_id(hass, person, key))
            assert entity.disabled == (key not in ENABLED), (person, key)
    unassigned = reg.async_get_entity_id(
        "sensor", DOMAIN, f"{ENTRY_ID}_unassigned_readings"
    )
    assert unassigned is not None

    devices = dr.async_get(hass)
    scale = devices.async_get_device_by_identifier((DOMAIN, ADDRESS), ENTRY_ID)
    assert scale is not None and scale.name == "FG260 Body Scale"
    chris = devices.async_get_device_by_identifier((DOMAIN, f"{ENTRY_ID}_chris"), ENTRY_ID)
    assert chris is not None and chris.name == "Chris"
    assert chris.via_device_id == scale.id
    assert entry.state is ConfigEntryState.LOADED


async def test_values_after_assignment(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry: Any,
    bt_register: Any,
) -> None:
    _store(
        hass_storage,
        {"queue": [], "last": {"chris": _last(115.0, EARLIER), "sam": _last(70.0, EARLIER)}},
    )
    entry = await setup_entry()
    await _feed(hass, freezer, bt_register, SESSION_2)

    when = _adverts()[14][0]
    bmi, comp = _expected_comp(CHRIS, 115.05, 525, when)
    assert comp is not None
    state = hass.states.get(_entity_id(hass, "chris", "weight"))
    assert state.state == "115.05"
    assert state.attributes["unit_of_measurement"] == "kg"
    assert state.attributes["device_class"] == "weight"
    assert float(hass.states.get(_entity_id(hass, "chris", "body_fat")).state) == pytest.approx(comp.body_fat)
    assert float(hass.states.get(_entity_id(hass, "chris", "bmi")).state) == pytest.approx(bmi)
    last = hass.states.get(_entity_id(hass, "chris", "last_measured"))
    assert datetime.fromisoformat(last.state) == when.replace(microsecond=0)
    lb = hass.states.get(_entity_id(hass, "chris", "weight_lb"))
    assert lb.state == "253.64"
    assert lb.attributes["unit_of_measurement"] == "lb"
    assert lb.attributes["device_class"] == "weight"
    assert lb.attributes["state_class"] == "measurement"
    assert hass.states.get(_entity_id(hass, "chris", "weight_st")).state == "18 st 1.6 lb"
    # Sam is untouched
    assert hass.states.get(_entity_id(hass, "sam", "weight")).state == "70.0"
    assert entry.runtime_data.last_result("chris") is not None


async def test_impedance_is_corrected_and_rounded(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry: Any,
    bt_register: Any,
) -> None:
    from custom_components.fg260_scale.body.wla03 import correct_impedance

    _store(hass_storage, {"queue": [], "last": {"chris": _last(115.0, EARLIER)}})
    await setup_entry()
    _enable_all(hass, "chris")
    await hass.config_entries.async_reload(ENTRY_ID)
    await hass.async_block_till_done()
    await _feed(hass, freezer, bt_register, SESSION_2)
    expected = round(correct_impedance(525, 115.05))
    state = hass.states.get(_entity_id(hass, "chris", "impedance"))
    assert state.state == str(expected)
    assert state.attributes["unit_of_measurement"] == "Ω"


async def test_socks_reading_keeps_previous_body_fat(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry: Any,
    bt_register: Any,
) -> None:
    _store(hass_storage, {"queue": [], "last": {"chris": _last(115.0, EARLIER)}})
    await setup_entry()
    _enable_all(hass, "chris")
    await hass.config_entries.async_reload(ENTRY_ID)
    await hass.async_block_till_done()

    await _feed(hass, freezer, bt_register, SESSION_2)
    fat = hass.states.get(_entity_id(hass, "chris", "body_fat")).state
    imp = hass.states.get(_entity_id(hass, "chris", "impedance")).state
    bone = hass.states.get(_entity_id(hass, "chris", "bone")).state
    assert fat not in ("unknown", "unavailable")

    await _feed(hass, freezer, bt_register, SESSION_3)
    freezer.tick(timedelta(seconds=8))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(_entity_id(hass, "chris", "weight")).state == "114.82"
    assert hass.states.get(_entity_id(hass, "chris", "bmi")).state != "unknown"
    assert hass.states.get(_entity_id(hass, "chris", "body_fat")).state == fat
    assert hass.states.get(_entity_id(hass, "chris", "impedance")).state == imp
    assert hass.states.get(_entity_id(hass, "chris", "bone")).state == bone


async def test_state_restored_after_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry: Any,
    bt_register: Any,
) -> None:
    _store(hass_storage, {"queue": [], "last": {"chris": _last(115.0, EARLIER)}})
    await setup_entry()
    await _feed(hass, freezer, bt_register, SESSION_2)
    await _feed(hass, freezer, bt_register, SESSION_3)
    freezer.tick(timedelta(seconds=8))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    fat = hass.states.get(_entity_id(hass, "chris", "body_fat")).state
    assert fat not in ("unknown", "unavailable")

    await hass.config_entries.async_reload(ENTRY_ID)
    await hass.async_block_till_done()
    # the stored last result is a socks reading without composition: the
    # restored state must survive the reload.
    assert hass.states.get(_entity_id(hass, "chris", "body_fat")).state == fat
    assert hass.states.get(_entity_id(hass, "chris", "weight")).state == "114.82"


async def test_unassigned_sensor_counts_queue(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry: Any,
    bt_register: Any,
) -> None:
    entry = await setup_entry()
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id(
        "sensor", DOMAIN, f"{ENTRY_ID}_unassigned_readings"
    )
    assert hass.states.get(entity_id).state == "0"
    reading = await _queue_session_2(hass, freezer, bt_register, entry)
    state = hass.states.get(entity_id)
    assert state.state == "1"
    assert state.attributes["readings"] == [
        {
            "id": reading.id,
            "timestamp": reading.timestamp.isoformat(),
            "weight_kg": 115.05,
        }
    ]
    await entry.runtime_data.async_discard(reading.id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "0"
    assert hass.states.get(entity_id).attributes["readings"] == []


async def test_removed_person_device_and_entities_are_removed(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    entry = await setup_entry()
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "people": [dict(CHRIS)]}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    devices = dr.async_get(hass)
    assert devices.async_get_device_by_identifier((DOMAIN, f"{ENTRY_ID}_sam"), ENTRY_ID) is None
    assert devices.async_get_device_by_identifier((DOMAIN, f"{ENTRY_ID}_chris"), ENTRY_ID) is not None
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, _uid("sam", "weight")) is None


async def test_added_person_gets_sensors(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    entry = await setup_entry(people=(CHRIS,))
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, _uid("sam", "weight")) is None
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "people": [dict(CHRIS), dict(SAM)]}
    )
    await hass.async_block_till_done()
    assert reg.async_get_entity_id("sensor", DOMAIN, _uid("sam", "weight")) is not None


async def test_person_devices_avoid_deprecated_via_device(
    hass: HomeAssistant, setup_entry: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """HA 2026.11 rejects DeviceInfo(via_device=...) when entities are re-added
    (e.g. after the device is moved to an area), so link by via_device_id."""
    await setup_entry()
    assert "deprecated `via_device`" not in caplog.text


@pytest.mark.parametrize(
    ("kg", "expected"),
    [
        # Readings shown on the scale's own display in st:lb mode.
        (115.05, "18 st 1.6 lb"),
        (114.82, "18 st 1.1 lb"),
        (115.28, "18 st 2.1 lb"),
        # 265.98 lb rounds to 266.0 lb, which must carry into a whole stone.
        (120.646, "19 st 0.0 lb"),
    ],
)
def test_stone_pounds_format(kg: float, expected: str) -> None:
    from custom_components.fg260_scale.manager import format_stone

    assert format_stone(kg) == expected
