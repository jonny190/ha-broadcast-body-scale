"""Tests for the per-person height number entity and recalculation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.fg260_scale.const import DOMAIN

from .test_manager import (  # noqa: F401
    CHRIS,
    ENTRY_ID,
    SESSION_2,
    _adverts,
    _expected_comp,
    _feed,
    _last,
    _store,
    bt_register,
    notify,
    setup_entry,
)

EARLIER = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)


def _eid(hass: HomeAssistant, platform: str, person: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{ENTRY_ID}_{person}_{key}"
    )
    assert entity_id is not None, key
    return entity_id


async def test_height_entity_shows_profile_height(
    hass: HomeAssistant, setup_entry: Any
) -> None:
    await setup_entry()
    entity_id = _eid(hass, "number", "chris", "height")
    state = hass.states.get(entity_id)
    assert float(state.state) == 180
    assert state.attributes["unit_of_measurement"] == "cm"
    assert state.attributes["min"] == 100
    assert state.attributes["max"] == 250
    assert state.attributes["step"] == 1
    entry = er.async_get(hass).async_get(entity_id)
    assert entry.entity_category == er.EntityCategory.CONFIG


async def test_setting_height_saves_profile_and_recalculates(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry: Any,
    bt_register: Any,
) -> None:
    _store(hass_storage, {"queue": [], "last": {"chris": _last(115.0, EARLIER)}})
    entry = await setup_entry(people=(CHRIS,))
    await _feed(hass, freezer, bt_register, SESSION_2)
    when = _adverts()[14][0]
    _, old = _expected_comp(CHRIS, 115.05, 525, when)
    fat = _eid(hass, "sensor", "chris", "body_fat")
    assert float(hass.states.get(fat).state) == pytest.approx(old.body_fat)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _eid(hass, "number", "chris", "height"), "value": 170},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert entry.options["people"][0]["height_cm"] == 170
    bmi, new = _expected_comp({**CHRIS, "height_cm": 170}, 115.05, 525, when)
    assert new.body_fat != pytest.approx(old.body_fat)
    assert float(hass.states.get(fat).state) == pytest.approx(new.body_fat)
    bmi_state = hass.states.get(_eid(hass, "sensor", "chris", "bmi"))
    assert float(bmi_state.state) == pytest.approx(bmi)
    assert float(hass.states.get(_eid(hass, "number", "chris", "height")).state) == 170
    # Weight and time of the reading are unchanged.
    assert hass.states.get(_eid(hass, "sensor", "chris", "weight")).state == "115.05"


async def test_result_without_recorded_profile_is_recalculated_at_start(
    hass: HomeAssistant, hass_storage: dict, setup_entry: Any
) -> None:
    """Results from before the current method are recalculated on startup."""
    legacy = {**_last(115.05, EARLIER), "impedance": 525}
    _store(hass_storage, {"queue": [], "last": {"chris": legacy}})
    entry = await setup_entry(people=(CHRIS,))
    bmi, comp = _expected_comp(CHRIS, 115.05, 525, EARLIER)
    result = entry.runtime_data.last_result("chris")
    assert (result.bmi, result.composition) == (bmi, comp)
    assert result.profile["method"] == "published-v1"


async def test_setting_height_recalculates_result_without_recorded_profile(
    hass: HomeAssistant, hass_storage: dict, setup_entry: Any
) -> None:
    """A result stored before profiles were recorded is still recalculated."""
    legacy = {**_last(115.05, EARLIER), "impedance": 525}
    _store(hass_storage, {"queue": [], "last": {"chris": legacy}})
    entry = await setup_entry(people=(CHRIS,))

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _eid(hass, "number", "chris", "height"), "value": 170},
        blocking=True,
    )
    await hass.async_block_till_done()

    bmi, comp = _expected_comp({**CHRIS, "height_cm": 170}, 115.05, 525, EARLIER)
    result = entry.runtime_data.last_result("chris")
    assert result.bmi == pytest.approx(bmi)
    assert result.composition.body_fat == pytest.approx(comp.body_fat)
    assert result.timestamp == EARLIER
