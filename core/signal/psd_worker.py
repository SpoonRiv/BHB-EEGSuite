#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: 在线频域分析、连续公共平均参考与因果频带方差计算
作者: Spoon
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import butter, filtfilt, iirnotch, sosfilt, welch

from core.signal.channel_quality import ChannelQualityDetector


@dataclass(frozen=True)
class PsdBandDefinition:
    """描述一个在线分析频带及其展示元数据。"""

    key: str
    name: str
    symbol: str
    fmin_hz: float
    fmax_hz: float


@dataclass(frozen=True)
class PsdWorkerConfig:
    """描述在线频域分析、CAR 与因果方差计算参数。"""

    enabled: bool
    window_sec: float
    update_hz: float
    nfft: int
    fmin_hz: float
    fmax_hz: float
    to_db: bool
    apply_notch: bool
    car_enabled: bool
    band_filter_order: int
    variance_window_sec: float
    variance_step_sec: float
    variance_floor_uv2: float
    bands: Tuple[PsdBandDefinition, ...]
    quality_automatic_enabled: bool
    quality_manual_enabled: bool
    quality_bad_channels: Tuple[str, ...]
    quality_lowcut_hz: float
    quality_highcut_hz: float
    quality_filter_order: int
    quality_window_sec: float
    quality_step_sec: float
    quality_relative_energy_ratio: float
    quality_absolute_energy_threshold_uv2: float
    quality_exclude_windows: int
    quality_recovery_windows: int
    quality_min_valid_channels: int


@dataclass(frozen=True)
class PsdSnapshot:
    """封装同一数据版本的频谱窗口与因果频带方差。"""

    window: np.ndarray
    causal_band_variance: np.ndarray
    timestamp: float
    valid_channel_mask: np.ndarray
    quality: Dict[str, object]


class PsdWorker:
    """连续摄取 EEG，并生成配置化频带的频谱和因果方差特征。"""

    def __init__(
        self,
        cfg: PsdWorkerConfig,
        sampling_rate_hz: int,
        eeg_channel_names: List[str],
        count_divisor: float,
        has_trigger_channel: bool,
        notch_freq_hz: float,
        notch_quality_factor: float,
        units_per_count: Optional[float] = None,
    ) -> None:
        """初始化计算器并建立连续滤波状态与固定长度窗口。"""
        self.cfg = cfg
        self.sampling_rate_hz = int(max(1, sampling_rate_hz))
        self.channel_names = list(eeg_channel_names)
        self.n_channels = int(len(self.channel_names))
        self.has_trigger_channel = bool(has_trigger_channel)
        self.count_divisor = float(count_divisor) if float(count_divisor) > 0 else 120.0
        try:
            parsed_units_per_count = float(units_per_count) if units_per_count is not None else None
        except (TypeError, ValueError):
            parsed_units_per_count = None
        self.units_per_count = (
            parsed_units_per_count
            if parsed_units_per_count is not None
            and np.isfinite(parsed_units_per_count)
            and parsed_units_per_count > 0
            else None
        )
        self.notch_freq_hz = float(notch_freq_hz)
        self.notch_quality_factor = float(notch_quality_factor)
        self._window_points = int(max(8, round(float(cfg.window_sec) * float(self.sampling_rate_hz))))
        self._variance_window_points = int(
            max(2, round(float(cfg.variance_window_sec) * float(self.sampling_rate_hz)))
        )
        self._variance_step_points = int(
            max(1, round(float(cfg.variance_step_sec) * float(self.sampling_rate_hz)))
        )
        self._quality_detector = ChannelQualityDetector(
            sampling_rate_hz=self.sampling_rate_hz,
            channel_names=self.channel_names,
            automatic_enabled=cfg.quality_automatic_enabled,
            manual_enabled=cfg.quality_manual_enabled,
            bad_channels=cfg.quality_bad_channels,
            lowcut_hz=cfg.quality_lowcut_hz,
            highcut_hz=cfg.quality_highcut_hz,
            filter_order=cfg.quality_filter_order,
            window_sec=cfg.quality_window_sec,
            step_sec=cfg.quality_step_sec,
            relative_energy_ratio=cfg.quality_relative_energy_ratio,
            absolute_energy_threshold_uv2=cfg.quality_absolute_energy_threshold_uv2,
            exclude_windows=cfg.quality_exclude_windows,
            recovery_windows=cfg.quality_recovery_windows,
            min_valid_channels=cfg.quality_min_valid_channels,
        )
        self._buf: List[Deque[float]] = [
            deque(maxlen=self._window_points) for _ in range(self.n_channels)
        ]
        self._band_buf: List[List[Deque[float]]] = [
            [deque(maxlen=self._variance_window_points) for _ in range(self.n_channels)]
            for _ in cfg.bands
        ]
        self._band_sos = [
            butter(
                int(cfg.band_filter_order),
                [float(band.fmin_hz), float(band.fmax_hz)],
                btype="bandpass",
                fs=float(self.sampling_rate_hz),
                output="sos",
            )
            for band in cfg.bands
        ]
        self._band_zi = [
            np.zeros((sos.shape[0], 2, self.n_channels), dtype=np.float64)
            for sos in self._band_sos
        ]
        self._lock = threading.Lock()
        self._notch_ba: Optional[Tuple[np.ndarray, np.ndarray]] = None
        self._data_version = 0
        self._last_psd_snapshot_version = -1
        self._last_variance_snapshot_version = -1
        self._last_quality_snapshot = self._quality_detector.snapshot()

    def reset(self) -> None:
        """清空窗口并重置全部连续因果滤波器状态。"""
        with self._lock:
            for channel_buffer in self._buf:
                channel_buffer.clear()
            self._quality_detector.reset()
            for band_buffers in self._band_buf:
                for channel_buffer in band_buffers:
                    channel_buffer.clear()
            for index, sos in enumerate(self._band_sos):
                self._band_zi[index] = np.zeros(
                    (sos.shape[0], 2, self.n_channels),
                    dtype=np.float64,
                )
            self._data_version = 0
            self._last_psd_snapshot_version = -1
            self._last_variance_snapshot_version = -1
            self._last_quality_snapshot = self._quality_detector.snapshot()

    def append_chunk(self, chunk: List[List[float]]) -> None:
        """换算并连续摄取一个 EEG 数据块，逐样本应用 CAR 和因果频带滤波。"""
        if not self.cfg.enabled or not chunk or self.n_channels <= 0:
            return
        try:
            arr = np.asarray(chunk, dtype=np.float64)
        except (TypeError, ValueError):
            return
        if arr.ndim != 2 or arr.shape[1] < self.n_channels:
            return
        eeg = arr[:, : self.n_channels]
        if self.units_per_count is not None:
            eeg = eeg * float(self.units_per_count)
        elif self.count_divisor != 1.0:
            eeg = eeg / float(self.count_divisor)
        with self._lock:
            self._quality_detector.append(eeg)
            self._last_quality_snapshot = self._quality_detector.snapshot()
            valid_mask = self._quality_detector.valid_mask()
            if bool(self.cfg.car_enabled) and int(np.count_nonzero(valid_mask)) >= 2:
                eeg = eeg - np.mean(eeg[:, valid_mask], axis=1, keepdims=True)
            for channel_index in range(self.n_channels):
                self._buf[channel_index].extend(eeg[:, channel_index].tolist())
            for band_index, sos in enumerate(self._band_sos):
                filtered, next_zi = sosfilt(
                    sos,
                    eeg,
                    axis=0,
                    zi=self._band_zi[band_index],
                )
                self._band_zi[band_index] = next_zi
                for channel_index in range(self.n_channels):
                    self._band_buf[band_index][channel_index].extend(
                        filtered[:, channel_index].tolist()
                    )
            self._data_version += int(eeg.shape[0])

    def get_warmup_status(self) -> Dict[str, object]:
        """返回当前 PSD 窗口的预热进度、样本数与就绪状态。"""
        with self._lock:
            sample_count = len(self._buf[0]) if self.n_channels > 0 else 0
        return self._build_warmup_status(sample_count, self._window_points, self.cfg.window_sec)

    def get_variance_warmup_status(self) -> Dict[str, object]:
        """返回因果频带方差窗口的预热进度、样本数与就绪状态。"""
        with self._lock:
            sample_count = (
                len(self._band_buf[0][0])
                if self.n_channels > 0 and self._band_buf and self._band_buf[0]
                else 0
            )
        return self._build_warmup_status(
            sample_count,
            self._variance_window_points,
            self.cfg.variance_window_sec,
        )

    def snapshot_window(self) -> Optional[PsdSnapshot]:
        """原子获取当前 CAR 窗口及对应的连续因果频带方差。"""
        if not self.cfg.enabled:
            return None
        with self._lock:
            if self.n_channels <= 0 or len(self._buf[0]) < self._window_points:
                return None
            if self._data_version <= self._last_psd_snapshot_version:
                return None
            window = np.empty((self.n_channels, self._window_points), dtype=np.float32)
            for channel_index in range(self.n_channels):
                window[channel_index, :] = np.asarray(
                    self._buf[channel_index],
                    dtype=np.float32,
                )
            quality = dict(self._last_quality_snapshot)
            valid_mask = self._quality_detector.valid_mask()
            causal_variance = np.empty(
                (self.n_channels, len(self.cfg.bands)),
                dtype=np.float64,
            )
            for band_index, band_buffers in enumerate(self._band_buf):
                for channel_index, channel_buffer in enumerate(band_buffers):
                    values = np.asarray(channel_buffer, dtype=np.float64)
                    causal_variance[channel_index, band_index] = float(np.var(values))
            self._last_psd_snapshot_version = self._data_version
            return PsdSnapshot(
                window=window,
                causal_band_variance=causal_variance,
                timestamp=float(time.time()),
                valid_channel_mask=valid_mask.copy(),
                quality=quality,
            )

    def build_variance_warmup_payload(self) -> Dict[str, object]:
        """生成带频带元数据的因果方差预热载荷。"""
        warmup = self.get_variance_warmup_status()
        return {
            "ts": float(time.time()),
            "warmup": warmup,
            "bands": self._bands_payload(),
            "unit": "uV^2",
            "window_sec": float(self.cfg.variance_window_sec),
            "step_sec": float(self.cfg.variance_step_sec),
            "sample_count": int(warmup.get("sample_count", 0)),
            "channels": {},
            "average": [],
            "quality": self.get_quality_snapshot(),
        }

    def snapshot_variance_payload(self) -> Optional[Dict[str, object]]:
        """按配置步长原子生成最近因果窗口的频带方差 WebSocket 载荷。"""
        if not self.cfg.enabled:
            return None
        with self._lock:
            if (
                self.n_channels <= 0
                or not self._band_buf
                or len(self._band_buf[0][0]) < self._variance_window_points
            ):
                return None
            if self._data_version - self._last_variance_snapshot_version < self._variance_step_points:
                return None
            quality = dict(self._last_quality_snapshot)
            valid_mask = self._quality_detector.valid_mask()
            causal_variance = np.empty(
                (self.n_channels, len(self.cfg.bands)),
                dtype=np.float64,
            )
            for band_index, band_buffers in enumerate(self._band_buf):
                for channel_index, channel_buffer in enumerate(band_buffers):
                    causal_variance[channel_index, band_index] = float(
                        np.var(np.asarray(channel_buffer, dtype=np.float64))
                    )
            self._last_variance_snapshot_version = self._data_version
        causal_variance = np.maximum(
            np.nan_to_num(causal_variance, nan=0.0, posinf=0.0, neginf=0.0),
            0.0,
        )
        timestamp = float(time.time())
        warmup = self._build_warmup_status(
            self._variance_window_points,
            self._variance_window_points,
            self.cfg.variance_window_sec,
        )
        return {
            "ts": timestamp,
            "warmup": warmup,
            "bands": self._bands_payload(),
            "unit": "uV^2",
            "window_sec": float(self.cfg.variance_window_sec),
            "step_sec": float(self.cfg.variance_step_sec),
            "sample_count": int(self._variance_window_points),
            "channels": {
                str(channel_name): causal_variance[channel_index, :].astype(np.float32).tolist()
                for channel_index, channel_name in enumerate(self.channel_names)
            },
            "average": self._effective_mean(causal_variance, valid_mask).astype(np.float32).tolist(),
            "quality": quality,
        }

    def compute_psd_payload(self, snapshot: PsdSnapshot) -> Optional[Dict[str, object]]:
        """计算 Welch 频谱并组合因果方差、差分熵和动态频带元数据。"""
        if not self.cfg.enabled or not isinstance(snapshot, PsdSnapshot):
            return None
        window = snapshot.window
        if not isinstance(window, np.ndarray) or window.ndim != 2:
            return None
        if window.shape[0] != self.n_channels:
            return None
        data = window.astype(np.float64, copy=False)
        fs = float(self.sampling_rate_hz)
        nyquist = fs / 2.0
        display_fmin = max(0.0, float(self.cfg.fmin_hz))
        display_fmax = min(float(self.cfg.fmax_hz), nyquist)
        if display_fmin >= display_fmax:
            return None
        if bool(self.cfg.apply_notch):
            b, a = self._get_notch_ba(fs)
            if b is not None and a is not None and data.shape[1] >= 32:
                try:
                    data = filtfilt(b, a, data, axis=1).astype(np.float64, copy=False)
                except ValueError:
                    pass
        nfft = int(self.cfg.nfft)
        nperseg = min(int(max(16, data.shape[1])), int(max(16, nfft)))
        try:
            freq, linear_psd = welch(
                data,
                fs=fs,
                nperseg=nperseg,
                nfft=nfft,
                axis=1,
                scaling="density",
            )
        except ValueError:
            return None
        band_power = self._integrate_band_power(freq, linear_psd)
        band_total = np.sum(band_power, axis=1)
        band_relative_pct = np.divide(
            band_power * 100.0,
            band_total[:, None],
            out=np.zeros_like(band_power),
            where=band_total[:, None] > 0.0,
        )
        causal_variance = np.nan_to_num(
            snapshot.causal_band_variance,
            nan=0.0,
            posinf=0.0,
            neginf=0.0,
        )
        causal_variance = np.maximum(causal_variance, 0.0)
        variance_floor = float(self.cfg.variance_floor_uv2)
        band_de = 0.5 * np.log(
            2.0 * np.pi * np.e * np.maximum(causal_variance, variance_floor)
        )
        valid_mask = np.asarray(snapshot.valid_channel_mask, dtype=bool)
        average_band_power = self._effective_mean(band_power, valid_mask)
        average_band_total = float(np.sum(average_band_power))
        average_band_relative_pct = np.divide(
            average_band_power * 100.0,
            average_band_total,
            out=np.zeros_like(average_band_power),
            where=average_band_total > 0.0,
        )
        average_causal_variance = self._effective_mean(causal_variance, valid_mask)
        average_band_de = self._effective_mean(band_de, valid_mask)
        display_freq, display_psd = self._slice_display_spectrum(
            freq,
            linear_psd,
            display_fmin,
            display_fmax,
        )
        if display_freq is None or display_psd is None:
            return None
        average_psd = self._effective_mean(display_psd, valid_mask)
        unit = "uV^2/Hz"
        if bool(self.cfg.to_db):
            display_psd = 10.0 * np.log10(np.maximum(display_psd, 1e-20))
            average_psd = 10.0 * np.log10(np.maximum(average_psd, 1e-20))
            unit = "dB"
        channels: Dict[str, List[float]] = {}
        band_channels: Dict[str, Dict[str, object]] = {}
        for channel_index, channel_name in enumerate(self.channel_names):
            channels[str(channel_name)] = display_psd[channel_index, :].astype(np.float32).tolist()
            band_channels[str(channel_name)] = {
                "absolute": band_power[channel_index, :].astype(np.float32).tolist(),
                "relative_pct": band_relative_pct[channel_index, :].astype(np.float32).tolist(),
                "causal_variance": causal_variance[channel_index, :].astype(np.float32).tolist(),
                "differential_entropy": band_de[channel_index, :].astype(np.float32).tolist(),
                "total": float(band_total[channel_index]),
            }
        bands_payload = self._bands_payload()
        return {
            "ts": float(snapshot.timestamp),
            "warmup": {
                "ready": True,
                "sample_count": int(window.shape[1]),
                "required_samples": int(self._window_points),
                "elapsed_sec": float(window.shape[1] / self.sampling_rate_hz),
                "required_sec": float(self.cfg.window_sec),
                "progress": 1.0,
            },
            "freq_hz": display_freq.astype(np.float32).tolist(),
            "channels": channels,
            "unit": unit,
            "display_fmin_hz": float(display_fmin),
            "display_fmax_hz": float(display_fmax),
            "sample_count": int(window.shape[1]),
            "preprocessing": {
                "car": bool(self.cfg.car_enabled),
                "causal_band_filter_order": int(self.cfg.band_filter_order),
            },
            "average": {
                "label": "全通道平均",
                "channel_count": int(np.count_nonzero(valid_mask)),
                "total_channel_count": int(self.n_channels),
                "spectrum": average_psd.astype(np.float32).tolist(),
                "band_power": {
                    "absolute": average_band_power.astype(np.float32).tolist(),
                    "relative_pct": average_band_relative_pct.astype(np.float32).tolist(),
                    "causal_variance": average_causal_variance.astype(np.float32).tolist(),
                    "differential_entropy": average_band_de.astype(np.float32).tolist(),
                    "total": average_band_total,
                },
            },
            "quality": snapshot.quality,
            "variance": {
                "unit": "uV^2",
                "channels": {
                    str(channel_name): causal_variance[channel_index, :].astype(np.float32).tolist()
                    for channel_index, channel_name in enumerate(self.channel_names)
                },
                "average": average_causal_variance.astype(np.float32).tolist(),
            },
            "band_power": {
                "bands": bands_payload,
                "channels": band_channels,
                "absolute_unit": "uV^2",
                "relative_unit": "%",
                "causal_variance_unit": "uV^2",
                "differential_entropy_unit": "nat",
                "differential_entropy_log_base": "e",
                "differential_entropy_method": "gaussian_from_causal_band_variance",
                "differential_entropy_reference_unit": "uV",
                "differential_entropy_power_floor_uV2": variance_floor,
                "normalization": "sum_of_listed_bands",
                "normalization_fmin_hz": float(self.cfg.bands[0].fmin_hz),
                "normalization_fmax_hz": float(self.cfg.bands[-1].fmax_hz),
                "normalization_complete": bool(nyquist > self.cfg.bands[-1].fmax_hz),
            },
        }

    def get_quality_snapshot(self) -> Dict[str, object]:
        """返回最近质量状态和预热进度。"""
        with self._lock:
            return self._quality_detector.snapshot()

    def set_quality_automatic_enabled(self, enabled: bool) -> Dict[str, object]:
        """切换自动坏通道排除并返回最新状态。"""
        with self._lock:
            result = self._quality_detector.set_automatic_enabled(enabled)
            self._last_quality_snapshot = dict(result)
            return result

    def set_manual_bad_channels(self, channel_names: List[str]) -> Dict[str, object]:
        """更新手动排除通道并返回新的质量状态快照。"""
        with self._lock:
            result = self._quality_detector.set_manual_bad_channels(channel_names)
            self._last_quality_snapshot = dict(result)
            return result

    def _effective_mean(self, values: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
        """仅使用有效通道计算均值，无有效通道时返回零向量。"""
        mask = np.asarray(valid_mask, dtype=bool)
        if values.ndim < 2 or mask.size != values.shape[0] or not np.any(mask):
            return np.zeros(values.shape[1:], dtype=np.float64)
        return np.mean(values[mask], axis=0)

    def get_update_interval_sec(self) -> float:
        """将配置的 PSD 更新频率转换为秒级间隔。"""
        hz = float(self.cfg.update_hz)
        if not math.isfinite(hz) or hz <= 0:
            hz = 1.0
        return 1.0 / hz

    def get_variance_interval_sec(self) -> float:
        """返回因果频带方差的配置化输出步长。"""
        interval = float(self.cfg.variance_step_sec)
        if not math.isfinite(interval) or interval <= 0:
            return 0.1
        return interval

    def _build_warmup_status(
        self,
        sample_count: int,
        required_samples: int,
        required_sec: float,
    ) -> Dict[str, object]:
        """构造统一的窗口预热状态。"""
        progress = min(1.0, sample_count / required_samples) if required_samples > 0 else 1.0
        return {
            "ready": bool(sample_count >= required_samples),
            "sample_count": int(sample_count),
            "required_samples": int(required_samples),
            "elapsed_sec": float(sample_count / self.sampling_rate_hz),
            "required_sec": float(required_sec),
            "progress": float(progress),
        }

    def _bands_payload(self) -> List[Dict[str, object]]:
        """生成稳定的动态频带元数据载荷。"""
        return [
            {
                "key": band.key,
                "name": band.name,
                "symbol": band.symbol,
                "fmin_hz": float(band.fmin_hz),
                "fmax_hz": float(band.fmax_hz),
            }
            for band in self.cfg.bands
        ]

    def _integrate_band_power(self, freq: np.ndarray, psd: np.ndarray) -> np.ndarray:
        """在配置频带边界插值后积分 Welch 线性功率谱。"""
        band_power = np.zeros((self.n_channels, len(self.cfg.bands)), dtype=np.float64)
        for band_index, band in enumerate(self.cfg.bands):
            low = max(float(band.fmin_hz), float(freq[0]))
            high = min(float(band.fmax_hz), float(freq[-1]))
            if low >= high:
                continue
            interior = (freq > low) & (freq < high)
            band_freq = np.concatenate(
                (
                    np.asarray([low], dtype=np.float64),
                    freq[interior],
                    np.asarray([high], dtype=np.float64),
                )
            )
            low_values = np.asarray(
                [np.interp(low, freq, row) for row in psd],
                dtype=np.float64,
            )[:, None]
            high_values = np.asarray(
                [np.interp(high, freq, row) for row in psd],
                dtype=np.float64,
            )[:, None]
            values = np.concatenate((low_values, psd[:, interior], high_values), axis=1)
            band_power[:, band_index] = np.trapezoid(values, band_freq, axis=1)
        band_power = np.nan_to_num(band_power, nan=0.0, posinf=0.0, neginf=0.0)
        return np.maximum(band_power, 0.0)

    def _slice_display_spectrum(
        self,
        freq: np.ndarray,
        psd: np.ndarray,
        low: float,
        high: float,
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """插值补齐展示边界并裁剪频谱。"""
        display_low = max(float(low), float(freq[0]))
        display_high = min(float(high), float(freq[-1]))
        if display_low >= display_high:
            return None, None
        interior = (freq > display_low) & (freq < display_high)
        display_freq = np.concatenate(
            (
                np.asarray([display_low], dtype=np.float64),
                freq[interior],
                np.asarray([display_high], dtype=np.float64),
            )
        )
        low_values = np.asarray(
            [np.interp(display_low, freq, row) for row in psd],
            dtype=np.float64,
        )[:, None]
        high_values = np.asarray(
            [np.interp(display_high, freq, row) for row in psd],
            dtype=np.float64,
        )[:, None]
        display_psd = np.concatenate((low_values, psd[:, interior], high_values), axis=1)
        return display_freq, display_psd

    def _get_notch_ba(
        self,
        sampling_rate_hz: float,
    ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """按需创建并缓存频谱窗口使用的陷波器系数。"""
        if self._notch_ba is not None:
            return self._notch_ba
        frequency = float(self.notch_freq_hz)
        if not math.isfinite(frequency) or frequency <= 0:
            return None, None
        if frequency >= float(sampling_rate_hz) / 2.0:
            return None, None
        try:
            b, a = iirnotch(
                w0=frequency,
                Q=float(self.notch_quality_factor),
                fs=float(sampling_rate_hz),
            )
        except ValueError:
            return None, None
        self._notch_ba = (b, a)
        return self._notch_ba
