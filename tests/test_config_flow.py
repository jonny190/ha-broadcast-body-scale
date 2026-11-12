"""Tests for the FG260 config and options flows."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fg260_scale.const import DOMAIN
from custom_components.fg260_scale.models import age_on, people_from_options

ADDRESS = "AA:BB:CC:DD:EE:FF"
FIXTURE = Path(__file__).parent / "fixtures" / "aaa006_capture.json"


def _service_info(address: str = ADDRESS) -> BluetoothServiceInfoBleak:
    advert = json.loads(FIXTURE.read_text())[0]
    mfr = {int(k): bytes.fromhex(v) for k, v in advert["manufacturer_data"].items()}
    return BluetoothServiceInfoBleak(
        name="FG260",
        address=address,
        rssi=advert["rssi"],
        manufacturer_data=mfr,
        service_data={},
        service_uuids=[],
        source="local",
        device=None,
        advertisement=None,
        connectable=False,
        time=advert["time"],
        tx_power=None,
    )


@pytest.fixture
def notify_services(hass: HomeAssistant) -> None:
    async def _noop(call: ServiceCall) -> None:
        return None

    for name in ("chris_phone", "other"):
        hass.services.async_register("notify", name, _noop)


def _setup_person_input(**over):
    data = {
        "name": "Chris",
        "height_cm": 180,
        "sex": "male",
        "birth_date": "1985-01-01",
        "athlete": False,
        "notify_services": ["notify.chris_phone"],
    }
    data.update(over)
    return data


async def test_bluetooth_discovery_guided_setup(
    hass: HomeAssistant, notify_services: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_service_info(),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "person"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _setup_person_input()
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "another_person"
    assert result["menu_options"] == ["add_another", "finish"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "FG260 Body Scale"
    assert result["data"] == {
        "address": ADDRESS, "family": "icomon", "manufacturer_id": 41132
    }
    assert result["result"].unique_id == ADDRESS
    options = result["options"]
    assert options["tolerance_kg"] == 3.0
    assert len(options["people"]) == 1
    person = options["people"][0]
    assert len(person["id"]) == 32
    assert person == {
        "id": person["id"],
        "name": "Chris",
        "height_cm": 180,
        "sex": "male",
        "birth_date": "1985-01-01",
        "athlete": False,
        "notify_services": ["notify.chris_phone"],
    }


async def test_user_step_guided_setup_two_people(
    hass: HomeAssistant, notify_services: None
) -> None:
    with patch(
        "custom_components.fg260_scale.config_flow.async_discovered_service_info",
        return_value=[_service_info()],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"address": ADDRESS}
        )
    assert result["step_id"] == "person"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _setup_person_input()
    )
    assert result["step_id"] == "another_person"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "add_another"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "person"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _setup_person_input(
            name="Sam",
            sex="female",
            athlete=True,
            notify_services=["notify.other"],
        ),
    )
    assert result["step_id"] == "another_person"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "address": ADDRESS, "family": "icomon", "manufacturer_id": 41132
    }
    assert result["result"].unique_id == ADDRESS
    people = result["options"]["people"]
    assert [p["name"] for p in people] == ["Chris", "Sam"]
    assert people[0]["id"] != people[1]["id"]
    assert people[1]["sex"] == "female"
    assert people[1]["athlete"] is True
    assert people[1]["notify_services"] == ["notify.other"]
    assert result["options"]["tolerance_kg"] == 3.0


async def _to_person_form(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_service_info(),
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={}
    )


async def test_setup_person_form_validation_errors(
    hass: HomeAssistant, notify_services: None
) -> None:
    result = await _to_person_form(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _setup_person_input(notify_services=["notify.nope"]),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "person"
    assert result["errors"] == {"notify_services": "unknown_notify_service"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _setup_person_input(notify_services=[])
    )
    assert result["step_id"] == "person"
    assert result["errors"] == {"notify_services": "notify_required"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _setup_person_input(birth_date="2999-01-01")
    )
    assert result["step_id"] == "person"
    assert result["errors"] == {"birth_date": "birth_date_in_future"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _setup_person_input(name="   ")
    )
    assert result["errors"] == {"name": "name_required"}
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_setup_notify_picker_excludes_unsuitable(
    hass: HomeAssistant, notify_services: None
) -> None:
    async def _noop(call: ServiceCall) -> None:
        return None

    for name in ("send_message", "persistent_notification"):
        hass.services.async_register("notify", name, _noop)
    result = await _to_person_form(hass)
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == "notify_services")
    options = schema[key].config["options"]
    assert "notify.chris_phone" in options
    assert "notify.send_message" not in options
    assert "notify.persistent_notification" not in options


async def test_setup_height_out_of_range_rejected(
    hass: HomeAssistant, notify_services: None
) -> None:
    from homeassistant.data_entry_flow import InvalidData

    result = await _to_person_form(hass)
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], _setup_person_input(height_cm=99)
        )


async def test_user_step_no_devices(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.fg260_scale.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_duplicate_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={"address": ADDRESS}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_service_info(),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    async def _noop(call: ServiceCall) -> None:
        return None

    hass.services.async_register("notify", "chris_phone", _noop)
    hass.services.async_register("notify", "other", _noop)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={"address": ADDRESS},
        options={"tolerance_kg": 3.0, "people": []},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.fg260_scale.async_setup_entry", return_value=True
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _person_input(**over):
    data = {
        "name": "Chris",
        "height_cm": 180,
        "sex": "male",
        "birth_date": "1985-01-01",
        "athlete": False,
        "notify_services": ["notify.chris_phone"],
    }
    data.update(over)
    return data


async def _menu(hass, entry, choice):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": choice}
    )


async def test_options_add_edit_remove_person(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result = await _menu(hass, entry, "add_person")
    assert result["step_id"] == "add_person"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _person_input()
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    people = people_from_options(entry.options)
    assert len(people) == 1
    person = next(iter(people.values()))
    assert person.name == "Chris"
    assert person.height_cm == 180
    assert person.sex == "male"
    assert person.birth_date == date(1985, 1, 1)
    assert person.athlete is False
    assert person.notify_services == ["notify.chris_phone"]
    assert entry.options["tolerance_kg"] == 3.0
    assert entry.options["people"][0]["birth_date"] == "1985-01-01"
    pid = person.id

    result = await _menu(hass, entry, "edit_person")
    assert result["step_id"] == "edit_person"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"person": pid}
    )
    assert result["step_id"] == "edit_person_form"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _person_input(height_cm=182)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    people = people_from_options(entry.options)
    assert list(people) == [pid]
    assert people[pid].height_cm == 182

    result = await _menu(hass, entry, "remove_person")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"person": pid}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["people"] == []


async def test_options_settings_tolerance(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result = await _menu(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tolerance_kg": 5.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["tolerance_kg"] == 5.5
    assert entry.options["people"] == []


async def test_notify_service_must_exist(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result = await _menu(hass, entry, "add_person")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _person_input(notify_services=["notify.nope"])
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"notify_services": "unknown_notify_service"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _person_input(notify_services=[])
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"notify_services": "notify_required"}
    assert entry.options["people"] == []


@pytest.mark.parametrize("height", [99, 251])
async def test_height_out_of_range_rejected(
    hass: HomeAssistant, entry: MockConfigEntry, height: int
) -> None:
    from homeassistant.data_entry_flow import InvalidData

    result = await _menu(hass, entry, "add_person")
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], _person_input(height_cm=height)
        )


async def test_birth_date_must_be_past(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result = await _menu(hass, entry, "add_person")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _person_input(birth_date="2999-01-01")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"birth_date": "birth_date_in_future"}


def test_age_on() -> None:
    assert age_on(date(1985, 6, 1), date(2026, 5, 31)) == 40
    assert age_on(date(1985, 6, 1), date(2026, 6, 1)) == 41


async def test_notify_picker_excludes_unsuitable_services(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    async def _noop(call: ServiceCall) -> None:
        return None

    for name in ("send_message", "persistent_notification"):
        hass.services.async_register("notify", name, _noop)
    result = await _menu(hass, entry, "add_person")
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == "notify_services")
    options = schema[key].config["options"]
    assert "notify.chris_phone" in options
    assert "notify.send_message" not in options
    assert "notify.persistent_notification" not in options

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _person_input(notify_services=["notify.send_message"])
    )
    assert result["errors"] == {"notify_services": "unknown_notify_service"}


async def test_finish_aborts_if_scale_configured_meanwhile(
    hass: HomeAssistant, notify_services: None
) -> None:
    result = await _to_person_form(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _setup_person_input()
    )
    assert result["step_id"] == "another_person"

    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={"address": ADDRESS},
        options={"tolerance_kg": 4.0, "people": []},
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert entries == [existing]
    assert existing.options == {"tolerance_kg": 4.0, "people": []}
