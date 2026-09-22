#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: BLE 采集进程实现（连接设备、接收通知数据、按协议组帧解析并推送到 LSL）
作者: Spoon
"""

import asyncio
import logging
import multiprocessing
import queue
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from bleak import BleakClient, BleakScanner

from configs.config_loader import load_config
from core.ble.commands import interpret_two_level_cmd
from core.ble.device_finder import find_device_by_spec
from core.ble.frame_parser import FrameSpec, FrameStreamDecoder, parse_frame
from core.ble.impedance_parser import ImpedanceFrameSpec, build_impedance_vector, parse_impedance_frame
from core.ble.lsl_outlet import LslOutletConfig, LslOutletWriter
from core.ble.module_naming import BleModuleNameInfo, parse_ble_module_name


def _normalize_ble_uuid(value: Optional[str]) -> Optional[str]:
    """
    归一化 BLE 特征 UUID，统一转为 128-bit 小写格式。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.lower()
    if s.startswith("0x"):
        s = s[2:].strip()
    if not s:
        return None
    if "-" in s:
        return s
    if len(s) == 4:
        return f"0000{s}-0000-1000-8000-00805f9b34fb"
    if len(s) == 8:
        return f"{s}-0000-1000-8000-00805f9b34fb"
    if len(s) == 32:
        return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"
    return s


async def _send_eeg_start_sequence(
    send_cmd: Callable[[List[int], str], Awaitable[None]],
    stop_command: List[int],
    start_command: List[int],
    *,
    settle_delay_sec: float = 0.3,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """
    将设备 EEG 状态归一化后再启动采集。

    部分设备在首次订阅 BLE notify 后仍可能保留旧的 ADC/测试输出状态。
    先发送 STOP，短暂等待设备状态稳定，再发送 START，可确保首次启动和
    人工“停止后再启动”使用相同的硬件状态转换序列。

    预停止是兼容性保护：部分固件在本来已停止时可能拒绝重复 STOP，因此
    该步骤失败时记录警告并继续尝试 START；START 失败仍向上抛出，由采集
    主循环上报错误。
    """
    try:
        await send_cmd(list(stop_command), action="pre_stop_eeg")
        await sleep_fn(max(0.0, float(settle_delay_sec)))
    except Exception as exc:
        logging.warning("EEG pre-stop failed; continuing with start: %s", exc)
    await send_cmd(list(start_command), action="start_eeg")


async def _connect_and_stream(
    config_path: str,
    stop_event: multiprocessing.Event,
    status_queue: multiprocessing.Queue,
    command_queue: multiprocessing.Queue,
    debug_queue: Optional[multiprocessing.Queue],
    connect_address: Optional[str],
    connect_name: Optional[str],
    ppg_queue: Optional[multiprocessing.Queue] = None,
) -> None:
    """
    BLE 连接与数据接收主协程。
    """
    cfg = load_config(config_path)

    eeg_n_channels = int(cfg.eeg.n_channels)
    eeg_proto = cfg.eeg.protocol.ch8 if eeg_n_channels == 8 else cfg.eeg.protocol.ch16
    if eeg_proto is None:
        raise ValueError("eeg.n_channels=16 时必须配置 eeg.protocol.ch16.frame")

    channel_count = eeg_n_channels + (1 if cfg.eeg.lsl.include_trigger_channel else 0)
    spec: Optional[FrameSpec] = None
    outlet: Optional[LslOutletWriter] = None

    imp_n_channels = int(cfg.impedance.n_channels)
    imp_tail_channels = 0
    imp_spec: Optional[ImpedanceFrameSpec] = None
    imp_outlet: Optional[LslOutletWriter] = None

    address: Optional[str] = (connect_address or "").strip() or cfg.bluetooth.mac_address.strip() or None
    resolved_name = (connect_name or "").strip() or cfg.bluetooth.target_device
    # Keep the BLEDevice returned by the scan. Windows WinRT can discover a
    # peripheral successfully but fail a second address-only lookup immediately
    # afterwards; passing the discovered object preserves its advertisement
    # identity and avoids that race.
    ble_device: Any = None
    if address:
        try:
            timeout_sec = float(cfg.bluetooth.scan.retry_interval_sec)
            if timeout_sec <= 0:
                timeout_sec = 1.0
            devices = await BleakScanner.discover(timeout=timeout_sec)
            for dev in devices:
                if str(getattr(dev, "address", "") or "") == str(address):
                    ble_device = dev
                    n = str(getattr(dev, "name", "") or "").strip()
                    if n:
                        resolved_name = n
                        break
        except Exception:
            pass

    if not address:
        target = await find_device_by_spec(
            target_name=str(cfg.bluetooth.target_device or ""),
            max_retries=int(cfg.bluetooth.scan.max_retries),
            retry_interval_sec=float(cfg.bluetooth.scan.retry_interval_sec),
            module_name_regex=str(cfg.bluetooth.module_name_regex or ""),
            desired_eeg_channels=int(cfg.eeg.n_channels),
            require_stim_module=bool(getattr(cfg, "tdcs", None) and cfg.tdcs.enabled),
        )
        if not target and bool(getattr(cfg, "tdcs", None) and cfg.tdcs.enabled):
            target = await find_device_by_spec(
                target_name=str(cfg.bluetooth.target_device or ""),
                max_retries=int(cfg.bluetooth.scan.max_retries),
                retry_interval_sec=float(cfg.bluetooth.scan.retry_interval_sec),
                module_name_regex=str(cfg.bluetooth.module_name_regex or ""),
                desired_eeg_channels=int(cfg.eeg.n_channels),
                require_stim_module=False,
            )
        if not target:
            status_queue.put({"type": "error", "message": "未扫描到目标 BLE 设备", "name": cfg.bluetooth.target_device})
            return
        address = target.address
        resolved_name = target.name
        # find_device_by_spec returns BleTarget (name/address), not BLEDevice.

    module_info: Optional[BleModuleNameInfo] = parse_ble_module_name(resolved_name, str(cfg.bluetooth.module_name_regex or ""))
    if module_info is not None:
        detected_channels = int(module_info.eeg_channels)
        configured_channels = int(cfg.eeg.n_channels)
        if detected_channels in {8, 16} and detected_channels != configured_channels:
            module_payload = {
                "eeg_channels": detected_channels,
                "stim_channels": int(module_info.stim_channels),
            }
            status_queue.put(
                {
                    "type": "error",
                    "code": "eeg_channel_mode_mismatch",
                    "message": (
                        f"设备为 {detected_channels} 通道，但当前有效配置为 "
                        f"{configured_channels} 通道"
                    ),
                    "configured_eeg_channels": configured_channels,
                    "detected_eeg_channels": detected_channels,
                    "address": address,
                    "name": resolved_name,
                    "module": module_payload,
                }
            )
            return

    device_has_stim = module_info is not None and int(module_info.stim_channels) > 0
    imp_include_tdcs = bool(cfg.impedance.frame.include_tdcs_if_ch8) and imp_n_channels == 8 and bool(device_has_stim)
    imp_tail_channels = (1 if bool(cfg.impedance.frame.include_bias) else 0) + (1 if imp_include_tdcs else 0)

    notify_uuid = _normalize_ble_uuid(getattr(cfg.bluetooth.gatt, "notify_char_uuid", None))
    write_uuid = _normalize_ble_uuid(getattr(cfg.bluetooth.gatt, "write_char_uuid", None))
    notify_char: Any = notify_uuid if notify_uuid is not None else int(cfg.bluetooth.gatt.notify_char_handle)
    write_char: Any = write_uuid if write_uuid is not None else int(cfg.bluetooth.gatt.write_char_handle)
    write_with_response = bool(getattr(cfg.bluetooth.gatt, "write_with_response", False))

    def _get_trigger_source_command() -> List[int]:
        """
        根据配置返回 trigger 源选择指令。
        """
        source_mode = str(getattr(cfg.trigger, "source_mode", "ble") or "ble").strip().lower()
        if source_mode == "ttl_uart":
            return list(cfg.bluetooth.commands.trigger_source_ttl_uart)
        if source_mode == "ttl_level":
            return list(cfg.bluetooth.commands.trigger_source_ttl_level)
        return list(cfg.bluetooth.commands.trigger_source_ble)

    def ensure_eeg_lsl_ready() -> None:
        nonlocal spec, outlet, eeg_decoder
        if spec is None:
            spec = FrameSpec(
                channels=eeg_n_channels,
                header_len_bytes=eeg_proto.frame.header_len_bytes,
                bytes_per_sample_per_channel=eeg_proto.frame.bytes_per_sample_per_channel,
                samples_per_frame=eeg_proto.frame.samples_per_frame,
                trigger_len_bytes=eeg_proto.frame.trigger_len_bytes,
                ppg_len_bytes=eeg_proto.frame.ppg_len_bytes,
                reserved_len_bytes=eeg_proto.frame.reserved_len_bytes,
                imu_len_bytes=eeg_proto.frame.imu_len_bytes,
                battery_len_bytes=eeg_proto.frame.battery_len_bytes,
                tail_len_bytes=eeg_proto.frame.tail_len_bytes,
                allow_ch8_ppg_checksum_quirk=eeg_proto.frame.allow_ch8_ppg_checksum_quirk,
            )
        if eeg_decoder is None:
            eeg_decoder = FrameStreamDecoder(spec)
        if outlet is None:
            outlet = LslOutletWriter(
                LslOutletConfig(
                    stream_name=cfg.eeg.lsl.stream_name,
                    stream_type=cfg.eeg.lsl.stream_type,
                    channel_count=channel_count,
                    sampling_rate_hz=cfg.eeg.sampling_rate_hz,
                    source_id=f"bhb-eeg-{cfg.bluetooth.target_device}",
                )
            )

    def ensure_impedance_lsl_ready() -> None:
        nonlocal imp_spec, imp_outlet
        if imp_spec is None:
            imp_spec = ImpedanceFrameSpec(
                header=(int(cfg.impedance.frame.header[0]) & 0xFF, int(cfg.impedance.frame.header[1]) & 0xFF),
                n_channels=imp_n_channels,
                frame_len_bytes=int(cfg.impedance.frame.frame_len_bytes_ch8 if imp_n_channels == 8 else cfg.impedance.frame.frame_len_bytes_ch16),
                include_bias=bool(cfg.impedance.frame.include_bias),
                include_tdcs=imp_include_tdcs,
                gain_scale=float(cfg.impedance.frame.gain_scale),
            )
        if imp_outlet is None:
            imp_outlet = LslOutletWriter(
                LslOutletConfig(
                    stream_name=cfg.impedance.lsl.stream_name,
                    stream_type=cfg.impedance.lsl.stream_type,
                    channel_count=imp_n_channels + imp_tail_channels,
                    sampling_rate_hz=int(cfg.impedance.lsl.sampling_rate_hz),
                    source_id=f"bhb-imp-{cfg.bluetooth.target_device}",
                )
            )

    eeg_decoder: Optional[FrameStreamDecoder] = None
    frame_counter = 0
    notify_counter = 0
    last_notify_ts: float = 0.0
    no_data_reported = False
    eeg_last_seq: Optional[int] = None
    ppg_frame_time = 0.0
    eeg_stats_window_start_ts: float = time.time()
    eeg_stats_last_report_ts: float = time.time()
    eeg_window_valid_frames: int = 0
    eeg_window_invalid_frames: int = 0
    eeg_window_lost_by_seq: int = 0
    eeg_window_dropped_bytes: int = 0
    start_retry_count = 0
    last_start_cmd_ts: float = 0.0
    current_mode: str = "idle"
    eeg_streaming_enabled = False

    def _publish_ppg(message: Dict[str, Any]) -> None:
        """
        将高频 PPG 数据投递到独立队列，避免阻塞通用状态队列。

        ``ppg_queue`` 为空时保留旧调用方的兼容路径；生产采集进程会传入
        独立队列，并使用 ``put_nowait``，这样 BLE 通知回调不会等待主进程
        消费诊断/生命周期状态。
        """
        target = ppg_queue if ppg_queue is not None else status_queue
        try:
            target.put_nowait(message)
        except AttributeError:
            # 兼容极简测试队列或第三方队列实现。
            target.put(message)
        except queue.Full:
            # PPG 只影响独立的可视化通道；队列满时丢弃该点，不能反过来
            # 阻塞 EEG 采集回调。
            return
        except (EOFError, OSError, ValueError):
            return

    imp_buf = bytearray()
    imp_frame_counter = 0
    imp_notify_counter = 0
    imp_last_notify_ts: float = 0.0
    imp_no_data_reported = False
    impedance_streaming_enabled = False

    tdcs_buf = bytearray()
    tdcs_notify_counter = 0
    tdcs_last_notify_ts: float = 0.0
    tdcs_no_data_reported = False
    tdcs_streaming_enabled = False

    def _reset_eeg_stats_window(now_ts: float) -> None:
        """
        重置 EEG 统计窗口。
        """
        nonlocal eeg_stats_window_start_ts, eeg_window_valid_frames, eeg_window_invalid_frames, eeg_window_lost_by_seq, eeg_window_dropped_bytes
        eeg_stats_window_start_ts = float(now_ts)
        eeg_window_valid_frames = 0
        eeg_window_invalid_frames = 0
        eeg_window_lost_by_seq = 0
        eeg_window_dropped_bytes = 0

    def _maybe_report_eeg_stats(now_ts: float) -> None:
        """
        周期性上报 EEG 丢包率与实际帧率。
        """
        nonlocal eeg_stats_last_report_ts
        if debug_queue is None:
            return
        if (now_ts - eeg_stats_last_report_ts) < 10.0:
            return
        elapsed = max(1e-6, now_ts - eeg_stats_window_start_ts)
        expected_fps = 0.0
        try:
            expected_fps = float(cfg.eeg.sampling_rate_hz) / float(eeg_proto.frame.samples_per_frame)
        except Exception:
            expected_fps = 0.0
        valid_fps = float(eeg_window_valid_frames) / float(elapsed)
        actual_hz = valid_fps * float(eeg_proto.frame.samples_per_frame)
        lost_total = int(eeg_window_lost_by_seq) + int(eeg_window_invalid_frames)
        expected_total = int(eeg_window_valid_frames) + int(lost_total)
        loss_rate = float(lost_total) / float(expected_total) if expected_total > 0 else 0.0
        try:
            debug_queue.put(
                {
                    "tag": "EEG_STATS",
                    "message": "EEG 丢包率与帧率统计（10秒）",
                    "data": {
                        "window_sec": float(elapsed),
                        "valid_frames": int(eeg_window_valid_frames),
                        "invalid_frames": int(eeg_window_invalid_frames),
                        "lost_by_seq": int(eeg_window_lost_by_seq),
                        "loss_rate": float(loss_rate),
                        "valid_fps": float(valid_fps),
                        "expected_fps": float(expected_fps),
                        "actual_sampling_hz": float(actual_hz),
                        "expected_sampling_hz": float(cfg.eeg.sampling_rate_hz),
                        "dropped_bytes": int(eeg_window_dropped_bytes),
                    },
                }
            )
        except Exception:
            return
        eeg_stats_last_report_ts = float(now_ts)
        _reset_eeg_stats_window(now_ts)

    def on_notify(_: int, data: bytearray) -> None:
        nonlocal frame_counter, notify_counter, last_notify_ts, no_data_reported, eeg_streaming_enabled
        nonlocal eeg_last_seq, eeg_stats_last_report_ts, eeg_window_valid_frames, eeg_window_invalid_frames, eeg_window_lost_by_seq, eeg_window_dropped_bytes
        nonlocal imp_frame_counter, imp_notify_counter, imp_last_notify_ts, imp_no_data_reported, impedance_streaming_enabled
        nonlocal tdcs_buf, tdcs_notify_counter, tdcs_last_notify_ts, tdcs_no_data_reported, tdcs_streaming_enabled

        if impedance_streaming_enabled:
            if imp_spec is None or imp_outlet is None:
                return
            imp_notify_counter += 1
            imp_last_notify_ts = time.time()
            imp_no_data_reported = False
            if debug_queue is not None:
                try:
                    if imp_notify_counter % 5 == 0:
                        debug_queue.put(
                            {
                                "tag": "IMP_RX",
                                "message": "收到阻抗通知数据",
                                "data": {"len": int(len(data))},
                            }
                        )
                except Exception:
                    pass

            expected = int(imp_spec.frame_len_bytes)
            hdr0, hdr1 = int(imp_spec.header[0]) & 0xFF, int(imp_spec.header[1]) & 0xFF
            if len(data) == expected and len(data) >= 2 and data[0] == hdr0 and data[1] == hdr1:
                imp_buf.clear()
                frames = [bytes(data)]
            else:
                imp_buf.extend(data)
                header_bytes = bytes([hdr0, hdr1])
                frames = []
                for _ in range(20):
                    pos = imp_buf.find(header_bytes)
                    if pos < 0:
                        if len(imp_buf) > 1:
                            del imp_buf[:-1]
                        break
                    if pos > 0:
                        del imp_buf[:pos]
                    if len(imp_buf) < expected:
                        break
                    frames.append(bytes(imp_buf[:expected]))
                    del imp_buf[:expected]

            for fr in frames:
                try:
                    ch_ohm, gain_coeff, bias_ohm, tdcs_ohm = parse_impedance_frame(fr, imp_spec)
                    vec = build_impedance_vector(ch_ohm, bias_ohm, tdcs_ohm)
                    imp_outlet.push_samples([vec])
                    imp_frame_counter += 1
                    if debug_queue is not None and imp_frame_counter % 10 == 0:
                        try:
                            vmin = float(min(ch_ohm)) if ch_ohm else 0.0
                            vmax = float(max(ch_ohm)) if ch_ohm else 0.0
                            debug_queue.put(
                                {
                                    "tag": "IMP_FRAME",
                                    "message": "阻抗帧解析并推送",
                                    "data": {
                                        "channels": int(len(ch_ohm)),
                                        "min_ohm": vmin,
                                        "max_ohm": vmax,
                                        "bias_ohm": float(bias_ohm) if bias_ohm is not None else None,
                                        "tdcs_ohm": float(tdcs_ohm) if tdcs_ohm is not None else None,
                                        "gain_coeff": float(gain_coeff),
                                    },
                                }
                            )
                        except Exception:
                            pass
                except Exception as e:
                    if debug_queue is not None:
                        try:
                            debug_queue.put({"tag": "IMP_PARSE", "message": "阻抗帧解析失败", "data": {"error": str(e), "len": int(len(fr))}})
                        except Exception:
                            pass
            return

        if tdcs_streaming_enabled:
            tdcs_notify_counter += 1
            tdcs_last_notify_ts = time.time()
            tdcs_no_data_reported = False
            
            if debug_queue is not None:
                try:
                    if tdcs_notify_counter % 5 == 0:
                        debug_queue.put(
                            {
                                "tag": "TDCS_RX",
                                "message": "收到tDCS通知数据",
                                "data": {"len": int(len(data))},
                            }
                        )
                except Exception:
                    pass
                    
            expected = 10
            hdr0, hdr1 = 0xEB, 0x90
            
            if len(data) == expected and len(data) >= 2 and data[0] == hdr0 and data[1] == hdr1:
                tdcs_buf.clear()
                frames = [bytes(data)]
            else:
                tdcs_buf.extend(data)
                header_bytes = bytes([hdr0, hdr1])
                frames = []
                for _ in range(20):
                    pos = tdcs_buf.find(header_bytes)
                    if pos < 0:
                        if len(tdcs_buf) > 1:
                            del tdcs_buf[:-1]
                        break
                    if pos > 0:
                        del tdcs_buf[:pos]
                    if len(tdcs_buf) < expected:
                        break
                    frames.append(bytes(tdcs_buf[:expected]))
                    del tdcs_buf[:expected]
                    
            for fr in frames:
                try:
                    out_curr_raw = int.from_bytes(fr[2:5], byteorder="big", signed=False)
                    out_curr_uA = out_curr_raw * 0.3125 / 40.0 - 444.0
                    
                    hv_raw = int.from_bytes(fr[5:8], byteorder="big", signed=False)
                    hv_uV = hv_raw * 195.3125
                    
                    status_byte = int(fr[8])
                    is_working = bool(status_byte & 0x04)
                    open_circuit = bool(status_byte & 0x02)
                    over_current = bool(status_byte & 0x01)
                    
                    battery = int(fr[9])
                    if battery >= 0 and battery <= 100:
                        status_queue.put({"type": "battery", "value": battery})
                        
                    if debug_queue is not None:
                        try:
                            debug_queue.put(
                                {
                                    "tag": "TDCS_FRAME",
                                    "message": "tDCS 监测数据解析",
                                    "data": {
                                        "out_curr_raw": out_curr_raw,
                                        "out_curr_uA": out_curr_uA,
                                        "hv_raw": hv_raw,
                                        "hv_uV": hv_uV,
                                        "is_working": is_working,
                                        "open_circuit": open_circuit,
                                        "over_current": over_current,
                                        "battery": battery,
                                    },
                                }
                            )
                        except Exception:
                            pass
                except Exception as e:
                    if debug_queue is not None:
                        try:
                            debug_queue.put({"tag": "TDCS_PARSE", "message": "tDCS 帧解析失败", "data": {"error": str(e), "len": int(len(fr))}})
                        except Exception:
                            pass
            return

        notify_counter += 1
        last_notify_ts = time.time()
        no_data_reported = False
        if not eeg_streaming_enabled:
            return
        if spec is None or outlet is None or eeg_decoder is None:
            return
        if debug_queue is not None:
            try:
                if notify_counter % 10 == 0:
                    debug_queue.put(
                        {
                            "tag": "EEG_RX",
                            "message": "收到EEG通知数据",
                            "data": {"len": int(len(data))},
                        }
                    )
            except Exception:
                pass
        now_ts = time.time()
        _maybe_report_eeg_stats(now_ts)

        def _push_one_frame(one_frame: bytes) -> None:
            nonlocal frame_counter, eeg_last_seq, eeg_window_valid_frames, eeg_window_lost_by_seq
            nonlocal ppg_frame_time

            decoded_frame = parse_frame(one_frame, spec)
            seq = decoded_frame.sequence
            # Use the device frame cadence so coalesced BLE notifications retain
            # their sample spacing. Sequence gaps also remain visible in time.
            frame_period = spec.samples_per_frame / float(cfg.eeg.sampling_rate_hz)
            if eeg_last_seq is None:
                ppg_frame_time = time.time()
            else:
                frame_steps = ((int(seq) - int(eeg_last_seq)) & 0xFF) if seq is not None else 1
                ppg_frame_time += max(1, frame_steps) * frame_period
            if seq is not None and eeg_last_seq is not None:
                gap = (int(seq) - int(eeg_last_seq) - 1) & 0xFF
                if gap > 0:
                    eeg_window_lost_by_seq += int(gap)
            if seq is not None:
                eeg_last_seq = int(seq)

            samples = decoded_frame.samples
            battery = decoded_frame.battery_level
            imu = decoded_frame.imu
            frame_counter += 1
            eeg_window_valid_frames += 1
            if cfg.eeg.lsl.include_trigger_channel:
                samples = [s[:eeg_n_channels + 1] for s in samples]
            else:
                samples = [s[:eeg_n_channels] for s in samples]
            outlet.push_samples(samples)
            if spec.battery_len_bytes > 0 and (frame_counter == 1 or frame_counter % 50 == 0):
                status_queue.put(
                    {
                        "type": "battery",
                        "value": int(battery) if battery is not None else None,
                        "valid": battery is not None,
                    }
                )
            if imu and frame_counter % 50 == 0:
                status_queue.put({"type": "imu", "value": imu})
            if decoded_frame.ppg:
                # Forward every updated sample; unchanged frames remain useful
                # as occasional diagnostic snapshots, never waveform points.
                if decoded_frame.ppg.get("valid") or frame_counter == 1 or frame_counter % 50 == 0:
                    _publish_ppg({"type": "ppg", "value": decoded_frame.ppg, "ts": ppg_frame_time})

        decoded = eeg_decoder.feed(bytes(data))
        eeg_window_invalid_frames += int(decoded.invalid_frames)
        eeg_window_dropped_bytes += int(decoded.dropped_bytes)
        for one_frame in decoded.frames:
            _push_one_frame(one_frame)
        _maybe_report_eeg_stats(time.time())

    status_payload: Dict[str, Any] = {"type": "connecting", "address": address, "name": resolved_name}
    if module_info is not None:
        status_payload["module"] = {"eeg_channels": int(module_info.eeg_channels), "stim_channels": int(module_info.stim_channels)}
    status_queue.put(status_payload)

    def _emit_disconnected_status(message: str) -> None:
        """
        上报蓝牙已断开状态，供主进程及时回收旧连接资源。
        """
        payload: Dict[str, Any] = {
            "type": "disconnected",
            "message": str(message or "蓝牙连接已断开"),
            "address": address,
            "name": resolved_name,
        }
        if module_info is not None:
            payload["module"] = {"eeg_channels": int(module_info.eeg_channels), "stim_channels": int(module_info.stim_channels)}
        status_queue.put(payload)

    while not stop_event.is_set():
        try:
            async with BleakClient(ble_device or address) as client:
                status_payload = {"type": "connected", "address": address, "name": resolved_name}
                if module_info is not None:
                    status_payload["module"] = {"eeg_channels": int(module_info.eeg_channels), "stim_channels": int(module_info.stim_channels)}
                status_queue.put(status_payload)
                await client.start_notify(notify_char, on_notify)
                notify_counter = 0
                last_notify_ts = time.time()
                no_data_reported = False
                eeg_last_seq = None
                eeg_stats_last_report_ts = time.time()
                _reset_eeg_stats_window(eeg_stats_last_report_ts)
                start_retry_count = 0
                last_start_cmd_ts = 0.0
                current_mode = "idle"
                eeg_streaming_enabled = False
                impedance_streaming_enabled = False
                imp_buf.clear()
                imp_frame_counter = 0
                imp_notify_counter = 0
                imp_last_notify_ts = 0.0
                imp_no_data_reported = False
                tdcs_streaming_enabled = False
                tdcs_buf.clear()
                tdcs_notify_counter = 0
                tdcs_last_notify_ts = 0.0
                tdcs_no_data_reported = False

                async def _send_cmd(cmd: List[int], action: str) -> None:
                    payload = bytearray(cmd)
                    await client.write_gatt_char(write_char, payload, response=write_with_response)
                    if debug_queue is not None:
                        try:
                            cmd_hex = " ".join(f"0x{int(x) & 0xFF:02X}" for x in cmd)
                            cmd_info = interpret_two_level_cmd(cmd)
                            debug_queue.put(
                                {
                                    "tag": "CMD_TX",
                                    "message": f"发送控制指令: {action}",
                                    "data": {"cmd_hex": cmd_hex, "write_char": str(write_char), "write_with_response": bool(write_with_response), **cmd_info},
                                }
                            )
                        except Exception:
                            pass

                try:
                    for one in cfg.bluetooth.commands.init_commands:
                        if one:
                            await _send_cmd(one, action="init")
                            await asyncio.sleep(0.05)
                except Exception as e:
                    if debug_queue is not None:
                        try:
                            debug_queue.put(
                                {
                                    "tag": "CMD_TX",
                                    "message": "发送 init 指令失败",
                                            "data": {"error": str(e), "write_char": str(write_char)},
                                }
                            )
                        except Exception:
                            pass

                status_payload = {"type": "ready", "address": address, "name": resolved_name}
                if module_info is not None:
                    status_payload["module"] = {"eeg_channels": int(module_info.eeg_channels), "stim_channels": int(module_info.stim_channels)}
                status_queue.put(status_payload)
                while not stop_event.is_set():
                    if not bool(getattr(client, "is_connected", False)):
                        _emit_disconnected_status("蓝牙连接已断开，等待重连")
                        break
                    try:
                        cmd_msg: Dict[str, Any] = command_queue.get_nowait()
                        msg_type = str(cmd_msg.get("type", ""))
                        if msg_type == "select_mode":
                            next_mode = str(cmd_msg.get("mode", "idle"))
                            current_mode = next_mode
                            status_queue.put({"type": "mode", "mode": current_mode})
                        elif msg_type == "start_mode":
                            mode = str(cmd_msg.get("mode", ""))
                            if mode == "eeg":
                                current_mode = "eeg"
                                eeg_streaming_enabled = False
                                impedance_streaming_enabled = False
                                imp_buf.clear()
                                if eeg_decoder is not None:
                                    eeg_decoder.clear()
                                eeg_last_seq = None
                                eeg_stats_last_report_ts = time.time()
                                _reset_eeg_stats_window(eeg_stats_last_report_ts)
                                notify_counter = 0
                                last_notify_ts = time.time()
                                no_data_reported = False
                                start_retry_count = 0
                                try:
                                    ensure_eeg_lsl_ready()
                                except Exception as e:
                                    status_queue.put({"type": "error", "message": f"创建 EEG LSL Outlet 失败：{str(e)}"})
                                    continue
                                if bool(getattr(cfg, "trigger", None) and cfg.trigger.enabled):
                                    await _send_cmd(_get_trigger_source_command(), action=f"select_trigger_source_{str(cfg.trigger.source_mode)}")
                                await _send_eeg_start_sequence(
                                    _send_cmd,
                                    cfg.bluetooth.commands.stop_eeg,
                                    cfg.bluetooth.commands.start_eeg,
                                )
                                # 预停止后的尾包不应被当成 START 已生效的证据，
                                # 否则会抑制后续的无数据重试。
                                notify_counter = 0
                                last_notify_ts = time.time()
                                no_data_reported = False
                                start_retry_count = 0
                                last_start_cmd_ts = time.time()
                                eeg_streaming_enabled = True
                                status_queue.put({"type": "mode_started", "mode": "eeg"})
                            elif mode == "impedance":
                                current_mode = "impedance"
                                eeg_streaming_enabled = False
                                impedance_streaming_enabled = False
                                if eeg_decoder is not None:
                                    eeg_decoder.clear()
                                imp_buf.clear()
                                imp_notify_counter = 0
                                imp_last_notify_ts = time.time()
                                imp_no_data_reported = False
                                try:
                                    ensure_impedance_lsl_ready()
                                except Exception as e:
                                    status_queue.put({"type": "error", "message": f"创建阻抗 LSL Outlet 失败：{str(e)}"})
                                    continue
                                await _send_cmd(cfg.bluetooth.commands.start_impedance, action="start_impedance")
                                impedance_streaming_enabled = True
                                status_queue.put({"type": "mode_started", "mode": "impedance"})
                            elif mode == "tdcs":
                                if int(eeg_n_channels) != 8:
                                    status_queue.put({"type": "error", "message": f"{int(eeg_n_channels)}通道模式不支持电刺激（tDCS）"})
                                    continue
                                if module_info is None or int(module_info.stim_channels) <= 0:
                                    status_queue.put({"type": "error", "message": "当前设备不带电刺激模块，电刺激（tDCS）已禁用"})
                                    continue
                                current_mode = "tdcs"
                                eeg_streaming_enabled = False
                                impedance_streaming_enabled = False
                                tdcs_streaming_enabled = False
                                imp_buf.clear()
                                if eeg_decoder is not None:
                                    eeg_decoder.clear()
                                tdcs_buf.clear()
                                tdcs_notify_counter = 0
                                tdcs_last_notify_ts = time.time()
                                tdcs_no_data_reported = False
                                await _send_cmd(cfg.bluetooth.commands.start_tdcs, action="start_tdcs")
                                tdcs_streaming_enabled = True
                                status_queue.put({"type": "mode_started", "mode": "tdcs"})
                        elif msg_type == "send_cmd":
                            raw = cmd_msg.get("cmd", [])
                            cmd: List[int] = []
                            if isinstance(raw, list):
                                for x in raw:
                                    try:
                                        v = int(x) & 0xFF
                                    except Exception:
                                        continue
                                    cmd.append(v)
                            if len(cmd) < 2:
                                if debug_queue is not None:
                                    try:
                                        debug_queue.put(
                                            {
                                                "tag": "CMD_TX",
                                                "message": "忽略非法控制指令：长度不足 2 字节",
                                                "data": {"raw": raw},
                                            }
                                        )
                                    except Exception:
                                        pass
                                continue
                            await _send_cmd(cmd, action="panel_send_cmd")
                        elif msg_type == "stop_mode":
                            mode = str(cmd_msg.get("mode", ""))
                            if mode == "eeg":
                                eeg_streaming_enabled = False
                                if eeg_decoder is not None:
                                    eeg_decoder.clear()
                                try:
                                    await _send_cmd(cfg.bluetooth.commands.stop_eeg, action="stop_eeg")
                                except Exception as e:
                                    if debug_queue is not None:
                                        try:
                                            debug_queue.put(
                                                {
                                                    "tag": "CMD_TX",
                                                    "message": "发送 stop_eeg 指令失败",
                                                    "data": {"error": str(e), "write_char": str(write_char)},
                                                }
                                            )
                                        except Exception:
                                            pass
                                    if not bool(getattr(client, "is_connected", True)):
                                        _emit_disconnected_status("蓝牙连接已断开，等待重连")
                                status_queue.put({"type": "mode_stopped", "mode": "eeg"})
                            elif mode == "impedance":
                                eeg_streaming_enabled = False
                                if eeg_decoder is not None:
                                    eeg_decoder.clear()
                                impedance_streaming_enabled = False
                                imp_buf.clear()
                                try:
                                    await _send_cmd(cfg.bluetooth.commands.stop_impedance, action="stop_impedance")
                                except Exception as e:
                                    if debug_queue is not None:
                                        try:
                                            debug_queue.put(
                                                {
                                                    "tag": "CMD_TX",
                                                    "message": "发送 stop_impedance 指令失败",
                                                    "data": {"error": str(e), "write_char": str(write_char)},
                                                }
                                            )
                                        except Exception:
                                            pass
                                    if not bool(getattr(client, "is_connected", True)):
                                        _emit_disconnected_status("蓝牙连接已断开，等待重连")
                                status_queue.put({"type": "mode_stopped", "mode": "impedance"})
                            elif mode == "tdcs":
                                eeg_streaming_enabled = False
                                if eeg_decoder is not None:
                                    eeg_decoder.clear()
                                impedance_streaming_enabled = False
                                imp_buf.clear()
                                tdcs_streaming_enabled = False
                                tdcs_buf.clear()
                                try:
                                    await _send_cmd(cfg.bluetooth.commands.stop_tdcs, action="stop_tdcs")
                                except Exception as e:
                                    if debug_queue is not None:
                                        try:
                                            debug_queue.put(
                                                {
                                                    "tag": "CMD_TX",
                                                    "message": "发送 stop_tdcs 指令失败",
                                                    "data": {"error": str(e), "write_char": str(write_char)},
                                                }
                                            )
                                        except Exception:
                                            pass
                                    if not bool(getattr(client, "is_connected", True)):
                                        _emit_disconnected_status("蓝牙连接已断开，等待重连")
                                status_queue.put({"type": "mode_stopped", "mode": "tdcs"})
                            current_mode = "idle"
                            status_queue.put({"type": "mode", "mode": current_mode})
                    except queue.Empty:
                        pass
                    except Exception as e:
                        status_queue.put({"type": "error", "message": str(e)})

                    # 持续无通知时仅上报一次调试事件。
                    if eeg_streaming_enabled and debug_queue is not None and not no_data_reported:
                        if last_notify_ts > 0 and notify_counter == 0 and (time.time() - last_notify_ts) > 3.0:
                            no_data_reported = True
                            try:
                                debug_queue.put(
                                    {
                                        "tag": "EEG_NODATA",
                                        "message": "3秒未收到EEG通知数据（可能 notify/write 特征不匹配，或设备未开始传输）",
                                        "data": {"notify_char": str(notify_char), "write_char": str(write_char)},
                                    }
                                )
                            except Exception:
                                pass

                    if impedance_streaming_enabled and debug_queue is not None and not imp_no_data_reported:
                        if imp_last_notify_ts > 0 and imp_notify_counter == 0 and (time.time() - imp_last_notify_ts) > 3.0:
                            imp_no_data_reported = True
                            try:
                                debug_queue.put(
                                    {
                                        "tag": "IMP_NODATA",
                                        "message": "3秒未收到阻抗通知数据（可能 notify/write 特征不匹配，或设备未开始传输）",
                                        "data": {"notify_char": str(notify_char), "write_char": str(write_char)},
                                    }
                                )
                            except Exception:
                                pass

                    if tdcs_streaming_enabled and debug_queue is not None and not tdcs_no_data_reported:
                        if tdcs_last_notify_ts > 0 and tdcs_notify_counter == 0 and (time.time() - tdcs_last_notify_ts) > 3.0:
                            tdcs_no_data_reported = True
                            try:
                                debug_queue.put(
                                    {
                                        "tag": "TDCS_NODATA",
                                        "message": "3秒未收到tDCS通知数据（可能 notify/write 特征不匹配，或设备未开始传输）",
                                        "data": {"notify_char": str(notify_char), "write_char": str(write_char)},
                                    }
                                )
                            except Exception:
                                pass

                    if eeg_streaming_enabled and debug_queue is not None and notify_counter == 0 and start_retry_count < 2:
                        if last_start_cmd_ts > 0 and (time.time() - last_start_cmd_ts) > 1.0:
                            start_retry_count += 1
                            last_start_cmd_ts = time.time()
                            try:
                                await _send_cmd(cfg.bluetooth.commands.start_eeg, action=f"start_eeg_retry_{int(start_retry_count)}")
                            except Exception as e:
                                try:
                                    debug_queue.put(
                                        {
                                            "tag": "CMD_TX",
                                            "message": "重发 start_eeg 指令失败",
                                            "data": {"retry": int(start_retry_count), "error": str(e), "write_char": str(write_char)},
                                        }
                                    )
                                except Exception:
                                    pass
                    await asyncio.sleep(0.05)
                try:
                    await client.stop_notify(notify_char)
                except Exception:
                    pass
                if eeg_streaming_enabled:
                    try:
                        await _send_cmd(cfg.bluetooth.commands.stop_eeg, action="stop_eeg")
                    except Exception as e:
                        if debug_queue is not None:
                            try:
                                debug_queue.put(
                                    {
                                        "tag": "CMD_TX",
                                        "message": "发送 stop_eeg 指令失败",
                                        "data": {"error": str(e), "write_char": str(write_char)},
                                    }
                                )
                            except Exception:
                                pass
                break
        except Exception as e:
            status_queue.put({"type": "error", "message": str(e), "address": address, "name": resolved_name})
        await asyncio.sleep(1.0)


def run_ble_acquisition_process(
    config_path: str,
    stop_event: multiprocessing.Event,
    status_queue: multiprocessing.Queue,
    command_queue: multiprocessing.Queue,
    debug_queue: Optional[multiprocessing.Queue] = None,
    connect_address: Optional[str] = None,
    connect_name: Optional[str] = None,
    ppg_queue: Optional[multiprocessing.Queue] = None,
) -> None:
    """
    BLE 采集进程入口。
    """
    try:
        asyncio.run(
            _connect_and_stream(
                config_path,
                stop_event,
                status_queue,
                command_queue,
                debug_queue,
                connect_address,
                connect_name,
                ppg_queue,
            )
        )
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # 必须在队列中放入错误状态，否则 eeg_controller.start_device 会空等 30 秒后抛出空字符串异常
        try:
            status_queue.put_nowait(
                {
                    "type": "error",
                    "message": str(exc) or "BLE 采集进程启动失败",
                    "address": connect_address,
                    "name": connect_name,
                }
            )
        except Exception:
            pass
    finally:
        time.sleep(0.1)
