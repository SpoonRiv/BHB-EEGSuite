"""Bounded PPG history, with independent cursors for each waveform client."""

import math
import threading
from collections import deque

from core.signal.ppg_bandpass_filter import PpgBandpassFilter


class PpgBuffer:
    def __init__(self, max_samples: int = 4096):
        self._samples = deque(maxlen=max_samples)
        self._lock = threading.Lock()
        self._sequence = 0
        self._session = 0
        self._filter = PpgBandpassFilter()

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
            self._session += 1
            self._filter.reset()

    def filter_config(self) -> dict:
        with self._lock:
            return {
                "enabled": self._filter.enabled,
                "lowcut_hz": self._filter.lowcut_hz,
                "highcut_hz": self._filter.highcut_hz,
                "order": self._filter.order,
                "sampling_rate_hz": self._filter.sampling_rate_hz,
            }

    def reconfigure_filter(self, *, enabled: bool, lowcut_hz: float, highcut_hz: float, order: int) -> dict:
        with self._lock:
            self._filter.reconfigure(enabled=enabled, lowcut_hz=lowcut_hz, highcut_hz=highcut_hz, order=order)
            self._samples.clear()
            self._session += 1
        return self.filter_config()

    def append(self, value: dict, timestamp: float) -> None:
        if not isinstance(value, dict) or value.get("valid") is not True:
            return
        values = [value.get(key) for key in ("green", "red", "infrared")]
        if any(not isinstance(v, int) or not 0 <= v <= 0xFFFFFF for v in values):
            return
        if not math.isfinite(timestamp):
            return
        with self._lock:
            value = self._filter.apply(value)
            self._sequence += 1
            self._samples.append(dict(
                id=self._sequence, ts=timestamp,
                green=float(value["green"]), red=float(value["red"]), infrared=float(value["infrared"]),
            ))

    def snapshot(self, after_id: int = 0) -> dict:
        with self._lock:
            return {
                "session": self._session,
                "data": [dict(sample) for sample in self._samples if sample["id"] > after_id],
            }
