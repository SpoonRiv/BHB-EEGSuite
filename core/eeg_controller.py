#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Copyright (c) 2026 BUAA BHB. All rights reserved.

文件功能: EEG 采集控制编排（启动/停止 BLE 采集进程），并向上层提供统一的设备生命周期管理接口
作者: Spoon
"""

import multiprocessing
import os
import logging
import time
import queue
import threading
from typing import Any, Dict, Optional

from configs.config_loader import load_config
from core.ble.acquisition_process import run_ble_acquisition_process
from core.ble.module_naming import parse_ble_module_name
from core.ppg_stream import PpgBuffer


class EEGController:
    """
    蓝牙 EEG 采集控制器，统一管理采集进程与模式切换。
    """

    def __init__(self, config_path: Optional[str] = None):
        self.process: Optional[multiprocessing.Process] = None
        self.stop_event: Optional[multiprocessing.Event] = None
        self.status_queue: Optional[multiprocessing.Queue] = None
        self.command_queue: Optional[multiprocessing.Queue] = None
        self.debug_queue: Optional[multiprocessing.Queue] = None
        self.ppg_queue: Optional[multiprocessing.Queue] = None
        self._ppg_drain_stop = threading.Event()
        self._ppg_drain_thread: Optional[threading.Thread] = None
        self._ppg_lock = threading.Lock()
        self._ppg_accepting = False
        self.last_status: Optional[Dict[str, Any]] = None
        self.last_battery: Optional[Dict[str, Any]] = None
        self.last_imu: Optional[Dict[str, Any]] = None
        self.last_ppg: Optional[Dict[str, Any]] = None
        self.ppg_buffer = PpgBuffer()
        self.current_mode: str = "idle"
        self.task_running: bool = False
        self.task_mode: str = ""
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "configs",
            "config.yaml",
        )
        self.config = load_config(self.config_path)

    def is_running(self) -> bool:
        """
        返回采集进程是否仍在运行。
        """
        self._sync_process_lifecycle()
        return bool(self.process and self.process.is_alive())

    def _clear_runtime_handles(self) -> None:
        """
        清理采集进程相关句柄，但保留最近一次状态，便于前端展示断联原因。
        """
        self._stop_ppg_drain_thread()
        self.process = None
        self.stop_event = None
        self.status_queue = None
        self.command_queue = None
        self.debug_queue = None
        self.ppg_queue = None
        self.current_mode = "idle"
        self.last_battery = None
        self.last_imu = None
        with self._ppg_lock:
            self._ppg_accepting = False
            self.last_ppg = None
            self.ppg_buffer.clear()
        self.task_running = False
        self.task_mode = ""

    def _start_ppg_drain_thread(self) -> None:
        """
        启动独立线程消费采集进程的 PPG 队列。

        PPG 是高频可视化数据，不能在 FastAPI 事件循环中同步清空跨进程
        队列；线程只更新本控制器内的 PPG 缓冲，不参与 EEG/设备状态处理。
        """
        if self.ppg_queue is None:
            return
        if self._ppg_drain_thread is not None and self._ppg_drain_thread.is_alive():
            return
        self._ppg_drain_stop.clear()
        self._ppg_drain_thread = threading.Thread(
            target=self._ppg_drain_loop,
            name="ppg-status-drain",
            daemon=True,
        )
        self._ppg_drain_thread.start()

    def _stop_ppg_drain_thread(self) -> None:
        """停止 PPG 消费线程并关闭父进程侧队列句柄。"""
        self._ppg_drain_stop.set()
        thread = self._ppg_drain_thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        self._ppg_drain_thread = None
        ppg_queue = self.ppg_queue
        if ppg_queue is not None:
            try:
                ppg_queue.cancel_join_thread()
            except Exception:
                pass
            try:
                ppg_queue.close()
            except Exception:
                pass

    def _ppg_drain_loop(self) -> None:
        """在线程中持续读取 PPG 消息，避免占用 FastAPI 事件循环。"""
        while not self._ppg_drain_stop.is_set():
            ppg_queue = self.ppg_queue
            if ppg_queue is None:
                return
            try:
                message = ppg_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            except (EOFError, OSError, ValueError):
                return
            if isinstance(message, dict) and str(message.get("type", "")) == "ppg":
                self._receive_ppg(message)

    def _sync_process_lifecycle(self) -> None:
        """
        同步采集进程生命周期，确保断联/异常退出后能及时释放旧句柄。
        """
        if self.process and not self.process.is_alive():
            if self.status_queue:
                self._drain_status_queue()
                if self.process is None:
                    return
            try:
                self.process.join(timeout=0.1)
            except Exception:
                pass
            self._clear_runtime_handles()

    def _reclaim_stale_process(self, timeout_sec: float = 0.6) -> None:
        """
        回收已经断联或异常的旧采集进程，避免阻塞下一次重连。
        """
        if not self.process:
            self._clear_runtime_handles()
            return
        try:
            if self.process.is_alive():
                self.process.join(timeout=max(0.1, float(timeout_sec)))
            if self.process.is_alive():
                logging.warning("Stale EEG process still alive after disconnect/error, terminating it.")
                self.process.terminate()
                self.process.join(timeout=1.0)
        except Exception as exc:
            logging.warning("Failed to reclaim stale EEG process: %s", exc)
        finally:
            self._clear_runtime_handles()

    def get_status(self) -> Dict[str, Any]:
        """
        获取采集侧状态快照。
        """
        self._drain_status_queue()
        self._sync_process_lifecycle()
        configured_name = self.config.bluetooth.target_device
        last = self.last_status or {"type": "idle", "message": "未启动", "name": configured_name}
        if "name" not in last:
            last = {**last, "name": configured_name}

        module = None
        try:
            m_raw = last.get("module", None) if isinstance(last, dict) else None
            if isinstance(m_raw, dict) and "eeg_channels" in m_raw and "stim_channels" in m_raw:
                module = {"eeg_channels": int(m_raw.get("eeg_channels", 0)), "stim_channels": int(m_raw.get("stim_channels", 0))}
            else:
                info = parse_ble_module_name(str(last.get("name", "") or ""), str(self.config.bluetooth.module_name_regex or ""))
                if info is not None:
                    module = {"eeg_channels": int(info.eeg_channels), "stim_channels": int(info.stim_channels)}
        except Exception:
            module = None

        tdcs_capable: Optional[bool]
        if module is not None:
            tdcs_capable = bool(int(module.get("stim_channels", 0)) > 0)
        else:
            name_norm = str(last.get("name", "") or "").strip()
            tdcs_capable = False if name_norm == "MSM" else None
        with self._ppg_lock:
            last_ppg = dict(self.last_ppg) if isinstance(self.last_ppg, dict) else self.last_ppg
        return {
            "running": self.is_running(),
            "last": last,
            "configured_name": configured_name,
            "mode": self.current_mode,
            "task_running": bool(self.task_running),
            "task_mode": str(self.task_mode or ""),
            "battery": self.last_battery,
            "imu": self.last_imu,
            "ppg": last_ppg,
            "module": module,
            "capabilities": {"tdcs": tdcs_capable},
        }

    def start_device(self, address: Optional[str] = None, name: Optional[str] = None) -> bool:
        """
        启动采集进程并建立 BLE 连接。
        """
        self._drain_status_queue()
        self._sync_process_lifecycle()
        last_type = str((self.last_status or {}).get("type", "")).strip().lower() if isinstance(self.last_status, dict) else ""
        if last_type in {"disconnected", "error", "stopped"} and self.process:
            self._reclaim_stale_process()
        if self.process and self.process.is_alive():
            logging.warning("EEG device process is already running.")
            return True

        try:
            self.stop_event = multiprocessing.Event()
            self.status_queue = multiprocessing.Queue()
            self.command_queue = multiprocessing.Queue()
            self.debug_queue = multiprocessing.Queue()
            self.ppg_queue = multiprocessing.Queue()
            self.process = multiprocessing.Process(
                target=run_ble_acquisition_process,
                args=(
                    self.config_path,
                    self.stop_event,
                    self.status_queue,
                    self.command_queue,
                    self.debug_queue,
                    address,
                    name,
                    self.ppg_queue,
                ),
            )
            self.process.start()
            self._start_ppg_drain_thread()

            deadline = time.time() + 30.0
            while time.time() < deadline:
                remaining = max(0.1, deadline - time.time())
                msg: Dict[str, Any] = self.status_queue.get(timeout=remaining)
                msg_type = str(msg.get("type", "")) if isinstance(msg, dict) else ""
                if msg_type in {"connecting", "connected", "ready", "error", "stopped", "idle", "disconnected"}:
                    self.last_status = msg
                elif msg_type == "battery" and "value" in msg:
                    battery_value = msg.get("value")
                    self.last_battery = {
                        "value": int(battery_value) if battery_value is not None else None,
                        "valid": battery_value is not None,
                        "ts": time.time(),
                    }
                elif msg_type == "imu" and "value" in msg:
                    self.last_imu = {"value": msg.get("value"), "ts": time.time()}
                elif msg_type == "ppg" and "value" in msg:
                    self._receive_ppg(msg)

                if msg.get("type") == "connected":
                    logging.info("Bluetooth EEG device connected successfully.")
                    self.current_mode = "idle"
                    return True
                if msg.get("type") == "error":
                    logging.error(f"EEG device start error: {msg.get('message')}")
                    self._reclaim_stale_process()
                    return False
            self.last_status = {"type": "error", "message": "蓝牙连接超时（30秒）", "address": address, "name": name or self.config.bluetooth.target_device}
            logging.error("EEG device start error: 蓝牙连接超时（30秒）")
            self._reclaim_stale_process()
            return False
        except Exception as e:
            logging.error(f"Failed to start EEG device: {e}")
            self.last_status = {"type": "error", "message": str(e), "address": address, "name": name or self.config.bluetooth.target_device}
            self._reclaim_stale_process()
            return False

    def select_mode(self, mode: str) -> bool:
        """
        选择业务模式，不自动下发启动指令。
        """
        if not self.command_queue or not self.is_running():
            return False
        self.command_queue.put({"type": "select_mode", "mode": str(mode)})
        self.current_mode = str(mode)
        return True

    def start_mode(self, mode: str) -> bool:
        """
        启动指定模式。
        """
        if not self.command_queue or not self.is_running():
            return False
        with self._ppg_lock:
            self._ppg_accepting = str(mode) == "eeg"
            self.ppg_buffer.clear()
            self.last_ppg = None
        self.command_queue.put({"type": "start_mode", "mode": str(mode)})
        self.current_mode = str(mode)
        return True

    def stop_mode(self, mode: str) -> bool:
        """
        停止指定模式。
        """
        if not self.command_queue or not self.is_running():
            return False
        with self._ppg_lock:
            self._ppg_accepting = False
            self.ppg_buffer.clear()
            self.last_ppg = None
        self.command_queue.put({"type": "stop_mode", "mode": str(mode)})
        self.current_mode = "idle"
        return True

    def send_two_level_command(self, l1: int, l2: int, data: Optional[list[int]] = None) -> bool:
        """
        向采集进程投递两级控制指令。
        """
        if not self.command_queue or not self.is_running():
            return False
        cmd: list[int] = [int(l1) & 0xFF, int(l2) & 0xFF]
        if data:
            for x in data:
                cmd.append(int(x) & 0xFF)
        self.command_queue.put({"type": "send_cmd", "cmd": cmd})
        return True

    def send_trigger_command(self, command: str, source: str) -> bool:
        """
        向采集进程投递设备 trigger 控制指令。
        """

        if not self.command_queue or not self.is_running():
            return False
        cmd = str(command or "").strip().lower()
        _ = str(source or "").strip()
        if cmd.startswith("start"):
            cmd_bytes = list(self.config.bluetooth.commands.trigger_set)
        elif cmd.startswith("end") or cmd.startswith("stop"):
            cmd_bytes = list(self.config.bluetooth.commands.trigger_clear)
        else:
            return False
        if len(cmd_bytes) < 2:
            return False
        self.command_queue.put({"type": "send_cmd", "cmd": cmd_bytes})
        return True

    def _receive_ppg(self, msg: Dict[str, Any]) -> None:
        try:
            timestamp = float(msg.get("ts", time.time()))
        except (TypeError, ValueError):
            timestamp = time.time()
        with self._ppg_lock:
            if not self._ppg_accepting:
                return
            self.last_ppg = {"value": msg.get("value"), "ts": timestamp}
            self.ppg_buffer.append(msg.get("value"), timestamp)

    def get_ppg_snapshot(self, after_id: int = 0) -> Dict[str, Any]:
        self._drain_status_queue()
        self._sync_process_lifecycle()
        return {
            "type": "ppg_data",
            "active": bool(self.task_running and self.task_mode == "eeg"),
            **self.ppg_buffer.snapshot(after_id),
        }

    def _drain_status_queue(self) -> None:
        """
        无阻塞读取状态队列，并更新本地缓存。
        """
        if not self.status_queue:
            return
        needs_reclaim = False
        while True:
            try:
                msg = self.status_queue.get_nowait()
                if isinstance(msg, dict):
                    msg_type = str(msg.get("type", ""))
                    if msg_type in {"connecting", "connected", "ready", "error", "stopped", "idle", "disconnected"}:
                        self.last_status = msg
                    if msg_type == "battery" and "value" in msg:
                        battery_value = msg.get("value")
                        self.last_battery = {
                            "value": int(battery_value) if battery_value is not None else None,
                            "valid": battery_value is not None,
                            "ts": time.time(),
                        }
                    if msg_type == "imu" and "value" in msg:
                        self.last_imu = {"value": msg.get("value"), "ts": time.time()}
                    if msg_type == "ppg" and "value" in msg:
                        self._receive_ppg(msg)
                    if msg_type == "mode" and "mode" in msg:
                        self.current_mode = str(msg.get("mode"))
                    if msg_type in {"mode_started", "mode_stopped"} and "mode" in msg:
                        with self._ppg_lock:
                            self._ppg_accepting = msg_type == "mode_started" and str(msg.get("mode")) == "eeg"
                            self.ppg_buffer.clear()
                            self.last_ppg = None
                        if msg_type == "mode_started":
                            self.current_mode = str(msg.get("mode"))
                            self.task_running = True
                            self.task_mode = str(msg.get("mode"))
                        else:
                            self.current_mode = "idle"
                            self.task_running = False
                            self.task_mode = ""
                    if msg_type in {"disconnected", "error"}:
                        with self._ppg_lock:
                            self._ppg_accepting = False
                        self.current_mode = "idle"
                        self.task_running = False
                        self.task_mode = ""
                        needs_reclaim = True
            except queue.Empty:
                break
            except Exception:
                break
        if needs_reclaim:
            self._reclaim_stale_process()

    def stop_device(self) -> bool:
        """
        停止蓝牙采集进程。
        """
        self._drain_status_queue()
        self._sync_process_lifecycle()
        last = self.last_status if isinstance(self.last_status, dict) else {}
        last_name = str(last.get("name") or "").strip() or str(self.config.bluetooth.target_device or "")
        last_address = str(last.get("address") or "").strip() or None
        if not self.process or not self.process.is_alive():
            logging.warning("EEG device process is not running.")
            self.last_status = {"type": "stopped", "message": "采集未运行", "name": last_name, "address": last_address}
            return True

        try:
            if self.stop_event:
                self.stop_event.set()

            self.process.join(timeout=5)
            if self.process.is_alive():
                logging.warning("Process did not exit gracefully, terminating...")
                self.process.terminate()
                self.process.join()

            self._clear_runtime_handles()
            self.last_status = {"type": "stopped", "message": "采集已停止", "name": last_name, "address": last_address}
            return True
        except Exception as e:
            logging.error(f"Failed to stop EEG device: {e}")
            self.last_status = {"type": "error", "message": str(e), "name": last_name, "address": last_address}
            return False
