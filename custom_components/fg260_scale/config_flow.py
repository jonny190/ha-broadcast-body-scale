"""Config and options flows for the Broadcast Body Scale."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADDRESS,
    CONF_CONFIRM_AUTO,
    CONF_FAMILY,
    CONF_MANUFACTURER_ID,
    CONF_PEOPLE,
    CONF_TOLERANCE_KG,
    DEFAULT_CONFIRM_AUTO,
    DEFAULT_TOLERANCE_KG,
    DOMAIN,
    MAX_HEIGHT_CM,
    MAX_TOLERANCE_KG,
    MIN_HEIGHT_CM,
    MIN_TOLERANCE_KG,
    NAME,
    TESTED_NAME,
)
from .parser import ScaleIdentity, identify


# Entity-targeted / schema-incompatible notify services we cannot call with
# the actionable-notification payload.
UNSUITABLE_NOTIFY = frozenset({"send_message", "persistent_notification"})




class PersonFormMixin:
    """Person form schema, validation and stored shape shared by both flows."""

    hass: HomeAssistant

    def _notify_services(self) -> list[str]:
        services = self.hass.services.async_services().get("notify", {})
        return sorted(
            f"notify.{name}" for name in services if name not in UNSUITABLE_NOTIFY
        )

    def _person_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        notify_options = self._notify_services()
        for existing in defaults.get("notify_services", []):
            if existing not in notify_options:
                notify_options.append(existing)
        return vol.Schema(
            {
                vol.Required("name", default=defaults.get("name", "")): str,
                vol.Required(
                    "height_cm", default=defaults.get("height_cm", 170)
                ): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_HEIGHT_CM, max=MAX_HEIGHT_CM)
                ),
                vol.Required("sex", default=defaults.get("sex", "male")): (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["male", "female"],
                            translation_key="sex",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                ),
                vol.Required(
                    "birth_date", default=defaults.get("birth_date", "1990-01-01")
                ): selector.DateSelector(),
                vol.Required(
                    "notify_services", default=defaults.get("notify_services", [])
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=notify_options,
                        multiple=True,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

    def _validate_person(self, user_input: dict[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not user_input["name"].strip():
            errors["name"] = "name_required"
        try:
            born = date.fromisoformat(user_input["birth_date"])
        except ValueError:
            errors["birth_date"] = "invalid_date"
        else:
            if born >= date.today():
                errors["birth_date"] = "birth_date_in_future"
        services = user_input["notify_services"]
        known = self._notify_services()
        if not services:
            errors["notify_services"] = "notify_required"
        elif any(s not in known for s in services):
            errors["notify_services"] = "unknown_notify_service"
        return errors

    @staticmethod
    def _person_record(pid: str, user_input: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": pid,
            "name": user_input["name"].strip(),
            "height_cm": int(user_input["height_cm"]),
            "sex": user_input["sex"],
            "birth_date": user_input["birth_date"],
            "notify_services": list(user_input["notify_services"]),
        }


class FG260ConfigFlow(PersonFormMixin, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a broadcast body scale."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._address: str | None = None
        self._identity: ScaleIdentity | None = None
        self._people_added: list[dict[str, Any]] = []

    @staticmethod
    def _title(identity: ScaleIdentity) -> str:
        return TESTED_NAME if identity.tested else NAME

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FG260OptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        identity = identify(
            discovery_info.manufacturer_data, discovery_info.service_uuids
        )
        if identity is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self._identity = identity
        self.context["title_placeholders"] = {"name": self._title(identity)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None and self._identity is not None
        if user_input is not None:
            self._address = self._discovery.address
            return await self._async_step_after_pick()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._title(self._identity),
                "model": self._identity.model,
            },
        )

    async def _async_step_after_pick(self) -> ConfigFlowResult:
        assert self._identity is not None
        if self._identity.tested:
            return await self.async_step_person()
        return await self.async_step_untested()

    async def async_step_untested(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Warn that this scale type has not been tried on a real scale."""
        assert self._identity is not None
        if user_input is not None:
            return await self.async_step_person()
        return self.async_show_form(
            step_id="untested",
            data_schema=vol.Schema({}),
            description_placeholders={"model": self._identity.model},
        )

    async def async_step_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate_person(user_input)
            if not errors:
                self._people_added.append(self._person_record(uuid4().hex, user_input))
                return self.async_show_menu(
                    step_id="another_person", menu_options=["add_another", "finish"]
                )
        return self.async_show_form(
            step_id="person",
            data_schema=self._person_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_another_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # HA requires a step method for every menu step_id it processes.
        return self.async_show_menu(
            step_id="another_person", menu_options=["add_another", "finish"]
        )

    async def async_step_add_another(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_person()

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._address is not None and self._people_added
        await self.async_set_unique_id(self._address, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        assert self._identity is not None
        return self.async_create_entry(
            title=self._title(self._identity),
            data={
                CONF_ADDRESS: self._address,
                CONF_FAMILY: self._identity.family,
                CONF_MANUFACTURER_ID: self._identity.manufacturer_id,
            },
            options={
                CONF_TOLERANCE_KG: DEFAULT_TOLERANCE_KG,
                CONF_PEOPLE: self._people_added,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        configured = self._async_current_ids()
        found: dict[str, ScaleIdentity] = {}
        for info in async_discovered_service_info(self.hass, False):
            if info.address in configured or info.address in found:
                continue
            if identity := identify(info.manufacturer_data, info.service_uuids):
                found[info.address] = identity

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            if address not in found:
                return self.async_abort(reason="no_devices_found")
            self._address = address
            self._identity = found[address]
            return await self._async_step_after_pick()

        if not found:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: f"{identity.model} ({address})"
                            for address, identity in found.items()
                        }
                    )
                }
            ),
        )


class FG260OptionsFlow(PersonFormMixin, OptionsFlow):
    """Options flow: manage people and matching tolerance."""

    def __init__(self) -> None:
        self._person_id: str | None = None

    # -- helpers ---------------------------------------------------------
    @property
    def _people(self) -> list[dict[str, Any]]:
        return [dict(p) for p in self.config_entry.options.get(CONF_PEOPLE, [])]

    def _save(self, people: list[dict[str, Any]]) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, CONF_PEOPLE: people}
        )

    def _person_picker(self, step_id: str) -> ConfigFlowResult:
        options = [
            selector.SelectOptionDict(value=p["id"], label=p["name"])
            for p in self._people
        ]
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required("person"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    # -- steps -----------------------------------------------------------
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["add_person"]
        if self._people:
            menu += ["edit_person", "remove_person"]
        menu.append("settings")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_add_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate_person(user_input)
            if not errors:
                people = self._people
                people.append(self._person_record(uuid4().hex, user_input))
                return self._save(people)
        return self.async_show_form(
            step_id="add_person",
            data_schema=self._person_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_edit_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._person_id = user_input["person"]
            return await self.async_step_edit_person_form()
        return self._person_picker("edit_person")

    async def async_step_edit_person_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        people = self._people
        current = next((p for p in people if p["id"] == self._person_id), None)
        if current is None:
            return self.async_abort(reason="person_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate_person(user_input)
            if not errors:
                record = self._person_record(current["id"], user_input)
                return self._save(
                    [record if p["id"] == current["id"] else p for p in people]
                )
        return self.async_show_form(
            step_id="edit_person_form",
            data_schema=self._person_schema(user_input or current),
            errors=errors,
        )

    async def async_step_remove_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(
                [p for p in self._people if p["id"] != user_input["person"]]
            )
        return self._person_picker("remove_person")

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_TOLERANCE_KG: float(user_input[CONF_TOLERANCE_KG]),
                    CONF_CONFIRM_AUTO: bool(
                        user_input.get(CONF_CONFIRM_AUTO, DEFAULT_CONFIRM_AUTO)
                    ),
                }
            )
        current = self.config_entry.options.get(
            CONF_TOLERANCE_KG, DEFAULT_TOLERANCE_KG
        )
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOLERANCE_KG, default=current): vol.All(
                        vol.Coerce(float),
                        vol.Range(min=MIN_TOLERANCE_KG, max=MAX_TOLERANCE_KG),
                    ),
                    vol.Required(
                        CONF_CONFIRM_AUTO,
                        default=self.config_entry.options.get(
                            CONF_CONFIRM_AUTO, DEFAULT_CONFIRM_AUTO
                        ),
                    ): bool,
                }
            ),
        )
