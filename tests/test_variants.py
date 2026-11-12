"""Untested scale families: synthetic adverts built from the decoded layouts.

None of these come from a real scale. They check that the parsers follow the
documented layouts, and that the rest of the integration copes with what
those scales send (other impedance handling, no formula, scale-computed body
composition).
"""

from __future__ import annotations

import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fg260_scale.assembler import WeighInAssembler
from custom_components.fg260_scale.const import DOMAIN
from custom_components.fg260_scale.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.fg260_scale.manager import compute
from custom_components.fg260_scale.models import Person
from custom_components.fg260_scale.parser import (
    FAMILY_BM02,
    FAMILY_BM15,
    FAMILY_ICOMON,
    MANUFACTURER_ID,
    identify,
    icomon_ids,
    is_icomon_id,
    parse_manufacturer_data,
    tea_decrypt,
)
from custom_components.fg260_scale.queue import PendingReading

from .test_manager import bt_register, notify  # noqa: F401  (fixtures)

MAC = bytes.fromhex("112233445566")
FFB0 = "0000ffb0-0000-1000-8000-00805f9b34fb"
ADDRESS = "B4:C1:44:33:22:11"
TEA_KEY = (0x47082001, 0xF3EF8008, 0x61C322DA, 0x1E407215)
MANIFEST = (
    Path(__file__).parent.parent / "custom_components" / "fg260_scale" / "manifest.json"
)


# -- advert builders ------------------------------------------------------------


def icomon(cmd: int, d: bytes, k: int = 0xA0, unit: int = 0, low: int = 0xAC):
    """An icomon advert: reversed MAC, then XOR'd d0..d3, cmd, checksum."""
    cks = ((sum(d) + cmd) & 0x1F) | (unit << 5)
    data = bytes(b ^ k for b in (*d, cmd, cks))
    return {(k << 8) | low: MAC[::-1] + data}


def tea_encrypt(block: bytes, key=TEA_KEY) -> bytes:
    v0, v1 = struct.unpack("<2I", block)
    k0, k1, k2, k3 = key
    total = 0
    for _ in range(32):
        total = (total + 0x9E3779B9) & 0xFFFFFFFF
        v0 = (v0 + ((((v1 << 4) + k0) ^ (v1 + total) ^ ((v1 >> 5) + k1)))) & 0xFFFFFFFF
        v1 = (v1 + ((((v0 << 4) + k2) ^ (v0 + total) ^ ((v0 >> 5) + k3)))) & 0xFFFFFFFF
    return struct.pack("<2I", v0, v1)


def bm15(state: int, weight_dg: int, adc: int = 0, temp: int = 0):
    """A BM15 advert; ``state`` 0 measuring, 1 stable, 2 impedance done."""
    status = ((state + 1) << 3) | 0x01
    plain = struct.pack(">3H2x", weight_dg, adc, temp * 10)
    payload = bytes([status]) + tea_encrypt(plain) + bytes([0x11, 0x22, 0x33, 0x44])
    return {0x01BC: payload}


def bm02(cmd: int, d: bytes, b1: int = 0x00, device: int = 0x00):
    frame = bytes([0x00, b1, *d, cmd, sum((*d, cmd)) & 0xFF])
    return {(device << 8) | 0xAC: MAC + bytes(4) + frame}


def info(mfr: dict[int, bytes], uuids: list[str] | None = None, t: float = 0):
    return BluetoothServiceInfoBleak(
        name="scale",
        address=ADDRESS,
        rssi=-60,
        manufacturer_data=mfr,
        service_data={},
        service_uuids=uuids or [],
        source="local",
        device=None,
        advertisement=None,
        connectable=False,
        time=t,
        tx_power=None,
    )


# -- recognition ------------------------------------------------------------------


def test_tea_matches_reference_vector() -> None:
    # Published TEA vector: zero key, zero block.
    zero_key = (0, 0, 0, 0)
    assert tea_encrypt(bytes(8), zero_key) == struct.pack("<2I", 0x41EA3A0A, 0x94BAA940)
    assert tea_decrypt(struct.pack("<2I", 0x41EA3A0A, 0x94BAA940), zero_key) == bytes(8)
    block = bytes(range(8))
    assert tea_decrypt(tea_encrypt(block)) == block


def test_manifest_lists_every_icomon_id() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert [m["manufacturer_id"] for m in manifest["bluetooth"]] == icomon_ids()
    assert MANUFACTURER_ID in icomon_ids()


@pytest.mark.parametrize(
    ("company_id", "expected"),
    [
        (0xA0AC, True),  # FG260: broadcast fat scale
        (0x90AC, True),  # broadcast weight scale
        (0xB0CA, True),  # broadcast balance, 0xCA prefix
        (0x20AC, False),  # connect mode
        (0xC0AC, False),  # kitchen scale
        (0xA3AC, False),  # "A4" sub-type: connect only
        (0xA0BC, False),
    ],
)
def test_icomon_ids(company_id: int, expected: bool) -> None:
    assert is_icomon_id(company_id) is expected


def test_identify_families() -> None:
    fg260 = identify(icomon(0xAD, bytes(4)))
    assert fg260 is not None and fg260.tested and fg260.model == "FG260 / AAA006"

    other = identify(icomon(0xAD, bytes(4), k=0x91))
    assert other is not None and other.family == FAMILY_ICOMON and not other.tested
    assert other.model == "iComon broadcast scale 0x91 (untested)"

    assert identify(bm15(0, 800)).family == FAMILY_BM15
    # BM15 adverts carry no service UUIDs; BM02 ones must.
    assert identify(bm15(0, 800), [FFB0]) is None
    assert identify(bm02(0xCA, bytes(4))) is None
    assert identify(bm02(0xCA, bytes(4)), [FFB0]).family == FAMILY_BM02


# -- icomon message types ------------------------------------------------------------


def test_icomon_legacy_weight_and_impedance() -> None:
    stable = parse_manufacturer_data(icomon(0xCA, bytes([0x1F, 0x40, 0, 0]), k=0x91))
    assert (stable.kind, stable.grams, stable.stable) == ("weight", 80000, True)
    live = parse_manufacturer_data(icomon(0xAE, bytes([0x1F, 0x41, 0, 0]), k=0x91))
    assert (live.kind, live.grams, live.stable) == ("weight", 80010, False)

    measure = parse_manufacturer_data(icomon(0xAB, bytes([0x1F, 0x40, 0x13, 0x88]), k=0x91))
    # 5000 > 1500, so the vendor app divides by ten.
    assert (measure.kind, measure.grams, measure.impedance) == ("impedance", 80000, 500)
    assert measure.adc_mode == "ohm" and measure.bfa_type == 0


def test_icomon_temperature_only_on_temperature_scales() -> None:
    d = bytes([0x1F, 0x40, 0x00, 0xE6])  # 23.0 °C
    assert parse_manufacturer_data(icomon(0xCA, d, k=0x91)).temperature is None
    live = parse_manufacturer_data(icomon(0xA9, d, k=0x91))
    assert (live.grams, live.stable, live.temperature) == (80000, False, 23.0)


def test_icomon_new_impedance_without_formula() -> None:
    frame = parse_manufacturer_data(icomon(0xAF, bytes([0, 72, 0x13, 0x88]), k=0x91))
    assert (frame.kind, frame.hr, frame.impedance) == ("impedance", 72, 500.0)
    assert frame.adc_mode == "ohm" and frame.bfa_type == 0


def test_icomon_balance_and_unknown_ignored() -> None:
    assert parse_manufacturer_data(icomon(0xA0, bytes([0x0F, 0xA0, 0x0F, 0xA0]))) is None
    assert parse_manufacturer_data(icomon(0x42, bytes(4))) is None


# -- BM15 -----------------------------------------------------------------------------


def test_bm15_states() -> None:
    live = parse_manufacturer_data(bm15(0, 805))
    assert (live.kind, live.grams, live.stable, live.family) == ("weight", 80500, False, FAMILY_BM15)
    stable = parse_manufacturer_data(bm15(1, 800, temp=21))
    assert (stable.kind, stable.grams, stable.stable, stable.temperature) == (
        "weight", 80000, True, 21.0,
    )
    done = parse_manufacturer_data(bm15(2, 800, adc=512))
    assert (done.kind, done.grams, done.impedance, done.adc_mode) == (
        "impedance", 80000, 512, "ohm",
    )
    no_adc = parse_manufacturer_data(bm15(2, 800))
    assert (no_adc.kind, no_adc.grams) == ("done", 80000)


# -- BM02 -----------------------------------------------------------------------------


def test_bm02_frames() -> None:
    def parse(cmd: int, d: bytes, **kw: Any):
        return parse_manufacturer_data(bm02(cmd, d, **kw), [FFB0])

    weight = parse(0xCA, bytes([0x03, 0x20, 0, 0]))  # 800 x 0.1 kg
    assert (weight.grams, weight.stable) == (80000, True)
    warm = parse(0xCE, bytes([0x03, 0x20, 0x00, 0xE6]), b1=3)
    assert (warm.stable, warm.temperature) == (False, 23.0)
    adc = parse(0xCB, bytes([0xFD, 0x01, 0x01, 0xF4]))
    assert (adc.kind, adc.impedance) == ("impedance", 500)
    assert parse(0xCB, bytes([0xFD, 0xFF, 0, 0])).kind == "done"
    fat = parse(0xCB, bytes([0xFE, 0x02, 0x00, 0xFA]))
    assert (fat.kind, fat.metric) == ("metric", ("body_fat", 25.0))
    assert parse(0xCB, bytes([0xFE, 0xFC, 0, 0])).kind == "done"


def test_bm02_bad_checksum_rejected() -> None:
    mfr = bm02(0xCA, bytes([0x03, 0x20, 0, 0]))
    (cid, payload), = mfr.items()
    broken = payload[:-1] + bytes([payload[-1] ^ 1])
    assert parse_manufacturer_data({cid: broken}, [FFB0]) is None


# -- assembler ------------------------------------------------------------------------

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _run(adverts: list[tuple[float, dict]], uuids=None, end: float = 60):
    asm = WeighInAssembler()
    out = []
    for t, mfr in adverts:
        frame = parse_manufacturer_data(mfr, uuids)
        assert frame is not None
        if (m := asm.feed(frame, T0 + timedelta(seconds=t))) is not None:
            out.append(m)
    for t in range(int(adverts[-1][0]) + 1, end):
        if (m := asm.flush(T0 + timedelta(seconds=t))) is not None:
            out.append(m)
    return out


def test_bm15_weigh_in_with_impedance() -> None:
    ms = _run(
        [(0, bm15(0, 790)), (1, bm15(1, 800)), (2, bm15(1, 800)),
         (3, bm15(2, 800, adc=512)), (4, bm15(2, 800, adc=512))]
    )
    assert len(ms) == 1
    m = ms[0]
    assert (m.weight_kg, m.impedance, m.adc_mode, m.bfa_type, m.stable) == (
        80.0, 512, "ohm", None, True,
    )


def test_bm15_finished_without_impedance() -> None:
    ms = _run([(0, bm15(1, 800)), (1, bm15(2, 800)), (2, bm15(2, 800))])
    assert [(m.weight_kg, m.impedance) for m in ms] == [(80.0, None)]


def test_bm02_scale_computed_metrics() -> None:
    def adv(cmd: int, *d: int) -> dict:
        return bm02(cmd, bytes(d))

    ms = _run(
        [(0, adv(0xCA, 0x03, 0x20, 0, 0)),
         (1, adv(0xCB, 0xFE, 0x02, 0x00, 0xFA)),  # body fat 25.0
         (1.2, adv(0xCB, 0xFE, 0x06, 0x06, 0x40)),  # bmr 1600
         (1.4, adv(0xCB, 0xFE, 0xFC, 0, 0))],
        uuids=[FFB0],
    )
    assert len(ms) == 1
    assert ms[0].weight_kg == 80.0
    assert ms[0].scale_metrics == {"body_fat": 25.0, "bmr": 1600}


# -- body composition -------------------------------------------------------------------

PERSON = Person(id="p", name="P", height_cm=180, sex="male", birth_date=T0.date().replace(year=1985))


def _reading(**kw: Any) -> PendingReading:
    return PendingReading(id="r", timestamp=T0, weight_kg=80.0, **kw)


def test_no_formula_gives_bmi_only() -> None:
    bmi, comp = compute(PERSON, _reading(impedance=500, formula=None, adc_mode="ohm"))
    assert bmi == pytest.approx(24.7, abs=0.05)
    assert comp is None


def test_wla03_with_ohms_skips_vendor_correction() -> None:
    raw = compute(PERSON, _reading(impedance=500, formula=3, adc_mode="ohm"))
    assert raw[1] is not None
    # Below the correction threshold both modes agree.
    assert raw == compute(PERSON, _reading(impedance=500, formula=3))


def test_scale_metrics_used_when_no_formula() -> None:
    bmi, comp = compute(
        PERSON,
        _reading(impedance=None, formula=None, scale_metrics={"body_fat": 25.0, "bmr": 1600}),
    )
    assert comp is not None
    assert (comp.bmi, comp.body_fat, comp.bmr, comp.muscle) == (bmi, 25.0, 1600, None)


# -- end to end ---------------------------------------------------------------------------


@pytest.fixture
def notify_services(hass: HomeAssistant) -> None:
    async def _noop(call: ServiceCall) -> None:
        return None

    hass.services.async_register("notify", "chris_phone", _noop)


async def test_untested_scale_setup_warns(
    hass: HomeAssistant, notify_services: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=info(bm15(0, 800)),
    )
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"]["model"] == (
        "iComon BM15 broadcast scale (untested)"
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "untested"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "person"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Chris", "height_cm": 180, "sex": "male",
            "birth_date": "1985-01-01", "athlete": False,
            "notify_services": ["notify.chris_phone"],
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Broadcast Body Scale"
    assert result["data"] == {
        "address": ADDRESS, "family": FAMILY_BM15, "manufacturer_id": 0x01BC,
    }


async def test_non_scale_discovery_aborts(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=info({0x004C: bytes(20)}),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_bm15_end_to_end(
    hass: HomeAssistant, bt_register: Any, notify: Any  # noqa: F811
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Broadcast Body Scale",
        data={"address": ADDRESS, "family": FAMILY_BM15, "manufacturer_id": 0x01BC},
        options={
            "tolerance_kg": 3.0,
            "people": [{
                "id": "chris", "name": "Chris", "height_cm": 180, "sex": "male",
                "birth_date": "1985-01-01", "athlete": False,
                "notify_services": ["notify.chris_phone"],
            }],
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    (device,) = [
        d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if (DOMAIN, ADDRESS) in d.identifiers
    ]
    assert device.model == "iComon BM15 broadcast scale (untested)"

    for mfr in (bm15(1, 800), bm15(2, 800, adc=512)):
        bt_register["cb"](info(mfr), BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()

    manager = entry.runtime_data
    (reading,) = manager.queue.items()
    assert (reading.weight_kg, reading.impedance, reading.formula, reading.adc_mode) == (
        80.0, 512, None, "ohm",
    )
    await manager.async_assign(reading.id, "chris")
    result = manager.last_result("chris")
    assert result.composition is None and result.bmi == pytest.approx(24.7, abs=0.05)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["scale"]["tested"] is False
    adverts = diag["scale"]["recent_adverts"]
    assert len(adverts) == 2
    # The MAC tail the scale repeats in its payload is blanked.
    assert all(a["manufacturer_data"]["444"].endswith("00000000") for a in adverts)

    await hass.config_entries.async_unload(entry.entry_id)
