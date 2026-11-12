"""Tests for the scale manager: assignment, queue, notifications and services."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth import (
    BluetoothCallbackReplay,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.fg260_scale.assembler import Measurement
from custom_components.fg260_scale.body.wla03 import calculate, correct_impedance
from custom_components.fg260_scale.const import DOMAIN
from custom_components.fg260_scale.models import age_on
from custom_components.fg260_scale.queue import PendingReading, ReadingQueue

ADDRESS = "AA:BB:CC:DD:EE:FF"
ENTRY_ID = "scale_entry"
STORE_KEY = f"{DOMAIN}.{ENTRY_ID}"
FIXTURE = Path(__file__).parent / "fixtures" / "aaa006_capture.json"
EVENT = "fg260_scale_measurement"
ACTION_EVENT = "mobile_app_notification_action"

CHRIS = {
    "id": "chris",
    "name": "Chris",
    "height_cm": 180,
    "sex": "male",
    "birth_date": "1985-01-01",
    "athlete": False,
    "notify_services": ["notify.chris_phone"],
}
SAM = {
    "id": "sam",
    "name": "Sam",
    "height_cm": 165,
    "sex": "female",
    "birth_date": "1990-06-01",
    "athlete": False,
    # shares Chris's group: it must only be notified once
    "notify_services": ["notify.sam_phone", "notify.chris_phone"],
}


def _adverts() -> list[tuple[datetime, BluetoothServiceInfoBleak]]:
    out = []
    for rec in json.loads(FIXTURE.read_text()):
        mfr = {int(k): bytes.fromhex(v) for k, v in rec["manufacturer_data"].items()}
        info = BluetoothServiceInfoBleak(
            name="FG260",
            address=ADDRESS,
            rssi=rec["rssi"],
            manufacturer_data=mfr,
            service_data={},
            service_uuids=[],
            source="local",
            device=None,
            advertisement=None,
            connectable=False,
            time=rec["time"],
            tx_power=None,
        )
        out.append((datetime.fromtimestamp(rec["time"], tz=UTC), info))
    return out


SESSION_2 = slice(6, 15)  # zero frame .. 0xA6 with impedance 525
SESSION_3 = slice(15, 18)  # socks: 114.82 kg, no impedance


def _last(weight: float, when: datetime) -> dict[str, Any]:
    return {
        "timestamp": when.isoformat(),
        "weight_kg": weight,
        "bmi": 20.0,
        "impedance": 500,
        "composition": None,
    }


def _store(hass_storage: dict, data: dict[str, Any]) -> None:
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": data,
    }


@pytest.fixture
def bt_register() -> Any:
    """Capture the bluetooth callback the manager registers."""
    holder: dict[str, Any] = {"calls": []}

    def _register(hass, callback, matcher, mode, **kwargs):
        holder["calls"].append((callback, matcher, mode, kwargs))
        holder["cb"] = callback
        holder["unsub"] = Mock()
        return holder["unsub"]

    with patch(
        "homeassistant.components.bluetooth.async_register_callback",
        side_effect=_register,
    ):
        yield holder


@pytest.fixture
def notify(hass: HomeAssistant) -> dict[str, list[ServiceCall]]:
    return {
        "chris_phone": async_mock_service(hass, "notify", "chris_phone"),
        "sam_phone": async_mock_service(hass, "notify", "sam_phone"),
    }


@pytest.fixture
def setup_entry(
    hass: HomeAssistant, bt_register: Any, notify: Any
) -> Callable[..., Any]:
    async def _setup(
        people=(CHRIS, SAM), tolerance: float = 3.0, **extra: Any
    ) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id=ENTRY_ID,
            unique_id=ADDRESS,
            title="FG260 Body Scale",
            # An entry from before other scale families: no family stored.
            data={"address": ADDRESS},
            options={
                "tolerance_kg": tolerance,
                "people": [dict(p) for p in people],
                **extra,
            },
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        return entry

    return _setup


async def _feed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    bt_register: Any,
    part: slice,
) -> None:
    for when, info in _adverts()[part]:
        freezer.move_to(when)
        bt_register["cb"](info, BluetoothChange.ADVERTISEMENT)
        await hass.async_block_till_done()


async def _queue_session_2(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, bt_register: Any, entry
) -> PendingReading:
    await _feed(hass, freezer, bt_register, SESSION_2)
    items = entry.runtime_data.queue.items()
    assert len(items) == 1
    return items[0]


def _expected_comp(person: dict, weight: float, adc: int, when: datetime):
    return calculate(
        weight,
        person["height_cm"],
        age_on(
            date.fromisoformat(person["birth_date"]), dt_util.as_local(when).date()
        ),
        person["sex"],
        person["athlete"],
        correct_impedance(adc, weight),
    )


async def test_registers_passive_callback(
    hass: HomeAssistant, setup_entry, bt_register
) -> None:
    await setup_entry()
    assert len(bt_register["calls"]) == 1
    _, matcher, mode, kwargs = bt_register["calls"][0]
    assert matcher["address"] == ADDRESS
    # Address only: other scale families use other company IDs.
    assert "manufacturer_id" not in matcher
    assert mode is BluetoothScanningMode.PASSIVE
    assert kwargs.get("replay") is BluetoothCallbackReplay.DISABLED


async def test_auto_assigns_clear_reading(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
    notify,
) -> None:
    earlier = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    _store(
        hass_storage,
        {
            "queue": [],
            "last": {"chris": _last(115.0, earlier), "sam": _last(70.0, earlier)},
        },
    )
    entry = await setup_entry()
    events = async_capture_events(hass, EVENT)
    updates: list[None] = []
    async_dispatcher_connect(
        hass, f"{DOMAIN}_{ENTRY_ID}_updated", lambda: updates.append(None)
    )

    await _feed(hass, freezer, bt_register, SESSION_2)

    manager = entry.runtime_data
    when = _adverts()[14][0]
    bmi, comp = _expected_comp(CHRIS, 115.05, 525, when)
    assert comp is not None

    result = manager.last_result("chris")
    assert result is not None
    assert result.weight_kg == 115.05
    assert result.timestamp == when
    assert result.impedance == 525
    assert result.bmi == bmi
    assert result.composition == comp
    assert manager.last_result("sam").weight_kg == 70.0

    assert manager.queue.items() == []
    # Only the matched person gets the confirmation.
    assert len(notify["chris_phone"]) == 1
    assert notify["chris_phone"][0].data["title"] == "Saved to Chris"
    assert notify["sam_phone"] == []
    assert updates

    assert len(events) == 1
    data = events[0].data
    assert data["person_id"] == "chris"
    assert data["person_name"] == "Chris"
    assert data["timestamp"] == when.isoformat()
    assert data["weight_kg"] == 115.05
    assert data["impedance"] == 525
    assert data["bmi"] == bmi
    assert data["body_fat"] == comp.body_fat
    for key, value in asdict(comp).items():
        assert data[key] == value

    # the result is persisted for the next restart
    saved = hass_storage[STORE_KEY]["data"]["history"]["chris"][-1]
    assert saved["weight_kg"] == 115.05
    assert saved["timestamp"] == when.isoformat()
    assert saved["composition"]["body_fat"] == comp.body_fat


async def test_socks_reading_has_no_composition(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
) -> None:
    earlier = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    _store(hass_storage, {"queue": [], "last": {"chris": _last(115.0, earlier)}})
    entry = await setup_entry()
    events = async_capture_events(hass, EVENT)

    await _feed(hass, freezer, bt_register, SESSION_3)
    # weight-only weigh-in completes on the 2 s flush timer
    freezer.tick(timedelta(seconds=8))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    result = entry.runtime_data.last_result("chris")
    assert result.weight_kg == 114.82
    assert result.impedance is None
    assert result.composition is None
    assert len(events) == 1
    assert events[0].data["impedance"] is None
    assert events[0].data["body_fat"] is None


async def test_ambiguous_notifies_and_queues(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
    notify,
) -> None:
    entry = await setup_entry()
    events = async_capture_events(hass, EVENT)

    reading = await _queue_session_2(hass, freezer, bt_register, entry)
    when = _adverts()[14][0]
    assert reading.weight_kg == 115.05
    assert reading.impedance == 525
    assert reading.timestamp == when
    assert events == []
    assert entry.runtime_data.last_result("chris") is None

    hhmm = dt_util.as_local(when).strftime("%H:%M")
    rid = reading.id
    for name in ("chris_phone", "sam_phone"):
        assert len(notify[name]) == 1, name
        call = notify[name][0]
        assert call.data["title"] == "Who just weighed in?"
        assert call.data["message"] == f"18 st 1.6 lb (115.05 kg) at {hhmm}"
        assert call.data["data"]["tag"] == f"fg260_{rid}"
        assert call.data["data"]["actions"] == [
            {"action": f"FG260_ASSIGN|{rid}|chris", "title": "Chris"},
            {"action": f"FG260_ASSIGN|{rid}|sam", "title": "Sam"},
            {"action": f"FG260_DISCARD|{rid}", "title": "Guest / discard"},
        ]

    saved = hass_storage[STORE_KEY]["data"]["queue"]
    assert saved == [
        {
            "id": rid,
            "timestamp": when.isoformat(),
            "weight_kg": 115.05,
            "impedance": 525,
            "formula": 3,
            "adc_mode": "fitdays",
            "scale_metrics": {},
        }
    ]


async def test_action_assigns_and_clears(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
    notify,
) -> None:
    entry = await setup_entry()
    events = async_capture_events(hass, EVENT)
    reading = await _queue_session_2(hass, freezer, bt_register, entry)
    for calls in notify.values():
        calls.clear()

    hass.bus.async_fire(ACTION_EVENT, {"action": f"FG260_ASSIGN|{reading.id}|sam"})
    await hass.async_block_till_done()

    manager = entry.runtime_data
    assert manager.queue.items() == []
    bmi, comp = _expected_comp(SAM, 115.05, 525, reading.timestamp)
    result = manager.last_result("sam")
    assert result.weight_kg == 115.05
    assert result.bmi == bmi
    assert result.composition == comp
    assert manager.last_result("chris") is None
    assert len(events) == 1
    assert events[0].data["person_id"] == "sam"

    for name in ("chris_phone", "sam_phone"):
        assert len(notify[name]) == 1, name
        call = notify[name][0]
        assert call.data["message"] == "clear_notification"
        assert call.data["data"] == {"tag": f"fg260_{reading.id}"}

    # a second answer from another phone is ignored: first answer wins
    hass.bus.async_fire(ACTION_EVENT, {"action": f"FG260_ASSIGN|{reading.id}|chris"})
    await hass.async_block_till_done()
    assert manager.last_result("chris") is None
    assert len(events) == 1


async def test_unrelated_actions_ignored(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
) -> None:
    entry = await setup_entry()
    await _queue_session_2(hass, freezer, bt_register, entry)
    for data in ({"action": "OTHER_THING|x"}, {}, {"action": "FG260_BOGUS"}):
        hass.bus.async_fire(ACTION_EVENT, data)
    await hass.async_block_till_done()
    assert len(entry.runtime_data.queue.items()) == 1


async def test_action_after_restart_still_assigns(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
    notify,
) -> None:
    entry = await setup_entry()
    reading = await _queue_session_2(hass, freezer, bt_register, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = entry.runtime_data
    assert [r.id for r in manager.queue.items()] == [reading.id]

    hass.bus.async_fire(ACTION_EVENT, {"action": f"FG260_ASSIGN|{reading.id}|chris"})
    await hass.async_block_till_done()

    assert manager.queue.items() == []
    assert manager.last_result("chris").weight_kg == 115.05


async def test_last_result_survives_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
) -> None:
    earlier = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    _store(hass_storage, {"queue": [], "last": {"chris": _last(115.0, earlier)}})
    entry = await setup_entry()
    await _feed(hass, freezer, bt_register, SESSION_2)
    before = entry.runtime_data.last_result("chris")

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    after = entry.runtime_data.last_result("chris")
    assert after == before
    assert after.composition is not None


async def test_assign_to_removed_person(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
    notify,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await setup_entry()
    events = async_capture_events(hass, EVENT)
    reading = await _queue_session_2(hass, freezer, bt_register, entry)
    hass.config_entries.async_update_entry(
        entry, options={"tolerance_kg": 3.0, "people": [dict(CHRIS)]}
    )
    await hass.async_block_till_done()
    manager = entry.runtime_data

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hass.bus.async_fire(
            ACTION_EVENT, {"action": f"FG260_ASSIGN|{reading.id}|sam"}
        )
        await hass.async_block_till_done()
    assert any(
        r.levelno == logging.WARNING and reading.id in r.getMessage()
        for r in caplog.records
    )
    assert [r.id for r in manager.queue.items()] == [reading.id]

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "assign_reading",
            {"reading_id": reading.id, "person": "Sam"},
            blocking=True,
        )
    assert [r.id for r in manager.queue.items()] == [reading.id]
    assert events == []

    # an unknown reading is also a validation error, and an unknown reading
    # in an action is only a warning
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "assign_reading",
            {"reading_id": "nope", "person": "chris"},
            blocking=True,
        )
    hass.bus.async_fire(ACTION_EVENT, {"action": "FG260_ASSIGN|nope|chris"})
    await hass.async_block_till_done()
    assert [r.id for r in manager.queue.items()] == [reading.id]

    # the remaining person can still be assigned, by name (case-insensitive)
    await hass.services.async_call(
        DOMAIN,
        "assign_reading",
        {"reading_id": reading.id, "person": "cHRIS"},
        blocking=True,
    )
    assert manager.queue.items() == []
    assert manager.last_result("chris").weight_kg == 115.05
    assert len(events) == 1


async def test_late_assignment_keeps_newer_sensor(
    hass: HomeAssistant,
    hass_storage: dict,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
) -> None:
    # Chris has a much newer reading at a weight far from the queued one.
    newer = datetime(2027, 1, 1, 7, 0, tzinfo=UTC)
    _store(hass_storage, {"queue": [], "last": {"chris": _last(90.0, newer)}})
    entry = await setup_entry()
    events = async_capture_events(hass, EVENT)
    reading = await _queue_session_2(hass, freezer, bt_register, entry)
    manager = entry.runtime_data
    before = manager.last_result("chris")

    await hass.services.async_call(
        DOMAIN,
        "assign_reading",
        {"reading_id": reading.id, "person": "chris"},
        blocking=True,
    )
    assert manager.queue.items() == []
    assert manager.last_result("chris") == before
    assert len(events) == 1
    assert events[0].data["weight_kg"] == 115.05
    assert events[0].data["timestamp"] == reading.timestamp.isoformat()


async def test_queue_cap_50(hass: HomeAssistant, hass_storage: dict) -> None:
    queue = ReadingQueue(hass, ENTRY_ID)
    await queue.load()
    base = datetime(2026, 11, 1, tzinfo=UTC)
    readings = [
        PendingReading(
            id=f"r{i}",
            timestamp=base + timedelta(minutes=i),
            weight_kg=70 + i / 10,
            impedance=500 + i,
        )
        for i in range(51)
    ]
    for r in readings:
        await queue.add(r)
    items = queue.items()
    assert len(items) == 50
    assert items[0].id == "r1"
    assert items[-1].id == "r50"
    assert await queue.pop("r0") is None
    assert await queue.pop("r10") == readings[10]
    assert len(queue.items()) == 49

    reloaded = ReadingQueue(hass, ENTRY_ID)
    await reloaded.load()
    assert [r.id for r in reloaded.items()] == [r.id for r in queue.items()]
    assert reloaded.items()[0] == readings[1]


async def test_discard_action_and_service(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
    notify,
) -> None:
    entry = await setup_entry()
    events = async_capture_events(hass, EVENT)
    manager = entry.runtime_data

    first = await _queue_session_2(hass, freezer, bt_register, entry)
    for calls in notify.values():
        calls.clear()
    hass.bus.async_fire(ACTION_EVENT, {"action": f"FG260_DISCARD|{first.id}"})
    await hass.async_block_till_done()
    assert manager.queue.items() == []
    for name in ("chris_phone", "sam_phone"):
        assert [c.data["message"] for c in notify[name]] == ["clear_notification"]
        assert notify[name][0].data["data"] == {"tag": f"fg260_{first.id}"}

    # queue another (a later weigh-in: session 4) and discard via the service
    await _feed(hass, freezer, bt_register, slice(18, 24))
    items = manager.queue.items()
    assert len(items) == 1
    second = items[0]
    assert second.weight_kg == 115.28
    await hass.services.async_call(
        DOMAIN, "discard_reading", {"reading_id": second.id}, blocking=True
    )
    assert manager.queue.items() == []
    assert events == []

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "discard_reading", {"reading_id": second.id}, blocking=True
        )


async def test_unload_stops_manager(
    hass: HomeAssistant, setup_entry, bt_register
) -> None:
    entry = await setup_entry()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    bt_register["unsub"].assert_called_once()
    # services stay registered at domain level
    assert hass.services.has_service(DOMAIN, "assign_reading")
    assert hass.services.has_service(DOMAIN, "discard_reading")


def _live_measurement(weight: float) -> Measurement:
    return Measurement(
        timestamp=datetime(2026, 11, 11, 7, 0, tzinfo=UTC),
        weight_kg=weight,
        impedance=None,
        bfa_type=None,
        stable=False,
    )


async def test_unstable_measurement_is_queued_not_auto_assigned(
    hass: HomeAssistant,
    hass_storage: dict,
    setup_entry,
    notify,
) -> None:
    earlier = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    _store(
        hass_storage,
        {
            "queue": [],
            "last": {"chris": _last(115.0, earlier), "sam": _last(70.0, earlier)},
        },
    )
    entry = await setup_entry()
    manager = entry.runtime_data
    # 115.5 is within tolerance of exactly one person, but the weight is live-only.
    await manager._async_handle_measurement(_live_measurement(115.5))
    await hass.async_block_till_done()

    assert manager.last_result("chris").weight_kg == 115.0
    assert len(manager.queue.items()) == 1
    for name in ("chris_phone", "sam_phone"):
        assert len(notify[name]) == 1, name
        assert notify[name][0].data["title"] == "Who just weighed in?"


@pytest.mark.parametrize("error", [vol.Invalid("bad"), RuntimeError("boom")])
async def test_failing_notify_target_does_not_stop_others(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_entry,
    bt_register,
    notify,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    async def _boom(call: ServiceCall) -> None:
        raise error

    hass.services.async_register("notify", "chris_phone", _boom)
    entry = await setup_entry()
    await _queue_session_2(hass, freezer, bt_register, entry)
    assert len(notify["sam_phone"]) == 1
    assert "notify.chris_phone" in caplog.text
