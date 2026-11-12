"""Diagnostics download, with personal details removed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .parser import FAMILY_BM15

if TYPE_CHECKING:
    from . import FG260ConfigEntry

TO_REDACT = {"address", "name", "birth_date", "notify_services", "unique_id"}


def _hide_mac(family: str, payload_hex: str) -> str:
    """Blank the MAC bytes the scale repeats inside its payload."""
    payload = bytearray.fromhex(payload_hex)
    span = range(9, 13) if family == FAMILY_BM15 else range(0, 6)
    for i in span:
        if i < len(payload):
            payload[i] = 0
    return payload.hex()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FG260ConfigEntry
) -> dict[str, Any]:
    """Scale state for bug reports: no names, birthdays, phones or address."""
    manager = entry.runtime_data
    people = list(manager.people.values())
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "scale": {
            "last_seen": manager.last_seen.isoformat() if manager.last_seen else None,
            "rssi": manager.rssi,
            "source": manager.source,
            "family": manager.identity.family,
            "manufacturer_id": manager.identity.manufacturer_id,
            "model": manager.identity.model,
            "tested": manager.identity.tested,
            # Distinct recent adverts, so untested scales can be checked.
            "recent_adverts": [
                {
                    **advert,
                    "manufacturer_data": {
                        cid: _hide_mac(manager.identity.family, payload)
                        for cid, payload in advert["manufacturer_data"].items()
                    },
                }
                for advert in manager.raw_adverts
            ],
        },
        "unassigned": [r.as_dict() for r in manager.queue.items()],
        # People by position, not name.
        "people": [
            {
                "index": index,
                "height_cm": person.height_cm,
                "sex": person.sex,
                "athlete": person.athlete,
                "notify_service_count": len(person.notify_services),
                "results": async_redact_data(
                    [r.as_dict() for r in manager.history(person.id)[-5:]], TO_REDACT
                ),
                "result_count": len(manager.queue.history(person.id)),
            }
            for index, person in enumerate(people)
        ],
    }
