#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stateful PPG high-pass and power-line notch filtering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


# PPG filtering is intentionally fixed in code.  The current CH8 protocol
# emits one PPG point per five 500-Hz EEG samples, i.e. 100 Hz.
PPG_SAMPLING_RATE_HZ = 100.0
PPG_LOWCUT_HZ = 0.5
PPG_FILTER_ORDER = 4


@dataclass(frozen=True)
class PpgBandpassFilterConfig:
    sampling_rate_hz: float = PPG_SAMPLING_RATE_HZ
    lowcut_hz: float = PPG_LOWCUT_HZ
    order: int = PPG_FILTER_ORDER
    enabled: bool = True


class PpgBandpassFilter:
    """Continuously filter green, red and infrared PPG samples."""

    CHANNELS = ("green", "red", "infrared")

    def __init__(self, cfg: Optional[PpgBandpassFilterConfig] = None):
        cfg = cfg or PpgBandpassFilterConfig()
        self._sampling_rate_hz = float(cfg.sampling_rate_hz)
        self._lowcut_hz = float(cfg.lowcut_hz)
        self._order = int(cfg.order)
        self._enabled = bool(cfg.enabled)
        self._sos: Optional[np.ndarray] = None
        self._states: Optional[np.ndarray] = None
        self._primed = False
        if self._enabled:
            self._build_sos()

    def _build_sos(self) -> None:
        self._sos = None
        self._states = None
        self._primed = False
        if self._sampling_rate_hz <= 0 or self._lowcut_hz <= 0:
            return
        nyquist = self._sampling_rate_hz / 2.0
        if not np.isfinite(self._lowcut_hz) or self._lowcut_hz >= nyquist:
            return
        self._sos = butter(
            max(1, self._order),
            self._lowcut_hz,
            btype="highpass",
            fs=self._sampling_rate_hz,
            output="sos",
        )

    def reset(self) -> None:
        self._states = None
        self._primed = False

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
    def order(self) -> int:
        return self._order
