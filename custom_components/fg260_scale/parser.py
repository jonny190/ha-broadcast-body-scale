"""Parsers for broadcast body scales' passive BLE manufacturer data.

Three families are decoded (layouts in docs/protocol.md):

* ``icomon``: iComon broadcast scales (the FG260/AAA006 and relatives).
  Company ID low byte 0xAC or 0xCA; the high byte describes the scale and is
  also the XOR key. Only the FG260 (0xA0AC) has been tested on a real scale.
* ``bm15``: scales whose company ID low byte is 0xBC, with the readings TEA
  encrypted under a fixed key. Untested.
* ``bm02``: older iComon scales (company ID 0x00AC/0x01AC) that embed a
  connect-protocol frame in the advert. Untested.

HA presents manufacturer data as ``{company_id: payload}``, where the company
ID is the first two on-air bytes (little-endian) and ``payload`` the rest.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

# The tested scale: FG260 / AAA006, on air "AC A0".
MANUFACTURER_ID = 0xA0AC

FAMILY_ICOMON = "icomon"
FAMILY_BM15 = "bm15"
FAMILY_BM02 = "bm02"

FAMILY_NAMES = {
    FAMILY_ICOMON: "iComon broadcast scale",
    FAMILY_BM15: "iComon BM15 broadcast scale",
    FAMILY_BM02: "iComon BM02 broadcast scale",
}

# Service UUIDs the vendor app requires on icomon/BM02 scale adverts.
SCALE_SERVICE_UUIDS = frozenset(
    f"0000{short}-0000-1000-8000-00805f9b34fb" for short in ("ffb0", "feb0", "c0ff")
)

_ICOMON_LOW_BYTES = (0xAC, 0xCA)
_BM15_LOW_BYTE = 0xBC
_BM02_DEVICE_BYTES = (0x00, 0x01)

_MAC_LEN = 6
_DATA_LEN = 6
_ICOMON_LEN = _MAC_LEN + _DATA_LEN
_BM15_LEN = 13
_BM02_LEN = 18
_BM02_FRAME = slice(10, 18)

# Impedance handling: "fitdays" (a stored value, kept for compatibility)
# means an 0xA6 raw value that a6_impedance_ohms converts; "ohm" means the
# value is already in ohms.
AdcMode = Literal["fitdays", "ohm"]

FrameKind = Literal["weight", "impedance", "done", "metric"]

# BM02 scale-computed body composition: index -> (field, divisor).
BM02_METRICS: dict[int, tuple[str, int]] = {
    0: ("weight", 10),
    1: ("bmi", 10),
    2: ("body_fat", 10),
    3: ("subcutaneous_fat", 10),
    4: ("visceral_fat", 1),
    5: ("muscle", 10),
    6: ("bmr", 1),
    7: ("bone", 10),
    8: ("water", 10),
    9: ("body_age", 1),
    10: ("protein", 10),
}

_TEA_KEY = (0x47082001, 0xF3EF8008, 0x61C322DA, 0x1E407215)
_TEA_DELTA = 0x9E3779B9
_MASK32 = 0xFFFFFFFF


@dataclass(frozen=True)
class Frame:
    """One decoded broadcast frame.

    ``weight``: a weight, ``stable`` once the scale has locked it.
    ``impedance``: the impedance result; ``grams`` is set when the frame also
    carries the weight. ``done``: the scale finished without impedance.
    ``metric``: one scale-computed body composition value (BM02).
    """

    kind: FrameKind
    grams: int = 0
    stable: bool = False
    impedance: float = 0
    adc_mode: AdcMode = "ohm"
    hr: int = 0
    # Wire body-fat formula number (WLA01..WLA44); 0 when the scale sends none.
    bfa_type: int = 0
    unit_bits: int = 0
    # 0xAD display-step codes (see docs): 0 -> 0.01, 1 -> 0.02, 2 -> 0.05,
    # 3 -> 0.1, 4 -> 0.2 steps.
    kg_division: int = 0
    lb_division: int = 0
    temperature: float | None = None
    metric: tuple[str, float] | None = None
    family: str = FAMILY_ICOMON


@dataclass(frozen=True)
class ScaleIdentity:
    """What kind of scale an advert comes from."""

    family: str
    manufacturer_id: int

    @property
    def tested(self) -> bool:
        return self.family == FAMILY_ICOMON and self.manufacturer_id == MANUFACTURER_ID

    @property
    def model(self) -> str:
        if self.tested:
            return "FG260 / AAA006"
        name = FAMILY_NAMES[self.family]
        if self.family == FAMILY_ICOMON:
            name = f"{name} 0x{self.manufacturer_id >> 8:02X}"
        return f"{name} (untested)"


def is_icomon_id(company_id: int) -> bool:
    """True for an iComon broadcast company ID the vendor app decodes."""
    low, k = company_id & 0xFF, company_id >> 8
    category, sub = (k >> 4) & 7, k & 0x0F
    return (
        low in _ICOMON_LOW_BYTES
        and bool(k & 0x80)
        and category in (1, 2, 3)  # weight scale, fat scale, balance
        and not 3 <= sub <= 6  # "A4" scales: connect only
    )


def icomon_ids() -> list[int]:
    """Every icomon company ID, for the manifest's discovery matchers."""
    return [cid for cid in range(0x10000) if is_icomon_id(cid)]


def _has_scale_uuid(service_uuids: Iterable[str] | None) -> bool:
    return any(u.lower() in SCALE_SERVICE_UUIDS for u in service_uuids or ())


def identify(
    manufacturer_data: Mapping[int, bytes], service_uuids: Iterable[str] | None = None
) -> ScaleIdentity | None:
    """Which supported scale family sent this advert, if any."""
    uuids = list(service_uuids or ())
    for company_id, payload in manufacturer_data.items():
        if is_icomon_id(company_id) and len(payload) >= _ICOMON_LEN:
            return ScaleIdentity(FAMILY_ICOMON, company_id)
        if (
            company_id & 0xFF in _ICOMON_LOW_BYTES
            and company_id >> 8 in _BM02_DEVICE_BYTES
            and len(payload) >= _BM02_LEN
            and _has_scale_uuid(uuids)
        ):
            return ScaleIdentity(FAMILY_BM02, company_id)
        if (
            company_id & 0xFF == _BM15_LOW_BYTE
            and len(payload) == _BM15_LEN
            and not uuids
        ):
            return ScaleIdentity(FAMILY_BM15, company_id)
    return None


def parse_manufacturer_data(
    manufacturer_data: Mapping[int, bytes], service_uuids: Iterable[str] | None = None
) -> Frame | None:
    """Decode a frame from HA manufacturer data, or None if it is not one."""
    identity = identify(manufacturer_data, service_uuids)
    if identity is None:
        return None
    payload = manufacturer_data[identity.manufacturer_id]
    if identity.family == FAMILY_ICOMON:
        return _parse_icomon(identity.manufacturer_id >> 8, payload)
    if identity.family == FAMILY_BM02:
        return _parse_bm02(payload[_BM02_FRAME])
    return _parse_bm15(payload)


# -- icomon -------------------------------------------------------------------


def a6_impedance_ohms(adc: float, weight_kg: float) -> float:
    """Impedance in ohms from an 0xA6 frame's raw value.

    Small values are already ohms; large ones are scaled using the weight.
    """
    value = adc
    if value >= 1500:
        value = ((value - 1000) - weight_kg * 4) / 0.6 / 10
    if value > 1500:
        value /= 10
    return value


def _vendor_adc(adc: float) -> float:
    """The vendor app's scaling for impedance from non-0xA6 frames."""
    return adc / 10 if adc > 1500 else adc


def _parse_icomon(k: int, payload: bytes) -> Frame | None:
    d0, d1, d2, d3, cmd, cks = (b ^ k for b in payload[_MAC_LEN:_ICOMON_LEN])
    if (d0 + d1 + d2 + d3 + cmd) & 0x1F != cks & 0x1F:
        return None
    unit = cks >> 5
    hi, lo = (d0 << 8) | d1, (d2 << 8) | d3

    if cmd == 0xAD:  # new-style weight (the FG260)
        v = (d0 << 24) | (d1 << 16) | (d2 << 8) | d3
        return Frame(
            kind="weight",
            grams=v & 0x3FFFF,
            stable=bool(v >> 31),
            unit_bits=unit,
            kg_division=(v >> 18) & 7,
            lb_division=(v >> 21) & 7,
        )
    if cmd == 0xA6:  # new-style impedance + HR + formula (the FG260)
        return Frame(
            kind="impedance",
            impedance=hi,
            adc_mode="fitdays",
            hr=d2,
            bfa_type=d3,
            unit_bits=unit,
        )
    if cmd == 0xAF:  # new-style impedance + HR, no formula
        return Frame(
            kind="impedance",
            impedance=_vendor_adc(lo / 10),
            hr=d1,
            unit_bits=unit,
        )
    if cmd in (0xCA, 0xAA, 0xCE, 0xAE):  # legacy weight, 0.01 kg
        temperature = (lo & 0x0FFF) / 10 if cmd in (0xCA, 0xCE) and k == 0x01 else None
        return Frame(
            kind="weight",
            grams=hi * 10,
            stable=cmd in (0xCA, 0xAA),
            unit_bits=unit,
            temperature=temperature,
        )
    if cmd == 0xA9:  # live weight + temperature
        return Frame(
            kind="weight",
            grams=hi * 10,
            unit_bits=unit,
            temperature=(lo & 0x0FFF) / 10,
        )
    if cmd in (0xAB, 0xCB):  # legacy weight + impedance
        return Frame(
            kind="impedance",
            grams=hi * 10,
            impedance=_vendor_adc(lo),
            unit_bits=unit,
        )
    return None  # 0xA0/0xA4 balance split, 0xCC settings echo, unknown


# -- BM15 ---------------------------------------------------------------------


def tea_decrypt(block: bytes, key: tuple[int, int, int, int] = _TEA_KEY) -> bytes:
    """Decrypt one 8-byte TEA block (little-endian words, 32 rounds)."""
    v0, v1 = struct.unpack("<2I", block)
    k0, k1, k2, k3 = key
    total = (_TEA_DELTA * 32) & _MASK32
    for _ in range(32):
        v1 = (v1 - ((((v0 << 4) + k2) ^ (v0 + total) ^ ((v0 >> 5) + k3)))) & _MASK32
        v0 = (v0 - ((((v1 << 4) + k0) ^ (v1 + total) ^ ((v1 >> 5) + k1)))) & _MASK32
        total = (total - _TEA_DELTA) & _MASK32
    return struct.pack("<2I", v0, v1)


def _parse_bm15(payload: bytes) -> Frame | None:
    status = payload[0]
    plain = tea_decrypt(payload[1:9])
    weight, adc, temp = struct.unpack(">3H", plain[:6])
    r = (status >> 3) & 0x1F
    if r & 0x1E == 0:
        r = 1
    state = r - 1
    grams = weight * 100
    common = {"family": FAMILY_BM15, "temperature": float(temp // 10) or None}
    if grams == 0:
        return None
    if state == 0:
        return Frame(kind="weight", grams=grams, **common)
    if state == 1:
        return Frame(kind="weight", grams=grams, stable=True, **common)
    if state == 2:
        if adc > 0:
            return Frame(kind="impedance", grams=grams, impedance=adc, **common)
        return Frame(kind="done", grams=grams, **common)
    return None


# -- BM02 ---------------------------------------------------------------------


def _parse_bm02(frame: bytes) -> Frame | None:
    if frame[0] == 0xAE:  # alternate framing, layout unknown
        return None
    b1, d0, d1, d2, d3, cmd, check = frame[1:8]
    if sum(frame[2:7]) & 0xFF != check:
        return None
    hi, lo = (d0 << 8) | d1, (d2 << 8) | d3
    family = {"family": FAMILY_BM02}

    if cmd in (0xCA, 0xCE):  # 0.1 kg
        temperature = (lo & 0x0FFF) / 10 if b1 == 3 else None
        return Frame(
            kind="weight", grams=hi * 100, stable=cmd == 0xCA,
            temperature=temperature, **family,
        )
    if cmd in (0xAA, 0xAE):  # 0.01 kg
        return Frame(kind="weight", grams=hi * 10, stable=cmd == 0xAA, **family)
    if cmd == 0xAB:
        return Frame(kind="impedance", grams=hi * 10, impedance=lo, **family)
    if cmd == 0xCB:
        if d0 == 0xFD:
            if d1 == 0x01 and lo:
                return Frame(kind="impedance", impedance=lo, **family)
            if d1 == 0xFF:
                return Frame(kind="done", **family)
            return None
        if d0 == 0xFE:
            if d1 == 0xFC:
                return Frame(kind="done", **family)
            if (spec := BM02_METRICS.get(d1)) is not None:
                name, divisor = spec
                return Frame(kind="metric", metric=(name, lo / divisor), **family)
    return None

