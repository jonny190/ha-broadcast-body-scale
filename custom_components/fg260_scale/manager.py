"""Turn scale adverts into per-person results, asking who it was when unsure."""

from __future__ import annotations

import logging
import math
from collections import deque
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .assembler import Measurement, WeighInAssembler
from .assignment import choose_person
from .body.composition import METHOD, BodyComposition, calculate
from .const import (
    CONF_ADDRESS,
    CONF_CONFIRM_AUTO,
    CONF_FAMILY,
    CONF_MANUFACTURER_ID,
    CONF_TOLERANCE_KG,
    DEFAULT_CONFIRM_AUTO,
    DEFAULT_TOLERANCE_KG,
    DOMAIN,
)
from .models import Person, age_on, people_from_options
from .parser import (
    FAMILY_ICOMON,
    MANUFACTURER_ID,
    ScaleIdentity,
    a6_impedance_ohms,
    parse_manufacturer_data,
)
from .queue import PendingReading, ReadingQueue

_LOGGER = logging.getLogger(__name__)

EVENT_MEASUREMENT = f"{DOMAIN}_measurement"
FLUSH_INTERVAL = timedelta(seconds=2)
ACTION_ASSIGN = "FG260_ASSIGN"
ACTION_DISCARD = "FG260_DISCARD"
ACTION_MOVE = "FG260_MOVE"
ACTION_UNASSIGN = "FG260_UNASSIGN"
NOTIFY_TITLE = "Who just weighed in?"
DISCARD_TITLE = "Guest / discard"
SOMEONE_ELSE_TITLE = "Someone else…"
NOT_ME_TITLE = "Not me"
DISCARD_OPTION = "Discard"
# Android shows at most three notification buttons.
MAX_ACTIONS = 3
ISSUE_MISSING_NOTIFY = "missing_notify_service"
KG_PER_LB = 0.45359237
RAW_ADVERT_CAP = 50
_COMPOSITION_FIELDS = [f.name for f in fields(BodyComposition)]


def signal_updated(entry_id: str) -> str:
    """Dispatcher signal sent whenever results or the queue change."""
    return f"{DOMAIN}_{entry_id}_updated"


def signal_seen(entry_id: str) -> str:
    """Dispatcher signal sent whenever the scale is heard."""
    return f"{DOMAIN}_{entry_id}_seen"


def notification_tag(reading_id: str) -> str:
    """Tag shared by every notification about one reading."""
    return f"fg260_{reading_id}"


def format_stone(weight_kg: float) -> str:
    """Format as the scale shows it in st:lb mode, e.g. ``18 st 1.6 lb``.

    Rounds to 0.1 lb first so 13.96 lb carries into the next whole stone.
    """
    tenths = round(weight_kg / KG_PER_LB * 10)
    stone, rest = divmod(tenths, 140)
    return f"{stone} st {rest / 10:.1f} lb"


def kg_to_lb(weight_kg: float) -> float:
    """Kilograms to pounds, rounded to 0.01 lb."""
    return round(weight_kg / KG_PER_LB, 2)


@dataclass(frozen=True)
class PersonResult:
    """One assigned reading. ``impedance`` etc. as in ``PendingReading``."""

    timestamp: datetime
    weight_kg: float
    bmi: float
    impedance: float | None
    composition: BodyComposition | None
    # The profile the values were computed with; None for results stored
    # before profiles were recorded.
    profile: dict[str, Any] | None = None
    # None for results stored before readings kept their id.
    reading_id: str | None = None
    formula: int | None = 3
    adc_mode: str = "fitdays"
    scale_metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reading_id": self.reading_id,
            "timestamp": self.timestamp.isoformat(),
            "weight_kg": self.weight_kg,
            "bmi": self.bmi,
            "impedance": self.impedance,
            "composition": asdict(self.composition) if self.composition else None,
            "profile": self.profile,
            "formula": self.formula,
            "adc_mode": self.adc_mode,
            "scale_metrics": self.scale_metrics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonResult:
        comp = data.get("composition")
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            weight_kg=float(data["weight_kg"]),
            bmi=float(data["bmi"]),
            impedance=data.get("impedance"),
            composition=BodyComposition(**comp) if comp else None,
            profile=data.get("profile"),
            reading_id=data.get("reading_id"),
            formula=data.get("formula", 3),
            adc_mode=data.get("adc_mode", "fitdays"),
            scale_metrics=dict(data.get("scale_metrics") or {}),
        )

    def as_reading(self) -> PendingReading:
        """Back to an unassigned reading (for undo)."""
        return PendingReading(
            id=self.reading_id or uuid.uuid4().hex,
            timestamp=self.timestamp,
            weight_kg=self.weight_kg,
            impedance=self.impedance,
            formula=self.formula,
            adc_mode=self.adc_mode,
            scale_metrics=self.scale_metrics,
        )


@dataclass(frozen=True)
class PersonView:
    """What a person's sensors show, derived from their history."""

    last: PersonResult
    # Newest result with body composition (socks readings have none).
    last_with_composition: PersonResult | None
    previous: PersonResult | None

    @property
    def change_kg(self) -> float | None:
        if self.previous is None:
            return None
        return round(self.last.weight_kg - self.previous.weight_kg, 2)


def profile_of(person: Person) -> dict[str, Any]:
    """The parts of a person that body composition depends on."""
    return {
        "height_cm": person.height_cm,
        "sex": person.sex,
        "birth_date": person.birth_date.isoformat(),
        "method": METHOD,
    }


def impedance_ohms(
    impedance: float | None, adc_mode: str, weight_kg: float
) -> float | None:
    """The impedance a formula uses, from the value the scale sent."""
    if not impedance:
        return None
    if adc_mode == "fitdays":
        return a6_impedance_ohms(impedance, weight_kg)
    return impedance


def _scale_composition(metrics: dict[str, Any], bmi: float) -> BodyComposition | None:
    """Body composition the scale computed itself; missing values are None."""
    values = {name: metrics.get(name) for name in _COMPOSITION_FIELDS}
    if values.get("body_fat") is None:
        return None
    values["bmi"] = bmi
    return BodyComposition(**values)


def compute(
    person: Person, reading: PendingReading | PersonResult
) -> tuple[float, BodyComposition | None]:
    """BMI and body composition of one reading for one person.

    Age is taken on the reading's local date, as the person sees it. Without
    an impedance the result is BMI only, unless the scale computed its own
    body composition.
    """
    bmi, composition = calculate(
        reading.weight_kg,
        person.height_cm,
        age_on(person.birth_date, dt_util.as_local(reading.timestamp).date()),
        person.sex,
        impedance_ohms(reading.impedance, reading.adc_mode, reading.weight_kg),
    )
    if composition is None and reading.scale_metrics:
        composition = _scale_composition(reading.scale_metrics, bmi)
    return bmi, composition


def entry_identity(entry: ConfigEntry) -> ScaleIdentity:
    """The scale family a config entry was set up for."""
    return ScaleIdentity(
        entry.data.get(CONF_FAMILY, FAMILY_ICOMON),
        int(entry.data.get(CONF_MANUFACTURER_ID, MANUFACTURER_ID)),
    )


def _issue_id(entry_id: str, service: str) -> str:
    return f"{ISSUE_MISSING_NOTIFY}_{entry_id}_{service}"


class ScaleManager:
    """Owns one scale: bluetooth intake, matching, queue and notifications."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.queue = ReadingQueue(hass, entry.entry_id)
        self._assembler = WeighInAssembler()
        self._unsubs: list[CALLBACK_TYPE] = []
        self.last_seen: datetime | None = None
        self.rssi: int | None = None
        self.source: str | None = None
        self.identity = entry_identity(entry)
        # Recent distinct adverts, for the diagnostics download: lets owners
        # of untested scales send real data to confirm or fix support.
        self.raw_adverts: deque[dict[str, Any]] = deque(maxlen=RAW_ADVERT_CAP)

    # -- lifecycle -------------------------------------------------------

    async def async_start(self) -> None:
        """Load persisted state, then start listening to the scale."""
        await self.queue.load()
        await self._async_recalculate_changed_profiles()
        self._unsubs.append(
            bluetooth.async_register_callback(
                self.hass,
                self._async_on_advertisement,
                bluetooth.BluetoothCallbackMatcher(address=self.address),
                bluetooth.BluetoothScanningMode.PASSIVE,
                # A replayed cached frame would look like a fresh weigh-in.
                replay=bluetooth.BluetoothCallbackReplay.DISABLED,
            )
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_flush, FLUSH_INTERVAL)
        )
        # Notify services (mobile_app) may register after us: check once
        # Home Assistant has finished starting.
        self._unsubs.append(async_at_started(self.hass, self._async_check_notify))

    async def async_stop(self) -> None:
        """Stop listening."""
        while self._unsubs:
            self._unsubs.pop()()

    # -- people and results ----------------------------------------------

    @property
    def people(self) -> dict[str, Person]:
        """People from the entry's current options."""
        return people_from_options(self.entry.options)

    def history(self, person_id: str) -> list[PersonResult]:
        """A person's results, oldest first."""
        return [PersonResult.from_dict(d) for d in self.queue.history(person_id)]

    def last_result(self, person_id: str) -> PersonResult | None:
        """Return a person's latest result, if any."""
        results = self.queue.history(person_id)
        return PersonResult.from_dict(results[-1]) if results else None

    def person_view(self, person_id: str) -> PersonView | None:
        """What a person's sensors show, or None with no results."""
        results = self.history(person_id)
        if not results:
            return None
        return PersonView(
            last=results[-1],
            last_with_composition=next(
                (r for r in reversed(results) if r.composition is not None), None
            ),
            previous=results[-2] if len(results) > 1 else None,
        )

    def has_reading(self, reading_id: str) -> bool:
        """True if a reading is queued or held by someone."""
        return (
            self.queue.get(reading_id) is not None
            or self.queue.find_result(reading_id) is not None
        )

    def resolve_person(self, person: str) -> Person:
        """Find a person by id, or by name (case-insensitive)."""
        people = self.people
        if person in people:
            return people[person]
        folded = person.casefold()
        for candidate in people.values():
            if candidate.name.casefold() == folded:
                return candidate
        raise ServiceValidationError(f"Unknown person: {person}")

    # -- profiles ------------------------------------------------------------

    async def _async_recalculate_changed_profiles(self) -> None:
        """Recompute results whose person's profile has since changed."""
        for person in self.people.values():
            profile = profile_of(person)
            if any(r.profile != profile for r in self.history(person.id)):
                await self._async_recalculate(person)

    async def _async_recalculate(self, person: Person) -> None:
        """Recompute a person's results with their given profile.

        Results computed with another profile or method (or stored before
        either was recorded) are recalculated from their weight and impedance.
        """
        profile = profile_of(person)
        updated = []
        for result in self.history(person.id):
            if result.profile != profile:
                bmi, composition = compute(person, result)
                result = replace(
                    result, bmi=bmi, composition=composition, profile=profile
                )
            updated.append(result.as_dict())
        await self.queue.set_history(person.id, updated)
        _LOGGER.debug("Recalculated %s's results", person.id)

    async def async_set_height(self, person_id: str, height_cm: int) -> None:
        """Save a new height and recalculate the person's results.

        Results are recalculated here so the sensors update at once, before
        the reload that the options change triggers.
        """
        person = self.people.get(person_id)
        if person is None:
            raise ServiceValidationError(f"Unknown person: {person_id}")
        await self._async_recalculate(replace(person, height_cm=height_cm))
        people = [
            {**raw, "height_cm": height_cm} if raw["id"] == person_id else raw
            for raw in self.entry.options.get("people", [])
        ]
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, "people": people}
        )

    # -- intake ------------------------------------------------------------

    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        self.last_seen = dt_util.utcnow()
        self.rssi = service_info.rssi
        self.source = service_info.source
        async_dispatcher_send(self.hass, signal_seen(self.entry.entry_id))
        self._record_raw(service_info)
        frame = parse_manufacturer_data(
            service_info.manufacturer_data, service_info.service_uuids
        )
        if frame is None:
            return
        if (measurement := self._assembler.feed(frame, dt_util.utcnow())) is not None:
            self._schedule(measurement)

    @callback
    def _record_raw(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        data = {
            str(cid): payload.hex()
            for cid, payload in service_info.manufacturer_data.items()
        }
        if self.raw_adverts and self.raw_adverts[-1]["manufacturer_data"] == data:
            return
        self.raw_adverts.append(
            {
                "time": dt_util.utcnow().isoformat(),
                "manufacturer_data": data,
                "service_uuids": list(service_info.service_uuids),
            }
        )

    @callback
    def _async_flush(self, now: datetime) -> None:
        if (measurement := self._assembler.flush(dt_util.utcnow())) is not None:
            self._schedule(measurement)

    @callback
    def _schedule(self, measurement: Measurement) -> None:
        self.entry.async_create_task(
            self.hass,
            self._async_handle_measurement(measurement),
            "fg260_scale measurement",
        )

    def _last_weights(self) -> dict[str, float | None]:
        return {
            pid: (result.weight_kg if (result := self.last_result(pid)) else None)
            for pid in self.people
        }

    async def _async_handle_measurement(self, measurement: Measurement) -> None:
        reading = PendingReading(
            id=uuid.uuid4().hex,
            timestamp=measurement.timestamp,
            weight_kg=measurement.weight_kg,
            impedance=measurement.impedance,
            formula=measurement.bfa_type,
            adc_mode=measurement.adc_mode,
            scale_metrics=measurement.scale_metrics,
        )
        people = self.people
        tolerance = float(
            self.entry.options.get(CONF_TOLERANCE_KG, DEFAULT_TOLERANCE_KG)
        )
        # A live-only weight may be mid-ramp: never auto-assign it.
        person_id = (
            choose_person(reading.weight_kg, self._last_weights(), tolerance)
            if measurement.stable
            else None
        )
        if person_id is not None:
            _LOGGER.debug("Reading %s matched %s", reading.weight_kg, person_id)
            person = people[person_id]
            await self._async_apply(reading, person)
            self._signal()
            if self.entry.options.get(CONF_CONFIRM_AUTO, DEFAULT_CONFIRM_AUTO):
                await self._async_confirm(reading, person)
            return

        await self._async_queue_and_ask(reading)

    async def _async_queue_and_ask(self, reading: PendingReading) -> None:
        dropped = await self.queue.add(reading)
        self._signal()
        await self._async_ask(reading)
        for old in dropped:
            _LOGGER.info("Unassigned queue full; dropped reading %s", old.id)
            await self._async_clear(old.id)

    # -- assignment ----------------------------------------------------------

    async def async_assign(self, reading_id: str, person_id: str) -> None:
        """Assign a reading to a person (id or name).

        A queued reading is assigned; one already held by someone is moved.
        """
        person = self.resolve_person(person_id)
        if (reading := await self.queue.pop(reading_id)) is not None:
            await self._async_apply(reading, person)
            self._signal()
            await self._async_clear(reading_id)
            return
        await self.async_move(reading_id, person.id)

    async def async_move(self, reading_id: str, person_id: str) -> None:
        """Move an assigned reading to another person."""
        person = self.resolve_person(person_id)
        found = self.queue.find_result(reading_id)
        if found is None:
            raise ServiceValidationError(f"Unknown reading: {reading_id}")
        if found[0] != person.id:
            await self.queue.remove_result(reading_id)
            await self._async_apply(
                PersonResult.from_dict(found[1]).as_reading(), person
            )
            self._signal()
        await self._async_clear(reading_id)

    async def async_unassign(self, reading_id: str) -> None:
        """Take a reading back from whoever holds it and ask who it was."""
        removed = await self.queue.remove_result(reading_id)
        if removed is None:
            raise ServiceValidationError(f"Unknown reading: {reading_id}")
        await self._async_queue_and_ask(PersonResult.from_dict(removed[1]).as_reading())

    async def async_discard(self, reading_id: str) -> None:
        """Drop a reading, queued or already assigned."""
        if (
            await self.queue.pop(reading_id) is None
            and await self.queue.remove_result(reading_id) is None
        ):
            raise ServiceValidationError(f"Unknown reading: {reading_id}")
        self._signal()
        await self._async_clear(reading_id)

    async def async_assign_latest(self, choice: str) -> None:
        """Assign (or discard) the newest unassigned reading."""
        items = self.queue.items()
        if not items:
            raise ServiceValidationError("There are no unassigned readings")
        reading_id = items[-1].id
        if choice == DISCARD_OPTION:
            await self.async_discard(reading_id)
        else:
            await self.async_assign(reading_id, choice)

    async def _async_apply(self, reading: PendingReading, person: Person) -> None:
        """Compute the person's result, store it, and fire the event."""
        bmi, composition = compute(person, reading)
        result = PersonResult(
            timestamp=reading.timestamp,
            weight_kg=reading.weight_kg,
            bmi=bmi,
            impedance=reading.impedance,
            composition=composition,
            profile=profile_of(person),
            reading_id=reading.id,
            formula=reading.formula,
            adc_mode=reading.adc_mode,
            scale_metrics=reading.scale_metrics,
        )
        await self.queue.add_result(person.id, result.as_dict())

        data: dict[str, Any] = {
            "reading_id": reading.id,
            "person_id": person.id,
            "person_name": person.name,
            "timestamp": reading.timestamp.isoformat(),
            "weight_kg": reading.weight_kg,
            "impedance": reading.impedance,
            "bmi": bmi,
        }
        comp = asdict(composition) if composition else {}
        for name in _COMPOSITION_FIELDS:
            if name != "bmi":
                data[name] = comp.get(name)
        self.hass.bus.async_fire(EVENT_MEASUREMENT, data)

    @callback
    def _signal(self) -> None:
        async_dispatcher_send(self.hass, signal_updated(self.entry.entry_id))

    # -- notifications ------------------------------------------------------

    def _notify_services(self, people: list[Person]) -> list[str]:
        """Every person's notify services, deduplicated, in order."""
        services: dict[str, None] = {}
        for person in people:
            for service in person.notify_services:
                services[service.removeprefix("notify.")] = None
        return list(services)

    def _missing_notify(self, service: str, person_names: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _issue_id(self.entry.entry_id, service),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_MISSING_NOTIFY,
            translation_placeholders={
                "service": f"notify.{service}",
                "people": person_names,
            },
        )

    def _users_of(self, service: str) -> str:
        return ", ".join(
            p.name
            for p in self.people.values()
            if service in {s.removeprefix("notify.") for s in p.notify_services}
        )

    @callback
    def _async_check_notify(self, _hass: HomeAssistant | None = None) -> None:
        """Raise or clear a repair for each person's notify service."""
        for service in self._notify_services(list(self.people.values())):
            if self.hass.services.has_service("notify", service):
                ir.async_delete_issue(
                    self.hass, DOMAIN, _issue_id(self.entry.entry_id, service)
                )
            else:
                self._missing_notify(service, self._users_of(service))

    async def _async_notify(self, services: list[str], data: dict[str, Any]) -> None:
        for service in services:
            if not self.hass.services.has_service("notify", service):
                _LOGGER.warning("notify.%s does not exist", service)
                self._missing_notify(service, self._users_of(service))
                continue
            ir.async_delete_issue(
                self.hass, DOMAIN, _issue_id(self.entry.entry_id, service)
            )
            try:
                await self.hass.services.async_call(
                    "notify", service, data, blocking=True
                )
            except Exception as err:  # noqa: BLE001 - one bad target must not stop the rest
                _LOGGER.warning("Could not call notify.%s: %s", service, err)

    def _nearest(self, weight_kg: float, people: list[Person]) -> list[Person]:
        """People ordered by how close their last weight is (unknown last)."""
        last = self._last_weights()

        def distance(person: Person) -> float:
            value = last.get(person.id)
            return abs(weight_kg - value) if value is not None else math.inf

        return sorted(people, key=distance)

    def _scale_device_uri(self) -> str:
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.entry.unique_id or self.entry.entry_id)}
        )
        if device is None:
            return "/config/integrations/integration/" + DOMAIN
        return f"/config/devices/device/{device.id}"

    @staticmethod
    def _describe(reading: PendingReading) -> str:
        when = dt_util.as_local(reading.timestamp).strftime("%H:%M")
        return (
            f"{format_stone(reading.weight_kg)} "
            f"({reading.weight_kg:.2f} kg) at {when}"
        )

    async def _async_ask(self, reading: PendingReading) -> None:
        people = list(self.people.values())
        if len(people) + 1 <= MAX_ACTIONS:
            actions = [
                {"action": f"{ACTION_ASSIGN}|{reading.id}|{p.id}", "title": p.name}
                for p in people
            ]
            actions.append(
                {"action": f"{ACTION_DISCARD}|{reading.id}", "title": DISCARD_TITLE}
            )
        else:
            nearest = self._nearest(reading.weight_kg, people)[: MAX_ACTIONS - 1]
            actions = [
                {"action": f"{ACTION_ASSIGN}|{reading.id}|{p.id}", "title": p.name}
                for p in nearest
            ]
            actions.append(
                {
                    "action": "URI",
                    "title": SOMEONE_ELSE_TITLE,
                    "uri": self._scale_device_uri(),
                }
            )
        await self._async_notify(
            self._notify_services(people),
            {
                "title": NOTIFY_TITLE,
                "message": self._describe(reading),
                "data": {"tag": notification_tag(reading.id), "actions": actions},
            },
        )

    async def _async_confirm(self, reading: PendingReading, person: Person) -> None:
        """Tell the matched person, with buttons to move or undo it."""
        others = [p for p in self.people.values() if p.id != person.id]
        actions = [
            {
                "action": f"{ACTION_MOVE}|{reading.id}|{p.id}",
                "title": f"It was {p.name}",
            }
            for p in self._nearest(reading.weight_kg, others)[: MAX_ACTIONS - 1]
        ]
        actions.append(
            {"action": f"{ACTION_UNASSIGN}|{reading.id}", "title": NOT_ME_TITLE}
        )
        message = self._describe(reading)
        view = self.person_view(person.id)
        if view is not None and view.previous is not None and view.change_kg is not None:
            change_lb = view.change_kg / KG_PER_LB
            since = dt_util.as_local(view.previous.timestamp).strftime("%-d %b")
            message += f", {'+' if change_lb >= 0 else '−'}{abs(change_lb):.1f} lb since {since}"
        await self._async_notify(
            self._notify_services([person]),
            {
                "title": f"Saved to {person.name}",
                "message": message,
                "data": {"tag": notification_tag(reading.id), "actions": actions},
            },
        )

    async def _async_clear(self, reading_id: str) -> None:
        await self._async_notify(
            self._notify_services(list(self.people.values())),
            {
                "message": "clear_notification",
                "data": {"tag": notification_tag(reading_id)},
            },
        )
