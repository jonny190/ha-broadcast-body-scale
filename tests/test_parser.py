"""Tests for the AAA006 broadcast frame parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.fg260_scale.parser import (
    MANUFACTURER_ID,
    Frame,
    parse_manufacturer_data,
)

FIXTURE = Path(__file__).parent / "fixtures" / "aaa006_capture.json"


def _records() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def _frames() -> list[Frame]:
    frames = []
    for rec in _records():
        data = {int(k): bytes.fromhex(v) for k, v in rec["manufacturer_data"].items()}
        frame = parse_manufacturer_data(data)
        if frame is not None:
            frames.append(frame)
    return frames


def _stable_weights() -> list[Frame]:
    return [f for f in _frames() if f.kind == "weight" and f.stable]


def _impedance() -> list[Frame]:
    return [f for f in _frames() if f.kind == "impedance"]


def _payload(index: int = 4) -> bytes:
    """Raw payload of the fixture record at index (a stable weight frame)."""
    return bytes.fromhex(_records()[index]["manufacturer_data"]["41132"])


def test_every_fixture_record_parses() -> None:
    assert len(_frames()) == len(_records())


def test_stable_weights_match_display() -> None:
    frames = _stable_weights()
    assert [f.grams for f in frames] == [115620, 115050, 114820, 115280]
    assert all(f.stable is True for f in frames)


def test_impedance_frames() -> None:
    frames = _impedance()
    assert [f.impedance for f in frames if f.impedance] == [524, 525, 526]
    assert [f.impedance for f in frames] == [524, 525, 0, 526]
    assert all(f.bfa_type == 3 for f in frames)


def test_scale_division_fields() -> None:
    frame = _stable_weights()[0]
    assert frame.kg_division == 2
    assert frame.lb_division == 3


def test_bad_checksum_rejected() -> None:
    payload = bytearray(_payload())
    payload[7] ^= 0x01  # flip one data bit
    assert parse_manufacturer_data({MANUFACTURER_ID: bytes(payload)}) is None


def test_wrong_or_short_payload() -> None:
    payload = _payload()
    assert parse_manufacturer_data({76: payload}) is None
    assert parse_manufacturer_data({MANUFACTURER_ID: payload[:-1]}) is None
    assert parse_manufacturer_data({}) is None
    # unknown type byte (data re-checksummed so only the type is at fault)
    key = MANUFACTURER_ID >> 8 & 0xFF
    d = [0x00, 0x69, 0xC3, 0xA4]
    typ = 0x11
    cks = (sum(d) + typ) & 0x1F
    bad = payload[:6] + bytes(b ^ key for b in (*d, typ, cks))
    assert parse_manufacturer_data({MANUFACTURER_ID: bad}) is None
