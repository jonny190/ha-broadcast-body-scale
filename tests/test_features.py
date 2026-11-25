"""Tests for undo/move, change since last, scale signal, repairs, diagnostics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.fg260_scale.body.composition import calculate
from custom_components.fg260_scale.manager import compute
from custom_components.fg260_scale.models import Person
from custom_components.fg260_scale.queue import PendingReading


async def test_age_uses_local_date(hass: HomeAssistant) -> None:
    """00:30 BST on a birthday is still 23:30 UTC the day before."""
    await hass.config.async_set_time_zone("Europe/London")
    person = Person(
        id="p", name="P", height_cm=180, sex="male", birth_date=date(1985, 7, 1)
    )
    birthday = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)  # 00:30 on 1 July BST
    day_before = birthday - timedelta(hours=1)  # 23:30 on 30 June BST

    def at_age(age: int) -> Any:
        return calculate(100.0, 180, age, "male", 500.0)

    assert at_age(41) != at_age(40)
    def reading(when: datetime) -> PendingReading:
        return PendingReading(id="r", timestamp=when, weight_kg=100.0, impedance=500)

    assert compute(person, reading(birthday)) == at_age(41)
    assert compute(person, reading(day_before)) == at_age(40)


# -- integration-level helpers ------------------------------------------------

from freezegun.api import FrozenDateTimeFactory  # noqa: E402
from homeassistant.components.diagnostics import REDACTED  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    async_capture_events,
    async_mock_service,
)

from custom_components.fg260_scale.const import DOMAIN  # noqa: E402
from custom_components.fg260_scale.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)

from .test_manager import (  # noqa: E402, F401
    ACTION_EVENT,
    CHRIS,
    ENTRY_ID,
    EVENT,
    SAM,
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

EARLIER = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
ROBIN = {
    "id": "robin",
    "name": "Robin",
    "height_cm": 175,
    "sex": "female",
    "birth_date": "1995-03-01",
    "notify_services": ["notify.chris_phone"],
}


def _eid(hass: HomeAssistant, platform: str, unique: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique)
    assert entity_id is not None, unique
    return entity_id


def _with_comp(weight: float, when: datetime, person: dict = CHRIS) -> dict[str, Any]:
    """A stored barefoot result, with the composition our code would compute."""
    bmi, comp = _expected_comp(person, weight, 520, when)
    from dataclasses import asdict

    return {
        "reading_id": f"old_{weight}",
        "timestamp": when.isoformat(),
        "weight_kg": weight,
        "bmi": bmi,
        "impedance": 520,
        "composition": asdict(comp),
        "profile": None,
    }


async def _auto_assign_session_2(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry: Any,
    bt_register: Any,
    **options: Any,
) -> Any:
    """Chris (last 115.0) and Sam (last 70.0); session 2 auto-assigns to Chris."""
    _store(
        hass_storage,
        {
            "queue": [],
            "history": {
                "chris": [_with_comp(115.0, EARLIER)],
                "sam": [_last(70.0, EARLIER)],
            },
        },
    )
    entry = await setup_entry(**options)
    await _feed(hass, freezer, bt_register, SESSION_2)
    return entry


def _reading_id(entry: Any, person: str) -> str:
    rid = entry.runtime_data.last_result(person).reading_id
    assert rid
    return rid


# -- 1. confirmation, move and undo -------------------------------------------


async def test_auto_assign_confirms_with_move_and_undo_buttons(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any, notify: Any,
) -> None:
    entry = await _auto_assign_session_2(
        hass, hass_storage, freezer, setup_entry, bt_register
    )
    rid = _reading_id(entry, "chris")
    assert notify["sam_phone"] == []
    (call,) = notify["chris_phone"]
    when = _adverts()[14][0]
    from homeassistant.util import dt as dt_util

    hhmm = dt_util.as_local(when).strftime("%H:%M")
    since = dt_util.as_local(EARLIER).strftime("%-d %b")
    assert call.data["title"] == "Saved to Chris"
    # +0.05 kg = +0.1 lb
    assert call.data["message"] == (
        f"18 st 1.6 lb (115.05 kg) at {hhmm}, +0.1 lb since {since}"
    )
    assert call.data["data"] == {
        "tag": f"fg260_{rid}",
        "actions": [
            {"action": f"FG260_MOVE|{rid}|sam", "title": "It was Sam"},
            {"action": f"FG260_UNASSIGN|{rid}", "title": "Not me"},
        ],
    }


async def test_confirmation_can_be_turned_off(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any, notify: Any,
) -> None:
    entry = await _auto_assign_session_2(
        hass, hass_storage, freezer, setup_entry, bt_register,
        confirm_auto_assign=False,
    )
    assert entry.runtime_data.last_result("chris").weight_kg == 115.05
    assert notify["chris_phone"] == []


async def test_move_action_moves_reading_to_other_person(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any, notify: Any,
) -> None:
    entry = await _auto_assign_session_2(
        hass, hass_storage, freezer, setup_entry, bt_register
    )
    manager = entry.runtime_data
    rid = _reading_id(entry, "chris")
    events = async_capture_events(hass, EVENT)
    for calls in notify.values():
        calls.clear()

    hass.bus.async_fire(ACTION_EVENT, {"action": f"FG260_MOVE|{rid}|sam"})
    await hass.async_block_till_done()

    # Chris is back to his earlier reading; Sam has the moved one,
    # recalculated with Sam's profile.
    assert manager.last_result("chris").weight_kg == 115.0
    moved = manager.last_result("sam")
    when = _adverts()[14][0]
    bmi, comp = _expected_comp(SAM, 115.05, 525, when)
    assert (moved.weight_kg, moved.reading_id, moved.bmi) == (115.05, rid, bmi)
    assert moved.composition == comp
    assert [e.data["person_id"] for e in events] == ["sam"]
    assert notify["chris_phone"][-1].data["message"] == "clear_notification"
    weight = hass.states.get(_eid(hass, "sensor", f"{ENTRY_ID}_chris_weight"))
    assert weight.state == "115.0"


async def test_not_me_puts_reading_back_and_asks(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any, notify: Any,
) -> None:
    entry = await _auto_assign_session_2(
        hass, hass_storage, freezer, setup_entry, bt_register
    )
    manager = entry.runtime_data
    rid = _reading_id(entry, "chris")
    fat = _eid(hass, "sensor", f"{ENTRY_ID}_chris_body_fat")
    old_fat = _with_comp(115.0, EARLIER)["composition"]["body_fat"]
    assert float(hass.states.get(fat).state) != pytest.approx(old_fat)
    for calls in notify.values():
        calls.clear()

    hass.bus.async_fire(ACTION_EVENT, {"action": f"FG260_UNASSIGN|{rid}"})
    await hass.async_block_till_done()

    assert manager.last_result("chris").weight_kg == 115.0
    assert [r.id for r in manager.queue.items()] == [rid]
    # Body fat goes back to the earlier reading's.
    assert float(hass.states.get(fat).state) == pytest.approx(old_fat)
    for name in ("chris_phone", "sam_phone"):
        assert notify[name][-1].data["title"] == "Who just weighed in?"


async def test_services_move_and_discard_assigned_readings(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any,
) -> None:
    entry = await _auto_assign_session_2(
        hass, hass_storage, freezer, setup_entry, bt_register
    )
    manager = entry.runtime_data
    rid = _reading_id(entry, "chris")
    await hass.services.async_call(
        DOMAIN, "assign_reading", {"reading_id": rid, "person": "Sam"}, blocking=True
    )
    assert manager.last_result("sam").reading_id == rid
    await hass.services.async_call(
        DOMAIN, "discard_reading", {"reading_id": rid}, blocking=True
    )
    assert manager.last_result("sam").weight_kg == 70.0
    assert not manager.has_reading(rid)


# -- 2. change since last -----------------------------------------------------


async def test_change_since_last_sensors(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any,
) -> None:
    await _auto_assign_session_2(
        hass, hass_storage, freezer, setup_entry, bt_register
    )
    kg = hass.states.get(_eid(hass, "sensor", f"{ENTRY_ID}_chris_weight_change"))
    assert float(kg.state) == pytest.approx(0.05)
    assert kg.attributes["unit_of_measurement"] == "kg"
    assert kg.attributes["previous_weight_kg"] == 115.0
    assert kg.attributes["previous_measured"] == EARLIER.isoformat()
    lb = hass.states.get(_eid(hass, "sensor", f"{ENTRY_ID}_chris_weight_change_lb"))
    assert float(lb.state) == pytest.approx(0.11)
    assert lb.attributes["unit_of_measurement"] == "lb"
    # Sam has a single reading: no change yet.
    sam = hass.states.get(_eid(hass, "sensor", f"{ENTRY_ID}_sam_weight_change"))
    assert sam.state == "unknown"


async def test_socks_reading_keeps_body_fat_from_history(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any,
) -> None:
    from homeassistant.util import dt as dt_util  # noqa: F401
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    from datetime import timedelta as td

    entry = await _auto_assign_session_2(
        hass, hass_storage, freezer, setup_entry, bt_register
    )
    fat = _eid(hass, "sensor", f"{ENTRY_ID}_chris_body_fat")
    barefoot = hass.states.get(fat).state
    await _feed(hass, freezer, bt_register, SESSION_3)
    freezer.tick(td(seconds=8))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert entry.runtime_data.last_result("chris").weight_kg == 114.82
    assert hass.states.get(fat).state == barefoot


# -- more than two people -------------------------------------------------------


async def test_three_people_ask_shows_nearest_two_and_someone_else(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any, notify: Any,
) -> None:
    # Nobody within tolerance: Robin 112, Chris 125, Sam 70.
    _store(
        hass_storage,
        {
            "queue": [],
            "history": {
                "chris": [_last(125.0, EARLIER)],
                "sam": [_last(70.0, EARLIER)],
                "robin": [_last(112.0, EARLIER)],
            },
        },
    )
    entry = await setup_entry(people=(CHRIS, SAM, ROBIN))
    await _feed(hass, freezer, bt_register, SESSION_2)
    (reading,) = entry.runtime_data.queue.items()
    actions = notify["chris_phone"][-1].data["data"]["actions"]
    assert [a["title"] for a in actions] == ["Robin", "Chris", "Someone else…"]
    assert actions[0]["action"] == f"FG260_ASSIGN|{reading.id}|robin"
    assert actions[2]["action"] == "URI"
    assert actions[2]["uri"].startswith("/config/devices/device/")


async def test_assign_latest_select(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any,
) -> None:
    entry = await setup_entry()
    select = _eid(hass, "select", f"{ENTRY_ID}_assign_latest")
    assert hass.states.get(select).state == "unavailable"
    reading = await _queue_session_2(hass, freezer, bt_register, entry)
    state = hass.states.get(select)
    assert state.attributes["options"] == ["Chris", "Sam", "Discard"]
    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "Sam"},
        blocking=True,
    )
    assert entry.runtime_data.last_result("sam").reading_id == reading.id
    assert hass.states.get(select).state == "unavailable"


# -- 3. scale signal ------------------------------------------------------------


async def test_scale_signal_and_last_seen(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any,
) -> None:
    await setup_entry()
    when, info = _adverts()[6]
    await _feed(hass, freezer, bt_register, slice(6, 7))
    rssi = hass.states.get(_eid(hass, "sensor", f"{ENTRY_ID}_signal_strength"))
    assert int(rssi.state) == info.rssi
    assert rssi.attributes["source"] == "local"
    seen = hass.states.get(_eid(hass, "sensor", f"{ENTRY_ID}_last_seen"))
    assert datetime.fromisoformat(seen.state) == when.replace(microsecond=0)
    reg = er.async_get(hass)
    assert reg.async_get(_eid(hass, "sensor", f"{ENTRY_ID}_last_seen")).entity_category == er.EntityCategory.DIAGNOSTIC


# -- repairs and diagnostics ----------------------------------------------------


async def test_missing_notify_service_raises_repair(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any, notify: Any,
) -> None:
    gone = {**SAM, "notify_services": ["notify.sams_old_phone"]}
    entry = await setup_entry(people=(CHRIS, gone))
    issue_id = f"missing_notify_service_{ENTRY_ID}_sams_old_phone"
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, issue_id) is not None  # at startup
    await _queue_session_2(hass, freezer, bt_register, entry)
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue.translation_placeholders == {
        "service": "notify.sams_old_phone",
        "people": "Sam",
    }
    # Chris still got asked.
    assert notify["chris_phone"][-1].data["title"] == "Who just weighed in?"

    async_mock_service(hass, "notify", "sams_old_phone")
    await _feed(hass, freezer, bt_register, slice(15, 18))
    from datetime import timedelta as td
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    freezer.tick(td(seconds=8))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_diagnostics_redacts_personal_details(
    hass: HomeAssistant, hass_storage: dict, freezer: FrozenDateTimeFactory,
    setup_entry: Any, bt_register: Any,
) -> None:
    entry = await _auto_assign_session_2(
        hass, hass_storage, freezer, setup_entry, bt_register
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = repr(diag)
    for secret in ("Chris", "Sam", "1985-01-01", "chris_phone", "AA:BB:CC:DD:EE:FF"):
        assert secret not in text, secret
    assert diag["entry"]["data"]["address"] == REDACTED
    assert diag["people"][0]["results"][-1]["weight_kg"] == 115.05
