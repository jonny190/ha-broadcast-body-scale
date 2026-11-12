"""Per-person body sensors and the unassigned readings sensor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .manager import (
    PersonView,
    ScaleManager,
    entry_identity,
    format_stone,
    impedance_ohms,
    kg_to_lb,
    signal_seen,
    signal_updated,
)
from .models import Person

if TYPE_CHECKING:
    from . import FG260ConfigEntry

PARALLEL_UPDATES = 0

OHM = "Ω"
UNIT_KCAL = "kcal"


def person_device_identifier(entry_id: str, person_id: str) -> tuple[str, str]:
    """Device registry identifier of one person's device."""
    return (DOMAIN, f"{entry_id}_{person_id}")


@dataclass(frozen=True, kw_only=True)
class PersonSensorDescription(SensorEntityDescription):
    """A person sensor; ``value_fn`` pulls the value out of their history.

    Body composition comes from the newest reading that has it, so a socks
    reading (no impedance) leaves body fat showing the last barefoot one.
    """

    value_fn: Callable[[PersonView], Any]
    attrs_fn: Callable[[PersonView], dict[str, Any]] | None = None


def _comp(name: str) -> Callable[[PersonView], Any]:
    def _get(view: PersonView) -> Any:
        result = view.last_with_composition
        return getattr(result.composition, name) if result else None

    return _get


def _impedance(view: PersonView) -> int | None:
    result = view.last_with_composition
    if result is None:
        return None
    ohms = impedance_ohms(result.impedance, result.adc_mode, result.weight_kg)
    return round(ohms) if ohms else None


def _change_lb(view: PersonView) -> float | None:
    change = view.change_kg
    return None if change is None else kg_to_lb(change)


def _previous_attrs(view: PersonView) -> dict[str, Any]:
    if view.previous is None:
        return {}
    return {
        "previous_weight_kg": view.previous.weight_kg,
        "previous_measured": view.previous.timestamp.isoformat(),
    }


def _pct(key: str, *, default: bool = False, precision: int = 1) -> PersonSensorDescription:
    return PersonSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
        entity_registry_enabled_default=default,
        value_fn=_comp(key),
    )


def _mass(key: str) -> PersonSensorDescription:
    return PersonSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=_comp(key),
    )


PERSON_SENSORS: tuple[PersonSensorDescription, ...] = (
    PersonSensorDescription(
        key="weight",
        translation_key="weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda v: v.last.weight_kg,
    ),
    PersonSensorDescription(
        key="weight_lb",
        translation_key="weight_lb",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda v: kg_to_lb(v.last.weight_kg),
    ),
    PersonSensorDescription(
        key="weight_st",
        translation_key="weight_st",
        value_fn=lambda v: format_stone(v.last.weight_kg),
    ),
    PersonSensorDescription(
        key="weight_change",
        translation_key="weight_change",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda v: v.change_kg,
        attrs_fn=_previous_attrs,
    ),
    PersonSensorDescription(
        key="weight_change_lb",
        translation_key="weight_change_lb",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.POUNDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_change_lb,
        attrs_fn=_previous_attrs,
    ),
    _pct("body_fat", default=True),
    PersonSensorDescription(
        key="bmi",
        translation_key="bmi",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda v: v.last.bmi,
    ),
    PersonSensorDescription(
        key="last_measured",
        translation_key="last_measured",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda v: v.last.timestamp,
    ),
    PersonSensorDescription(
        key="impedance",
        translation_key="impedance",
        native_unit_of_measurement=OHM,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=_impedance,
    ),
    _pct("water"),
    _mass("muscle"),
    _mass("bone"),
    PersonSensorDescription(
        key="visceral_fat",
        translation_key="visceral_fat",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=_comp("visceral_fat"),
    ),
    PersonSensorDescription(
        key="bmr",
        translation_key="bmr",
        native_unit_of_measurement=UNIT_KCAL,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=_comp("bmr"),
    ),
    PersonSensorDescription(
        key="body_age",
        translation_key="body_age",
        native_unit_of_measurement=UnitOfTime.YEARS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
        value_fn=_comp("body_age"),
    ),
    _pct("protein"),
    _pct("subcutaneous_fat"),
    _pct("skeletal_muscle"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FG260ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the unassigned sensor and each person's sensors."""
    manager = entry.runtime_data
    scale_device_id = async_scale_device_id(hass, entry)
    entities: list[SensorEntity] = [
        UnassignedReadingsSensor(manager),
        ScaleSignalSensor(manager),
        ScaleLastSeenSensor(manager),
    ]
    for person in manager.people.values():
        entities.extend(
            PersonSensor(manager, person, description, scale_device_id)
            for description in PERSON_SENSORS
        )
    async_add_entities(entities)


def scale_device_info(entry: FG260ConfigEntry) -> DeviceInfo:
    """The scale's own device; person devices hang off it."""
    identity = entry_identity(entry)
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name=entry.title,
        manufacturer="iComon",
        model=identity.model,
    )


def async_scale_device_id(hass: HomeAssistant, entry: FG260ConfigEntry) -> str:
    """Registry id of the scale's device, creating it if needed."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **scale_device_info(entry)
    ).id


def person_device_info(
    entry_id: str, person: Person, scale_device_id: str
) -> DeviceInfo:
    """One person's device, linked to the scale."""
    return DeviceInfo(
        identifiers={person_device_identifier(entry_id, person.id)},
        name=person.name,
        via_device_id=scale_device_id,
    )


class PersonSensor(RestoreSensor):
    """One measurement of one person."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: PersonSensorDescription

    def __init__(
        self,
        manager: ScaleManager,
        person: Person,
        description: PersonSensorDescription,
        scale_device_id: str,
    ) -> None:
        self.entity_description = description
        self._manager = manager
        self._person_id = person.id
        entry_id = manager.entry.entry_id
        self._attr_unique_id = f"{entry_id}_{person.id}_{description.key}"
        self._attr_device_info = person_device_info(
            entry_id, person, scale_device_id
        )

    async def async_added_to_hass(self) -> None:
        """Restore the previous value, then follow the manager."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None:
            value = data.native_value
            if (
                self.entity_description.device_class is SensorDeviceClass.TIMESTAMP
                and isinstance(value, str)
            ):
                value = datetime.fromisoformat(value)
            self._attr_native_value = value
        self._apply(self._manager.person_view(self._person_id), startup=True)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_updated(self._manager.entry.entry_id),
                self._handle_update,
            )
        )

    def _apply(self, view: PersonView | None, *, startup: bool = False) -> None:
        """Show the person's history; at startup, keep a restored value
        rather than blank it when the history can't provide one."""
        description = self.entity_description
        value = description.value_fn(view) if view is not None else None
        if value is None and startup:
            return
        self._attr_native_value = value
        self._attr_extra_state_attributes = (
            description.attrs_fn(view)
            if description.attrs_fn is not None and view is not None
            else {}
        )

    @callback
    def _handle_update(self) -> None:
        self._apply(self._manager.person_view(self._person_id))
        self.async_write_ha_state()


class UnassignedReadingsSensor(SensorEntity):
    """Number of readings waiting to be assigned to a person."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "unassigned_readings"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager: ScaleManager) -> None:
        self._manager = manager
        entry = manager.entry
        self._attr_unique_id = f"{entry.entry_id}_unassigned_readings"
        self._attr_device_info = scale_device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self._manager.queue.items())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "readings": [
                {
                    "id": r.id,
                    "timestamp": r.timestamp.isoformat(),
                    "weight_kg": r.weight_kg,
                }
                for r in self._manager.queue.items()
            ]
        }

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


class ScaleSignalSensor(SensorEntity):
    """Strength of the scale's last advert at the best proxy that heard it."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "signal_strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager: ScaleManager) -> None:
        self._manager = manager
        entry = manager.entry
        self._attr_unique_id = f"{entry.entry_id}_signal_strength"
        self._attr_device_info = scale_device_info(entry)

    @property
    def native_value(self) -> int | None:
        return self._manager.rssi

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source": self._manager.source} if self._manager.source else {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_seen(self._manager.entry.entry_id),
                self.async_write_ha_state,
            )
        )


class ScaleLastSeenSensor(RestoreSensor):
    """When the scale was last heard by any proxy."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, manager: ScaleManager) -> None:
        self._manager = manager
        entry = manager.entry
        self._attr_unique_id = f"{entry.entry_id}_last_seen"
        self._attr_device_info = scale_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None and isinstance(
            data.native_value, str
        ):
            self._attr_native_value = datetime.fromisoformat(data.native_value)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_seen(self._manager.entry.entry_id),
                self._handle_seen,
            )
        )

    @callback
    def _handle_seen(self) -> None:
        self._attr_native_value = self._manager.last_seen
        self.async_write_ha_state()
