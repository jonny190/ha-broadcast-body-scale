"""Assemble complete weigh-ins from the scale's broadcast frames.

Pure logic with no Home Assistant imports. The caller feeds every parsed
advert to ``feed`` and calls ``flush`` on a timer (about every 2 s).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .parser import AdcMode, Frame


@dataclass(frozen=True)
class Measurement:
    """One completed weigh-in."""

    timestamp: datetime
    weight_kg: float
    impedance: float | None
    bfa_type: int | None
    # True when the weight came from a stable frame, False when it fell back
    # to the last live (possibly mid-ramp) weight.
    stable: bool
    adc_mode: AdcMode = "fitdays"
    # Body composition the scale computed itself (BM02), by field name.
    scale_metrics: dict[str, Any] = field(default_factory=dict)


class WeighInAssembler:
    """Turn a stream of weight and impedance frames into measurements.

    Timestamps: an 0xA6 emit uses the 0xA6 frame's time; a weight-only emit
    (impedance_wait or session_gap timeout) uses the stable frame's time.
    """

    def __init__(
        self,
        impedance_wait: float = 6.0,
        session_gap: float = 20.0,
        reweigh_after: float = 5.0,
    ) -> None:
        self._impedance_wait = timedelta(seconds=impedance_wait)
        self._session_gap = timedelta(seconds=session_gap)
        # A live (ramping) frame at a new weight this long after an emit means
        # someone stepped back on: forget the dedupe state so an identical
        # weight still counts. Earlier ramps are step-off frames.
        self._reweigh_after = timedelta(seconds=reweigh_after)
        self._last_emit_time: datetime | None = None
        self._reset()
        # Dedupe state: survives session resets. _last_emit_seen is refreshed by
        # every ignored repeat so a long run of stable frames stays suppressed.
        self._last_emit_grams: int | None = None
        self._last_emit_impedance: int | None = None
        self._last_emit_seen: datetime | None = None

    def _reset(self) -> None:
        self._active = False
        self._live_grams = 0
        self._stable_grams: int | None = None
        self._last_frame_time: datetime | None = None
        self._stable_time: datetime | None = None
        self._metrics: dict[str, Any] = {}

    def _session_grams(self) -> int:
        """The locked stable weight, else the last live weight."""
        return self._stable_grams if self._stable_grams is not None else self._live_grams

    def _emit(
        self,
        timestamp: datetime,
        seen: datetime,
        impedance: float | None,
        bfa_type: int | None,
        adc_mode: AdcMode = "fitdays",
    ) -> Measurement:
        grams = self._session_grams()
        stable = self._stable_grams is not None
        measurement = Measurement(
            timestamp=timestamp,
            weight_kg=round(grams / 1000, 2),
            impedance=impedance or None,
            bfa_type=bfa_type,
            stable=stable,
            adc_mode=adc_mode,
            scale_metrics=dict(self._metrics),
        )
        self._last_emit_grams = grams
        self._last_emit_impedance = impedance
        self._last_emit_seen = seen
        self._last_emit_time = seen
        self._reset()
        return measurement

    def _recent_emit(self, now: datetime) -> bool:
        return (
            self._last_emit_seen is not None
            and now - self._last_emit_seen < self._session_gap
        )

    def feed(self, frame: Frame, now: datetime) -> Measurement | None:
        """Process one frame; return a Measurement if it completes a weigh-in."""
        held = None
        if (
            self._active
            and self._last_frame_time is not None
            and now - self._last_frame_time >= self._session_gap
        ):
            held = self.flush(now)

        if frame.kind == "weight":
            if frame.grams == 0:
                return held
            if (
                not frame.stable
                and frame.grams != self._last_emit_grams
                and self._last_emit_time is not None
                and now - self._last_emit_time >= self._reweigh_after
            ):
                self._last_emit_grams = None
                self._last_emit_impedance = None
                self._last_emit_seen = None
            if frame.grams == self._last_emit_grams and self._recent_emit(now):
                # Duplicate/continued frame of the weigh-in just emitted.
                self._last_emit_seen = now
                self._reset()
                return held
            self._active = True
            self._live_grams = frame.grams
            self._last_frame_time = now
            if frame.stable:
                self._stable_grams = frame.grams
                self._stable_time = now
            return held

        if frame.kind == "metric":
            if self._active and frame.metric is not None:
                self._metrics[frame.metric[0]] = frame.metric[1]
                self._last_frame_time = now
            return held

        # impedance or done frame; some scales repeat the weight in it.
        if frame.grams and self._stable_grams is None:
            if frame.grams == self._last_emit_grams and self._recent_emit(now):
                self._last_emit_seen = now
                self._reset()
                return held
            self._active = True
            self._live_grams = frame.grams
            self._stable_grams = frame.grams
            self._stable_time = now
        if not self._active:
            return held
        if frame.kind == "done":
            if self._stable_grams is None:
                self._reset()
                return held
            return self._emit(self._stable_time or now, now, None, None)
        if (
            self._recent_emit(now)
            and self._last_emit_impedance == frame.impedance
            and (
                self._stable_grams is None
                or self._stable_grams == self._last_emit_grams
            )
        ):
            # Repeat of the weigh-in just emitted (e.g. second proxy).
            self._last_emit_seen = now
            self._reset()
            return held
        return self._emit(
            now, now, frame.impedance, frame.bfa_type or None, frame.adc_mode
        )

    def flush(self, now: datetime) -> Measurement | None:
        """Close the session if a timeout has passed."""
        if not self._active:
            return None
        if (
            self._stable_time is not None
            and now - self._stable_time >= self._impedance_wait
        ):
            return self._emit(self._stable_time, now, None, None)
        if (
            self._last_frame_time is not None
            and now - self._last_frame_time >= self._session_gap
        ):
            if self._stable_time is None:
                self._reset()  # live frames only: nothing to report
                return None
            return self._emit(self._stable_time, now, None, None)
        return None
