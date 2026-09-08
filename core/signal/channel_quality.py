#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: 对原始 EEG 执行连续宽频质量检测并维护手动与自动坏通道状态
作者: Spoon
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, List, Sequence

import numpy as np
from scipy.signal import butter, sosfilt


class ChannelQualityDetector:
    """使用连续因果滤波、滑动方差与防抖规则维护有效通道掩码。"""

    def __init__(
        self,
        sampling_rate_hz: int,
        channel_names: Sequence[str],
        automatic_enabled: bool,
        manual_enabled: bool,
        bad_channels: Sequence[str],
        lowcut_hz: float,
        highcut_hz: float,
        filter_order: int,
        window_sec: float,
        step_sec: float,
        relative_energy_ratio: float,
        absolute_energy_threshold_uv2: float,
        exclude_windows: int,
        recovery_windows: int,
        min_valid_channels: int,
    ) -> None:
        """初始化质量滤波器、窗口、防抖计数器与通道掩码。"""
        self.sampling_rate_hz = int(sampling_rate_hz)
        self.channel_names = list(channel_names)
        self.n_channels = len(self.channel_names)
        self.automatic_enabled = bool(automatic_enabled)
        self.manual_enabled = bool(manual_enabled)
        self.relative_energy_ratio = float(relative_energy_ratio)
        self.absolute_energy_threshold_uv2 = float(absolute_energy_threshold_uv2)
        self.exclude_windows = int(exclude_windows)
        self.recovery_windows = int(recovery_windows)
        self.min_valid_channels = int(min_valid_channels)
        self.window_points = max(2, int(round(float(window_sec) * self.sampling_rate_hz)))
        self.step_points = max(1, int(round(float(step_sec) * self.sampling_rate_hz)))
        self._sos = butter(
            int(filter_order),
            [float(lowcut_hz), float(highcut_hz)],
            btype="bandpass",
            fs=float(self.sampling_rate_hz),
            output="sos",
        )
        self._zi = np.zeros((self._sos.shape[0], 2, self.n_channels), dtype=np.float64)
        requested = {str(name) for name in bad_channels}
        self._manual_bad = np.asarray(
            [name in requested for name in self.channel_names],
            dtype=bool,
        )
        self._auto_excluded = np.zeros(self.n_channels, dtype=bool)
        self._bad_counts = np.zeros(self.n_channels, dtype=np.int64)
        self._recovery_counts = np.zeros(self.n_channels, dtype=np.int64)
        self._states = np.asarray(
            ["manual_bad" if value and self.manual_enabled else "unknown" for value in self._manual_bad],
            dtype=object,
        )
        self._buffers: List[Deque[float]] = [
            deque(maxlen=self.window_points) for _ in range(self.n_channels)
        ]
        self._samples_since_evaluation = 0
        self._last_variance = np.full(self.n_channels, np.nan, dtype=np.float64)
        self._last_relative = np.full(self.n_channels, np.nan, dtype=np.float64)
        self._ready = False
        self._snapshot = self._build_snapshot()

    def reset(self) -> None:
        """清空质量窗口、连续滤波状态和自动判定状态。"""
        for buffer in self._buffers:
            buffer.clear()
        self._zi.fill(0.0)
        self._auto_excluded.fill(False)
        self._bad_counts.fill(0)
        self._recovery_counts.fill(0)
        self._samples_since_evaluation = 0
        self._last_variance.fill(np.nan)
        self._last_relative.fill(np.nan)
        self._ready = False
        self._states = np.asarray(
            ["manual_bad" if value and self.manual_enabled else "unknown" for value in self._manual_bad],
            dtype=object,
        )
        self._snapshot = self._build_snapshot()

    def append(self, eeg_uv: np.ndarray) -> None:
        """摄取原始微伏 EEG，并按配置步长推进质量状态机。"""
        values = np.asarray(eeg_uv, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != self.n_channels or values.shape[0] <= 0:
            return
        filtered, self._zi = sosfilt(
            self._sos,
            values,
            axis=0,
            zi=self._zi,
        )
        for row in filtered:
            for index, value in enumerate(row):
                self._buffers[index].append(float(value))
            self._samples_since_evaluation += 1
            if (
                self._samples_since_evaluation >= self.step_points
                and len(self._buffers[0]) >= self.window_points
            ):
                self._samples_since_evaluation = 0
                self._evaluate()
        if not self._ready:
            self._snapshot = self._build_snapshot()

    def valid_mask(self) -> np.ndarray:
        """返回当前手动与自动规则合并后的有效通道掩码副本。"""
        manual_bad = self._manual_bad if self.manual_enabled else np.zeros(self.n_channels, dtype=bool)
        automatic_bad = (
            self._auto_excluded
            if self.automatic_enabled
            else np.zeros(self.n_channels, dtype=bool)
        )
        return ~(manual_bad | automatic_bad)

    def snapshot(self) -> Dict[str, object]:
        """返回最近一次质量评估的结构化快照。"""
        result = dict(self._snapshot)
        result["sample_count"] = int(len(self._buffers[0])) if self._buffers else 0
        result["required_samples"] = int(self.window_points)
        return result

    def set_automatic_enabled(self, enabled: bool) -> Dict[str, object]:
        """切换自动排除并立即刷新有效通道快照。"""
        self.automatic_enabled = bool(enabled)
        if not self.automatic_enabled:
            self._auto_excluded.fill(False)
            self._bad_counts.fill(0)
            self._recovery_counts.fill(0)
        self._refresh_states()
        self._snapshot = self._build_snapshot()
        return self.snapshot()

    def set_manual_bad_channels(self, channel_names: Sequence[str]) -> Dict[str, object]:
        """更新手动停用通道，并保证至少保留配置数量的手动有效通道。"""
        requested = {str(name or "").strip() for name in channel_names if str(name or "").strip()}
        unknown = requested.difference(self.channel_names)
        if unknown:
            raise ValueError(f"包含未知通道: {', '.join(sorted(unknown))}")
        if self.manual_enabled and self.n_channels - len(requested) < self.min_valid_channels:
            raise ValueError(f"手动排除后必须至少保留 {self.min_valid_channels} 个有效通道")
        self._manual_bad = np.asarray(
            [name in requested for name in self.channel_names],
            dtype=bool,
        )
        self._auto_excluded[self._manual_bad] = False
        self._bad_counts[self._manual_bad] = 0
        self._recovery_counts[self._manual_bad] = 0
        self._enforce_minimum_valid_channels()
        self._refresh_states()
        self._snapshot = self._build_snapshot()
        return self.snapshot()

    def _evaluate(self) -> None:
        """计算窗口总体方差并推进自动排除与恢复计数。"""
        window = np.column_stack(
            [np.asarray(buffer, dtype=np.float64) for buffer in self._buffers]
        )
        variances = np.var(window, axis=0, ddof=0).astype(np.float64)
        manual_enabled = ~self._manual_bad if self.manual_enabled else np.ones(self.n_channels, dtype=bool)
        comparison = manual_enabled & ~self._auto_excluded
        if np.count_nonzero(comparison) < 2:
            comparison = manual_enabled.copy()
        finite_comparison = comparison & np.isfinite(variances)
        if np.any(finite_comparison):
            minimum = float(np.min(variances[finite_comparison]))
            denominator = max(minimum, float(np.finfo(np.float64).tiny))
            relative = variances / denominator
        else:
            relative = np.full(self.n_channels, np.inf, dtype=np.float64)
        candidates = manual_enabled & (
            ~np.isfinite(variances)
            | (relative > self.relative_energy_ratio)
            | (variances > self.absolute_energy_threshold_uv2)
        )
        self._last_variance = variances
        self._last_relative = relative
        self._ready = True
        for index in range(self.n_channels):
            if not manual_enabled[index]:
                continue
            if not self.automatic_enabled:
                self._bad_counts[index] = 0
                self._recovery_counts[index] = 0
                continue
            if candidates[index]:
                self._recovery_counts[index] = 0
                if self._auto_excluded[index]:
                    continue
                self._bad_counts[index] += 1
                if self._bad_counts[index] >= self.exclude_windows:
                    available = int(np.count_nonzero(self.valid_mask()))
                    if available > self.min_valid_channels:
                        self._auto_excluded[index] = True
            else:
                self._bad_counts[index] = 0
                if self._auto_excluded[index]:
                    self._recovery_counts[index] += 1
                    if self._recovery_counts[index] >= self.recovery_windows:
                        self._auto_excluded[index] = False
                        self._recovery_counts[index] = 0
                else:
                    self._recovery_counts[index] = 0
        self._enforce_minimum_valid_channels()
        self._refresh_states(candidates)
        self._snapshot = self._build_snapshot()

    def _enforce_minimum_valid_channels(self) -> None:
        """按最近异常程度恢复自动排除通道，确保有效通道下限。"""
        deficit = self.min_valid_channels - int(np.count_nonzero(self.valid_mask()))
        if deficit <= 0:
            return
        candidates = np.flatnonzero(self._auto_excluded)
        ordered = sorted(
            candidates.tolist(),
            key=lambda index: (
                float(self._last_relative[index])
                if np.isfinite(self._last_relative[index])
                else float("inf")
            ),
        )
        for index in ordered[:deficit]:
            self._auto_excluded[index] = False
            self._bad_counts[index] = 0
            self._recovery_counts[index] = 0

    def _refresh_states(self, candidates: np.ndarray | None = None) -> None:
        """根据手动、自动与防抖计数生成每通道展示状态。"""
        if candidates is None:
            candidates = np.zeros(self.n_channels, dtype=bool)
        for index in range(self.n_channels):
            if self.manual_enabled and self._manual_bad[index]:
                self._states[index] = "manual_bad"
            elif self.automatic_enabled and self._auto_excluded[index]:
                self._states[index] = (
                    "recovering" if self._recovery_counts[index] > 0 else "bad"
                )
            elif self.automatic_enabled and (
                candidates[index] or self._bad_counts[index] > 0
            ):
                self._states[index] = "suspect"
            elif self._ready:
                self._states[index] = "good"
            else:
                self._states[index] = "unknown"

    def _build_snapshot(self) -> Dict[str, object]:
        """构造供 API 与 WebSocket 使用的质量状态载荷。"""
        valid = self.valid_mask()
        channels: Dict[str, object] = {}
        for index, name in enumerate(self.channel_names):
            variance = self._last_variance[index]
            relative = self._last_relative[index]
            channels[name] = {
                "state": str(self._states[index]),
                "included": bool(valid[index]),
                "valid": bool(valid[index]),
                "manual_enabled": bool(not self._manual_bad[index]),
                "auto_excluded": bool(self._auto_excluded[index]),
                "total_variance_uv2": float(variance) if np.isfinite(variance) else None,
                "variance_uv2": float(variance) if np.isfinite(variance) else None,
                "relative_to_minimum": float(relative) if np.isfinite(relative) else None,
            }
        return {
            "enabled": bool(self.automatic_enabled or self.manual_enabled),
            "automatic_enabled": bool(self.automatic_enabled),
            "manual_enabled": bool(self.manual_enabled),
            "ready": bool(self._ready),
            "valid_channel_count": int(np.count_nonzero(valid)),
            "total_channel_count": int(self.n_channels),
            "valid_channels": [name for index, name in enumerate(self.channel_names) if valid[index]],
            "excluded_channels": [name for index, name in enumerate(self.channel_names) if not valid[index]],
            "manual_excluded_channels": [
                name
                for index, name in enumerate(self.channel_names)
                if self.manual_enabled and self._manual_bad[index]
            ],
            "automatic_bad_channels": [
                name
                for index, name in enumerate(self.channel_names)
                if self.automatic_enabled and self._auto_excluded[index]
            ],
            "channels": channels,
            "updated_at": float(time.time()),
        }
