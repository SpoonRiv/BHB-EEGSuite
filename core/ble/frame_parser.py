#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BLE EEG 数据帧协议的定义、流式组帧与字段解码。

8 通道 V1.2 帧布局（140 bytes）为::

    AA BB | seq | EEG(120) | trigger(1) | PPG(10) | reserved(2)
           | battery(2) | SUM(1) | CC

其中 EEG 每个采样点按通道顺序排列，每通道 3 bytes、24-bit 大端有符号数；
Trigger 低 5 位从低到高对应一帧中的 5 个采样点；PPG 首字节为更新标志，
随后依次为绿光、红光、近红外三个 24-bit 大端无符号原始值。

模块同时兼容旧配置中的 ``imu_len_bytes`` 字段：当 PPG/预留长度为 0 时，
触发段之后仍按 IMU、battery 的旧布局解析。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class FrameSpec:
    """一类 EEG 帧的字节布局。"""

    channels: int
    header_len_bytes: int
    bytes_per_sample_per_channel: int
    samples_per_frame: int
    trigger_len_bytes: int
    imu_len_bytes: int
    battery_len_bytes: int
    tail_len_bytes: int
    ppg_len_bytes: int = 0
    reserved_len_bytes: int = 0
    header_bytes: Tuple[int, int] = (0xAA, 0xBB)
    tail_byte: int = 0xCC
    checksum_len_bytes: int = 1

    def __post_init__(self) -> None:
        for name in (
            "channels",
            "header_len_bytes",
            "bytes_per_sample_per_channel",
            "samples_per_frame",
            "trigger_len_bytes",
            "imu_len_bytes",
            "battery_len_bytes",
            "tail_len_bytes",
            "ppg_len_bytes",
            "reserved_len_bytes",
            "checksum_len_bytes",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} 不能为负数")
        if int(self.channels) <= 0 or int(self.bytes_per_sample_per_channel) <= 0:
            raise ValueError("channels 和 bytes_per_sample_per_channel 必须为正数")
        if int(self.samples_per_frame) <= 0:
            raise ValueError("samples_per_frame 必须为正数")
        if len(tuple(self.header_bytes)) != 2:
            raise ValueError("header_bytes 必须包含两个字节")

    @property
    def header(self) -> bytes:
        return bytes(int(value) & 0xFF for value in self.header_bytes)

    @property
    def eeg_payload_len_bytes(self) -> int:
        return int(self.channels) * int(self.bytes_per_sample_per_channel) * int(self.samples_per_frame)

    @property
    def payload_len_bytes(self) -> int:
        return (
            int(self.header_len_bytes)
            + self.eeg_payload_len_bytes
            + int(self.trigger_len_bytes)
            + int(self.ppg_len_bytes)
            + int(self.reserved_len_bytes)
            + int(self.imu_len_bytes)
            + int(self.battery_len_bytes)
        )

    @property
    def frame_len_bytes(self) -> int:
        return self.payload_len_bytes + int(self.tail_len_bytes)

    @property
    def checksum_offset(self) -> Optional[int]:
        if int(self.tail_len_bytes) < int(self.checksum_len_bytes) + 1:
            return None
        return self.frame_len_bytes - int(self.tail_len_bytes)

    def validate_checksum(self, frame: bytes) -> bool:
        """校验长度、帧头、帧尾和 SUM。

        协议文档规定 SUM 排除帧头、SUM 自身和帧尾，但包含帧序号。因此
        首选从两个帧头字节之后开始累加；同时兼容旧版实现中从
        ``header_len_bytes`` 开始、排除帧序号的算法。
        """
        expected_len = int(self.frame_len_bytes)
        if len(frame) != expected_len:
            return False
        if len(frame) < 2 or frame[:2] != self.header:
            return False
        if int(self.tail_len_bytes) < 1 or frame[-1] != (int(self.tail_byte) & 0xFF):
            return False

        checksum_offset = self.checksum_offset
        if checksum_offset is None:
            return True
        if int(self.checksum_len_bytes) != 1:
            raise ValueError("当前协议仅支持 1 字节 SUM")
        # 协议文档规定序号也参与 SUM。部分旧版固件/上位机实现曾从
        # header_len_bytes 开始累加（把序号排除在外），这里保留兼容校验，
        # 否则帧长虽正确，所有 EEG/PPG 帧仍会在组帧阶段被静默丢弃。
        actual_sum = int(frame[checksum_offset])
        expected_sum = sum(frame[2:checksum_offset]) & 0xFF
        if actual_sum == expected_sum:
            return True
        if int(self.header_len_bytes) > 2:
            legacy_sum = sum(frame[int(self.header_len_bytes):checksum_offset]) & 0xFF
            return actual_sum == legacy_sum
        return False


@dataclass(frozen=True)
class FrameDecodeBatch:
    """一次 BLE 通知中解出的完整帧及重新同步统计。"""

    frames: Tuple[bytes, ...]
    invalid_frames: int = 0
    dropped_bytes: int = 0


class FrameStreamDecoder:
    """处理任意分片、粘包和损坏数据的增量 EEG 帧解码器。"""

    # 保留旧版公开常量，便于外部诊断代码引用；实际解码使用 spec.header。
    _HEADER = b"\xAA\xBB"

    def __init__(self, spec: FrameSpec) -> None:
        self.spec = spec
        self._header = spec.header
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()

    def feed(self, data: bytes) -> FrameDecodeBatch:
        """追加一个 BLE 通知并返回其中所有完整有效帧。"""
        if data:
            self._buffer.extend(data)

        frames: List[bytes] = []
        invalid_frames = 0
        dropped_bytes = 0
        expected = int(self.spec.frame_len_bytes)
        header_len = len(self._header)

        while len(self._buffer) >= header_len:
            header_pos = self._buffer.find(self._header)
            if header_pos < 0:
                # 保留可能跨通知的最后一个 AA。
                keep = 1 if self._buffer[-1:] == self._header[:1] else 0
                drop_count = len(self._buffer) - keep
                if drop_count:
                    del self._buffer[:drop_count]
                    dropped_bytes += drop_count
                break

            if header_pos > 0:
                del self._buffer[:header_pos]
                dropped_bytes += header_pos

            if len(self._buffer) < expected:
                break

            candidate = bytes(self._buffer[:expected])
            if self.spec.validate_checksum(candidate):
                frames.append(candidate)
                del self._buffer[:expected]
                continue

            # 当前头可能属于损坏帧；前进一个字节，寻找下一个真实帧头。
            invalid_frames += 1
            dropped_bytes += 1
            del self._buffer[:1]

        return FrameDecodeBatch(tuple(frames), invalid_frames, dropped_bytes)


def _read_uint24(value: bytes) -> int:
    """读取一个大端 24-bit 无符号整数。"""
    if len(value) != 3:
        raise ValueError("24-bit 字段必须恰好为 3 bytes")
    return int.from_bytes(value, byteorder="big", signed=False)


def parse_imu(imu_bytes: bytes) -> Dict[str, int]:
    """解析兼容旧协议的 IMU 六轴数据。"""
    valid = imu_bytes[:12]
    if len(valid) < 12:
        return {}
    return {
        "gyro_x": int.from_bytes(valid[0:2], byteorder="big", signed=True),
        "gyro_y": int.from_bytes(valid[2:4], byteorder="big", signed=True),
        "gyro_z": int.from_bytes(valid[4:6], byteorder="big", signed=True),
        "accel_x": int.from_bytes(valid[6:8], byteorder="big", signed=True),
        "accel_y": int.from_bytes(valid[8:10], byteorder="big", signed=True),
        "accel_z": int.from_bytes(valid[10:12], byteorder="big", signed=True),
    }


def parse_ppg(ppg_bytes: bytes) -> Dict[str, Any]:
    """解析 10-byte PPG 段。"""
    if len(ppg_bytes) < 10:
        return {}
    marker = int(ppg_bytes[0])
    return {
        "marker": marker,
        "valid": marker == 0x00,
        "green": _read_uint24(ppg_bytes[1:4]),
        "red": _read_uint24(ppg_bytes[4:7]),
        "infrared": _read_uint24(ppg_bytes[7:10]),
        # 使用十六进制字符串，结构化结果可直接通过状态 API 序列化。
        "raw": bytes(ppg_bytes[:10]).hex(),
    }


def parse_trigger_per_sample(trig_bytes: bytes, samples_per_frame: int) -> List[float]:
    """将 Trigger 低 5 位按低位到高位展开为逐采样点标记。"""
    if samples_per_frame <= 0:
        return []
    trigger_byte = int(trig_bytes[0]) if trig_bytes else 0
    trigger_byte &= 0x1F
    return [
        float((trigger_byte >> frame_idx) & 0x01) if frame_idx < 5 else 0.0
        for frame_idx in range(int(samples_per_frame))
    ]


@dataclass(frozen=True)
class EegFrame:
    """完整 EEG 帧的结构化解码结果。"""

    sequence: Optional[int]
    samples: List[List[float]]
    battery_level: Optional[int]
    ppg: Dict[str, Any]
    reserved: bytes
    imu: Dict[str, int]
    trigger_byte: int


def parse_frame(frame: bytes, spec: FrameSpec) -> EegFrame:
    """校验并解析一帧，保留协议中所有非 EEG 字段。"""
    if not spec.validate_checksum(frame):
        raise ValueError("invalid EEG frame length, header, tail, or checksum")

    cursor = int(spec.header_len_bytes)
    eeg_end = cursor + spec.eeg_payload_len_bytes
    eeg_bytes = frame[cursor:eeg_end]
    cursor = eeg_end

    trig_end = cursor + int(spec.trigger_len_bytes)
    trig_bytes = frame[cursor:trig_end]
    cursor = trig_end

    ppg_end = cursor + int(spec.ppg_len_bytes)
    ppg_bytes = frame[cursor:ppg_end]
    cursor = ppg_end

    reserved_end = cursor + int(spec.reserved_len_bytes)
    reserved = bytes(frame[cursor:reserved_end])
    cursor = reserved_end

    imu_end = cursor + int(spec.imu_len_bytes)
    imu = parse_imu(frame[cursor:imu_end])
    cursor = imu_end

    battery_end = cursor + int(spec.battery_len_bytes)
    battery_bytes = frame[cursor:battery_end]
    battery_level: Optional[int] = None
    if len(battery_bytes) == int(spec.battery_len_bytes) and int(spec.battery_len_bytes) > 0:
        raw_battery = int.from_bytes(battery_bytes, byteorder="big", signed=False)
        if 0 <= raw_battery <= 100:
            battery_level = raw_battery

    trigger_per_sample = parse_trigger_per_sample(trig_bytes, spec.samples_per_frame)
    bytes_per_sample_all_channels = int(spec.channels) * int(spec.bytes_per_sample_per_channel)
    samples: List[List[float]] = []
    for frame_idx in range(int(spec.samples_per_frame)):
        base = frame_idx * bytes_per_sample_all_channels
        sample: List[float] = []
        for ch_idx in range(int(spec.channels)):
            start = base + ch_idx * int(spec.bytes_per_sample_per_channel)
            end = start + int(spec.bytes_per_sample_per_channel)
            raw = int.from_bytes(eeg_bytes[start:end], byteorder="big", signed=True)
            sample.append(float(raw))
        sample.append(float(trigger_per_sample[frame_idx]))
        samples.append(sample)

    return EegFrame(
        sequence=(int(frame[2]) if int(spec.header_len_bytes) >= 3 else None),
        samples=samples,
        battery_level=battery_level,
        ppg=parse_ppg(ppg_bytes),
        reserved=reserved,
        imu=imu,
        trigger_byte=(int(trig_bytes[0]) if trig_bytes else 0),
    )


def parse_frame_to_samples(
    frame: bytes,
    spec: FrameSpec,
) -> Tuple[List[List[float]], Optional[int], Dict[str, int]]:
    """兼容旧调用方；完整 PPG/预留字段请使用 :func:`parse_frame`。"""
    decoded = parse_frame(frame, spec)
    return decoded.samples, decoded.battery_level, decoded.imu
