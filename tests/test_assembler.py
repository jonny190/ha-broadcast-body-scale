"""Tests for the weigh-in assembler."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from custom_components.fg260_scale.assembler import Measurement, WeighInAssembler
from custom_components.fg260_scale.parser import Frame, parse_manufacturer_data

FIXTURE = Path(__file__).parent / "fixtures" / "aaa006_capture.json"
EXPECTED = [(115.62, 524), (115.05, 525), (114.82, None), (115.28, 526)]


def _timed_frames() -> list[tuple[datetime, Frame]]:
    out = []
    for rec in json.loads(FIXTURE.read_text()):
        data = {int(k): bytes.fromhex(v) for k, v in rec["manufacturer_data"].items()}
        frame = parse_manufacturer_data(data)
        assert frame is not None
        out.append((datetime.fromtimestamp(rec["time"], tz=UTC), frame))
    return out


def _replay(items: list[tuple[datetime, Frame]], repeat: int = 1) -> list[Measurement]:
    asm = WeighInAssembler()
    results: list[Measurement] = []
    for now, frame in items:
        for _ in range(repeat):
            for m in (asm.flush(now), asm.feed(frame, now)):
                if m is not None:
                    results.append(m)
    end = items[-1][0] + timedelta(seconds=30)
    if (m := asm.flush(end)) is not None:
        results.append(m)
    return results


def _pairs(ms: list[Measurement]) -> list[tuple[float, int | None]]:
    return [(m.weight_kg, m.impedance) for m in ms]


def test_capture_yields_four_measurements() -> None:
    ms = _replay(_timed_frames())
    assert _pairs(ms) == EXPECTED
    assert len(ms) == 4
    assert all(m.timestamp.tzinfo is not None for m in ms)
    assert ms[0].bfa_type == 3
    # timestamp is that of the 0xA6 frame that completed the weigh-in
    assert ms[0].timestamp == _timed_frames()[5][0]


def test_duplicate_frames_one_measurement() -> None:
    assert _pairs(_replay(_timed_frames(), repeat=2)) == EXPECTED


def test_missed_stable_uses_last_weight_before_impedance() -> None:
    items = _timed_frames()
    dropped = [
        (t, f) for i, (t, f) in enumerate(items) if i != 13
    ]  # session 2 stable frame
    ms = _replay(dropped)
    assert _pairs(ms)[1] == (115.05, 525)
    assert len(ms) == 4


def test_live_frames_only_emit_nothing() -> None:
    asm = WeighInAssembler()
    items = [(t, f) for t, f in _timed_frames() if f.kind == "weight" and not f.stable]
    for now, frame in items:
        assert asm.feed(frame, now) is None
    assert asm.flush(items[-1][0] + timedelta(seconds=30)) is None


def test_zero_gram_frames_ignored() -> None:
    items = _timed_frames()
    zero = items[0][1]
    assert zero.grams == 0
    t0 = items[0][0]
    asm = WeighInAssembler()
    assert asm.feed(zero, t0) is None
    assert asm.flush(t0 + timedelta(seconds=60)) is None
    # a zero frame mid-session does not end it
    for i in (3, 4):
        assert asm.feed(items[i][1], items[i][0]) is None
        if i == 3:
            assert asm.feed(zero, items[i][0]) is None
    m = asm.feed(items[5][1], items[5][0])
    assert m is not None and (m.weight_kg, m.impedance) == (115.62, 524)


def test_stable_weight_emits_weight_only_after_impedance_wait() -> None:
    items = _timed_frames()
    asm = WeighInAssembler()
    t, stable = items[4]
    assert asm.feed(stable, t) is None
    assert asm.flush(t + timedelta(seconds=5)) is None
    m = asm.flush(t + timedelta(seconds=6))
    assert m is not None
    assert (m.weight_kg, m.impedance, m.bfa_type) == (115.62, None, None)
    assert m.timestamp == t
    assert asm.flush(t + timedelta(seconds=60)) is None


def _live(grams: int, stable: bool = False) -> Frame:
    return Frame(kind="weight", grams=grams, stable=stable)


def _a6(impedance: int) -> Frame:
    return Frame(kind="impedance", impedance=impedance, adc_mode="fitdays", bfa_type=3)


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def _run(events: list[tuple[float, Frame]], end: float) -> list[Measurement]:
    asm = WeighInAssembler()
    out: list[Measurement] = []
    for t, frame in events:
        for m in (asm.flush(_at(t)), asm.feed(frame, _at(t))):
            if m is not None:
                out.append(m)
    if (m := asm.flush(_at(end))) is not None:
        out.append(m)
    return out


def test_late_duplicate_stable_after_emit_no_second_measurement() -> None:
    ms = _run(
        [(0, _live(100000)), (1, _live(100000, True)), (2, _a6(500)),
         (3, _live(100000, True))],
        end=60,
    )
    assert _pairs(ms) == [(100.0, 500)]


def test_continued_stable_after_weight_only_emit_is_one_measurement() -> None:
    events = [(0, _live(100000, True))] + [
        (t, _live(100000, True)) for t in (3, 7, 9, 13, 15, 19, 25, 31)
    ]
    ms = _run(events, end=90)
    assert _pairs(ms) == [(100.0, None)]


def test_interleaved_proxy_duplicates_full_fixture() -> None:
    items = _timed_frames()
    events = []
    for i, (t, f) in enumerate(items):
        events.append((t, f))
        events.append((t + timedelta(seconds=0.5 + (i % 4) * 0.5), f))
    events.sort(key=lambda e: e[0])
    asm = WeighInAssembler()
    out = []
    for t, f in events:
        for m in (asm.flush(t), asm.feed(f, t)):
            if m is not None:
                out.append(m)
    if (m := asm.flush(events[-1][0] + timedelta(seconds=30))) is not None:
        out.append(m)
    assert _pairs(out) == EXPECTED


def test_dropped_a6_falls_back_to_stable_weight() -> None:
    items = [(t, f) for i, (t, f) in enumerate(_timed_frames()) if i != 5]
    ms = _replay(items)
    assert _pairs(ms)[0] == (115.62, None)
    assert len(ms) == 4


def test_stable_weight_is_locked_against_later_live_frames() -> None:
    ms = _run([(0, _live(100000, True)), (1, _live(90000))], end=30)
    assert _pairs(ms) == [(100.0, None)]


def test_two_socks_weighins_not_collapsed() -> None:
    ms = _run(
        [(0, _live(100000, True)), (1, _a6(0)),
         (8, _live(110000, True)), (9, _a6(0))],
        end=60,
    )
    assert _pairs(ms) == [(100.0, None), (110.0, None)]


def test_gap_close_emits_stable_when_impedance_wait_is_longer() -> None:
    asm = WeighInAssembler(impedance_wait=60.0, session_gap=20.0)
    assert asm.feed(_live(100000, True), _at(0)) is None
    assert asm.flush(_at(19)) is None
    m = asm.flush(_at(20))
    assert m is not None and (m.weight_kg, m.impedance) == (100.0, None)


def test_stable_flag_true_for_stable_and_false_for_live_fallback() -> None:
    items = _timed_frames()
    assert all(m.stable for m in _replay(items))
    # Drop session 2's stable frame: the 0xA6 emit falls back to a live weight.
    dropped = [(t, f) for i, (t, f) in enumerate(items) if i != 13]
    ms = _replay(dropped)
    assert [m.stable for m in ms] == [True, False, True, True]


def test_same_weight_reweigh_after_ramp_is_a_new_measurement() -> None:
    """Stepping straight back on at the same weight (socks, no impedance)."""
    ms = _run(
        [(0, _live(100000, True)), (1, _a6(0)),
         (10, _live(60000)), (11, _live(100000, True)), (12, _a6(0))],
        end=60,
    )
    assert _pairs(ms) == [(100.0, None), (100.0, None)]


def test_same_weight_reweigh_with_missed_stable_is_a_new_measurement() -> None:
    ms = _run(
        [(0, _live(100000, True)), (1, _a6(0)),
         (10, _live(60000)), (11, _live(99000)), (12, _a6(0))],
        end=60,
    )
    assert _pairs(ms) == [(100.0, None), (99.0, None)]


def test_step_off_ramp_then_late_duplicate_is_still_one_measurement() -> None:
    """A step-off frame right after the emit must not let a late proxy
    duplicate of the stable frame through as a second weigh-in."""
    ms = _run(
        [(0, _live(100000, True)), (1, _a6(500)),
         (2, _live(40000)), (3, _live(100000, True)), (3.5, _a6(500))],
        end=60,
    )
    assert _pairs(ms) == [(100.0, 500)]
