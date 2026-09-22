#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stateful Butterworth band-pass filtering for the independent PPG stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from core.signal.notch_filter import NotchFilter, NotchFilterConfig


# PPG filtering is intentionally fixed in code.  The current CH8 protocol
# emits one PPG point per five 500-Hz EEG samples, i.e. 100 Hz.
PPG_SAMPLING_RATE_HZ = 100.0
PPG_LOWCUT_HZ = 0.2
PPG_REQUESTED_HIGHCUT_HZ = 1000.0
PPG_FILTER_ORDER = 4
PPG_NOTCH_FREQ_HZ = 50.0
PPG_NOTCH_QUALITY_FACTOR = 30.0


@dataclass(frozen=True)
class PpgBandpassFilterConfig:
    sampling_rate_hz: float = PPG_SAMPLING_RATE_HZ
    lowcut_hz: float = PPG_LOWCUT_HZ
    highcut_hz: float = PPG_REQUESTED_HIGHCUT_HZ
    order: int = PPG_FILTER_ORDER
    enabled: bool = True


class PpgBandpassFilter:
    """Continuously filter green, red and infrared PPG samples."""

    CHANNELS = ("green", "red", "infrared")

    def __init__(self, cfg: Optional[PpgBandpassFilterConfig] = None):
        cfg = cfg or PpgBandpassFilterConfig()
        self._sampling_rate_hz = float(cfg.sampling_rate_hz)
        self._lowcut_hz = float(cfg.lowcut_hz)
        self._requested_highcut_hz = float(cfg.highcut_hz)
        self._order = int(cfg.order)
        self._enabled = bool(cfg.enabled)
        self._effective_highcut_hz = self._effective_highcut(self._requested_highcut_hz)
        self._notch = NotchFilter(
            NotchFilterConfig(
                sampling_rate_hz=int(self._sampling_rate_hz),
                freq_hz=PPG_NOTCH_FREQ_HZ,
                quality_factor=PPG_NOTCH_QUALITY_FACTOR,
                channel_count=len(self.CHANNELS),
                has_trigger_channel=False,
            )
        )
        self._sos: Optional[np.ndarray] = None
        self._states: Optional[np.ndarray] = None
        self._primed = False
        if self._enabled:
            self._build_sos()

    def _effective_highcut(self, requested_hz: float) -> float:
        nyquist = self._sampling_rate_hz / 2.0
        # scipy requires highcut < Nyquist. Leave a small margin for stable
        # SOS coefficients; the requested 1 kHz upper bound is therefore
        # automatically limited to 99% of Nyquist for this 100-Hz stream.
        return min(float(requested_hz), nyquist * 0.99)

    def _build_sos(self) -> None:
        self._sos = None
        self._states = None
        self._primed = False
        if self._sampling_rate_hz <= 0 or self._lowcut_hz <= 0:
            return
        nyquist = self._sampling_rate_hz / 2.0
        high = self._effective_highcut_hz
        if not np.isfinite(high) or self._lowcut_hz >= high or high >= nyquist:
            return
        self._sos = butter(
            max(1, self._order),
            [self._lowcut_hz, high],
            btype="band",
            fs=self._sampling_rate_hz,
            output="sos",
        )

    def reset(self) -> None:
        self._states = None
        self._primed = False
        self._notch.reset()

    def apply(self, value: Dict[str, Any]) -> Dict[str, Any]:
        """Return one filtered PPG sample, preserving its validity marker."""
        if not isinstance(value, dict) or value.get("valid") is not True:
            return value
        if not self._enabled or self._sos is None:
            return value
        try:
            sample = np.asarray([float(value[name]) for name in self.CHANNELS], dtype=np.float64)
        except (KeyError, TypeError, ValueError):
            return value
        if not np.all(np.isfinite(sample)):
            return value

        # Reuse the application's existing 50-Hz notch implementation. At
        # the current 100-Hz PPG update rate it is transparently bypassed by
        # NotchFilter because 50 Hz is exactly Nyquist.
        sample = np.asarray(self._notch.apply([sample.tolist()])[0], dtype=np.float64)

        if self._states is None:
            zi = sosfilt_zi(self._sos).astype(np.float64)
            self._states = np.repeat(zi[:, :, np.newaxis], len(self.CHANNELS), axis=2)
        if not self._primed:
            self._states *= sample[np.newaxis, np.newaxis, :]
            self._primed = True

        filtered = np.empty(len(self.CHANNELS), dtype=np.float64)
        for index in range(len(self.CHANNELS)):
            output, state = sosfilt(self._sos, [sample[index]], zi=self._states[:, :, index])
            self._states[:, :, index] = state
            filtered[index] = output[0]
        if not np.all(np.isfinite(filtered)):
            return value
        return {
            **value,
            **{name: float(filtered[index]) for index, name in enumerate(self.CHANNELS)},
            "filtered": True,
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def sampling_rate_hz(self) -> float:
        return self._sampling_rate_hz

    @property
    def lowcut_hz(self) -> float:
        return self._lowcut_hz

    @property
    def requested_highcut_hz(self) -> float:
        return self._requested_highcut_hz

    @property
    def effective_highcut_hz(self) -> float:
        return self._effective_highcut_hz

    @property
    def order(self) -> int:
        return self._order
